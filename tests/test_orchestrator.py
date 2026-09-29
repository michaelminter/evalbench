"""End-to-end through the orchestrator using fake `claude`/`codex` executables."""

import asyncio
import json
import stat
import sys
from pathlib import Path

from evalbench.config import Settings
from evalbench.db import DB
from evalbench.orchestrator import EvalRequest, Manager

FAKE_CLAUDE = r'''
import json, sys, pathlib
prompt = sys.stdin.read()
if "impartial" in prompt:  # acting as judge
    import re
    ids = re.findall(r'<candidate id="(\w+)">', prompt)
    out = json.dumps({"scores": [{"id": i, "score": 9 - n, "rationale": "r"} for n, i in enumerate(ids)],
                      "ranking": ids, "summary": "ok"})
else:
    pathlib.Path("NEW.txt").write_text("made by claude\n")
    out = "claude says: " + prompt
def emit(o): print(json.dumps(o), flush=True)
emit({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": out}}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": out, "total_cost_usd": 0.5,
      "usage": {"input_tokens": 3, "output_tokens": 4}})
'''

FAKE_CODEX = r'''
import json, sys
sys.stdin.read()
if "bad-model" in sys.argv:
    print(json.dumps({"type": "error", "message": "unknown model"}))
    print(json.dumps({"type": "turn.failed", "error": {"message": "unknown model"}}))
    sys.exit(1)
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "codex answer"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 6}}))
'''


def _fake_bin(path: Path, code: str) -> str:
    path.write_text(f"#!{sys.executable}\n{code}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_full_eval_with_folder_and_judge(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("original\n")

    settings = Settings(
        claude_bin=_fake_bin(tmp_path / "claude", FAKE_CLAUDE),
        codex_bin=_fake_bin(tmp_path / "codex", FAKE_CODEX),
        data_dir=tmp_path / "data", workspace_root=tmp_path / "ws", run_timeout_s=30,
    )
    db = DB(settings.db_path)

    async def scenario():
        mgr = Manager(settings, db)
        eval_id = mgr.start(EvalRequest(
            prompt="hello", folder=src, max_parallel=2,
            combos=[("claude", "opus", "high"), ("codex", "gpt-x", "default"), ("codex", "bad-model", "low")],
            judge={"provider": "claude", "model": "opus", "effort": "default"},
        ))
        q, snapshot, done = mgr.subscribe(eval_id)
        assert not done and len(snapshot) == 6
        events = []
        while True:
            e = await asyncio.wait_for(q.get(), 30)
            events.append(e)
            if e["type"] == "complete":
                break
        return eval_id, events

    eval_id, events = asyncio.run(scenario())
    runs = {r["model"]: r for r in db.get_runs(eval_id)}

    assert runs["opus"]["status"] == "done"
    assert runs["opus"]["output"] == "claude says: hello"
    assert runs["opus"]["cost_usd"] == 0.5
    assert "NEW.txt" in runs["opus"]["diff"] and "made by claude" in runs["opus"]["diff"]
    assert (src / "NEW.txt").exists() is False  # original untouched

    assert runs["gpt-x"]["status"] == "done" and runs["gpt-x"]["output"] == "codex answer"
    assert runs["gpt-x"]["diff"] == ""
    assert runs["bad-model"]["status"] == "error" and "unknown model" in runs["bad-model"]["error"]

    ev = db.get_eval(eval_id)
    assert ev["status"] == "done" and ev["judge_status"] == "done", ev.get("judge_error")
    scores = ev["judge_result"]["scores"]
    assert set(scores) == {runs["opus"]["id"], runs["gpt-x"]["id"]}  # failed run not judged
    assert any(e["type"] == "text" for e in events)
