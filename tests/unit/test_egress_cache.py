"""Nothing is fetched twice: the egress cache, ledger and Crawl-delay.

Each test here is a request that used to be issued and no longer is, or a rule
the product states and did not follow.  The measurements are from
``docs/Data_Gathering_Plan.md`` section 4 and items N2/N4/N5/N7.

* **Conditional GET.**  ``etag`` and ``last_modified`` were stored on every
  cached response and never sent, so an unchanged 3.6 MB Greenhouse board was
  re-downloaded in full the moment its 24 h TTL lapsed.  All three big ATS
  vendors answer a conditional GET with a 0-byte 304.
* **Negative caching.**  A 404 was re-probed on every pass - up to four RSS
  probes per company times 7,500 companies - and its body was written into the
  raw-document store, where 33 of 52 live rows were error pages (eight of them
  the same Cloudflare challenge differing only in its Ray ID).
* **Single flight.**  ``fetch_many([url] * 5)`` made five server hits.  Serial
  collection hid that; the domain-parallel collection the four-hour budget
  needs would have made it N-1 duplicate downloads for every shared page.
* **Cache keys.**  ``?x=1&y=2`` and ``?y=2&x=1`` were two entries and two
  downloads of one page.
* **Crawl-delay.**  europa.eu asks every client for 10 s and the limiter did
  not read it (FR-182, plan section 6 item 4).
* **The fetch ledger.**  Reuse was decided per adapter, so one Greenhouse board
  read on Monday marked all 3,400 of them fresh for a week.

Nothing here touches the network: the real :class:`EgressClient` runs on an
``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import execute, query_all, query_one, utcnow
from dreamjob.egress.client import (
    TRANSIENT_NEGATIVE_CACHE_TTL_SECONDS,
    DomainLimiter,
    EgressClient,
    RateLimited,
    fresh_targets,
    last_fetch,
    normalise_url,
    record_fetch,
    target_is_fresh,
)

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_SESSION_SECRET",
    "DREAMJOB_ENV",
    "DEEPSEEK_API_KEY",
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_SESSION_SECRET"] = secrets.token_urlsafe(32)
    os.environ["DREAMJOB_ENV"] = "development"
    os.environ["DEEPSEEK_API_KEY"] = ""
    get_settings.cache_clear()

    from dreamjob.db.migrator import migrate

    migrate()
    yield

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# A socket that counts what it was actually asked for
# ---------------------------------------------------------------------------

PAGE = b'{"jobs": [{"id": 1, "title": "Data Engineer"}]}'
ETAG = '"board-v1"'
LAST_MODIFIED = "Mon, 08 Sep 2026 09:00:00 GMT"


class Site:
    """A stubbed origin server that remembers every request it received."""

    def __init__(self, robots: str = "", **routes: httpx.Response):
        self.robots = robots
        self.routes = routes
        self.requests: list[httpx.Request] = []

    @property
    def page_requests(self) -> list[httpx.Request]:
        """Everything except robots.txt, which is its own conversation."""
        return [r for r in self.requests if r.url.path != "/robots.txt"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=self.robots)
        return self.respond(request)

    def respond(self, request: httpx.Request) -> httpx.Response:  # pragma: no cover - overridden
        raise NotImplementedError


class StaticSite(Site):
    """Always the same answer."""

    def __init__(self, response: httpx.Response, robots: str = ""):
        super().__init__(robots=robots)
        self.response = response

    def respond(self, request: httpx.Request) -> httpx.Response:
        return self.response


class ValidatingSite(Site):
    """A board that answers 304 when the client already has the current copy."""

    def __init__(self, robots: str = ""):
        super().__init__(robots=robots)
        self.conditional_requests = 0
        self.bodies_sent = 0

    def respond(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == ETAG:
            self.conditional_requests += 1
            return httpx.Response(304, headers={"etag": ETAG})
        self.bodies_sent += 1
        return httpx.Response(
            200,
            content=PAGE,
            headers={
                "content-type": "application/json",
                "etag": ETAG,
                "last-modified": LAST_MODIFIED,
            },
        )


def run(site: Site, body, **client_kwargs):
    """Run ``body(egress)`` on a real EgressClient whose socket is ``site``."""

    async def main():
        client = EgressClient(**client_kwargs)
        async with client:
            await client._client.aclose()  # noqa: SLF001 - swapping in the transport
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(site.handler),
                headers={"User-Agent": client.settings.user_agent},
            )
            client.limiter = DomainLimiter(1000.0)  # no pacing sleeps in a unit test
            return await body(client)

    return asyncio.run(main())


def expire_cache() -> None:
    """Move every cache entry past its TTL, as tomorrow's campaign would find it."""
    execute("UPDATE http_cache SET expires_at = ?", ("2000-01-01T00:00:00+00:00",))


