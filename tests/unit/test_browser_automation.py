"""Browser automation over CDP (FR-201..208, NFR-203, NFR-303, NFR-502, CR-401, CR-406, RK-01).

Everything here runs without a browser and without network access: the live
session is replaced by a fake that serves saved HTML, and the run is measured
in virtual time by replacing the two indirections
:func:`dreamjob.browser.pacing.now` and :func:`dreamjob.browser.pacing.sleep`.

The properties under test are the ones the requirements make promises about:
the target list is closed (FR-205), the announced duration lands within +/-25%
of the real one (FR-204), a challenge stops the run at once (FR-203), people
collected in the browser are campaign-scoped and expire (NFR-303), nothing
that looks like a credential is ever written down (NFR-203), and the ATS
helper never submits (FR-328).
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from dreamjob.browser import ats_form, glassdoor
from dreamjob.browser import linkedin as li
from dreamjob.browser import pacing as pacing_mod
from dreamjob.browser import session as session_mod
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_all, update_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import browser as repo
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.security import auth_service as auth

BROWSER_PACKAGE = Path(__file__).resolve().parents[2] / "backend" / "dreamjob" / "browser"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Fixtures: a fake browser session and a virtual clock
# ---------------------------------------------------------------------------


class _FakeMouse:
    def __init__(self, clock):
        self.clock = clock
        self.wheels = 0

    async def wheel(self, dx: int, dy: int) -> None:
        self.wheels += 1


class _FakePage:
    def __init__(self, clock):
        self.mouse = _FakeMouse(clock)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeSession:
    """Stands in for :class:`dreamjob.browser.session.BrowserSession`."""

    def __init__(self, pages: dict[str, str], clock: _Clock, *, load_seconds: float = 1.5,
                 statuses: dict[str, int] | None = None):
        self.pages = pages
        self.clock = clock
        self.load_seconds = load_seconds
        self.statuses = statuses or {}
        self.visited: list[str] = []
        self._page = _FakePage(clock)
        self._current = ""

    async def page(self):
        return self._page

    async def goto(self, url: str):
        self.visited.append(url)
        self._current = url
        self.clock.advance(self.load_seconds)
        return session_mod.PageLoad(
            url=url,
            status=self.statuses.get(url, 200),
            seconds=self.load_seconds,
            title="page",
        )

    async def content(self) -> str:
        return self.pages.get(self._current, "<html><body>nothing</body></html>")

    async def screenshot(self) -> bytes:
        return b""

    async def bring_to_front(self) -> None:
        return None

    # -- the live-session surface the workers use ---------------------------
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def check_login(self, site: str) -> dict:
        return {"site": site, "logged_in": True, "status": 200, "title": "", "detail": ""}


@pytest.fixture()
def virtual_time(monkeypatch):
    """Run a paced run instantly while keeping its measured duration real."""
    clock = _Clock()
    monkeypatch.setattr(pacing_mod, "now", lambda: clock.t)

    async def _sleep(seconds: float) -> None:
        clock.advance(seconds)

    monkeypatch.setattr(pacing_mod, "sleep", _sleep)
    return clock


PROFILE_HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
 {"@type":"Person","name":"Jane Doe","jobTitle":"Head of Data",
  "worksFor":{"@type":"Organization","name":"Acme NV"},
  "address":{"@type":"PostalAddress","addressLocality":"Ghent","addressCountry":"BE"}}]}
</script>
<script>window.csrfToken = "ajax:9988776655443322";
document.cookie = "JSESSIONID=SECRET-TOKEN-1";</script>
</head><body><h1>Jane Doe</h1>
<div class="text-body-medium">Head of Data at Acme NV</div></body></html>
"""

SEARCH_HTML = """
<html><body><ul>
<li class="reusable-search__result-container">
  <a href="/in/jane-doe-123/?trk=search"><span aria-hidden="true">Jane Doe</span></a>
  <div class="entity-result__primary-subtitle">Head of Data at Acme NV</div>
</li>
<li class="reusable-search__result-container">
  <a href="https://www.linkedin.com/in/karel-peeters">
    <span aria-hidden="true">Karel Peeters</span></a>
  <div class="entity-result__primary-subtitle">Data Platform Lead</div>
</li>
</ul></body></html>
"""

CAPTCHA_HTML = """
<html><body><div class="challenge-dialog">Let's do a quick security check</div>
<div id="captcha-internal"></div></body></html>
"""


