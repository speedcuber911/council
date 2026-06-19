"""Backend availability detection.

Not everyone has all three CLIs. council should run with whatever subset the
user has — Claude only, Codex only, just Ollama, or any combination. This module
is the single source of truth for "is this backend usable right now," consumed by
the roster loader, the researcher selector, and `council doctor`.
"""

from __future__ import annotations

import shutil
from typing import Dict

import httpx


OLLAMA_HOST = "http://localhost:11434"

# Human labels + the install hint shown when a backend is missing.
BACKEND_INFO = {
    "claude_code": ("Claude Code", "https://claude.com/claude-code"),
    "codex": ("Codex", "npm i -g @openai/codex"),
    "ollama": ("Ollama", "https://ollama.com/download"),
}


def _ollama_up() -> bool:
    if not shutil.which("ollama"):
        return False
    try:
        httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=3.0).raise_for_status()
        return True
    except Exception:
        return False


def available() -> Dict[str, bool]:
    """Return {backend: usable}. Ollama also requires its daemon to respond."""
    return {
        "claude_code": shutil.which("claude") is not None,
        "codex": shutil.which("codex") is not None,
        "ollama": _ollama_up(),
    }


def label(backend: str) -> str:
    return BACKEND_INFO.get(backend, (backend, ""))[0]


def install_hint(backend: str) -> str:
    return BACKEND_INFO.get(backend, (backend, ""))[1]
