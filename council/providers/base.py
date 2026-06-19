"""Backend-agnostic provider interface for the LLM Council.

Every backend (Ollama cloud, Claude Code, Codex) is wrapped in a Provider that
exposes a single async streaming method. The orchestrator and TUI only ever see
the unified `Event` stream below — they never touch backend-specific details.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Literal


# An Event is the single currency between providers and the UI.
#   reasoning -> the model's thinking / chain-of-thought stream (dimmed in TUI)
#   answer    -> final answer tokens (normal weight in TUI)
#   done      -> terminal success; meta carries token counts, timings, etc.
#   error     -> terminal failure; text is a human-readable message
EventType = Literal["reasoning", "answer", "done", "error"]

# A chat message in the usual {role, content} shape.
Message = Dict[str, str]


@dataclass
class Event:
    """One unit of streamed output from a provider."""

    type: EventType
    text: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


class Provider(ABC):
    """Abstract base for a single council member's backend.

    A Provider is constructed from one council.yaml member entry and is
    responsible for turning a list of chat messages into a live `Event` stream.

    Subclasses implement `stream`. They MUST:
      - yield `reasoning` / `answer` events incrementally as text arrives,
      - yield exactly one terminal event (`done` on success, `error` on failure),
      - never raise out of `stream` for an expected failure (timeout, bad model,
        backend not signed in) — surface it as an `error` event instead so one
        member failing never takes down the whole council.
    """

    def __init__(
        self,
        name: str,
        model: str,
        options: Dict[str, Any] | None = None,
        workdir: str | None = None,
        tools_enabled: bool = False,
    ) -> None:
        self.name = name          # display name, e.g. "GLM 5.2 (max)"
        self.model = model        # backend model id, e.g. "glm-5.2:cloud"
        self.options = options or {}
        # When tools_enabled, members act as agents rooted at workdir (read/
        # write/bash/web). When disabled, they're pure chat models in a temp dir.
        self.workdir = workdir or os.getcwd()
        self.tools_enabled = tools_enabled

    # Identifies the backend family, e.g. "ollama" / "claude_code" / "codex".
    backend: str = "base"

    @abstractmethod
    async def stream(self, messages: List[Message]) -> AsyncIterator[Event]:
        """Stream the model's response to `messages` as a sequence of Events."""
        raise NotImplementedError
        yield  # pragma: no cover  (makes this an async generator for typing)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<{type(self).__name__} name={self.name!r} model={self.model!r}>"
