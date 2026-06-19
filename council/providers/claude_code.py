"""Claude Code provider — subprocess streaming via the `claude` CLI.

Runs `claude -p --output-format stream-json --include-partial-messages` and
parses the newline-delimited JSON event stream:

  stream_event / content_block_delta / text_delta      -> answer tokens
  stream_event / content_block_delta / thinking_delta  -> reasoning tokens
  result                                                -> done (+ token usage)

Auth: uses the local Claude Code login (keychain/OAuth). We deliberately do NOT
pass --bare (bare mode skips OAuth and would require ANTHROPIC_API_KEY).

These are pure chat calls, so we neutralize the agent: the subprocess runs in a
fresh empty temp dir with `--allowedTools ""` so it can't read or touch this repo.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from typing import Any, AsyncIterator, Dict, List

from .base import Event, Message, Provider


DEFAULT_TIMEOUT = 300.0
_STREAM_LIMIT = 2 ** 20  # 1 MiB per line; init/result events can be large


def _messages_to_prompt(messages: List[Message]) -> str:
    """Flatten chat messages into a single prompt string fed via stdin."""
    parts = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            parts.append(f"[System instructions]\n{content}")
        elif role == "assistant":
            parts.append(f"[Assistant]\n{content}")
        else:
            parts.append(content)
    return "\n\n".join(parts)


class ClaudeCodeProvider(Provider):
    """Streams a Claude model through the local `claude` CLI."""

    backend = "claude_code"

    def __init__(
        self,
        name: str,
        model: str,
        options: Dict[str, Any] | None = None,
        workdir: str | None = None,
        tools_enabled: bool = False,
        allowed_tools: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(name, model, options, workdir, tools_enabled)
        # When set (e.g. "WebSearch WebFetch"), restrict to exactly these tools —
        # used for the web-only research pass, no bash/write/filesystem.
        self.allowed_tools = allowed_tools
        self.timeout = timeout

    def _argv(self) -> List[str]:
        argv = [
            "claude",
            "-p",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--model", self.model,
        ]
        if self.allowed_tools is not None:
            # Restricted toolset (pre-approved, no dangerous perms needed).
            argv += ["--allowedTools", self.allowed_tools]
        elif self.tools_enabled:
            # Full agent: native tools (Read/Bash/WebSearch/WebFetch/…) in the
            # user's folder, with permissions bypassed for non-interactive use.
            argv += ["--add-dir", self.workdir, "--dangerously-skip-permissions"]
        else:
            argv += ["--allowedTools", ""]  # neutralize -> pure chat model
        # Max out reasoning when configured (council.yaml options.reasoning_effort).
        effort = self.options.get("reasoning_effort")
        if effort:
            argv += ["--effort", effort]
        return argv

    async def stream(self, messages: List[Message]) -> AsyncIterator[Event]:
        prompt = _messages_to_prompt(messages)
        # Agent mode runs in the user's folder; pure-chat mode in a throwaway dir.
        tmpdir = None if self.tools_enabled else tempfile.mkdtemp(prefix="council-claude-")
        cwd = self.workdir if self.tools_enabled else tmpdir
        proc = None
        emitted_terminal = False
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                limit=_STREAM_LIMIT,
            )

            # Feed the prompt and close stdin so the model starts.
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()

            async def read_events() -> AsyncIterator[Event]:
                nonlocal emitted_terminal
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # skip partial/garbage line, keep going

                    t = d.get("type")

                    if t == "stream_event":
                        ev = d.get("event", {})
                        etype = ev.get("type")
                        if etype == "content_block_delta":
                            delta = ev.get("delta", {})
                            dtype = delta.get("type")
                            if dtype == "text_delta" and delta.get("text"):
                                yield Event("answer", delta["text"], {})
                            elif dtype == "thinking_delta" and delta.get("thinking"):
                                yield Event("reasoning", delta["thinking"], {})
                            # signature_delta / input_json_delta: ignore
                        elif etype == "content_block_start":
                            block = ev.get("content_block", {})
                            if block.get("type") == "tool_use":
                                yield Event("reasoning",
                                            f"\n🔧 {block.get('name', 'tool')}\n", {})
                        continue

                    if t == "result":
                        if d.get("is_error"):
                            msg = d.get("result") or d.get("error") or "Claude Code error"
                            yield Event("error", str(msg), {})
                        else:
                            usage = d.get("usage", {}) or {}
                            yield Event(
                                "done",
                                "",
                                {
                                    "input_tokens": usage.get("input_tokens"),
                                    "output_tokens": usage.get("output_tokens"),
                                    "duration_ms": d.get("duration_ms"),
                                },
                            )
                        emitted_terminal = True
                        return
                    # system / assistant / rate_limit_event: ignore

            # Enforce the per-member timeout across the whole stream.
            try:
                agen = read_events()
                while True:
                    try:
                        ev = await asyncio.wait_for(agen.__anext__(), timeout=self.timeout)
                    except StopAsyncIteration:
                        break
                    yield ev
            except asyncio.TimeoutError:
                yield Event("error", f"Claude Code timed out after {self.timeout:.0f}s.", {})
                return

            if not emitted_terminal:
                err = (await proc.stderr.read()).decode(errors="replace").strip()
                if err:
                    yield Event("error", f"Claude Code exited without result: {err[:400]}", {})
                else:
                    yield Event("done", "", {"note": "stream ended without result event"})

        except FileNotFoundError:
            yield Event("error", "`claude` CLI not found on PATH.", {})
        except Exception as exc:  # never let one member crash the council
            yield Event("error", f"Claude Code provider error: {exc}", {})
        finally:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
            if tmpdir is not None:
                shutil.rmtree(tmpdir, ignore_errors=True)