# ---------------------------------------------------------------------------
# 1. Single flight (plan item N5)
# ---------------------------------------------------------------------------


def test_five_plan_items_wanting_one_url_cost_one_request() -> None:
    """``fetch_many([url] * 5)`` used to be five downloads of one page.

    It was invisible while collection was serial - the second call was an L1
    hit - and would have become N-1 duplicate fetches per shared page the day
    collection went parallel, which is what the four-hour budget requires.
    """
    site = StaticSite(httpx.Response(200, content=PAGE, headers={"content-type": "application/json"}))
    url = "https://boards.example/v1/boards/acme/jobs"

    results = run(site, lambda eg: eg.fetch_many([url] * 5))

    assert len(site.page_requests) == 1, "one URL, one request, however many callers asked"
    assert [r.content for r in results] == [PAGE] * 5
    assert all(r.status_code == 200 for r in results)


def test_concurrent_callers_of_equivalent_urls_are_coalesced_too() -> None:
    """The key is normalised *before* the fan-out, so spelling cannot split it."""
    site = StaticSite(httpx.Response(200, content=PAGE, headers={"content-type": "application/json"}))

    async def both(egress: EgressClient):
        return await asyncio.gather(
            egress.fetch("https://boards.example/jobs?b=2&a=1"),
            egress.fetch("https://boards.example/jobs?a=1&b=2"),
        )

    first, second = run(site, both)

    assert len(site.page_requests) == 1
    assert first.content == second.content == PAGE


# ---------------------------------------------------------------------------
# 2. Cache-key normalisation (plan section 4 L1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://api.example/jobs?x=1&y=2", "https://api.example/jobs?y=2&x=1"),
        ("https://API.Example/jobs", "https://api.example/jobs"),
        ("https://api.example", "https://api.example/"),
        ("https://api.example:443/jobs", "https://api.example/jobs"),
        ("https://api.example/jobs#anchor", "https://api.example/jobs"),
    ],
)
def test_equivalent_spellings_are_one_cache_key(left: str, right: str) -> None:
    assert normalise_url(left) == normalise_url(right)


def test_a_trailing_slash_inside_a_path_is_left_alone() -> None:
    """``/jobs`` and ``/jobs/`` are different resources to plenty of servers."""
    assert normalise_url("https://api.example/jobs") != normalise_url("https://api.example/jobs/")


def test_a_page_asked_for_under_two_spellings_is_downloaded_once() -> None:
    site = StaticSite(httpx.Response(200, content=PAGE, headers={"content-type": "application/json"}))

    async def twice(egress: EgressClient):
        first = await egress.fetch("https://Boards.Example/jobs?b=2&a=1")
        second = await egress.fetch("https://boards.example/jobs?a=1&b=2")
        return first, second

    first, second = run(site, twice)

    assert len(site.page_requests) == 1
    assert first.from_cache is False and second.from_cache is True
    assert second.content == PAGE
    assert len(query_all("SELECT url_hash FROM http_cache")) == 1


