from pathlib import Path

from evalbench.config import Settings, load_codex_models
from evalbench.runners import ClaudeParser, CodexParser, RunOptions, build_command


def feed_all(parser, events):
    out = []
    for e in events:
        out.extend(parser.feed(e))
    return out


def test_claude_stream_text_tools_and_result():
    p = ClaudeParser()
    ups = feed_all(p, [
        {"type": "system", "subtype": "init"},
        {"type": "stream_event", "event": {"type": "message_start"}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Hello"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
        ]}},
        {"type": "stream_event", "event": {"type": "message_start"}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Done"}}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "Done", "total_cost_usd": 0.01,
         "num_turns": 2, "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5,
                                   "cache_read_input_tokens": 100, "output_tokens": 40,
                                   "output_tokens_details": {"thinking_tokens": 12}}},
    ])
    text = "".join(u.data for u in ups if u.kind == "text")
    assert text == "Hello\n\nDone"  # assistant text block not duplicated
    assert [u.data for u in ups if u.kind == "activity"] == ["Bash: ls -la"]
    final = [u.data for u in ups if u.kind == "final"][0]
    assert final["output"] == "Done"
    assert final["input_tokens"] == 115 and final["cached_tokens"] == 100
    assert final["reasoning_tokens"] == 12 and final["cost_usd"] == 0.01
    assert "error" not in final


def test_claude_error_result():
    final = ClaudeParser().feed({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""})[0].data
    assert final["error"] == "error_max_turns"


def test_codex_events():
    s = Settings(codex_prices={"m": {"input": 1.0, "cached_input": 0.1, "output": 10.0}})
    p = CodexParser(s, "m")
    ups = feed_all(p, [
        {"type": "thread.started", "thread_id": "x"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Looking."}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "rg foo", "exit_code": 1}},
        {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "a.py", "kind": "update"}]}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Final answer"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1_000_000, "cached_input_tokens": 500_000,
                                             "output_tokens": 100_000, "reasoning_output_tokens": 7}},
    ])
    assert "".join(u.data for u in ups if u.kind == "text") == "Looking.\n\nFinal answer"
    assert [u.data for u in ups if u.kind == "activity"] == ["$ rg foo  (exit 1)", "files: update a.py"]
    final = ups[-1].data
    assert final["output"] == "Final answer"
    assert final["reasoning_tokens"] == 7
    assert abs(final["cost_usd"] - (0.5 + 0.05 + 1.0)) < 1e-9


def test_codex_failure():
    p = CodexParser()
    p.feed({"type": "error", "message": "model not found"})
    final = p.feed({"type": "turn.failed", "error": {"message": "model not found"}})[0].data
    assert final["error"] == "model not found"


def test_build_commands(tmp_path: Path):
    s = Settings()
    c = build_command(s, "claude", "opus", "high", tmp_path, RunOptions())
    assert c[:2] == ["claude", "-p"] and ["--effort", "high"] == c[c.index("--effort"):c.index("--effort") + 2]
    assert "--tools" in c and "--setting-sources" in c
    c = build_command(s, "claude", "haiku", "default", tmp_path, RunOptions(folder_mode=True, allow_shell=True, isolate=False))
    assert "--effort" not in c and "--dangerously-skip-permissions" in c and "--setting-sources" not in c

    x = build_command(s, "codex", "gpt-5.5", "xhigh", tmp_path, RunOptions(folder_mode=True))
    assert x[:3] == ["codex", "exec", "--json"] and x[-1] == "-"
    assert 'model_reasoning_effort="xhigh"' in x and "workspace-write" in x
    assert "read-only" in build_command(s, "codex", "m", "default", tmp_path, RunOptions())


def test_load_codex_models(tmp_path: Path):
    cache = tmp_path / "models_cache.json"
    cache.write_text('{"models": [{"slug": "a", "display_name": "A", "visibility": "list",'
                     ' "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}]},'
                     ' {"slug": "hidden", "visibility": "hide"}]}')
    assert load_codex_models(cache) == [{"id": "a", "label": "A", "efforts": ["low", "high"]}]
    assert load_codex_models(tmp_path / "missing.json")  # falls back