def _seeker() -> str:
    return insert_row(
        "job_seeker",
        {
            "email": "browser@example.com",
            "display_name": "Test Seeker",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )


def _campaign(seeker_id: str) -> str:
    directive_id = insert_row(
        "directive_set",
        {
            "job_seeker_id": seeker_id,
            "name": "Benelux data leadership",
            "created_at": utcnow(),
        },
    )
    profile_id = insert_row(
        "profile_version",
        {
            "job_seeker_id": seeker_id,
            "version": 1,
            "sections": {"experience": []},
            "created_at": utcnow(),
        },
    )
    return campaign_repo.create_campaign(
        seeker_id,
        {
            "name": "Autumn search",
            "directive_set_id": directive_id,
            "profile_version_id": profile_id,
        },
    )


def _linkedin_plan(campaign_id: str, urls: list[str]) -> str:
    from dreamjob.db.connection import upsert_row

    upsert_row(
        "source_catalogue",
        {
            "adapter_key": li.ADAPTER_KEY,
            "display_name": "LinkedIn network",
            "source_type": "linkedin",
            "access_method": "browser",
            "tos_status": "restricted",
            "enabled": 1,
            "requires_ack": 1,
            "updated_at": utcnow(),
        },
        ["adapter_key"],
    )
    return campaign_repo.insert_plan_item(
        campaign_id,
        {
            "adapter_key": li.ADAPTER_KEY,
            "native_query": {"strategy": "network", "search_urls": urls},
            "rationale": "FR-165 network strategy",
            "caps": {"max_profiles": 10, "max_companies": 5},
            "estimated_pages": len(urls),
            "created_at": utcnow(),
        },
    )


# ---------------------------------------------------------------------------
# FR-201: launch instructions, dedicated profile
# ---------------------------------------------------------------------------


def test_launch_instructions_are_data_with_a_dedicated_profile(db):
    payload = session_mod.launch_instructions("linkedin", os_name="macos")

    assert payload["cdp_url"].endswith(":9222")
    assert payload["profile_dir"] == str(session_mod.profile_dir())
    commands = payload["steps"][1]["commands"]
    assert commands, "there must be at least one macOS recipe"
    chromium = [c for c in commands if c["family"] == "chromium"]
    assert all("--remote-debugging-port=9222" in c["command"] for c in chromium)
    assert all(payload["profile_dir"] in c["command"] for c in commands)
    # RK-01 / CR-401: the terms warning travels with the instructions.
    assert payload["warnings"] and "user agreement" in payload["warnings"][0]
    # FR-208: Firefox is offered as an alternative driver.
    assert any(r["family"] == "firefox" for r in payload["recipes"])


def test_login_state_is_read_from_the_page_not_from_cookies():
    assert session_mod.login_state("linkedin", "https://www.linkedin.com/feed/",
                                   '<div class="global-nav__me"></div>') is True
    assert session_mod.login_state("linkedin", "https://www.linkedin.com/authwall", "") is False
    assert session_mod.login_state("linkedin", "https://www.linkedin.com/x", "<p>hi</p>") is None


# ---------------------------------------------------------------------------
# FR-202: the markers live on a client-rendered page, so the check has to wait
# for one before it judges.  Reading the shell at domcontentloaded reported
# "could not tell" for a session that was in fact signed in.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# FR-206: the window is the user's, and the automation does not resize it
# ---------------------------------------------------------------------------


class _RecordingContext:
    def __init__(self):
        self.pages = []

    async def new_page(self):
        raise AssertionError("not needed for this test")

    async def close(self):
        pass


class _RecordingBrowser:
    def __init__(self, calls):
        self.calls = calls
        self.contexts = []

    async def new_context(self, **kwargs):
        self.calls.append(("new_context", kwargs))
        return _RecordingContext()

    async def close(self):
        pass


class _RecordingEngine:
    def __init__(self, calls):
        self.calls = calls

    async def launch_persistent_context(self, profile, **kwargs):
        self.calls.append(("launch_persistent_context", kwargs))
        return _RecordingContext()

    async def connect_over_cdp(self, url):
        return _RecordingBrowser(self.calls)


class _RecordingPlaywright:
    def __init__(self, calls):
        self.chromium = _RecordingEngine(calls)
        self.firefox = _RecordingEngine(calls)

    async def stop(self):
        pass


class _RecordingStarter:
    def __init__(self, calls):
        self.calls = calls

    async def start(self):
        return _RecordingPlaywright(self.calls)


def _record_connect(monkeypatch, **session_kwargs) -> list:
    calls: list = []
    import playwright.async_api as pw

    monkeypatch.setattr(pw, "async_playwright", lambda: _RecordingStarter(calls))
    live = session_mod.BrowserSession(**session_kwargs)
    asyncio.run(live.connect())
    return calls


def test_a_launched_window_is_not_shrunk_to_a_size_nobody_chose(monkeypatch, tmp_path):
    """FR-206 asks the user to watch this window; it must not open resized.

    Playwright's default emulates a 1280x720 viewport, and on a *headed* browser
    that is not emulation - it resizes the real window. Measured before the fix:
    a persistent context opened at 1282x846 regardless of the display.
    """
    monkeypatch.setattr(session_mod, "profile_dir", lambda: tmp_path)
    calls = _record_connect(monkeypatch, driver="persistent", family="chromium")
    kind, kwargs = next(c for c in calls if c[0] == "launch_persistent_context")
    assert kwargs.get("no_viewport") is True, "the browser sizes its own window"


def test_a_context_dream_job_creates_over_cdp_imposes_no_size(monkeypatch):
    """The window belongs to the user even when Dream Job has to make the context."""
    calls = _record_connect(monkeypatch, driver="cdp")
    kind, kwargs = next(c for c in calls if c[0] == "new_context")
    assert kwargs.get("no_viewport") is True


def test_current_linkedin_feed_markup_reads_as_signed_in():
    """A slice of the frontend LinkedIn actually serves (checked 2026-09-10).

    The previous marker set matched none of this, so a session that was signed
    in reported "could not tell" and the whole feature looked inert (RK-04).
    """
    feed = (
        '<html><body><nav data-testid="primary-nav" id="primaryNavLinksComponentRef">'
        "</nav>"
        '<main data-testid="mainFeed">'
        '<div id="shareboxProfilePictureComponentRef"></div>'
        "</main></body></html>"
    )
    assert session_mod.login_state("linkedin", "https://www.linkedin.com/feed/", feed) is True
    # Each marker has to stand on its own: LinkedIn will not retire them together.
    for marker in ('data-testid="primary-nav"', 'data-testid="mainFeed"',
                   "primaryNavLinksComponentRef", "shareboxProfilePictureComponentRef"):
        one = f"<html><body><div {marker}></div></body></html>"
        assert session_mod.login_state(
            "linkedin", "https://www.linkedin.com/feed/", one
        ) is True, marker


def test_the_previous_linkedin_frontend_is_still_recognised():
    old = '<html><body><div class="global-nav__me"></div></body></html>'
    assert session_mod.login_state("linkedin", "https://www.linkedin.com/feed/", old) is True


def test_markers_become_selectors_that_match_the_same_thing():
    # A bare token is matched as a substring of class or id, exactly as
    # login_state matches it as a substring of the HTML.
    assert session_mod.marker_selector("global-nav__me") == (
        '[class*="global-nav__me"],[id*="global-nav__me"]'
    )
    # A marker that already names an attribute becomes an attribute selector.
    assert session_mod.marker_selector('data-control-name="identity_welcome_message"') == (
        '[data-control-name="identity_welcome_message"]'
    )
    # A URL fragment is judged from the address; there is nothing to wait for.
    assert session_mod.marker_selector("/uas/login") is None
    assert session_mod.marker_selector("") is None


def test_login_selectors_cover_both_directions_and_no_unknown_site():
    selector = session_mod.login_selectors("linkedin")
    # Signed in ...
    assert '[class*="global-nav__me"]' in selector
    assert '[data-control-name="identity_welcome_message"]' in selector
    # ... and signed out: the first marker either way ends the wait.
    assert '[class*="authwall"]' in selector
    # The URL-only marker contributes nothing to the selector.
    assert "/uas/login" not in selector
    assert selector.count(",") + 1 == len(set(selector.split(",")))  # no duplicates
    assert session_mod.login_selectors("nope") == ""


class _HydratingPage:
    """A page whose markers appear only once someone waits for them."""

    SHELL = '<html><body><div id="app"></div></body></html>'
    HYDRATED = '<html><body><nav class="artdeco-globalnav"></nav></body></html>'

    def __init__(self, url: str, *, hydrates: bool = True):
        self.url = url
        self._hydrates = hydrates
        self.waited_for: list[str] = []
        self._ready = False

    async def goto(self, url: str, wait_until: str = "domcontentloaded"):
        self.url = url
        return None

    async def title(self) -> str:
        return "Feed | LinkedIn"

    def set_default_navigation_timeout(self, ms: int) -> None:
        pass

    async def wait_for_selector(self, selector: str, timeout: int = 0, state: str = ""):
        self.waited_for.append(selector)
        if not self._hydrates:
            raise TimeoutError("no marker appeared")
        self._ready = True
        return object()

    async def content(self) -> str:
        return self.HYDRATED if self._ready else self.SHELL


def _session_on(page):
    live = session_mod.BrowserSession()
    live._context = object()
    live._page = page
    return live


def test_check_login_waits_for_the_marker_instead_of_reading_the_shell():
    page = _HydratingPage("https://www.linkedin.com/feed/")
    result = asyncio.run(_session_on(page).check_login("linkedin"))
    assert page.waited_for, "the shell must not be judged before it has rendered"
    assert result["logged_in"] is True
    assert result["detail"] == "Signed in."


def test_check_login_still_answers_when_no_marker_ever_appears():
    page = _HydratingPage("https://www.linkedin.com/feed/", hydrates=False)
    result = asyncio.run(_session_on(page).check_login("linkedin"))
    assert result["logged_in"] is None
    assert "Could not tell" in result["detail"]


def test_check_login_does_not_wait_when_the_url_already_answered():
    # Redirected to the authwall: waiting for a marker that will never come
    # would only spend the timeout.
    page = _HydratingPage("https://www.linkedin.com/authwall", hydrates=False)

    async def goto(url: str, wait_until: str = "domcontentloaded"):
        return None  # the redirect already happened; page.url stays the authwall

    page.goto = goto
    result = asyncio.run(_session_on(page).check_login("linkedin"))
    assert page.waited_for == [], "a settled URL needs no wait"
    assert result["logged_in"] is False


def test_goto_records_the_url_the_browser_landed_on():
    # pacing.detect_challenge reads "the URL the browser ended on": a redirect
    # to a checkpoint is invisible if goto reports the URL that was asked for.
    page = _HydratingPage("https://www.linkedin.com/feed/")

    async def goto(url: str, wait_until: str = "domcontentloaded"):
        page.url = "https://www.linkedin.com/checkpoint/challenge/"
        return None

    page.goto = goto
    load = asyncio.run(_session_on(page).goto("https://www.linkedin.com/feed/"))
    assert load.url == "https://www.linkedin.com/checkpoint/challenge/"
    assert pacing_mod.detect_challenge(load.url).detected is True


# ---------------------------------------------------------------------------
# FR-205: the target list is closed
# ---------------------------------------------------------------------------


def test_allowlist_is_limited_to_the_plans_targets(db):
    seeker_id = _seeker()
    campaign_id = _campaign(seeker_id)
    _linkedin_plan(
        campaign_id,
        [
            "https://www.linkedin.com/search/results/people/?keywords=head%20of%20data",
            "https://www.linkedin.com/in/jane-doe-123/?trk=public",
        ],
    )

    allowlist = li.build_allowlist(campaign_id)

    assert len(allowlist) == 2
    assert {t.kind for t in allowlist} == {"people_search", "profile"}
    assert allowlist.allows("https://www.linkedin.com/in/jane-doe-123")
    # Tracking parameters do not change identity, but a different profile does.
    assert not allowlist.allows("https://www.linkedin.com/in/someone-else")
    with pytest.raises(li.TargetRefused):
        allowlist.check("https://www.linkedin.com/in/someone-else")
    # Open-ended crawling is refused at the shape level too.
    assert li.classify_target("https://www.linkedin.com/feed/") is None
    assert li.classify_target("https://example.com/in/jane") is None
    with pytest.raises(li.TargetRefused):
        li.make_target("https://www.linkedin.com/mynetwork/")


def test_caps_truncate_the_list_before_the_browser_opens(db, monkeypatch):
    seeker_id = _seeker()
    campaign_id = _campaign(seeker_id)
    monkeypatch.setattr(li, "MAX_TARGETS_PER_RUN", 3)
    _linkedin_plan(
        campaign_id,
        [f"https://www.linkedin.com/in/person-{i}" for i in range(10)],
    )

    assert len(li.build_allowlist(campaign_id)) == 3  # CR-406


# ---------------------------------------------------------------------------
# FR-203: challenge, captcha and rate-limit detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "html", "status", "kind"),
    [
        ("https://www.linkedin.com/in/jane", CAPTCHA_HTML, 200, "captcha"),
        ("https://www.linkedin.com/checkpoint/challenge/x", "", 200, "challenge"),
        ("https://www.linkedin.com/in/jane", "", 429, "rate_limit"),
        ("https://www.linkedin.com/in/jane", "", 999, "challenge"),
        ("https://www.linkedin.com/in/jane", "<p>too many requests</p>", 200, "rate_limit"),
        ("https://www.glassdoor.com/Overview/x", '<div class="g-recaptcha"></div>', 200, "captcha"),
    ],
)
def test_challenge_detection(url, html, status, kind):
    verdict = pacing_mod.detect_challenge(url, html, status=status)
    assert verdict.detected and verdict.kind == kind


