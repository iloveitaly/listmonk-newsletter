"""
Fetches articles read in Readwise Reader with specific tags.
Used in generate_campaign() to build optional newsletter section.
"""

import os
from pathlib import Path

import backoff
import requests
from pydantic import BaseModel
from whenever import Instant, days

from listmonk_newsletter import log


class ReadwiseArticle(BaseModel):
    id: str
    url: str
    title: str
    author: str | None = None
    word_count: int | None = None
    reading_progress: float
    tags: list[str] = []
    updated_at: str
    summary: str | None = None


def get_state_file_path() -> Path:
    return Path(__file__).parent.parent / "data" / "last_readwise_checked.txt"


def get_last_readwise_check() -> Instant | None:
    state_file = get_state_file_path()

    if not state_file.exists():
        return None

    content = state_file.read_text().strip()
    if not content:
        return None

    return Instant.parse_common_iso(content)


def update_last_readwise_check(timestamp: Instant) -> None:
    state_file = get_state_file_path()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(timestamp.format_common_iso())


@backoff.on_exception(
    backoff.expo,
    requests.exceptions.RequestException,
    max_tries=3
)
def get_readwise_articles(
    token: str,
    tag: str,
    since: Instant | None = None,
    lookback_days: int = 30
) -> list[ReadwiseArticle]:
    if since is None:
        since = Instant.now() - days(lookback_days)

    articles = []
    page_cursor = None

    while True:
        params = {
            "tag": tag,
            "updatedAfter": since.format_common_iso(),
            "location": "archive",
        }

        if page_cursor:
            params["pageCursor"] = page_cursor

        log.info(
            "fetching readwise articles",
            tag=tag,
            since=since.format_common_iso(),
            page_cursor=page_cursor
        )

        response = requests.get(
            "https://readwise.io/api/v3/list/",
            headers={"Authorization": f"Token {token}"},
            params=params,
            timeout=30
        )
        response.raise_for_status()

        data = response.json()
        results = data.get("results", [])

        for doc in results:
            if doc.get("reading_progress") == 1.0:
                articles.append(ReadwiseArticle(
                    id=doc["id"],
                    url=doc["url"],
                    title=doc["title"],
                    author=doc.get("author"),
                    word_count=doc.get("word_count"),
                    reading_progress=doc["reading_progress"],
                    tags=doc.get("tags", []),
                    updated_at=doc["updated"],
                    summary=doc.get("summary")
                ))

        page_cursor = data.get("nextPageCursor")
        if not page_cursor:
            break

    log.info("fetched readwise articles", count=len(articles))

    return articles