def test_two_representations_of_one_url_are_two_entries() -> None:
    """The NBB gateway serves a filing as JSON-XBRL or as PDF at one route.

    Keyed on the URL alone, the PDF fallback (FR-242) got the JSON body that the
    structured attempt had stored minutes earlier, failed its ``%PDF-`` check
    and returned no financial year at all - and coalescing would have made that
    concurrent as well as cached.
    """

    class GatewaySite(Site):
        def respond(self, request: httpx.Request) -> httpx.Response:
            if "pdf" in request.headers.get("accept", ""):
                return httpx.Response(200, content=b"%PDF-1.7 filing",
                                      headers={"content-type": "application/pdf"})
            return httpx.Response(200, content=b'{"facts": []}',
                                  headers={"content-type": "application/json"})

    site = GatewaySite()
    url = "https://ws.cbso.example/authentic/deposit/abc/accountingData"

    async def both(egress: EgressClient):
        structured = await egress.fetch(url, headers={"Accept": "application/x.jsonxbrl"})
        pdf = await egress.fetch(url, headers={"Accept": "application/pdf"})
        return structured, pdf

    structured, pdf = run(site, both)

    assert structured.content.startswith(b"{")
    assert pdf.content.startswith(b"%PDF-"), "the PDF caller must not get the JSON body"
    assert len(site.page_requests) == 2
    assert len(query_all("SELECT url_hash FROM http_cache")) == 2


# ---------------------------------------------------------------------------
# 3. Conditional GET and 304 (plan item N4)
# ---------------------------------------------------------------------------


def test_an_expired_entry_is_revalidated_and_the_body_is_kept() -> None:
    """A 304 must keep the stored body and extend the TTL, not re-download.

    The validators were stored from the first response and never sent, so every
    board came back in full the day after it was read.  Here the second fetch
    sends ``If-None-Match``, the board answers 304 with no body, and the caller
    still gets the 200 it would have got - from the file we already had.
    """
    site = ValidatingSite()
    url = "https://boards.example/v1/boards/gitlab/jobs?content=true"

    async def twice(egress: EgressClient):
        first = await egress.fetch(url)
        expire_cache()
        second = await egress.fetch(url)
        return first, second

    first, second = run(site, twice)

    assert site.bodies_sent == 1, "the body crossed the wire once"
    assert site.conditional_requests == 1
    assert len(site.page_requests) == 2, "revalidation is still a request (and a rate-limit slot)"

    sent = site.page_requests[1]
    assert sent.headers.get("if-none-match") == ETAG
    assert sent.headers.get("if-modified-since") == LAST_MODIFIED

    assert second.status_code == 200 and second.ok, "the caller sees the page, not the 304"
    assert second.content == PAGE
    assert second.from_cache is True and second.revalidated is True
    assert second.content_hash == first.content_hash
    assert second.raw_document_id == first.raw_document_id


def test_a_revalidated_entry_is_good_for_another_ttl() -> None:
    """Without extending ``expires_at`` the next call would revalidate again."""
    site = ValidatingSite()
    url = "https://boards.example/v1/boards/gitlab/jobs"

    async def three_times(egress: EgressClient):
        await egress.fetch(url)
        expire_cache()
        await egress.fetch(url)
        return await egress.fetch(url)

    third = run(site, three_times)

    assert len(site.page_requests) == 2, "the third call is a plain cache hit"
    assert third.from_cache is True and third.revalidated is False
    row = query_one("SELECT * FROM http_cache")
    assert row["expires_at"] > utcnow()
    assert row["etag"] == ETAG


def test_a_cache_entry_carries_the_hash_and_document_it_was_stored_with() -> None:
    """Migration 090: a hit no longer re-hashes a 3.6 MB body to find its id."""
    site = ValidatingSite()
    first = run(site, lambda eg: eg.fetch("https://boards.example/jobs"))
    row = query_one("SELECT * FROM http_cache")
    assert row["content_hash"] == first.content_hash
    assert row["raw_document_id"] == first.raw_document_id
    stored = query_one("SELECT * FROM raw_document WHERE id = ?", (first.raw_document_id,))
    assert stored and stored["content_hash"] == first.content_hash