def test_an_ordinary_page_is_not_a_challenge():
    assert not pacing_mod.detect_challenge(
        "https://www.linkedin.com/in/jane", PROFILE_HTML, status=200
    ).detected


async def test_a_run_stops_immediately_on_a_challenge(db, virtual_time):
    seeker_id = _seeker()
    targets = [
        li.make_target("https://www.linkedin.com/in/jane-doe-123"),
        li.make_target("https://www.linkedin.com/in/karel-peeters"),
        li.make_target("https://www.linkedin.com/in/third-person"),
    ]
    session = FakeSession(
        {
            targets[0].url: PROFILE_HTML,
            targets[1].url: CAPTCHA_HTML,
            targets[2].url: PROFILE_HTML,
        },
        virtual_time,
    )
    pacing = pacing_mod.Pacing(min_delay_ms=100, max_delay_ms=200, rng=random.Random(1))
    run = pacing_mod.PacedRun(
        site="linkedin",
        session=session,
        pacing=pacing,
        estimator=pacing_mod.DurationEstimator(pacing, site="linkedin", page_load_seconds=1.0),
        job_seeker_id=seeker_id,
        capture=False,
    )

    report = await run.execute(targets, lambda visit: _count(1))

    assert report.challenge is not None and report.challenge.kind == "captcha"
    assert report.stopped_reason and "captcha" in report.stopped_reason
    assert session.visited == [targets[0].url, targets[1].url]  # the third is never opened
    # FR-203: and the user is told.
    notifications = repo.unread_notifications(seeker_id, "browser_challenge")
    assert notifications and "stopped" in notifications[0]["title"]


