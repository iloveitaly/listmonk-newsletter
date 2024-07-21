# Listmonk Email Newsletter Digest

Generate a [listmonk](https://listmonk.app) newsletter digest campaign from an RSS feed. I'm using it for my personal blog.

## Usage

Best way to use this is docker and set it to run on a schedule. Check out the envrc-example for configuration details.

## Email Templates

You can provide your own email template to use. The one bundled, which is specifically styled for [my blog](https://mikebian.co),
is based off [of this template](https://github.com/ColorlibHQ/email-templates/blob/master/7/index.html).

### ListMonk Template Variables & Jinga2

What's neat is you can include ListMonk template variables (unsubscribe, tracking, etc) in the generated jinga2 template. This means you want your listmonk template to be a completely blank template and instead include everything in your email template added to this docker container.

## TODO

- [ ] report test email bug to listmonk
- [ ] add cli switches for env vars
- [ ] list selection should be configurable