# CLAUDE.md — Technical notes for LLM Council (terminal edition)

A terminal CLI/TUI that runs a 3-stage council over three **local** backends
(Claude Code, Codex, Ollama cloud). The original web app (Flask/FastAPI backend,
React frontend, OpenRouter client) has been removed.

## Architecture

```
council/
  council.yaml          # roster: chairman + members (backend, model, options)
  providers/
    base.py             # Provider ABC + Event{type: reasoning|answer|done|error}
    ollama.py           # HTTP NDJSON stream to localhost:11434/api/chat
    claude_code.py      # `claude -p --output-format stream-json` subprocess
    codex.py            # `codex exec --json` subprocess
  prompts.py            # review + chairman prompt templates (ported from upstream)
  council.py            # config loading, 3-stage orchestration, Borda aggregation
  tui.py                # Textual split-pane live view
  cli.py                # argparse entrypoint; chooses TUI vs --plain; --save
```

### The Event contract (providers/base.py)
Every backend is a `Provider` with `async def stream(messages) -> AsyncIterator[Event]`.
`Event.type` is one of `reasoning` (CoT, dimmed in TUI), `answer` (final tokens),
`done` (terminal success; `meta` has `input_tokens`/`output_tokens`), `error`
(terminal failure; `text` is the message). Providers MUST emit exactly one
terminal event and must NOT raise on expected failures (timeout, bad model, not
signed in) — surface them as `error` so one member can't crash the council.

### Orchestration (council.py)
`run_council(members, chairman_name, question, max_concurrent, on_event, on_stage)`
runs the three stages. It's UI-agnostic: both cli.py (plain) and tui.py pass
callbacks. `on_event(member, event)` fires for every streamed event; `on_stage`
fires on stage transitions. Members run concurrently under an `asyncio.Semaphore`.

- **Stage 1 OPINIONS**: each member answers the raw question in parallel.
- **Stage 2 REVIEW**: successful answers are shuffled into "Response 1..N"
  (`number_to_member` map), each member ranks them, rankings are parsed
  (`parse_ranking`) and scored by **Borda count** (position `p` of `n` → `n-p`
  points). Needs ≥2 surviving members or it's skipped.
- **Stage 3 CHAIRMAN**: chairman gets labeled answers + aggregate ranking.

A member with `error` set is excluded from review aggregation and the chairman
context; the run continues.

## Backend specifics (verified against the installed CLIs)

- **Ollama**: `think` field accepts bool or effort string; top level is **`max`**
  (NOT `xhigh`). `message.thinking` → reasoning, `message.content` → answer,
  final line `done:true` with `prompt_eval_count`/`eval_count`. 404 → tell user
  to `ollama pull <id>`.
- **Claude Code**: needs `--include-partial-messages` for token streaming. Parse
  `stream_event` → `content_block_delta`: `text_delta`=answer,
  `thinking_delta`=reasoning, `signature_delta`=ignore. `result` event = done
  with `usage`. Effort via `--effort xhigh`. Do NOT use `--bare` (skips OAuth).
  Runs in a temp cwd with `--allowedTools ""` so it's a chat model, not an agent.
- **Codex**: model id is **`gpt-5.5`** (ChatGPT auth rejects `gpt-5.5-codex`).
  Needs `--skip-git-repo-check` (we run in a temp dir). `item.completed`/
  `agent_message` = answer, `turn.completed` = done with `usage`. CoT text is NOT
  exposed (only `reasoning_output_tokens`). Effort via
  `-c model_reasoning_effort=xhigh`. `--ephemeral` + `--sandbox read-only`.

`options.reasoning_effort` in council.yaml is mapped per backend to the knobs
above. All members are configured at max effort.

## Gotchas

- **Python 3.10 target**: no nested same-quote f-strings (`f"{x['k']}"` is fine,
  `f"{f'{x['k']}'}"` is not). Pull values into locals first.
- **Subprocess line limit**: `create_subprocess_exec(..., limit=2**20)` — Claude's
  init/result JSON lines can exceed the 64 KiB default.
- **Chairman must be in the active roster** (also after `--only` filtering).
- Per-member timeouts surface as `error` events; the whole council never hangs.

## Dev smoke tests (throwaway, repo root)

- `smoke_provider.py <backend> <model> <effort> "q"` — one provider end-to-end.
- `smoke_tui.py` — headless Textual run via `run_test()` feeding synthetic events.

## Run

```bash
uv sync
council "question"                  # TUI
council --plain "question"          # stdout
council --only "A,B" --chairman B --max-concurrent 6 --save out.md "question"
```