async def _count(value: int) -> int:
    return value


# ---------------------------------------------------------------------------
# FR-204 / NFR-502: the announced duration is within +/-25% of the real one
# ---------------------------------------------------------------------------


async def test_announced_duration_lands_within_25_percent(db, virtual_time):
    targets = [li.make_target(f"https://www.linkedin.com/in/person-{i}") for i in range(40)]
    session = FakeSession(
        {t.url: PROFILE_HTML for t in targets}, virtual_time, load_seconds=1.5
    )
    pacing = pacing_mod.Pacing(min_delay_ms=2500, max_delay_ms=6000, rng=random.Random(7))
    estimator = pacing_mod.DurationEstimator(
        pacing, site="linkedin", page_load_seconds=1.5, overhead_seconds=0.6
    )

    announced = estimator.estimate(len(targets))
    assert announced.basis == "pacing model"

    async def handler(visit):
        virtual_time.advance(0.55)  # what extraction and the knowledge-base write cost
        return 1

    run = pacing_mod.PacedRun(
        site="linkedin",
        session=session,
        pacing=pacing,
        estimator=estimator,
        capture=False,
    )
    report = await run.execute(targets, handler)

    error = abs(announced.seconds - report.elapsed_seconds) / report.elapsed_seconds
    assert error < 0.25, (
        f"announced {announced.seconds:.0f}s vs actual {report.elapsed_seconds:.0f}s"
    )
    # FR-204: the estimate is refined during execution and ends on measurement.
    assert report.estimate is not None
    assert report.estimate.basis == "measured"
    assert report.estimate.done == len(targets)
    assert announced.human().startswith("about")


