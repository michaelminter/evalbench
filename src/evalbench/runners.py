"""Build CLI commands for Claude Code / Codex and parse their JSON event streams.

Both CLIs run headless and emit one JSON object per line. The parsers turn
those into a small set of normalized updates the orchestrator understands:

    Update("text", text)      streamed assistant text
    Update("activity", line)  a tool call / command / file edit, shown in the transcript
    Update("final", fields)   result fields: output, tokens, cost, error...
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import DEFAULT_EFFORT, Settings

MAX_LINE = 64 * 1024 * 1024  # stream-json lines can be large (big tool results)


@dataclass
class Update:
    kind: str  # "text" | "activity" | "final"
    data: Any


@dataclass
class RunOptions:
    folder_mode: bool = False  # True: agent may read/edit its workspace copy
    allow_shell: bool = False  # Claude only: allow Bash etc. without prompts (folder mode)
    isolate: bool = True  # ignore your personal CLI settings/hooks/MCP servers


def build_command(settings: Settings, provider: str, model: str, effort: str,
                  workdir: Path, opts: RunOptions) -> list[str]:
    """The prompt is always sent on stdin, so it never needs shell escaping."""
    if provider == "claude":
        cmd = [
            settings.claude_bin, "-p",
            "--model", model,
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--no-session-persistence",
        ]
        if effort and effort != DEFAULT_EFFORT:
            cmd += ["--effort", effort]
        if opts.isolate:
            cmd += ["--setting-sources", "", "--strict-mcp-config"]
        if not opts.folder_mode:
            cmd += ["--tools", ""]
        elif opts.allow_shell:
            cmd += ["--dangerously-skip-permissions"]
        else:
            cmd += ["--permission-mode", "acceptEdits"]
        return cmd

    if provider == "codex":
        cmd = [
            settings.codex_bin, "exec", "--json",
            "--skip-git-repo-check", "--ephemeral",
            "-m", model,
            "-C", str(workdir),
            "-s", "workspace-write" if opts.folder_mode else "read-only",
        ]
        if effort and effort != DEFAULT_EFFORT:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        if opts.isolate:
            cmd += ["--ignore-user-config"]
        cmd.append("-")  # read prompt from stdin
        return cmd

    raise ValueError(f"unknown provider {provider!r}")


# ---------------------------------------------------------------- parsers


def _short(value: Any, limit: int = 160) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


class ClaudeParser:
    def __init__(self) -> None:
        self._streamed_text = False
        self._text_since_message = False

    def feed(self, ev: dict) -> list[Update]:
        t = ev.get("type")
        if t == "stream_event":
            inner = ev.get("event", {})
            it = inner.get("type")
            if it == "message_start" and self._streamed_text:
                self._text_since_message = False
            elif it == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta" and delta.get("text"):
                    out = []
                    if self._streamed_text and not self._text_since_message:
                        out.append(Update("text", "\n\n"))
                    self._streamed_text = self._text_since_message = True
                    out.append(Update("text", delta["text"]))
                    return out
            return []

        if t == "assistant":
            out = []
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    out.append(Update("activity", f"{block.get('name')}: {_short(_tool_arg(block.get('input')))}"))
                elif block.get("type") == "text" and not self._streamed_text and block.get("text"):
                    # Fallback if partial messages weren't streamed.
                    out.append(Update("text", block["text"]))
            return out

        if t == "result":
            usage = ev.get("usage") or {}
            cached = usage.get("cache_read_input_tokens") or 0
            fields = {
                "output": ev.get("result") or "",
                "input_tokens": (usage.get("input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0) + cached,
                "cached_tokens": cached,
                "output_tokens": usage.get("output_tokens") or 0,
                "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0,
                "cost_usd": ev.get("total_cost_usd"),
                "num_turns": ev.get("num_turns"),
            }
            if ev.get("is_error") or ev.get("subtype") not in (None, "success"):
                detail = ev.get("result") or ev.get("subtype") or "error"
                if ev.get("api_error_status"):
                    detail = f"{detail} (API status {ev['api_error_status']})"
                fields["error"] = str(detail)
            return [Update("final", fields)]
        return []


def _tool_arg(inp: Any) -> Any:
    """Pick the most informative field of a tool input for a one-line summary."""
    if isinstance(inp, dict):
        for key in ("command", "file_path", "path", "pattern", "url", "query", "description"):
            if key in inp:
                return inp[key]
    return inp


class CodexParser:
    def __init__(self, settings: Settings | None = None, model: str = "") -> None:
        self._messages: list[str] = []
        self._error: str | None = None
        self._prices = (settings.codex_prices if settings else {}).get(model)

    def feed(self, ev: dict) -> list[Update]:
        t = ev.get("type")
        if t == "item.completed":
            item = ev.get("item", {})
            it = item.get("type")
            if it == "agent_message" and item.get("text"):
                sep = "\n\n" if self._messages else ""
                self._messages.append(item["text"])
                return [Update("text", sep + item["text"])]
            if it == "command_execution":
                code = item.get("exit_code")
                suffix = f"  (exit {code})" if code not in (None, 0) else ""
                return [Update("activity", f"$ {_short(item.get('command', ''))}{suffix}")]
            if it == "file_change":
                paths = ", ".join(f"{c.get('kind', 'edit')} {c.get('path')}" for c in item.get("changes") or [])
                return [Update("activity", f"files: {_short(paths)}")]
            if it == "mcp_tool_call":
                return [Update("activity", f"mcp {item.get('server')}.{item.get('tool')}")]
            if it == "web_search":
                return [Update("activity", f"web search: {_short(item.get('query', ''))}")]
            if it == "error":
                return [Update("activity", f"error: {_short(item.get('message', ''))}")]
            return []

        if t == "turn.completed":
            usage = ev.get("usage") or {}
            fields = {
                "output": self._messages[-1] if self._messages else "",
                "input_tokens": usage.get("input_tokens") or 0,
                "cached_tokens": usage.get("cached_input_tokens") or 0,
                "output_tokens": usage.get("output_tokens") or 0,
                "reasoning_tokens": usage.get("reasoning_output_tokens") or 0,
                "cost_usd": self._cost(usage),
            }
            return [Update("final", fields)]

        if t == "turn.failed":
            msg = (ev.get("error") or {}).get("message") or "turn failed"
            return [Update("final", {"output": self._messages[-1] if self._messages else "", "error": msg})]

        if t == "error":
            self._error = ev.get("message") or "error"
            return [Update("activity", f"error: {_short(self._error)}")]
        return []

    def _cost(self, usage: dict) -> float | None:
        p = self._prices
        if not p:
            return None
        cached = usage.get("cached_input_tokens") or 0
        fresh = (usage.get("input_tokens") or 0) - cached
        return (
            fresh * p.get("input", 0)
            + cached * p.get("cached_input", p.get("input", 0))
            + (usage.get("output_tokens") or 0) * p.get("output", 0)
        ) / 1_000_000

    @property
    def last_error(self) -> str | None:
        return self._error


def make_parser(settings: Settings, provider: str, model: str):
    return ClaudeParser() if provider == "claude" else CodexParser(settings, model)


# ---------------------------------------------------------------- execution


@dataclass
class ProcessResult:
    returncode: int | None
    final: dict = field(default_factory=dict)
    stderr_tail: str = ""
    timed_out: bool = False
    cancelled: bool = False


class RunHandle:
    """Lets the orchestrator kill a running CLI process (and its children)."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.cancelled = False

    def kill(self) -> None:
        self.cancelled = True
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


