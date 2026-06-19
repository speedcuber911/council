"""Config-file resolution so council works installed system-wide, not just in-repo.

Lookup order for the roster config:
  1. $COUNCIL_CONFIG (explicit override)
  2. ~/.config/council/council.yaml (user config; created by `council init`)
  3. the council.yaml bundled inside the installed package (sensible default)
"""

from __future__ import annotations

import os
from pathlib import Path


USER_CONFIG_DIR = Path(os.path.expanduser("~/.config/council"))
USER_CONFIG = USER_CONFIG_DIR / "council.yaml"
BUNDLED_CONFIG = Path(__file__).parent / "council.yaml"


def resolve_config(explicit: str | None = None) -> Path:
    """Return the config path to use (does not require it to exist if explicit)."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("COUNCIL_CONFIG")
    if env:
        return Path(env)
    if USER_CONFIG.exists():
        return USER_CONFIG
    return BUNDLED_CONFIG


def init_user_config(force: bool = False) -> tuple[Path, bool]:
    """Copy the bundled default to the user config dir. Returns (path, created)."""
    USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if USER_CONFIG.exists() and not force:
        return USER_CONFIG, False
    USER_CONFIG.write_text(BUNDLED_CONFIG.read_text())
    return USER_CONFIG, True