def test_the_estimate_is_refined_from_measured_targets():
    pacing = pacing_mod.Pacing(min_delay_ms=1000, max_delay_ms=1000, rng=random.Random(0))
    estimator = pacing_mod.DurationEstimator(
        pacing, page_load_seconds=1.0, overhead_seconds=0.0
    )
    assert estimator.estimate(10).seconds == pytest.approx(40.0)  # 3 waits + 1s load

    for _ in range(5):
        estimator.observe(8.0)
    refined = estimator.refine(targets=10, done=5, elapsed=40.0)

    assert refined.per_target_seconds == pytest.approx(8.0)
    assert refined.seconds == pytest.approx(80.0)
    assert refined.remaining_seconds == pytest.approx(40.0)


# ---------------------------------------------------------------------------
# FR-206: pause, cancel and per-target skip
# ---------------------------------------------------------------------------


async def test_a_skipped_target_is_never_opened(db, virtual_time):
    targets = [li.make_target(f"https://www.linkedin.com/in/person-{i}") for i in range(3)]
    session = FakeSession({t.url: PROFILE_HTML for t in targets}, virtual_time)
    control = pacing_mod.register_run("job-1", "linkedin")
    try:
        assert pacing_mod.request_skip("job-1", targets[1].url) is True
        pacing = pacing_mod.Pacing(min_delay_ms=10, max_delay_ms=20, rng=random.Random(3))
        run = pacing_mod.PacedRun(
            site="linkedin",
            session=session,
            pacing=pacing,
            estimator=pacing_mod.DurationEstimator(pacing, page_load_seconds=1.0),
            control=control,
            capture=False,
        )
        report = await run.execute(targets, lambda visit: _count(1))
    finally:
        pacing_mod.release_run("job-1")

    assert targets[1].url not in session.visited
    assert report.skipped == 1 and report.done == 2
    assert pacing_mod.control_for("job-1") is None


# ---------------------------------------------------------------------------
# NFR-203: no credential, cookie or session token is ever stored
# ---------------------------------------------------------------------------


def test_sanitise_capture_removes_scripts_and_token_assignments():
    cleaned = session_mod.sanitise_capture(PROFILE_HTML)

    assert "9988776655443322" not in cleaned
    assert "SECRET-TOKEN-1" not in cleaned
    assert "Jane Doe" in cleaned  # the readable page survives
    assert session_mod.sanitise_capture('<p>set-cookie: li_at=abc123def</p>') != ""
    assert "abc123def" not in session_mod.sanitise_capture("<p>li_at=abc123def</p>")


def test_sanitise_capture_removes_tokens_carried_in_markup():
    """NFR-203: a logged-in page states its session identifiers as markup too."""
    page = (
        '<meta name="csrf-token" content="ajax:99887766554433">'
        '<input type="hidden" name="authenticity_token" value="XyZ-secret-1234">'
        '<input type="text" name="full_name" value="Jane Doe">'
    )
    cleaned = session_mod.sanitise_capture(page)

    assert "ajax:99887766554433" not in cleaned
    assert "XyZ-secret-1234" not in cleaned
    assert "Jane Doe" in cleaned  # an ordinary field is left alone


def test_credential_fields_are_dropped_before_persistence():
    stripped = session_mod.strip_credential_fields(
        {"full_name": "Jane", "cookie": "li_at=1", "nested": {"session_token": "x", "role": "CTO"}}
    )
    assert stripped == {"full_name": "Jane", "nested": {"role": "CTO"}}
    assert session_mod.safe_url("https://x.com/in/jane?trk=secret") == "https://x.com/in/jane"


