#!/usr/bin/python3

# Copyright (C) 2023 Elliot Killick <contact@elliotkillick.com>
# Licensed under the MIT License. See LICENSE file for details.

"""
Convert RSS to email newsletters and send them using the Listmonk API automatically

On first run, we generate the feed_entry_links.txt file to start keeping track of new feed entries.

On every run after that, we read feed_entry_links.txt checking for new entries since the previous
read. If new entries are detected, we create and start a Listmonk campaign.

We use absolute article links as the unique identifier for each entry.
"""

# pylint: disable=invalid-name

import time
import os
from pathlib import Path
import re
from collections.abc import Iterator
import requests
import feedparser
from lxml import etree

PROGRAM_DIRECTORY = Path(__file__).parent.resolve()

# Configuration variables
ROOT_URL = "https://elliotonsecurity.com"
RSS_URL = f"{ROOT_URL}/atom.xml"
FEED_ENTRY_LINKS_FILE = PROGRAM_DIRECTORY / "feed_entry_links.txt"
CONTENT_TEMPLATE_FILE = PROGRAM_DIRECTORY / "content_template.html"
POLL_INTERVAL = 3 * 60  # Consider using a cron job or systemd timer instead
DRY_RUN = False  # Use this for testing, campaigns will be created but no emails will be sent out

LISTMONK_URL = "https://ping.elliotonsecurity.com:8443"
LISTMONK_USERNAME = "elliotkillick"
LISTMONK_PASSWORD = "PASSWORD_HERE"


def main():
    """Program entry point"""

    banner()

    while True:
        main_loop()
        time.sleep(POLL_INTERVAL)


def banner():
    """Print program banner"""

    print(
        "\n"
        "               d88b                             8        w    w\n"
        '8d8b d88b d88b " dP 8d8b. .d88b Yb  db  dP d88b 8 .d88b w8ww w8ww .d88b 8d8b\n'
        "8P   `Yb. `Yb.  dP  8P Y8 8.dP'  YbdPYbdP  `Yb. 8 8.dP'  8    8   8.dP' 8P\n"
        "8    Y88P Y88P d888 8   8 `Y88P   YP  YP   Y88P 8 `Y88P  Y8P  Y8P `Y88P 8\n"
        "... by @ElliotKillick\n"
    )


def main_loop():
    """Program infinite loop"""

    feed = fetch_rss()
    if not feed:
        return

    # Sort feed entries chronologically
    feed.entries.reverse()

    # On first run (feed_entry_links.txt does not exist)
    if populate_preexisting_entries((e.link for e in feed.entries)):
        return

    entry_links_last_update = read_feed_entry_links_file()
    for new_entry in check_for_new_entries(entry_links_last_update, feed.entries):
        campaign_id = create_newsletter(new_entry.link, new_entry.title)
        send_successful = send_newsletter(campaign_id)
        if send_successful:
            update_feed_entry_links_file(new_entry.link)


def fetch_rss() -> feedparser.FeedParserDict | None:
    """Fetch and parse RSS"""

    feed = feedparser.parse(RSS_URL)
    # In case of failure
    if hasattr(feed, "bozo_exception"):
        print(f"Error fetching RSS from: {RSS_URL}")
        return None

    return feed


def populate_preexisting_entries(entry_links: list[str]) -> bool:
    """Don't process feed entries that existed before first run of rss2newsletter"""

    if not os.path.exists(FEED_ENTRY_LINKS_FILE):
        print(
            "Feed entry links file does not exist:"
            f"{FEED_ENTRY_LINKS_FILE}. Populating it for the first time..."
        )
        create_feed_entry_links_file(entry_links)
        return True

    print("Checking for new feed entries...")
    return False


def create_feed_entry_links_file(entry_links: list[str]):
    """Create initial feed entry links file"""

    with open(FEED_ENTRY_LINKS_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(entry_links))