def test_a_changed_board_is_re_downloaded() -> None:
    """The counterpart: revalidation must not serve a stale body forever."""

    class ChangingSite(Site):
        def __init__(self) -> None:
            super().__init__()
            self.version = 0

        def respond(self, request: httpx.Request) -> httpx.Response:
            self.version += 1
            return httpx.Response(
                200,
                content=f'{{"version": {self.version}}}'.encode(),
                headers={"content-type": "application/json", "etag": f'"v{self.version}"'},
            )

    site = ChangingSite()

    async def twice(egress: EgressClient):
        first = await egress.fetch("https://boards.example/jobs")
        expire_cache()
        return first, await egress.fetch("https://boards.example/jobs")

    first, second = run(site, twice)
    assert first.content == b'{"version": 1}'
    assert second.content == b'{"version": 2}'
    assert second.from_cache is False and second.revalidated is False


# ---------------------------------------------------------------------------
# 4. Negative caching, and non-200 bodies that are not documents (item N7)
# ---------------------------------------------------------------------------


def test_a_gone_page_is_not_probed_again_inside_the_window() -> None:
    """A 404 used to be re-probed on every pass, and stored as a document.

    Both halves are asserted here because both were invisible: the request was
    repeated, and the error page it returned was written into ``raw_document``
    as though it were retrieved material (FR-183).
    """
    site = StaticSite(httpx.Response(404, text="<html>not found</html>",
                                     headers={"content-type": "text/html"}))
    url = "https://acme.example/feed.rss"

    async def twice(egress: EgressClient):
        return await egress.fetch(url), await egress.fetch(url)

    first, second = run(site, twice)

    assert len(site.page_requests) == 1, "a 404 is answered from the negative cache"
    assert first.status_code == second.status_code == 404
    assert first.ok is False and second.ok is False, "the failure stays visible"
    assert second.from_cache is True and second.negative is True

    assert query_all("SELECT id FROM raw_document") == [], "an error page is not a document"
    assert first.raw_document_id is None

    row = query_one("SELECT * FROM http_cache WHERE status_code = 404")
    assert row is not None and row["body_path"] is None
    horizon = (datetime.now(UTC) + timedelta(days=6)).isoformat(timespec="seconds")
    assert row["expires_at"] > horizon, "404 and 410 are believed for a week"


def test_a_gone_page_is_probed_again_once_the_window_closes() -> None:
    """Negative caching is a delay, not a tombstone."""
    site = StaticSite(httpx.Response(410, text="gone"))
    url = "https://acme.example/jobs.xml"

    async def twice(egress: EgressClient):
        first = await egress.fetch(url)
        expire_cache()
        return first, await egress.fetch(url)

    first, second = run(site, twice)
    assert len(site.page_requests) == 2
    assert second.from_cache is False and second.status_code == 410


def test_a_domain_that_kept_saying_429_is_left_alone_for_six_hours() -> None:
    """Giving up on a domain is remembered, so the next item does not re-poke it.

    Recruitee answered 429 to 11 of 56 requests at concurrency 10; a vendor sees
    one client IP however many tenant subdomains it has, and a blocked IP loses
    every board of that vendor (plan section 6 item 5).
    """
    site = StaticSite(httpx.Response(429, text="slow down"))
    url = "https://acme.recruitee.com/api/offers/"

    async def twice(egress: EgressClient):
        outcomes = []
        for _ in range(2):
            try:
                outcomes.append(await egress.fetch(url, max_retries=1))
            except RateLimited as exc:
                outcomes.append(exc)
        return outcomes

    first, second = run(site, twice)

    assert isinstance(first, RateLimited) and isinstance(second, RateLimited)
    assert len(site.page_requests) == 1, "the second attempt never reached the wire"
    row = query_one("SELECT * FROM http_cache WHERE status_code = 429")
    assert row is not None
    ceiling = (
        datetime.now(UTC) + timedelta(seconds=TRANSIENT_NEGATIVE_CACHE_TTL_SECONDS + 60)
    ).isoformat(timespec="seconds")
    assert row["expires_at"] < ceiling, "a 429 is re-tried in hours, not in a week"


