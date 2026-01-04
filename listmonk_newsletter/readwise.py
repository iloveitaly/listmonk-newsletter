"""
Fetches articles read in Readwise Reader with specific tags.
Used in generate_campaign() to build optional newsletter section.
"""

from pathlib import Path
from pprint import pprint

import backoff
import click
import requests
import structlog
from decouple import config
from pydantic import BaseModel
from whenever import Instant, ZonedDateTime, days

log = structlog.get_logger()


class ReadwiseArticle(BaseModel):
    id: str
    url: str
    title: str
    author: str | None = None
    word_count: int | None = None
    reading_progress: float
    updated_at: str
    notes: str | None = None
    summary: str | None = None


def get_state_file_path() -> Path:
    return Path(__file__).parent.parent / "data" / "last_readwise_checked.txt"


def get_last_readwise_check() -> ZonedDateTime | None:
    state_file = get_state_file_path()

    if not state_file.exists():
        return None

    content = state_file.read_text().strip()
    if not content:
        return None

    return Instant.parse_iso(content).to_system_tz()


def update_last_readwise_check(timestamp: ZonedDateTime) -> None:
    state_file = get_state_file_path()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(timestamp.format_iso())


@backoff.on_exception(
    backoff.expo,
    requests.exceptions.RequestException,
    max_tries=3
)
def get_readwise_articles(
    token: str,
    tag: str,
    since: ZonedDateTime | None = None,
    lookback_days: int = 30
) -> list[ReadwiseArticle]:
    """
    Example of the response:

    {
        "count": 1,
        "nextPageCursor": null,
        "results": [
            {
                "author": "Paris Martineau",
                "category": "article",
                "content": null,
                "created_at": "2025-12-31T14:22:45.513827+00:00",
                "first_opened_at": "2025-12-31T14:22:46.210000+00:00",
                "id": "01kdtcmae9bs1kh5qjcytn7t9e",
                "image_url": "https://article.images.consumerreports.org/image/upload/t_article_tout/v1760120451/prod/content/dam/CRO-Images-2025/Special%20Projects/CR-SP-InlineHero-Protein-Powders-and-Shakes-Contain-High-Levels-of-Lead-1025-v2",
                "last_moved_at": "2026-01-01T17:45:04.810000+00:00",
                "last_opened_at": "2026-01-02T14:36:00.545000+00:00",
                "location": "archive",
                "notes": "Continues to support the idea that avoiding weird abnormal not-real-food-foods is a good idea.",
                "parent_id": null,
                "published_date": "2025-10-14",
                "reading_progress": 1,
                "reading_time": "18 mins",
                "saved_at": "2025-12-31T14:22:44.681000+00:00",
                "site_name": "Consumer Reports",
                "source": "Reader add from import URL",
                "source_url": "https://www.consumerreports.org/lead/protein-powders-and-shakes-contain-high-levels-of-lead-a4206364640/?utm_source=signals.superpower.com&utm_medium=newsletter&utm_campaign=signals-28-the-10-health-shifts-that-actually-mattered-in-2025&_bhlid=38fb6b3828d2211bf79bd170efbd9794efe3f23d",
                "summary": "CR tests of 23 popular protein powders and shakes found that most contain high levels of lead.",
                "tags": {
                    "public": {
                        "created": 1767289502716,
                        "name": "public",
                        "type": "manual"
                    }
                },
                "title": "Protein Powders and Shakes Contain High Levels of Lead",
                "updated_at": "2026-01-02T14:37:28.911586+00:00",
                "url": "https://read.readwise.io/read/01kdtcmae9bs1kh5qjcytn7t9e",
                "word_count": 4593
            }
        ]
    }
    """

    if since is None:
        since = Instant.now().to_system_tz().add(days=-lookback_days)

    articles = []
    page_cursor = None

    while True:
        params = {
            "tag": tag,
            "updatedAfter": since.format_iso().split('[')[0],
        }

        if page_cursor:
            params["pageCursor"] = page_cursor

        log.info(
            "fetching readwise articles",
            tag=tag,
            since=since.format_iso(),
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
            tags_dict = doc.get("tags", {})
            if tag not in tags_dict:
                log.info(
                    "skipping article: tag not found",
                    article_id=doc["id"],
                    title=doc["title"]
                )
                continue

            tag_created_ms = tags_dict[tag]["created"]
            tag_created = Instant.from_timestamp_millis(tag_created_ms).to_system_tz()

            if tag_created < since:
                log.info(
                    "skipping article: tag created before cutoff",
                    article_id=doc["id"],
                    title=doc["title"],
                    tag_created=tag_created.format_iso(),
                    cutoff=since.format_iso()
                )
                continue

            notes = doc.get("notes")
            summary = doc.get("summary")

            if not notes:
                log.warning(
                    "article missing notes, using summary instead",
                    article_id=doc["id"],
                    title=doc["title"]
                )

            articles.append(ReadwiseArticle(
                id=doc["id"],
                url=doc["source_url"],
                title=doc["title"],
                author=doc.get("author"),
                word_count=doc.get("word_count"),
                reading_progress=doc["reading_progress"],
                updated_at=doc["updated_at"],
                notes=notes,
                summary=summary
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
        click.echo(f"Last checked: {last_checked.format_iso()}")
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


if __name__ == "__main__":
    cli()
