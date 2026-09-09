"""End-to-end harness: the tooling every e2e suite drives the app with.

The suites in this directory are meant to read as a *narrative* — "sign in",
"open the campaign", "approve the letter" — rather than as a list of
assertions, and the artefacts they leave behind are meant to be readable by
someone who was not watching the run.  Three things make that work:

``step()``
    Every meaningful action runs inside a step.  It announces itself, is
    timed, screenshots itself when it finishes and records its outcome, so
    ``report.py`` can replay the run as prose.

:class:`Recorder`
    One is attached to every page.  It listens for console messages of every
    level, uncaught page errors, failed network requests and the correlation
    ids the backend puts on its responses (NFR-701), so a browser-side failure
    is reported with its cause instead of as a mystery timeout.

:func:`assert_log_contains`
    Reads the server's own log directory.  A test that exercises a feature can
    therefore also assert that the feature *logged* what NFR-701 says it must;
    observability that is never read is observability that quietly rots.

Selector policy: role- and text-based locators everywhere, because the point
of these tests is the behaviour, not the class names.  The two exceptions are
the loading spinner and the error alert, which carry no role and no stable
text — both are marked where they are used.
"""

from __future__ import annotations

import json
import os
import re
import time
import weakref
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import ConsoleMessage, Page
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Request as PlaywrightRequest
from playwright.sync_api import Response as PlaywrightResponse
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

E2E_DIR = Path(__file__).resolve().parent
REPO_ROOT = E2E_DIR.parents[1]
OUT_DIR = E2E_DIR / "out"
SCREENSHOT_DIR = OUT_DIR / "screenshots"
PERSONA_PATH = E2E_DIR / "persona" / "out" / "persona.json"
RUN_JSON = OUT_DIR / "run.json"
NARRATIVE_PATH = OUT_DIR / "run.log"

#: Where the backend writes its structured logs (NFR-701).  Read from the
#: application's own settings when they are importable, so the harness follows
#: a relocated ``DREAMJOB_LOG_DIR`` instead of guessing.
def _default_log_dir() -> Path:
    try:
        from dreamjob.config import get_settings  # noqa: PLC0415

        return get_settings().abs_log_dir
    except Exception:  # noqa: BLE001 - the harness must work without the app importable
        return REPO_ROOT / "logs"


LOG_DIR = _default_log_dir()

DEFAULT_TIMEOUT_MS = int(os.environ.get("DREAMJOB_E2E_TIMEOUT_MS", "15000"))

#: Response headers that may carry the request correlation id (NFR-701).  The
#: exact spelling is the backend's business; anything that looks like one is
#: collected so the report can show the ids a failing screen was serving.
CORRELATION_HEADERS = (
    "x-correlation-id",
    "x-request-id",
    "x-trace-id",
    "correlation-id",
    "request-id",
    "traceparent",
)

#: Failures that are part of normal operation and would otherwise drown the
#: report: the SPA probes ``/auth/me`` on boot precisely to find out whether
#: there is a session, and a bare dev server has no favicon.
EXPECTED_FAILURES: tuple[tuple[str, int], ...] = (
    ("/api/auth/me", 401),
    ("/api/auth/logout", 401),
    ("favicon.ico", 404),
)

#: Route -> screen name, mirroring the TITLES table in ``frontend/src/App.jsx``
#: so the report names screens the way the product does.
SCREEN_TITLES = {
    "/": "Sign in",
    "/overview": "Where I am",
    "/profile": "Profile",
    "/composite": "Composite profile",
    "/dream-job": "Dream job",
    "/directives": "Search directives",
    "/campaigns": "Campaigns",
    "/browser": "Browser session",
    "/opportunities": "Opportunities",
    "/companies": "Companies",
    "/intelligence": "Dream-job intelligence",
    "/contacts": "Hiring contacts",
    "/applications": "Applications",
    "/networking": "Networking and export",
    "/pipeline": "Application pipeline",
    "/responses": "Responses received",
    "/insights": "What works",
    "/monitoring": "Monitoring",
    "/mail": "Mail setup",
    "/admin": "Administration",
}


# ---------------------------------------------------------------------------
# Run record — what report.py turns into prose
# ---------------------------------------------------------------------------