def test_a_transient_failure_keeps_the_body_it_was_revalidating() -> None:
    """A 500 during revalidation must not throw away a good body and its ETag.

    Negative-caching over the entry would cost a full re-download when the
    origin came back - and the ETag that would have made it a 0-byte 304 with
    it.  The failure is still handed to the caller, so a source that has
    genuinely stopped answering is still visible as a failure.
    """

    class FlakySite(Site):
        def __init__(self) -> None:
            super().__init__()
            self.mode = "ok"

        def respond(self, request: httpx.Request) -> httpx.Response:
            if self.mode == "broken":
                return httpx.Response(500, text="upstream error")
            if request.headers.get("if-none-match") == ETAG:
                return httpx.Response(304, headers={"etag": ETAG})
            return httpx.Response(200, content=PAGE, headers={
                "content-type": "application/json", "etag": ETAG,
            })

    site = FlakySite()
    url = "https://boards.example/jobs"

    async def outage(egress: EgressClient):
        await egress.fetch(url)
        expire_cache()
        site.mode = "broken"
        broken = await egress.fetch(url)
        row = query_one("SELECT * FROM http_cache")
        site.mode = "ok"
        recovered = await egress.fetch(url)
        return broken, row, recovered

    broken, row, recovered = run(site, outage)

    assert broken.status_code == 500 and broken.ok is False, "the outage stays visible"
    assert row["body_path"] and row["etag"] == ETAG, "the good copy survived the outage"
    assert recovered.content == PAGE
    assert recovered.revalidated is True, "recovery cost one 304, not a download"
    assert site.page_requests[-1].headers.get("if-none-match") == ETAG


def test_a_403_is_not_negative_cached() -> None:
    """Only "gone" and "come back later" are remembered; a refusal is not.

    A 403 can be a missing header or a login wall that the next item, with
    different credentials, may legitimately get past.
    """
    site = StaticSite(httpx.Response(403, text="forbidden"))
    url = "https://walled.example/jobs"

    async def twice(egress: EgressClient):
        return await egress.fetch(url), await egress.fetch(url)

    run(site, twice)
    assert len(site.page_requests) == 2
    assert query_one("SELECT * FROM http_cache WHERE status_code = 403") is None


# ---------------------------------------------------------------------------
# 5. Crawl-delay (FR-182, plan item N2)
# ---------------------------------------------------------------------------


def test_a_crawl_delay_in_robots_txt_paces_the_domain() -> None:
    """europa.eu asks every client for 10 s; the limiter used to read only the rules.

    Honouring it costs Campaign B 26 minutes and is the price of being the one
    client the EU's aggregator has no reason to block (plan section 6 item 4).
    """
    site = StaticSite(
        httpx.Response(200, content=PAGE, headers={"content-type": "application/json"}),
        robots="User-agent: *\nCrawl-delay: 10\nDisallow: /admin/\n",
    )

    async def once(egress: EgressClient):
        await egress.fetch("https://europa.example/eures/api/jv-search")
        return egress.limiter

    limiter = run(site, once)

    assert limiter.interval_for("europa.example") == 10.0
    assert limiter.interval_for("other.example") == limiter.min_interval


def test_the_slower_of_the_two_rates_wins() -> None:
    """Our rate is a ceiling, the site's delay is a floor; neither is ignored."""
    limiter = DomainLimiter(0.5)  # 2 s between requests
    limiter.set_crawl_delay("api.lever.co", 1)      # slower than us? no - we keep 2 s
    limiter.set_crawl_delay("europa.eu", 10)        # slower than us - honour it
    assert limiter.interval_for("api.lever.co") == 2.0
    assert limiter.interval_for("europa.eu") == 10.0
    assert limiter.interval_for("unknown.example") == 2.0


