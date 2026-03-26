from datetime import datetime
from pathlib import Path

import backoff
import click
import css_inline
import dateparser
import funcy_pipe as fp
import html2text
import jinja2
import requests
from decouple import Csv, config
from lxml import etree
from markdown import markdown
from structlog_config import configure_logger
from whenever import Instant, ZonedDateTime

from .feed import Entry, rss as feed_rss, discourse as feed_discourse
from .readwise import (
    ReadwiseArticle,
    get_last_readwise_check,
    get_readwise_articles,
    update_last_readwise_check,
)
from .subject_generation import generate_subject_line
from .summarize_github import (
    fetch_github_activity,
    generate_summary_prompt,
    summarize_with_gemini,
)

log = configure_logger()

ROOT_DIRECTORY = Path(__file__).parent.parent.resolve()
DATA_DIRECTORY = ROOT_DIRECTORY / "data"
FEED_ENTRY_LINKS_FILE = DATA_DIRECTORY / "processed_links.txt"
CONTENT_TEMPLATE_FILE = DATA_DIRECTORY / config("TEMPLATE_FILE", default="template.j2")
GITHUB_LAST_CHECKED_FILE = DATA_DIRECTORY / "last_github_checked.txt"

RSS_URL = config("RSS_URL", default=None)
DISCOURSE_JSON_URL = config("DISCOURSE_JSON_URL", default=None)

LISTMONK_URL = config("LISTMONK_URL", cast=str)
LISTMONK_USERNAME = config("LISTMONK_USERNAME", cast=str)
LISTMONK_API_TOKEN = config("LISTMONK_API_TOKEN", cast=str)
LISTMONK_TITLE = config("LISTMONK_TITLE", cast=str)

LISTMONK_TEMPLATE = config("LISTMONK_TEMPLATE", cast=int, default=None)
FEED_MAX_ITEMS = config("FEED_MAX_ITEMS", cast=int, default=5)
LISTMONK_LISTS = config("LISTMONK_LISTS", cast=Csv(cast=int))
LISTMONK_SEND_AT = config("LISTMONK_SEND_AT", cast=str, default=None)
LISTMONK_TEST_EMAILS = config("LISTMONK_TEST_EMAILS", cast=Csv(), default=None)
LISTMONK_GEMINI_SUBJECT = config("LISTMONK_GEMINI_SUBJECT", cast=bool, default=False)

BACKOFF_LIMIT = 8

LISTMONK_REQUEST_HEADERS = {
    "Content-Type": "application/json;charset=utf-8",
    "Authorization": f"token {LISTMONK_USERNAME}:{LISTMONK_API_TOKEN}",
}


def ensure_data_resources() -> None:
    DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)

    if not FEED_ENTRY_LINKS_FILE.exists():
        FEED_ENTRY_LINKS_FILE.write_text("", encoding="utf-8")

    if not GITHUB_LAST_CHECKED_FILE.exists():
        GITHUB_LAST_CHECKED_FILE.write_text("", encoding="utf-8")


ensure_data_resources()


def should_abort_retry(exc: Exception) -> bool:
    """Stop retrying immediately when Listmonk returns an auth error."""
    if not isinstance(exc, requests.exceptions.HTTPError):
        return False

    response = exc.response

    if response is None:
        return False

    if response.status_code != 403:
        return False

    log.error("listmonk authentication failed", status_code=response.status_code)

    return True


def read_last_github_checked(default_days: int) -> str:
    if GITHUB_LAST_CHECKED_FILE.exists():
        stored = GITHUB_LAST_CHECKED_FILE.read_text(encoding="utf-8").strip()
        if stored:
            return stored

    log.info("github last checked missing", default_days=default_days)

    return (
        Instant.now()
        .to_system_tz()
        .add(days=-default_days)
        .format_iso()
    )


def write_last_github_checked(timestamp: str) -> None:
    GITHUB_LAST_CHECKED_FILE.write_text(timestamp, encoding="utf-8")


def build_github_summary_html() -> tuple[str | None, str | None]:
    github_token = config("GITHUB_TOKEN", default=None)
    google_api_key = config("GOOGLE_API_KEY", default=None)

    if not github_token or not google_api_key:
        log.info(
            "github summary skipped",
            github_token_present=github_token is not None,
            google_api_key_present=google_api_key is not None,
        )
        return None, None

    username = config("GITHUB_USERNAME")
    days = config("GITHUB_SUMMARY_DAYS", cast=int, default=30)

    log.info("generating github summary", username=username, days=days)

    last_checked = read_last_github_checked(days)

    activity = fetch_github_activity(username, last_checked)

    next_checkpoint = (
        Instant.now()
        .to_system_tz()
        .format_iso()
    )

    if not activity.get("releases") and not activity.get("new_repos"):
        log.info("github summary skipped", reason="no_activity")
        return None, next_checkpoint

    prompt = generate_summary_prompt(activity, username)
    summary_markdown = summarize_with_gemini(prompt)

    summary_html = markdown(summary_markdown)

    log.info("github summary generated")

    return summary_html, next_checkpoint


