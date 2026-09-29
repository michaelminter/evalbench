"""Runs evaluations: fans runs out under a concurrency limit, streams progress
to browser subscribers, persists results, and triggers the judge."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import judge, workspace
from .config import Settings
from .db import DB
from .runners import RunHandle, RunOptions, Update, build_command, make_parser, run_cli

log = logging.getLogger("evalbench")

ACTIVITY_PREFIX = "▸ "
FINAL_STATUSES = {"done", "error", "cancelled"}
STAT_FIELDS = (
    "status", "started_at", "finished_at", "duration_ms", "input_tokens", "cached_tokens",
    "output_tokens", "reasoning_tokens", "cost_usd", "num_turns", "error", "diff_stat",
)


@dataclass
class EvalRequest:
    prompt: str
    combos: list[tuple[str, str, str]]  # (provider, model, effort)
    repeats: int = 1
    folder: Path | None = None
    allow_shell: bool = False
    isolate: bool = True
    rubric: str = ""
    max_parallel: int = 4
    judge: dict | None = None  # {"provider", "model", "effort"}


@dataclass
class LiveEval:
    eval_id: str
    transcripts: dict[str, list[str]] = field(default_factory=dict)
    handles: dict[str, RunHandle] = field(default_factory=dict)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    runs_finished: bool = False
    judge_running: bool = False
    task: asyncio.Task | None = None

    @property
    def complete(self) -> bool:
        return self.runs_finished and not self.judge_running

    def publish(self, event: dict) -> None:
        for q in list(self.subscribers):
            q.put_nowait(event)


class Manager:
    def __init__(self, settings: Settings, db: DB) -> None:
        self.settings = settings
        self.db = db
        self.live: dict[str, LiveEval] = {}
        self._judge_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------ public API

    def start(self, req: EvalRequest) -> str:
        eval_id = uuid.uuid4().hex[:10]
        first_line = req.prompt.strip().splitlines()[0] if req.prompt.strip() else "untitled"
        ev = {
            "id": eval_id,
            "created_at": time.time(),
            "title": first_line[:90],
            "prompt": req.prompt,
            "mode": "folder" if req.folder else "text",
            "folder": str(req.folder) if req.folder else None,
            "allow_shell": int(req.allow_shell),
            "isolate": int(req.isolate),
            "rubric": req.rubric or None,
            "max_parallel": req.max_parallel,
            "judge_spec": json.dumps(req.judge) if req.judge else None,
            "judge_status": "queued" if req.judge else None,
        }
        runs = []
        idx = 0
        for provider, model, effort in req.combos:
            for rep in range(1, req.repeats + 1):
                runs.append({
                    "id": f"{eval_id}-{idx:02d}", "eval_id": eval_id, "idx": idx,
                    "provider": provider, "model": model, "effort": effort, "repeat": rep,
                })
                idx += 1
        self.db.create_eval(ev, runs)

        live = LiveEval(eval_id=eval_id)
        for r in runs:
            live.transcripts[r["id"]] = []
            live.handles[r["id"]] = RunHandle()
        self.live[eval_id] = live
        live.task = asyncio.create_task(self._run_eval(eval_id))
        return eval_id

    def cancel_run(self, eval_id: str, run_id: str) -> None:
        live = self.live.get(eval_id)
        if live and run_id in live.handles:
            live.handles[run_id].kill()

    def cancel_eval(self, eval_id: str) -> None:
        live = self.live.get(eval_id)
        if not live:
            return
        for h in live.handles.values():
            h.kill()

    def start_judge(self, eval_id: str, spec: dict) -> bool:
        ev = self.db.get_eval(eval_id)
        if not ev:
            return False
        live = self.live.setdefault(eval_id, LiveEval(eval_id=eval_id, runs_finished=True))
        if not live.runs_finished or live.judge_running:
            return False
        self.db.update_eval(eval_id, judge_spec=json.dumps(spec), judge_status="queued",
                            judge_error=None, judge_result=None)
        live.judge_running = True
        live.publish({"type": "judge", "status": "queued"})
        t = asyncio.create_task(self._judge(eval_id, spec))
        self._judge_tasks.add(t)
        t.add_done_callback(self._judge_tasks.discard)
        return True

    async def delete(self, eval_id: str) -> None:
        live = self.live.pop(eval_id, None)
        if live:
            self.cancel_eval(eval_id)
            if live.task:
                try:
                    await asyncio.wait_for(asyncio.shield(live.task), timeout=10)
                except (asyncio.TimeoutError, Exception):
                    live.task.cancel()
        self.db.delete_eval(eval_id)
        await asyncio.to_thread(workspace.remove_eval, self.settings.workspace_root, eval_id)

    def subscribe(self, eval_id: str) -> tuple[asyncio.Queue, list[dict], bool]:
        """Return (queue, snapshot events, already_complete)."""
        live = self.live.get(eval_id)
        q: asyncio.Queue = asyncio.Queue()
        snapshot: list[dict] = []
        for run in self.db.get_runs(eval_id):
            transcript = "".join(live.transcripts.get(run["id"], [])) if live and run["status"] == "running" else run["transcript"]
            snapshot.append({"type": "run", "run": _stats(run)})
            snapshot.append({"type": "snapshot", "run_id": run["id"], "transcript": transcript})
        if live and not live.complete:
            live.subscribers.add(q)
            return q, snapshot, False
        return q, snapshot, True

    def unsubscribe(self, eval_id: str, q: asyncio.Queue) -> None:
        live = self.live.get(eval_id)
        if live:
            live.subscribers.discard(q)

    async def shutdown(self) -> None:
        for live in self.live.values():
            for h in live.handles.values():
                h.kill()
        tasks = [lv.task for lv in self.live.values() if lv.task] + list(self._judge_tasks)
        if tasks:
            await asyncio.wait(tasks, timeout=10)

    # ------------------------------------------------------------ internals

    async def _run_eval(self, eval_id: str) -> None:
        live = self.live[eval_id]
        ev = self.db.get_eval(eval_id)
        runs = self.db.get_runs(eval_id)
        sem = asyncio.Semaphore(max(1, ev["max_parallel"]))
        try:
            await asyncio.gather(*(self._run_one(live, ev, run, sem) for run in runs))
        finally:
            live.runs_finished = True
            self.db.update_eval(eval_id, status="done")
            live.publish({"type": "eval", "status": "done"})

        spec = ev.get("judge_spec")
        finished = [r for r in self.db.get_runs(eval_id) if r["status"] == "done"]
        if spec and finished:
            live.judge_running = True
            await self._judge(eval_id, spec)  # publishes "complete" when it finishes
            return
        if spec:
            self.db.update_eval(eval_id, judge_status="error", judge_error="no successful runs to judge")
            live.publish({"type": "judge", "status": "finished"})
        live.publish({"type": "complete"})

    async def _run_one(self, live: LiveEval, ev: dict, run: dict, sem: asyncio.Semaphore) -> None:
        run_id = run["id"]
        handle = live.handles[run_id]
        chunks = live.transcripts[run_id]

        def update(**fields) -> None:
            self.db.update_run(run_id, **fields)
            run.update(fields)
            live.publish({"type": "run", "run": _stats(run)})

        def on_update(u: Update) -> None:
            if u.kind == "text":
                chunks.append(u.data)
                live.publish({"type": "text", "run_id": run_id, "text": u.data})
            elif u.kind == "activity":
                line = ("\n" if chunks and not chunks[-1].endswith("\n") else "") + ACTIVITY_PREFIX + u.data + "\n"
                chunks.append(line)
                live.publish({"type": "text", "run_id": run_id, "text": line})

        async with sem:
            if handle.cancelled:
                update(status="cancelled", finished_at=time.time())
                return
            started = time.time()
            update(status="running", started_at=started)
            fields: dict = {}
            try:
                source = Path(ev["folder"]) if ev["mode"] == "folder" else None
                ws = await workspace.create(self.settings.workspace_root, ev["id"], run_id, source,
                                            self.settings.copy_ignore)
                opts = RunOptions(folder_mode=source is not None, allow_shell=bool(ev["allow_shell"]),
                                  isolate=bool(ev["isolate"]))
                cmd = build_command(self.settings, run["provider"], run["model"], run["effort"], ws.path, opts)
                self.db.update_run(run_id, workdir=str(ws.path), command=shlex.join(cmd))
                parser = make_parser(self.settings, run["provider"], run["model"])
                res = await run_cli(cmd, ev["prompt"], ws.path, parser, on_update,
                                    self.settings.run_timeout_s, handle)
                fields.update({k: v for k, v in res.final.items() if k != "error"})
                if source is not None:
                    fields["diff_stat"], fields["diff"] = await workspace.diff(ws)

                if res.cancelled:
                    status, error = "cancelled", None
                elif res.timed_out:
                    status, error = "error", f"timed out after {self.settings.run_timeout_s}s"
                elif res.final.get("error"):
                    status, error = "error", res.final["error"]
                elif not res.final:
                    status = "error"
                    error = f"CLI exited with code {res.returncode} without a result"
                    if res.stderr_tail.strip():
                        error += f":\n{res.stderr_tail.strip()[-1500:]}"
                else:
                    status, error = "done", None
            except FileNotFoundError as e:
                status, error = "error", f"command not found: {e.filename}"
            except Exception as e:  # noqa: BLE001 - surface anything to the UI
                log.exception("run %s failed", run_id)
                status, error = "error", f"{type(e).__name__}: {e}"

            finished = time.time()
            update(
                **fields, status=status, error=error, finished_at=finished,
                duration_ms=int((finished - started) * 1000), transcript="".join(chunks),
            )

    async def _judge(self, eval_id: str, spec: dict) -> None:
        live = self.live[eval_id]
        try:
            self.db.update_eval(eval_id, judge_status="running")
            live.publish({"type": "judge", "status": "running"})
            ev = self.db.get_eval(eval_id)
            runs = [r for r in self.db.get_runs(eval_id) if r["status"] == "done"]
            if not runs:
                raise RuntimeError("no successful runs to judge")
            prompt, mapping = judge.build_prompt(ev["prompt"], ev["rubric"], runs, ev["mode"] == "folder")

            ws = await workspace.create(self.settings.workspace_root, eval_id, f"judge-{uuid.uuid4().hex[:6]}", None, [])
            cmd = build_command(self.settings, spec["provider"], spec["model"], spec.get("effort", "default"),
                                ws.path, RunOptions(folder_mode=False, isolate=True))
            parser = make_parser(self.settings, spec["provider"], spec["model"])
            res = await run_cli(cmd, prompt, ws.path, parser, lambda u: None, self.settings.run_timeout_s, RunHandle())
            raw = res.final.get("output") or ""
            if res.final.get("error") or not raw:
                raise RuntimeError(res.final.get("error") or f"judge produced no output. {res.stderr_tail[-800:]}")
            try:
                verdict = judge.parse_verdict(raw, mapping)
            except ValueError as e:
                self.db.update_eval(eval_id, judge_status="error", judge_error=str(e),
                                    judge_result={"raw": raw, "judge": spec})
            else:
                verdict.update({"judge": spec, "raw": raw, "cost_usd": res.final.get("cost_usd")})
                self.db.update_eval(eval_id, judge_status="done", judge_result=verdict, judge_error=None)
        except Exception as e:  # noqa: BLE001
            log.exception("judge for %s failed", eval_id)
            self.db.update_eval(eval_id, judge_status="error", judge_error=f"{type(e).__name__}: {e}")
        finally:
            live.judge_running = False
            live.publish({"type": "judge", "status": "finished"})
            if live.complete:
                live.publish({"type": "complete"})


def _stats(run: dict) -> dict:
    return {"id": run["id"], **{k: run.get(k) for k in STAT_FIELDS}}
