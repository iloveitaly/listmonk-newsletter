"""
Fetches articles read in Readwise Reader with specific tags.
Used in generate_campaign() to build optional newsletter section.
"""

import os
from pathlib import Path
from pprint import pprint

import backoff
import click
import requests
from decouple import config
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


@click.command()
@click.option("--token", help="Readwise API token (or set READWISE_API_TOKEN env var)")
@click.option("--tag", help="Tag to filter articles (or set READWISE_TAG env var)")
@click.option("--summary-days", type=int, default=30, help="Days to look back (default: 30)")
def cli(token: str | None, tag: str | None, summary_days: int):
    """
    Test Readwise Reader integration by fetching articles with a specific tag.
    """
    readwise_token = token or config("READWISE_API_TOKEN", default=None)
    readwise_tag = tag or config("READWISE_TAG", default=None)

    if not readwise_token:
        click.echo("Error: READWISE_API_TOKEN not set. Either pass --token or set environment variable.", err=True)
        return

    if not readwise_tag:
        click.echo("Error: READWISE_TAG not set. Either pass --tag or set environment variable.", err=True)
        return

    click.echo(f"Fetching articles with tag '{readwise_tag}' (lookback: {summary_days} days)...")

    last_checked = get_last_readwise_check()

    if last_checked:
        click.echo(f"Last checked: {last_checked.format_common_iso()}")
    else:
        click.echo("No previous check found - using lookback period")

    articles = get_readwise_articles(
        token=readwise_token,
        tag=readwise_tag,
        since=last_checked,
        lookback_days=summary_days
    )

    click.echo(f"\nFound {len(articles)} articles:\n")
    pprint([article.model_dump() for article in articles])

    if articles:
        if click.confirm("\nUpdate last checked timestamp?"):
            update_last_readwise_check(Instant.now())
            click.echo("Timestamp updated!")


if __name__ == "__main__":
    cli()
