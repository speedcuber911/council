"""Council orchestration: config loading, the 3-stage protocol, aggregation.

Stage 1 OPINIONS : every member answers the question independently, in parallel.
Stage 2 REVIEW   : answers are anonymized + shuffled into "Response 1..N"; each
                   member ranks the full set; rankings are Borda-aggregated.
Stage 3 CHAIRMAN : the chairman synthesizes a final answer from the labeled
                   answers + the aggregate ranking.

This module is UI-agnostic. It exposes async functions that accept an optional
`on_event(member, event)` callback so both the plain CLI and the Textual TUI can
render the same live streams. Members that error are excluded from aggregation
and the chairman context; the run continues with the rest.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from .providers.base import Event, Message, Provider
from .providers.ollama import OllamaProvider
from .providers.claude_code import ClaudeCodeProvider
from .providers.codex import CodexProvider
from . import prompts, backends


_BACKENDS = {
    "ollama": OllamaProvider,
    "claude_code": ClaudeCodeProvider,
    "codex": CodexProvider,
}

# Callback invoked for every streamed event, tagged with the member it came from.
EventCallback = Optional[Callable[["Member", Event], None]]


# --------------------------------------------------------------------------- #
# Config + members
# --------------------------------------------------------------------------- #

@dataclass
class Member:
    """A council member: its provider plus accumulated results across stages."""

    name: str
    backend: str
    model: str
    options: Dict[str, Any]
    provider: Provider

    # Stage 1
    answer: str = ""
    answer_reasoning: str = ""
    # Stage 2
    review_text: str = ""
    review_reasoning: str = ""
    ranking: List[int] = field(default_factory=list)  # ordered Response numbers
    # Bookkeeping
    error: Optional[str] = None
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None


def build_provider(
    entry: Dict[str, Any],
    workdir: Optional[str] = None,
    tools_enabled: bool = False,
) -> Provider:
    backend = entry["backend"]
    if backend not in _BACKENDS:
        raise ValueError(f"Unknown backend '{backend}' for member '{entry.get('name')}'")
    cls = _BACKENDS[backend]
    return cls(
        name=entry["name"],
        model=entry["model"],
        options=entry.get("options") or {},
        workdir=workdir,
        tools_enabled=tools_enabled,
    )


def load_council(
    config_path: str | Path,
    only: Optional[List[str]] = None,
    chairman_override: Optional[str] = None,
    workdir: Optional[str] = None,
    tools_enabled: bool = False,
) -> Tuple[List[Member], str, List[Tuple[str, str]]]:
    """Load council.yaml into Member objects, keeping only members whose backend
    is installed/usable.

    Returns (active_members, chairman_name, skipped) where skipped is a list of
    (member_name, backend) dropped because that backend isn't available.
    """
    data = yaml.safe_load(Path(config_path).read_text())
    configured_chairman = chairman_override or data.get("chairman")
    avail = backends.available()

    active: List[Member] = []
    skipped: List[Tuple[str, str]] = []
    for entry in data.get("members", []):
        if only and entry["name"] not in only:
            continue
        backend = entry["backend"]
        if not avail.get(backend, False):
            skipped.append((entry["name"], backend))
            continue
        active.append(
            Member(
                name=entry["name"],
                backend=backend,
                model=entry["model"],
                options=entry.get("options") or {},
                provider=build_provider(entry, workdir, tools_enabled),
            )
        )

    if not active:
        missing = sorted({b for _, b in skipped})
        raise ValueError(
            "No council members available — none of the required backends are "
            f"installed/running ({', '.join(missing) or 'none configured'}). "
            "Run `council doctor` to see what's missing."
        )

    # Pick a chairman that's actually present.
    names = {m.name for m in active}
    if configured_chairman in names:
        chairman = configured_chairman
    else:
        # Prefer a Claude Code member, else the first available member.
        claude = next((m.name for m in active if m.backend == "claude_code"), None)
        chairman = claude or active[0].name
    return active, chairman, skipped


# --------------------------------------------------------------------------- #
# Streaming a single member
# --------------------------------------------------------------------------- #

async def _consume(
    member: Member,
    messages: List[Message],
    sem: asyncio.Semaphore,
    on_event: EventCallback,
) -> Tuple[str, str, Dict[str, Any], Optional[str]]:
    """Drain one member's event stream. Returns (answer, reasoning, meta, error)."""
    answer: List[str] = []
    reasoning: List[str] = []
    meta: Dict[str, Any] = {}
    error: Optional[str] = None
    async with sem:
        async for ev in member.provider.stream(messages):
            if on_event:
                on_event(member, ev)
            if ev.type == "answer":
                answer.append(ev.text)
            elif ev.type == "reasoning":
                reasoning.append(ev.text)
            elif ev.type == "done":
                meta = ev.meta
            elif ev.type == "error":
                error = ev.text
    return "".join(answer), "".join(reasoning), meta, error


