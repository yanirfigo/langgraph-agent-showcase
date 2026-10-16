"""Title filters for the sample domain: business-systems roles.

ROLE tags a title as GTM/RevOps systems, business applications, or business-facing AI automation;
EXCLUDE drops early-career and off-domain titles; AI_EXCLUDE drops ML/infra titles that only match the
AI pattern; WIDE_NET is the broader net for company passes. Change keywords here;
tests/test_filters.py pins the intended matches.
re.A makes word boundaries ASCII-only, matching JS regexes without the u flag (a Hebrew prefix
letter glued to an English word still counts as a word boundary).
"""

from __future__ import annotations

import re

ROLE: dict[str, re.Pattern[str]] = {
    "gtm": re.compile(
        r"\bGTM\s+(engineer|engineering|systems?|operations|ops|architect|automation|tech|tooling)"
        r"|go[\s-]?to[\s-]?market\s+(engineer|systems|operations)|\bRev\s?Ops\b"
        r"|revenue\s+(operations|systems|engineer)",
        re.I | re.A,
    ),
    "bizapps": re.compile(
        r"business\s+(applications?|systems|apps)|enterprise\s+(applications?|systems|apps)|\bbiz\s?apps\b"
        r"|salesforce|\bCRM\s+(manager|lead|engineer|developer|architect|admin|systems|team)"
        r"|hubspot\s+(admin|developer|engineer|manager)|netsuite|internal\s+(tools|systems)",
        re.I | re.A,
    ),
    "ai": re.compile(
        r"AI\s+(product\s+)?builder"
        r"|\bAI\s+(automation|solutions?|enablement|transformation|adoption|operations|ops|strategy|agents?)\b"
        r"|gen\s?AI\s+(operations|solutions|lead|manager|engineer)|agentic\s+(automation|workflows?|operations)"
        r"|forward[\s-]deployed|\bAI\s+(engineer|lead|manager|architect|specialist)\b",
        re.I | re.A,
    ),
}

EXCLUDE = re.compile(
    r"\bintern(ship)?\b|\bstudent\b|סטודנט|\bjunior\b|\bph\.?d\b|hardware|chip|silicon|\bSDR\b|\bBDR\b"
    r"|product\s+marketing|\bCRM\s+marketing|lifecycle",
    re.I | re.A,
)

# AI-only matches that are really ML / infra / core-engineering jobs, not business AI building.
AI_EXCLUDE = re.compile(
    r"\bML\b|machine\s+learning|scientist|research|inference|\bSRE\b|site\s+reliability|data\s+engineer"
    r"|backend|back[\s-]end|frontend|front[\s-]end|full[\s-]?stack|devops|platform|search|computer\s+vision"
    r"|algorithm|embedded|firmware",
    re.I | re.A,
)

# Company passes: a wider "similar roles" net over every posting of a configured company
# (config.yaml, sources.company_passes).
WIDE_NET = re.compile(
    r"solutions?\s+(engineer|architect)|program\s+manager"
    r"|business\s+(intelligence|operations|analyst|systems|applications?)|agentic|\bAI\s+agents?\b"
    r"|\bAI\s+(engineer|enablement|adoption|automation|transformation)|enterprise\s+(applications?|systems)"
    r"|\bIT\s+(applications?|systems|business)|salesforce|\bCRM\b|\bSAP\b|developer\s+relations"
    r"|operations\s+manager",
    re.I | re.A,
)
WIDE_NET_EXCLUDE = re.compile(
    r"devops|\bHPC\b|test\s+engineer|design\s+engineer|verification|layout|thermal|\bIC\b|\bSoC\b|asic"
    r"|kernel|firmware|security\s+engineer|sourcing",
    re.I | re.A,
)


def role_of(title: str) -> tuple[str, ...]:
    """Role tags for a title; empty when it isn't a target role."""
    roles = tuple(k for k, rx in ROLE.items() if rx.search(title))
    if roles == ("ai",) and AI_EXCLUDE.search(title):
        return ()
    return roles


def is_excluded(title: str) -> bool:
    return bool(EXCLUDE.search(title))


def wide_net(title: str) -> bool:
    """True for a title in the wider "similar roles" net used by company passes."""
    return bool(WIDE_NET.search(title)) and not WIDE_NET_EXCLUDE.search(title)
