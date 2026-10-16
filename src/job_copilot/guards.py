"""Deterministic guards that run before (or instead of) any model call."""

from __future__ import annotations

import re

# Example title deal-breakers for the sample domain (adapt to your own profile).
# Matching here drops the posting with no model call.
# "contracts" (plural, as in a contracts-management role) deliberately does not match.
_DEAL_BREAKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("maternity cover", re.compile(r"maternity|parental\s+leave\s+cover|חל\"?ד|ממלא(?:/ת)?\s+מקום", re.I)),
    (
        "contract / temporary",
        re.compile(
            r"\bcontractor\b|\(contract\)|\bcontract\s+(?:role|position|basis|job)\b|^contract\b"
            r"|\btemporary\b|\btemp\b|\bfreelance\b|זמני",
            re.I,
        ),
    ),
)


def title_deal_breaker(title: str) -> str | None:
    """Name of the deal-breaker visible in the title, or None."""
    for name, rx in _DEAL_BREAKERS:
        if rx.search(title):
            return name
    return None