def update_feed_entry_links_file(entry_link: str):
    """Update feed entry links file by appending so new entries are no longer considered new"""

    with open(FEED_ENTRY_LINKS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{entry_link}\n")


def read_feed_entry_links_file() -> list[str]:
    """Read state of which feed entries need no further processing"""

    with open(FEED_ENTRY_LINKS_FILE, "r", encoding="utf-8") as f:
        return f.read().strip("\n").split("\n")


def check_for_new_entries(
    entry_links_last_update: list[str], entries: list[feedparser.FeedParserDict]
) -> Iterator[feedparser.FeedParserDict]:
    """Iterate feed entries looking for any new ones"""

    for entry in entries:
        if entry.link not in entry_links_last_update:
            yield entry


def create_newsletter(link: str, title: str) -> int | None:
    """Create newsletter with content and add campaign to Listmonk"""

    print("Creating newsletter for:", title)
    return create_campaign(title, create_content(link, title))


def create_content(link: str, title: str) -> str:
    """Create content to be used as body of newsletter"""

    with open(CONTENT_TEMPLATE_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    content = content.replace("LINK_HERE", link)
    content = content.replace("TITLE_HERE", title)

    og_image = get_og_image(fetch_url(link))
    if og_image:
        content = content.replace("IMAGE_HERE", og_image)
    else:
        # Remove optional image section
        content = re.sub(
            "IMAGE_OPTIONAL_BEGIN.*IMAGE_OPTIONAL_END\n", "", content, flags=re.DOTALL
        )

    return content


def fetch_url(url: str) -> str | None:
    """Get response body at a URL"""

    while True:
        try:
            return requests.get(url).content
        except requests.exceptions.ConnectionError:
            print(f"Failed to fetch URL: {url}! Retrying in 60 seconds...")
            time.sleep(60)


def get_og_image(html: str) -> str | None:
    """Get Open Graph cover image URL from given HTML"""

    # I've confirmed (by testing with a payload) that HTMLParser is NOT vulnerable to XXE
    # https://bugs.launchpad.net/lxml/+bug/1742885
    # https://lxml.de/4.0/api/lxml.etree.HTMLParser-class.html
    tree = etree.fromstring(html, etree.HTMLParser())

    og_image = tree.find("head/meta[@property='og:image']")
    if og_image:
        return og_image.get("content")


def send_newsletter(campaign_id: int) -> bool:
    """Given a campaign ID tell Listmonk to start it then perform error handling"""

    if DRY_RUN:
        print("Dry run: Not sending newsletter")
        return True

    print("Sending newsletter...")
    status_code = start_campaign(campaign_id)
    if status_code == 200:
        print("Successfully started campaign!")
        return True

    print("Error starting campaign!")
    return False


def create_campaign(title: str, body: str) -> int:
    """Create Listmonk email campaign for new article"""

    headers = {"Content-Type": "application/json;charset=utf-8"}

    json_data = {
        "name": f"New Article: {title}",
        "subject": title,
        "lists": [1],  # List ID 1 is my "New Articles" list
        "content_type": "richtext",
        "body": body,
        "messenger": "email",
        "type": "regular",
        "tags": ["new-article"],
    }

    while True:
        try:
            response = requests.post(
                f"{LISTMONK_URL}/api/campaigns",
                headers=headers,
                json=json_data,
                auth=(LISTMONK_USERNAME, LISTMONK_PASSWORD),
            )
            break
        except requests.exceptions.ConnectionError:
            print(
                "Failed to send Listmonk campaign create request! Retrying in 60 seconds..."
            )
            time.sleep(60)

    return response.json()["data"]["id"]


def start_campaign(campaign_id: int) -> int:
    """Start Listmonk email campaign"""

    headers = {"Content-Type": "application/json"}

    json_data = {"status": "running"}

    while True:
        try:
            response = requests.put(
                f"{LISTMONK_URL}/api/campaigns/{campaign_id}/status",
                headers=headers,
                json=json_data,
                auth=(LISTMONK_USERNAME, LISTMONK_PASSWORD),
            )
            break
        except requests.exceptions.ConnectionError:
            print(
                "Failed to send Listmonk campaign start request! Retrying in 60 seconds..."
            )
            time.sleep(60)

    return response.status_code


if __name__ == "__main__":
    main()