@dataclass
class Run:
    """Everything one pytest session observed, in one place.

    A single module-level instance is filled in as the suites run and written
    to ``out/run.json`` at the end; ``report.py`` reads only that file, so the
    report can be regenerated without re-running the browser.
    """

    meta: dict[str, Any] = field(default_factory=dict)
    tests: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    console: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[dict[str, Any]] = field(default_factory=list)
    failed_requests: list[dict[str, Any]] = field(default_factory=list)
    navigations: list[dict[str, Any]] = field(default_factory=list)
    screenshots: list[dict[str, Any]] = field(default_factory=list)
    correlation_ids: list[dict[str, Any]] = field(default_factory=list)
    log_checks: list[dict[str, Any]] = field(default_factory=list)

    _shot_seq: int = 0

    def next_shot_index(self) -> int:
        self._shot_seq += 1
        return self._shot_seq

    def as_dict(self) -> dict[str, Any]:
        return {
            "meta": self.meta,
            "tests": self.tests,
            "steps": self.steps,
            "console": self.console,
            "page_errors": self.page_errors,
            "failed_requests": self.failed_requests,
            "navigations": self.navigations,
            "screenshots": self.screenshots,
            "correlation_ids": self.correlation_ids,
            "log_checks": self.log_checks,
        }

    def write(self, path: Path = RUN_JSON) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=2, default=str), encoding="utf-8")
        return path


RUN = Run()


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def narrate(line: str) -> None:
    """Say what is happening, on stdout and in ``out/run.log``.

    pytest captures stdout unless ``-s`` is passed, so the file is the copy
    that always survives; without it a passing run leaves no trace of what it
    actually exercised.
    """
    print(line, flush=True)
    try:
        NARRATIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with NARRATIVE_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{utcnow_iso()} {line}\n")
    except OSError:  # pragma: no cover - never fail a test over the log file
        pass


def screen_name(url: str) -> str:
    """Name the screen a URL belongs to, the way the sidebar names it."""
    path = urlparse(url).path or "/"
    if path in ("", "/", "/index.html"):
        return SCREEN_TITLES["/"]
    parts = [p for p in path.split("/") if p]
    base = "/" + parts[0]
    title = SCREEN_TITLES.get(base)
    if title is None:
        return path
    return f"{title} · detail" if len(parts) > 1 else title


def screen_path(url: str) -> str:
    path = urlparse(url).path or "/"
    parts = [p for p in path.split("/") if p]
    if not parts:
        return "/"
    return "/" + parts[0] + ("/:id" if len(parts) > 1 else "")


