"""Settings and model catalogs.

Everything has a sensible default; an optional ``evalbench.toml`` in the
directory you start the server from overrides any of it (see README).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]

DEFAULT_CLAUDE_MODELS = [
    {"id": "fable", "label": "Fable", "efforts": CLAUDE_EFFORTS},
    {"id": "opus", "label": "Opus", "efforts": CLAUDE_EFFORTS},
    {"id": "sonnet", "label": "Sonnet", "efforts": CLAUDE_EFFORTS},
    {"id": "haiku", "label": "Haiku", "efforts": []},
]

# Used only when ~/.codex/models_cache.json is missing or unreadable.
FALLBACK_CODEX_MODELS = [
    {"id": "gpt-5.5", "label": "gpt-5.5", "efforts": ["low", "medium", "high", "xhigh"]},
]

# "default" means: don't pass an effort flag, let the CLI pick.
DEFAULT_EFFORT = "default"


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    max_parallel: int = 4
    run_timeout_s: int = 1800
    claude_bin: str = "claude"
    codex_bin: str = "codex"
    data_dir: Path = field(default_factory=lambda: Path.cwd() / "data")
    workspace_root: Path = field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "evalbench"
    )
    # Directory names skipped when copying a folder into a run workspace.
    copy_ignore: list[str] = field(
        default_factory=lambda: [
            "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
            ".pytest_cache", ".ruff_cache", ".next", ".turbo", ".DS_Store",
        ]
    )
    claude_models: list[dict] = field(default_factory=lambda: list(DEFAULT_CLAUDE_MODELS))
    codex_models: list[dict] = field(default_factory=list)
    # Optional Codex price table, USD per 1M tokens:
    #   {"gpt-5.5": {"input": 1.25, "cached_input": 0.125, "output": 10.0}}
    # Codex doesn't report cost, so without this its cost column shows "—".
    codex_prices: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "evalbench.db"

    def models_for(self, provider: str) -> list[dict]:
        return self.claude_models if provider == "claude" else self.codex_models

    def efforts_for(self, provider: str, model: str) -> list[str]:
        for m in self.models_for(provider):
            if m["id"] == model:
                return [DEFAULT_EFFORT, *m["efforts"]]
        # Custom model typed into the UI: allow every known effort.
        return [DEFAULT_EFFORT, *self.all_efforts(provider)]

    def all_efforts(self, provider: str) -> list[str]:
        seen: list[str] = []
        for m in self.models_for(provider):
            for e in m["efforts"]:
                if e not in seen:
                    seen.append(e)
        return seen


def load_claude_models(claude_bin: str = "claude", timeout: float = 10) -> list[dict]:
    """Query Claude Code's catalog without sending a prompt or generating tokens."""
    request_id = "evalbench-model-catalog"
    request = {
        "type": "control_request", "request_id": request_id,
        "request": {"subtype": "initialize"},
    }
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    try:
        with tempfile.TemporaryDirectory(prefix="evalbench-catalog-") as cwd:
            result = subprocess.run(
                [claude_bin, "-p", "--input-format", "stream-json",
                 "--output-format", "stream-json", "--verbose", "--tools", "",
                 "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence"],
                input=json.dumps(request) + "\n", text=True, capture_output=True,
                cwd=cwd, env=env, timeout=timeout, check=True,
            )
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "control_response":
                continue
            response = event.get("response")
            if not isinstance(response, dict) or response.get("request_id") != request_id:
                continue
            data = response.get("response")
            if response.get("subtype") != "success" or not isinstance(data, dict):
                continue
            entries = data.get("models")
            if not isinstance(entries, list):
                continue
            models, seen = [], set()
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("value") in ("default", "opusplan"):
                    continue
                model_id = entry.get("resolvedModel") or entry.get("value")
                if not isinstance(model_id, str) or not model_id or model_id in seen:
                    continue
                seen.add(model_id)
                label = entry.get("displayName")
                efforts = entry.get("supportedEffortLevels")
                models.append({
                    "id": model_id,
                    "label": label if isinstance(label, str) and label else model_id,
                    "efforts": [e for e in efforts if e in CLAUDE_EFFORTS]
                    if isinstance(efforts, list) else [],
                })
            if models:
                return models
    except (OSError, subprocess.SubprocessError, UnicodeError):
        pass
    log.warning("Could not read Claude Code's model catalog; using model aliases.")
    return list(DEFAULT_CLAUDE_MODELS)


def load_codex_models(cache_path: Path | None = None) -> list[dict]:
    """Read the model list the Codex CLI itself caches, so the UI matches it."""
    path = cache_path or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "models_cache.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return list(FALLBACK_CODEX_MODELS)
    models = []
    for m in data.get("models", []):
        if m.get("visibility") != "list" or not m.get("slug"):
            continue
        efforts = []
        for lvl in m.get("supported_reasoning_levels") or []:
            e = lvl.get("effort") if isinstance(lvl, dict) else lvl
            if isinstance(e, str):
                efforts.append(e)
        models.append({"id": m["slug"], "label": m.get("display_name") or m["slug"], "efforts": efforts})
    return models or list(FALLBACK_CODEX_MODELS)


def load_settings(config_path: Path | None = None) -> Settings:
    s = Settings()
    path = config_path or Path(os.environ.get("EVALBENCH_CONFIG", Path.cwd() / "evalbench.toml"))
    raw: dict = {}
    if path.exists():
        raw = tomllib.loads(path.read_text())

    for key in ("host", "port", "max_parallel", "run_timeout_s", "claude_bin", "codex_bin", "copy_ignore"):
        if key in raw:
            setattr(s, key, raw[key])
    for key in ("data_dir", "workspace_root"):
        if key in raw:
            setattr(s, key, Path(raw[key]).expanduser())
    if "EVALBENCH_DATA" in os.environ:
        s.data_dir = Path(os.environ["EVALBENCH_DATA"]).expanduser()

    claude = raw.get("claude", {})
    if "models" in claude:
        s.claude_models = [_norm_model(m) for m in claude["models"]]
    else:
        s.claude_models = load_claude_models(s.claude_bin)
    codex = raw.get("codex", {})
    s.codex_models = [_norm_model(m) for m in codex["models"]] if "models" in codex else load_codex_models()
    s.codex_prices = codex.get("prices", {})
    return s


def _norm_model(m: dict) -> dict:
    return {"id": m["id"], "label": m.get("label", m["id"]), "efforts": list(m.get("efforts", []))}