def build_readwise_articles() -> tuple[list[ReadwiseArticle], ZonedDateTime | None]:
    readwise_token = config("READWISE_API_TOKEN", default=None)
    readwise_tag = config("READWISE_TAG", default=None)

    if not readwise_token or not readwise_tag:
        log.error(
            "no token or tag, readwise articles skipped",
            readwise_token_present=readwise_token is not None,
            readwise_tag_present=readwise_tag is not None,
        )
        return [], None

    summary_days = config("READWISE_SUMMARY_DAYS", cast=int, default=30)

    log.info("fetching readwise articles", tag=readwise_tag, summary_days=summary_days)

    last_checked = get_last_readwise_check()

    articles = get_readwise_articles(
        token=readwise_token,
        tag=readwise_tag,
        since=last_checked,
        lookback_days=summary_days
    )

    if articles:
        log.info("readwise articles fetched", count=len(articles))
        return articles, Instant.now().to_system_tz()

    return articles, None


def is_first_feed_entry_run() -> bool:
    if not FEED_ENTRY_LINKS_FILE.exists():
        log.info(
            "Feed entry links file does not exist: "
            f"{FEED_ENTRY_LINKS_FILE}. Populating it for the first time..."
        )

        return True

    if not FEED_ENTRY_LINKS_FILE.read_text(encoding="utf-8").strip():
        log.info(
            "Feed entry links file does not exist: "
            f"{FEED_ENTRY_LINKS_FILE}. Populating it for the first time..."
        )

        return True

    return False


def read_feed_entry_links_file() -> list[str]:
    if not FEED_ENTRY_LINKS_FILE.exists():
        return []

    content = FEED_ENTRY_LINKS_FILE.read_text(encoding="utf-8").strip()

    if not content:
        return []

    return content.split("\n")


def append_new_feed_links(existing_links: list[str], new_entries: list[Entry]) -> None:
    known_links = set(existing_links)

    additions = []

    # TODO funcy could clear this up :/
    for entry in new_entries:
        link = str(entry.link)

        if link in known_links:
            continue

        known_links.add(link)
        additions.append(link)

    if not additions:
        return

    updated_links = [*existing_links, *additions]

    FEED_ENTRY_LINKS_FILE.write_text("\n".join(updated_links), encoding="utf-8")


def write_feed_entry_links(entry_links: list[str]) -> None:
    FEED_ENTRY_LINKS_FILE.write_text("\n".join(entry_links), encoding="utf-8")


@backoff.on_exception(
    backoff.expo,
    # highest exception up the exception hierarchy
    (requests.exceptions.RequestException),
    max_tries=BACKOFF_LIMIT,
)
def get_og_image(url: str) -> str | None:
    "Pull the image tied to a blog post"

    html = requests.get(url).content

    tree = etree.fromstring(html, etree.HTMLParser())

    og_image = tree.find("head/meta[@property='og:image']")

    if og_image is not None:
        return og_image.get("content")


@backoff.on_exception(
    backoff.expo,
    (requests.exceptions.RequestException),
    max_tries=BACKOFF_LIMIT,
    giveup=should_abort_retry,
)
def create_campaign(title: str, body: str) -> int:
    send_at = None

    if LISTMONK_SEND_AT:
        parsed_date = dateparser.parse(
            LISTMONK_SEND_AT, settings={"PREFER_DATES_FROM": "future"}
        )

        # from listmonk docs:
        # Timestamp to schedule campaign. Format: 'YYYY-MM-DDTHH:MM:SS'.

        if parsed_date is None:
            raise ValueError("LISTMONK_SEND_AT could not be parsed")

        send_at = parsed_date.strftime("%Y-%m-%dT%H:%M:%SZ")

    # https://listmonk.app/docs/apis/campaigns/#post-apicampaigns
    json_data = {
        "name": title,
        "subject": title,
        "lists": LISTMONK_LISTS,
        "content_type": "html",
        "body": body,
        "send_at": send_at,
        "altbody": html2text.html2text(body),
        "template_id": LISTMONK_TEMPLATE,
        "messenger": "email",
        "type": "regular",
        "tags": ["listmonk-newsletter"],
        "archive": True,
    }


    response = requests.post(
        f"{LISTMONK_URL}/api/campaigns",
        json=json_data,
        headers=LISTMONK_REQUEST_HEADERS,
    )

    if not response.ok:
        log.error("create_campaign failed", status=response.status_code, body=response.text)

    response.raise_for_status()

    return response.json()["data"]["id"]


