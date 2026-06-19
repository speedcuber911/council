"""`council doctor` — a friendly onboarding / environment check.

Verifies the three backends council drives (Claude Code, Codex, Ollama cloud)
are installed and signed in, and that the roster's Ollama models are pulled.
Prints a clear table with fix-it commands for anything missing.

  council doctor           fast, free local checks
  council doctor --probe   also makes tiny live calls to confirm auth works
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from typing import List, Optional, Tuple

import httpx
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .paths import resolve_config, USER_CONFIG


OLLAMA_HOST = "http://localhost:11434"

OK = "[green]✓[/green]"
BAD = "[red]✗[/red]"
WARN = "[yellow]●[/yellow]"


def _banner(console: Console) -> None:
    art = Text()
    art.append("\n  🏛  council", style="bold cyan")
    art.append(f"  v{__version__}\n", style="dim")
    art.append("  an LLM council in your terminal — Claude · Codex · Ollama cloud\n", style="dim")
    console.print(art)


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _run(cmd: List[str], timeout: float = 10.0) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _ollama_models() -> Optional[List[str]]:
    try:
        r = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=5.0)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return None


def _roster_ollama_models(config_path) -> List[str]:
    try:
        data = yaml.safe_load(config_path.read_text())
    except Exception:
        return []
    return [
        m["model"] for m in data.get("members", [])
        if m.get("backend") == "ollama"
    ]


async def _probe_ollama(model: str) -> Tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{OLLAMA_HOST}/api/chat",
                json={"model": model, "messages": [{"role": "user", "content": "ok"}],
                      "stream": False},
            )
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            err = r.json().get("error")
            return (err is None), (err or "responded")
    except Exception as e:
        return False, str(e)


async def _probe_claude() -> Tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "--model", "haiku", "--allowedTools", "",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(input=b"say ok"), timeout=60.0)
        text = (out.decode() + err.decode()).lower()
        if "/login" in text or "not logged in" in text or "invalid api key" in text:
            return False, "not logged in (run: claude)"
        if proc.returncode != 0:
            return False, (err.decode().strip()[:60] or "error")
        return True, "responded"
    except Exception as e:
        return False, str(e)


def run_doctor(config: Optional[str] = None, probe: bool = False) -> int:
    console = Console()
    _banner(console)

    config_path = resolve_config(config)
    rows: List[Tuple[str, str, str]] = []  # (status, component, detail)
    fixes: List[str] = []          # blocking issues for backends you DO have
    optional: List[str] = []       # install hints for backends you don't
    usable = 0                     # count of backends ready to contribute

    # ---- Claude Code (optional) ----
    if _which("claude"):
        if probe:
            ok, detail = asyncio.run(_probe_claude())
            rows.append((OK if ok else BAD, "Claude Code", detail))
            if ok:
                usable += 1
            else:
                fixes.append("Log in to Claude Code:  [bold]claude[/bold]  (then /login)")
        else:
            rows.append((OK, "Claude Code", "installed (use --probe to verify login)"))
            usable += 1
    else:
        rows.append((WARN, "Claude Code", "not installed — optional"))
        optional.append("Add Claude Code (members + web research):  "
                        "[bold]https://claude.com/claude-code[/bold]")

    # ---- Codex (optional) ----
    if _which("codex"):
        code, out = _run(["codex", "login", "status"])
        logged_in = code == 0 and "logged in" in out.lower()
        rows.append((OK if logged_in else WARN, "Codex",
                     out.splitlines()[0] if out else ("ok" if logged_in else "not logged in")))
        if logged_in:
            usable += 1
        else:
            fixes.append("Log in to Codex:  [bold]codex login[/bold]")
    else:
        rows.append((WARN, "Codex", "not installed — optional"))
        optional.append("Add Codex (GPT member):  [bold]npm i -g @openai/codex[/bold]")

    # ---- Ollama (optional) ----
    if _which("ollama"):
        models = _ollama_models()
        if models is None:
            rows.append((BAD, "Ollama daemon", "installed but not responding on :11434"))
            fixes.append("Start Ollama:  [bold]ollama serve[/bold]")
        else:
            rows.append((OK, "Ollama daemon", f"running · {len(models)} local model(s)"))
            usable += 1
            roster = _roster_ollama_models(config_path)
            cloud = [m for m in roster if m.endswith(":cloud")]
            local = [m for m in roster if not m.endswith(":cloud")]
            # Cloud models aren't "pulled" — they live server-side and need signin.
            for m in cloud:
                rows.append((WARN if not probe else OK, f"  {m}",
                             "cloud · needs `ollama signin`" if not probe else "cloud"))
            for m in local:
                if m in models:
                    rows.append((OK, f"  {m}", "pulled"))
                else:
                    rows.append((WARN, f"  {m}", "not pulled"))
                    fixes.append(f"Pull model:  [bold]ollama pull {m}[/bold]")
            if probe and roster:
                ok, detail = asyncio.run(_probe_ollama(roster[0]))
                rows.append((OK if ok else BAD, "Ollama cloud signin", detail))
                if not ok:
                    fixes.append("Sign in to Ollama cloud:  [bold]ollama signin[/bold]")
            elif cloud and not probe:
                fixes.append("Verify Ollama cloud:  [bold]ollama signin[/bold] "
                             "(then `council doctor --probe`)")
    else:
        rows.append((WARN, "Ollama", "not installed — optional"))
        optional.append("Add Ollama (open-weight members):  "
                        "[bold]https://ollama.com/download[/bold]")

    # ---- Render ----
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2, 0, 0))
    table.add_column("", width=1)
    table.add_column("Component")
    table.add_column("Status", style="dim")
    for status, comp, detail in rows:
        table.add_row(status, comp, detail)
    console.print(table)
    console.print()

    cfg_note = f"{config_path}"
    if config_path == USER_CONFIG:
        cfg_note += "  [dim](user config)[/dim]"
    elif not USER_CONFIG.exists():
        cfg_note += "  [dim](bundled default · run `council init` to customize)[/dim]"
    console.print(f"[dim]Config:[/dim] {cfg_note}")
    console.print()

    if fixes:
        body = "\n".join(f"• {f}" for f in dict.fromkeys(fixes))
        console.print(Panel(body, title="[yellow]To finish setup[/yellow]",
                            border_style="yellow", expand=False))

    if optional:
        body = "\n".join(f"• {o}" for o in dict.fromkeys(optional))
        console.print(Panel(body, title="[dim]Optional — add more council members[/dim]",
                            border_style="dim", expand=False))

    console.print()
    if usable == 0:
        console.print(Panel(
            "[red]No backends are ready yet.[/red] Install at least one of "
            "Claude Code, Codex, or Ollama above — council runs with whatever you have.",
            border_style="red", expand=False))
        return 1

    plural = "backend" if usable == 1 else "backends"
    msg = f"[green]Ready — {usable} {plural} available.[/green] council will use what you have.\n"
    msg += 'Try:  [bold]council "what is the most underrated idea in software engineering?"[/bold]'
    console.print(Panel(msg, border_style="green", expand=False))
    if not probe:
        console.print("\n[dim]Run [bold]council doctor --probe[/bold] to live-test auth & signin.[/dim]")
    return 0