def _record_tokens(member: Member, meta: Dict[str, Any]) -> None:
    member.tokens_in += meta.get("input_tokens") or 0
    member.tokens_out += meta.get("output_tokens") or 0


# --------------------------------------------------------------------------- #
# Stage 1 — opinions
# --------------------------------------------------------------------------- #

async def run_opinions(
    members: List[Member],
    question: str,
    sem: asyncio.Semaphore,
    on_event: EventCallback = None,
    research_context: Optional[str] = None,
) -> None:
    content = prompts.with_research_context(question, research_context)
    messages = [{"role": "user", "content": content}]

    async def one(m: Member) -> None:
        ans, rsn, meta, err = await _consume(m, messages, sem, on_event)
        m.answer, m.answer_reasoning = ans, rsn
        m.error = err or (None if ans.strip() else "empty answer")
        _record_tokens(m, meta)

    await asyncio.gather(*(one(m) for m in members))


# --------------------------------------------------------------------------- #
# Stage 2 — review + Borda aggregation
# --------------------------------------------------------------------------- #

_RANK_LINE = re.compile(r"^\s*\d+\.\s*Response\s+(\d+)", re.IGNORECASE)


def parse_ranking(text: str, n: int) -> List[int]:
    """Extract ordered Response numbers from a member's FINAL RANKING block."""
    if "FINAL RANKING" in text.upper():
        text = re.split(r"FINAL RANKING:?", text, flags=re.IGNORECASE)[-1]
    order: List[int] = []
    for line in text.splitlines():
        mo = _RANK_LINE.match(line)
        if mo:
            num = int(mo.group(1))
            if 1 <= num <= n and num not in order:
                order.append(num)
    if not order:  # fallback: any "Response k" mentions in order
        for mo in re.finditer(r"Response\s+(\d+)", text, re.IGNORECASE):
            num = int(mo.group(1))
            if 1 <= num <= n and num not in order:
                order.append(num)
    return order


async def run_review(
    members: List[Member],
    question: str,
    sem: asyncio.Semaphore,
    on_event: EventCallback = None,
    research_context: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[int, Member]]:
    """Stage 2. Returns (aggregate_ranking, response_number -> member map).

    aggregate_ranking: best-first list of {name, score, rank}.
    """
    contributors = [m for m in members if m.ok]
    if len(contributors) < 2:
        return [], {}

    # Anonymize + shuffle. response_no (1..N) -> member.
    shuffled = contributors[:]
    random.shuffle(shuffled)
    number_to_member: Dict[int, Member] = {
        i + 1: m for i, m in enumerate(shuffled)
    }
    anonymized = [m.answer for m in shuffled]
    n = len(anonymized)
    prompt = prompts.with_research_context(
        prompts.review_prompt(question, anonymized), research_context
    )
    messages = [{"role": "user", "content": prompt}]

    async def one(m: Member) -> None:
        text, rsn, meta, err = await _consume(m, messages, sem, on_event)
        m.review_text, m.review_reasoning = text, rsn
        _record_tokens(m, meta)
        if err:
            # A failed reviewer simply casts no ballot; it stays in the council.
            m.ranking = []
        else:
            m.ranking = parse_ranking(text, n)

    # Only members that produced a stage-1 answer get to vote.
    await asyncio.gather(*(one(m) for m in contributors))

    # Borda count: in a ranking of n, position p (1-based) earns (n - p) points.
    scores: Dict[int, int] = {num: 0 for num in number_to_member}
    for voter in contributors:
        for pos, resp_no in enumerate(voter.ranking, start=1):
            if resp_no in scores:
                scores[resp_no] += (n - pos)

    ordered = sorted(
        number_to_member.keys(),
        key=lambda num: scores[num],
        reverse=True,
    )
    aggregate = []
    for rank, resp_no in enumerate(ordered, start=1):
        aggregate.append(
            {
                "name": number_to_member[resp_no].name,
                "score": scores[resp_no],
                "rank": rank,
                "response_no": resp_no,
            }
        )
    return aggregate, number_to_member