async def test_a_run_persists_no_token_anywhere(db, virtual_time):
    seeker_id = _seeker()
    campaign_id = _campaign(seeker_id)
    target = li.make_target("https://www.linkedin.com/in/jane-doe-123")
    session = FakeSession({target.url: PROFILE_HTML}, virtual_time)
    pacing = pacing_mod.Pacing(min_delay_ms=10, max_delay_ms=20, rng=random.Random(5))
    collector = li.LinkedInCollector(
        campaign_id=campaign_id,
        job_seeker_id=seeker_id,
        tally=li.RunTally(max_profiles=10, max_companies=10),
    )
    run = pacing_mod.PacedRun(
        site="linkedin",
        session=session,
        pacing=pacing,
        estimator=pacing_mod.DurationEstimator(pacing, page_load_seconds=1.0),
        job_seeker_id=seeker_id,
        capture=True,
    )

    report = await run.execute([target], collector.handle)
    assert report.records >= 1

    secrets = ("9988776655443322", "SECRET-TOKEN-1", "JSESSIONID")
    dump = _dump_database()
    assert not any(secret in dump for secret in secrets)
    for path in (get_settings().raw_dir / "browser").rglob("*"):
        if path.is_file():
            body = path.read_text(errors="replace")
            assert not any(secret in body for secret in secrets)
    # And the capture that *was* stored is tagged as browser-collected (FR-207).
    docs = query_all("SELECT * FROM raw_document")
    assert docs and all(d["access_method"] == "browser" for d in docs)


def test_no_module_in_the_slice_touches_the_cookie_jar():
    """NFR-203 as a property of the code, not only of one run."""
    forbidden = ("storage_state", "add_cookies", "add_init_script", ".cookies(", "set_cookie")
    for path in BROWSER_PACKAGE.glob("*.py"):
        source = path.read_text()
        for token in forbidden:
            assert token not in source, f"{path.name} must not call {token} (NFR-203)"


# ---------------------------------------------------------------------------
# FR-207 / NFR-303: browser-collected people are campaign-scoped and expire
# ---------------------------------------------------------------------------


async def test_people_are_campaign_scoped_and_expire(db, virtual_time):
    seeker_id = _seeker()
    campaign_id = _campaign(seeker_id)
    target = li.make_target(
        "https://www.linkedin.com/search/results/people/?keywords=head%20of%20data"
    )
    session = FakeSession({target.url: SEARCH_HTML}, virtual_time)
    pacing = pacing_mod.Pacing(min_delay_ms=10, max_delay_ms=20, rng=random.Random(11))
    collector = li.LinkedInCollector(
        campaign_id=campaign_id,
        job_seeker_id=seeker_id,
        tally=li.RunTally(max_profiles=10, max_companies=10),
    )
    run = pacing_mod.PacedRun(
        site="linkedin",
        session=session,
        pacing=pacing,
        estimator=pacing_mod.DurationEstimator(pacing, page_load_seconds=1.0),
        capture=False,
    )

    report = await run.execute([target], collector.handle)
    assert report.records == 2

    contacts = repo.campaign_contacts(campaign_id)
    assert len(contacts) == 2
    for contact in contacts:
        assert contact["access_method"] == "browser"     # FR-207
        assert contact["shareable"] == 0                 # NFR-303
        assert contact["owning_campaign_id"] == campaign_id
        assert contact["retention_until"] > utcnow()
        assert contact["email"] is None                  # CR-402 data minimisation
        assert contact["linkedin_url"].startswith("https://www.linkedin.com/in/")

    # Re-collecting the same people refreshes rather than duplicates.
    await run.execute([target], collector.handle)
    assert len(repo.campaign_contacts(campaign_id)) == 2

    # NFR-303: past the retention date they are deleted.
    future = (datetime.now(UTC) + timedelta(days=3650)).isoformat(timespec="seconds")
    assert len(repo.expired_browser_contacts(future)) == 2
    assert repo.purge_expired_browser_contacts(future) == 2
    assert repo.campaign_contacts(campaign_id) == []


def test_retention_grace_period_is_configurable(db):
    assert repo.retention_grace_days() == repo.DEFAULT_RETENTION_GRACE_DAYS
    assert repo.set_retention_grace_days(7) == 7
    assert repo.retention_grace_days() == 7


# ---------------------------------------------------------------------------
# CR-401: the acknowledgement gate
# ---------------------------------------------------------------------------


async def test_a_run_is_refused_without_the_acknowledgement(db):
    seeker_id = _seeker()
    campaign_id = _campaign(seeker_id)
    _linkedin_plan(campaign_id, ["https://www.linkedin.com/in/jane-doe-123"])

    state = li.acknowledgement_state(seeker_id)
    assert state["acknowledged"] is False
    assert "user agreement" in state["warning"]

    with pytest.raises(auth.ConsentRequired):
        await li.start_run(campaign_id, seeker_id, confirmed=True)

    li.record_acknowledgement(seeker_id, True)
    assert li.acknowledgement_state(seeker_id)["acknowledged"] is True

    # FR-204: acknowledged, but still not confirmed.
    with pytest.raises(li.ConfirmationRequired):
        await li.start_run(campaign_id, seeker_id, confirmed=False)


