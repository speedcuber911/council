"""council — terminal entrypoint.

Sends one question to a council of models, runs the 3-stage protocol, and renders
the result. Defaults to the live Textual TUI; --plain streams to stdout instead
(good for piping / CI).

Usage:
    council "your question"
    echo "your question" | council
    council --plain "your question"
    council --only "Claude Opus 4.8,GPT-5.5" --chairman "GPT-5.5" "question"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from . import __version__
from .council import load_council, run_council, RunResult, Member
from .paths import resolve_config, init_user_config, USER_CONFIG


# --------------------------------------------------------------------------- #
# Styling
# --------------------------------------------------------------------------- #

class S:
    on = sys.stdout.isatty()

    @classmethod
    def _c(cls, code, t):
        return f"\033[{code}m{t}\033[0m" if cls.on else t

    @classmethod
    def bold(cls, t): return cls._c("1", t)
    @classmethod
    def dim(cls, t): return cls._c("2", t)
    @classmethod
    def red(cls, t): return cls._c("31", t)
    @classmethod
    def green(cls, t): return cls._c("32", t)
    @classmethod
    def yellow(cls, t): return cls._c("33", t)
    @classmethod
    def cyan(cls, t): return cls._c("36", t)


def _member_tag(m: Member) -> str:
    return S.bold(S.cyan(f"◆ {m.name}")) + S.dim(f"  [{m.backend}/{m.model}]")


def _opt_value(argv, flag):
    """Pull the value following `flag` out of a raw argv list (for subcommands)."""
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


# --------------------------------------------------------------------------- #
# Plain renderer — prints each stage as it completes
# --------------------------------------------------------------------------- #

def _print_stage_header(title: str) -> None:
    print()
    print(S.bold(f"═══ {title} ═══"))
    print()


def _print_opinions(members) -> None:
    for m in members:
        print(_member_tag(m))
        if not m.ok:
            print("  " + S.red(f"error: {m.error}"))
            print()
            continue
        if m.answer_reasoning.strip():
            preview = m.answer_reasoning.strip()
            print(S.dim("  reasoning: " + _indent(preview, "  ").strip()))
        print(_indent(m.answer.strip(), "  "))
        print()


def _print_review(members, aggregate) -> None:
    for m in members:
        if not m.ok:
            continue
        ranks = ", ".join(f"R{n}" for n in m.ranking) if m.ranking else "(no ballot)"
        print(_member_tag(m) + S.dim(f"  ranked: {ranks}"))
    print()
    if aggregate:
        print(S.bold("Aggregate (Borda, best first):"))
        medals = ["🥇", "🥈", "🥉"]
        for row in aggregate:
            r = row["rank"]
            badge = medals[r - 1] if r <= 3 else f" {r}."
            score = row["score"]
            print(f"  {badge} {S.cyan(row['name'])} {S.dim('· score ' + str(score))}")
        print()


def _print_chairman(result: RunResult) -> None:
    if result.final_reasoning.strip():
        print(S.dim("reasoning:"))
        print(S.dim(_indent(result.final_reasoning.strip(), "  ")))
        print()
    bar = "═" * 70
    print(S.green(bar))
    print(S.bold(S.green("FINAL ANSWER")) + S.dim(f"  · chairman: {result.chairman_name}"))
    print(S.green(bar))
    print(result.final_answer.strip())
    print(S.green(bar))


def _print_summary(result: RunResult, wall: float) -> None:
    t = result.timings
    tin, tout = result.total_tokens()
    n_ok = sum(1 for m in result.members if m.ok)
    research = f"research {t['research']:.1f}s · " if "research" in t else ""
    print()
    print(S.dim(
        f"{research}opinions {t['opinions']:.1f}s · review {t['review']:.1f}s · "
        f"chairman {t['chairman']:.1f}s · total {wall:.1f}s · "
        f"{n_ok}/{len(result.members)} members · "
        f"tokens in {tin:,} / out {tout:,}"
    ))


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + ln for ln in text.splitlines())


async def run_plain(members, chairman_name, question, max_concurrent,
                    research=False, researcher_model=None) -> RunResult:
    print(S.bold("🏛  LLM COUNCIL"))
    print(S.dim(f"Question: {question}"))
    print(S.dim("Members: " + ", ".join(m.name for m in members)))
    print(S.dim(f"Chairman: {chairman_name}"))
    if research:
        print(S.dim("Research: web pre-search via Claude Code (shared briefing)"))

    stage_titles = {
        "RESEARCH": "STAGE 0 · RESEARCH",
        "OPINIONS": "STAGE 1 · OPINIONS",
        "REVIEW": "STAGE 2 · REVIEW",
        "CHAIRMAN": "STAGE 3 · CHAIRMAN",
    }

    def on_stage(stage: str) -> None:
        if stage == "RESEARCH":
            print(S.dim("\n🔎 Researching the web for shared context…"))
            return
        if stage == "OPINIONS":
            print(S.dim(f"\nQuerying {len(members)} members in parallel…"))
        elif stage == "REVIEW":
            print(S.dim("\nMembers ranking anonymized answers…"))
        elif stage == "CHAIRMAN":
            print(S.dim(f"\n{chairman_name} synthesizing…"))

    wall0 = time.monotonic()
    result = await run_council(
        members, chairman_name, question,
        max_concurrent=max_concurrent,
        on_stage=on_stage,
        research=research,
        researcher_model=researcher_model,
    )
    wall = time.monotonic() - wall0

    # Print collected output per stage (after the fact; live view is the TUI).
    if result.research_briefing:
        _print_stage_header(stage_titles["RESEARCH"])
        print(result.research_briefing.strip())
        print()
    _print_stage_header(stage_titles["OPINIONS"])
    _print_opinions(result.members)
    _print_stage_header(stage_titles["REVIEW"])
    _print_review(result.members, result.aggregate_ranking)
    _print_stage_header(stage_titles["CHAIRMAN"])
    _print_chairman(result)
    _print_summary(result, wall)
    return result


# --------------------------------------------------------------------------- #
# Transcript saving
# --------------------------------------------------------------------------- #

def save_transcript(path: str, result: RunResult) -> None:
    lines = [f"# LLM Council transcript", "", f"**Question:** {result.question}", ""]
    if result.research_briefing:
        lines.append("## Stage 0 — Research briefing (web)\n")
        lines.append(result.research_briefing.strip() + "\n")
    lines.append("## Stage 1 — Opinions\n")
    for m in result.members:
        lines.append(f"### {m.name}  ·  `{m.backend}/{m.model}`\n")
        if not m.ok:
            lines.append(f"> error: {m.error}\n")
            continue
        if m.answer_reasoning.strip():
            lines.append("<details><summary>reasoning</summary>\n")
            lines.append("```\n" + m.answer_reasoning.strip() + "\n```\n")
            lines.append("</details>\n")
        lines.append(m.answer.strip() + "\n")

    lines.append("## Stage 2 — Review\n")
    for m in result.members:
        if not m.ok:
            continue
        ranks = ", ".join(f"Response {n}" for n in m.ranking) or "(no ballot)"
        lines.append(f"**{m.name}** ranked: {ranks}\n")
        if m.review_text.strip():
            lines.append("<details><summary>full review</summary>\n")
            lines.append("```\n" + m.review_text.strip() + "\n```\n")
            lines.append("</details>\n")
    lines.append("\n**Aggregate (Borda):**\n")
    for row in result.aggregate_ranking:
        lines.append(f"{row['rank']}. {row['name']} — score {row['score']}")
    lines.append("")

    lines.append(f"## Stage 3 — Chairman ({result.chairman_name})\n")
    lines.append(result.final_answer.strip() + "\n")

    t = result.timings
    tin, tout = result.total_tokens()
    lines.append("---\n")
    lines.append(
        f"_timings: opinions {t['opinions']:.1f}s · review {t['review']:.1f}s · "
        f"chairman {t['chairman']:.1f}s · tokens in {tin:,} / out {tout:,}_"
    )
    Path(path).write_text("\n".join(lines))
    print(S.dim(f"\nTranscript saved to {path}"))


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #

def parse_args(argv):
    p = argparse.ArgumentParser(prog="council", description="LLM Council CLI")
    p.add_argument("question", nargs="*", help="the question (or pipe via stdin)")
    p.add_argument("--plain", action="store_true", help="no TUI; stream to stdout")
    p.add_argument("--save", metavar="FILE.md", help="write full transcript to disk")
    p.add_argument("--chairman", metavar="NAME", help="override chairman for this run")
    p.add_argument("--only", metavar="A,B,C", help="run a subset of members by name")
    p.add_argument("--max-concurrent", type=int, default=4, help="cap simultaneous calls")
    p.add_argument("--config", default=None,
                   help="council config file (default: ~/.config/council/council.yaml or bundled)")
    p.add_argument("--tools", action="store_true",
                   help="give members FULL agent access (read/write/bash/web) in the working dir")
    p.add_argument("--workdir", metavar="PATH",
                   help="folder agents operate in with --tools (default: current dir)")
    p.add_argument("--research", action="store_true",
                   help="Stage 0: web-research the question via Claude Code, brief all members")
    p.add_argument("--researcher-model", metavar="MODEL",
                   help="Claude model for the research pass (default: roster's Claude model)")
    p.add_argument("--version", action="store_true", help="print version and exit")
    return p.parse_args(argv)


def _welcome() -> None:
    print(S.bold("🏛  council") + S.dim(f"  v{__version__}"))
    print(S.dim("an LLM council in your terminal — Claude · Codex · Ollama cloud\n"))
    print("Usage:")
    print('  council "your question here"')
    print('  echo "your question" | council')
    print()
    print("Get started:")
    print(S.bold("  council doctor") + S.dim("    check your setup (which backends you have)"))
    print(S.bold("  council init") + S.dim("      create an editable roster at ~/.config/council"))
    print(S.dim("\nFlags: --plain --research --tools --only --chairman --save  (council -h)"))


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)

    # Subcommands (kept simple so `council "question"` stays the default).
    if raw and raw[0] == "doctor":
        from .doctor import run_doctor
        probe = "--probe" in raw
        cfg = _opt_value(raw, "--config")
        return run_doctor(cfg, probe)
    if raw and raw[0] == "init":
        path, created = init_user_config("--force" in raw)
        if created:
            print(S.green(f"✓ Created editable roster: {path}"))
            print(S.dim("Edit it to add/remove members, then run `council doctor`."))
        else:
            print(S.yellow(f"Roster already exists: {path}"))
            print(S.dim("Use `council init --force` to overwrite with defaults."))
        return 0
    if raw and raw[0] in ("version", "--version", "-V"):
        print(f"council {__version__}")
        return 0

    args = parse_args(raw)
    if args.version:
        print(f"council {__version__}")
        return 0

    question = " ".join(args.question).strip()
    if not question and not sys.stdin.isatty():
        question = sys.stdin.read().strip()
    if not question:
        _welcome()
        return 0

    only = [s.strip() for s in args.only.split(",")] if args.only else None
    config = str(resolve_config(args.config))

    import os
    workdir = os.path.abspath(args.workdir) if args.workdir else os.getcwd()
    if args.tools:
        if not os.path.isdir(workdir):
            print(S.red(f"--workdir not a directory: {workdir}"), file=sys.stderr)
            return 2
        print(S.yellow(
            "⚠  --tools: council members get FULL access (read/write/bash/web) in:\n"
            f"   {workdir}\n"
            "   models may run arbitrary commands here. Ctrl-C now to abort.\n"
        ), file=sys.stderr)

    try:
        members, chairman_name, skipped = load_council(
            config, only, args.chairman,
            workdir=workdir, tools_enabled=args.tools,
        )
    except (ValueError, FileNotFoundError) as e:
        print(S.red(f"{e}"), file=sys.stderr)
        return 2

    if skipped:
        names = ", ".join(f"{n} ({b} not installed)" for n, b in skipped)
        print(S.yellow(f"Skipping unavailable members: {names}"), file=sys.stderr)

    # Research needs Claude Code; warn + disable if it's not present.
    research = args.research
    if research and not any(m.backend == "claude_code" for m in members):
        from . import backends
        if not backends.available().get("claude_code"):
            print(S.yellow("--research needs Claude Code (web search); it's not "
                           "installed, so skipping the research stage."), file=sys.stderr)
            research = False

    if args.plain:
        result = asyncio.run(run_plain(
            members, chairman_name, question, args.max_concurrent,
            research=research, researcher_model=args.researcher_model))
    else:
        try:
            from .tui import run_tui
        except ImportError as e:
            print(S.yellow(f"TUI unavailable ({e}); falling back to --plain.\n"), file=sys.stderr)
            result = asyncio.run(run_plain(
                members, chairman_name, question, args.max_concurrent,
                research=research, researcher_model=args.researcher_model))
        else:
            wall0 = time.monotonic()
            result = run_tui(members, chairman_name, question, args.max_concurrent,
                             research=research, researcher_model=args.researcher_model)
            # After the live TUI closes, echo the final answer + summary to stdout
            # so the run is captured in scrollback / pipes.
            if result is not None:
                print(S.bold("🏛  LLM COUNCIL — final answer"))
                print(S.dim(f"chairman: {result.chairman_name}\n"))
                print(result.final_answer.strip())
                _print_summary(result, time.monotonic() - wall0)

    if args.save and result is not None:
        save_transcript(args.save, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
