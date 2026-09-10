"""The API must answer while a collection campaign runs (NFR-102, FR-185, NFR-401).

This is the test that would have caught the eighteen-hour outage.

A real campaign of 2,686 plan items was launched against a 96 MB database
holding 50,695 plan items.  The job ran, checkpointed, and eventually survived
a SIGKILL with its data intact - and for eighteen hours the interface was dead.
The process had not crashed and was not deadlocked; it sat in uninterruptible
sleep doing I/O at 11% CPU, and every request was simply waiting for the event
loop to get round to it::

    16:09:59  SELECT * FROM session WHERE token_hash = ?      68,862 ms
    16:10:16  SELECT * FROM session WHERE token_hash = ?      16,838 ms
    16:10:16  SELECT * FROM source_catalogue                  16,016 ms
    16:09:59  POST /api/campaigns/{id}/plan  200               13.14 s

``jobs/runner.py`` started the worker with ``asyncio.create_task``, on the loop
uvicorn serves HTTP from, and ``pipeline/collection.py`` then did synchronous
SQLite work inside it - ``insert_row``, ``update_row``, ``write_tx``, once per
record, tens of records per page.  While collection wrote, nothing was served.

So the claim under test is not that some function is called on a thread.  It is
the user-visible property: **with a collection job doing real work, the API
still answers inside the latency the product itself calls acceptable.**
``DREAMJOB_LOG_SLOW_REQUEST_MS`` defaults to 1,500 ms and the middleware
already logs anything above it as a defect, so that is the budget - a request
that takes 68 seconds fails this test by a factor of forty-five.

**How it is arranged, and why.**  The application is served by a real uvicorn
on a real socket, on its own thread, and the requests are made by an ordinary
HTTP client from the test's thread.  That matters: a probe running *on the loop
under test* can only issue a request when the loop is free, so it cannot see a
stall that begins while it has nothing in flight, and it reports a flattering
number.  An arriving TCP connection has no such courtesy.  This is the
production arrangement - one loop, serving HTTP and running the job - and the
job is started the way a person starts it, with ``POST /launch``.

The load itself runs through the real ``collection_worker``, the real
knowledge-base writer and the real ``db.connection`` layer.  Only the network
is replaced: the adapter is a stub in the ordinary registry and every fetch is
answered from memory, so the test is offline, deterministic and repeatable.
What it keeps from production is the part that mattered - many plan items in
many rate-limit buckets, each page writing tens of records synchronously.

``tests/unit/test_runner_isolation.py`` pins the same properties one layer
down, where a failure names the mechanism instead of the symptom.

The heavy test is marked ``load``, so a quick run can drop it with
``-m 'not load'``, but it is collected by default.  This is the failure that
cost a real user eighteen hours of a campaign; a test that only runs when
somebody remembers it is not protection.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from dreamjob.adapters import base as adapter_registry
from dreamjob.adapters.base import (
    AccessMethod,
    AdapterCapabilities,
    NormalisedRecord,
    PlanItem,
    RawRecord,
    SourceAdapter,
    SourceType,
)
from dreamjob.config import REPO_ROOT, get_settings
from dreamjob.db.connection import from_json, insert_row, query_one, upsert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.egress.client import EgressClient, FetchResult
from dreamjob.jobs.runner import runner

# ---------------------------------------------------------------------------
# The budgets, and where the numbers come from
# ---------------------------------------------------------------------------

#: ``DREAMJOB_LOG_SLOW_REQUEST_MS``.  The middleware already writes a WARNING
#: for a request slower than this, so it is not a number invented for this
#: test: it is the latency the product itself calls a defect.
SLOW_REQUEST_MS = 1_500

#: p95 for reads taken while a job runs.  The same 1,500 ms: a dashboard
#: polling a running campaign is the normal case, not a degraded one, and
#: NFR-102 asks for at least 20 concurrent HTTP workers with writes serialised
#: through a single writer - not for reads to stop.
P95_BUDGET_MS = SLOW_REQUEST_MS

#: No single request may exceed this, whatever the p95 says.  Four times the
#: threshold leaves room for one unlucky request arriving behind a write
#: transaction, and still fails a 68-second one by more than a decimal order.
WORST_REQUEST_MS = 4 * SLOW_REQUEST_MS

#: How long a control a person clicked may take to be answered and to take
#: effect (FR-185).  Twice the slow-request threshold.
CONTROL_BUDGET_MS = 3_000

# The shape of the load.  Production was 2,686 plan items across many hosts,
# each page writing tens of records through the knowledge-base writer.  This is
# the same shape at 1/168th of the size: enough plan items for the loop's ready
# queue to be long, enough records per page for the writes to be the cost.
PLAN_ITEMS = 16
PAGES_PER_ITEM = 2
RECORDS_PER_PAGE = 60
EXPECTED_VACANCIES = PLAN_ITEMS * PAGES_PER_ITEM * RECORDS_PER_PAGE

OWNER_EMAIL = "owner@example.com"
PASSWORD = "Str0ng-Passphrase!2026"


# ---------------------------------------------------------------------------
# A stub source, in the ordinary registry
# ---------------------------------------------------------------------------


class _LoadBoard(SourceAdapter):
    """A job board that answers instantly and hands back a page of postings.

    It exists to make the *pipeline* work, not to be slow itself: the blocking
    cost under test is ``KnowledgeBaseWriter.write_many`` storing
    ``RECORDS_PER_PAGE`` vacancies and their employers, one synchronous
    transaction at a time, exactly as a real board's page does.
    """

    key = "load.board"
    display_name = "Load board"
    source_type = SourceType.JOB_BOARD
    access_method = AccessMethod.API
    capabilities = AdapterCapabilities(pagination=True, max_results_per_query=100)
    coverage_countries = ["BE"]

    #: Every (plan item tag, page) this process has fetched, in order.  The
    #: restart test reads it to prove no completed page was collected twice.
    fetched: list[tuple[str, int]] = []

    def plan(self, directives, composite_profile, caps):  # noqa: ANN001 - stub
        return []

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        query = item.native_query or {}
        tag = str(query.get("tag") or "?")
        page = int(query.get("page") or 1)
        type(self).fetched.append((tag, page))
        await self.egress.fetch(f"https://{tag}.load.test/jobs?page={page}")
        return [
            RawRecord(
                url=f"https://{tag}.load.test/jobs/{page}/{n}",
                content="{}",
                meta={
                    "title": f"Data Engineer {tag}-{page}-{n}",
                    "company_name_raw": f"Employer {tag}-{n % 7}",
                    "source_url": f"https://{tag}.load.test/jobs/{page}/{n}",
                    "location": "Brussels",
                    "country": "BE",
                    "description": "Collected by the load harness. " * 8,
                },
            )
            for n in range(RECORDS_PER_PAGE)
        ]

    def parse(self, raw: RawRecord) -> list[dict]:
        return [dict(raw.meta)]

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        return NormalisedRecord(entity_type="vacancy", data=dict(parsed))


# ---------------------------------------------------------------------------
# Isolation.  There is no conftest under tests/integration, so the guards the
# unit suite gets from one are set up here, in the module that needs them.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _installed_database_is_out_of_reach() -> Iterator[None]:
    """No test here may open ``data/dreamjob.db`` (plan item C5).

    This module registers a stub adapter in the process-wide registry, which is
    exactly how ``stub_board`` and ``broken_board`` were once catalogued as
    real sources and selected by every campaign.  Pointing the settings at a
    temporary file is enough right up until something resolves a path of its
    own, so the rule is enforced at the one door every database access goes
    through instead.
    """
    installed = (REPO_ROOT / "data").resolve()
    real_connect = sqlite3.connect

    def guarded(database, *args, **kwargs):  # noqa: ANN001, ANN202 - sqlite3's signature
        text = str(database)
        if text and text != ":memory:":
            resolved = None
            with contextlib.suppress(OSError, ValueError, RuntimeError):
                resolved = Path(text).resolve()
            if str(installed) in text or (resolved and installed in resolved.parents):
                raise RuntimeError(
                    f"An integration test tried to open the installed database ({database!r}). "
                    "This module runs against a temporary file; writing to data/ is how test "
                    "fixtures were once catalogued as real sources (C5)."
                )
        return real_connect(database, *args, **kwargs)

    sqlite3.connect = guarded
    try:
        yield
    finally:
        sqlite3.connect = real_connect


def _forget_connections() -> None:
    """Never hand a test a connection an earlier one opened on a reused path."""
    from dreamjob.db import connection as conn_module  # noqa: PLC0415

    for key in [k for k in vars(conn_module._local) if k.startswith("conn_")]:
        conn = getattr(conn_module._local, key, None)
        delattr(conn_module._local, key)
        with contextlib.suppress(Exception):
            conn.close()


@pytest.fixture(autouse=True)
def offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A throw-away database, the stub adapter registered, and no network."""
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "load.db"))
    monkeypatch.setenv("DREAMJOB_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("DREAMJOB_ENV", "development")
    monkeypatch.setenv(
        "DREAMJOB_MASTER_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    )
    monkeypatch.setenv("DREAMJOB_SESSION_SECRET", secrets.token_urlsafe(32))
    get_settings.cache_clear()
    _forget_connections()
    get_settings().ensure_dirs()
    migrate()

    async def _answer(self, url: str, **kwargs: object) -> FetchResult:  # noqa: ANN001
        return FetchResult(
            url=url, status_code=200, text="{}", content=b"{}", headers={},
            from_cache=False, raw_document_id=None, content_hash="",
        )

    monkeypatch.setattr(EgressClient, "fetch", _answer)

    _LoadBoard.fetched = []
    adapter_registry._REGISTRY[_LoadBoard.key] = _LoadBoard
    try:
        yield
    finally:
        # C5: the stub leaves the registry with the test, so nothing can later
        # write it into a catalogue as a real source.
        adapter_registry._REGISTRY.pop(_LoadBoard.key, None)
        runner._controls.clear()
        _forget_connections()
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# The server, the client and the campaign
# ---------------------------------------------------------------------------


class Server:
    """The real application, on a real socket, on its own thread.

    ``lifespan='off'`` because the fixture has already migrated, and because
    the lifespan would synchronise the adapter registry into the source
    catalogue - which would write this module's stub adapter into it.  The one
    piece of startup that matters to a test here, ``resume_orphans``, is called
    by that test itself, where it can be seen.
    """

    def __init__(self, app: object) -> None:
        config = uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="warning", lifespan="off", access_log=False
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, name="dreamjob-test-api",
                                        daemon=True)

    def start(self) -> str:
        self._thread.start()
        deadline = time.perf_counter() + 20
        while not self._server.started and time.perf_counter() < deadline:
            time.sleep(0.01)
        assert self._server.started, "the test API server did not come up"
        port = self._server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        # Ask every job still running to stop, and give it a moment to unwind
        # on its own loop.  A job runs on a pool thread now, so its task
        # belongs to another thread and setting the flag is the way in.
        for control in list(runner._controls.values()):
            control.cancelled = True
            control.paused = False
        deadline = time.perf_counter() + 15
        while runner._controls and time.perf_counter() < deadline:
            time.sleep(0.05)
        self._server.should_exit = True
        self._thread.join(timeout=20)
        assert not self._thread.is_alive(), "the test API server did not shut down"


@pytest.fixture()
def api(offline: None) -> Iterator[httpx.Client]:
    """An HTTP client for the running application, signed in as the owner."""
    from dreamjob.main import create_app  # noqa: PLC0415 - after the settings are pointed

    app = create_app()
    # An endpoint that does the defect on purpose, so the probe can be shown
    # failing before it is trusted (see test_the_probe_sees_the_api_go_dark).
    # ``async def`` is load-bearing: FastAPI runs a coroutine endpoint on the
    # serving loop itself, which is exactly where the job used to run.
    app.add_api_route("/api/_test/stall", _stall_the_loop, methods=["GET"])
    # ``create_app`` mounts a catch-all for the SPA, so a route added after it
    # would never be reached.
    app.router.routes.insert(0, app.router.routes.pop())
    server = Server(app)
    base_url = server.start()
    client = httpx.Client(base_url=base_url, timeout=120.0)
    try:
        registered = client.post(
            "/api/auth/register",
            json={"email": OWNER_EMAIL, "display_name": "Owner", "password": PASSWORD},
        )
        assert registered.status_code == 201, registered.text
        yield client
    finally:
        client.close()
        server.stop()


def owner_id() -> str:
    """The account the ``api`` fixture registered.  A campaign must belong to it.

    Every campaign route goes through ``_campaign_or_404``, which is the
    tenant-isolation boundary (FR-344): a campaign seeded under some other job
    seeker would 404 rather than launch, and the test would pass for the wrong
    reason.
    """
    row = query_one("SELECT id FROM job_seeker WHERE email = ?", (OWNER_EMAIL,))
    assert row is not None, "the api fixture must register the owner before a campaign is seeded"
    return str(row["id"])


def seed_campaign() -> tuple[str, dict[str, str]]:
    """A campaign of ``PLAN_ITEMS`` plan items, each on its own host.

    One host per plan item is the production shape, and it is the shape that
    matters here: items in different rate-limit buckets run at the same time
    (``collection._run_wave``), so the loop's ready queue holds one drain task
    per bucket and an arriving request waits behind all of them.
    """
    seeker = owner_id()
    directive = insert_row(
        "directive_set",
        {"job_seeker_id": seeker, "name": "Load", "created_at": utcnow(),
         "job_content": {"target_titles": ["Data Engineer"]},
         "location": {"countries": ["BE"]}},
    )
    profile = insert_row(
        "profile_version",
        {"job_seeker_id": seeker, "version": 1, "sections": {}, "created_at": utcnow()},
    )
    upsert_row(
        "source_catalogue",
        {
            "adapter_key": _LoadBoard.key,
            "display_name": _LoadBoard.display_name,
            "source_type": "job_board",
            "coverage_countries": ["BE"],
            "coverage_industries": [],
            "query_capabilities": _LoadBoard.capabilities.__dict__,
            "access_method": "api",
            "rate_limit_rps": 100.0,
            "cost_per_call_eur": 0.0,
            "tos_status": "permitted",
            "enabled": 1,
            "requires_ack": 0,
            "updated_at": utcnow(),
        },
        ["adapter_key"],
    )
    campaign = campaign_repo.create_campaign(
        seeker,
        {"name": "Load", "directive_set_id": directive, "profile_version_id": profile,
         "caps": {"max_pages": PLAN_ITEMS * PAGES_PER_ITEM + 10,
                  "max_pages_per_source": PAGES_PER_ITEM,
                  "max_seconds": 600}},
    )
    items: dict[str, str] = {}
    for n in range(PLAN_ITEMS):
        tag = f"board{n:02d}"
        items[tag] = campaign_repo.insert_plan_item(
            campaign,
            {"adapter_key": _LoadBoard.key,
             # ``url`` is what collection buckets on: one host per item.
             "native_query": {"q": "data engineer", "tag": tag,
                              "url": f"https://{tag}.load.test/jobs"},
             "estimated_pages": PAGES_PER_ITEM, "estimated_seconds": 4,
             "created_at": utcnow()},
        )
    return campaign, items


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------


class RequestProbe:
    """One browser, polling one endpoint, from outside the loop under test."""

    def __init__(self, base_url: str, path: str, cookies: httpx.Cookies,
                 interval: float = 0.05) -> None:
        self.path = path
        self.interval = interval
        self.latencies_ms: list[float] = []
        self.statuses: list[int] = []
        self.errors: list[str] = []
        self._client = httpx.Client(base_url=base_url, cookies=cookies, timeout=120.0)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._poll, name=f"probe{path}", daemon=True)

    def __enter__(self) -> RequestProbe:
        self._thread.start()
        time.sleep(0.1)
        self.latencies_ms.clear()
        self.statuses.clear()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=130)
        self._client.close()

    def _poll(self) -> None:
        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                response = self._client.get(self.path)
                self.statuses.append(response.status_code)
            except Exception as exc:  # noqa: BLE001 - a dark API is the failure under test
                self.errors.append(f"{type(exc).__name__}: {exc}")
            self.latencies_ms.append((time.perf_counter() - started) * 1000)
            self._stop.wait(self.interval)

    @property
    def p95_ms(self) -> float:
        if not self.latencies_ms:
            return float("inf")
        ordered = sorted(self.latencies_ms)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]

    @property
    def worst_ms(self) -> float:
        return max(self.latencies_ms, default=float("inf"))

    def report(self) -> str:
        if not self.latencies_ms:
            return f"{self.path}: no answer at all"
        ordered = sorted(self.latencies_ms)
        return (
            f"{self.path}: {len(ordered)} requests, p50 {ordered[len(ordered) // 2]:.0f} ms, "
            f"p95 {self.p95_ms:.0f} ms, worst {ordered[-1]:.0f} ms "
            f"(budget: p95 {P95_BUDGET_MS} ms, worst {WORST_REQUEST_MS} ms)"
        )


