"""Resumable background jobs (FR-185, NFR-401, NFR-502).

Collection and generation work runs as ``JobRun`` records that can be paused,
resumed and cancelled, and that survive a crash: each worker checkpoints its
position after every unit of work, so a restart loses at most the in-flight
page (NFR-401).

The runner is deliberately in-process and asyncio-based.  Dream Job is a
single-node application over a single SQLite file (CR-408); adding a broker
would buy nothing and would break the "one writer" invariant (NFR-102).

In-process, however, must not mean *on the request loop*.  A job body is
allowed to block: the pipeline writes to SQLite synchronously and
``llm/client.py`` is a synchronous HTTP client with blocking retry sleeps.
Started with ``asyncio.create_task`` on the loop uvicorn serves from, one
collection campaign therefore owned that loop for as long as it ran -- a real
2,686-item campaign held it for eighteen hours, during which a ``SELECT`` on a
unique index over a table of tens of rows was logged at 68,862 ms because the
coroutine that would run it waited that long to be *scheduled*, not to be
answered.

So each job gets a thread from a bounded pool and its own event loop inside
it.  Whatever the worker blocks on -- the database, DeepSeek, ``time.sleep``
in a retry -- it blocks only its own loop, and the API keeps answering.  Both
pools are bounded because the opposite of a starved loop is an exhausted
process, and 2,686 items must not become 2,686 threads queued on the single
writer.  ``tests/unit/test_runner_isolation.py`` measures the loop directly
rather than asserting that this module calls ``to_thread``.

Three things make that arrangement safe rather than merely faster.

**The lane.**  Getting off the loop moves the contention rather than removing
it: a job thread and a request thread now want the same single writer
(CR-408).  Every thread this module owns is put in the ``bulk`` write lane
(``db.connection.set_write_lane``), so a request never queues behind the
thousands of writes a campaign still has to do.  That is the difference
between a campaign slowing the API down and a campaign stopping it.

**Daemon threads.**  Not ``ThreadPoolExecutor`` for the job pool: it registers
an interpreter-exit hook that *joins* its worker threads, so a process asked
to stop during a campaign would wait for the campaign to end.  The incident
this module exists for finished with a SIGKILL because the process would not
go away; a pool that can keep it alive is the wrong tool.  Daemon threads let
the interpreter leave, and NFR-401 makes the job resumable when it returns.

**A measurement.**  While any job runs, a watchdog on the *serving* loop
records how late it is woken.  That number -- not a statement duration -- is
what "the API is dark" looks like from inside the process, and its absence is
why 68,862 ms was read as a missing index (see ``observability/db_logging``).
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import queue
import threading
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from dreamjob.config import get_settings
from dreamjob.db.connection import (
    BULK,
    close_thread_connections,
    from_json,
    insert_row,
    query_all,
    query_one,
    set_write_lane,
    to_json,
    update_row,
    utcnow,
)

log = logging.getLogger(__name__)

#: How often a job's progress may reach the database.  ``progress()`` is
#: called once per unit of work, and a campaign's units are small: 2,686 plan
#: items paginated is tens of thousands of write transactions whose only
#: reader is a progress bar a person looks at a few times a minute.  Coalescing
#: them costs nothing anybody can see and takes that many acquisitions off the
#: single writer.  The value is always flushed before a terminal status, so a
#: finished job never shows a stale number.
PROGRESS_WRITE_INTERVAL_SECONDS = 0.25

#: How late the serving loop may be woken before it is worth a line.  The
#: product calls a request over 1,500 ms a defect; a quarter of that in pure
#: scheduling delay is already the beginning of the failure being watched for.
LOOP_LAG_WARN_MS = 250.0
_LOOP_PROBE_INTERVAL_SECONDS = 0.25


class JobCancelled(Exception):
    """Raised inside a worker when the user cancels the job."""


# ---------------------------------------------------------------------------
# The pools
# ---------------------------------------------------------------------------
class _DaemonPool:
    """A bounded pool of daemon threads that runs one job per thread.

    Deliberately not ``concurrent.futures.ThreadPoolExecutor`` -- see the
    module docstring: that pool joins its threads at interpreter exit, and a
    campaign must never be the reason a process will not stop.
    """

    def __init__(self, size: int, name: str) -> None:
        self.size = max(1, size)
        self._name = name
        self._work: queue.SimpleQueue[tuple[Callable[..., None], tuple[Any, ...]]] = (
            queue.SimpleQueue()
        )
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def submit(self, fn: Callable[..., None], *args: Any) -> None:
        with self._lock:
            self._work.put((fn, args))
            if len(self._threads) < self.size:
                thread = threading.Thread(
                    target=self._serve,
                    name=f"{self._name}-{len(self._threads)}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()

    def queued(self) -> int:
        """Jobs waiting for a slot.  Approximate; for the log, not for logic."""
        return self._work.qsize()

    def _serve(self) -> None:
        # Every thread in this pool exists to run job work, so it lives in the
        # bulk write lane for good (CR-408, NFR-102).
        set_write_lane(BULK)
        while True:
            fn, args = self._work.get()
            try:
                fn(*args)
            except BaseException:  # noqa: BLE001 - a pool thread must not die silently
                log.exception("A job pool thread raised outside the job")


class _SharedIOPool(ThreadPoolExecutor):
    """A thread pool that outlives the loops it is the default executor of.

    ``loop.close()`` shuts its default executor down.  That is right when the
    executor belongs to the loop, and wrong here: this one is shared by every
    job in the process, so the first job to finish would take it away from all
    the others ("cannot schedule new futures after shutdown", one job in).
    ``shutdown`` is therefore a no-op; :meth:`close_for_good` is the real one,
    and the interpreter's own exit hook drains the threads without it.
    """

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        return None

    def close_for_good(self) -> None:
        ThreadPoolExecutor.shutdown(self, wait=False, cancel_futures=True)


_io_pool: _SharedIOPool | None = None
_io_pool_lock = threading.Lock()


def _bulk_io_pool() -> _SharedIOPool:
    """The bounded pool a job's ``to_thread`` calls land in, shared by all jobs.

    One pool for the process rather than one per job, on purpose.  Per-job
    pools bound each job and nothing else: four jobs would be thirty-two
    threads all queued on the one writer of CR-408, which is the exhausted
    process the loop was traded for.  The cost of sharing is that a busy job
    can make another job's ``to_thread`` wait -- bounded queueing between
    background jobs, which is the right thing to spend.

    It has to be a ``ThreadPoolExecutor`` because ``loop.set_default_executor``
    accepts nothing else.  Its exit hook is harmless here: these threads are
    idle between calls, so joining them at exit costs one database call, not a
    campaign.
    """
    global _io_pool
    with _io_pool_lock:
        if _io_pool is None:
            _io_pool = _SharedIOPool(
                max_workers=max(1, int(getattr(get_settings(), "job_io_threads", 8))),
                thread_name_prefix="dreamjob-job-io",
                # Same lane as the job thread that will submit to it.
                initializer=lambda: set_write_lane(BULK),
            )
        return _io_pool


@dataclass
class JobContext:
    """Handle passed to a worker: progress, checkpointing and control flow."""

    job_id: str
    campaign_id: str | None = None
    job_seeker_id: str | None = None
    checkpoint: dict[str, Any] = field(default_factory=dict)
    _control: JobControl | None = None
    #: Progress is coalesced rather than written per unit of work; see
    #: PROGRESS_WRITE_INTERVAL_SECONDS.  Guarded because a worker may report
    #: from its own ``to_thread`` calls as well as from its loop.
    _progress_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _pending: dict[str, Any] = field(default_factory=dict, repr=False)
    _next_write: float = 0.0

    def progress(self, done: int, total: int | None = None) -> None:
        with self._progress_lock:
            self._pending["progress_done"] = done
            if total is not None:
                self._pending["progress_total"] = total
            now = time.monotonic()
            # A new denominator changes what the bar means, so it goes now; a
            # new numerator can wait a quarter of a second.
            if total is None and now < self._next_write:
                return
            self._next_write = now + PROGRESS_WRITE_INTERVAL_SECONDS
            values = self._take_pending()
        if values:
            update_row("job_run", self.job_id, values)

    def _take_pending(self) -> dict[str, Any]:
        """Collect the coalesced progress.  Caller holds ``_progress_lock``."""
        values, self._pending = self._pending, {}
        return values

    def drain_progress(self) -> dict[str, Any]:
        """Whatever progress has not reached the database yet."""
        with self._progress_lock:
            return self._take_pending()

    def flush_progress(self) -> None:
        values = self.drain_progress()
        if values:
            update_row("job_run", self.job_id, values)

    def save_checkpoint(self, **kwargs: Any) -> None:
        """Persist resume state (NFR-401).  Called after each unit of work.

        Carries any un-written progress with it, so the two describe the same
        moment and cost one transaction rather than two.
        """
        self.checkpoint.update(kwargs)
        values: dict[str, Any] = {"checkpoint": to_json(self.checkpoint)}
        values.update(self.drain_progress())
        update_row("job_run", self.job_id, values)

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
    #: Set once the job has been picked up by a pool thread.  Until then the
    #: job is queued, and cancelling it has to settle the row directly: there
    #: is no worker yet to notice the flag at a checkpoint barrier.
    started: bool = False
    #: The job's own task, on the job's own loop.  Written by the pool thread.
    task: asyncio.Task | None = None
    #: Guards the cancelled/started pair.  Without it a cancel can settle the
    #: row as 'cancelled' in the instant before the pool thread writes
    #: 'running' over it, and the job would run with nobody watching.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


Worker = Callable[[JobContext], "AsyncIterator[Any] | Any"]


class JobRunner:
    """Owns the running jobs of one process."""

    def __init__(self) -> None:
        self._controls: dict[str, JobControl] = {}
        self._workers: dict[str, Worker] = {}
        self._pool: _DaemonPool | None = None
        self._watchdog: asyncio.Task | None = None
        self._watchdog_loop: asyncio.AbstractEventLoop | None = None
        self._worst_lag_ms = 0.0

    def _job_pool(self) -> _DaemonPool:
        """The bounded pool jobs run in (NFR-102), created on first use."""
        if self._pool is None:
            self._pool = _DaemonPool(
                int(getattr(get_settings(), "job_pool_size", 4)), "dreamjob-job"
            )
        return self._pool

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
        # Read off the loop as well.  It is one primary-key lookup, but this
        # call is made from a request handler, and "it is only a small query"
        # is precisely the reasoning that put a campaign on the serving loop.
        row = await asyncio.to_thread(
            query_one, "SELECT * FROM job_run WHERE id = ?", (job_id,)
        )
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
        # Watch the loop we were called on -- the one uvicorn serves from --
        # for as long as any job is running (NFR-102, NFR-701).
        self._watch_loop()
        # NFR-102: hand the job to a pool thread and return.  Nothing the
        # worker does from here on can delay the loop this call was made from.
        self._job_pool().submit(self._run_isolated, ctx, fn)

    def _run_isolated(self, ctx: JobContext, fn: Worker) -> None:
        """Run one job to completion on its own loop, on a pool thread.

        The job's blocking work -- synchronous SQLite, the synchronous LLM
        client, its retry sleeps -- stalls this loop and no other, which is the
        whole point.  A job cancelled while it was still queued never starts:
        ``cancel`` has already settled its row.
        """
        control = ctx._control
        if control is not None:
            with control.lock:
                if control.cancelled:
                    return
                control.started = True

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            # Bound the job's own ``to_thread`` calls too, so that N jobs cost
            # one shared pool and not N * the interpreter default of 32.
            loop.set_default_executor(_bulk_io_pool())
            task = loop.create_task(self._run(ctx, fn))
            if control is not None:
                control.task = task
            try:
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                # The job was killed rather than asked to stop.  Its row is
                # left exactly as it was -- 'running' -- so that
                # ``resume_orphans`` finds it after a restart (NFR-401).
                log.warning("Job %s was cancelled at the task level", ctx.job_id)
        except Exception:  # noqa: BLE001 - a pool thread must not die silently
            log.exception("Job %s crashed outside its worker", ctx.job_id)
        finally:
            self._close_loop(loop, ctx.job_id)
            asyncio.set_event_loop(None)
            # This thread is going back into the pool and may next serve a job
            # against a different database file.  Its connections go now.
            close_thread_connections()
            self._controls.pop(ctx.job_id, None)
            self._release_loop_watch()

    @staticmethod
    def _close_loop(loop: asyncio.AbstractEventLoop, job_id: str) -> None:
        """Shut one job's loop down without leaving its tasks half-run.

        ``asyncio.run`` is not used here because it would also shut the default
        executor down, and that executor is shared by every job.
        """
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001 - best effort on the way out
            log.debug("Job %s left work unfinished on its loop", job_id, exc_info=True)
        finally:
            loop.close()

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
            self._settle(
                ctx,
                {
                    "status": "done",
                    "finished_at": utcnow(),
                    "estimated_seconds": int(time.monotonic() - started),
                },
            )
        except JobCancelled:
            self._settle(ctx, {"status": "cancelled", "finished_at": utcnow()})
        except Exception as exc:  # noqa: BLE001 - recorded, surfaced in the dashboard
            log.exception("Job %s failed", ctx.job_id)
            self._settle(
                ctx,
                {"status": "failed", "finished_at": utcnow(), "last_error": str(exc)[:2000]},
            )
        finally:
            self._controls.pop(ctx.job_id, None)

    @staticmethod
    def _settle(ctx: JobContext, values: dict[str, Any]) -> None:
        """Write the terminal status, carrying any coalesced progress with it.

        One transaction, and in this order: nothing may ever read ``done``
        beside a progress number that has not caught up.
        """
        values.update(ctx.drain_progress())
        update_row("job_run", ctx.job_id, values)

    # -- the serving loop's own health (NFR-102, NFR-701) -------------------
    def _watch_loop(self) -> None:
        """Start measuring the calling loop's scheduling delay, if it is not.

        ``start`` is called from a request handler, so the loop it is called on
        is the loop uvicorn serves from.  A job is the only thing that has ever
        stalled it, so the watch lives exactly as long as one is running.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - start() outside a loop
            return
        if (
            self._watchdog is not None
            and not self._watchdog.done()
            and self._watchdog_loop is loop
        ):
            return
        self._worst_lag_ms = 0.0
        self._watchdog_loop = loop
        self._watchdog = loop.create_task(self._measure_loop_lag())

    async def _measure_loop_lag(self) -> None:
        """Ask to be woken on a fixed interval; record how late it happens.

        The overshoot *is* the API's unavailability.  It is the quantity that
        turned a session lookup into 68,862 ms, and the one thing no statement
        timer can report, because a statement timer is running inside the
        stall it is trying to describe.
        """
        try:
            while True:
                sent = time.perf_counter()
                await asyncio.sleep(_LOOP_PROBE_INTERVAL_SECONDS)
                lag_ms = (time.perf_counter() - sent - _LOOP_PROBE_INTERVAL_SECONDS) * 1000.0
                if lag_ms > self._worst_lag_ms:
                    self._worst_lag_ms = lag_ms
                if lag_ms >= LOOP_LAG_WARN_MS:
                    log.warning(
                        "event loop lag ms=%.0f jobs=%d: the API was not being served for "
                        "this long, whatever the statement timings say",
                        lag_ms, len(self._controls),
                    )
                if not self._controls:
                    return
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            raise

    def _release_loop_watch(self) -> None:
        """Stop watching once the last job is done, and say what it cost."""
        if self._controls or self._watchdog is None:
            return
        worst = self._worst_lag_ms
        task, loop = self._watchdog, self._watchdog_loop
        self._watchdog = None
        log.info("No jobs running; worst event loop lag while they ran: %.0f ms", worst)
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:  # pragma: no cover - the loop went away first
            pass

    @property
    def worst_loop_lag_ms(self) -> float:
        """Worst scheduling delay seen on the serving loop since a job began."""
        return self._worst_lag_ms

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
        with c.lock:
            c.cancelled = True
            c.paused = False
            queued = not c.started
        if queued:
            # FR-185: the job is queued behind a full pool, so there is no
            # worker to notice the flag at a barrier.  Settle it here rather
            # than leave the user waiting for a slot to free up; the pool
            # thread will find ``cancelled`` set and never start it.
            update_row("job_run", job_id, {"status": "cancelled", "finished_at": utcnow()})
            self._controls.pop(job_id, None)
        return True

    def is_running(self, job_id: str) -> bool:
        return job_id in self._controls

    def shutdown(self, timeout: float = 5.0) -> int:
        """Ask every running job to stop, and wait a little for it to (NFR-401).

        Best effort by design.  A job that will not reach a barrier inside the
        timeout is left to the daemon threads and to ``resume_orphans``: the
        checkpoint is what makes that safe, and a process that cannot exit is
        the failure this module is here to prevent.
        """
        running = list(self._controls)
        for job_id in running:
            control = self._controls.get(job_id)
            if control is not None:
                control.cancelled = True
                control.paused = False
        deadline = time.monotonic() + timeout
        while self._controls and time.monotonic() < deadline:
            time.sleep(0.05)
        if self._controls:
            log.warning(
                "%d job(s) did not stop within %.1fs; they are resumable (NFR-401)",
                len(self._controls), timeout,
            )
        return len(running)

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
        """On startup, requeue jobs a crash left unfinished (NFR-401).

        Two populations are recoverable.  A job whose worker was killed outright
        is still written 'running' or 'paused', and is marked resumable.  A job
        whose worker raised *because* the process was going down is written
        'failed' with a last_error that says so (``interrupted by restart;
        resumable``); leaving those alone is what left a campaign holding 48,269
        unscored opportunities after the ranking passes were cut short.
        """
        return await asyncio.to_thread(self._resume_orphans)

    @staticmethod
    def _resume_orphans() -> int:
        rows = query_all(
            "SELECT id FROM job_run WHERE status IN ('running', 'paused') "
            "   OR (status = 'failed' AND last_error LIKE '%resumable%')"
        )
        for row in rows:
            update_row(
                "job_run",
                row["id"],
                {"status": "pending", "last_error": "interrupted by restart; resumable"},
            )
        return len(rows)

    async def dispatch_recoverable(self) -> int:
        """Start the jobs ``resume_orphans`` just requeued (NFR-401).

        Marking a job resumable without starting it is a promise the product
        never kept: nothing else scans for ``pending`` rows, so an interrupted
        campaign stayed interrupted until a person pressed Resume.  Only jobs
        carrying the resumable marker are dispatched, so a deliberately created
        but unstarted job is never picked up by the boot path.
        """
        rows = query_all(
            "SELECT id, kind FROM job_run "
            "WHERE status = 'pending' AND last_error LIKE '%resumable%'"
        )
        started = 0
        for row in rows:
            if row["kind"] not in self._workers or self.is_running(row["id"]):
                continue
            try:
                await self.start(row["id"])
                started += 1
            except Exception:  # noqa: BLE001 - one unrecoverable row must not block boot
                log.exception("Could not resume interrupted job %s", row["id"])
        return started


runner = JobRunner()

# A process on its way out asks its jobs to stop, so the last thing a campaign
# does is checkpoint rather than be killed mid-page.
atexit.register(runner.shutdown, 2.0)
