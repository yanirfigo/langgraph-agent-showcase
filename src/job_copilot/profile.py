"""The owner's non-identifying profile (YAML, mode 600 on the host, never in git)."""

from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_PROFILE = Path.home() / ".config" / "job-copilot" / "profile.yaml"
MAX_PROFILE_BYTES = 8_000


class ProfileError(RuntimeError):
    pass


def load_profile(path: Path = DEFAULT_PROFILE) -> str:
    """Return the profile as YAML text after checking it parses and stays small."""
    if not path.is_file():
        raise ProfileError(f"profile not found: {path}")
    text = path.read_text(encoding="utf-8")
    if len(text.encode()) > MAX_PROFILE_BYTES:
        raise ProfileError("profile too large for the screener prompt")
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or "target_roles" not in data:
        raise ProfileError("profile must be a mapping with target_roles")
    return text