def until(predicate, timeout: float, *, poll: float = 0.05) -> float:
    """Seconds until ``predicate()`` is true; ``timeout`` if it never is."""
    started = time.perf_counter()
    while time.perf_counter() - started < timeout:
        if predicate():
            return time.perf_counter() - started
        time.sleep(poll)
    return timeout


def job_row(campaign_id: str) -> dict:
    return query_one(
        "SELECT * FROM job_run WHERE campaign_id = ? ORDER BY created_at DESC LIMIT 1",
        (campaign_id,),
    ) or {}


def vacancy_count() -> int:
    return int(query_one("SELECT COUNT(*) AS n FROM vacancy")["n"])


def checkpointed_pages(job_id: str) -> dict[str, int]:
    """The pages the job has recorded as finished (NFR-401), plan item to page.

    Empty until the first page settles, which is the only reliable signal that
    there is something for a restart to resume *from*.
    """
    row = query_one("SELECT checkpoint FROM job_run WHERE id = ?", (job_id,))
    stored = from_json(row["checkpoint"], {}) if row else {}
    return dict((stored or {}).get("completed") or {})


# ---------------------------------------------------------------------------
# 1. The instrument has teeth
# ---------------------------------------------------------------------------


async def _stall_the_loop(seconds: float = 2.0) -> dict:
    """Synchronous database writes on the serving loop: the defect, on demand.

    The same work collection does - ``insert_row`` into ``vacancy``, one
    transaction per record - run inline in a coroutine endpoint, so it holds
    the loop the way ``asyncio.create_task`` used to let a job hold it.
    """
    deadline = time.monotonic() + seconds
    rows = 0
    while time.monotonic() < deadline:
        insert_row(
            "vacancy",
            {"title": f"Blocking {rows}", "dedup_key": f"block-{rows}",
             "source_url": f"https://block.test/{rows}", "source_adapter": _LoadBoard.key,
             "collected_at": utcnow(), "confidence": 0.7},
        )
        rows += 1
    return {"rows": rows, "seconds": seconds}


