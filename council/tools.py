"""Local tool implementations for the Ollama function-calling loop.

Ollama cloud models are plain chat endpoints with function-calling support, but
no agent loop — so we define the tools here and execute them ourselves. Claude
Code and Codex do NOT use this module; they have their own native tools.

Access level: FULL (read + write + bash + web), per the council's configured
posture. Tools run with the user's local privileges, rooted at `workdir`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Dict, List

import httpx


# OpenAI/Ollama-style function schemas advertised to the model.
TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file. Path is relative to the working directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path to read"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the contents of a directory (relative to the working directory).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory path; defaults to '.'"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in the working directory and return its combined stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Shell command to execute"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a URL over HTTP(S) and return its text content (HTML stripped).",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Absolute http(s) URL"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web and return the top results (title, url, snippet).",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query"}},
                "required": ["query"],
            },
        },
    },
]

# Output caps so a giant file / command flood doesn't blow the context window.
_MAX_CHARS = 8000
_BASH_TIMEOUT = 60.0
_HTTP_TIMEOUT = 30.0


def _truncate(text: str, limit: int = _MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated, {len(text) - limit} more chars]"


def _safe_path(workdir: str, path: str) -> str:
    """Resolve a (possibly relative) path against workdir."""
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(workdir, path))


async def _read_file(workdir: str, path: str) -> str:
    full = _safe_path(workdir, path)
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            return _truncate(f.read())
    except FileNotFoundError:
        return f"Error: file not found: {path}"
    except IsADirectoryError:
        return f"Error: {path} is a directory (use list_dir)"
    except Exception as e:
        return f"Error reading {path}: {e}"


async def _list_dir(workdir: str, path: str = ".") -> str:
    full = _safe_path(workdir, path)
    try:
        entries = sorted(os.listdir(full))
    except FileNotFoundError:
        return f"Error: directory not found: {path}"
    except NotADirectoryError:
        return f"Error: {path} is not a directory"
    except Exception as e:
        return f"Error listing {path}: {e}"
    lines = []
    for name in entries:
        p = os.path.join(full, name)
        lines.append(f"{name}/" if os.path.isdir(p) else name)
    return _truncate("\n".join(lines) or "(empty)")


async def _bash(workdir: str, command: str) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=workdir,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_BASH_TIMEOUT)
        text = out.decode(errors="replace")
        return _truncate(text) if text.strip() else f"(exit {proc.returncode}, no output)"
    except asyncio.TimeoutError:
        return f"Error: command timed out after {_BASH_TIMEOUT:.0f}s"
    except Exception as e:
        return f"Error running command: {e}"


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


async def _web_fetch(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "llm-council/0.2"})
            r.raise_for_status()
            ctype = r.headers.get("content-type", "")
            body = r.text if "html" in ctype or "text" in ctype else f"[{ctype}, {len(r.content)} bytes]"
            return _truncate(_html_to_text(body) if "html" in ctype else body)
    except Exception as e:
        return f"Error fetching {url}: {e}"


async def _web_search(query: str) -> str:
    """Best-effort web search via DuckDuckGo's HTML endpoint."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            r = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers={"User-Agent": "Mozilla/5.0 llm-council/0.2"},
            )
            r.raise_for_status()
            html = r.text
    except Exception as e:
        return f"Error searching: {e}"

    results = []
    for m in re.finditer(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S
    ):
        url, title = m.group(1), _html_to_text(m.group(2))
        if title:
            results.append((title, url))
        if len(results) >= 6:
            break
    if not results:
        return "No results found."
    return "\n".join(f"{i+1}. {t}\n   {u}" for i, (t, u) in enumerate(results))


async def execute_tool(name: str, args: Dict[str, Any], workdir: str) -> str:
    """Dispatch a tool call to its implementation; always returns a string."""
    if isinstance(args, str):  # some models return a JSON string
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    try:
        if name == "read_file":
            return await _read_file(workdir, args.get("path", ""))
        if name == "list_dir":
            return await _list_dir(workdir, args.get("path", "."))
        if name == "bash":
            return await _bash(workdir, args.get("command", ""))
        if name == "web_fetch":
            return await _web_fetch(args.get("url", ""))
        if name == "web_search":
            return await _web_search(args.get("query", ""))
        return f"Error: unknown tool '{name}'"
    except Exception as e:  # never let a tool crash the loop
        return f"Error in tool {name}: {e}"


def summarize_call(name: str, args: Dict[str, Any]) -> str:
    """A one-line human-readable label for a tool call (shown in the TUI)."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    if name == "bash":
        return f"bash: {args.get('command', '')}"
    if name == "read_file":
        return f"read_file: {args.get('path', '')}"
    if name == "list_dir":
        return f"list_dir: {args.get('path', '.')}"
    if name == "web_fetch":
        return f"web_fetch: {args.get('url', '')}"
    if name == "web_search":
        return f"web_search: {args.get('query', '')}"
    return f"{name}({args})"
