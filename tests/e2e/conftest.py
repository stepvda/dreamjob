"""Fixtures for the end-to-end suites.

These tests drive the *running* application — the one ``./scripts/dev.sh``
starts — rather than a TestClient, because the questions they answer are the
ones only a real browser can: does the lazy-loaded screen actually paint, does
the session cookie survive a reload, does the console stay clean.

Consequences of that choice, handled here:

* nothing is mocked, so the suite skips itself with an explanation when the
  app is not up rather than failing twenty times with connection errors;
* each test gets a fresh browser context, so one test's session can never
  explain another test's pass;
* the whole session's steps, screenshots, console output and failed requests
  are written to ``out/run.json`` at the end and rendered by ``report.py``.

The harness itself lives in ``harness.py``; suites import it directly::

    from harness import expect_no_console_errors, screenshot, step
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

E2E_DIR = Path(__file__).resolve().parent
if str(E2E_DIR) not in sys.path:  # so `import harness` works from every suite
    sys.path.insert(0, str(E2E_DIR))

import harness  # noqa: E402  - needs the path entry above
import report  # noqa: E402

DEFAULT_APP_URL = "http://127.0.0.1:5173"
DEFAULT_API_URL = "http://127.0.0.1:8000"

_START_HINT = (
    "Start it first:\n"
    "    ./scripts/dev.sh                       # backend on :8000, SPA on :5173\n"
    "Override the addresses with DREAMJOB_E2E_BASE_URL / DREAMJOB_E2E_API_URL."
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def e2e_config() -> dict[str, Any]:
    """Where the app is and how the browser should behave.

    Everything is an environment variable so the same suite runs against a dev
    server, a preview build served by FastAPI, or CI, without an edit.
    """
    cfg = {
        "base_url": os.environ.get("DREAMJOB_E2E_BASE_URL", DEFAULT_APP_URL).rstrip("/"),
        "api_url": os.environ.get("DREAMJOB_E2E_API_URL", DEFAULT_API_URL).rstrip("/"),
        "headless": os.environ.get("DREAMJOB_E2E_HEADED", "") not in ("1", "true", "yes"),
        "slow_mo_ms": int(os.environ.get("DREAMJOB_E2E_SLOWMO_MS", "0")),
        "timeout_ms": harness.DEFAULT_TIMEOUT_MS,
        "viewport": {"width": 1440, "height": 900},
        "log_dir": str(harness.LOG_DIR),
    }
    harness.RUN.meta.update(cfg)
    harness.RUN.meta.setdefault("started_at", harness.utcnow_iso())
    return cfg


@pytest.fixture(scope="session")
def base_url(e2e_config: dict[str, Any]) -> str:
    return e2e_config["base_url"]


@pytest.fixture(scope="session")
def api_url(e2e_config: dict[str, Any]) -> str:
    return e2e_config["api_url"]


def _get(url: str, timeout: float = 3.0) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "dreamjob-e2e"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.status, response.read()


@pytest.fixture(scope="session", autouse=True)
def app_running(e2e_config: dict[str, Any]) -> dict[str, Any]:
    """Skip the whole suite, with the reason, unless both halves answer.

    An end-to-end suite that cannot reach the app has nothing useful to say;
    failing every test would only bury the one fact that matters.
    """
    api = e2e_config["api_url"]
    app = e2e_config["base_url"]

    try:
        status, body = _get(f"{api}/api/health")
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"The Dream Job API is not answering on {api} ({exc}).\n{_START_HINT}")
    if status != 200:
        pytest.skip(f"{api}/api/health answered {status}, not 200.\n{_START_HINT}")

    try:
        health = json.loads(body)
    except ValueError:
        health = {}

    try:
        status, _ = _get(app)
    except (urllib.error.URLError, OSError) as exc:
        pytest.skip(f"The Dream Job SPA is not answering on {app} ({exc}).\n{_START_HINT}")
    if status != 200:
        pytest.skip(f"{app} answered {status}, not 200.\n{_START_HINT}")

    harness.RUN.meta["health"] = health
    harness.narrate(f"App is up: SPA {app}, API {api} (env={health.get('env', '?')})")
    return health


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def playwright_instance() -> Iterator[Any]:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="session")
def browser(playwright_instance: Any, e2e_config: dict[str, Any]) -> Iterator[Any]:
    """One chromium for the session; contexts are what isolate the tests."""
    instance = playwright_instance.chromium.launch(
        headless=e2e_config["headless"],
        slow_mo=e2e_config["slow_mo_ms"],
    )
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def context(browser: Any, e2e_config: dict[str, Any]) -> Iterator[Any]:
    """A fresh browsing context per test: no cookies, no storage, no history.

    1440x900 is a laptop at a comfortable zoom — wide enough that the sidebar
    and the content are both on screen, which is what the screenshots in the
    report need to be worth looking at.
    """
    ctx = browser.new_context(
        viewport=e2e_config["viewport"],
        locale="en-GB",
        timezone_id="Europe/Brussels",
    )
    ctx.set_default_timeout(e2e_config["timeout_ms"])
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture
def page(context: Any, e2e_config: dict[str, Any], request: pytest.FixtureRequest) -> Iterator[Any]:
    """A page with a recorder attached (console, errors, requests, NFR-701 ids)."""
    pg = context.new_page()
    pg.set_default_timeout(e2e_config["timeout_ms"])
    harness.attach(
        pg,
        base_url=e2e_config["base_url"],
        api_url=e2e_config["api_url"],
        test_id=request.node.nodeid,
    )
    try:
        yield pg
    finally:
        # A test that failed outside a step still deserves a picture of the
        # screen it died on.
        if getattr(request.node, "_e2e_failed", False):
            harness.screenshot(pg, f"{request.node.name} (test failed)")
        pg.close()


# ---------------------------------------------------------------------------
# Test data and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def persona() -> harness.Persona:
    """The generated job seeker the suites act as.

    Generated once, outside the test run, because it costs LLM calls; a
    missing file is a setup problem, not a test failure.
    """
    try:
        loaded = harness.load_persona()
    except FileNotFoundError:
        pytest.skip(
            f"No persona at {harness.PERSONA_PATH}. Generate one first:\n"
            "    python3 tests/e2e/persona/generate.py\n"
            "    python3 tests/e2e/persona/render.py"
        )
    except ValueError as exc:
        pytest.skip(f"{harness.PERSONA_PATH} could not be read: {exc}")
    harness.RUN.meta["persona"] = {
        "display_name": loaded.display_name,
        "email": loaded.email,
        "path": str(loaded.path),
    }
    return loaded


@pytest.fixture
def log_tail() -> harness.LogTail:
    """The backend's log directory, snapshotted at the start of this test.

    Assertions made through it can only be satisfied by lines this test
    caused, which is what makes them evidence that the logging works
    (NFR-701).
    """
    return harness.log_tail()


@pytest.fixture
def e2e() -> Any:
    """The harness module, for suites that prefer a fixture to an import."""
    return harness


# ---------------------------------------------------------------------------
# Session bookkeeping and the report
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def e2e_out_dir() -> Iterator[Path]:
    """Start each session from an empty ``out/`` so the report is this run.

    Set DREAMJOB_E2E_KEEP_OUT=1 to accumulate instead (useful when running one
    suite at a time).
    """
    if os.environ.get("DREAMJOB_E2E_KEEP_OUT", "") not in ("1", "true", "yes"):
        shutil.rmtree(harness.SCREENSHOT_DIR, ignore_errors=True)
        for stale in (harness.RUN_JSON, harness.NARRATIVE_PATH):
            stale.unlink(missing_ok=True)
    harness.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    # Artefacts, not sources. Ignoring them from inside the directory they live
    # in keeps the repository's own .gitignore free of test housekeeping.
    (harness.OUT_DIR / ".gitignore").write_text("*\n", encoding="utf-8")
    yield harness.OUT_DIR


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> Any:
    """Record how each e2e test ended, for the report's narrative.

    The path guard keeps the run record about the e2e suites even when the
    whole test tree is run in one go.
    """
    result = yield
    if E2E_DIR not in Path(str(getattr(item, "path", ""))).parents:
        return result
    if result.when == "call":
        harness.RUN.tests.append(
            {
                "nodeid": result.nodeid,
                "name": item.name,
                "file": str(Path(str(item.path)).name) if getattr(item, "path", None) else "",
                "outcome": result.outcome,
                "duration_ms": round(result.duration * 1000),
                "message": _short_failure(result),
            }
        )
    if result.failed and result.when in ("setup", "call"):
        item._e2e_failed = True  # read by the page fixture, for the last screenshot
    return result


def _short_failure(report_obj: Any) -> str | None:
    if report_obj.outcome != "failed":
        return None
    text = str(getattr(report_obj, "longrepr", "") or "")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-6:])[:1500] or None


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Write ``out/run.json`` and render the report."""
    if hasattr(session.config, "workerinput"):  # pragma: no cover - xdist worker
        return
    run = harness.RUN
    if not run.steps and not run.tests:
        return  # nothing ran (usually the app-not-running skip); keep the last report
    run.meta["finished_at"] = harness.utcnow_iso()
    run.meta["exit_status"] = int(exitstatus)
    run_path = run.write()
    markdown, data = report.write_reports(run.as_dict())
    harness.narrate(f"Report: {markdown}")
    print(f"\ne2e artefacts:\n  {run_path}\n  {markdown}\n  {data}", flush=True)
