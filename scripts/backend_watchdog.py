#!/usr/bin/env python3
"""Keep the Dream Job API answering.

The API worker has died natively more than once (heap corruption inside the
crawler's HTML parser) while uvicorn's ``--reload`` parent stayed alive holding
port 8000, so clients hung instead of failing fast. This watchdog probes
``/api/health`` and restarts the server when the worker is actually gone.

It must **not** restart a live worker whose event loop is merely busy. The
machine memory-thrashes under the contacts sweep; a stalled loop makes
``/api/health`` time out for several seconds after the socket accepted the
connection, and an earlier version of this script killed and restarted the
worker for that - losing a job that had no resume state. So a failed probe is
only a restart reason once the process tree says the worker has died:

* the listeners on 8000 are found with ``lsof``;
* the worker is alive when one of those PIDs has a child process that is not
  the ``multiprocessing.resource_tracker`` (the ``--reload`` parent spawns one
  server child and one tracker child, and only the tracker outlives a dead
  server);
* a stalled-but-alive worker only logs ``stalled but alive`` once per stall
  and is left to recover;
* a dead worker restarts after two consecutive failed probes, and a missing
  listener (dead socket) restarts after one.

Run it detached:
    .venv/bin/python scripts/backend_watchdog.py

Stop it by killing the process (the API keeps running; only supervision stops).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

REPO = Path(__file__).resolve().parents[1]
PYTHON = REPO / ".venv" / "bin" / "python"
BACKEND_LOG = Path("/tmp/dreamjob-backend.log")
WATCHDOG_LOG = Path("/tmp/dreamjob-watchdog.log")
HEALTH_URL = "http://127.0.0.1:8000/api/health"

CHECK_EVERY = 20  # seconds between probes
HEALTH_TIMEOUT = 15.0  # seconds a stalled loop may take before the probe fails
FAILS_BEFORE_RESTART = 2  # dead worker: two consecutive failed probes
COOLDOWN = 90  # seconds to let a fresh server boot before probing again
LSOF = shutil.which("lsof") or "/usr/sbin/lsof"
PGREP = shutil.which("pgrep") or "/usr/bin/pgrep"
PS = shutil.which("ps") or "/bin/ps"
#: How the reloader names its bookkeeping child; that child is not the server.
RESOURCE_TRACKER_MARKER = "multiprocessing.resource_tracker"


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    with WATCHDOG_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def healthy(timeout: float = HEALTH_TIMEOUT) -> bool:
    try:
        with urlopen(HEALTH_URL, timeout=timeout) as response:  # noqa: S310 - localhost
            return 200 <= response.status < 300
    except (URLError, OSError, ValueError):
        return False


def listeners() -> list[int]:
    """PIDs holding a LISTEN socket on 8000 (the reloader and its worker)."""
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv
            [LSOF, "-nP", "-iTCP:8000", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for raw in out.split():
        try:
            pids.append(int(raw))
        except ValueError:
            continue
    return pids


def child_pids(pid: int) -> list[int]:
    """Direct children of ``pid``, empty when it has none or pgrep fails."""
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv
            [PGREP, "-P", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for raw in out.split():
        try:
            pids.append(int(raw))
        except ValueError:
            continue
    return pids


def child_commands(pids: list[int]) -> dict[int, str]:
    """``{pid: command line}`` for the given PIDs, empty strings when unknown."""
    if not pids:
        return {}
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv
            [PS, "-o", "pid=,command=", "-p", ",".join(str(pid) for pid in pids)],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    commands: dict[int, str] = {}
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        raw_pid, _, command = stripped.partition(" ")
        try:
            commands[int(raw_pid)] = command.strip()
        except ValueError:
            continue
    return commands


def worker_alive(pids: list[int] | None = None) -> bool:
    """Is an actual uvicorn server process still there?

    The reloader parent owns the listening socket, so its presence proves
    nothing: it survived the native worker crashes.  What distinguishes alive
    from dead is whether a listener has a child that is not the
    ``multiprocessing.resource_tracker`` - that child is the server uvicorn
    spawned, and it disappears when the server dies.
    """
    listening = listeners() if pids is None else pids
    if not listening:
        return False
    for pid in listening:
        children = child_pids(pid)
        commands = child_commands(children)
        for child in children:
            command = commands.get(child, "")
            if command and RESOURCE_TRACKER_MARKER not in command:
                return True
    return False


def stop_listeners() -> None:
    for pid in listeners():
        try:
            os.kill(pid, signal.SIGTERM)
            log(f"watchdog: SIGTERM to {pid}")
        except ProcessLookupError:
            pass
    time.sleep(5)
    for pid in listeners():
        try:
            os.kill(pid, signal.SIGKILL)
            log(f"watchdog: SIGKILL to {pid}")
        except ProcessLookupError:
            pass


def start_api() -> None:
    handle = BACKEND_LOG.open("ab", buffering=0)
    subprocess.Popen(  # noqa: S603 - fixed argv
        [
            str(PYTHON),
            "-m",
            "uvicorn",
            "dreamjob.main:app",
            "--reload",
            "--timeout-graceful-shutdown",
            "10",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        cwd=str(REPO),
        env={**os.environ, "PYTHONPATH": "backend"},
        stdout=handle,
        stderr=handle,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    log("watchdog: started the API")


def restart(reason: str) -> None:
    log(f"watchdog: {reason}; killing any listeners and starting the API")
    stop_listeners()
    start_api()


def main() -> None:
    log(
        f"watchdog: supervising {HEALTH_URL} every {CHECK_EVERY}s "
        f"(health timeout {HEALTH_TIMEOUT:.0f}s)"
    )
    dead_failures = 0
    stalled_logged = False
    while True:
        if healthy():
            if dead_failures or stalled_logged:
                log("watchdog: health check OK; supervision state reset")
            dead_failures = 0
            stalled_logged = False
        else:
            listening = listeners()
            if not listening:
                # Nothing holds the port: there is no worker to protect.
                restart("health check failed and nothing listens on 8000 (dead socket)")
                dead_failures = 0
                stalled_logged = False
                time.sleep(COOLDOWN)
            elif worker_alive(listening):
                # The loop is busy, not gone: a restart would kill live work.
                dead_failures = 0
                if not stalled_logged:
                    log(
                        "watchdog: health check failed but the worker is stalled but alive; "
                        "not restarting"
                    )
                    stalled_logged = True
            else:
                dead_failures += 1
                if dead_failures >= FAILS_BEFORE_RESTART:
                    restart(
                        f"health check failed and the worker is dead "
                        f"({dead_failures}/{FAILS_BEFORE_RESTART})"
                    )
                    dead_failures = 0
                    stalled_logged = False
                    time.sleep(COOLDOWN)
                else:
                    log(
                        f"watchdog: health check failed and no live worker was found "
                        f"({dead_failures}/{FAILS_BEFORE_RESTART}); watching before restart"
                    )
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
