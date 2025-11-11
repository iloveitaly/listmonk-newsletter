import os
from pathlib import Path

import backoff
import click
import css_inline
import dateparser
import feedparser
import funcy_pipe as fp
import html2text
import jinja2
import requests
from decouple import Csv, config
from lxml import etree
from markdown import markdown
from structlog_config import configure_logger
from whenever import Instant

from .summarize_github import (
    fetch_github_activity,
    generate_summary_prompt,
    summarize_with_gemini,
)

log = configure_logger()

ROOT_DIRECTORY = Path(__file__).parent.parent.resolve()
DATA_DIRECTORY = ROOT_DIRECTORY / "data"
FEED_ENTRY_LINKS_FILE = DATA_DIRECTORY / "processed_links.txt"
CONTENT_TEMPLATE_FILE = DATA_DIRECTORY / "template.j2"
GITHUB_LAST_CHECKED_FILE = DATA_DIRECTORY / "last_github_checked.txt"

RSS_URL = config("RSS_URL", cast=str)

LISTMONK_URL = config("LISTMONK_URL", cast=str)
LISTMONK_USERNAME = config("LISTMONK_USERNAME", cast=str)
LISTMONK_API_TOKEN = config("LISTMONK_API_TOKEN", cast=str)
LISTMONK_TITLE = config("LISTMONK_TITLE", cast=str)

LISTMONK_TEMPLATE = config("LISTMONK_TEMPLATE", cast=int, default=None)
LISTMONK_SEND_AT = config("LISTMONK_SEND_AT", cast=str, default=None)
LISTMONK_TEST_EMAILS = config("LISTMONK_TEST_EMAILS", cast=Csv(), default=None)

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


def build_github_summary_html() -> str | None:
    github_token = config("GITHUB_TOKEN", default=None)
    google_api_key = config("GOOGLE_API_KEY", default=None)

    if not github_token or not google_api_key:
        log.info(
            "github summary skipped",
            github_token_present=github_token is not None,
            google_api_key_present=google_api_key is not None,
        )
        return None

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
        write_last_github_checked(next_checkpoint)
        return None

    prompt = generate_summary_prompt(activity, username)
    summary_markdown = summarize_with_gemini(prompt)

    summary_html = markdown(summary_markdown)

    log.info("github summary generated")

    write_last_github_checked(next_checkpoint)

    return summary_html


def populate_preexisting_entries(entry_links: list[str]) -> bool:
    if not os.path.exists(FEED_ENTRY_LINKS_FILE):
        log.info(
            "Feed entry links file does not exist: "
            f"{FEED_ENTRY_LINKS_FILE}. Populating it for the first time..."
        )

        FEED_ENTRY_LINKS_FILE.write_text("\n".join(entry_links), encoding="utf-8")

        return True

    if not FEED_ENTRY_LINKS_FILE.read_text(encoding="utf-8").strip():
        log.info(
            "Feed entry links file does not exist: "
            f"{FEED_ENTRY_LINKS_FILE}. Populating it for the first time..."
        )

        FEED_ENTRY_LINKS_FILE.write_text("\n".join(entry_links), encoding="utf-8")

        return True

    return False


def read_feed_entry_links_file() -> list[str]:
    content = FEED_ENTRY_LINKS_FILE.read_text(encoding="utf-8").strip()

    if not content:
        return []

    return content.split("\n")


def append_new_feed_links(existing_links: list[str], new_entries: list[feedparser.FeedParserDict]) -> None:
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
        # TODO list reference should be dynamic
        "lists": [1],
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

    response.raise_for_status()


@backoff.on_exception(
    backoff.expo,
    (requests.exceptions.RequestException),
    max_tries=BACKOFF_LIMIT,
    giveup=should_abort_retry,
)
def start_campaign(campaign_id: int) -> bool:
    response = requests.put(
        f"{LISTMONK_URL}/api/campaigns/{campaign_id}/status",
        json={"status": "scheduled"},
        headers=LISTMONK_REQUEST_HEADERS,
    )

    response.raise_for_status()

    return response.status_code == 200


def render_email_content(
    new_entries: list[feedparser.FeedParserDict],
    github_summary: str | None,
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
    )

    inliner = css_inline.CSSInliner(keep_style_tags=True)
    inlined_content = inliner.inline(rendered_content)

    return inlined_content


def generate_campaign():
    log.info("pulling feed", feed_url=RSS_URL)

    feed: feedparser.FeedParserDict = feedparser.parse(RSS_URL)

    # strange way to report an error...
    if "bozo_exception" in feed:
        log.error("Feed parsing error", error=feed["bozo_exception"])
        return

    if not feed:
        log.error("feed is empty")
        return

    # Sort feed entries chronologically
    feed.entries.reverse()

    # On first run (feed_entry_links.txt does not exist)
    entry_links = [str(entry.link) for entry in feed.entries]

    first_run = populate_preexisting_entries(entry_links)

    entry_links_last_update = read_feed_entry_links_file()

    log.info(
        "checking for new feed entries",
        existing_entries=len(entry_links_last_update),
        feed_entries=len(feed.entries),
    )

    def add_image_link(entry):
        og_image = get_og_image(entry.link)
        entry["image"] = og_image
        return entry

    if first_run:
        log.info("first run, including recent entries", count=min(5, len(feed.entries)))

        new_entries = (
            feed.entries[-5:]
            | fp.lmap(add_image_link)
        )
    else:
        new_entries = (
            feed.entries
            | fp.filter(lambda e: e.link not in entry_links_last_update)
            | fp.lmap(add_image_link)
        )

    if not new_entries:
        log.info("no new entries found")
        return

    log.info("new entries found", count=len(new_entries))

    github_summary_html = build_github_summary_html()

    content = render_email_content(new_entries, github_summary_html)
    campaign_id = create_campaign(LISTMONK_TITLE, content)

    send_successful = start_campaign(campaign_id)

    if send_successful:
        # TODO this is currently broken on the listmonk side
        # TODO this isn't going to be fixed, should fix the payload being sent tot he
        # if LISTMONK_TEST_EMAILS:
        #     send_tests(campaign_id, LISTMONK_TEST_EMAILS)

        log.info("campaign scheduled successfully, updating inspected feed links")

        append_new_feed_links(entry_links_last_update, list(new_entries))


@click.command()
def cli():
    """
    Generate a newsletter campaign from new articles in a blog's RSS feed and schedule it for sending
    """
    generate_campaign()
