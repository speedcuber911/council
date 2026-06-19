# council

**An LLM council in your terminal.** Ask one question; a council of models each
answers independently, peer-reviews the *anonymized* answers, and a chairman
synthesizes the final response — live in a split-pane TUI (or streamed plainly
for pipes/CI).

No web UI, no API keys to manage. It drives the agent CLIs you're already logged
into:

| Backend | Driver | Auth |
|---|---|---|
| **Claude Code** | `claude -p` subprocess (stream-json) | local Claude login (keychain/OAuth) |
| **Codex** | `codex exec --json` subprocess | saved ChatGPT/Codex login |
| **Ollama cloud** | `POST localhost:11434/api/chat` | `ollama signin` |

All members run at **maximum reasoning effort** (Claude/Codex `xhigh`, Ollama `max`).

## The protocol (3 stages)

1. **OPINIONS** — the question goes to every member in parallel; each answers independently.
2. **REVIEW** — the answers are anonymized + shuffled as "Response 1..N" and sent
   to every member, which ranks them. Rankings are aggregated by **Borda count**.
3. **CHAIRMAN** — the chairman gets the question, the labeled answers, and the
   aggregate ranking, and writes the final synthesized answer.

A member that errors or times out shows its error and is dropped from review
aggregation and the chairman context; the run continues with the rest.

## Install

**Homebrew (recommended):**

```bash
brew tap speedcuber911/council
brew install council
```

> If your Homebrew has `HOMEBREW_REQUIRE_TAP_TRUST` set, run
> `brew trust speedcuber911/council` once after tapping.

**Or with uv / pipx (no Homebrew):**

```bash
uv tool install git+https://github.com/speedcuber911/council
# or: pipx install git+https://github.com/speedcuber911/council
```

Then check your setup:

```bash
council doctor        # which backends you have + what's missing
council doctor --probe   # live-test auth/signin
```

### Backends (use any subset — council adapts)

You don't need all three. council runs with whatever you've got installed and
signed in; missing backends are skipped, and the chairman is chosen from what's
available.

```bash
claude          # log in once (Claude Code)            → https://claude.com/claude-code
codex login     # saved ChatGPT/Codex auth             → npm i -g @openai/codex
ollama signin   # cloud access for open-weight models  → https://ollama.com/download
```

`:cloud` Ollama models aren't downloaded — they run server-side and just need
`ollama signin`. Edit your roster anytime with `council init` (writes
`~/.config/council/council.yaml`).

## Usage

```bash
council "What is the most underrated idea in distributed systems?"
echo "your question" | council          # from stdin

council --plain "..."                    # no TUI; stream to stdout (pipe/CI)
council --save run.md "..."              # write full transcript
council --chairman "GPT-5.5" "..."       # override chairman for this run
council --only "Claude Opus 4.8,GPT-5.5" "..."   # subset of members
council --max-concurrent 6 "..."         # cap simultaneous calls (default 4)
council --config ./council.yaml "..."    # custom roster
```

The run ends with a one-line summary: per-stage timing, total wall time, members
succeeded, total tokens.

## Research mode (`--research`) — shared web grounding

The Ollama members have no real web access, so a question about current events
would put them at a disadvantage. `--research` fixes that with a **Stage 0**: it
uses **Claude Code's native WebSearch** to research the question once, writes a
neutral briefing (facts, figures, named sources/URLs), and injects that briefing
into *every* member before they answer — so the whole council starts from the
same up-to-date footing.

```bash
council --research "What's the state of AI agents in 2026?"
council --research --researcher-model sonnet "..."   # faster research pass
```

- The researcher runs Claude Code restricted to `WebSearch`/`WebFetch` only — no
  bash, no filesystem.
- The briefing flows into Stage 1 (opinions), Stage 2 (review), and Stage 3
  (chairman), and is saved at the top of `--save` transcripts.
- Independent of `--tools`: `--research` is about *web context fairness*;
  `--tools` is about *file/bash access*. Use either, both, or neither.

## Agent mode (`--tools`)

By default members are **pure chat** — they answer from the question text alone
and can't touch your machine. Add `--tools` to turn every member into an agent
with **full access** (read / write / bash / web) rooted at the current directory:

```bash
cd ~/my-project
council --tools "What does this codebase do? Read the files and summarize."
council --tools --workdir ~/other-repo "Find and explain the auth bug."
```

How each backend gets tools:

| Backend | Mechanism |
|---|---|
| **Claude Code** | native tools (`Read/Bash/WebSearch/WebFetch/…`), `--dangerously-skip-permissions` in your dir |
| **Codex** | `--sandbox workspace-write` + network, run in your dir |
| **Ollama** | a client-side function-calling loop in `tools.py` (`read_file`, `list_dir`, `bash`, `web_fetch`, `web_search`) — the model requests a tool, the council executes it locally and feeds the result back |

⚠️ **`--tools` is genuinely powerful and unsandboxed for the Ollama members**: six
frontier models can run arbitrary shell commands, write files, and reach the
network in `--workdir`. It prints a warning and a Ctrl-C window before starting.
Point it at a repo you trust it to poke at. Without `--tools`, none of this is on.

## The roster is config-driven

Edit [`council/council.yaml`](council/council.yaml) — adding/removing a member is
a pure config change, no code. Each entry:

```yaml
- name: "GLM 5.2 (max)"
  backend: ollama            # claude_code | codex | ollama
  model: glm-5.2:cloud
  options: {reasoning_effort: max}   # -> --effort (claude/codex) or think: (ollama)
```

`reasoning_effort` is mapped per backend: `claude_code` → `--effort <v>`,
`codex` → `-c model_reasoning_effort=<v>`, `ollama` → `think: <v>`.
Max values: `xhigh` for Claude/Codex, `max` for Ollama.

## Layout

```
council/
  council.yaml              # the roster
  providers/
    base.py                 # Provider interface + Event(reasoning|answer|done|error)
    ollama.py               # HTTP NDJSON streaming
    claude_code.py          # claude -p stream-json subprocess
    codex.py                # codex exec --json subprocess
  prompts.py                # review + chairman templates (ported from upstream)
  council.py                # orchestration: 3 stages, anonymize/shuffle, Borda
  tui.py                    # Textual split-pane live view
  cli.py                    # entrypoint; picks TUI vs --plain
```

## Notes from wiring against the live CLIs

- Codex model id is **`gpt-5.5`** — ChatGPT auth rejects the `-codex` suffix.
- Ollama's top reasoning level is **`max`** (it rejects `xhigh`).
- Codex does not expose chain-of-thought text (only a reasoning-token count), so
  its pane shows a "thinking" pause then the answer; Claude and Ollama stream
  their reasoning live.
- These CLIs change often — providers were built after confirming each tool's
  current `--help`/event shape.

## Credits

council is an independent, from-scratch implementation. The deliberation protocol
— independent answers, *anonymized* peer ranking, then chairman synthesis — was
introduced by Andrej Karpathy's [llm-council](https://github.com/karpathy/llm-council);
council reimagines it as a backend-agnostic terminal tool over local agent CLIs.

## License

MIT — see [LICENSE](LICENSE).
