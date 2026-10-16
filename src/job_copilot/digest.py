"""Turn a run's results into plain-text Telegram messages.

Every untrusted string (posting fields from source APIs, and all model-written text) is defanged
(see textsafe.py) and model text also loses quoted spans, so nothing a web page or posting said can
arrive as a clickable link, mention or verbatim quote. The only link left clickable is the
posting's own URL, which came from the source API and passed URL validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ingest.models import Posting, sanitize
from .subagents.research import CheckedBrief, scrub_quotes
from .textsafe import defang


def model_text(text: str | None, cap: int) -> str:
    """Make model output safe to show: no links/mentions, no quoted spans, no control chars, capped."""
    if not text:
        return ""
    return sanitize(defang(scrub_quotes(text)), cap)


def field_text(text: str | None, cap: int = 200) -> str:
    return sanitize(defang(text or ""), cap)


def posting_header(p: Posting) -> str:
    where = " · ".join(x for x in (field_text(p.company) or "(no company)", field_text(p.city)) if x)
    return f"{field_text(p.title, 300)}\n{where}"


INJECTION_WARNING = "⚠ This posting contains text aimed at an AI. It was not researched; treat it with care."


def item_text(
    p: Posting,
    score: int | None,
    reason: str | None,
    checked: CheckedBrief | None,
    note: str = "",
    *,
    injection: bool = False,
) -> str:
    lines = [posting_header(p)]
    if injection:
        lines.append(INJECTION_WARNING)
    if score is not None:
        lines.append(f"Fit {score}/100: {model_text(reason, 200)}")
    if note:
        lines.append(f"Note: {field_text(note, 120)}")
    if checked:
        b = checked.brief
        lines.append(f"Business-facing: {b.business_facing} · Work mode: {b.work_mode}")
        if b.company_summary:
            lines.append(model_text(b.company_summary, 400))
        for c in checked.verified[:3]:
            lines.append(f"✓ {model_text(c.text, 200)}")
        for c in checked.unverified[:2]:
            lines.append(f"? {model_text(c.text, 200)} (unverified)")
        for f in checked.flags[:3]:
            lines.append(f"⚠ {model_text(f, 200)}")
    lines.append(p.url)
    return "\n".join(lines)


@dataclass
class DigestPlan:
    top: list = field(default_factory=list)  # Screened items to send with buttons, best first
    more: list = field(default_factory=list)  # also 55+, listed compactly (no buttons)
    deferred: list = field(default_factory=list)  # not screened (e.g. daily cap): requeue for next run
    dropped: int = 0


def select_digest(final: list, max_items: int) -> DigestPlan:
    """Pure selection: research first, then by score; deferred items are set aside, not shown."""
    plan = DigestPlan(
        deferred=[s for s in final if s.route == "deferred"],
        dropped=sum(1 for s in final if s.route == "drop"),
    )
    shown = sorted(
        (s for s in final if s.route in ("research", "digest")),
        key=lambda s: (s.route != "research", -(s.fit.score if s.fit else 0)),
    )
    plan.top, plan.more = shown[:max_items], shown[max_items:]
    return plan


def more_text(items: list, cap: int = 20) -> str:
    lines = [f"Also scored 55+ ({len(items)}), listed without buttons (open the source sites to apply):"]
    for s in items[:cap]:
        score = f"{s.fit.score}" if s.fit else "?"
        warn = " ⚠ text aimed at an AI" if s.fit and s.fit.injection_suspected else ""
        lines.append(f"{score} · {field_text(s.posting.title, 90)} · {field_text(s.posting.company, 40)}{warn}")
    if len(items) > cap:
        lines.append(f"…and {len(items) - cap} more")
    return "\n".join(lines)


def summary_text(plan: DigestPlan, researched: int, errors: list[str]) -> str:
    shown = len(plan.top) + len(plan.more)
    screened = shown + plan.dropped
    lines = [
        f"Job Copilot: {shown} to look at ({researched} researched) · {screened} screened · {plan.dropped} dropped"
    ]
    if plan.deferred:
        lines.append(f"Daily model budget reached: {len(plan.deferred)} postings deferred to the next run.")
    if errors:
        lines.append("Source issues: " + "; ".join(sanitize(e, 80) for e in errors[:3]))
    return "\n".join(lines)
