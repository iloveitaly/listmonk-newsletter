from pathlib import Path

import click
import requests
from decouple import config
from jinja2 import Template
from pydantic_ai import Agent
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from structlog_config import configure_logger
from whenever import Instant

log = configure_logger()


def get_headers() -> dict[str, str]:
    token = config("GITHUB_TOKEN", default=None)
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def github_api_get(url: str) -> list | dict:
    headers = get_headers()
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()


def summarize_with_gemini(prompt: str) -> str:
    api_key = config("GOOGLE_API_KEY", default=None)
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY must be set when using --summarize")

    model_name = config("GEMINI_MODEL", default="gemini-flash-latest")
    log.info("requesting gemini summary", model=model_name)

    provider = GoogleProvider(api_key=api_key)
    model = GoogleModel(model_name, provider=provider)
    agent = Agent(model, output_type=str)

    result = agent.run_sync(prompt)

    log.info("gemini summary received")
    return result.output


def fetch_all_repos(username: str) -> list[dict]:
    log.info("fetching all repos", username=username)
    repos = []
    page = 1
    per_page = 100
    while True:
        url = f"https://api.github.com/users/{username}/repos?per_page={per_page}&page={page}"
        data = github_api_get(url)
        if not data:
            break
        repos.extend(
            [
                repo
                for repo in data
                if not repo.get("fork") and (repo.get("description") or "").strip()
            ]
        )
        page += 1
    log.info("all repos fetched", count=len(repos), username=username)
    return repos


def fetch_releases(username: str, last_checked: Instant, repos: list[dict]) -> list[dict]:
    log.info("fetching releases", username=username)
    releases = []
    for repo in repos:
        repo_name = repo["name"]
        releases_url = f"https://api.github.com/repos/{username}/{repo_name}/releases"
        repo_releases = github_api_get(releases_url)
        if not repo_releases:
            continue
        latest = repo_releases[0]
        release_date = Instant.parse_iso(latest["published_at"])
        if release_date <= last_checked:
            continue
        releases.append(
            {
                "repo": repo_name,
                "tag": latest["tag_name"],
                "date": latest["published_at"],
                "name": latest["name"] or latest["tag_name"],
                "repo_url": repo["html_url"],
                "url": latest["html_url"],
                "description": latest["body"] if latest["body"] else "No description",
                "owner": username,
            }
        )
    log.info("releases fetched", count=len(releases), username=username)
    return releases


def fetch_contributed_repos(username: str, last_checked: Instant) -> list[dict]:
    """
    Find repositories where the user has authored commits.

    GitHub does not provide an API to search release notes across all repositories.
    As a workaround, we search for commits authored by the user to identify repositories
    they've contributed to, then filter releases from those repos for ones mentioning the user.
    """
    log.info("fetching repos with user commits", username=username)
    last_checked_str = last_checked.format_iso().split("T")[0]

    search_url = f"https://api.github.com/search/commits?q=author:{username}+committer-date:>{last_checked_str}&per_page=100"

    try:
        data = github_api_get(search_url)
    except Exception as e:
        log.warning("failed to search for user commits", error=str(e))
        return []

    if not data or "items" not in data:
        log.info("no repos with user commits found")
        return []

    seen_repos = set()
    repos = []

    for commit in data["items"]:
        if "repository" not in commit:
            continue

        repo = commit["repository"]
        repo_full_name = repo["full_name"]
        repo_owner = repo["owner"]["login"]

        if repo_owner == username:
            continue

        if repo_full_name in seen_repos:
            continue

        seen_repos.add(repo_full_name)
        repos.append(repo)

    log.info("repos with user commits found", count=len(repos), username=username)
    return repos


def fetch_cross_user_releases(username: str, last_checked: Instant, repos: list[dict]) -> list[dict]:
    """
    Fetch releases from contributed repositories where the user is mentioned.

    Filters releases to only include those where the username appears in the release body,
    as these are likely releases where the user is credited as a contributor.
    """
    log.info("fetching cross-user releases mentioning user", username=username)
    releases = []

    username_lower = username.lower()

    for repo in repos:
        repo_full_name = repo["full_name"]
        releases_url = f"https://api.github.com/repos/{repo_full_name}/releases"

        try:
            repo_releases = github_api_get(releases_url)
        except Exception as e:
            log.warning("failed to fetch releases", repo=repo_full_name, error=str(e))
            continue

        if not repo_releases:
            continue

        for release in repo_releases:
            release_date = Instant.parse_iso(release["published_at"])

            if release_date <= last_checked:
                break

            release_body = (release.get("body") or "").lower()

            if username_lower not in release_body and f"@{username_lower}" not in release_body:
                continue

            releases.append(
                {
                    "repo": repo["name"],
                    "tag": release["tag_name"],
                    "date": release["published_at"],
                    "name": release["name"] or release["tag_name"],
                    "repo_url": repo["html_url"],
                    "url": release["html_url"],
                    "description": release["body"] if release["body"] else "No description",
                    "owner": repo["owner"]["login"],
                }
            )

    log.info("cross-user releases fetched", count=len(releases), username=username)
    return releases


