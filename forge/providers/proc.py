"""Shared subprocess runner for CLI adapters.

* Starts the CLI in its own process group so cancellation kills the whole tree.
* Streams stdout line by line to a parser and to a raw log file.
* Enforces wall-clock timeout and cooperative cancellation.
* Uses an allowlisted environment: host-session variables and API keys are not
  inherited, so a CLI cannot silently switch to directly billed API usage.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ENV_ALLOW = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR", "CODEX_HOME",
             "XDG_CONFIG_HOME", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def clean_env(extra: dict[str, str] | None = None, *, path_prepend: list[str] | None = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    parts = list(path_prepend or [])
    home = os.environ.get("HOME", "")
    parts += [f"{home}/.local/bin", DEFAULT_PATH]
    env["PATH"] = ":".join(parts)
    env.setdefault("LANG", "en_US.UTF-8")
    env["NO_COLOR"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra or {})
    return env


@dataclass
class ProcOutcome:
    exit_code: int | None
    stderr: str
    timed_out: bool
    cancelled: bool
    aborted_reason: str | None
    duration_s: float


class Aborted(Exception):
    """Raised by a line handler to stop the process (e.g. turn limit exceeded)."""


async def _kill_group(proc: asyncio.subprocess.Process, grace: float = 3.0) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()


async def run_streaming(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdin_text: str | None,
    timeout_s: float,
    cancel: asyncio.Event,
    on_line: Callable[[str], "asyncio.Future | None"],
    log_path: Path | None = None,
    on_start: Callable[[int], None] | None = None,
) -> ProcOutcome:
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=16 * 1024 * 1024,
    )
    if on_start:
        on_start(proc.pid)
    logf = open(log_path, "a", encoding="utf-8") if log_path else None
    stderr_chunks: list[bytes] = []
    aborted: str | None = None

    async def feed_stdin() -> None:
        if stdin_text is not None and proc.stdin:
            try:
                proc.stdin.write(stdin_text.encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()

    async def read_stdout() -> None:
        nonlocal aborted
        assert proc.stdout
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip("\n")
            if logf:
                logf.write(text + "\n")
                logf.flush()
            try:
                r = on_line(text)
                if asyncio.iscoroutine(r):
                    await r
            except Aborted as e:
                aborted = str(e)
                await _kill_group(proc)
                break

    async def read_stderr() -> None:
        assert proc.stderr
        while True:
            chunk = await proc.stderr.read(65536)
            if not chunk:
                break
            stderr_chunks.append(chunk)
            if logf:
                logf.write("[stderr] " + chunk.decode(errors="replace"))
                logf.flush()

    io = asyncio.gather(feed_stdin(), read_stdout(), read_stderr())
    cancel_wait = asyncio.create_task(cancel.wait())
    timed_out = cancelled = False
    try:
        done, _ = await asyncio.wait({io, cancel_wait}, timeout=timeout_s, return_when=asyncio.FIRST_COMPLETED)
        if io not in done:
            if cancel_wait in done:
                cancelled = True
            else:
                timed_out = True
            await _kill_group(proc)
            try:
                await asyncio.wait_for(io, 5)
            except (asyncio.TimeoutError, Exception):
                io.cancel()
        await proc.wait()
    except asyncio.CancelledError:
        await _kill_group(proc)
        raise
    finally:
        cancel_wait.cancel()
        if logf:
            logf.close()
    return ProcOutcome(
        exit_code=proc.returncode,
        stderr=b"".join(stderr_chunks).decode(errors="replace")[-20000:],
        timed_out=timed_out,
        cancelled=cancelled,
        aborted_reason=aborted,
        duration_s=time.monotonic() - start,
    )


async def run_simple(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
                     timeout_s: float = 30) -> tuple[int | None, str, str]:
    """Run a short command and capture output (used for detection)."""
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd) if cwd else None, env=env or clean_env(), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except asyncio.TimeoutError:
        await _kill_group(proc)
        return None, "", "timeout"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_pid_group(pid: int) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.5)
