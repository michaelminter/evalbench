import random

import pytest

from evalbench import judge

RUNS = [{"id": "r1", "output": "answer one"}, {"id": "r2", "output": "answer two", "diff": "+x"}]


def test_prompt_is_blind_and_mapped():
    prompt, mapping = judge.build_prompt("Do X", None, RUNS, folder_mode=True, rng=random.Random(1))
    assert sorted(mapping) == ["A", "B"] and sorted(mapping.values()) == ["r1", "r2"]
    assert "r1" not in prompt and "r2" not in prompt
    assert "answer one" in prompt and "<code_changes>" in prompt
    assert "Correctness" in prompt  # default rubric


def test_parse_verdict_with_prose_and_fences():
    text = 'Sure! Here you go:\n```json\n{"scores": [{"id": "A", "score": 8, "rationale": "good"},' \
           ' {"id": "B", "score": "4", "rationale": "meh"}, {"id": "Z", "score": 9}],' \
           ' "ranking": ["A", "B"], "summary": "A wins"}\n```'
    v = judge.parse_verdict(text, {"A": "r2", "B": "r1"})
    assert v["scores"] == {"r2": {"score": 8.0, "rationale": "good"}, "r1": {"score": 4.0, "rationale": "meh"}}
    assert v["ranking"] == ["r2", "r1"] and v["summary"] == "A wins"


def test_parse_verdict_ranking_fallback_and_errors():
    v = judge.parse_verdict('{"scores": [{"id": "A", "score": 3}, {"id": "B", "score": 7}]}', {"A": "r1", "B": "r2"})
    assert v["ranking"] == ["r2", "r1"]
    with pytest.raises(ValueError):
        judge.parse_verdict("no json here", {"A": "r1"})