async def test_the_worker_runs_the_job_and_records_what_it_did(db, virtual_time, monkeypatch):
    """start_run -> job_run -> worker -> knowledge base, with no browser in sight."""
    seeker_id = _seeker()
    campaign_id = _campaign(seeker_id)
    profile_url = "https://www.linkedin.com/in/jane-doe-123"
    _linkedin_plan(campaign_id, [profile_url])
    li.record_acknowledgement(seeker_id, True)  # CR-401

    session = FakeSession({profile_url: PROFILE_HTML}, virtual_time)
    monkeypatch.setattr(li, "BrowserSession", lambda *args, **kwargs: session)
    monkeypatch.setattr(
        li.pacing_mod.Pacing, "from_settings",
        classmethod(lambda cls, **kw: cls(min_delay_ms=10, max_delay_ms=20, rng=random.Random(2))),
    )

    started = await li.start_run(campaign_id, seeker_id, confirmed=True)
    job_id = started["job_id"]
    for _ in range(500):
        if not runner.is_running(job_id):
            break
        await asyncio.sleep(0.005)

    status = li.run_status(job_id, seeker_id)
    assert status["status"] == "done"
    assert status["progress_done"] == 1 and status["progress_total"] == 1
    assert status["report"]["records"] >= 1
    assert status["estimate"]["basis"] == "measured"  # FR-204 refined
    assert session.visited == [profile_url]  # FR-205

    contacts = repo.campaign_contacts(campaign_id)
    assert len(contacts) == 1 and contacts[0]["full_name"] == "Jane Doe"
    companies = query_all("SELECT * FROM company")
    assert companies and companies[0]["access_method"] == "browser"  # FR-207
    # FR-166: every record points back at the plan item that produced it.
    provenance = query_all("SELECT * FROM provenance")
    assert provenance and any(p["adapter_key"] == li.ADAPTER_KEY for p in provenance)


def test_firefox_is_offered_as_an_alternative_driver():
    """FR-208: choosing Firefox always means the persistent context, not CDP."""
    assert session_mod.driver_choice(None, None) == ("cdp", "chromium")
    assert session_mod.driver_choice("cdp", "firefox") == ("persistent", "firefox")
    assert session_mod.driver_choice("persistent", "chromium") == ("persistent", "chromium")
    assert session_mod.driver_choice("nonsense", "nonsense") == ("cdp", "chromium")


async def test_the_run_uses_the_driver_the_user_chose(db, virtual_time, monkeypatch):
    """FR-208: the alternative driver is reachable from the run, not only in theory."""
    seeker_id = _seeker()
    campaign_id = _campaign(seeker_id)
    profile_url = "https://www.linkedin.com/in/jane-doe-123"
    _linkedin_plan(campaign_id, [profile_url])
    li.record_acknowledgement(seeker_id, True)

    session = FakeSession({profile_url: PROFILE_HTML}, virtual_time)
    opened: list[dict] = []

    def _fake_session(*args, **kwargs):
        opened.append(kwargs)
        return session

    monkeypatch.setattr(li, "BrowserSession", _fake_session)
    monkeypatch.setattr(
        li.pacing_mod.Pacing, "from_settings",
        classmethod(lambda cls, **kw: cls(min_delay_ms=10, max_delay_ms=20, rng=random.Random(2))),
    )

    started = await li.start_run(
        campaign_id, seeker_id, confirmed=True, browser_family="firefox"
    )
    for _ in range(500):
        if not runner.is_running(started["job_id"]):
            break
        await asyncio.sleep(0.005)

    assert started["driver"] == "persistent" and started["browser_family"] == "firefox"
    assert opened == [{"driver": "persistent", "family": "firefox"}]


def test_prepare_run_shows_the_list_the_warning_and_the_estimate(db):
    seeker_id = _seeker()
    campaign_id = _campaign(seeker_id)
    _linkedin_plan(campaign_id, ["https://www.linkedin.com/in/jane-doe-123"])

    prepared = li.prepare_run(campaign_id, seeker_id)

    assert prepared["target_count"] == 1
    assert prepared["confirmation_required"] is True
    assert prepared["estimate"]["seconds"] > 0
    assert prepared["acknowledgement"]["acknowledged"] is False


async def test_a_resumed_run_checkpoints_absolute_progress(db, virtual_time):
    """NFR-401: a resumed run must not renumber the targets it already visited.

    A checkpoint counted from the tail of the list would send the next resume
    back to a page the run has already opened, which is exactly the extra
    traffic RK-01 asks us to avoid.
    """
    seeker_id = _seeker()
    campaign_id = _campaign(seeker_id)
    urls = [f"https://www.linkedin.com/in/person-{n}" for n in range(4)]
    job_id = runner.create(
        "browser", campaign_id=campaign_id, job_seeker_id=seeker_id, total=len(urls)
    )
    ctx = JobContext(job_id=job_id, campaign_id=campaign_id, job_seeker_id=seeker_id)
    session = FakeSession(dict.fromkeys(urls, PROFILE_HTML), virtual_time)
    pacing = pacing_mod.Pacing(min_delay_ms=10, max_delay_ms=20, rng=random.Random(3))

    async def _handler(visit):
        return 1

    run = pacing_mod.PacedRun(
        site=li.SITE,
        session=session,
        pacing=pacing,
        estimator=pacing_mod.DurationEstimator(pacing, site=li.SITE),
        ctx=ctx,
        job_seeker_id=seeker_id,
        capture=False,
        done_offset=2,  # the first two targets ran before the restart
    )
    await run.execute([li.make_target(u) for u in urls[2:]], _handler)

    job = repo.get_job(job_id, seeker_id)
    assert job["progress_done"] == 4 and job["progress_total"] == 4
    assert repo.job_state(job_id)["done"] == 4


