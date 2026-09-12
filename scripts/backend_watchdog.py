#!/usr/bin/env python3
"""Keep the Dream Job API answering.

The API worker has died natively more than once (heap corruption inside the
crawler's HTML parser) while uvicorn's ``--reload`` parent stayed alive holding
port 8000, so clients hung instead of failing fast. This watchdog probes
``/api/health``; after a few consecutive failures it kills whatever holds the
port and starts the server again, detached, logging both actions.

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
FAILS_BEFORE_RESTART = 3  # ~60s of no answer before acting
COOLDOWN = 90  # seconds to let a fresh server boot before probing again
LSOF = shutil.which("lsof") or "/usr/sbin/lsof"


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    with WATCHDOG_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def healthy(timeout: float = 5.0) -> bool:
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


def main() -> None:
    log(f"watchdog: supervising {HEALTH_URL} every {CHECK_EVERY}s")
    failures = 0
    while True:
        if healthy():
            failures = 0
        else:
            failures += 1
            log(f"watchdog: health check failed ({failures}/{FAILS_BEFORE_RESTART})")
            if failures >= FAILS_BEFORE_RESTART:
                stop_listeners()
                start_api()
                failures = 0
                time.sleep(COOLDOWN)
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()
