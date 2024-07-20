import time
import os
from pathlib import Path
import re
from collections.abc import Iterator
import backoff
import requests
import feedparser
from lxml import etree
from decouple import Csv, config
import structlog

# side effects! for append_text
import listmonk_newsletter.pathlib_extension
from listmonk_newsletter.util import configure_logger

log = structlog.get_logger()
configure_logger()

PROGRAM_DIRECTORY = Path(__file__).parent.resolve()

# Configuration variables
ROOT_URL = config("BLOG_URL", cast=str)
RSS_URL = f"{ROOT_URL}/feed/"
FEED_ENTRY_LINKS_FILE = PROGRAM_DIRECTORY / "feed_entry_links.txt"
CONTENT_TEMPLATE_FILE = PROGRAM_DIRECTORY / "template.j2"

DRY_RUN = True  # Use this for testing, campaigns will be created but no emails will be sent out

LISTMONK_URL = config("LISTMONK_URL", cast=str)
LISTMONK_USERNAME = config("LISTMONK_USERNAME", cast=str)
LISTMONK_PASSWORD = config("LISTMONK_PASSWORD", cast=str)
LISTMONK_TITLE = config("LISTMONK_TITLE", cast=str)

LISTMONK_TEMPLATE = config("LISTMONK_TEMPLATE", cast=int, default=None)
LISTMONK_SEND_AT = config("LISTMONK_SEND_AT", cast=str, default=None)
LISTMONK_TEST_EMAILS = config("LISTMONK_TEST_EMAILS", cast=Csv(), default=None)

BACKOFF_LIMIT = 1

LISTMONK_REQUEST_PARAMS = {
    "headers": {"Content-Type": "application/json;charset=utf-8"},
    "auth": (LISTMONK_USERNAME, LISTMONK_PASSWORD),
}


def fetch_rss() -> feedparser.FeedParserDict | None:
    feed = feedparser.parse(RSS_URL)
    return feed


def populate_preexisting_entries(entry_links: list[str]) -> bool:
    """Don't process feed entries that existed before first run of rss2newsletter"""

    if not os.path.exists(FEED_ENTRY_LINKS_FILE):
        log.info(
            "Feed entry links file does not exist:"
            f"{FEED_ENTRY_LINKS_FILE}. Populating it for the first time..."
        )

        FEED_ENTRY_LINKS_FILE.write_text("\n".join(entry_links), encoding="utf-8")
        return True

    log.info("Checking for new feed entries...")
    return False


def read_feed_entry_links_file() -> list[str]:
    return FEED_ENTRY_LINKS_FILE.read_text(encoding="utf-8").strip("\n").split("\n")


@backoff.on_exception(
    backoff.expo,
    # highest exception up the execption hierarchy
    (requests.exceptions.RequestException),
    max_tries=BACKOFF_LIMIT,
)
def get_og_image(url: str) -> str | None:
    """Get Open Graph cover image URL from given HTML"""

    html = requests.get(url).content

    tree = etree.fromstring(html, etree.HTMLParser())

    og_image = tree.find("head/meta[@property='og:image']")

    if og_image is not None:
        return og_image.get("content")


import html2text

import dateparser


@backoff.on_exception(
    backoff.expo,
    # highest exception up the execption hierarchy
    (requests.exceptions.RequestException),
    max_tries=BACKOFF_LIMIT,
)
def create_campaign(title: str, body: str) -> int:
    send_at = None

    if LISTMONK_SEND_AT:
        parsed_date = dateparser.parse(
            LISTMONK_SEND_AT, settings={"PREFER_DATES_FROM": "future"}
        )

        # from listmonk docs:
        # Timestamp to schedule campaign. Format: 'YYYY-MM-DDTHH:MM:SS'.

        send_at = parsed_date.strftime("%Y-%m-%dT%H:%M:%SZ")

    # https://listmonk.app/docs/apis/campaigns/#post-apicampaigns
    json_data = {
        "name": title,
        "subject": title,
        "lists": [1],
        "content_type": "html",
        "body": body,
        "send_at": send_at,
        "altbody": html2text.html2text(body),
        "template_id": LISTMONK_TEMPLATE,
        "messenger": "email",
        "type": "regular",
        "tags": ["listmonk-newsletter"],
    }

    response = requests.post(
        f"{LISTMONK_URL}/api/campaigns", json=json_data, **LISTMONK_REQUEST_PARAMS
    )

    return response.json()["data"]["id"]


# TODO this is broken on the listmonk side of things
@backoff.on_exception(
    backoff.expo,
    # highest exception up the execption hierarchy
    (requests.exceptions.RequestException),
    max_tries=BACKOFF_LIMIT,
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
        **LISTMONK_REQUEST_PARAMS,
    )

    response.raise_for_status()


@backoff.on_exception(
    backoff.expo,
    # highest exception up the execption hierarchy
    (requests.exceptions.RequestException),
    max_tries=BACKOFF_LIMIT,
)
def start_campaign(campaign_id: int) -> bool:
    response = requests.put(
        f"{LISTMONK_URL}/api/campaigns/{campaign_id}/status",
        json={"status": "scheduled"},
        **LISTMONK_REQUEST_PARAMS,
    )

    response.raise_for_status()

    return response.status_code == 200


import jinja2


import css_inline


def render_email_content(new_entries: list[feedparser.FeedParserDict]) -> str:
    # Create a Jinja2 environment and load the template file
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(searchpath=str(PROGRAM_DIRECTORY)),
        autoescape=jinja2.select_autoescape(["html", "xml"]),
    )

    template = env.get_template(str("template.j2"))
    rendered_content = template.render(entries=new_entries)

    inliner = css_inline.CSSInliner(keep_style_tags=True)
    inlined_content = inliner.inline(rendered_content)

    return inlined_content


import funcy_pipe as fp


def generate_campaign():
    feed = fetch_rss()

    if not feed:
        return

    # Sort feed entries chronologically
    feed.entries.reverse()

    # On first run (feed_entry_links.txt does not exist)
    if populate_preexisting_entries((e.link for e in feed.entries)):
        log.info("first run, assuming all entries are new")
        return

    entry_links_last_update = read_feed_entry_links_file()

    def add_image_link(entry):
        og_image = get_og_image(entry.link)
        entry["image"] = og_image
        return entry

    new_entries = (
        feed.entries
        | fp.filter(lambda e: e.link not in entry_links_last_update)
        | fp.map(add_image_link)
        | fp.to_list()
    )

    if not new_entries:
        log.info("no new entries found")
        return

    content = render_email_content(new_entries)
    campaign_id = create_campaign(LISTMONK_TITLE, content)

    send_successful = start_campaign(campaign_id)

    if send_successful:
        # TODO this is currently broken on the listmonk side
        # if LISTMONK_TEST_EMAILS:
        #     send_tests(campaign_id, LISTMONK_TEST_EMAILS)

        # TODO should really have a `exec()` or something in fp for this use case
        new_entries | fp.pluck_attr("link") | fp.map(lambda t: "\n" + t) | fp.map(
            FEED_ENTRY_LINKS_FILE.append_text
        ) | fp.to_list()


if __name__ == "__main__":
    generate_campaign()
