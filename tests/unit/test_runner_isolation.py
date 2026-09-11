"""A background job must not starve the loop that serves HTTP (NFR-102).

These tests exist because of one incident.  A real collection campaign of
2,686 plan items was launched, and for the next eighteen hours the backend
answered nothing.  It had not crashed::

    PID 36638   STAT: UN   11.4% CPU   18h34m elapsed

``U`` is uninterruptible sleep - blocked in a kernel I/O call - so SIGTERM did
not touch it and the process had to be SIGKILLed.  The logs blamed the
database::

    16:09:59  SELECT * FROM session WHERE token_hash = ?      68,862 ms
    16:10:16  SELECT * FROM source_catalogue                  16,016 ms

A 68-second lookup on a UNIQUE index over a table with tens of rows is not a
query.  ``jobs/runner.py`` started the worker with ``asyncio.create_task`` on
the very loop uvicorn serves from, and the pipeline then did synchronous
SQLite work inside it, so the coroutine that was going to read ``session``
waited 68 seconds to be *scheduled*.  Earlier the same day the same thing was
misdiagnosed as a missing index on a two-row table - which is the second
defect: the slow-statement timer measures wall clock, so starvation and a slow
query are the same line in the log.

So the claim under test is never "the code calls ``to_thread``".  It is: while
a job does a realistic amount of blocking work, the loop stays available.  The
tests here measure that directly, with a heartbeat whose gaps *are* the loop's
unavailability, and they pin four properties:

* the runner does not run a job's blocking body on the event loop (NFR-102);
* pause and cancel still land promptly while the pool is busy (FR-185);
* a job killed mid-run resumes from its checkpoint, losing at most the unit of
  work that was in flight and repeating no other (NFR-401);
* the single write lock of CR-408 still serialises writers, still lets readers
  through, and does not deadlock when several threads contend for it.

``tests/integration/test_loop_under_load.py`` makes the same claim end to end,
through real HTTP against the real collection pipeline.  This file works one
layer down, where a failure names the mechanism rather than the symptom.

Everything here is offline and runs against a throw-away database.  The
"blocking work" is real ``INSERT``s into ``vacancy`` - the table collection
actually writes, FTS triggers and all - run to a deadline rather than a row
count, so the *pressure* on the loop is the same on a slow machine and a fast
one.  No test sleeps in place of doing work: a ``time.sleep`` would prove only
that ``to_thread`` moves a sleep.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading
import time
from collections.abc import Iterator

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import (
    insert_row,
    query_all,
    query_one,
    update_row,
    utcnow,
    write_tx,
)
from dreamjob.db.migrator import migrate
from dreamjob.jobs.runner import JobContext, runner

# ---------------------------------------------------------------------------
# The budgets, and where each number comes from
# ---------------------------------------------------------------------------

#: ``DREAMJOB_LOG_SLOW_REQUEST_MS``.  The product already calls a request
#: slower than this a defect and writes a WARNING about it, so it is the
#: budget the loop has to live inside - not a number invented here.
SLOW_REQUEST_MS = 1_500

#: How long the loop may be unavailable in one go.  A request is not one turn
#: of the loop: the middleware runs, the dependency is handed to the thread
#: pool, the result is handed back, the response is written.  Budgeting a
#: third of the slow-request threshold for a single stall keeps a whole
#: request inside that threshold even when the stall lands on its worst turn.
LOOP_STALL_BUDGET_MS = 500

#: How long a control a person clicked may take to land (FR-185).  Twice the
#: slow-request threshold: enough headroom for the unit of work that was in
#: flight when they clicked, and no more.
CONTROL_BUDGET_MS = 3_000

#: ``PRAGMA busy_timeout`` in ``db/connection.py``.  Named here so the
#: contention tests below fail loudly if it is ever changed silently.
BUSY_TIMEOUT_MS = 15_000

#: One unit of a job's work, in milliseconds of real blocking database work.
#: A page of a real plan item measured ~220 ms in the integration harness; 100
#: is a conservative model of one and keeps the suite quick.
UNIT_MS = 100


@pytest.fixture()
def db(tmp_path, monkeypatch) -> Iterator[None]:
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings().ensure_dirs()
    migrate()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_jobs_left_running() -> Iterator[None]:
    """The runner is a process-wide singleton; no test may leave a job in it.

    A job now runs on its own loop on a pool thread, so its task belongs to
    another thread and must not be cancelled from here.  Setting the flag is
    the supported way in: the worker raises ``JobCancelled`` at its next
    barrier and the pool thread unwinds itself.
    """
    yield
    for job_id in list(runner._controls):
        control = runner._controls.pop(job_id, None)
        if control is not None:
            control.cancelled = True
            control.paused = False
    deadline = time.monotonic() + 10
    while runner._controls and time.monotonic() < deadline:
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------


class LoopProbe:
    """Measures how long the event loop is unavailable.

    It asks to be woken every ``interval`` seconds and records how much later
    than that it actually was.  That overshoot is the loop's unavailability -
    the same quantity that turned a session lookup into 68 seconds - measured
    without a socket, a server or a clock skew in the way.
    """

    def __init__(self, interval: float = 0.01) -> None:
        self.interval = interval
        self.gaps_ms: list[float] = []
        self._task: asyncio.Task | None = None
        self._stop = False

    async def __aenter__(self) -> LoopProbe:
        self._task = asyncio.create_task(self._sample())
        await asyncio.sleep(self.interval * 3)  # let it settle
        self.gaps_ms.clear()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _sample(self) -> None:
        previous = time.perf_counter()
        while not self._stop:
            await asyncio.sleep(self.interval)
            now = time.perf_counter()
            self.gaps_ms.append(max(0.0, (now - previous - self.interval) * 1000))
            previous = now

    @property
    def worst_ms(self) -> float:
        return max(self.gaps_ms, default=0.0)

    def report(self) -> str:
        if not self.gaps_ms:
            return "the probe never ran"
        ordered = sorted(self.gaps_ms)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        return (
            f"{len(ordered)} samples, worst stall {ordered[-1]:.0f} ms, "
            f"p95 {p95:.0f} ms (budget {LOOP_STALL_BUDGET_MS} ms)"
        )


def blocking_writes(milliseconds: int = UNIT_MS, *, tag: str = "unit") -> int:
    """One unit of a job's work: real synchronous writes, for a fixed time.

    Deliberately not a row count.  What starves the loop is *seconds spent not
    yielding*, and a row count buys a different number of those on every
    machine.  Returns how many rows were actually written, so a test can show
    the pressure was real work rather than a sleep.
    """
    deadline = time.monotonic() + milliseconds / 1000.0
    written = 0
    while time.monotonic() < deadline:
        insert_row(
            "vacancy",
            {
                "title": f"Engineer {tag}-{written}",
                "source_url": f"https://load.test/{tag}/{written}",
                "dedup_key": f"{tag}-{written}",
                "source_adapter": "test.isolation",
                "description": "x" * 400,
                "collected_at": utcnow(),
                "confidence": 0.7,
            },
        )
        written += 1
    return written


def rows_written() -> int:
    return int(query_one("SELECT COUNT(*) AS n FROM vacancy")["n"])


def job_status(job_id: str) -> str:
    row = query_one("SELECT status FROM job_run WHERE id = ?", (job_id,))
    return str(row["status"]) if row else "gone"


async def wait_for(predicate, timeout: float, *, poll: float = 0.02) -> float:
    """Seconds until ``predicate()`` is true, or ``timeout`` if it never is."""
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        if predicate():
            return time.perf_counter() - started
        await asyncio.sleep(poll)
    return timeout


# ---------------------------------------------------------------------------
# 1. The instrument has teeth
# ---------------------------------------------------------------------------


async def test_the_probe_sees_a_loop_that_is_actually_blocked(db: None) -> None:
    """The control: without this, every other test here could be vacuous.

    A test that measures loop latency and always passes is worse than no test,
    because it is read as evidence.  So the probe is first shown failing
    against the defect it exists to catch: blocking work run inline on the
    loop, which is exactly what ``asyncio.create_task`` + synchronous SQLite
    did in ``jobs/runner.py``.
    """
    async with LoopProbe() as probe:
        written = blocking_writes(700, tag="inline")  # on the loop, on purpose
        await asyncio.sleep(0.05)

    assert written > 50, "the probe must be fed real work, not a sleep"
    assert probe.worst_ms > LOOP_STALL_BUDGET_MS, (
        "the probe did not notice the loop being blocked for ~700 ms of "
        f"synchronous database work: {probe.report()}"
    )


# ---------------------------------------------------------------------------
# 2. The regression: a job must not block the loop (NFR-102)
# ---------------------------------------------------------------------------


async def test_a_job_does_not_starve_the_loop_it_shares_with_the_api(db: None) -> None:
    """NFR-102: 20 concurrent HTTP workers, and one job must not stop them.

    ``JobRunner`` accepts a plain synchronous worker - ``Worker`` is typed for
    it and ``_run`` calls ``fn(ctx)`` - and today that body runs to completion
    inline on the event loop.  A worker doing twelve units of real database
    work therefore owns the loop for as long as the work takes, which is the
    18-hour outage in miniature.

    Whatever the runner does with the worker, this is the property: the loop
    stays available while the job runs.
    """
    job_id = runner.create("test.isolation", total=12)

    def worker(_ctx: JobContext) -> None:
        for unit in range(12):
            blocking_writes(UNIT_MS, tag=f"sync-{unit}")

    async with LoopProbe() as probe:
        started = time.perf_counter()
        await runner.start(job_id, worker)
        await wait_for(lambda: job_status(job_id) == "done", timeout=30)
        elapsed = time.perf_counter() - started

    assert job_status(job_id) == "done"
    # The job has to have been under real load, or the measurement says
    # nothing: twelve units of ~100 ms is at least a second of blocking work.
    assert elapsed > 1.0, f"the job did too little work to prove anything ({elapsed:.2f}s)"
    assert rows_written() > 500, "the pressure was not real database work"
    assert probe.worst_ms < LOOP_STALL_BUDGET_MS, (
        "a background job blocked the event loop the API is served from "
        f"(NFR-102): {probe.report()}.  This is the defect that made "
        "'SELECT * FROM session WHERE token_hash = ?' take 68,862 ms."
    )


async def test_the_pool_a_job_uses_is_bounded(db: None) -> None:
    """A job's work is offloaded into a *bounded* pool, not a thread per item.

    The campaign that broke the backend had 2,686 plan items.  Moving that
    work off the loop by starting a thread for each one trades a starved loop
    for an exhausted process, and the single writer of CR-408 would then have
    thousands of threads queued on it.  Eight jobs at once must not cost the
    process an unbounded number of threads.
    """
    baseline = threading.active_count()
    peak = baseline

    def worker(_ctx: JobContext) -> None:
        for unit in range(4):
            blocking_writes(UNIT_MS, tag=f"pool-{threading.get_ident()}-{unit}")

    jobs = [runner.create("test.isolation") for _ in range(8)]
    for job_id in jobs:
        await runner.start(job_id, worker)

    while any(job_status(j) not in ("done", "failed") for j in jobs):
        peak = max(peak, threading.active_count())
        await asyncio.sleep(0.02)

    assert all(job_status(j) == "done" for j in jobs)
    assert peak - baseline <= 32, (
        f"eight concurrent jobs added {peak - baseline} threads to the process; "
        "job work belongs in a bounded pool (CR-408: one writer)"
    )


# ---------------------------------------------------------------------------
# 3. Control still works while the job is busy (FR-185)
# ---------------------------------------------------------------------------


async def test_cancel_lands_promptly_while_every_worker_is_busy(db: None) -> None:
    """FR-185: cancel takes effect within a bounded time, not eventually.

    Cancelling is the only thing a person could have done during those
    eighteen hours, so it has to work when the process is at its busiest -
    including for a job that is queued behind others rather than running.
    """
    jobs = [runner.create("test.isolation") for _ in range(8)]

    async def worker(ctx: JobContext) -> None:
        for unit in range(200):
            blocking_writes(UNIT_MS, tag=f"cancel-{ctx.job_id[:6]}-{unit}")
            await ctx.checkpoint_barrier()

    for job_id in jobs:
        await runner.start(job_id, worker)

    # More jobs than the pool has threads, so some are running and some are
    # queued behind them - both cases a person can click cancel on.
    running = await wait_for(
        lambda: any(job_status(j) == "running" for j in jobs), timeout=10
    )
    assert running < 10, "no job ever started"
    await asyncio.sleep(0.3)
    victim = next(j for j in jobs if job_status(j) == "running")

    asked = time.perf_counter()
    assert runner.cancel(victim) is True
    landed = await wait_for(lambda: job_status(victim) == "cancelled", timeout=15)
    assert job_status(victim) == "cancelled", (
        f"a running job was not cancelled {landed:.1f}s after the request"
    )
    assert landed * 1000 < CONTROL_BUDGET_MS, (
        f"cancel took {landed * 1000:.0f} ms to take effect (budget "
        f"{CONTROL_BUDGET_MS} ms).  FR-185 promises a control, not a request."
    )
    assert (time.perf_counter() - asked) * 1000 < CONTROL_BUDGET_MS

    # ... and so is a job that never got a thread: there is no worker to
    # notice a flag, so cancelling it has to settle the row itself.
    queued = [j for j in jobs if job_status(j) == "pending"]
    assert queued, "the pool was never oversubscribed, so the queued case went untested"
    runner.cancel(queued[0])
    assert await wait_for(
        lambda: job_status(queued[0]) == "cancelled", timeout=5
    ) * 1000 < CONTROL_BUDGET_MS, "a queued job stayed pending after it was cancelled"

    for job_id in jobs:
        runner.cancel(job_id)
    await wait_for(lambda: not runner._controls, timeout=20)


async def test_pause_stops_the_work_and_resume_starts_it_again(db: None) -> None:
    """FR-185: pause is only a pause if the writing actually stops.

    ``pause()`` writes ``status='paused'`` itself, so the row proves nothing.
    What has to be true is that the job stops *doing work* - and then does it
    again on resume.
    """
    job_id = runner.create("test.isolation")

    async def worker(ctx: JobContext) -> None:
        for unit in range(80):
            blocking_writes(UNIT_MS, tag=f"pause-{unit}")
            await ctx.checkpoint_barrier()

    await runner.start(job_id, worker)
    await wait_for(lambda: rows_written() > 20, timeout=10)

    assert runner.pause(job_id) is True
    # Promptness, measured: the row count has to stop moving inside the
    # control budget.  ``pause()`` writes ``status='paused'`` itself, so only
    # the work stopping is evidence that anything happened.
    settled = time.perf_counter()
    deadline = settled + CONTROL_BUDGET_MS / 1000.0
    at_rest = rows_written()
    while time.perf_counter() < deadline:
        await asyncio.sleep(0.1)
        current = rows_written()
        if current == at_rest:
            break
        at_rest = current
    else:
        pytest.fail(
            f"a paused job was still writing after {CONTROL_BUDGET_MS} ms (FR-185)"
        )
    stopped_after_ms = (time.perf_counter() - settled) * 1000

    await asyncio.sleep(0.5)
    assert rows_written() == at_rest, (
        f"a paused job wrote {rows_written() - at_rest} more rows half a second after "
        f"it had come to rest ({stopped_after_ms:.0f} ms after the pause)"
    )

    assert runner.resume(job_id) is True
    resumed = await wait_for(lambda: rows_written() > at_rest, timeout=5)
    assert rows_written() > at_rest, f"a resumed job did not start again within {resumed:.1f}s"

    runner.cancel(job_id)
    await wait_for(lambda: not runner.is_running(job_id), timeout=10)


# ---------------------------------------------------------------------------
# 4. A job survives a restart (NFR-401)
# ---------------------------------------------------------------------------


async def test_a_killed_job_resumes_and_repeats_at_most_the_unit_in_flight(db: None) -> None:
    """NFR-401: the campaign's data survived the SIGKILL; prove it still does.

    The kill is modelled the way it happened - the task disappears without the
    worker being told, so the row is left saying ``running`` - and the restart
    is modelled the way ``main.lifespan`` does it, with ``resume_orphans``
    followed by a fresh ``start`` from the stored checkpoint.

    Exactly-once is the wrong contract to assert and the module docstring of
    ``jobs/runner.py`` says so: a checkpoint is written *after* the unit it
    describes, so a kill between the two repeats that one unit.  Nothing else
    may be repeated, and nothing at all may be lost.
    """
    total = 24
    job_id = runner.create("test.isolation", total=total)

    async def worker(ctx: JobContext) -> None:
        done = int(ctx.checkpoint.get("done") or 0)
        for unit in range(done, total):
            insert_row(
                "audit_event",
                {"action": "unit", "entity_type": "resume", "entity_id": str(unit),
                 "created_at": utcnow()},
            )
            blocking_writes(40, tag=f"resume-{unit}")
            ctx.save_checkpoint(done=unit + 1)
            await ctx.checkpoint_barrier()

    def units_done() -> list[str]:
        return [
            r["entity_id"]
            for r in query_all(
                "SELECT entity_id FROM audit_event WHERE entity_type = 'resume'"
            )
        ]

    await runner.start(job_id, worker)
    await wait_for(lambda: len(units_done()) >= 8, timeout=20)

    # SIGKILL: the task stops, nothing is told, the row still says 'running'.
    control = runner._controls[job_id]
    assert control.task is not None
    control.task.cancel()
    await asyncio.sleep(0.05)
    runner._controls.pop(job_id, None)
    assert job_status(job_id) == "running", "a killed job must look interrupted, not finished"
    before_restart = units_done()
    assert 0 < len(before_restart) < total

    # Restart, exactly as the lifespan does it.
    assert await runner.resume_orphans() >= 1
    assert job_status(job_id) == "pending"
    await runner.start(job_id, worker)
    await wait_for(lambda: job_status(job_id) == "done", timeout=30)

    after = units_done()
    assert job_status(job_id) == "done"
    assert set(after) == {str(n) for n in range(total)}, (
        f"work was lost across the restart: {sorted(set(range(total)) - {int(x) for x in after})}"
    )
    repeated = len(after) - len(set(after))
    assert repeated <= 1, (
        f"{repeated} units were done twice; a restart may repeat only the unit that was "
        "in flight when the process died (NFR-401)"
    )


# ---------------------------------------------------------------------------
# 5. The single write lock (CR-408, NFR-102)
# ---------------------------------------------------------------------------


def _write_one(tag: str) -> None:
    insert_row("audit_event", {"action": tag, "entity_type": "lock", "created_at": utcnow()})


def test_concurrent_writers_are_serialised_rather_than_interleaved(db: None) -> None:
    """CR-408/NFR-102: one writer, whatever the pool does.

    Moving job work into threads only helps if the guarantee it was relying on
    survives.  Eight threads open a write transaction at the same time; the
    order in which they enter and leave has to be strictly nested - enter A,
    exit A, enter B, exit B - because ``write_tx`` holds ``_write_lock`` for
    the whole transaction.  An overlap here would mean two transactions were
    open at once and the "single writer" invariant was gone.
    """
    order: list[tuple[str, int]] = []
    order_lock = threading.Lock()
    failures: list[BaseException] = []
    ready = threading.Barrier(8)

    def writer(n: int) -> None:
        try:
            ready.wait(timeout=5)
            with write_tx() as conn:
                with order_lock:
                    order.append(("enter", n))
                conn.execute(
                    "INSERT INTO audit_event (id, action, created_at) VALUES (?, ?, ?)",
                    (f"lock-{n}", "serialised", utcnow()),
                )
                time.sleep(0.01)  # widen the window an overlap would show in
                with order_lock:
                    order.append(("exit", n))
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            failures.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not [t for t in threads if t.is_alive()], "a writer deadlocked on the write lock"
    assert not failures, f"a writer failed: {failures[0]!r}"
    assert len(order) == 16
    for i in range(0, len(order), 2):
        opened, closed = order[i], order[i + 1]
        assert opened[0] == "enter" and closed == ("exit", opened[1]), (
            f"write transactions interleaved around {opened}: {order}"
        )
    assert query_one("SELECT COUNT(*) AS n FROM audit_event")["n"] == 8


def test_a_reader_is_never_blocked_by_an_open_write_transaction(db: None) -> None:
    """The evidence that the 68-second session lookup was never the database.

    WAL plus one writer means a reader runs concurrently with a writer, and
    ``read_tx`` does not take ``_write_lock`` at all.  So with a write
    transaction held open for half a second, the very query that was blamed -
    ``SELECT ... FROM session WHERE token_hash = ?`` - still has to answer in
    single-digit milliseconds.  If it does, no amount of writing by a job can
    explain 68,862 ms, and the missing-index diagnosis is dead.
    """
    insert_row(
        "job_seeker",
        {"email": "reader@example.test", "display_name": "R",
         "created_at": utcnow(), "updated_at": utcnow()},
    )
    holding = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with write_tx() as conn:
            conn.execute(
                "INSERT INTO audit_event (id, action, created_at) VALUES ('held', 'x', ?)",
                (utcnow(),),
            )
            holding.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert holding.wait(timeout=5), "the writer never opened its transaction"

    worst = 0.0
    try:
        for _ in range(200):
            started = time.perf_counter()
            query_one(
                "SELECT s.job_seeker_id FROM session s WHERE s.token_hash = ?", ("absent",)
            )
            worst = max(worst, (time.perf_counter() - started) * 1000)
    finally:
        release.set()
        thread.join(timeout=5)

    assert worst < 50, (
        f"a session lookup took {worst:.1f} ms while one write transaction was open. "
        "In the incident it took 68,862 ms, which no writer can account for: that was "
        "the event loop, not the database."
    )


def test_a_transaction_may_not_be_opened_inside_another_one(db: None) -> None:
    """``_write_lock`` is re-entrant; the SQLite transaction underneath is not.

    Worth pinning because it is a trap for exactly the change these tests are
    about.  Re-entrancy makes ``with write_tx(): insert_row(...)`` look safe -
    the lock lets the same thread straight back in - and then SQLite refuses
    the second ``BEGIN IMMEDIATE``.  It fails fast rather than deadlocking,
    which is the tolerable half of the answer, and any offload that ends up
    nesting two write transactions on one thread will find out here rather
    than in a campaign.
    """
    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        with write_tx() as conn:
            conn.execute(
                "INSERT INTO audit_event (id, action, created_at) VALUES ('outer', 'x', ?)",
                (utcnow(),),
            )
            _write_one("inner")

    # ... and the failed outer transaction rolled back, leaving nothing behind.
    assert query_one("SELECT COUNT(*) AS n FROM audit_event")["n"] == 0


def test_a_stalled_writer_is_bounded_by_the_busy_timeout_times_the_queue(
    db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a cross-process lock plus an in-process gate really costs.

    ``write_tx`` takes the gate and only then issues ``BEGIN IMMEDIATE``, so a
    thread that is waiting on SQLite is also holding the gate every other
    writer in the process needs.  When the database is held by something
    outside this process - a second uvicorn on the same file, a ``sqlite3``
    shell, a backup - the queue does not share one timeout: each waiter pays
    its own budget in turn.

    The busy timeout and the lane budget are shortened to a few hundred
    milliseconds here so the shape can be measured in under a second.  At the
    shipped 15 s the same shape means a pool of W writers can make the last of
    them wait W x 15 s, which is longer than any request timeout - so the
    property that has to hold is that the wait is bounded and the process
    recovers, and that the pool that spends it stays small.
    """
    from dreamjob.db import connection as connection_module

    settings = get_settings()
    outsider = sqlite3.connect(str(settings.abs_db_path), timeout=30.0)
    outsider.execute("PRAGMA journal_mode=WAL")
    outsider.execute("BEGIN IMMEDIATE")
    outsider.execute(
        "INSERT INTO audit_event (id, action, created_at) VALUES ('outsider', 'x', ?)",
        (utcnow(),),
    )

    short_timeout_ms = 200
    short_budget_s = 0.5
    queue = 4
    # Both the SQLite wait and our own retry budget are the shipped values in
    # production; below they are the same shape, just faster to measure.
    monkeypatch.setattr(connection_module, "BUSY_TIMEOUT_MS", short_timeout_ms)
    monkeypatch.setattr(connection_module, "_wait_budget", lambda lane: short_budget_s)
    waits: dict[int, float] = {}
    errors: dict[int, str] = {}

    def writer(n: int) -> None:
        started = time.perf_counter()
        try:
            _write_one(f"queued-{n}")
        except (sqlite3.OperationalError, connection_module.WriteLockTimeout) as exc:
            errors[n] = str(exc)
        finally:
            waits[n] = (time.perf_counter() - started) * 1000

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(queue)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        outsider.rollback()
        outsider.close()

    assert not [t for t in threads if t.is_alive()], (
        "a writer never returned; an in-process lock held across BEGIN IMMEDIATE "
        "must not deadlock when the database is held elsewhere"
    )
    worst = max(waits.values())
    bound_ms = queue * short_budget_s * 1000
    assert worst <= bound_ms + 1_000, (
        f"the slowest queued writer waited {worst:.0f} ms behind {queue} others, more than "
        f"the {queue} x {bound_ms:.0f} ms this arrangement can cost.  Whatever the pool "
        f"does, one writer's wait must stay bounded by the queue times the lane budget "
        f"({BUSY_TIMEOUT_MS} ms busy_timeout in db/connection.py)."
    )
    assert errors, (
        "every writer got through while the database was held by another connection - "
        "the shortened budget did not take effect, so this test proved nothing"
    )

    # The database is released: the process recovers and writes again.
    _write_one("after")
    assert query_one(
        "SELECT COUNT(*) AS n FROM audit_event WHERE action = 'after'"
    )["n"] == 1


