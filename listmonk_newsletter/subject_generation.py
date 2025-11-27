from __future__ import annotations

from pathlib import Path

import click
import feedparser
from decouple import config
from structlog_config import configure_logger

from .summarize_github import summarize_with_gemini

log = configure_logger()


def _entry_payload(entry: feedparser.FeedParserDict) -> dict[str, str]:
    title = str(entry.get("title", "")).strip()
    link = str(entry.get("link", "")).strip()
    summary = str(entry.get("summary", "") or entry.get("description", "")).strip()

    return {
        "title": title,
        "link": link,
        "summary": summary,
    }


def _format_entries_for_prompt(entries: list[dict[str, str]]) -> str:
    lines = []

    for entry in entries:
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        summary = entry.get("summary", "").strip()

        snippet = summary[:160]
        if summary and len(summary) > 160:
            snippet = f"{snippet}..."

        if link:
            lines.append(f"- {title} ({link})" + (f": {snippet}" if snippet else ""))
        else:
            lines.append(f"- {title}" + (f": {snippet}" if snippet else ""))

    return "\n".join(lines) if lines else "- No articles in this digest."


def _format_additional_context(additional_context: str | None) -> str:
    if not additional_context:
        return "- No additional context provided."

    return additional_context.strip()


def generate_subject_line(
    title: str,
    entries: list[dict[str, str]],
    additional_context: str | None,
) -> str:
    entries_block = _format_entries_for_prompt(entries)
    context_block = _format_additional_context(additional_context)

    prompt = f"""
You are a marketing copywriter who writes concise, compelling subject lines for personal technology newsletters.

Newsletter title: {title}

Articles included in this issue:
{entries_block}

Additional context, notes, or commentary to consider:
{context_block}

Write a single email subject line (max 60 characters) that teases the most interesting updates above. Use first-person language when referring to the author, avoid emojis, and return only the subject line with no trailing punctuation beyond standard sentence-ending characters.
"""

    subject = summarize_with_gemini(prompt).strip()

    first_line = subject.splitlines()[0].strip()

    if len(first_line) > 60:
        first_line = first_line[:57].rstrip() + "..."

    log.info("generated gemini subject", subject=first_line)

    return first_line


@click.command()
@click.option("--rss-url", default=None, type=str)
@click.option("--count", default=5, type=int)
@click.option("--context-file", default=None, type=click.Path(path_type=Path))
@click.option("--newsletter-title", default=None, type=str)
def main(
    rss_url: str | None,
    count: int,
    context_file: Path | None,
    newsletter_title: str | None,
) -> None:
    rss = rss_url or config("RSS_URL", default=None)
    if not rss:
        raise click.UsageError("Provide --rss-url or set RSS_URL")

    title = newsletter_title or config("LISTMONK_TITLE", default="Newsletter")

    feed = feedparser.parse(rss)
    if not feed or not feed.entries:
        raise click.ClickException("Feed returned no entries")

    selected = feed.entries[:count]
    entries = [_entry_payload(entry) for entry in selected]

    additional_context = None
    if context_file:
        additional_context = context_file.read_text(encoding="utf-8").strip()

    subject = generate_subject_line(title, entries, additional_context)

    click.echo(subject)


if __name__ == "__main__":
    main()
