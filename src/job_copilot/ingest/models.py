"""Posting model. Everything from a source API is untrusted: fields are bounded and URLs checked."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_TITLE = 300
MAX_FIELD = 200
MAX_URL = 2000
MAX_TEXT = 20_000
# Unicode categories removed from every field: controls (incl. DEL and C1), format characters
# (bidi overrides, zero-width), and line/paragraph separators.
_STRIP_CATEGORIES = {"Cc", "Cf", "Zl", "Zp"}


def clean_url(url: str) -> str:
    """Drop utm_* tracking parameters."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def sanitize(value: object, cap: int, *, multiline: bool = False) -> str:
    """Strip control/format characters; single-line fields also collapse whitespace."""
    text = "" if value is None else str(value)
    keep = "\n\t" if multiline else ""
    if not multiline:
        text = re.sub(r"[\r\n\t\v\f  \x85]", " ", text)  # line breaks become spaces, not glue
    text = "".join(ch for ch in text if ch in keep or unicodedata.category(ch) not in _STRIP_CATEGORIES)
    if not multiline:
        text = re.sub(r"\s+", " ", text)
    return text.strip()[:cap]


class Posting(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(max_length=40)
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    company: str = Field(default="", max_length=MAX_FIELD)  # may be empty: some sources omit it
    url: str = Field(max_length=MAX_URL)
    city: str | None = Field(default=None, max_length=MAX_FIELD)
    posted: str | None = Field(default=None, max_length=40)
    salary: str | None = Field(default=None, max_length=MAX_FIELD)
    seniority: str | None = Field(default=None, max_length=MAX_FIELD)
    roles: tuple[str, ...] = ()
    text: str | None = Field(default=None, max_length=MAX_TEXT)

    @field_validator("url")
    @classmethod
    def _web_url(cls, v: str) -> str:
        """Absolute http(s) URL with no userinfo; tracking params removed.

        http is accepted here because these are only links shown to the owner. Anything the agent
        itself fetches goes through the stricter https-only guard.
        """
        parts = urlsplit(v)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("posting url must be an absolute http(s) URL")
        if parts.username or parts.password or "\\" in parts.netloc:
            raise ValueError("posting url must not contain userinfo or backslashes")
        cleaned = clean_url(v)
        if len(cleaned) > MAX_URL:
            raise ValueError("posting url too long after cleaning")
        return cleaned

    @property
    def key(self) -> str:
        """Dedupe key: same company + title is the same posting."""
        return f"{_norm(self.company)}|{_norm(self.title)}"

    @classmethod
    def from_raw(cls, **fields: object) -> Posting:
        """Build from untrusted source fields: sanitize and cap lengths before validating."""
        # url is never truncated (that would change where it points); an over-long url fails validation.
        caps = {"title": MAX_TITLE, "text": MAX_TEXT, "url": 10 * MAX_URL, "posted": 40, "source": 40}
        clean: dict[str, object] = {}
        for k, v in fields.items():
            if k == "roles":
                clean[k] = v
            elif k == "company":
                clean[k] = sanitize(v, MAX_FIELD)
            else:
                clean[k] = sanitize(v, caps.get(k, MAX_FIELD), multiline=(k == "text")) or None
        return cls.model_validate(clean)
