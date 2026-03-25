"""Fetches feed entries from a Discourse JSON API endpoint."""

import requests
from structlog_config import configure_logger

from .entry import Entry

log = configure_logger()


def _base_url(url: str) -> str:
    parts = url.split("/")
    return parts[0] + "//" + parts[2]


def _fetch_topic_body(base_url: str, topic_id: int) -> str:
    response = requests.get(f"{base_url}/t/{topic_id}.json")
    response.raise_for_status()

    posts = response.json()["post_stream"]["posts"]

    return posts[0]["cooked"] if posts else ""


def fetch_entries(discourse_json_url: str) -> list[Entry]:
    response = requests.get(discourse_json_url)
    response.raise_for_status()

    base = _base_url(response.url)
    topics = response.json()["topic_list"]["topics"]

    entries = []

    for topic in topics:
        url = f"{base}/t/{topic['slug']}/{topic['id']}"
        body = _fetch_topic_body(base, topic["id"])

        entries.append(Entry(
            title=topic["title"],
            link=url,
            description=body,
            summary=topic.get("excerpt", ""),
            image=topic.get("image_url"),
        ))

    return entries
