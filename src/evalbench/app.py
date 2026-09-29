"""FastAPI app: HTML pages (Jinja + HTMX), SSE progress stream, exports."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import DEFAULT_EFFORT, Settings, load_settings
from .db import DB
from .orchestrator import EvalRequest, Manager

HERE = Path(__file__).parent
PROVIDERS = {"claude": "Claude Code", "codex": "Codex"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = DB(settings.db_path)
    db.mark_interrupted()
    manager = Manager(settings, db)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await manager.shutdown()

    app = FastAPI(title="evalbench", lifespan=lifespan)
    app.state.settings, app.state.db, app.state.manager = settings, db, manager
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    _register_filters(templates)
    installed = {p: shutil.which(settings.claude_bin if p == "claude" else settings.codex_bin) is not None
                 for p in PROVIDERS}

    def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"providers": PROVIDERS, **ctx}, status_code=status_code)

    def get_eval_or_404(eval_id: str) -> dict:
        ev = db.get_eval(eval_id)
        if not ev:
            raise HTTPException(404, "evaluation not found")
        return ev

    def catalog() -> list[dict]:
        out = []
        for pid, label in PROVIDERS.items():
            out.append({
                "id": pid, "label": label, "installed": installed[pid],
                "efforts": [DEFAULT_EFFORT, *settings.all_efforts(pid)],
                "models": settings.models_for(pid),
            })
        return out

    # ------------------------------------------------------------ pages

    @app.get("/", response_class=HTMLResponse)
    async def new_eval(request: Request):
        prefill = None
        src = request.query_params.get("from")
        if src and (ev := db.get_eval(src)):
            runs = db.get_runs(src)
            prefill = {
                **ev,
                "combos": sorted({f"{r['provider']}|{r['model']}|{r['effort']}" for r in runs}),
                "repeats": max((r["repeat"] for r in runs), default=1),
            }
        return render(request, "index.html", catalog=catalog(), prefill=prefill, settings=settings)

    @app.post("/evals")
    async def create_eval(request: Request):
        form = await request.form()
        errors: list[str] = []

        prompt = str(form.get("prompt", "")).strip()
        if not prompt:
            errors.append("Prompt is required.")

        combos: list[tuple[str, str, str]] = []
        for raw in form.getlist("combo"):
            try:
                provider, model, effort = str(raw).split("|", 2)
            except ValueError:
                continue
            if provider in PROVIDERS and model.strip():
                combos.append((provider, model.strip(), effort or DEFAULT_EFFORT))
        combos = list(dict.fromkeys(combos))
        if not combos:
            errors.append("Select at least one model/effort combination.")
        for provider in {c[0] for c in combos}:
            if not installed[provider]:
                errors.append(f"The {PROVIDERS[provider]} CLI isn't on your PATH.")

        folder = None
        if form.get("mode") == "folder":
            folder_raw = str(form.get("folder", "")).strip()
            folder = Path(folder_raw).expanduser().resolve() if folder_raw else None
            if not folder or not folder.is_dir():
                errors.append(f"Folder not found: {folder_raw or '(empty)'}")

        repeats = _int(form.get("repeats"), 1, 1, 10)
        max_parallel = _int(form.get("max_parallel"), settings.max_parallel, 1, 32)
        if len(combos) * repeats > 100:
            errors.append(f"That's {len(combos) * repeats} runs; the limit is 100 per evaluation.")

        judge_spec = _judge_spec(form)
        if errors:
            return render(request, "partials/errors.html", status_code=422, errors=errors)

        eval_id = manager.start(EvalRequest(
            prompt=prompt, combos=combos, repeats=repeats, folder=folder,
            allow_shell=form.get("allow_shell") == "on", isolate=form.get("isolate") == "on",
            rubric=str(form.get("rubric", "")).strip(), max_parallel=max_parallel, judge=judge_spec,
        ))
        return Response(status_code=204, headers={"HX-Redirect": f"/evals/{eval_id}"})

    @app.get("/evals/{eval_id}", response_class=HTMLResponse)
    async def show_eval(request: Request, eval_id: str):
        ev = get_eval_or_404(eval_id)
        return render(request, "eval.html", ev=ev, runs=db.get_runs(eval_id), catalog=catalog(),
                      **_summary_ctx(ev, db.get_runs(eval_id)))

    @app.get("/evals/{eval_id}/summary", response_class=HTMLResponse)
    async def summary(request: Request, eval_id: str):
        ev = get_eval_or_404(eval_id)
        return render(request, "partials/summary.html", ev=ev, catalog=catalog(),
                      **_summary_ctx(ev, db.get_runs(eval_id)))

    @app.get("/evals/{eval_id}/runs/{run_id}", response_class=HTMLResponse)
    async def run_card(request: Request, eval_id: str, run_id: str):
        ev = get_eval_or_404(eval_id)
        runs = db.get_runs(eval_id)
        run = next((r for r in runs if r["id"] == run_id), None)
        if not run:
            raise HTTPException(404, "run not found")
        return render(request, "partials/run_card.html", ev=ev, run=run, **_summary_ctx(ev, runs))

    @app.get("/history", response_class=HTMLResponse)
    async def history(request: Request):
        evals = db.list_evals()
        for e in evals:
            ranking = (e.get("judge_result") or {}).get("ranking") or []
            if ranking:
                best = next((r for r in db.get_runs(e["id"]) if r["id"] == ranking[0]), None)
                e["best"] = f"{best['model']} · {best['effort']}" if best else ""
        return render(request, "history.html", evals=evals)

    # ------------------------------------------------------------ actions

    @app.post("/evals/{eval_id}/cancel")
    async def cancel_eval(eval_id: str):
        get_eval_or_404(eval_id)
        manager.cancel_eval(eval_id)
        return Response(status_code=204)

    @app.post("/evals/{eval_id}/runs/{run_id}/cancel")
    async def cancel_run(eval_id: str, run_id: str):
        manager.cancel_run(eval_id, run_id)
        return Response(status_code=204)

    @app.post("/evals/{eval_id}/judge", response_class=HTMLResponse)
    async def rejudge(request: Request, eval_id: str):
        ev = get_eval_or_404(eval_id)
        spec = _judge_spec(await request.form())
        error = None
        if not spec:
            error = "Pick a judge model."
        elif not manager.start_judge(eval_id, spec):
            error = "Wait for the runs (and any current judge) to finish first."
        if error:
            resp = render(request, "partials/errors.html", status_code=422, errors=[error])
            resp.headers.update({"HX-Retarget": "#judge-errors", "HX-Reswap": "innerHTML"})
            return resp
        ev = db.get_eval(eval_id)
        resp = render(request, "partials/summary.html", ev=ev, catalog=catalog(),
                      **_summary_ctx(ev, db.get_runs(eval_id)))
        resp.headers["HX-Trigger"] = "evalbench:reconnect"
        return resp

    @app.delete("/evals/{eval_id}")
    async def delete_eval(request: Request, eval_id: str):
        get_eval_or_404(eval_id)
        await manager.delete(eval_id)
        if request.headers.get("HX-Target", "").startswith("row-"):
            return HTMLResponse("")
        return Response(status_code=204, headers={"HX-Redirect": "/history"})

    @app.get("/api/folder-check", response_class=HTMLResponse)
    async def folder_check(folder: str = ""):
        if not folder.strip():
            return HTMLResponse("")
        p = Path(folder.strip()).expanduser()
        if not p.is_dir():
            return HTMLResponse('<span class="bad">Folder not found</span>')
        kind = "git repo" if (p / ".git").exists() else "folder (not a git repo)"
        return HTMLResponse(f'<span class="ok">✓ {kind}: {_esc(str(p.resolve()))}</span>')

    # ------------------------------------------------------------ streaming + export

    @app.get("/evals/{eval_id}/stream")
    async def stream(request: Request, eval_id: str):
        get_eval_or_404(eval_id)

        async def gen():
            q, snapshot, complete = manager.subscribe(eval_id)
            try:
                yield "retry: 3000\n\n"
                for e in snapshot:
                    yield _sse(e)
                if complete:
                    yield _sse({"type": "complete"})
                    return
                while True:
                    try:
                        first = await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            return
                        yield ": ping\n\n"
                        continue
                    batch = [first]
                    while not q.empty() and len(batch) < 500:
                        batch.append(q.get_nowait())
                    yield "".join(_sse(e) for e in batch)
                    if any(e["type"] == "complete" for e in batch):
                        return
            finally:
                manager.unsubscribe(eval_id, q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/evals/{eval_id}/export.json")
    async def export_json(eval_id: str):
        ev = get_eval_or_404(eval_id)
        data = {"eval": ev, "runs": db.get_runs(eval_id)}
        return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="eval-{eval_id}.json"'})

    @app.get("/evals/{eval_id}/export.csv")
    async def export_csv(eval_id: str):
        ev = get_eval_or_404(eval_id)
        scores = ((ev.get("judge_result") or {}).get("scores")) or {}
        cols = ["id", "provider", "model", "effort", "repeat", "status", "score", "duration_ms",
                "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "cost_usd",
                "num_turns", "error", "output"]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in db.get_runs(eval_id):
            w.writerow({**r, "score": (scores.get(r["id"]) or {}).get("score")})
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="eval-{eval_id}.csv"'})

    return app


# ---------------------------------------------------------------- helpers


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(str(value))))
    except (TypeError, ValueError):
        return default


def _judge_spec(form) -> dict | None:
    raw = str(form.get("judge", "none"))
    if raw in ("", "none") or "|" not in raw:
        return None
    provider, model = raw.split("|", 1)
    if provider not in PROVIDERS:
        return None
    return {"provider": provider, "model": model, "effort": str(form.get("judge_effort") or DEFAULT_EFFORT)}


def _summary_ctx(ev: dict, runs: list[dict]) -> dict:
    verdict = ev.get("judge_result") or {}
    scores = verdict.get("scores") or {}
    ranking = verdict.get("ranking") or []
    rank_of = {rid: i + 1 for i, rid in enumerate(ranking)}

    def sort_key(r: dict):
        s = scores.get(r["id"], {}).get("score")
        return (s is None, -(s or 0), rank_of.get(r["id"], len(ranking)), r["idx"])

    finished = [r for r in runs if r["status"] in ("done", "error", "cancelled")]
    totals = {
        "cost": sum(r["cost_usd"] or 0 for r in runs),
        "cost_known": any(r["cost_usd"] is not None for r in runs),
        "done": sum(r["status"] == "done" for r in runs),
        "error": sum(r["status"] == "error" for r in runs),
        "finished": len(finished),
        "total": len(runs),
    }
    return {"scores": scores, "rank_of": rank_of, "verdict": verdict,
            "ranked_runs": sorted(runs, key=sort_key), "totals": totals}


def _register_filters(templates: Jinja2Templates) -> None:
    def duration(ms):
        if ms is None:
            return "—"
        s = ms / 1000
        if s < 60:
            return f"{s:.1f}s"
        m, s = divmod(int(s), 60)
        return f"{m}m {s:02d}s" if m < 60 else f"{m // 60}h {m % 60:02d}m"

    def tokens(n):
        if n is None:
            return "—"
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

    def cost(v):
        if v is None:
            return "—"
        return f"${v:.4f}" if v < 1 else f"${v:.2f}"

    def when(ts):
        if not ts:
            return ""
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%H:%M") if time.time() - ts < 86400 else dt.strftime("%b %d, %H:%M")

    def diffshort(stat: str | None) -> str:
        """'2 files changed, 3 insertions(+), 1 deletion(-)' -> '2 files +3 −1'"""
        if not stat:
            return ""
        last = stat.splitlines()[-1]
        files = re.search(r"(\d+) files? changed", last)
        ins = re.search(r"(\d+) insertion", last)
        dels = re.search(r"(\d+) deletion", last)
        parts = [f"{files.group(1)} file{'s' if files.group(1) != '1' else ''}"] if files else []
        parts += [f"+{ins.group(1)}"] if ins else []
        parts += [f"−{dels.group(1)}"] if dels else []
        return " ".join(parts)

    templates.env.filters.update(duration=duration, tokens=tokens, cost=cost, when=when, diffshort=diffshort)