def test_the_probe_sees_the_api_go_dark(api: httpx.Client) -> None:
    """The control.  A latency test that cannot fail is not evidence.

    The loop is blocked here on purpose, by an endpoint doing two seconds of
    the same synchronous database work collection does, and the probe has to
    notice.  If this ever passes without the deliberate stall, every assertion
    below it is worthless - and if it fails, the harness is measuring
    something other than the loop.
    """
    base_url = str(api.base_url)
    stalled: list[dict] = []

    def stall() -> None:
        with httpx.Client(base_url=base_url, cookies=api.cookies, timeout=60.0) as client:
            stalled.append(client.get("/api/_test/stall", params={"seconds": 2.0}).json())

    with RequestProbe(base_url, "/api/health", api.cookies) as probe:
        time.sleep(0.2)
        blocker = threading.Thread(target=stall, name="stall", daemon=True)
        blocker.start()
        blocker.join(timeout=60)

    assert stalled and stalled[0]["rows"] > 100, "the stall must be real work, not a sleep"
    assert probe.worst_ms > SLOW_REQUEST_MS, (
        "two seconds of synchronous database work on the event loop did not show up as a "
        f"slow request - the probe is not measuring anything.  {probe.report()}"
    )


# ---------------------------------------------------------------------------
# 2. The regression (NFR-102)
# ---------------------------------------------------------------------------


