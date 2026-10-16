"""Secrets loading.

Keys live in a mode-600 file owned by the service user and are read into a dict, never exported
to os.environ. An ANTHROPIC_API_KEY in a shell environment would switch other tools on the host that
use Claude logins (e.g. Claude Code) to API billing, so nothing here touches the environment.
Callers pass keys explicitly, e.g. ChatAnthropic(api_key=secrets.anthropic_api_key).
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_SECRETS = Path.home() / ".config" / "job-copilot" / "secrets.env"
REQUIRED = ("ANTHROPIC_API_KEY",)


class SecretsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Secrets:
    anthropic_api_key: str = field(repr=False)
    langsmith_api_key: str | None = field(default=None, repr=False)
    telegram_bot_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = None


def _check_permissions(path: Path) -> None:
    if os.name != "posix":
        return
    st = path.stat()
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SecretsError(f"{path} must not be readable by group or others (chmod 600)")
    if st.st_uid != os.getuid():
        raise SecretsError(f"{path} must be owned by the user running the agent")


def load_secrets(path: Path = DEFAULT_SECRETS) -> Secrets:
    if not path.is_file():
        raise SecretsError(f"secrets file not found: {path}")
    _check_permissions(path)
    values = {k: (v or "").strip() for k, v in dotenv_values(path).items()}
    missing = [k for k in REQUIRED if not values.get(k)]
    if missing:
        raise SecretsError(f"missing in {path}: {', '.join(missing)}")
    return Secrets(
        anthropic_api_key=values["ANTHROPIC_API_KEY"],
        langsmith_api_key=values.get("LANGSMITH_API_KEY") or None,
        telegram_bot_token=values.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=values.get("TELEGRAM_CHAT_ID") or None,
    )
