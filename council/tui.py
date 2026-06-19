"""Textual TUI — live split-pane view of the council.

Layout:
  - Header: question · current stage · elapsed timer.
  - Responsive grid of member panes. Each pane: title (name · backend · status ·
    token count) and a scrollable body. Reasoning text is dimmed/italic; answer
    text is normal weight.
  - A full-width Chairman pane pinned at the bottom for the final synthesis.

The orchestrator (council.run_council) streams events through an on_event
callback; because the council coroutine runs inside this app's event loop, the
callback updates widgets directly. Members run concurrently and their panes
update independently as tokens arrive.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Grid, Vertical, VerticalScroll
from textual.widgets import Static

from .council import Member, RunResult, run_council


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_STATUS_GLYPH = {
    "idle": "·",
    "thinking": None,    # spinner
    "writing": None,     # spinner
    "ranking": None,     # spinner
    "done": "✓",
    "error": "✗",
}


class MemberPane(Vertical):
    """One council member's live pane."""

    def __init__(self, member: Member, is_chairman: bool = False) -> None:
        super().__init__()
        self.member = member
        self.is_chairman = is_chairman
        self.status = "idle"
        self.title_override = None
        self._reasoning = Text()
        self._answer = Text()
        self._tokens = 0
        self.add_class("chairman" if is_chairman else "member")

    def compose(self) -> ComposeResult:
        yield Static(self._title(), classes="pane-title")
        with VerticalScroll(classes="pane-body"):
            yield Static("", classes="pane-text", markup=False)

    # ---- title -------------------------------------------------------- #

    def _status_glyph(self, frame: int) -> str:
        glyph = _STATUS_GLYPH.get(self.status)
        if glyph is None:  # active -> spinner
            return _SPINNER[frame % len(_SPINNER)]
        return glyph

    def set_title_override(self, text) -> None:
        self.title_override = text
        self.refresh_title()

    def _title(self, frame: int = 0) -> Text:
        t = Text()
        if self.title_override:
            t.append(self.title_override, style="bold green")
        else:
            prefix = "★ " if self.is_chairman else "◆ "
            t.append(prefix + self.member.name, style="bold cyan")
            t.append(f"  {self.member.backend}/{self.member.model}", style="dim")
        glyph = self._status_glyph(frame)
        style = "green" if self.status == "done" else ("red" if self.status == "error" else "yellow")
        t.append(f"   {glyph} {self.status}", style=style)
        if self._tokens:
            t.append(f"  ·  {self._tokens} tok", style="dim")
        return t

    def refresh_title(self, frame: int = 0) -> None:
        try:
            self.query_one(".pane-title", Static).update(self._title(frame))
        except Exception:
            pass

    # ---- body --------------------------------------------------------- #

    def _render_body(self) -> Text:
        body = Text()
        if self._reasoning:
            body.append_text(self._reasoning)
            if self._answer:
                body.append("\n\n")
        body.append_text(self._answer)
        return body

    def _update_body(self) -> None:
        try:
            self.query_one(".pane-text", Static).update(self._render_body())
            self.query_one(".pane-body", VerticalScroll).scroll_end(animate=False)
        except Exception:
            pass

    # ---- event hooks -------------------------------------------------- #

    def start_stage(self, status: str) -> None:
        self.status = status
        self._reasoning = Text()
        self._answer = Text()
        self._tokens = 0
        self._update_body()
        self.refresh_title()

    def add_reasoning(self, text: str) -> None:
        if self.status not in ("done", "error"):
            self.status = "thinking"
        self._reasoning.append(text, style="dim italic")
        self._tokens += _approx_tokens(text)
        self._update_body()

    def add_answer(self, text: str) -> None:
        if self.status not in ("done", "error"):
            self.status = "ranking" if self.member._in_review else "writing"
        self._answer.append(text)
        self._tokens += _approx_tokens(text)
        self._update_body()

    def mark_done(self, meta: dict) -> None:
        self.status = "done"
        out = meta.get("output_tokens")
        if out:
            self._tokens = out
        self.refresh_title()

    def mark_error(self, message: str) -> None:
        self.status = "error"
        self._answer.append(("\n" if self._answer else "") + f"⚠ {message}", style="red")
        self._update_body()
        self.refresh_title()