@pytest.mark.load
def test_the_api_answers_while_a_collection_job_runs(api: httpx.Client) -> None:
    """The eighteen-hour outage, at 1/168th scale, in about ten seconds.

    A campaign of ``PLAN_ITEMS`` sources collects ``EXPECTED_VACANCIES``
    postings through the real pipeline while a browser polls the three things a
    person actually looks at during a campaign: liveness, their own session -
    the ``session.token_hash`` lookup that was logged at 68,862 ms - and the
    campaign's own status screen.

    All three have to stay inside the latency the product already calls
    acceptable.
    """
    campaign, _items = seed_campaign()
    base_url = str(api.base_url)

    with RequestProbe(base_url, "/api/health", api.cookies) as health, \
            RequestProbe(base_url, "/api/auth/me", api.cookies) as me, \
            RequestProbe(base_url, f"/api/campaigns/{campaign}/status", api.cookies) as status:
        started = time.perf_counter()
        launched = api.post(f"/api/campaigns/{campaign}/launch")
        assert launched.status_code == 200, launched.text
        job_id = launched.json()["job_id"]

        until(
            lambda: job_row(campaign).get("status") in ("done", "failed", "cancelled"),
            timeout=300,
        )
        elapsed = time.perf_counter() - started

    row = job_row(campaign)
    assert row.get("id") == job_id
    assert row.get("status") == "done", f"the job did not finish cleanly: {row.get('last_error')}"

    # The load has to have been real, or the latency figures mean nothing.
    assert vacancy_count() == EXPECTED_VACANCIES, (
        f"the campaign wrote {vacancy_count()} vacancies, expected {EXPECTED_VACANCIES}"
    )
    assert elapsed > 2.0, f"the job was over in {elapsed:.1f}s - too little work to prove anything"

    for probe in (health, me, status):
        assert not probe.errors, f"{probe.path} failed while a job ran: {probe.errors[:3]}"
        assert set(probe.statuses) == {200}, (
            f"{probe.path} returned {sorted(set(probe.statuses))} while a job ran"
        )
        # Starvation shows twice over: in the latency, and in how few requests
        # got through at all.  Both are asserted, because a probe whose own
        # requests were blocked from starting would otherwise report a
        # flattering p95 off a handful of samples.
        assert len(probe.latencies_ms) > elapsed * 4, (
            f"{probe.path} completed only {len(probe.latencies_ms)} requests in {elapsed:.1f}s "
            "of polling every 50 ms - the API was not answering"
        )
        assert probe.p95_ms < P95_BUDGET_MS, (
            f"the API was too slow while a collection job ran (NFR-102).  {probe.report()}.  "
            "This is the defect that left the backend unreachable for eighteen hours; the "
            "same session query was logged at 68,862 ms."
        )
        assert probe.worst_ms < WORST_REQUEST_MS, (
            f"one request fell far outside the budget while a job ran.  {probe.report()}"
        )

    # ... and the polling did not stop the job either: 32 pages of 60 records
    # is ~2,000 synchronous writes, and it still finished.
    assert int(row.get("progress_done") or 0) >= PLAN_ITEMS * PAGES_PER_ITEM


