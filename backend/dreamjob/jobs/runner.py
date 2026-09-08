"""Resumable background jobs (FR-185, NFR-401, NFR-502).

Collection and generation work runs as ``JobRun`` records that can be paused,
resumed and cancelled, and that survive a crash: each worker checkpoints its
position after every unit of work, so a restart loses at most the in-flight
page (NFR-401).

The runner is deliberately in-process and asyncio-based.  Dream Job is a
single-node application over a single SQLite file (CR-408); adding a broker
would buy nothing and would break the "one writer" invariant (NFR-102).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from dreamjob.db.connection import (
    from_json,
    insert_row,
    query_all,
    query_one,
    to_json,
    update_row,
    utcnow,
)

log = logging.getLogger(__name__)


class JobCancelled(Exception):
    """Raised inside a worker when the user cancels the job."""


@dataclass
class JobContext:
    """Handle passed to a worker: progress, checkpointing and control flow."""

    job_id: str
    campaign_id: str | None = None
    job_seeker_id: str | None = None
    checkpoint: dict[str, Any] = field(default_factory=dict)
    _control: JobControl | None = None

    def progress(self, done: int, total: int | None = None) -> None:
        values: dict[str, Any] = {"progress_done": done}
        if total is not None:
            values["progress_total"] = total
        update_row("job_run", self.job_id, values)

    def save_checkpoint(self, **kwargs: Any) -> None:
        """Persist resume state (NFR-401).  Called after each unit of work."""
        self.checkpoint.update(kwargs)
        update_row("job_run", self.job_id, {"checkpoint": to_json(self.checkpoint)})

    def record_error(self, error: str) -> None:
        row = query_one("SELECT error_count FROM job_run WHERE id = ?", (self.job_id,))
        update_row(
            "job_run",
            self.job_id,
            {"error_count": int(row["error_count"] or 0) + 1 if row else 1, "last_error": error[:2000]},
        )

    async def checkpoint_barrier(self) -> None:
        """Yield to the loop and honour pause/cancel requests (FR-185, FR-206)."""
        await asyncio.sleep(0)
        if self._control is None:
            return
        if self._control.cancelled:
            raise JobCancelled(f"Job {self.job_id} cancelled by user")
        while self._control.paused:
            await asyncio.sleep(0.5)
            if self._control.cancelled:
                raise JobCancelled(f"Job {self.job_id} cancelled by user")


@dataclass
class JobControl:
    paused: bool = False
    cancelled: bool = False
    task: asyncio.Task | None = None


Worker = Callable[[JobContext], "AsyncIterator[Any] | Any"]


class JobRunner:
    """Owns the running jobs of one process."""

    def __init__(self) -> None:
        self._controls: dict[str, JobControl] = {}
        self._workers: dict[str, Worker] = {}

    def register_worker(self, kind: str, fn: Worker) -> None:
        self._workers[kind] = fn

    # -- lifecycle ----------------------------------------------------------
    def create(
        self,
        kind: str,
        *,
        campaign_id: str | None = None,
        job_seeker_id: str | None = None,
        adapter_key: str | None = None,
        total: int | None = None,
        estimated_seconds: int | None = None,
    ) -> str:
        return insert_row(
            "job_run",
            {
                "job_seeker_id": job_seeker_id,
                "campaign_id": campaign_id,
                "kind": kind,
                "adapter_key": adapter_key,
                "status": "pending",
                "progress_total": total,
                "estimated_seconds": estimated_seconds,
                "created_at": utcnow(),
            },
        )

    async def start(self, job_id: str, worker: Worker | None = None) -> None:
        row = query_one("SELECT * FROM job_run WHERE id = ?", (job_id,))
        if row is None:
            raise KeyError(f"No such job {job_id}")
        fn = worker or self._workers.get(row["kind"])
        if fn is None:
            raise KeyError(f"No worker registered for job kind {row['kind']!r}")

        control = JobControl()
        self._controls[job_id] = control
        ctx = JobContext(
            job_id=job_id,
            campaign_id=row["campaign_id"],
            job_seeker_id=row["job_seeker_id"],
            checkpoint=from_json(row["checkpoint"], {}) or {},
            _control=control,
        )
        control.task = asyncio.create_task(self._run(ctx, fn))

    async def _run(self, ctx: JobContext, fn: Worker) -> None:
        started = time.monotonic()
        update_row("job_run", ctx.job_id, {"status": "running", "started_at": utcnow()})
        try:
            result = fn(ctx)
            if hasattr(result, "__aiter__"):
                async for _ in result:  # type: ignore[union-attr]
                    await ctx.checkpoint_barrier()
            elif asyncio.iscoroutine(result):
                await result
            update_row(
                "job_run",
                ctx.job_id,
                {
                    "status": "done",
                    "finished_at": utcnow(),
                    "estimated_seconds": int(time.monotonic() - started),
                },
            )
        except JobCancelled:
            update_row("job_run", ctx.job_id, {"status": "cancelled", "finished_at": utcnow()})
        except Exception as exc:  # noqa: BLE001 - recorded, surfaced in the dashboard
            log.exception("Job %s failed", ctx.job_id)
            update_row(
                "job_run",
                ctx.job_id,
                {"status": "failed", "finished_at": utcnow(), "last_error": str(exc)[:2000]},
            )
        finally:
            self._controls.pop(ctx.job_id, None)

    # -- control (FR-185, FR-206) ------------------------------------------
    def pause(self, job_id: str) -> bool:
        c = self._controls.get(job_id)
        if not c:
            return False
        c.paused = True
        update_row("job_run", job_id, {"status": "paused"})
        return True

    def resume(self, job_id: str) -> bool:
        c = self._controls.get(job_id)
        if not c:
            return False
        c.paused = False
        update_row("job_run", job_id, {"status": "running"})
        return True

    def cancel(self, job_id: str) -> bool:
        c = self._controls.get(job_id)
        if not c:
            update_row("job_run", job_id, {"status": "cancelled", "finished_at": utcnow()})
            return False
        c.cancelled = True
        c.paused = False
        return True

    def is_running(self, job_id: str) -> bool:
        return job_id in self._controls

    # -- inspection (FR-361) ------------------------------------------------
    @staticmethod
    def status(job_id: str) -> dict | None:
        return query_one("SELECT * FROM job_run WHERE id = ?", (job_id,))

    @staticmethod
    def for_campaign(campaign_id: str) -> list[dict]:
        return query_all(
            "SELECT * FROM job_run WHERE campaign_id = ? ORDER BY created_at DESC",
            (campaign_id,),
        )

    async def resume_orphans(self) -> int:
        """On startup, mark jobs left 'running' by a crash as resumable (NFR-401)."""
        rows = query_all("SELECT id FROM job_run WHERE status IN ('running', 'paused')")
        for row in rows:
            update_row(
                "job_run",
                row["id"],
                {"status": "pending", "last_error": "interrupted by restart; resumable"},
            )
        return len(rows)


runner = JobRunner()
