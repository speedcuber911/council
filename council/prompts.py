"""Prompt templates for the council protocol.

Ported and adapted from karpathy/llm-council's stage prompts. Two templates:
the REVIEW prompt (stage 2 — each member ranks the anonymized answers) and the
CHAIRMAN prompt (stage 3 — synthesize a final answer from everything).
"""

from __future__ import annotations

from typing import Dict, List


def research_prompt(question: str) -> str:
    """Stage-0 prompt for the web researcher (Claude Code with WebSearch)."""
    return f"""You are the research aide for an expert council. Your job is NOT to
answer the question — it is to gather current, factual web context so every
council member starts from the same well-informed footing.

Question the council will answer:
{question}

Use web search and fetch to collect what's relevant: key facts and figures,
recent developments, competing viewpoints, definitions, named sources, and any
data that bears on the question. Then write a concise, neutral briefing:

- Lead with the most decision-relevant facts.
- Include concrete numbers, dates, and named sources/URLs where you have them.
- Note genuine disagreement or uncertainty rather than papering over it.
- Do NOT answer the question or argue a position; just brief the council.

Keep it tight (a few hundred words). Output only the briefing."""


def with_research_context(content: str, briefing: str | None) -> str:
    """Prepend the shared web briefing to a stage prompt, if present."""
    if not briefing:
        return content
    return (
        "A shared web-research briefing is provided so all council members are "
        "grounded in the same up-to-date context.\n\n"
        "=== RESEARCH BRIEFING ===\n"
        f"{briefing}\n"
        "=== END BRIEFING ===\n\n"
        f"{content}"
    )


def review_prompt(question: str, anonymized_answers: List[str]) -> str:
    """Build the stage-2 ranking prompt.

    `anonymized_answers` is the already-shuffled list of answer texts; they are
    presented as "Response 1..N" with no model identities.
    """
    blocks = []
    for i, ans in enumerate(anonymized_answers, start=1):
        blocks.append(f"Response {i}:\n{ans}")
    responses_text = "\n\n".join(blocks)

    n = len(anonymized_answers)
    return f"""You are a member of an expert council evaluating answers to a question.

Question:
{question}

Below are {n} answers from different council members, presented anonymously and
in random order. Evaluate them on accuracy, depth, reasoning, and usefulness.

{responses_text}

Your task:
1. Briefly assess each response's strengths and weaknesses.
2. Then output a final ranking from best to worst.

Format the ranking EXACTLY like this, as the LAST thing in your reply:

FINAL RANKING:
1. Response <number> - <one-line justification>
2. Response <number> - <one-line justification>
... (rank ALL {n} responses, best first)

Use only the response numbers shown above. Do not invent responses."""


def chairman_prompt(
    question: str,
    labeled_answers: List[Dict[str, str]],
    aggregate_ranking: List[Dict[str, object]],
) -> str:
    """Build the stage-3 synthesis prompt for the chairman.

    `labeled_answers` : [{"name": member name, "answer": text}, ...]
    `aggregate_ranking`: [{"name", "score", "rank"}, ...] best-first.
    """
    answers_text = "\n\n".join(
        f"[{a['name']}]\n{a['answer']}" for a in labeled_answers
    )

    ranking_lines = []
    for row in aggregate_ranking:
        ranking_lines.append(
            f"{row['rank']}. {row['name']} (council score: {row['score']})"
        )
    ranking_text = "\n".join(ranking_lines) if ranking_lines else "(no rankings available)"

    return f"""You are the Chairman of an expert council. Each member independently
answered the question below, then the members peer-ranked each other's answers
anonymously. Your job is to deliver the single best final answer.

Original question:
{question}

Council members' answers (attributed):
{answers_text}

Aggregated peer ranking (best first, by council score):
{ranking_text}

As Chairman:
- Synthesize the strongest, most accurate answer to the original question.
- Lean on the highest-ranked contributions, but incorporate good points from any
  member and correct any errors you notice.
- Be decisive and self-contained: the reader should not need to see the council's
  individual answers. Do not mention the ranking mechanics or that you are a
  chairman; just deliver the definitive answer."""