def test_the_crawl_delay_is_actually_waited_for() -> None:
    """Not just recorded: ``acquire`` must pace on it."""

    async def two_requests() -> float:
        limiter = DomainLimiter(1000.0)  # our own rate: 1 ms
        limiter.set_crawl_delay("slow.example", 0.25)
        await limiter.acquire("slow.example")
        started = asyncio.get_running_loop().time()
        await limiter.acquire("slow.example")
        return asyncio.get_running_loop().time() - started

    assert asyncio.run(two_requests()) >= 0.2


def test_a_malformed_crawl_delay_is_ignored_not_fatal() -> None:
    limiter = DomainLimiter(0.5)
    limiter.set_crawl_delay("api.example", "soon")
    limiter.set_crawl_delay("api.example", None)
    limiter.set_crawl_delay("api.example", -5)
    assert limiter.interval_for("api.example") == 2.0


# ---------------------------------------------------------------------------
# 6. The fetch ledger (FR-342, plan item N4)
# ---------------------------------------------------------------------------


def test_reading_one_board_does_not_mark_every_board_of_the_vendor_fresh() -> None:
    """The exact defect: reuse was decided per adapter, not per target.

    After any run wrote a hundred fresh ``ats.greenhouse`` rows, every
    Greenhouse plan item was skipped for seven days - including the 3,399
    boards nobody had ever read.  The ledger answers for one board.
    """
    record_fetch("ats.greenhouse", "collibra", "https://boards-api.greenhouse.io/v1/boards/collibra/jobs",
                 http_status=200, record_count=39, etag=ETAG)

    assert target_is_fresh("ats.greenhouse", "collibra", 7) is True
    assert target_is_fresh("ats.greenhouse", "gitlab", 7) is False, (
        "a board nobody has read is not fresh because a sibling board was read"
    )
    assert fresh_targets("ats.greenhouse", 7) == {"collibra"}


def test_a_target_falls_out_of_the_window() -> None:
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat(timespec="seconds")
    record_fetch("ats.lever", "acme", "https://api.lever.co/v0/postings/acme",
                 http_status=200, record_count=12, fetched_at=old)

    assert target_is_fresh("ats.lever", "acme", 7) is False
    assert target_is_fresh("ats.lever", "acme", 30) is True
    assert fresh_targets("ats.lever", 7) == set()


def test_a_dead_board_is_remembered_as_read() -> None:
    """A 404 board is worth a ledger row: "do not read this again this week"."""
    record_fetch("ats.recruitee", "ghost", "https://ghost.recruitee.com/api/offers/",
                 http_status=404, record_count=0)
    entry = last_fetch("ats.recruitee", "ghost")
    assert entry is not None
    assert entry.http_status == 404 and entry.record_count == 0
    assert target_is_fresh("ats.recruitee", "ghost", 7) is True


def test_re_reading_a_target_updates_its_row_rather_than_growing_history() -> None:
    url = "https://boards-api.greenhouse.io/v1/boards/collibra/jobs"
    record_fetch("ats.greenhouse", "collibra", url, http_status=200, record_count=39, etag='"v1"')
    record_fetch("ats.greenhouse", "collibra", url, http_status=304, record_count=39, etag='"v2"')

    assert len(query_all("SELECT * FROM fetch_ledger")) == 1
    entry = last_fetch("ats.greenhouse", "collibra")
    assert entry is not None and entry.http_status == 304 and entry.etag == '"v2"'


def test_a_paginated_target_keeps_a_row_per_page_and_one_answer() -> None:
    base = "https://europa.eu/eures/api/jv-search?page="
    for page in range(3):
        record_fetch("board.eures", "be1|nace-j", f"{base}{page}", http_status=200, record_count=50)

    assert len(query_all("SELECT * FROM fetch_ledger")) == 3
    assert fresh_targets("board.eures", 7) == {"be1|nace-j"}
    assert last_fetch("board.eures", "be1|nace-j") is not None


def test_the_ledger_is_scoped_to_its_adapter() -> None:
    record_fetch("ats.greenhouse", "acme", "https://a.example", http_status=200)
    assert target_is_fresh("ats.lever", "acme", 7) is False
    assert fresh_targets("ats.lever", 7) == set()
