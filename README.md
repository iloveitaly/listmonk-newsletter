# Listmonk Email Newsletter Digest

Generate a [listmonk](https://listmonk.app) newsletter digest campaign from an RSS feed. I'm using it [for my personal blog.](https://mikebian.co)

## Usage

Best way to use [this is docker](docker-compose.yml) and set it to run on a schedule. Check out the envrc-example for configuration details.

Some useful commands:

* `LOG_LEVEL=DEBUG` is useful for debugging
* `docker compose exec listmonk-newsletter bash -l` is useful for executing commands directly
* `uv sync` and then `uv run listmonk-newsletter` to run it locally

## Email Templates

You can provide your own email template to use. The one bundled, which is specifically styled for [my blog](https://mikebian.co),
is based off [of this template](https://github.com/ColorlibHQ/email-templates/blob/master/7/index.html).

### ListMonk Template Variables & Jinga2

What's neat is you can include ListMonk template variables (unsubscribe, tracking, etc) in the generated jinga2 template. This means you want your listmonk template to be a completely blank template and instead include everything in your email template added to this docker container.

You'll see this in the example template as:

```
{% raw %}{{ UnsubscribeURL }}{% endraw %}
```

Another interesting listmonk quirk is if you want link tracking, you need to add `@TrackLink` to the end of the URL. If you pass links to the campaign API without this, they will not be tracked.