def _slug(text: str, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (slug[:limit].rstrip("-")) or "step"


# ---------------------------------------------------------------------------
# Per-page recorder
# ---------------------------------------------------------------------------


@dataclass
class Recorder:
    """Listens to one page and files what it hears under the current step."""

    page: Page
    base_url: str
    api_url: str
    test_id: str
    console: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[dict[str, Any]] = field(default_factory=list)
    failed_requests: list[dict[str, Any]] = field(default_factory=list)
    screenshots: list[dict[str, Any]] = field(default_factory=list)
    correlation_ids: dict[str, dict[str, Any]] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    step_stack: list[str] = field(default_factory=list)

    @property
    def current_step(self) -> str | None:
        return self.step_stack[-1] if self.step_stack else None

    def _where(self) -> dict[str, Any]:
        # page.url is a cached property, so it is safe to read from inside an
        # event handler (calling into the browser from one is not).
        try:
            url = self.page.url
        except PlaywrightError:  # pragma: no cover - page already gone
            url = ""
        return {
            "test": self.test_id,
            "step": self.current_step,
            "screen": screen_name(url),
            "path": screen_path(url),
            "url": url,
            "at": utcnow_iso(),
        }

    # -- listeners ----------------------------------------------------------

    def on_console(self, msg: ConsoleMessage) -> None:
        loc = msg.location or {}
        source_url = loc.get("url", "")
        entry = {
            **self._where(),
            "level": msg.type,
            "text": msg.text,
            "source": f"{source_url}:{loc.get('lineNumber', 0)}",
            "expected": _is_expected_console(msg.type, msg.text, source_url),
        }
        self.console.append(entry)
        RUN.console.append(entry)

    def on_page_error(self, error: Exception) -> None:
        message = getattr(error, "message", None) or str(error)
        entry = {
            **self._where(),
            "level": "pageerror",
            "text": message,
            "stack": (getattr(error, "stack", "") or "")[:2000],
        }
        self.page_errors.append(entry)
        RUN.page_errors.append(entry)
        narrate(f"  ✗ uncaught client error: {message}")

    def on_request_failed(self, request: PlaywrightRequest) -> None:
        failure = request.failure or "failed"
        # ``net::ERR_ABORTED`` means the browser cancelled the request because
        # the page moved on - a screen unmounting, a sign-out, the context
        # closing. The client log ships on ``pagehide`` precisely then, so
        # treating an abort as a product failure would report a working app as
        # broken. It is still recorded, only not counted.
        entry = {
            **self._where(),
            "method": request.method,
            "request_url": request.url,
            "status": None,
            "reason": failure,
            "expected": "ERR_ABORTED" in failure or _is_expected(request.url, None),
        }
        self.failed_requests.append(entry)
        RUN.failed_requests.append(entry)

    def on_response(self, response: PlaywrightResponse) -> None:
        try:
            headers = response.headers
        except PlaywrightError:  # pragma: no cover
            headers = {}
        self._collect_correlation(response, headers)
        if response.status < 400:
            return
        entry = {
            **self._where(),
            "method": response.request.method,
            "request_url": response.url,
            "status": response.status,
            "reason": response.status_text or "",
            "expected": _is_expected(response.url, response.status),
        }
        self.failed_requests.append(entry)
        RUN.failed_requests.append(entry)

    def _collect_correlation(self, response: PlaywrightResponse, headers: dict[str, str]) -> None:
        for name in CORRELATION_HEADERS:
            value = headers.get(name)
            if not value or value in self.correlation_ids:
                continue
            entry = {
                **self._where(),
                "header": name,
                "value": value,
                "request_url": response.url,
            }
            self.correlation_ids[value] = entry
            RUN.correlation_ids.append(entry)

    def on_navigated(self, frame: Any) -> None:
        try:
            if frame != self.page.main_frame:
                return
            url = frame.url
        except PlaywrightError:  # pragma: no cover
            return
        if not url or url == "about:blank":
            return
        entry = {
            "test": self.test_id,
            "step": self.current_step,
            "screen": screen_name(url),
            "path": screen_path(url),
            "url": url,
            "at": utcnow_iso(),
        }
        RUN.navigations.append(entry)


_RECORDERS: weakref.WeakKeyDictionary[Page, Recorder] = weakref.WeakKeyDictionary()


def _is_expected(url: str, status: int | None) -> bool:
    for fragment, expected_status in EXPECTED_FAILURES:
        if fragment in url and (status is None or status == expected_status):
            return True
    return False


def _is_expected_console(level: str, text: str, source_url: str) -> bool:
    """Is this console error only Chromium echoing an expected HTTP failure?

    The browser logs every non-2xx response as a console error of its own. The
    SPA's boot-time ``/auth/me`` probe therefore *always* leaves two of them on
    the sign-in screen, which would make ``expect_no_console_errors`` useless
    noise. Only the echo is discounted, and only for the status we expect —
    a 500 from the same endpoint is still an error.
    """
    if level != "error" or not text.startswith("Failed to load resource"):
        return False
    match = re.search(r"status of (\d{3})", text)
    return _is_expected(source_url, int(match.group(1)) if match else None)


def attach(page: Page, *, base_url: str, api_url: str, test_id: str) -> Recorder:
    """Wire a recorder to ``page``.  Called by the ``page`` fixture."""
    rec = Recorder(page=page, base_url=base_url.rstrip("/"), api_url=api_url.rstrip("/"),
                   test_id=test_id)
    page.on("console", rec.on_console)
    page.on("pageerror", rec.on_page_error)
    page.on("requestfailed", rec.on_request_failed)
    page.on("response", rec.on_response)
    page.on("framenavigated", rec.on_navigated)
    _RECORDERS[page] = rec
    return rec


def recorder(page: Page) -> Recorder:
    rec = _RECORDERS.get(page)
    if rec is None:  # pragma: no cover - only if a suite builds its own page
        raise RuntimeError(
            "This page has no recorder. Use the `page` fixture from tests/e2e/conftest.py, "
            "or call harness.attach(page, base_url=..., api_url=..., test_id=...) first."
        )
    return rec


def base_url_of(page: Page) -> str:
    return recorder(page).base_url


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------


def screenshot(page: Page, name: str, *, full_page: bool = True) -> Path:
    """Write a full-page screenshot to ``out/screenshots`` and record it.

    Numbered in run order, so the directory read top to bottom is the story of
    the run.  A screenshot that cannot be taken (a closed page during teardown)
    is recorded as such rather than failing an otherwise good test.
    """
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    index = RUN.next_shot_index()
    path = SCREENSHOT_DIR / f"{index:03d}-{_slug(name)}.png"
    ok, error = True, None
    try:
        page.screenshot(path=str(path), full_page=full_page)
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        ok, error = False, str(exc).splitlines()[0]
    rec = _RECORDERS.get(page)
    entry = {
        "index": index,
        "name": name,
        "file": path.name,
        "relative": str(path.relative_to(OUT_DIR)),
        "ok": ok,
        "error": error,
        "test": rec.test_id if rec else None,
        "step": rec.current_step if rec else None,
        "screen": screen_name(page.url) if ok else None,
        "at": utcnow_iso(),
    }
    RUN.screenshots.append(entry)
    if rec:
        rec.screenshots.append(entry)
    return path


# ---------------------------------------------------------------------------
# Steps — the narrative unit
# ---------------------------------------------------------------------------


@contextmanager
def step(page: Page, what: str, *, screen: str | None = None, shot: bool = True) -> Iterator[dict]:
    """Run one meaningful action as a recorded, timed, screenshotted step.

    ::

        with step(page, "Open the campaign the persona planned"):
            page.get_by_role("link", name="Campaigns").click()

    The step name is written in the first person present — it becomes a line
    in the report, so it should say what a person would say they were doing.
    """
    rec = recorder(page)
    narrate("▶ " + "  " * len(rec.step_stack) + what)
    entry: dict[str, Any] = {
        "test": rec.test_id,
        "name": what,
        "depth": len(rec.step_stack),
        "status": "running",
        "started_at": utcnow_iso(),
        "duration_ms": 0,
        "screenshots": [],
        "error": None,
        "console_errors": 0,
        "failed_requests": 0,
    }
    RUN.steps.append(entry)
    rec.steps.append(entry)
    rec.step_stack.append(what)

    # Indices, not counts: what the step is charged with is whatever arrives
    # between these marks.
    console_before = len(rec.console)
    page_errors_before = len(rec.page_errors)
    requests_before = len([r for r in rec.failed_requests if not r["expected"]])
    started = time.monotonic()
    try:
        yield entry
    except BaseException as exc:  # recorded, then re-raised untouched
        entry["status"] = "failed"
        entry["error"] = f"{type(exc).__name__}: {exc}".strip()
        raise
    else:
        entry["status"] = "passed"
    finally:
        entry["duration_ms"] = round((time.monotonic() - started) * 1000)
        try:
            url = page.url
        except PlaywrightError:  # pragma: no cover
            url = ""
        entry["url"] = url
        entry["screen"] = screen or screen_name(url)
        entry["path"] = screen_path(url)
        errors_here = [
            e
            for e in rec.console[console_before:]
            if e["level"] == "error" and not e["expected"]
        ]
        entry["console_errors"] = len(errors_here) + len(rec.page_errors) - page_errors_before
        entry["failed_requests"] = (
            len([r for r in rec.failed_requests if not r["expected"]]) - requests_before
        )
        if shot:
            # Still inside the step, so the screenshot is filed under it.
            suffix = "" if entry["status"] == "passed" else " (failed)"
            taken = screenshot(page, what + suffix)
            entry["screenshots"].append(str(taken.relative_to(OUT_DIR)))
        rec.step_stack.pop()
        mark = "✓" if entry["status"] == "passed" else "✗"
        narrate(f"  {mark} {what} — {entry['duration_ms']}ms — {entry['screen']}")


# ---------------------------------------------------------------------------
# Waiting for the SPA
# ---------------------------------------------------------------------------


def wait_for_ready(page: Page, *, timeout: int | None = None) -> None:
    """Wait until the screen has finished loading itself.

    The SPA is route-split (NFR-101), so arriving at a URL first renders a
    spinner while the chunk downloads, and then skeleton rows while the screen
    fetches its data.  Waiting for both to go is the only reliable signal that
    the *content* is there; sleeping is not, and ``networkidle`` never settles
    while a screen polls.  These two are CSS hooks on purpose: a loading
    placeholder has no role and no text to match.

    A screen that never settles is not failed here — the step's own assertion
    should be the one that fails, with something to say.
    """
    timeout = timeout or DEFAULT_TIMEOUT_MS
    page.wait_for_load_state("domcontentloaded")
    try:
        page.wait_for_function(
            "() => document.querySelector('#root')"
            " && !document.querySelector('.spinner')"
            " && !document.querySelector('.skeleton')",
            timeout=timeout,
        )
    except PlaywrightTimeoutError:
        narrate("  … still loading after the timeout; continuing so the failure is the real one")


def open_app(page: Page, path: str = "/", *, timeout: int | None = None) -> None:
    """Load the SPA at ``path`` and wait for it to be interactive."""
    rec = recorder(page)
    url = rec.base_url + (path if path.startswith("/") else "/" + path)
    page.goto(url, wait_until="domcontentloaded", timeout=timeout or DEFAULT_TIMEOUT_MS)
    wait_for_ready(page, timeout=timeout)


def open_screen(page: Page, label: str, *, timeout: int | None = None) -> None:
    """Click a sidebar entry by its visible label and wait for the screen.

    Navigating by the link a person would click keeps the test honest: if the
    entry is renamed or dropped, the suite says so instead of quietly hitting
    a URL that no longer has a way in.
    """
    page.get_by_role("link", name=label).first.click(timeout=timeout or DEFAULT_TIMEOUT_MS)
    wait_for_ready(page, timeout=timeout)


def current_error(page: Page) -> str | None:
    """The visible error banner, if the screen is showing one.

    ``.alert-danger`` is a CSS hook for the same reason as the spinner: the
    banner carries no role, and its text is exactly what we want to report.
    """
    banner = page.locator(".alert-danger")
    try:
        if banner.count() and banner.first.is_visible():
            return " ".join(banner.first.inner_text().split())
    except PlaywrightError:  # pragma: no cover
        return None
    return None


# ---------------------------------------------------------------------------
# Sign in / register
# ---------------------------------------------------------------------------


def is_signed_in(page: Page) -> bool:
    """True when the application shell is up (the sign-out button is its tell)."""
    try:
        return page.get_by_role("button", name="Sign out").is_visible()
    except PlaywrightError:  # pragma: no cover - page closed mid-check
        return False


def _first_present(page: Page, *selectors: str):
    """The first of ``selectors`` that matches something on the page.

    The sign-in card does not bind its ``<label>`` elements to their inputs, so
    ``get_by_label`` finds nothing there and an input's accessible name falls
    back to its placeholder — which is example text and will change. The input
    *type* and *autocomplete* attributes are the semantic, restyling-proof
    anchors, and each field is tried in order of how specific it is.
    """
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count():
            return locator.first
    return page.locator(selectors[-1]).first


def _email_field(page: Page):
    return _first_present(page, "input[type='email']", "input[autocomplete='username']")


def _password_field(page: Page):
    return _first_present(page, "input[type='password']")


def _name_field(page: Page):
    return _first_present(page, "input[autocomplete='name']", "form input[type='text']")


def _totp_field(page: Page):
    return _first_present(page, "input[inputmode='numeric']", "input[maxlength='6']")


def _visible(locator) -> bool:
    try:
        return bool(locator.count()) and locator.is_visible()
    except PlaywrightError:  # pragma: no cover
        return False


def _await_sign_in_outcome(page: Page, *, timeout: int | None = None) -> str:
    """Watch the card until it resolves: shell, MFA prompt, or a refusal.

    Polling all three outcomes at once is what keeps a wrong password a
    one-second failure that quotes the server's message, instead of a
    fifteen-second timeout that says nothing.
    """
    deadline = time.monotonic() + (timeout or DEFAULT_TIMEOUT_MS) / 1000
    while time.monotonic() < deadline:
        if is_signed_in(page):
            return "signed-in"
        if _visible(_totp_field(page)):
            return "mfa"
        refusal = current_error(page)
        if refusal:
            raise AssertionError(refusal)
        page.wait_for_timeout(150)
    raise AssertionError("the application shell never appeared")


def sign_in(
    page: Page,
    email: str,
    password: str,
    *,
    totp: str | None = None,
    timeout: int | None = None,
) -> None:
    """Sign an existing account in, answering the TOTP prompt if one appears."""
    if not is_signed_in(page):
        open_app(page)
    if is_signed_in(page):
        return

    # The card remembers whichever mode it was last put in.
    back_to_sign_in = page.get_by_role("button", name="I already have an account")
    if _visible(back_to_sign_in):
        back_to_sign_in.click()

    _email_field(page).fill(email)
    _password_field(page).fill(password)
    page.get_by_role("button", name="Sign in", exact=True).click()

    # NFR-202: the server, not the client, decides whether a second factor is
    # needed, so the prompt only appears once the first factor has passed.
    try:
        outcome = _await_sign_in_outcome(page, timeout=timeout)
        if outcome == "mfa":
            if not totp:
                raise AssertionError(f"{email} has MFA enrolled but no TOTP code was given")
            _totp_field(page).fill(totp)
            page.get_by_role("button", name="Sign in", exact=True).click()
            if _await_sign_in_outcome(page, timeout=timeout) != "signed-in":
                raise AssertionError("the authentication code was not accepted")
    except AssertionError as exc:
        raise AssertionError(f"Signing in as {email} failed: {exc}") from exc

    wait_for_ready(page, timeout=timeout)
    narrate(f"  · signed in as {email}")


def register_fresh_account(
    page: Page,
    persona: Persona,
    *,
    email: str | None = None,
    timeout: int | None = None,
) -> dict[str, str]:
    """Create a brand-new account for the persona and land in the app.

    Every run gets its own address so the suite starts from an empty private
    space (FR-101): no leftover opportunities, no half-finished campaign from
    yesterday, and nothing that could be mistaken for another job seeker's
    data. The credentials are returned so a later test can sign the same
    account back in.
    """
    address = email or persona.fresh_email()
    password = persona.password
    display_name = persona.display_name

    open_app(page)
    create = page.get_by_role("button", name="Create an account")
    if _visible(create):
        create.click()

    _name_field(page).fill(display_name)
    _email_field(page).fill(address)
    _password_field(page).fill(password)
    page.get_by_role("button", name="Create account", exact=True).click()

    try:
        if _await_sign_in_outcome(page, timeout=timeout) != "signed-in":
            raise AssertionError("the sign-in card asked for a code a new account cannot have")
    except AssertionError as exc:
        raise AssertionError(f"Registering {address} failed: {exc}") from exc

    wait_for_ready(page, timeout=timeout)
    narrate(f"  · registered {address} ({display_name})")
    return {"email": address, "password": password, "display_name": display_name}


def sign_out(page: Page, *, timeout: int | None = None) -> None:
    """Sign out and wait for the sign-in card to come back."""
    page.get_by_role("button", name="Sign out").click()
    page.get_by_role("button", name="Create an account").wait_for(
        state="visible", timeout=timeout or DEFAULT_TIMEOUT_MS
    )


# ---------------------------------------------------------------------------
# Client-side health checks
# ---------------------------------------------------------------------------


def console_errors(page: Page, *, include_expected: bool = False) -> list[dict[str, Any]]:
    """Console messages of level ``error``, plus uncaught exceptions.

    Chromium's echo of an expected HTTP failure (see ``EXPECTED_FAILURES``) is
    left out unless asked for.
    """
    rec = recorder(page)
    errors = [
        e for e in rec.console if e["level"] == "error" and (include_expected or not e["expected"])
    ]
    return errors + list(rec.page_errors)


def console_warnings(page: Page) -> list[dict[str, Any]]:
    return [e for e in recorder(page).console if e["level"] in ("warning", "warn")]


def failed_requests(page: Page, *, include_expected: bool = False) -> list[dict[str, Any]]:
    rec = recorder(page)
    return [r for r in rec.failed_requests if include_expected or not r["expected"]]


def correlation_ids(page: Page) -> list[str]:
    """Correlation ids seen on responses to this page (NFR-701)."""
    return sorted(recorder(page).correlation_ids)


def _matches_any(text: str, patterns: Sequence[str]) -> bool:
    return any(p in text for p in patterns)


def expect_no_console_errors(page: Page, *, ignore: Sequence[str] = ()) -> None:
    """Fail the test if the browser reported an error, and say what it was.

    Called at the end of every step-heavy suite.  A React render that throws
    leaves the screen half-drawn rather than blank, so without this check a
    broken screen can still satisfy every locator on it.

    ``ignore`` is matched against the message *and* the source it came from,
    because Chromium's own network errors all read "Failed to load resource"
    and only the URL says which call it was:
    ``ignore=("/api/profile/",)``.
    """
    problems = [
        e
        for e in console_errors(page)
        if not _matches_any(f"{e['text']} {e.get('source', '')}", ignore)
    ]
    if not problems:
        return
    lines = [
        f"  [{e['level']}] on {e['screen']} during {e['step'] or 'no step'}: {e['text']}"
        + (f"\n      at {e['source']}" if e.get("source") else "")
        for e in problems
    ]
    raise AssertionError(
        f"{len(problems)} uncaught client error(s):\n" + "\n".join(lines)
    )


def expect_no_failed_requests(page: Page, *, ignore: Sequence[str] = ()) -> None:
    """Fail if any request the screen made came back 4xx/5xx or never landed."""
    problems = [r for r in failed_requests(page) if not _matches_any(r["request_url"], ignore)]
    if not problems:
        return
    lines = [
        f"  {r['method']} {r['request_url']} -> {r['status'] or r['reason']}"
        f" (on {r['screen']}, during {r['step'] or 'no step'})"
        for r in problems
    ]
    raise AssertionError(f"{len(problems)} failed request(s):\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# The server's own logs (NFR-701)
# ---------------------------------------------------------------------------


def log_files(log_dir: Path | None = None) -> list[Path]:
    directory = log_dir or LOG_DIR
    if not directory.is_dir():
        return []
    keep = {".log", ".jsonl", ".ndjson", ".txt", ""}
    return sorted(
        p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in keep
    )


def _read_tail(path: Path, offset: int = 0) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            return fh.read()
    except OSError:  # pragma: no cover - rotated out from under us
        return ""


class LogTail:
    """Only the log lines written *after* this object was created.

    Anchoring to a byte offset is what makes the assertion meaningful: a
    yesterday's line matching the pattern would otherwise let a broken
    logger pass.
    """

    def __init__(self, log_dir: Path | None = None) -> None:
        self.log_dir = log_dir or LOG_DIR
        self.offsets = {p: p.stat().st_size for p in log_files(self.log_dir)}

    def new_lines(self) -> list[tuple[Path, str]]:
        lines: list[tuple[Path, str]] = []
        for path in log_files(self.log_dir):
            start = self.offsets.get(path, 0)  # a file created since: read it whole
            # A file that shrank was rotated or truncated, so everything in it
            # now was written after the snapshot: read it whole too. Clamping
            # to the new size instead would skip to the end and see nothing.
            offset = 0 if path.stat().st_size < start else start
            for line in _read_tail(path, offset).splitlines():
                if line.strip():
                    lines.append((path, line))
        return lines

    def find(self, pattern: str | re.Pattern[str]) -> list[tuple[Path, str]]:
        return [(p, line) for p, line in self.new_lines() if _line_matches(line, pattern)]

    def assert_contains(
        self,
        pattern: str | re.Pattern[str],
        *,
        what: str | None = None,
        timeout: float = 5.0,
    ) -> str:
        """Assert the backend logged ``pattern`` since this tail was opened.

        Logging happens on the server's own schedule, so the check polls
        rather than reading once.
        """
        return _await_log_match(self, pattern, what=what, timeout=timeout)


def _line_matches(line: str, pattern: str | re.Pattern[str]) -> bool:
    if isinstance(pattern, re.Pattern):
        return bool(pattern.search(line))
    if pattern in line:
        return True
    try:
        return bool(re.search(pattern, line))
    except re.error:
        return False


def _await_log_match(
    tail: LogTail,
    pattern: str | re.Pattern[str],
    *,
    what: str | None,
    timeout: float,
) -> str:
    label = pattern.pattern if isinstance(pattern, re.Pattern) else pattern
    deadline = time.monotonic() + timeout
    seen: list[tuple[Path, str]] = []
    while True:
        matches = tail.find(pattern)
        if matches:
            path, line = matches[0]
            RUN.log_checks.append(
                {
                    "pattern": label,
                    "what": what,
                    "found": True,
                    "file": str(path),
                    "line": line[:500],
                    "at": utcnow_iso(),
                }
            )
            narrate(f"  · log ✓ {what or label}")
            return line
        seen = tail.new_lines()
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)

    RUN.log_checks.append(
        {
            "pattern": label,
            "what": what,
            "found": False,
            "file": None,
            "line": None,
            "at": utcnow_iso(),
        }
    )
    files = log_files(tail.log_dir)
    if not files:
        raise AssertionError(
            f"No log files under {tail.log_dir}. NFR-701 requires the backend to emit "
            "structured logs there; start the app with ./scripts/dev.sh and check "
            "DREAMJOB_LOG_DIR."
        )
    context = "\n".join(f"    {line}" for _, line in seen[-10:]) or "    (nothing new was written)"
    raise AssertionError(
        f"Expected a log line matching {label!r}"
        + (f" ({what})" if what else "")
        + f" in {len(files)} file(s) under {tail.log_dir} (NFR-701).\n"
        f"  Last lines written during this test:\n{context}"
    )


def assert_log_contains(
    pattern: str | re.Pattern[str],
    *,
    what: str | None = None,
    log_dir: Path | None = None,
    timeout: float = 5.0,
) -> str:
    """Assert the backend logged ``pattern`` (NFR-701), scanning whole files.

    Prefer the ``log_tail`` fixture when the point is that *this* test caused
    the line; use this when any recent occurrence will do.
    """
    tail = LogTail(log_dir)
    tail.offsets = dict.fromkeys(tail.offsets, 0)
    return _await_log_match(tail, pattern, what=what, timeout=timeout)


def log_tail(log_dir: Path | None = None) -> LogTail:
    """Snapshot the log directory now; assert against what follows."""
    return LogTail(log_dir)


# ---------------------------------------------------------------------------
# The persona
# ---------------------------------------------------------------------------

#: Satisfies NFR-202's rule (nine characters, three character classes) without
#: being anyone's real password.
DEFAULT_PASSWORD = "DreamJob!2026"

#: Reserved TLDs (RFC 2606 / 6761) that the backend's e-mail validator refuses.
#: A generated persona may well live at ``@example.test``; registering it would
#: then fail on the address rather than on anything the test is about.
UNROUTABLE_TLDS = frozenset({"test", "invalid", "local", "localhost", "example", "onion", "arpa"})
FALLBACK_DOMAIN = "example.com"


@dataclass(frozen=True)
class Persona:
    """The generated job seeker the suites act as.

    ``tests/e2e/persona`` owns the shape of ``persona.json``; the harness only
    needs a name, an address and a password, so it looks for each of them in
    the handful of places they could reasonably live rather than insisting on
    one schema.
    """

    raw: dict[str, Any]
    path: Path | None = None

    def get(self, *dotted: str, default: Any = None) -> Any:
        for candidate in dotted:
            node: Any = self.raw
            for part in candidate.split("."):
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    node = None
                    break
            if node not in (None, "", [], {}):
                return node
        return default

    @property
    def display_name(self) -> str:
        return str(
            self.get(
                "display_name", "name", "full_name", "account.display_name",
                "contact.name", "profile.contact.name", "identity.name",
                default="Dream Job Tester",
            )
        )

    @property
    def email(self) -> str:
        return str(
            self.get(
                "email", "account.email", "contact.email", "profile.contact.email",
                "identity.email",
                default=f"persona@{FALLBACK_DOMAIN}",
            )
        )

    @property
    def password(self) -> str:
        return str(self.get("password", "account.password", default=DEFAULT_PASSWORD))

    def fresh_email(self, tag: str = "e2e") -> str:
        """A unique address derived from the persona's own.

        The plus-alias keeps the local part recognisable in the database while
        guaranteeing that this run registers rather than collides with the
        account the last run created.
        """
        local, _, domain = self.email.partition("@")
        local = local.split("+")[0] or "persona"
        if not domain or domain.rsplit(".", 1)[-1].lower() in UNROUTABLE_TLDS:
            domain = FALLBACK_DOMAIN
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        return f"{local}+{tag}-{stamp}@{domain}"

    def skills(self) -> list[str]:
        value = self.get("skills", "profile.skills", default=[])
        out: list[str] = []
        for item in value if isinstance(value, Iterable) and not isinstance(value, str) else []:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("skill")
                if name:
                    out.append(str(name))
        return out


def load_persona(path: Path | None = None) -> Persona:
    """Read ``tests/e2e/persona/out/persona.json``.

    Raises :class:`FileNotFoundError` when it has not been generated; the
    fixture in ``conftest.py`` turns that into a skip with the command to run.
    """
    source = path or PERSONA_PATH
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):  # pragma: no cover - defensive
        raise ValueError(f"{source} should contain a JSON object, got {type(data).__name__}")
    return Persona(raw=data, path=source)
