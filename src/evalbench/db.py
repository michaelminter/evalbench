"""SQLite persistence. Writes are small and infrequent, so a single locked
connection used directly from the event loop is fine."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS evals (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    mode TEXT NOT NULL,              -- 'text' | 'folder'
    folder TEXT,
    allow_shell INTEGER NOT NULL DEFAULT 0,
    isolate INTEGER NOT NULL DEFAULT 1,
    rubric TEXT,
    max_parallel INTEGER NOT NULL,
    judge_spec TEXT,                 -- JSON {provider, model, effort} or NULL
    judge_status TEXT,               -- NULL | queued | running | done | error
    judge_result TEXT,               -- JSON
    judge_error TEXT,
    status TEXT NOT NULL             -- running | done
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    eval_id TEXT NOT NULL REFERENCES evals(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    effort TEXT NOT NULL,
    repeat INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,            -- queued | running | done | error | cancelled
    output TEXT NOT NULL DEFAULT '',
    transcript TEXT NOT NULL DEFAULT '',
    error TEXT,
    started_at REAL,
    finished_at REAL,
    duration_ms INTEGER,
    input_tokens INTEGER,
    cached_tokens INTEGER,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    cost_usd REAL,
    num_turns INTEGER,
    diff_stat TEXT,
    diff TEXT,
    workdir TEXT,
    command TEXT
);
CREATE INDEX IF NOT EXISTS runs_eval ON runs(eval_id, idx);
"""

RUN_FIELDS = {
    "status", "output", "transcript", "error", "started_at", "finished_at", "duration_ms",
    "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "cost_usd",
    "num_turns", "diff_stat", "diff", "workdir", "command",
}
EVAL_FIELDS = {"status", "judge_spec", "judge_status", "judge_result", "judge_error"}


class DB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def _exec(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    # ------------------------------------------------------------ evals

    def create_eval(self, ev: dict, runs: list[dict]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute(
                    """INSERT INTO evals (id, created_at, title, prompt, mode, folder, allow_shell, isolate,
                       rubric, max_parallel, judge_spec, judge_status, status)
                       VALUES (:id, :created_at, :title, :prompt, :mode, :folder, :allow_shell, :isolate,
                       :rubric, :max_parallel, :judge_spec, :judge_status, 'running')""",
                    ev,
                )
                for r in runs:
                    self._conn.execute(
                        """INSERT INTO runs (id, eval_id, idx, provider, model, effort, repeat, status)
                           VALUES (:id, :eval_id, :idx, :provider, :model, :effort, :repeat, 'queued')""",
                        r,
                    )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def update_eval(self, eval_id: str, **fields: Any) -> None:
        bad = set(fields) - EVAL_FIELDS
        if bad:
            raise ValueError(f"unknown eval fields {bad}")
        if "judge_result" in fields and not isinstance(fields["judge_result"], (str, type(None))):
            fields["judge_result"] = json.dumps(fields["judge_result"])
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        self._exec(f"UPDATE evals SET {sets} WHERE id = :_id", {**fields, "_id": eval_id})

    def get_eval(self, eval_id: str) -> dict | None:
        row = self._exec("SELECT * FROM evals WHERE id = ?", (eval_id,)).fetchone()
        return _eval_dict(row) if row else None

    def list_evals(self, limit: int = 200) -> list[dict]:
        rows = self._exec(
            """SELECT e.*, COUNT(r.id) AS n_runs,
                      SUM(r.status = 'done') AS n_done, SUM(r.status = 'error') AS n_error,
                      SUM(r.cost_usd) AS total_cost
               FROM evals e LEFT JOIN runs r ON r.eval_id = e.id
               GROUP BY e.id ORDER BY e.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [_eval_dict(r) for r in rows]

    def delete_eval(self, eval_id: str) -> None:
        self._exec("DELETE FROM runs WHERE eval_id = ?", (eval_id,))
        self._exec("DELETE FROM evals WHERE id = ?", (eval_id,))

    # ------------------------------------------------------------ runs

    def update_run(self, run_id: str, **fields: Any) -> None:
        bad = set(fields) - RUN_FIELDS
        if bad:
            raise ValueError(f"unknown run fields {bad}")
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        self._exec(f"UPDATE runs SET {sets} WHERE id = :_id", {**fields, "_id": run_id})

    def get_runs(self, eval_id: str) -> list[dict]:
        rows = self._exec("SELECT * FROM runs WHERE eval_id = ? ORDER BY idx", (eval_id,)).fetchall()
        return [dict(r) for r in rows]

    def mark_interrupted(self) -> None:
        """Runs that were in flight when the server stopped can't be resumed."""
        now = time.time()
        self._exec(
            "UPDATE runs SET status = 'error', error = 'interrupted: server restarted', finished_at = ? "
            "WHERE status IN ('queued', 'running')",
            (now,),
        )
        self._exec(
            "UPDATE evals SET judge_status = 'error', judge_error = 'interrupted: server restarted' "
            "WHERE judge_status IN ('queued', 'running')"
        )
        self._exec("UPDATE evals SET status = 'done' WHERE status = 'running'")


def _eval_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("judge_spec", "judge_result"):
        if d.get(key):
            d[key] = json.loads(d[key])
    return d