def _approx_tokens(text: str) -> int:
    # Rough live proxy until the backend reports real counts on 'done'.
    return max(1, len(text) // 4)


class CouncilApp(App):
    """The council TUI application."""

    CSS = """
    Screen { layout: vertical; }
    #header {
        height: 3; padding: 0 1; background: $panel; color: $text;
        border-bottom: solid $primary;
    }
    #grid {
        layout: grid;
        grid-gutter: 1 2;
        grid-rows: 1fr;
        height: 1fr;
        padding: 1 1 0 1;
    }
    .member {
        border: round $primary;
        height: 100%;
        padding: 0 1;
    }
    #chairman-wrap { height: 35%; min-height: 8; padding: 1 1; }
    .chairman { border: round $success; height: 100%; padding: 0 1; }
    .pane-title { height: 1; }
    .pane-body { height: 1fr; }
    .pane-text { padding: 0; }
    """

    BINDINGS = [("q", "quit", "Quit"), ("ctrl+c", "quit", "Quit")]

    def __init__(self, members: List[Member], chairman_name: str,
                 question: str, max_concurrent: int, autorun: bool = True,
                 research: bool = False, researcher_model=None) -> None:
        super().__init__()
        self.members = members
        self.chairman_name = chairman_name
        self.question = question
        self.max_concurrent = max_concurrent
        self.autorun = autorun
        self.research = research
        self.researcher_model = researcher_model
        self.result: Optional[RunResult] = None
        self.stage = "—"
        self._elapsed = 0
        self._frame = 0
        self._panes: Dict[str, MemberPane] = {}
        for m in members:
            m._in_review = False  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        yield Static(self._header_text(), id="header")
        cols = self._columns()
        grid = Grid(id="grid")
        grid.styles.grid_size_columns = cols
        with grid:
            for m in self.members:
                pane = MemberPane(m)
                self._panes[m.name] = pane
                yield pane
        with Vertical(id="chairman-wrap"):
            chair = next(m for m in self.members if m.name == self.chairman_name)
            cpane = MemberPane(chair, is_chairman=True)
            self._panes["__chairman__"] = cpane
            yield cpane

    # ---- header ------------------------------------------------------- #

    def _columns(self) -> int:
        # Wider, more readable panes: aim for ~64-col panes, max 3 across.
        width = self.size.width or 120
        n = len(self.members)
        cols = max(1, min(3, width // 56))
        # Avoid a lonely last row (e.g. 4 members -> 2x2 rather than 3+1).
        if cols == 3 and n == 4:
            cols = 2
        return min(cols, n)

    def _header_text(self) -> Text:
        t = Text()
        t.append("🏛  LLM COUNCIL", style="bold")
        t.append(f"   stage: {self.stage}", style="bold yellow")
        t.append(f"   ⏱ {self._elapsed}s", style="dim")
        t.append("\n")
        q = self.question if len(self.question) < 140 else self.question[:137] + "…"
        t.append(q, style="dim")
        return t

    def _refresh_header(self) -> None:
        try:
            self.query_one("#header", Static).update(self._header_text())
        except Exception:
            pass

    # ---- lifecycle ---------------------------------------------------- #

    def on_mount(self) -> None:
        self.set_interval(0.1, self._tick)
        self.set_interval(1.0, self._tick_timer)
        if self.autorun:
            self.run_worker(self._drive(), exclusive=True)

    def on_resize(self) -> None:
        try:
            self.query_one("#grid", Grid).styles.grid_size_columns = self._columns()
        except Exception:
            pass

    def _tick(self) -> None:
        self._frame += 1
        for pane in self._panes.values():
            if pane.status in ("thinking", "writing", "ranking"):
                pane.refresh_title(self._frame)

    def _tick_timer(self) -> None:
        if self.stage not in ("—", "DONE"):
            self._elapsed += 1
            self._refresh_header()

    # ---- council event routing --------------------------------------- #

    def on_stage_change(self, stage: str) -> None:
        self.stage = stage
        self._refresh_header()
        cp = self._panes["__chairman__"]
        if stage == "RESEARCH":
            cp.set_title_override("🔎 Researcher · web search")
            cp.start_stage("thinking")
            return
        if stage == "CHAIRMAN":
            cp.set_title_override(None)
            cp.start_stage("thinking")
            return
        active = {
            "OPINIONS": ("thinking", False),
            "REVIEW": ("thinking", True),
        }.get(stage, ("idle", False))
        status, in_review = active
        for m in self.members:
            m._in_review = in_review  # type: ignore[attr-defined]
            self._panes[m.name].start_stage(status)

    def on_member_event(self, member: Member, event) -> None:
        # Research events (member "Researcher") render in the bottom pane.
        if self.stage == "RESEARCH":
            pane = self._panes["__chairman__"]
        elif self.stage == "CHAIRMAN" and member.name == self.chairman_name:
            pane = self._panes["__chairman__"]
        else:
            pane = self._panes.get(member.name)
        if pane is None:
            return
        if event.type == "reasoning":
            pane.add_reasoning(event.text)
        elif event.type == "answer":
            pane.add_answer(event.text)
        elif event.type == "done":
            pane.mark_done(event.meta)
        elif event.type == "error":
            pane.mark_error(event.text)

    async def _drive(self) -> None:
        def on_event(member: Member, event) -> None:
            self.on_member_event(member, event)

        def on_stage(stage: str) -> None:
            self.on_stage_change(stage)

        self.result = await run_council(
            self.members, self.chairman_name, self.question,
            max_concurrent=self.max_concurrent,
            on_event=on_event, on_stage=on_stage,
            research=self.research, researcher_model=self.researcher_model,
        )
        self.stage = "DONE"
        self._refresh_header()
        # Ensure chairman pane shows the final answer text.
        cpane = self._panes["__chairman__"]
        cpane.status = "done"
        cpane.refresh_title()


def run_tui(members: List[Member], chairman_name: str,
            question: str, max_concurrent: int,
            research: bool = False, researcher_model=None) -> Optional[RunResult]:
    app = CouncilApp(members, chairman_name, question, max_concurrent,
                     research=research, researcher_model=researcher_model)
    app.run()
    return app.result
