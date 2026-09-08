"""Human pacing, the duration contract and the stop rule (FR-203, FR-204, NFR-502).

Three things live here, and they are deliberately in one module because they
are one promise: *the run behaves like a person, it tells you up front how
long it will take, and it stops the moment the site pushes back*.

* **Pacing** (FR-203) - a randomised delay between ``browser_min_delay_ms`` and
  ``browser_max_delay_ms`` before every action, and scroll simulation so a page
  loads its lazy content the way it does for a reader.
* **The duration estimator** (FR-204, NFR-502) - the announced duration is
  computed from the *same* distribution the executor samples from, plus the
  measured page-load time for that site, which is why it lands close to the
  truth; during execution it is refined from what the run has actually taken.
* **Challenge detection** (FR-203, RK-01) - :func:`detect_challenge` is a pure
  function over URL, status and DOM markers, so the rule that protects the
  user's account is testable rather than buried in the executor.

:class:`PacedRun` ties them together: it walks a fixed target list, applies the
pacing, refines the estimate, honours pause / skip / cancel (FR-206) and stops
dead on a challenge.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from dreamjob.config import get_settings
from dreamjob.db.repositories import browser as repo
from dreamjob.jobs.runner import JobContext
from dreamjob.security.audit import record_audit

log = logging.getLogger(__name__)


def now() -> float:
    """Monotonic clock.  Indirected so a run can be measured in virtual time."""
    return time.monotonic()


async def sleep(seconds: float) -> None:
    """The only place this slice waits.  Indirected for the same reason as :func:`now`."""
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# Pacing (FR-203)
# ---------------------------------------------------------------------------


@dataclass
class Pacing:
    """Randomised, human-like delays.  CR-406 says slow; this is where slow lives."""

    min_delay_ms: int
    max_delay_ms: int
    scroll_steps: int = 2
    rng: random.Random = field(default_factory=random.Random, repr=False, compare=False)

    @classmethod
    def from_settings(cls, *, scroll_steps: int = 2, rng: random.Random | None = None) -> Pacing:
        s = get_settings()
        return cls(
            min_delay_ms=int(s.browser_min_delay_ms),
            max_delay_ms=max(int(s.browser_max_delay_ms), int(s.browser_min_delay_ms)),
            scroll_steps=scroll_steps,
            rng=rng or random.Random(),
        )

    @property
    def waits_per_target(self) -> int:
        """One wait per scroll step plus one between targets - what the executor does."""
        return self.scroll_steps + 1

    @property
    def mean_delay_seconds(self) -> float:
        return (self.min_delay_ms + self.max_delay_ms) / 2000.0

    def sample_delay(self) -> float:
        return self.rng.uniform(self.min_delay_ms, self.max_delay_ms) / 1000.0

    async def wait(self) -> float:
        delay = self.sample_delay()
        await sleep(delay)
        return delay


async def human_scroll(session: Any, pacing: Pacing) -> int:
    """Scroll the page in steps, pausing between them (FR-203).

    Lazy sections of a profile or a search result page only render once they
    come into view, so this is extraction as much as it is camouflage.
    """
    scrolled = 0
    try:
        page = await session.page()
    except Exception as exc:  # noqa: BLE001 - a closed tab is handled by the caller
        log.debug("No page to scroll: %s", exc)
        return 0
    for _ in range(max(0, pacing.scroll_steps)):
        delta = int(pacing.rng.uniform(400, 900))
        try:
            mouse = getattr(page, "mouse", None)
            if mouse is not None and hasattr(mouse, "wheel"):
                await mouse.wheel(0, delta)
            else:  # pragma: no cover - older drivers
                await page.evaluate(f"window.scrollBy(0, {delta})")
            scrolled += delta
        except Exception as exc:  # noqa: BLE001
            log.debug("Scroll step failed: %s", exc)
        await pacing.wait()
    return scrolled


# ---------------------------------------------------------------------------
# Duration estimate (FR-204, NFR-502)
# ---------------------------------------------------------------------------


@dataclass
class DurationEstimate:
    """What the user is asked to confirm, and what they watch being refined."""

    targets: int
    seconds: float
    low_seconds: float
    high_seconds: float
    per_target_seconds: float
    page_load_seconds: float
    samples: int
    basis: str
    done: int = 0
    elapsed_seconds: float = 0.0

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.seconds - self.elapsed_seconds)

    def human(self) -> str:
        seconds = self.seconds
        if seconds < 90:
            return f"about {int(round(seconds))} seconds"
        minutes = seconds / 60
        if minutes < 90:
            return f"about {int(round(minutes))} minutes"
        return f"about {minutes / 60:.1f} hours"

    def as_dict(self) -> dict[str, Any]:
        return {
            "targets": self.targets,
            "seconds": round(self.seconds, 1),
            "low_seconds": round(self.low_seconds, 1),
            "high_seconds": round(self.high_seconds, 1),
            "per_target_seconds": round(self.per_target_seconds, 2),
            "page_load_seconds": round(self.page_load_seconds, 2),
            "samples": self.samples,
            "basis": self.basis,
            "done": self.done,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "remaining_seconds": round(self.remaining_seconds, 1),
            "human": self.human(),
        }


class DurationEstimator:
    """Announce a duration, then keep it honest (FR-204).

    The initial figure is ``targets x (waits x mean delay + page load)``: the
    executor draws its delays from the very distribution this mean comes from,
    and the page-load term is the average measured on this machine for this
    site, so the announced figure lands inside the +/-25% the acceptance
    criterion asks for.  Once the run starts, measured targets replace the
    model one by one.
    """

    def __init__(
        self,
        pacing: Pacing,
        *,
        site: str = "",
        page_load_seconds: float | None = None,
        tolerance: float = 0.25,
        overhead_seconds: float = 0.6,
    ):
        self.pacing = pacing
        self.site = site
        self.tolerance = tolerance
        self.overhead_seconds = overhead_seconds
        self.page_load = (
            float(page_load_seconds)
            if page_load_seconds is not None
            else self._measured_page_load(site)
        )
        self.samples: list[float] = []

    @staticmethod
    def _measured_page_load(site: str) -> float:
        try:
            return repo.page_load_seconds(site or "default")
        except Exception:  # noqa: BLE001 - an unmigrated database must not block an estimate
            log.debug("No stored page-load sample for %s", site)
            return repo.DEFAULT_PAGE_LOAD_SECONDS

    # -- the model ----------------------------------------------------------
    @property
    def modelled_per_target(self) -> float:
        return (
            self.pacing.waits_per_target * self.pacing.mean_delay_seconds
            + self.page_load
            + self.overhead_seconds
        )

    @property
    def per_target(self) -> float:
        """Measured average once there is one, the model until then."""
        if self.samples:
            return sum(self.samples) / len(self.samples)
        return self.modelled_per_target

    def estimate(self, targets: int) -> DurationEstimate:
        per_target = self.per_target
        total = targets * per_target
        return DurationEstimate(
            targets=targets,
            seconds=total,
            low_seconds=total * (1 - self.tolerance),
            high_seconds=total * (1 + self.tolerance),
            per_target_seconds=per_target,
            page_load_seconds=self.page_load,
            samples=len(self.samples),
            basis="measured" if self.samples else "pacing model",
        )

    # -- refinement (FR-204) ------------------------------------------------
    def observe(self, target_seconds: float, page_load_seconds: float | None = None) -> None:
        """Record one completed target; page loads also update the stored average."""
        if target_seconds > 0:
            self.samples.append(target_seconds)
        if page_load_seconds and page_load_seconds > 0:
            self.page_load = (
                0.3 * page_load_seconds + 0.7 * self.page_load
                if self.page_load
                else page_load_seconds
            )
            try:
                repo.record_page_load(self.site or "default", page_load_seconds)
            except Exception:  # noqa: BLE001 - a sample is not worth failing a run for
                log.debug("Could not store the page-load sample")

    def refine(self, *, targets: int, done: int, elapsed: float) -> DurationEstimate:
        """The estimate the UI shows while the run is going (FR-204, NFR-502)."""
        remaining = max(0, targets - done)
        per_target = self.per_target
        total = elapsed + remaining * per_target
        estimate = DurationEstimate(
            targets=targets,
            seconds=total,
            low_seconds=elapsed + remaining * per_target * (1 - self.tolerance),
            high_seconds=elapsed + remaining * per_target * (1 + self.tolerance),
            per_target_seconds=per_target,
            page_load_seconds=self.page_load,
            samples=len(self.samples),
            basis="measured" if self.samples else "pacing model",
        )
        estimate.done = done
        estimate.elapsed_seconds = elapsed
        return estimate


# ---------------------------------------------------------------------------
# Challenge, captcha and rate-limit detection (FR-203, RK-01)
# ---------------------------------------------------------------------------

#: URL shapes that mean "the site is no longer serving you content".
CHALLENGE_URL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("captcha", re.compile(r"captcha", re.IGNORECASE)),
    (
        "challenge",
        re.compile(
            r"/checkpoint/|/challenge|/authwall|/uas/login|/security/verify|/sorry/|/blocked",
            re.IGNORECASE,
        ),
    ),
    ("rate_limit", re.compile(r"too[-_]?many[-_]?requests|rate[-_]?limit", re.IGNORECASE)),
)

#: DOM and copy markers, lower-cased, in the order they are tested.
CHALLENGE_DOM_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "captcha",
        (
            "g-recaptcha", "h-captcha", "hcaptcha", "recaptcha", "cf-turnstile",
            "px-captcha", 'id="captcha"', "captcha-internal",
        ),
    ),
    (
        "challenge",
        (
            "challenge-dialog", "checkpoint/challenge", "cf-browser-verification",
            "verify you are a human", "verify you're a human", "confirm your identity",
            "unusual activity", "security verification", "let's do a quick security check",
        ),
    ),
    (
        "rate_limit",
        (
            "too many requests", "rate limit", "you have exceeded", "commercial use limit",
            "we've restricted", "we have restricted", "try again later",
        ),
    ),
)

#: HTTP statuses that are a stop signal on their own.  999 is LinkedIn's.
CHALLENGE_STATUSES: dict[int, str] = {429: "rate_limit", 403: "challenge", 999: "challenge"}


@dataclass(frozen=True)
class ChallengeVerdict:
    detected: bool
    kind: str | None = None
    evidence: str | None = None
    url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "detected": self.detected,
            "kind": self.kind,
            "evidence": self.evidence,
            "url": self.url,
        }


class ChallengeDetected(RuntimeError):
    """Raised the instant a challenge, captcha or rate-limit page appears (FR-203)."""

    def __init__(self, verdict: ChallengeVerdict):
        self.verdict = verdict
        super().__init__(
            f"{verdict.kind} page detected at {verdict.url} ({verdict.evidence}); stopping"
        )


def detect_challenge(
    url: str, html: str = "", *, status: int | None = None, title: str = ""
) -> ChallengeVerdict:
    """Decide whether the site has stopped serving content (FR-203).

    Pure, ordered and documented so it can be regression-tested against saved
    pages: HTTP status first (it is unambiguous), then the URL the browser
    ended on, then markers in the DOM and in the visible copy.
    """
    if status is not None and status in CHALLENGE_STATUSES:
        return ChallengeVerdict(True, CHALLENGE_STATUSES[status], f"HTTP {status}", url)

    for kind, pattern in CHALLENGE_URL_PATTERNS:
        if pattern.search(url or ""):
            return ChallengeVerdict(True, kind, f"url matches {pattern.pattern}", url)

    haystack = f"{title}\n{html}".lower()
    if haystack.strip():
        for kind, markers in CHALLENGE_DOM_MARKERS:
            for marker in markers:
                if marker in haystack:
                    return ChallengeVerdict(True, kind, f"page contains {marker!r}", url)
    return ChallengeVerdict(False, url=url)


# ---------------------------------------------------------------------------
# Watch, pause, skip (FR-206)
# ---------------------------------------------------------------------------


@dataclass
class RunControl:
    """Per-run control surface.  Pause and cancel live in ``jobs.runner``; skip lives here."""

    job_id: str
    site: str = ""
    skipped: set[str] = field(default_factory=set)
    current_url: str | None = None
    stopped_reason: str | None = None

    def skip(self, url: str) -> bool:
        if not url:
            return False
        self.skipped.add(url)
        return True

    def is_skipped(self, url: str) -> bool:
        return url in self.skipped


_RUNS: dict[str, RunControl] = {}


def register_run(job_id: str, site: str = "") -> RunControl:
    control = RunControl(job_id=job_id, site=site)
    _RUNS[job_id] = control
    return control


def control_for(job_id: str) -> RunControl | None:
    return _RUNS.get(job_id)


def request_skip(job_id: str, url: str) -> bool:
    """Ask the running job to leave one target alone (FR-206)."""
    control = _RUNS.get(job_id)
    return bool(control and control.skip(url))


def release_run(job_id: str) -> None:
    _RUNS.pop(job_id, None)


# ---------------------------------------------------------------------------
# The paced executor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """One allowed page.  The executor never derives a new one from a page it read."""

    url: str
    kind: str
    label: str = ""
    plan_item_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind,
            "label": self.label,
            "plan_item_id": self.plan_item_id,
            "meta": self.meta,
        }


@dataclass
class Visit:
    """What a handler gets: the page as read, already stored as a raw document."""

    target: Target
    url: str
    html: str
    status: int | None
    title: str
    raw_document_id: str | None
    seconds: float


@dataclass
class TargetOutcome:
    url: str
    kind: str
    state: str            # done | skipped | failed | blocked
    records: int = 0
    seconds: float = 0.0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind,
            "state": self.state,
            "records": self.records,
            "seconds": round(self.seconds, 2),
            "error": self.error,
        }


@dataclass
class RunReport:
    site: str
    job_id: str | None
    total: int
    outcomes: list[TargetOutcome] = field(default_factory=list)
    records: int = 0
    elapsed_seconds: float = 0.0
    stopped_reason: str | None = None
    challenge: ChallengeVerdict | None = None
    estimate: DurationEstimate | None = None

    @property
    def done(self) -> int:
        return sum(1 for o in self.outcomes if o.state == "done")

    @property
    def skipped(self) -> int:
        return sum(1 for o in self.outcomes if o.state == "skipped")

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if o.state == "failed")

    def as_dict(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "job_id": self.job_id,
            "total": self.total,
            "done": self.done,
            "skipped": self.skipped,
            "failed": self.failed,
            "records": self.records,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "stopped_reason": self.stopped_reason,
            "challenge": self.challenge.as_dict() if self.challenge else None,
            "estimate": self.estimate.as_dict() if self.estimate else None,
            "outcomes": [o.as_dict() for o in self.outcomes],
        }


Handler = Callable[[Visit], Awaitable[int]]


class PacedRun:
    """Walk a fixed target list at human pace, under the user's control.

    The list is fixed before the run starts (FR-205): a handler may read what a
    page contains, but it can never add a target, so the run cannot turn into a
    crawl.  Every target goes through pause/cancel (FR-206) and through the
    challenge detector (FR-203) before any extraction happens.
    """

    def __init__(
        self,
        *,
        site: str,
        session: Any,
        pacing: Pacing,
        estimator: DurationEstimator,
        ctx: JobContext | None = None,
        control: RunControl | None = None,
        job_seeker_id: str | None = None,
        capture: bool = True,
        screenshots: bool = False,
        done_offset: int = 0,
    ):
        self.site = site
        self.session = session
        self.pacing = pacing
        self.estimator = estimator
        self.ctx = ctx
        self.control = control
        self.job_seeker_id = job_seeker_id
        self.capture = capture
        self.screenshots = screenshots
        # NFR-401: a resumed run walks the tail of the list, but progress and
        # the checkpoint must stay absolute or the next resume would re-open
        # pages this run already visited (RK-01).
        self.done_offset = max(0, int(done_offset))

    async def execute(self, targets: Sequence[Target], handler: Handler) -> RunReport:
        from dreamjob.browser import session as session_mod

        report = RunReport(
            site=self.site,
            job_id=self.ctx.job_id if self.ctx else None,
            total=len(targets),
            estimate=self.estimator.estimate(len(targets)),
        )
        run_started = now()
        if self.ctx:
            self.ctx.progress(self.done_offset, self.done_offset + len(targets))

        for index, target in enumerate(targets):
            if self.ctx:
                await self.ctx.checkpoint_barrier()  # FR-206 pause / cancel
            if self.control and self.control.is_skipped(target.url):
                report.outcomes.append(TargetOutcome(target.url, target.kind, "skipped"))
                self._checkpoint(report, index + 1, now() - run_started)
                continue
            if self.control:
                self.control.current_url = target.url

            started = now()
            try:
                load = await self.session.goto(target.url)
                html = await self.session.content()
            except Exception as exc:  # noqa: BLE001 - one bad page must not end the run
                log.warning("Could not open %s: %s", session_mod.safe_url(target.url), exc)
                report.outcomes.append(
                    TargetOutcome(target.url, target.kind, "failed", error=str(exc)[:500])
                )
                if self.ctx:
                    self.ctx.record_error(str(exc))
                await self.pacing.wait()
                self._checkpoint(report, index + 1, now() - run_started)
                continue

            verdict = detect_challenge(
                load.url, html, status=load.status, title=getattr(load, "title", "")
            )
            if verdict.detected:
                report.outcomes.append(TargetOutcome(target.url, target.kind, "blocked"))
                report.stopped_reason = f"stopped on a {verdict.kind} page"
                report.challenge = verdict
                await self._on_challenge(verdict)
                break

            await human_scroll(self.session, self.pacing)
            try:
                html = await self.session.content()
            except Exception as exc:  # noqa: BLE001
                log.debug("Could not re-read the page after scrolling: %s", exc)

            raw_document_id = None
            if self.capture:
                raw_document_id = repo.store_capture(
                    session_mod.safe_url(load.url),
                    session_mod.sanitise_capture(html),
                    status=load.status,
                )
                if self.screenshots:
                    shot = await self.session.screenshot()
                    if shot:
                        repo.store_screenshot(session_mod.safe_url(load.url), shot)

            visit = Visit(
                target=target,
                url=load.url,
                html=html,
                status=load.status,
                title=getattr(load, "title", ""),
                raw_document_id=raw_document_id,
                seconds=load.seconds,
            )
            try:
                written = int(await handler(visit) or 0)
                state, error = "done", None
            except Exception as exc:  # noqa: BLE001 - extraction failure is not a stop
                log.exception("Handling %s failed", session_mod.safe_url(target.url))
                written, state, error = 0, "failed", str(exc)[:500]
                if self.ctx:
                    self.ctx.record_error(str(exc))

            await self.pacing.wait()
            seconds = now() - started
            self.estimator.observe(seconds, page_load_seconds=load.seconds)
            report.records += written
            report.outcomes.append(
                TargetOutcome(target.url, target.kind, state, written, seconds, error)
            )
            self._checkpoint(report, index + 1, now() - run_started)

        report.elapsed_seconds = now() - run_started
        report.estimate = self.estimator.refine(
            targets=len(targets),
            done=len(report.outcomes),
            elapsed=report.elapsed_seconds,
        )
        if self.control and self.control.stopped_reason is None:
            self.control.stopped_reason = report.stopped_reason
        return report

    # -- progress and stop --------------------------------------------------
    def _checkpoint(self, report: RunReport, done: int, elapsed: float) -> None:
        """NFR-401/NFR-502: progress, refined estimate and resume state after each target."""
        report.estimate = self.estimator.refine(
            targets=report.total, done=done, elapsed=elapsed
        )
        if not self.ctx:
            return
        absolute = self.done_offset + done
        self.ctx.progress(absolute, self.done_offset + report.total)
        self.ctx.save_checkpoint(
            site=self.site,
            done=absolute,
            records=report.records,
            estimate=report.estimate.as_dict(),
            outcomes=[o.as_dict() for o in report.outcomes],
        )

    async def _on_challenge(self, verdict: ChallengeVerdict) -> None:
        """Stop immediately and tell the user why (FR-203, RK-01)."""
        from dreamjob.browser import session as session_mod

        log.warning("Challenge detected on %s: %s", self.site, verdict.evidence)
        if self.screenshots:
            shot = await self.session.screenshot()
            if shot:
                repo.store_screenshot(session_mod.safe_url(verdict.url), shot)
        if self.job_seeker_id:
            repo.notify(
                self.job_seeker_id,
                "browser_challenge",
                f"{self.site.title()} showed a {verdict.kind} page - the run stopped",
                (
                    "The automation stopped at the first sign of a challenge, as it must "
                    "(FR-203). Open the automation window, complete whatever the site asks "
                    "for, and start the run again later. Repeated challenges are the signal "
                    "to reduce the target list."
                ),
                {**verdict.as_dict(), "site": self.site},
            )
        record_audit(
            "browser.challenge_detected",
            "job_run",
            self.ctx.job_id if self.ctx else None,
            seeker_id=self.job_seeker_id,
            detail={**verdict.as_dict(), "site": self.site},
            actor="system",
        )
        if self.ctx:
            repo.set_job_error(
                self.ctx.job_id, f"stopped on a {verdict.kind} page: {verdict.evidence}"
            )
