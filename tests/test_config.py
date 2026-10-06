import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evalbench.app import create_app
from evalbench.config import DEFAULT_CLAUDE_MODELS, load_claude_models, load_settings
from evalbench.runners import RunOptions, build_command


def fake_claude(tmp_path: Path, entries: list) -> str:
    path = tmp_path / "claude"
    path.write_text(f"#!{sys.executable}\n" + """
import json, sys
request = json.loads(sys.stdin.read())
assert request['type'] == 'control_request'
assert request['request'] == {'subtype': 'initialize'}
assert '--tools' in sys.argv and sys.argv[sys.argv.index('--tools') + 1] == ''
print('non-JSON startup output')
print(json.dumps({'type': 'control_response', 'response': {
    'subtype': 'success', 'request_id': request['request_id'],
    'response': {'models': ENTRIES}}}))
""".replace("ENTRIES", repr(entries)))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_discovered_versions_in_grid_and_judge(tmp_path: Path):
    claude_bin = fake_claude(tmp_path, [
        {"value": "default", "resolvedModel": "claude-opus-5-5", "displayName": "Default"},
        {"value": "opus", "resolvedModel": "claude-opus-5-5", "displayName": "Opus 5.5",
         "supportedEffortLevels": ["low", "high", "max"]},
        {"value": "claude-opus-5-5", "displayName": "Duplicate"},
        {"value": "claude-opus-4-6", "displayName": "Opus 4.6", "supportedEffortLevels": ["high"]},
        {"value": "haiku", "resolvedModel": "claude-haiku-4-5-20251001", "displayName": "Haiku 4.5"},
    ])
    config = tmp_path / "evalbench.toml"
    config.write_text(f"claude_bin = {json.dumps(claude_bin)}\n")
    settings = load_settings(config)
    assert settings.claude_models == [
        {"id": "claude-opus-5-5", "label": "Opus 5.5", "efforts": ["low", "high", "max"]},
        {"id": "claude-opus-4-6", "label": "Opus 4.6", "efforts": ["high"]},
        {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5", "efforts": []},
    ]
    settings.data_dir = tmp_path / "data"
    settings.codex_models = []
    with TestClient(create_app(settings)) as client:
        response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert 'title="claude-opus-5-5">Opus 5.5</button>' in html
    assert 'value="claude|claude-opus-5-5|high"' in html
    assert 'value="claude|claude-opus-4-6|high"' in html
    assert 'value="claude|claude-opus-4-6|low"' not in html
    assert 'value="claude|claude-haiku-4-5-20251001|high"' not in html
    assert 'value="claude|claude-opus-5-5"' in html
    assert '>Opus 5.5</option>' in html
    command = build_command(settings, "claude", settings.claude_models[0]["id"], "high", tmp_path, RunOptions())
    assert command[command.index("--model") + 1] == "claude-opus-5-5"


@pytest.mark.parametrize("failure", [
    FileNotFoundError("missing claude"),
    subprocess.TimeoutExpired("claude", 0.1),
    subprocess.CalledProcessError(1, "claude"),
])
def test_discovery_failure_falls_back(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr("evalbench.config.subprocess.run", fail)
    assert load_claude_models() == DEFAULT_CLAUDE_MODELS


@pytest.mark.parametrize("entries", [[], [None, {}, {"value": "default"}], ["invalid"]])
def test_empty_or_invalid_catalog_falls_back(tmp_path: Path, entries):
    assert load_claude_models(fake_claude(tmp_path, entries)) == DEFAULT_CLAUDE_MODELS


def test_config_override_skips_discovery(tmp_path: Path, monkeypatch):
    def unexpected_discovery(*args, **kwargs):
        pytest.fail("Explicit model configuration should skip discovery")

    monkeypatch.setattr("evalbench.config.subprocess.run", unexpected_discovery)
    config = tmp_path / "evalbench.toml"
    config.write_text('[[claude.models]]\nid = "custom-model"\nlabel = "My model"\nefforts = ["high"]\n')
    assert load_settings(config).claude_models == [
        {"id": "custom-model", "label": "My model", "efforts": ["high"]},
    ]