# ---------------------------------------------------------------------------
# 6. Recovery: mark *and* start (NFR-401)
# ---------------------------------------------------------------------------


async def test_a_failed_but_resumable_job_is_requeued_and_redispatched(db: None) -> None:
    """NFR-401: the boot path must start what it marks resumable.

    A worker that raised because the process was going down leaves the row
    ``failed`` with a last_error that says resumable.  ``resume_orphans`` used
    to ignore those, and nothing ever started a ``pending`` row - so a campaign
    whose ranking passes were cut short stayed cut short, with 48,269 scored
    opportunities missing.
    """
    job_id = runner.create("test.recovery", total=1)
    update_row(
        "job_run",
        job_id,
        {"status": "failed", "last_error": "interrupted by restart; resumable"},
    )

    async def worker(ctx: JobContext) -> None:
        ctx.save_checkpoint(ran=True)

    runner.register_worker("test.recovery", worker)
    try:
        assert await runner.resume_orphans() >= 1
        assert job_status(job_id) == "pending"

        started = await runner.dispatch_recoverable()
        assert started >= 1
        await wait_for(lambda: job_status(job_id) == "done", timeout=10)
        assert job_status(job_id) == "done"
    finally:
        runner._workers.pop("test.recovery", None)


async def test_a_genuine_failure_is_not_requeued(db: None) -> None:
    """Only the resumable marker is honoured; a real failure stays failed."""
    job_id = runner.create("test.not_resumable", total=1)
    update_row("job_run", job_id, {"status": "failed", "last_error": "ValueError: bad data"})

    await runner.resume_orphans()
    assert job_status(job_id) == "failed"


def test_checkpoint_collapses_the_wal(db: None) -> None:
    """A WAL that never checkpoints grows without bound (NFR-102)."""
    from dreamjob.db.connection import checkpoint

    for n in range(50):
        _write_one(f"wal-{n}")
    result = checkpoint("TRUNCATE")
    assert result["busy"] == 0, "no reader is attached in this test, so it must not be busy"
    assert result["checkpointed"] >= 0

