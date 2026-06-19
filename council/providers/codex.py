"""Codex provider — subprocess streaming via the `codex exec` CLI.

Runs `codex exec --json` and parses its newline-delimited event stream:

  item.completed / agent_message  -> answer text (Codex emits the full message;
                                     it does not stream tokens incrementally)
  item.completed / reasoning      -> reasoning text, when exposed
  turn.completed                  -> done (+ token usage)
  error / turn.failed             -> error

Notes confirmed against the installed CLI:
  * Model id is `gpt-5.5` (ChatGPT auth rejects the `-codex` suffix).
  * Codex does NOT expose chain-of-thought text, only a reasoning token count,
    so most runs surface a single answer event after a "thinking" pause.
  * `--skip-git-repo-check` is required because we run in an empty temp dir.
  * `--ephemeral` avoids persisting rollout files; `--sandbox read-only` plus the
    temp cwd keep it from touching this repo.

Auth: reuses the saved ChatGPT/Codex login on this machine.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from typing import Any, AsyncIterator, Dict, List

from .base import Event, Message, Provider


DEFAULT_TIMEOUT = 300.0
_STREAM_LIMIT = 2 ** 20  # 1 MiB per line


def _messages_to_prompt(messages: List[Message]) -> str:
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


class CodexProvider(Provider):
    """Streams a Codex model through the local `codex exec` CLI."""

    backend = "codex"

    def __init__(
        self,
        name: str,
        model: str,
        options: Dict[str, Any] | None = None,
        workdir: str | None = None,
        tools_enabled: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        super().__init__(name, model, options, workdir, tools_enabled)
        self.timeout = timeout

    def _argv(self, prompt: str) -> List[str]:
        argv = [
            "codex", "exec",
            "--json",
            "--skip-git-repo-check",
            "--model", self.model,
            "--ephemeral",
        ]
        if self.tools_enabled:
            # Agent in the user's folder: write to workspace + network for web.
            argv += [
                "--sandbox", "workspace-write",
                "-c", "sandbox_workspace_write.network_access=true",
            ]
        else:
            argv += ["--sandbox", "read-only"]
        effort = self.options.get("reasoning_effort")
        if effort:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        argv.append(prompt)  # prompt as argv; stdin is kept empty (DEVNULL)
        return argv

    async def stream(self, messages: List[Message]) -> AsyncIterator[Event]:
        prompt = _messages_to_prompt(messages)
        tmpdir = None if self.tools_enabled else tempfile.mkdtemp(prefix="council-codex-")
        cwd = self.workdir if self.tools_enabled else tmpdir
        proc = None
        emitted_terminal = False
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv(prompt),
                stdin=asyncio.subprocess.DEVNULL,  # don't let codex wait on / append stdin
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                limit=_STREAM_LIMIT,
            )

            async def read_events() -> AsyncIterator[Event]:
                nonlocal emitted_terminal
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    t = d.get("type")

                    if t == "item.completed":
                        item = d.get("item", {}) or {}
                        itype = item.get("type")
                        if itype == "agent_message" and item.get("text"):
                            yield Event("answer", item["text"], {})
                        elif itype == "reasoning" and item.get("text"):
                            yield Event("reasoning", item["text"], {})
                        elif itype in ("command_execution", "local_shell_call"):
                            cmd = item.get("command") or item.get("aggregated_output") or ""
                            yield Event("reasoning", f"\n🔧 bash: {cmd}\n", {})
                        elif itype in ("file_change", "patch_apply"):
                            yield Event("reasoning", f"\n🔧 {itype}\n", {})
                        continue

                    if t == "turn.completed":
                        usage = d.get("usage", {}) or {}
                        yield Event(
                            "done",
                            "",
                            {
                                "input_tokens": usage.get("input_tokens"),
                                "output_tokens": usage.get("output_tokens"),
                                "reasoning_tokens": usage.get("reasoning_output_tokens"),
                            },
                        )
                        emitted_terminal = True
                        return

                    if t in ("error", "turn.failed"):
                        msg = d.get("message")
                        if not msg:
                            err = d.get("error", {})
                            msg = err.get("message") if isinstance(err, dict) else str(err)
                        # Codex wraps API errors as a JSON string; surface it readably.
                        yield Event("error", _clean_error(msg), {})
                        emitted_terminal = True
                        return

            try:
                agen = read_events()
                while True:
                    try:
                        ev = await asyncio.wait_for(agen.__anext__(), timeout=self.timeout)
                    except StopAsyncIteration:
                        break
                    yield ev
            except asyncio.TimeoutError:
                yield Event("error", f"Codex timed out after {self.timeout:.0f}s.", {})
                return

            if not emitted_terminal:
                err = (await proc.stderr.read()).decode(errors="replace").strip()
                if err:
                    yield Event("error", f"Codex exited without result: {err[:400]}", {})
                else:
                    yield Event("done", "", {"note": "stream ended without turn.completed"})

        except FileNotFoundError:
            yield Event("error", "`codex` CLI not found on PATH.", {})
        except Exception as exc:
            yield Event("error", f"Codex provider error: {exc}", {})
        finally:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
            if tmpdir is not None:
                shutil.rmtree(tmpdir, ignore_errors=True)


def _clean_error(msg: Any) -> str:
    """Codex reports API errors as an embedded JSON string; pull out the message."""
    if not isinstance(msg, str):
        return str(msg)
    try:
        parsed = json.loads(msg)
        inner = parsed.get("error", {})
        if isinstance(inner, dict) and inner.get("message"):
            return inner["message"]
    except (json.JSONDecodeError, AttributeError):
        pass
    return msg