# --------------------------------------------------------------------------- #
# Stage 3 — chairman synthesis
# --------------------------------------------------------------------------- #

async def run_chairman(
    chairman: Member,
    question: str,
    members: List[Member],
    aggregate_ranking: List[Dict[str, Any]],
    sem: asyncio.Semaphore,
    on_event: EventCallback = None,
    research_context: Optional[str] = None,
) -> Tuple[str, str]:
    labeled = [
        {"name": m.name, "answer": m.answer} for m in members if m.ok
    ]
    prompt = prompts.with_research_context(
        prompts.chairman_prompt(question, labeled, aggregate_ranking), research_context
    )
    messages = [{"role": "user", "content": prompt}]
    answer, reasoning, meta, err = await _consume(chairman, messages, sem, on_event)
    _record_tokens(chairman, meta)
    if err:
        return f"[Chairman error: {err}]", reasoning
    return answer, reasoning


# --------------------------------------------------------------------------- #
# Full run + summary
# --------------------------------------------------------------------------- #

async def run_research(
    researcher: Member,
    question: str,
    sem: asyncio.Semaphore,
    on_event: EventCallback = None,
) -> Optional[str]:
    """Stage 0. Web-research the question; return a shared briefing (or None)."""
    prompt = prompts.research_prompt(question)
    messages = [{"role": "user", "content": prompt}]
    text, _rsn, meta, err = await _consume(researcher, messages, sem, on_event)
    researcher.answer = text
    researcher.error = err
    _record_tokens(researcher, meta)
    if err or not text.strip():
        return None
    return text.strip()


def _make_researcher(
    members: List[Member], researcher_model: Optional[str]
) -> Optional[Member]:
    """Build the web researcher (Claude Code restricted to web tools).

    Returns None when Claude Code isn't available — the only backend with native
    web search — so the council degrades gracefully and just skips research.
    """
    if not backends.available().get("claude_code"):
        return None
    model = researcher_model
    if not model:
        claude_member = next((m for m in members if m.backend == "claude_code"), None)
        model = claude_member.model if claude_member else "opus"
    provider = ClaudeCodeProvider(
        name="Researcher", model=model, allowed_tools="WebSearch WebFetch"
    )
    return Member(name="Researcher", backend="claude_code", model=model,
                  options={}, provider=provider)


@dataclass
class RunResult:
    question: str
    members: List[Member]
    chairman_name: str
    aggregate_ranking: List[Dict[str, Any]]
    final_answer: str
    final_reasoning: str
    timings: Dict[str, float]
    research_briefing: Optional[str] = None

    def total_tokens(self) -> Tuple[int, int]:
        return (
            sum(m.tokens_in for m in self.members),
            sum(m.tokens_out for m in self.members),
        )


async def run_council(
    members: List[Member],
    chairman_name: str,
    question: str,
    max_concurrent: int = 4,
    on_event: EventCallback = None,
    on_stage: Optional[Callable[[str], None]] = None,
    research: bool = False,
    researcher_model: Optional[str] = None,
    researcher_member: Optional[Member] = None,
) -> RunResult:
    sem = asyncio.Semaphore(max_concurrent)
    timings: Dict[str, float] = {}
    chairman = next(m for m in members if m.name == chairman_name)

    briefing: Optional[str] = None
    if research:
        researcher = researcher_member or _make_researcher(members, researcher_model)
        if researcher is not None:
            if on_stage:
                on_stage("RESEARCH")
            t = time.monotonic()
            briefing = await run_research(researcher, question, sem, on_event)
            timings["research"] = time.monotonic() - t

    if on_stage:
        on_stage("OPINIONS")
    t = time.monotonic()
    await run_opinions(members, question, sem, on_event, briefing)
    timings["opinions"] = time.monotonic() - t

    if on_stage:
        on_stage("REVIEW")
    t = time.monotonic()
    aggregate, _ = await run_review(members, question, sem, on_event, briefing)
    timings["review"] = time.monotonic() - t

    if on_stage:
        on_stage("CHAIRMAN")
    t = time.monotonic()
    final, final_rsn = await run_chairman(
        chairman, question, members, aggregate, sem, on_event, briefing
    )
    timings["chairman"] = time.monotonic() - t

    return RunResult(
        question=question,
        members=members,
        chairman_name=chairman_name,
        aggregate_ranking=aggregate,
        final_answer=final,
        final_reasoning=final_rsn,
        timings=timings,
        research_briefing=briefing,
    )
