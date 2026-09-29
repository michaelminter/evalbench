"""LLM-as-judge: blind, order-randomized scoring of the finished runs."""

from __future__ import annotations

import json
import random
import re
import string

MAX_OUTPUT_CHARS = 24_000
MAX_DIFF_CHARS = 30_000

DEFAULT_RUBRIC = """\
- Correctness: is it right? Penalize factual errors, bugs, and hallucinated APIs heavily.
- Completeness: does it fully address everything the task asked for?
- Instruction following: does it respect every constraint in the task?
- Clarity and concision: is it well organized, without padding?"""

TEMPLATE = """\
You are an impartial expert evaluator comparing responses from different AI models to the same task.
The candidates are anonymized and presented in random order; do not let order, length, or style alone
influence you. Judge substance.

<task>
{prompt}
</task>

<rubric>
{rubric}
</rubric>

{candidates}

Score every candidate from 1 (useless) to 10 (excellent) against the rubric. Scores should
discriminate: reserve 9-10 for responses with no meaningful flaws.

Respond with ONLY a JSON object, no prose before or after, in exactly this shape:
{{"scores": [{{"id": "A", "score": 7, "rationale": "one or two sentences"}}],
 "ranking": ["A", "B"],
 "summary": "2-4 sentences comparing the candidates and naming the best one and why"}}
"""


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…[truncated, {len(text):,} chars total]"


def build_prompt(task_prompt: str, rubric: str | None, runs: list[dict], folder_mode: bool,
                 rng: random.Random | None = None) -> tuple[str, dict[str, str]]:
    """Return (judge prompt, {letter: run_id})."""
    rng = rng or random.Random()
    shuffled = runs[:]
    rng.shuffle(shuffled)
    letters = _labels(len(shuffled))
    mapping: dict[str, str] = {}
    parts = []
    for letter, run in zip(letters, shuffled):
        mapping[letter] = run["id"]
        body = f"<final_response>\n{_clip(run.get('output') or '(empty response)', MAX_OUTPUT_CHARS)}\n</final_response>"
        if folder_mode:
            body += f"\n<code_changes>\n{_clip(run.get('diff') or '(no file changes)', MAX_DIFF_CHARS)}\n</code_changes>"
        parts.append(f'<candidate id="{letter}">\n{body}\n</candidate>')
    prompt = TEMPLATE.format(
        prompt=task_prompt.strip(),
        rubric=(rubric or "").strip() or DEFAULT_RUBRIC,
        candidates="\n\n".join(parts),
    )
    return prompt, mapping


def _labels(n: int) -> list[str]:
    letters = list(string.ascii_uppercase)
    out = []
    for i in range(n):
        out.append(letters[i] if i < 26 else letters[i // 26 - 1] + letters[i % 26])
    return out


def extract_json(text: str) -> dict:
    """Find the judge's JSON object even if it wrapped it in prose or code fences."""
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict) and "scores" in obj:
            return obj
    raise ValueError("judge response did not contain a JSON object with 'scores'")


def parse_verdict(text: str, mapping: dict[str, str]) -> dict:
    """Map the judge's letters back to run ids."""
    obj = extract_json(text)
    scores: dict[str, dict] = {}
    for s in obj.get("scores") or []:
        run_id = mapping.get(str(s.get("id", "")).strip())
        if not run_id:
            continue
        try:
            score = float(s.get("score"))
        except (TypeError, ValueError):
            continue
        scores[run_id] = {"score": max(0.0, min(10.0, score)), "rationale": str(s.get("rationale", "")).strip()}
    if not scores:
        raise ValueError("judge returned no usable scores")
    ranking = [mapping[x] for x in (obj.get("ranking") or []) if x in mapping]
    if not ranking:
        ranking = sorted(scores, key=lambda r: -scores[r]["score"])
    return {"scores": scores, "ranking": ranking, "summary": str(obj.get("summary", "")).strip(),
            "labels": {v: k for k, v in mapping.items()}}
