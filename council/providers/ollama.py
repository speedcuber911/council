"""Ollama provider — streams from a local Ollama daemon signed in to the cloud.

Two modes:
  * pure chat (default): one streamed /api/chat call, like a normal council member.
  * agentic (tools_enabled): a client-side function-calling loop. Ollama cloud
    models support tool calls but have no agent runtime, so we advertise the
    schemas in `tools.py`, parse `message.tool_calls`, execute them locally
    (read/write/bash/web) rooted at `workdir`, feed the results back, and repeat
    until the model returns a final answer.

Reasoning tokens map to `reasoning` events; answer tokens to `answer`; tool
activity is surfaced as `reasoning` events prefixed with 🔧 so it shows in the UI.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List

import httpx

from .base import Event, Message, Provider
from .. import tools as toolbox


DEFAULT_HOST = "http://localhost:11434"
DEFAULT_TIMEOUT = 300.0
_MAX_TOOL_ROUNDS = 12


class OllamaProvider(Provider):
    """Streams a single Ollama (cloud or local) model as council Events."""

    backend = "ollama"

    def __init__(
        self,
        name: str,
        model: str,
        options: Dict[str, Any] | None = None,
        workdir: str | None = None,
        tools_enabled: bool = False,
        host: str = DEFAULT_HOST,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(name, model, options, workdir, tools_enabled)
        self.host = host.rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------------ #
    # Request construction
    # ------------------------------------------------------------------ #

    def _think_value(self) -> Any:
        effort = self.options.get("reasoning_effort")
        if effort is not None:
            return effort
        return self.options.get("think", True)

    def _payload(self, messages: List[Message]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "think": self._think_value(),
        }
        if self.tools_enabled:
            payload["tools"] = toolbox.TOOL_SCHEMAS
        passthrough = {
            k: v for k, v in self.options.items()
            if k not in ("reasoning_effort", "think")
        }
        if passthrough:
            payload["options"] = passthrough
        return payload

    # ------------------------------------------------------------------ #
    # Streaming + tool loop
    # ------------------------------------------------------------------ #

    async def stream(self, messages: List[Message]) -> AsyncIterator[Event]:
        # Work on a private copy because the tool loop appends turns.
        convo: List[Message] = list(messages)
        url = f"{self.host}/api/chat"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                for _round in range(_MAX_TOOL_ROUNDS):
                    content_parts: List[str] = []
                    tool_calls: List[Dict[str, Any]] = []
                    done_meta: Dict[str, Any] = {}

                    async with client.stream("POST", url, json=self._payload(convo)) as resp:
                        if resp.status_code == 404:
                            await resp.aread()
                            yield Event("error",
                                        f"Model '{self.model}' not found. Run: ollama pull {self.model}",
                                        {"status": 404})
                            return
                        if resp.status_code != 200:
                            body = (await resp.aread()).decode(errors="replace")
                            yield Event("error",
                                        f"Ollama HTTP {resp.status_code}: {body[:300]}",
                                        {"status": resp.status_code})
                            return

                        async for line in resp.aiter_lines():
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                chunk = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if chunk.get("error"):
                                yield Event("error", str(chunk["error"]), {})
                                return

                            msg = chunk.get("message") or {}
                            if msg.get("thinking"):
                                yield Event("reasoning", msg["thinking"], {})
                            if msg.get("content"):
                                yield Event("answer", msg["content"], {})
                                content_parts.append(msg["content"])
                            if msg.get("tool_calls"):
                                tool_calls.extend(msg["tool_calls"])

                            if chunk.get("done"):
                                done_meta = {
                                    "input_tokens": chunk.get("prompt_eval_count"),
                                    "output_tokens": chunk.get("eval_count"),
                                    "done_reason": chunk.get("done_reason"),
                                }
                                break

                    # No tool calls (or tools disabled) -> this turn is the answer.
                    if not (self.tools_enabled and tool_calls):
                        yield Event("done", "", done_meta)
                        return

                    # Otherwise: record the assistant turn, run the tools, loop.
                    convo.append({
                        "role": "assistant",
                        "content": "".join(content_parts),
                        "tool_calls": tool_calls,
                    })
                    for tc in tool_calls:
                        fn = tc.get("function", {}) or {}
                        name = fn.get("name", "")
                        args = fn.get("arguments", {})
                        yield Event("reasoning", f"\n🔧 {toolbox.summarize_call(name, args)}\n", {})
                        result = await toolbox.execute_tool(name, args, self.workdir)
                        preview = result if len(result) <= 400 else result[:400] + " …"
                        yield Event("reasoning", f"↳ {preview}\n", {})
                        convo.append({"role": "tool", "content": result, "tool_name": name})

                # Ran out of tool rounds.
                yield Event("done", "", {"note": f"stopped after {_MAX_TOOL_ROUNDS} tool rounds"})

        except httpx.ConnectError:
            yield Event("error",
                        f"Cannot reach the Ollama daemon at {self.host}. Is `ollama serve` running?",
                        {})
        except httpx.TimeoutException:
            yield Event("error", f"Ollama request timed out after {self.timeout:.0f}s.", {})
        except Exception as exc:
            yield Event("error", f"Ollama provider error: {exc}", {})
