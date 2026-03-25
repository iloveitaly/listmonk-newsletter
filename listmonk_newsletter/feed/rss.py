"""Fetches feed entries from an RSS URL using feedparser."""

import feedparser
from structlog_config import configure_logger

from .entry import Entry

log = configure_logger()


def fetch_entries(rss_url: str) -> list[Entry]:
    feed: feedparser.FeedParserDict = feedparser.parse(rss_url)

    if "bozo_exception" in feed:
        log.error("feed parsing error", error=feed["bozo_exception"])
        return []

    if not feed:
        log.error("feed is empty")
        return []

    return [
        Entry(
            title=e.get("title", ""),
            link=e.get("link", ""),
            description=e.get("summary", ""),
            summary=e.get("summary", ""),
        )
        for e in feed.entries
    ]
