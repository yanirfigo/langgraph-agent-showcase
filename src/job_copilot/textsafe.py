"""Make untrusted text safe to show in Telegram.

Telegram auto-links URLs, bare domains, emails and @usernames even in plain-text messages. Text
from source APIs or models is defanged: scheme URLs are removed, and dots in domain-like tokens and
"@" signs are swapped for look-alike characters, so nothing becomes a clickable link or mention.
The look-alikes (U+2024 one-dot leader, U+FF20 fullwidth @) are ordinary punctuation, so the
sanitizer keeps them and the text stays readable.
"""

from __future__ import annotations

import re

_SCHEME_URL = re.compile(r"(?i)\b(?:https?|ftp|tg)://\S+")
_DOMAIN = re.compile(r"(?i)\b(?:[a-z0-9-]+\.)+[a-z]{2,24}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def defang(text: str) -> str:
    text = _SCHEME_URL.sub("[link removed]", text)
    text = _EMAIL.sub("[email removed]", text)
    text = _DOMAIN.sub(lambda m: m.group(0).replace(".", "․"), text)
    return text.replace("@", "＠")