def fetch_new_repos(last_checked: Instant, repos: list[dict]) -> list[dict]:
    log.info("fetching new repos")
    new_repos = []
    for repo in repos:
        created_date = Instant.parse_iso(repo["created_at"])
        if created_date <= last_checked:
            continue
        new_repos.append(
            {
                "name": repo["name"],
                "created_at": repo["created_at"],
                "description": repo["description"] or "No description",
                "url": repo["html_url"],
            }
        )
    log.info("new repos fetched", count=len(new_repos))
    return new_repos


def fetch_github_activity(username: str, last_checked: str) -> dict:
    log.info("fetching github activity", username=username, last_checked=last_checked)
    last_checked_dt = Instant.parse_iso(last_checked)

    repos = fetch_all_repos(username)
    user_releases = fetch_releases(username, last_checked_dt, repos)

    contributed_repos = fetch_contributed_repos(username, last_checked_dt)
    cross_user_releases = fetch_cross_user_releases(username, last_checked_dt, contributed_repos)

    all_releases = user_releases + cross_user_releases

    activity = {
        "releases": all_releases,
        "new_repos": fetch_new_repos(last_checked_dt, repos),
    }
    log.info("github activity fetched", username=username, total_releases=len(all_releases))
    return activity


def filter_releases_for_new_repos(activity: dict) -> dict:
    new_repo_names = {repo["name"] for repo in activity.get("new_repos", [])}
    filtered_releases = [
        release
        for release in activity.get("releases", [])
        if release["repo"] not in new_repo_names
    ]
    removed = len(activity.get("releases", [])) - len(filtered_releases)
    if removed:
        log.info("filtered releases overlapping new repos", removed=removed)
    updated_activity = dict(activity)
    updated_activity["releases"] = filtered_releases
    return updated_activity


def generate_summary_prompt(activity: dict, username: str = "iloveitaly") -> str:
    log.debug("generating summary prompt")
    filtered_activity = filter_releases_for_new_repos(activity)
    template_str = """

You are an expert newsletter writer specializing in concise and engaging summaries of GitHub activity:

* Summarize the following GitHub activity for my personal email newsletter.
* Keep the tone friendly, casual yet professional, and highlight key updates in a concise manner.
* Include sections for new releases and new repositories. If there are no updates in a section, note that explicitly.
* If there is an entry in new releases and new repositories, omit it from new releases.
* Do not include release dates in the summary.
* Link to the repositories and releases using markdown format.
* You can assume that elsewhere in the newsletter we've already introduced the user to the newsletter and added a signoff.
  * Do not include general information like "Hey there, newsletter crew! Here's the latest scoop on my GitHub activity, packed with exciting updates from the past couple of months."
* Write an intro that gives a 1-2 sentence overview of the activity.
* Avoid fluff, filler phrases, and unnecessary adjectives.
* Use `## New Projects` and `## New Releases` as the section headers.
* No horizontal lines.
* Write in the 1st person.

Write a summary that is clear, concise, and suitable for a newsletter audience. Here's an example:

```markdown
Some major feature additions in existing libraries, particularly in
data modeling and logging tools, alongside the launch of six focused new
projects.

## New Projects

* [beautiful-traceback](https://github.com/iloveitaly/beautiful-traceback): Beautiful, readable Python tracebacks with colors and formatting.
* [cloudflare-analytics](https://github.com/iloveitaly/cloudflare-analytics): A client for the Cloudflare Analytics GraphQL API.

## New Releases

* [activemodel](https://github.com/iloveitaly/activemodel). Added new query methods to the query wrapper, including an efficient `exists()` function and a `sample()` method for random row selection.
* [aiautocommit](https://github.com/iloveitaly/aiautocommit). Integrated difftastic for structured diff visualization.
```

Below is the GitHub activity data to summarize.

---

## New Releases
{% if activity.releases %}
{% for release in activity.releases %}
- [{{ release.name }}]({{ release.url }}) in Repository: [{% if release.owner != username %}{{ release.owner }}/{% endif %}{{ release.repo }}]({{ release.repo_url }}) (Tag: {{ release.tag }}), Published: {{ release.date }}
  Description:
  ```markdown
  {{ release.description }}
  ```
{% endfor %}
{% else %}
- No new releases.
{% endif %}

## New Repositories
{% if activity.new_repos %}
{% for repo in activity.new_repos %}
- Repository: [{{ repo.name }}]({{ repo.url }}), Created: {{ repo.created_at }}, Description: {{ repo.description }}
{% endfor %}
{% else %}
- No new repositories.
{% endif %}
"""
    template = Template(template_str)
    prompt = template.render(activity=filtered_activity, username=username)
    log.debug("summary prompt generated")
    return prompt


@click.command()
@click.option("--username", default="iloveitaly")
@click.option("--days", default=60, type=int)
@click.option("--output-file", default=None, type=str)
@click.option(
    "--summarize",
    is_flag=True,
    help="Send the prompt to Gemini and output the summary",
)
def main(
    username: str,
    days: int,
    output_file: str | None,
    summarize: bool,
):
    last_checked = Instant.now().to_system_tz().add(days=-days).format_iso()
    activity = fetch_github_activity(username, last_checked)
    prompt = generate_summary_prompt(activity, username)
    if summarize:
        summary = summarize_with_gemini(prompt)
        if output_file:
            Path(output_file).write_text(summary)
        else:
            click.echo(summary)
        return

    if output_file:
        Path(output_file).write_text(prompt)
        return

    click.echo(prompt)


if __name__ == "__main__":
    main()
