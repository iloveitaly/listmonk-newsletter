import os
import requests
from jinja2 import Template
import structlog
from structlog_config import configure_logger
from whenever import Instant
import funcy as f
import click

log = configure_logger()

def get_headers() -> dict[str, str]:
    token = os.getenv("GITHUB_TOKEN")
    return f.compact({
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {token}" if token else None
    })

def github_api_get(url: str) -> list | dict:
    headers = get_headers()
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()

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
        repos.extend(data)
        page += 1
    log.info("all repos fetched", count=len(repos), username=username)
    return repos

def fetch_releases(username: str, last_checked: Instant, repos: list[dict]) -> list[dict]:
    log.info("fetching releases", username=username)
    releases = []
    for repo in repos:
        repo_name = repo['name']
        releases_url = f"https://api.github.com/repos/{username}/{repo_name}/releases"
        repo_releases = github_api_get(releases_url)
        if not repo_releases:
            continue
        latest = repo_releases[0]
        release_date = Instant.parse_iso(latest['published_at'])
        if release_date <= last_checked:
            continue
        releases.append({
            'repo': repo_name,
            'tag': latest['tag_name'],
            'date': latest['published_at'],
            'name': latest['name'] or latest['tag_name'],
            'repo_url': repo['html_url'],
            'url': latest['html_url'],
            'description': latest['body'] if latest['body'] else "No description"
        })
    log.info("releases fetched", count=len(releases), username=username)
    return releases

def fetch_new_repos(last_checked: Instant, repos: list[dict]) -> list[dict]:
    log.info("fetching new repos")
    new_repos = []
    for repo in repos:
        created_date = Instant.parse_iso(repo['created_at'])
        if created_date <= last_checked:
            continue
        new_repos.append({
            'name': repo['name'],
            'created_at': repo['created_at'],
            'description': repo['description'] or "No description",
            'url': repo['html_url']
        })
    log.info("new repos fetched", count=len(new_repos))
    return new_repos

def fetch_github_activity(username: str, last_checked: str) -> dict:
    log.info("fetching github activity", username=username, last_checked=last_checked)
    last_checked_dt = Instant.parse_iso(last_checked)
    repos = fetch_all_repos(username)
    activity = {
        "releases": fetch_releases(username, last_checked_dt, repos),
        "new_repos": fetch_new_repos(last_checked_dt, repos)
    }
    log.info("github activity fetched", username=username)
    return activity

def generate_summary_prompt(activity: dict) -> str:
    log.debug("generating summary prompt")
    template_str = """

You are an expert newsletter writer specializing in concise and engaging summaries of GitHub activity:

* Summarize the following GitHub activity for my personal email newsletter.
* Keep the tone friendly, casual yet professional, and highlight key updates in a concise manner.
* Include sections for new releases and new repositories. If there are no updates in a section, note that explicitly.
* If there is an entry in new releases and new repositories, omit it from new releases.
* Do not include release dates in the summary
* You can assume that elsewhere in the newsletter we've already introduced the user to the newsletter and added a signoff.
  * Do not include general information like "Hey there, newsletter crew! Here's the latest scoop on my GitHub activity, packed with exciting updates from the past couple of months."
* Write an intro that gives a 1-2 sentence overview of the activity.
* Avoid fluff and filler phrases.
* Include links to the
Below is the GitHub activity data to summarize.

---

## New Releases
{% if activity.releases %}
{% for release in activity.releases %}
- [{{ release.name }}]({{ release.url }}) in Repository: [{{ release.repo }}]({{ release.repo_url }}) (Tag: {{ release.tag }}), Published: {{ release.date }}
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

Provide a summary that is clear, concise, and suitable for a newsletter audience.
"""
    template = Template(template_str)
    prompt = template.render(activity=activity)
    log.debug("summary prompt generated")
    return prompt

@click.command()
@click.option('--username', default="iloveitaly")
@click.option('--days', default=60, type=int)
@click.option('--output-file', default=None, type=str)
def main(username: str, days: int, output_file: str | None):
    last_checked = Instant.now().to_system_tz().add(days=-days).format_iso()
    activity = fetch_github_activity(username, last_checked)
    prompt = generate_summary_prompt(activity)
    if output_file:
        with open(output_file, 'w') as f:
            f.write(prompt)
    else:
        print(prompt)

if __name__ == "__main__":
    main()