def test_glassdoor_snapshots_survive_a_later_linkedin_run(db):
    """FR-264/FR-265: both sites share the 'browser' job kind, so the reader

    must pick the Glassdoor run rather than whichever browser run finished last.
    """
    seeker_id = _seeker()
    campaign_id = _campaign(seeker_id)
    gd_job = runner.create(
        "browser",
        campaign_id=campaign_id,
        job_seeker_id=seeker_id,
        adapter_key=glassdoor.ADAPTER_KEY,
    )
    repo.save_job_state(
        gd_job,
        {
            "site": glassdoor.SITE,
            "snapshots": {
                "advisory": True,
                "employers": [{"company_name": "Acme NV", "rating": 3.9, "advisory": True}],
                "salaries": [],
            },
        },
    )
    later = runner.create(
        "browser", campaign_id=campaign_id, job_seeker_id=seeker_id, adapter_key=li.ADAPTER_KEY
    )
    repo.save_job_state(later, {"site": li.SITE})
    update_row("job_run", later, {"created_at": "2099-01-01T00:00:00+00:00"})

    snapshots = glassdoor.snapshots_for_campaign(campaign_id)

    assert snapshots["advisory"] is True
    assert snapshots["employers"][0]["company_name"] == "Acme NV"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_profile_and_search_extraction():
    profile = li.parse_profile(PROFILE_HTML, "https://www.linkedin.com/in/jane-doe-123")
    assert profile["full_name"] == "Jane Doe"
    assert profile["company"] == "Acme NV"
    assert profile["location"] == "Ghent"

    people = li.parse_people_search(SEARCH_HTML, "https://www.linkedin.com/search/results/people/")
    assert [p["full_name"] for p in people] == ["Jane Doe", "Karel Peeters"]
    assert people[0]["profile_url"] == "https://www.linkedin.com/in/jane-doe-123"


def test_glassdoor_money_parsing_and_scope():
    assert glassdoor.parse_money("€55K - €70K") == (55000.0, 70000.0, "EUR")
    assert glassdoor.parse_money("$120,000") == (120000.0, 120000.0, "USD")
    assert glassdoor.parse_money("") == (None, None, None)
    assert glassdoor.classify_target("https://www.glassdoor.com/Overview/x.htm") == "employer"
    assert glassdoor.classify_target("https://www.glassdoor.com/Community/index.htm") is None
    with pytest.raises(glassdoor.TargetRefused):
        glassdoor.make_target("https://www.glassdoor.com/Community/index.htm")


# ---------------------------------------------------------------------------
# FR-328: the ATS form is pre-filled, never submitted
# ---------------------------------------------------------------------------


def test_ats_field_matching_refuses_what_it_must_not_touch():
    assert ats_form.match_field(name="first_name") == "first_name"
    assert ats_form.match_field(name="candidate[email]", input_type="email") == "email"
    assert ats_form.match_field(label="Telefoonnummer") == "phone"
    assert ats_form.match_field(name="job_application[resume]", input_type="file") == "resume"
    # Passwords, consent ticks and hidden fields are left alone.
    assert ats_form.match_field(name="password", input_type="password") is None
    assert ats_form.match_field(name="gdpr_consent", input_type="checkbox") is None
    assert ats_form.match_field(name="authenticity_token", input_type="hidden") is None
    assert ats_form.is_submit_control(text="Submit application") is True
    assert ats_form.is_submit_control(name="first_name") is False


def test_ats_prefill_plan_leaves_the_last_word_to_the_user():
    fields = [
        ats_form.FormField("#name", "full_name", "text", "Name"),
        ats_form.FormField("#agree", None, "checkbox", "I agree to the terms"),
        ats_form.FormField("#why", "cover_letter", "textarea", "Why you?"),
        ats_form.FormField("#notice", "notice_period", "text", "Notice period"),
    ]
    fillable, leave = ats_form.plan_prefill(
        fields, {"full_name": "Jane Doe", "cover_letter": "Dear ..."}
    )

    assert [f.canonical for f in fillable] == ["full_name", "cover_letter"]
    assert {f.selector for f in leave} == {"#agree", "#notice"}
    assert ats_form.PrefillReport(url="https://ats.example/apply").submitted is False


def test_the_ats_module_contains_no_submit_click():
    """FR-328: submitting is the applicant's act, so the code cannot do it."""
    source = (BROWSER_PACKAGE / "ats_form.py").read_text()
    assert ".click(" not in source
    assert "press(\"Enter\")" not in source


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _dump_database() -> str:
    tables = [
        row["name"]
        for row in query_all("SELECT name FROM sqlite_master WHERE type = 'table'")
        if not row["name"].startswith("sqlite_")
    ]
    parts: list[str] = []
    for table in tables:
        try:
            parts.extend(str(row) for row in query_all(f"SELECT * FROM {table}"))
        except Exception:  # noqa: BLE001 - fts shadow tables are not readable this way
            continue
    return "\n".join(parts)
