"""The single egress layer for all outbound HTTP (IR-102, FR-182, FR-183).

Nothing in Dream Job calls ``httpx`` directly except this module and the mail
backends.  Routing every request through one place is what makes these
requirements testable:

* **FR-182** - robots.txt compliance, per-domain rate limiting with back-off,
  one consistent user agent, and a response cache with a configurable TTL so
  repeat campaigns do not re-fetch unchanged pages.
* **FR-183** - every fetched page is stored raw on disk with its URL,
  timestamp and content hash, and registered in ``raw_document`` so that
  extractors can be re-run later (DR-102).
* **IR-102** - retries, logging and metrics live here rather than in each of
  the twenty-odd source adapters.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx

from dreamjob.config import get_settings
from dreamjob.db.connection import (
    insert_row,
    query_one,
    upsert_row,
    utcnow,
)

log = logging.getLogger(__name__)


class RobotsDisallowed(RuntimeError):
    """Raised when robots.txt forbids the fetch (FR-182, CR-402)."""


class RateLimited(RuntimeError):
    """Raised after repeated 429/503 responses from a domain."""


@dataclass
class FetchResult:
    url: str
    status_code: int
    text: str
    content: bytes
    headers: dict
    from_cache: bool
    raw_document_id: str | None
    content_hash: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class DomainLimiter:
    """Per-domain token pacing with exponential back-off on 429/503."""

    def __init__(self, rps: float):
        self.min_interval = 1.0 / rps if rps > 0 else 0.0
        self._last: dict[str, float] = {}
        self._penalty: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, domain: str) -> asyncio.Lock:
        if domain not in self._locks:
            self._locks[domain] = asyncio.Lock()
        return self._locks[domain]

    async def acquire(self, domain: str) -> None:
        async with self._lock(domain):
            wait = self.min_interval + self._penalty.get(domain, 0.0)
            elapsed = time.monotonic() - self._last.get(domain, 0.0)
            if elapsed < wait:
                await asyncio.sleep(wait - elapsed)
            self._last[domain] = time.monotonic()

    def penalise(self, domain: str) -> float:
        current = self._penalty.get(domain, 0.0)
        self._penalty[domain] = min(60.0, max(2.0, current * 2))
        return self._penalty[domain]

    def relieve(self, domain: str) -> None:
        if domain in self._penalty:
            self._penalty[domain] = max(0.0, self._penalty[domain] / 2)


class EgressClient:
    """Async HTTP client with caching, robots.txt and raw-document capture."""

    def __init__(
        self,
        *,
        max_concurrency: int | None = None,
        respect_robots: bool | None = None,
        cache_ttl: int | None = None,
        store_raw: bool = True,
    ):
        s = get_settings()
        self.settings = s
        self.store_raw = store_raw
        self.respect_robots = s.respect_robots if respect_robots is None else respect_robots
        self.cache_ttl = s.http_cache_ttl_seconds if cache_ttl is None else cache_ttl
        self.limiter = DomainLimiter(s.per_domain_rps)
        self._sem = asyncio.Semaphore(max_concurrency or s.http_max_concurrency)
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._client: httpx.AsyncClient | None = None
        self.stats = {"fetched": 0, "cached": 0, "errors": 0, "blocked": 0}

    async def __aenter__(self) -> EgressClient:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept-Language": "en,nl;q=0.8,fr;q=0.7",
            },
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # -- robots.txt (FR-182, CR-402) ---------------------------------------
    async def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parsed = urlparse(url)
        domain = parsed.netloc
        if domain in self._robots:
            return self._robots[domain]

        cached = query_one("SELECT body, expires_at FROM robots_cache WHERE domain = ?", (domain,))
        body: str | None = None
        if cached and cached["expires_at"] > utcnow():
            body = cached["body"]
        else:
            try:
                assert self._client is not None
                resp = await self._client.get(f"{parsed.scheme}://{domain}/robots.txt", timeout=10.0)
                body = resp.text if resp.status_code == 200 else ""
            except Exception:  # noqa: BLE001 - unreachable robots.txt means "no rules"
                body = ""
            upsert_row(
                "robots_cache",
                {
                    "domain": domain,
                    "body": body,
                    "fetched_at": utcnow(),
                    "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(
                        timespec="seconds"
                    ),
                },
                ["domain"],
            )

        rp = urllib.robotparser.RobotFileParser()
        rp.parse((body or "").splitlines())
        self._robots[domain] = rp
        return rp

    async def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = await self._robots_for(url)
        if rp is None:
            return True
        return rp.can_fetch(self.settings.user_agent, url)

    # -- cache (FR-182) -----------------------------------------------------
    @staticmethod
    def _url_hash(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()

    def _cache_lookup(self, url: str) -> FetchResult | None:
        row = query_one("SELECT * FROM http_cache WHERE url_hash = ?", (self._url_hash(url),))
        if not row or row["expires_at"] <= utcnow():
            return None
        body_path = Path(row["body_path"])
        if not body_path.exists():
            return None
        content = body_path.read_bytes()
        self.stats["cached"] += 1
        return FetchResult(
            url=url,
            status_code=int(row["status_code"] or 200),
            text=content.decode("utf-8", errors="replace"),
            content=content,
            headers={},
            from_cache=True,
            raw_document_id=None,
            content_hash=hashlib.sha256(content).hexdigest(),
        )

    # -- raw document store (FR-183, DR-102) --------------------------------
    def _store_raw(
        self, url: str, content: bytes, content_type: str, status: int, access_method: str
    ) -> tuple[str | None, str]:
        content_hash = hashlib.sha256(content).hexdigest()
        if not self.store_raw:
            return None, content_hash

        existing = query_one("SELECT id FROM raw_document WHERE content_hash = ?", (content_hash,))
        if existing:
            return existing["id"], content_hash

        shard = self.settings.raw_dir / content_hash[:2] / content_hash[2:4]
        shard.mkdir(parents=True, exist_ok=True)
        ext = ".pdf" if "pdf" in content_type else ".html" if "html" in content_type else ".bin"
        path = shard / f"{content_hash}{ext}"
        path.write_bytes(content)

        doc_id = insert_row(
            "raw_document",
            {
                "url": url,
                "content_type": content_type,
                "content_hash": content_hash,
                "storage_path": str(path.relative_to(self.settings.abs_data_dir)),
                "byte_size": len(content),
                "http_status": status,
                "access_method": access_method,
                "fetched_at": utcnow(),
            },
        )
        return doc_id, content_hash

    # -- fetch --------------------------------------------------------------
    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        use_cache: bool = True,
        access_method: str = "http",
        max_retries: int = 3,
        **kwargs: object,
    ) -> FetchResult:
        if use_cache and method == "GET":
            hit = self._cache_lookup(url)
            if hit:
                return hit

        if not await self.allowed(url):
            self.stats["blocked"] += 1
            raise RobotsDisallowed(f"robots.txt disallows {url}")

        domain = urlparse(url).netloc
        assert self._client is not None, "EgressClient must be used as an async context manager"

        async with self._sem:
            for attempt in range(max_retries):
                await self.limiter.acquire(domain)
                try:
                    resp = await self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
                except httpx.HTTPError as exc:
                    if attempt == max_retries - 1:
                        self.stats["errors"] += 1
                        raise
                    log.debug("Retrying %s after %s", url, exc)
                    await asyncio.sleep(2 ** attempt)
                    continue

                if resp.status_code in (429, 503):
                    delay = self.limiter.penalise(domain)
                    log.info("Rate-limited by %s; backing off %.1fs", domain, delay)
                    if attempt == max_retries - 1:
                        raise RateLimited(f"{domain} returned {resp.status_code} repeatedly")
                    await asyncio.sleep(delay)
                    continue

                self.limiter.relieve(domain)
                self.stats["fetched"] += 1
                content = resp.content
                ctype = resp.headers.get("content-type", "")
                doc_id, chash = self._store_raw(
                    url, content, ctype, resp.status_code, access_method
                )

                if use_cache and method == "GET" and resp.status_code == 200 and doc_id:
                    doc = query_one("SELECT storage_path FROM raw_document WHERE id = ?", (doc_id,))
                    if doc:
                        upsert_row(
                            "http_cache",
                            {
                                "url_hash": self._url_hash(url),
                                "url": url,
                                "status_code": resp.status_code,
                                "headers": dict(resp.headers),
                                "body_path": str(
                                    self.settings.abs_data_dir / doc["storage_path"]
                                ),
                                "etag": resp.headers.get("etag"),
                                "last_modified": resp.headers.get("last-modified"),
                                "fetched_at": utcnow(),
                                "expires_at": (
                                    datetime.now(UTC) + timedelta(seconds=self.cache_ttl)
                                ).isoformat(timespec="seconds"),
                            },
                            ["url_hash"],
                        )

                return FetchResult(
                    url=str(resp.url),
                    status_code=resp.status_code,
                    text=resp.text,
                    content=content,
                    headers=dict(resp.headers),
                    from_cache=False,
                    raw_document_id=doc_id,
                    content_hash=chash,
                )

        raise RateLimited(f"Gave up fetching {url} after {max_retries} attempts")

    async def fetch_json(self, url: str, **kwargs: object) -> object:
        result = await self.fetch(url, **kwargs)
        import json

        return json.loads(result.text)

    async def fetch_many(self, urls: list[str], **kwargs: object) -> list[FetchResult | Exception]:
        """Fetch concurrently; failures come back as exceptions, not raises."""
        tasks = [self.fetch(u, **kwargs) for u in urls]
        return await asyncio.gather(*tasks, return_exceptions=True)  # type: ignore[return-value]
