"""Shared feed entry type used by both RSS and Discourse feed parsers."""


class Entry(dict):
    "Dict subclass supporting attribute access, compatible with feedparser entries."

    def __getattr__(self, key: str):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)
