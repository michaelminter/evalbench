"""Per-run working directories.

Text mode: an empty directory.
Folder mode: a copy of the source folder plus a snapshot repo *outside* the
copy (``<run>.snap``), so the diff shows exactly what the agent changed,
without touching the copy's own ``.git``.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

MAX_DIFF_CHARS = 400_000

_GIT_ID = [
    "-c", "user.name=evalbench", "-c", "user.email=evalbench@localhost",
    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
    "-c", "core.autocrlf=false",
]


@dataclass
class Workspace:
    path: Path
    snap: Path | None  # git dir for the baseline snapshot, folder mode only

    def _git(self, *args: str) -> list[str]:
        assert self.snap
        return ["git", *_GIT_ID, f"--git-dir={self.snap}", f"--work-tree={self.path}", *args]


async def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


async def create(root: Path, eval_id: str, run_id: str, source: Path | None, ignore: list[str]) -> Workspace:
    base = root / eval_id
    path = base / run_id
    if path.exists():
        shutil.rmtree(path)
    base.mkdir(parents=True, exist_ok=True)

    if source is None:
        path.mkdir()
        return Workspace(path=path, snap=None)

    await asyncio.to_thread(
        shutil.copytree, source, path, symlinks=True, ignore=shutil.ignore_patterns(*ignore),
    )
    ws = Workspace(path=path, snap=base / f"{run_id}.snap")
    for args in (("init", "-q"), ("add", "-A"), ("commit", "-q", "--allow-empty", "-m", "baseline")):
        code, out = await _run(ws._git(*args), path)
        if code != 0:
            raise RuntimeError(f"snapshot failed (git {args[0]}): {out.strip()[:500]}")
    return ws


async def diff(ws: Workspace) -> tuple[str, str]:
    """Return (stat, patch) of changes the agent made. Empty strings in text mode."""
    if ws.snap is None:
        return "", ""
    await _run(ws._git("add", "-A"), ws.path)
    _, stat = await _run(ws._git("diff", "--cached", "--stat", "--no-color", "HEAD"), ws.path)
    _, patch = await _run(ws._git("diff", "--cached", "--no-color", "HEAD"), ws.path)
    if len(patch) > MAX_DIFF_CHARS:
        patch = patch[:MAX_DIFF_CHARS] + f"\n… diff truncated ({len(patch):,} chars total)\n"
    return stat.strip(), patch


def remove_eval(root: Path, eval_id: str) -> None:
    shutil.rmtree(root / eval_id, ignore_errors=True)
