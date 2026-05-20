"""Subprocess manager for `hybrid-agent run`, durable across Streamlit reruns.

Streamlit re-executes the page script on every interaction; `st.session_state`
survives that but only within one browser tab. To make Run-state visible from
any tab (and after a webui restart) we also keep a PID file next to the state
DB. The Popen handle itself is cached in session_state when the current tab
owns the process so we can drain stdout cleanly.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PIDFILE_NAME = ".hybrid_agent_webui_run.pid"
LOGFILE_NAME = ".hybrid_agent_webui_run.log"


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID currently exists."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # Probing via OpenProcess is the cleanest answer on Windows.
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return f'"{pid}"' in out.stdout
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # exists but owned by someone else — treat as alive
        return True


@dataclass
class RunHandle:
    pid: int
    started_at: float
    log_file: Path

    def is_alive(self) -> bool:
        return _pid_alive(self.pid)


def _pidfile(workdir: Path) -> Path:
    return workdir / PIDFILE_NAME


def _logfile(workdir: Path) -> Path:
    return workdir / LOGFILE_NAME


def read_handle(workdir: Path) -> RunHandle | None:
    p = _pidfile(workdir)
    if not p.exists():
        return None
    try:
        pid_str, started = p.read_text(encoding="utf-8").strip().split("|", 1)
        pid = int(pid_str)
    except Exception:
        p.unlink(missing_ok=True)
        return None
    h = RunHandle(pid=pid, started_at=float(started), log_file=_logfile(workdir))
    if not h.is_alive():
        p.unlink(missing_ok=True)
        return None
    return h


def start(
    *,
    workdir: Path,
    config_path: Path,
    tasks_path: Path,
    auto_resume: int = 0,
) -> RunHandle:
    """Spawn `hybrid-agent run -c <cfg> -t <tasks> [--auto-resume N]`.

    Stdout/stderr go to a log file in workdir so the UI can tail them.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    log_path = _logfile(workdir)
    log_fh = open(log_path, "wb")  # truncate per run; tail re-reads each tick

    cmd: list[str] = [
        sys.executable,
        "-m",
        "hybrid_agent",
        "run",
        "-c",
        str(config_path),
        "-t",
        str(tasks_path),
    ]
    if auto_resume > 0:
        cmd += ["--auto-resume", str(auto_resume)]

    creationflags = 0
    preexec_fn = None
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        preexec_fn = os.setsid  # type: ignore[assignment]

    proc = subprocess.Popen(
        cmd,
        cwd=str(workdir),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        preexec_fn=preexec_fn,
    )

    _pidfile(workdir).write_text(f"{proc.pid}|{time.time()}", encoding="utf-8")
    return RunHandle(pid=proc.pid, started_at=time.time(), log_file=log_path)


def stop(workdir: Path) -> bool:
    """Ask the running orchestrator to shut down gracefully (SIGINT / CTRL+BREAK).

    Returns True if a process was signaled.
    """
    h = read_handle(workdir)
    if h is None:
        return False
    try:
        if os.name == "nt":
            os.kill(h.pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        else:
            os.killpg(os.getpgid(h.pid), signal.SIGINT)
    except (ProcessLookupError, PermissionError, OSError):
        # process already exited or we lost the right to signal it
        _pidfile(workdir).unlink(missing_ok=True)
        return False
    return True


def kill(workdir: Path) -> bool:
    """Force-kill the run. Use when graceful stop hangs."""
    h = read_handle(workdir)
    if h is None:
        return False
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(h.pid)],
                check=False,
                capture_output=True,
            )
        else:
            os.killpg(os.getpgid(h.pid), signal.SIGKILL)
    except Exception:
        return False
    _pidfile(workdir).unlink(missing_ok=True)
    return True


def tail_log(workdir: Path, max_bytes: int = 200_000) -> str:
    p = _logfile(workdir)
    if not p.exists():
        return ""
    size = p.stat().st_size
    with open(p, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()  # discard partial first line
        data = f.read()
    return data.decode("utf-8", errors="replace")