async def run_cli(cmd: list[str], prompt: str, cwd: Path, parser, on_update: Callable[[Update], None],
                  timeout_s: int, handle: RunHandle) -> ProcessResult:
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)  # don't make a nested claude think it's inside Claude Code
    env.setdefault("NO_COLOR", "1")

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd), env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=MAX_LINE, start_new_session=True,
    )
    handle.proc = proc
    result = ProcessResult(returncode=None)
    stderr_chunks: list[bytes] = []

    async def feed_stdin() -> None:
        assert proc.stdin
        try:
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()

    async def read_stdout() -> None:
        assert proc.stdout
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                on_update(Update("activity", _short(line)))
                continue
            for upd in parser.feed(ev):
                if upd.kind == "final":
                    result.final.update(upd.data)
                on_update(upd)

    async def read_stderr() -> None:
        assert proc.stderr
        async for raw in proc.stderr:
            stderr_chunks.append(raw)
            if sum(len(c) for c in stderr_chunks) > 64_000:
                stderr_chunks.pop(0)

    try:
        await asyncio.wait_for(
            asyncio.gather(feed_stdin(), read_stdout(), read_stderr(), proc.wait()),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        result.timed_out = True
        handle.kill()
    except asyncio.CancelledError:
        handle.kill()
        raise
    finally:
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()

    result.returncode = proc.returncode
    result.cancelled = handle.cancelled and not result.timed_out
    result.stderr_tail = b"".join(stderr_chunks).decode(errors="replace")[-4000:]
    if isinstance(parser, CodexParser) and parser.last_error and "error" not in result.final and not result.final.get("output"):
        result.final["error"] = parser.last_error
    return result