@backoff.on_exception(
    backoff.expo,
    (requests.exceptions.RequestException),
    max_tries=BACKOFF_LIMIT,
    giveup=should_abort_retry,
)
def send_tests(campaign_id: int, emails: list[str]):
    """
    Note that all emails must be in the list attached to the campaign, which is a weird requirement
    """
    response = requests.post(
        f"{LISTMONK_URL}/api/campaigns/{campaign_id}/test",
        json={
            "subscribers": emails,
        },
        headers=LISTMONK_REQUEST_HEADERS,
    )

    if not response.ok:
        log.error("send_tests failed", status=response.status_code, body=response.text)

    response.raise_for_status()


@backoff.on_exception(
    backoff.expo,
    (requests.exceptions.RequestException),
    max_tries=BACKOFF_LIMIT,
    giveup=should_abort_retry,
)
def start_campaign(campaign_id: int) -> bool:
    status = "scheduled" if LISTMONK_SEND_AT else "running"

    response = requests.put(
        f"{LISTMONK_URL}/api/campaigns/{campaign_id}/status",
        json={"status": status},
        headers=LISTMONK_REQUEST_HEADERS,
    )

    if not response.ok:
        log.error("start_campaign failed", status=response.status_code, body=response.text)

    response.raise_for_status()

    return response.status_code == 200


def render_email_content(
    new_entries: list[Entry],
    github_summary: str | None,
    readwise_articles: list[ReadwiseArticle],
) -> str:
    # Create a Jinja2 environment and load the template file
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(searchpath=str(ROOT_DIRECTORY)),
        autoescape=jinja2.select_autoescape(["html", "xml"]),
    )

    template = env.get_template(str(CONTENT_TEMPLATE_FILE.relative_to(ROOT_DIRECTORY)))
    rendered_content = template.render(
        entries=new_entries,
        github_summary=github_summary,
        readwise_articles=readwise_articles,
    )

    inliner = css_inline.CSSInliner(keep_style_tags=True)
    inlined_content = inliner.inline(rendered_content)

    return inlined_content


def generate_campaign():
    assert RSS_URL or DISCOURSE_JSON_URL, "Either RSS_URL or DISCOURSE_JSON_URL must be set"

    if DISCOURSE_JSON_URL:
        log.info("pulling discourse feed", feed_url=DISCOURSE_JSON_URL)
        all_entries = feed_discourse.fetch_entries(DISCOURSE_JSON_URL)
    else:
        log.info("pulling rss feed", feed_url=RSS_URL)
        all_entries = feed_rss.fetch_entries(RSS_URL)

    if not all_entries:
        return

    # On first run (feed_entry_links.txt does not exist)
    entry_links = [str(entry.link) for entry in all_entries]

    first_run = is_first_feed_entry_run()

    entry_links_last_update = read_feed_entry_links_file()

    log.info(
        "checking for new feed entries",
        existing_entries=len(entry_links_last_update),
        feed_entries=len(all_entries),
    )

    def add_og_image(entry):
        if not entry.get("image"):
            entry["image"] = get_og_image(entry.link)
        return entry

    if first_run:
        log.info("first run, including recent entries", count=min(FEED_MAX_ITEMS, len(all_entries)))

        new_entries = (
            all_entries[:FEED_MAX_ITEMS][::-1]
            | fp.lmap(add_og_image)
        )
    else:
        new_entries = (
            all_entries
            | fp.filter(lambda e: e.link not in entry_links_last_update)
            | fp.lmap(add_og_image)
        )

    new_entries = list(new_entries)

    if not new_entries:
        log.info("no new entries found")
        return

    log.info("new entries found", count=len(new_entries))

    github_summary_html, github_checkpoint = build_github_summary_html()
    readwise_articles, readwise_checkpoint = build_readwise_articles()

    content = render_email_content(
        new_entries,
        github_summary_html,
        readwise_articles,
    )

    subject_line = datetime.now().strftime(LISTMONK_TITLE)

    if LISTMONK_GEMINI_SUBJECT:
        entries_for_subject = [
            {
                "title": str(entry.get("title", "")),
                "summary": str(entry.get("summary") or entry.get("description", "")),
                "link": str(entry.get("link", "")),
            }
            for entry in new_entries
        ]

        subject_line = generate_subject_line(
            subject_line,
            entries_for_subject,
            github_summary_html,
        )

    content = render_email_content(new_entries, github_summary_html, readwise_articles)
    campaign_id = create_campaign(subject_line, content)

    if LISTMONK_TEST_EMAILS:
        send_tests(campaign_id, LISTMONK_TEST_EMAILS)
        send_successful = True
    else:
        send_successful = start_campaign(campaign_id)

    if send_successful:
        log.info("campaign scheduled successfully, updating inspected feed links")

        if github_checkpoint:
            write_last_github_checked(github_checkpoint)

        if readwise_checkpoint:
            update_last_readwise_check(readwise_checkpoint)

        if first_run:
            write_feed_entry_links(entry_links)
        else:
            append_new_feed_links(entry_links_last_update, new_entries)


@click.command()
def cli():
    """
    Generate a newsletter campaign from new articles in a blog's RSS feed and schedule it for sending
    """
    generate_campaign()