# ---------------------------------------------------------------------------
# 3. Control under load (FR-185)
# ---------------------------------------------------------------------------


def test_cancel_lands_promptly_while_a_collection_job_is_running(api: httpx.Client) -> None:
    """FR-185: cancel takes effect within a bounded time, not eventually.

    Two things are timed, and both were broken during the incident: how long
    the *request* takes to be answered, and how long the job then takes to
    stop.  A cancel accepted in 40 ms and honoured ten minutes later is not a
    cancel; a cancel that cannot be submitted at all is what the user had.
    """
    campaign, _items = seed_campaign()
    launched = api.post(f"/api/campaigns/{campaign}/launch")
    assert launched.status_code == 200, launched.text

    until(lambda: vacancy_count() > 100, timeout=120)
    assert job_row(campaign)["status"] == "running"

    asked = time.perf_counter()
    cancelled = api.post(f"/api/campaigns/{campaign}/cancel")
    answered_ms = (time.perf_counter() - asked) * 1000
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["was_running"] is True

    landed = until(lambda: job_row(campaign)["status"] == "cancelled", timeout=60)

    assert answered_ms < CONTROL_BUDGET_MS, (
        f"POST /cancel took {answered_ms:.0f} ms to be answered while a job ran "
        f"(budget {CONTROL_BUDGET_MS} ms)"
    )
    assert job_row(campaign)["status"] == "cancelled", (
        f"the job was still not cancelled {landed:.1f}s after the request (FR-185)"
    )
    assert landed * 1000 < CONTROL_BUDGET_MS, (
        f"cancel took {landed * 1000:.0f} ms to take effect (budget {CONTROL_BUDGET_MS} ms)"
    )

    # NFR-401: an item that was in flight is rewound so it is runnable again,
    # rather than left reading 'running' on a job that is not running at all.
    statuses = {i["status"] for i in campaign_repo.list_plan_items(campaign)}
    assert "running" not in statuses, f"a cancelled campaign left items running: {statuses}"


