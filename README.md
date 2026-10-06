# evalbench

Run one prompt against many **Claude Code** and **Codex CLI** models and effort levels at the same
time, watch them stream side by side in your browser, and have an LLM judge score the results.

It shells out to the `claude` and `codex` CLIs you already have installed and signed in to, so
it uses your existing subscriptions or API keys. There are no API keys to configure here.

## Quick start

```bash
uv sync
uv run evalbench            # opens http://127.0.0.1:8765
```

Options: `--port 9000`, `--no-browser`, `--host` (keep it on localhost; see Security).

## What it does

1. **Pick a prompt and a grid of model × effort combinations.** Claude models are queried from
   Claude Code at startup, showing versioned names and supported effort levels. Runs use the
   resolved model IDs. This query sends no prompt and generates no tokens; if it fails or times
   out, the list falls back to `fable`, `opus`, `sonnet`, and `haiku` aliases. Codex models are
   read from the Codex CLI's own cache (`~/.codex/models_cache.json`), so the list matches what `codex` offers.
   You can add any other model id in the UI. `default` means no effort flag is passed.
2. **Choose a mode.**
   - **Text only**: Claude runs with every tool disabled (`--tools ""`) and Codex in a
     `read-only` sandbox, each in an empty temporary directory.
   - **Work in a folder**: every run gets its **own copy** of the folder; your original is never
     touched. Claude can read and edit files (`acceptEdits`), and can also run shell commands if
     you tick the box. Codex runs in its `workspace-write` sandbox. When a run finishes you get a
     diff of exactly what it changed.
3. **Runs execute in parallel** (default 4 at a time), streaming text and tool activity
   into per-run cards. You can cancel runs individually or all at once.
4. **The judge** (optional, any Claude or Codex model and effort) sees the task, your rubric
   (or a default one), and every successful run's final answer (plus its diff in folder mode).
   Runs are **anonymized and shuffled** before judging. It returns 1–10 scores, a rationale for
   each, a ranking and a summary. You can re-judge later with a different model.
5. **Everything is saved** to SQLite (`./data/evalbench.db`): browse it under History,
   "Run again" to prefill the form, and export to CSV or JSON.

## Metrics

| | Claude Code | Codex |
|---|---|---|
| Wall time | ✓ | ✓ |
| Input / cached / output / reasoning tokens | ✓ | ✓ |
| Cost | ✓ (reported by the CLI) | only if you add prices to `evalbench.toml` |

## Configuration (optional)

Create `evalbench.toml` in the directory you run `evalbench` from:

```toml
port = 8765
max_parallel = 4          # default for the form
run_timeout_s = 1800      # per run
workspace_root = "/tmp/evalbench"
copy_ignore = ["node_modules", ".venv", "dist"]   # skipped when copying a folder

[[claude.models]]         # replaces the built-in Claude list
id = "opus"
efforts = ["low", "medium", "high", "xhigh", "max"]

[[claude.models]]
id = "claude-haiku-4-5-20251001"
label = "Haiku 4.5"
efforts = []

[codex.prices."gpt-5.5"]  # USD per 1M tokens, used to estimate Codex cost
input = 1.25
cached_input = 0.125
output = 10.0
```

`[[codex.models]]` works the same way if you want to override the Codex list.

## How runs are invoked

The prompt is always sent on **stdin**. Each card's Details tab shows the exact command.

- Claude: `claude -p --model M [--effort E] --output-format stream-json --verbose
  --include-partial-messages --no-session-persistence`, plus `--setting-sources "" --strict-mcp-config`
  when *Isolate* is on, which skips your personal settings, hooks and MCP servers so runs are
  comparable. Your login is still used.
- Codex: `codex exec --json --skip-git-repo-check --ephemeral -m M -C <workspace> -s <sandbox>
  [-c model_reasoning_effort="E"] -`, plus `--ignore-user-config` when *Isolate* is on.

Workspaces are kept under `$TMPDIR/evalbench/<eval-id>/` so you can inspect or run the results.
Deleting an evaluation deletes them. In folder mode the baseline snapshot lives *next to* the copy
(`<run>.snap`), so the copy's own `.git` is untouched.

## Security

The server runs agents **with your credentials** and, in folder mode with shell enabled,
unsandboxed Claude commands. It binds to `127.0.0.1` by default; don't expose it on a network.

## Development

```bash
uv run pytest
```

Layout: `config.py` (settings and model catalogs), `runners.py` (CLI commands and stream
parsers), `workspace.py` (copies and diffs), `orchestrator.py` (concurrency, live events,
judge), `judge.py` (prompt and verdict parsing), `db.py` (SQLite), `app.py` (FastAPI routes and SSE),
`templates/` (Jinja and HTMX), `static/` (JS and CSS).