def test_pause_stops_the_collection_and_resume_carries_it_on(api: httpx.Client) -> None:
    """FR-185: a pause that does not stop the writing is not a pause.

    ``pause()`` writes ``status='paused'`` itself, so the row proves nothing.
    What has to be true is that the campaign stops *collecting* - and then
    collects again on resume.
    """
    campaign, _items = seed_campaign()
    launched = api.post(f"/api/campaigns/{campaign}/launch")
    assert launched.status_code == 200, launched.text
    until(lambda: vacancy_count() > 60, timeout=120)

    asked = time.perf_counter()
    paused = api.post(f"/api/campaigns/{campaign}/pause")
    answered_ms = (time.perf_counter() - asked) * 1000
    assert paused.status_code == 200 and paused.json()["paused"] is True, paused.text
    assert answered_ms < CONTROL_BUDGET_MS, f"POST /pause took {answered_ms:.0f} ms to answer"

    # Measured, not assumed: the row count has to stop moving inside the budget.
    deadline = time.perf_counter() + CONTROL_BUDGET_MS / 1000.0
    at_rest = vacancy_count()
    while time.perf_counter() < deadline:
        time.sleep(0.1)
        current = vacancy_count()
        if current == at_rest:
            break
        at_rest = current
    else:
        pytest.fail(f"a paused campaign was still writing after {CONTROL_BUDGET_MS} ms (FR-185)")

    time.sleep(0.5)
    assert vacancy_count() == at_rest, (
        f"a paused campaign wrote {vacancy_count() - at_rest} more rows after coming to rest"
    )

    resumed = api.post(f"/api/campaigns/{campaign}/resume")
    assert resumed.status_code == 200, resumed.text
    assert until(lambda: vacancy_count() > at_rest, timeout=60) < 60, (
        "a resumed campaign never started collecting again"
    )

    api.post(f"/api/campaigns/{campaign}/cancel")
    until(lambda: job_row(campaign)["status"] == "cancelled", timeout=60)


# ---------------------------------------------------------------------------
# 4. A campaign survives a restart (NFR-401)
# ---------------------------------------------------------------------------


def test_a_campaign_resumes_across_a_restart_without_losing_or_repeating_work(
    api: httpx.Client,
) -> None:
    """NFR-401: the user's data survived the SIGKILL.  Prove it still does.

    The kill is modelled the way it happened - the task stops without the
    worker being told, so ``job_run`` is left saying ``running`` - and the
    restart the way ``main.lifespan`` does it: ``resume_orphans`` marks the
    interrupted job resumable, and then the user presses resume.

    "No work duplicated" is checked against the checkpoint rather than the row
    count, because the knowledge-base writer is idempotent: a re-collected page
    would cost a request and an extraction and then hide, and that waste is
    what turns a four-hour campaign into an overnight one.  A page the
    checkpoint recorded as complete must not be fetched again; the page each
    bucket had in flight may be, and ``jobs/runner.py`` says as much.
    """
    campaign, items = seed_campaign()
    launched = api.post(f"/api/campaigns/{campaign}/launch")
    assert launched.status_code == 200, launched.text
    job_id = launched.json()["job_id"]

    # Kill it once it has actually checkpointed a page, which is the state this
    # test is about.  A row count is the wrong proxy for that and was measured
    # to be: the buckets all fetch page one at the same time and then queue on
    # the single writer (CR-408), so a quarter of the vacancies exist at ~0.65s
    # while the first page settles at ~1.05s.  Waiting on the row count killed
    # the job before any checkpoint existed, and the assertion below then
    # failed for a reason that had nothing to do with resuming.
    until(lambda: checkpointed_pages(job_id), timeout=180)

    # SIGKILL: the task vanishes, nothing is told, the row still says 'running'.
    control = runner._controls[job_id]
    assert control.task is not None
    control.task.get_loop().call_soon_threadsafe(control.task.cancel)
    time.sleep(0.2)
    runner._controls.pop(job_id, None)
    assert job_row(campaign)["status"] == "running", (
        "a job killed with the process must look interrupted, not finished"
    )

    completed = checkpointed_pages(job_id)
    assert completed, "the job was killed before it checkpointed anything"
    before_kill = list(_LoadBoard.fetched)
    collected_before = vacancy_count()

    # Restart, exactly as the lifespan does it.
    assert asyncio.run(runner.resume_orphans()) >= 1
    assert job_row(campaign)["status"] == "pending"
    resumed = api.post(f"/api/campaigns/{campaign}/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["resumed"] is True

    until(lambda: job_row(campaign).get("status") in ("done", "failed"), timeout=300)
    row = job_row(campaign)
    assert row["status"] == "done", f"the resumed job did not finish: {row.get('last_error')}"

    # Nothing lost: the campaign collected everything it was planned for.
    assert vacancy_count() > collected_before
    assert vacancy_count() == EXPECTED_VACANCIES, (
        f"the resumed campaign ended with {vacancy_count()} vacancies of {EXPECTED_VACANCIES}; "
        "work was lost across the restart (NFR-401)"
    )

    # Nothing repeated but the page each bucket had in flight.
    after_kill = _LoadBoard.fetched[len(before_kill):]
    repeated = [
        (tag, page) for tag, page in after_kill if page <= completed.get(items[tag], 0)
    ]
    assert not repeated, (
        f"the resumed job re-collected pages its checkpoint already recorded as done: "
        f"{repeated}.  NFR-401 allows repeating the page that was in flight, no more."
    )
    twice = [pair for pair in after_kill if pair in before_kill]
    assert len(twice) <= PLAN_ITEMS, (
        f"{len(twice)} pages were collected twice; at most one per bucket may be"
    )

    # ... and the checkpoint carried the counters, so the interrupted run
    # reports the same totals an uninterrupted one would (FR-185, FR-186).
    assert int(row["progress_done"] or 0) >= PLAN_ITEMS * PAGES_PER_ITEM


# ---------------------------------------------------------------------------
# 5. The writer stays single, and the readers stay free (CR-408, NFR-102)
# ---------------------------------------------------------------------------


def test_reads_are_not_queued_behind_the_write_lock(api: httpx.Client) -> None:
    """The proof that the 68-second session lookup was never the database.

    A thread holds the process-wide write lock, and an open write transaction,
    for half a second - the worst thing a job's writes can do to a request -
    while ``/api/auth/me`` is called over HTTP.  WAL plus a single writer means
    a reader never waits for a writer, and ``read_tx`` does not take the lock
    at all.  So if the request is still fast, no amount of collection writing
    can account for 68,862 ms, and the missing-index diagnosis is dead: what
    was starving was the scheduler.
    """
    from dreamjob.db.connection import write_tx  # noqa: PLC0415 - the layer under test

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

    try:
        worst = 0.0
        for _ in range(20):
            started = time.perf_counter()
            response = api.get("/api/auth/me")
            worst = max(worst, (time.perf_counter() - started) * 1000)
            assert response.status_code == 200
    finally:
        release.set()
        thread.join(timeout=5)

    assert worst < SLOW_REQUEST_MS, (
        f"GET /api/auth/me took {worst:.0f} ms while one write transaction was held open. "
        "In the incident the session lookup alone took 68,862 ms - which one open write "
        "transaction cannot cause, and a starved event loop can."
    )
