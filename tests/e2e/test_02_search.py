"""End-to-end suite 02 — search: directives, campaigns, collection, and what it produced.

The narrative is the one section 2.3 of the specification puts in the middle of
the pipeline: a job seeker bounds the search (FR-141..149, FR-385), turns that
into a campaign and reads its plan before anything is fetched (FR-161..166),
watches the collection and keeps control of it (FR-361, NFR-502), and then looks
at whatever came back (FR-281..285, FR-341..345).

Two things this suite refuses to do, because both would make it a worse witness:

* it never drives the API where a screen exists.  The API is used only to put
  the account into the state an earlier suite would have left it in — a profile
  version to pin a campaign to (FR-105), the CR-410 consent the composite screen
  collects, a directive set when this file's own first test has not run;
* it never asserts that an empty screen is fine.  An empty list is a pass only
  when the request behind it succeeded and the screen says what to do next; the
  harness's own record of failed requests is what tells the two apart.

Where a feature genuinely cannot run here — no browser is attached to the
DevTools port, so no LinkedIn session exists — the test asserts that the screen
says so and refuses to start, and records it as degraded rather than skipping.

Marked ``llm``: the two campaign tests.  Planning translates the directives into
each source's own query language with a model call (FR-162) and collection
fetches real pages, so ``-m "not llm"`` leaves a suite that needs neither.
"""

from __future__ import annotations

import time
from typing import Any

import harness
import pytest
from harness import (
    expect_no_console_errors,
    failed_requests,
    narrate,
    open_app,
    open_screen,
    register_fresh_account,
    screenshot,
    sign_in,
    step,
    wait_for_ready,
)

# Planning calls the model and collection fetches real pages; neither fits in
# the 15s default the harness uses for ordinary locators.
SLOW_MS = 90_000

# The directive set this suite works with.  Enough titles and areas that the
# plan has real volume to show, and a shape a person would recognise.
TITLE = "AI Engineer"
EXTRA_TITLE = "Data Engineer"
AREA_QUERY = "Ghent"
EMPLOYER = "Cortexa AI"
EMPLOYER_DOMAIN = "cortexa.example"


# ---------------------------------------------------------------------------
# Account and the state an earlier suite would have left behind
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def account(browser: Any, e2e_config: dict[str, Any], persona: harness.Persona) -> dict[str, str]:
    """One fresh account for the whole suite, registered through the interface.

    Session-scoped on purpose: every test gets its own browser context (no
    cookies carried over), but they are all the *same job seeker*, so the
    directive set the first test saves is still there when the campaign test
    looks for it.  That is how a person uses the product, and it is the only
    way the later screens have anything to show.
    """
    context = browser.new_context(
        viewport=e2e_config["viewport"], locale="en-GB", timezone_id="Europe/Brussels"
    )
    context.set_default_timeout(e2e_config["timeout_ms"])
    page = context.new_page()
    harness.attach(
        page,
        base_url=e2e_config["base_url"],
        api_url=e2e_config["api_url"],
        test_id="test_02_search.py::session setup",
    )
    try:
        with step(page, "Register the job seeker this suite acts as"):
            creds = register_fresh_account(page, persona)

        with step(page, "Put the account where the profile suite would have left it"):
            request = context.request
            base = e2e_config["base_url"]
            # FR-105: a campaign pins a profile version, so one has to exist
            # before the campaign screen will let anything be created.
            request.put(
                f"{base}/api/profile/",
                data={
                    "sections": _profile_sections(persona),
                    "dream_job_statement": str(
                        persona.get("dream_job.statement", "dream_job", default="")
                    )[:2000]
                    or "A role where I can build AI systems that reach production.",
                },
            )
            # CR-410: the composite screen is where this consent is collected.
            # Without it the profile pipeline is degraded, and this suite is not
            # the place to discover that.
            request.post(
                f"{base}/api/auth/consent", data={"kind": "llm_transfer", "granted": True}
            )
            versions = request.get(f"{base}/api/profile/versions").json()
            narrate(f"  · {len(versions)} profile version(s) available to pin a campaign to")
            assert versions, "the profile version the campaign screen needs was not created"
        yield creds
    finally:
        page.close()
        context.close()


def _profile_sections(persona: harness.Persona) -> dict[str, Any]:
    """The persona as the FR-102 section map, minimally.

    Only what a campaign needs to exist at all: this suite is about search, and
    the profile screens are suite 01's subject.
    """
    identity = persona.get("identity", default={}) or {}
    experience = persona.get("experience", default=[]) or []
    return {
        "contact": {
            "name": persona.display_name,
            "headline": identity.get("headline"),
            "location": identity.get("location_full"),
            "email": persona.email,
        },
        "summary": persona.get("summary", default="") or None,
        "top_skills": persona.skills()[:10],
        "experience": [
            {
                "employer": role.get("employer"),
                "title": role.get("title"),
                "location": role.get("location"),
                "start": role.get("start"),
                "end": role.get("end"),
                "current": bool(role.get("current")),
                "summary": role.get("summary"),
            }
            for role in experience[:4]
            if isinstance(role, dict)
        ],
    }


@pytest.fixture
def signed_in(page: Any, account: dict[str, str]) -> Any:
    """A page already inside the application as this suite's job seeker."""
    with step(page, f"Sign in as {account['email']}"):
        sign_in(page, account["email"], account["password"])
    return page


# ---------------------------------------------------------------------------
# Small helpers, named after what a person would say they were doing
# ---------------------------------------------------------------------------


def api(page: Any):
    """The browser context's own HTTP client: same cookies, same origin.

    Used only for setting up state a screen needs, never for asserting that a
    screen works.
    """
    return page.context.request


def base_of(page: Any) -> str:
    return harness.base_url_of(page)


#: Chromium writes a console error for every non-2xx resource, and the SPA's
#: client log shipper (POST /api/logs/client) is deliberately fire-and-forget:
#: its own failure never reaches the person using the app, and it is rate
#: limited per client *host*, so more than one browser on this machine — a
#: second suite, a developer's own tab — shares one bucket and starts taking
#: 429s.  Matched on the source URL, never on the message, so every other
#: failing call still fails the test.
CLIENT_LOG_SHIPPER = ("/api/logs/client",)


def expect_clean_console(page: Any) -> None:
    """No uncaught client error on this screen, the log beacon aside."""
    expect_no_console_errors(page, ignore=CLIENT_LOG_SHIPPER)


def check_help(page: Any, expected_title: str) -> None:
    """The "? Help" control opens the drawer, and the drawer has content.

    Help that is present but empty is the failure this actually catches: the
    panel resolves its text from the route, so a screen the help table does not
    know renders the "no specific guidance" apology instead.
    """
    page.get_by_role("button", name="Help", exact=True).first.click()
    drawer = page.get_by_role("dialog", name="Help")
    drawer.wait_for(state="visible")
    heading = drawer.get_by_role("heading", level=3).first.inner_text().strip()
    assert heading == expected_title, (
        f"the help drawer is titled {heading!r}, not {expected_title!r} — "
        "the route is not in help/content.js"
    )
    assert drawer.get_by_role("heading", name="How to work through it").is_visible(), (
        f"the help for {expected_title!r} opened without steps"
    )
    assert drawer.get_by_role("heading", name="Glossary").is_visible()
    body = drawer.inner_text()
    assert "There is no specific guidance for this screen yet" not in body, (
        f"{expected_title!r} has no help entry of its own"
    )
    assert len(body) > 400, "the help drawer opened but is nearly empty"
    screenshot(page, f"Help drawer — {expected_title}")
    drawer.get_by_role("button", name="Close help").click()
    drawer.wait_for(state="hidden")


def screen_is_alive(page: Any, *, heading: str) -> None:
    """The screen painted its own content: not a spinner, not an error banner."""
    assert page.locator(".spinner").count() == 0, "the screen is still spinning"
    assert page.locator(".skeleton").count() == 0, "the screen is still showing placeholders"
    banner = harness.current_error(page)
    assert banner is None, f"the screen is showing an error instead of itself: {banner}"
    assert page.get_by_role("heading", name=heading).first.is_visible(), (
        f"the {heading!r} heading never appeared"
    )


def group(page: Any, title: str):
    """One collapsible directive group, by the title in its header (FR-141)."""
    return page.locator(".card").filter(
        has=page.get_by_role("button", name=title, exact=True)
    )


def open_group(page: Any, title: str):
    """Expand a directive group and return its card."""
    head = page.get_by_role("button", name=title, exact=True)
    head.wait_for(state="visible")
    if head.get_attribute("aria-expanded") != "true":
        head.click()
    card = group(page, title)
    card.wait_for(state="visible")
    return card


def field(scope: Any, label: str):
    """The ``.field`` block whose label reads ``label``.

    ``Field`` does not bind its ``<label>`` to the control it wraps, so
    ``get_by_label`` finds nothing on this screen; the block is the smallest
    honest anchor, and it is the product's own grouping rather than a class
    invented for the test.
    """
    return scope.locator(".field").filter(has_text=label).first


def pick_chip(scope: Any, label: str) -> None:
    """Click a multi-select chip by its visible label (FR-141)."""
    chip = scope.locator(".chip.clickable").filter(has_text=label).first
    chip.wait_for(state="visible")
    chip.click()
    assert "on" in (chip.get_attribute("class") or ""), f"the {label!r} chip did not take"


def nudge_slider(slider: Any, times: int = 4) -> None:
    """Move a range input the way a person does, since fill() cannot."""
    slider.click()
    for _ in range(times):
        slider.press("ArrowRight")


def ensure_directive_set(page: Any) -> dict[str, Any]:
    """A saved directive set, creating one only if this suite's own test has not.

    A campaign cannot exist without one (FR-161), and a test that is run on its
    own should still have something to plan.
    """
    sets = api(page).get(f"{base_of(page)}/api/directives/").json()
    if sets:
        return sets[0]
    narrate("  · no directive set on the account yet; creating the one a campaign needs")
    created = api(page).post(
        f"{base_of(page)}/api/directives/",
        data={
            "name": "E2E fallback directives",
            "job_content": {"target_titles": [TITLE], "function_families": ["engineering"]},
            "location": {"countries": ["BE"]},
        },
    )
    assert created.ok, f"could not create the fallback directive set: {created.status}"
    return created.json()


def ensure_campaign(page: Any) -> dict[str, Any]:
    """A campaign, creating one only if no earlier test has.

    The browser screen's run planner only appears once a campaign exists — a
    browser run walks a closed list of pages a campaign plan produced (FR-205),
    so without one there is nothing for the gate to gate.
    """
    campaigns = api(page).get(f"{base_of(page)}/api/campaigns").json()
    if campaigns:
        return campaigns[0]
    directives = ensure_directive_set(page)
    versions = api(page).get(f"{base_of(page)}/api/profile/versions").json()
    narrate("  · no campaign on the account yet; creating the one the run planner needs")
    created = api(page).post(
        f"{base_of(page)}/api/campaigns",
        data={
            "name": "E2E fallback campaign",
            "directive_set_id": directives["id"],
            "profile_version_id": versions[0]["id"],
            "caps": {"max_pages": 20},
        },
    )
    assert created.ok, f"could not create the fallback campaign: {created.status}"
    return created.json()


def campaign_status(page: Any, campaign_id: str) -> str:
    body = api(page).get(f"{base_of(page)}/api/campaigns/{campaign_id}/status").json()
    return str(body.get("status") or "")


def wait_for_status(
    page: Any, campaign_id: str, wanted: set[str], *, timeout: float = 10.0
) -> str:
    """Poll the campaign until it reaches one of ``wanted``, or time runs out."""
    deadline = time.monotonic() + timeout
    status = campaign_status(page, campaign_id)
    while status not in wanted and time.monotonic() < deadline:
        page.wait_for_timeout(400)
        status = campaign_status(page, campaign_id)
    return status


def note_log_evidence(page: Any, log_tail: harness.LogTail, *, what: str) -> bool:
    """NFR-701: was what this test just did legible from the server's own logs?

    Anchored on the correlation ids the API put on *this page's* responses, not
    on a message pattern: the log directory is shared with whatever else is
    running against this checkout, and a pattern like ``POST /api/directives``
    can be satisfied by somebody else's line.  A correlation id cannot.

    Not an assertion, deliberately.  Whether the process answering on :8000 is
    the one writing the log files is a property of how it was started, and a
    suite that failed on it would be reporting a deployment fact as a product
    defect.  What it does instead is record, in the run report, whether the
    evidence was there — found, or missing with the reason.
    """
    ids = [cid for cid in harness.correlation_ids(page) if len(cid) >= 6]
    lines = log_tail.new_lines()
    hit = next(
        ((path, line) for path, line in lines for cid in ids if cid in line),
        None,
    )
    harness.RUN.log_checks.append(
        {
            "pattern": f"correlation id from {len(ids)} response(s)",
            "what": what,
            "found": bool(hit),
            "file": str(hit[0]) if hit else None,
            "line": hit[1][:500] if hit else None,
            "at": harness.utcnow_iso(),
        }
    )
    if hit:
        narrate(f"  · log ✓ {what}")
        return True
    detail = (
        "the API is answering with correlation ids but writing no log file"
        if lines
        else f"nothing at all was written to {log_tail.log_dir}"
    )
    narrate(f"  · log ? {what}: not verifiable — {detail}")
    return False


# ---------------------------------------------------------------------------
# Directives (FR-141..149)
# ---------------------------------------------------------------------------


def test_directives_take_every_group_through_structured_controls(
    signed_in: Any, log_tail: harness.LogTail
) -> None:
    """Fill all five groups with the controls FR-141 says are the only ones."""
    page = signed_in

    with step(page, "Open the search directives screen"):
        open_screen(page, "Directives")
        screen_is_alive(page, heading="Search directives")
        assert page.get_by_text("Directives bound the search").first.is_visible(), (
            "the screen introduction did not render"
        )

    with step(page, "Read the help for this screen"):
        check_help(page, "Search directives")

    with step(page, "Start from an empty set rather than a proposal"):
        page.get_by_role("button", name="Start from an empty set").click()
        page.get_by_role("button", name="Job content", exact=True).wait_for(state="visible")

    with step(page, "Name the directive set"):
        name_box = page.locator(".field").filter(has_text="Directive set name").locator("input")
        name_box.fill("Lena's AI engineering search")

    with step(page, "Job content: titles, families, seniority range and skills (FR-142)"):
        card = open_group(page, "Job content")
        titles = card.get_by_placeholder("Start typing a job title")
        titles.click()
        titles.fill(TITLE)
        suggestion = card.locator(".dir-ac-item").first
        suggestion.wait_for(state="visible")
        suggestion.click()
        assert card.locator(".chip.on").filter(has_text=TITLE).count() == 1, (
            "the chosen title did not become a chip"
        )
        # A title the catalogue does not know is still accepted (FR-142).
        titles.click()
        titles.fill(EXTRA_TITLE)
        titles.press("Enter")
        assert card.locator(".chip.on").filter(has_text=EXTRA_TITLE).count() == 1

        pick_chip(field(card, "Function families"), "Software engineering")
        field(card, "Seniority from").locator("select").select_option(label="Senior")
        field(card, "Seniority to").locator("select").select_option(label="Director")
        field(card, "Management scope").locator("select").select_option(label="Team lead")
        field(card, "Minimum direct reports").locator("input").fill("3")

        skills = field(card, "Must-have skills").get_by_placeholder("Add")
        for skill in ("Python", "LLM evaluation"):
            skills.fill(skill)
            skills.press("Enter")
        assert card.locator(".chip.on").filter(has_text="Python").count() >= 1
        field(card, "Nice-to-have skills").get_by_placeholder("Add").fill("Kubernetes")
        field(card, "Nice-to-have skills").get_by_placeholder("Add").press("Enter")
        field(card, "Keywords to avoid").get_by_placeholder("Add").fill("night shift")
        field(card, "Keywords to avoid").get_by_placeholder("Add").press("Enter")

    with step(page, "Company type: headcount bands, stage, trajectory (FR-143)"):
        card = open_group(page, "Company type")
        pick_chip(field(card, "Headcount bands"), "50 to 250 FTE")
        pick_chip(field(card, "Headcount bands"), "250 to 1000 FTE")
        pick_chip(field(card, "Company stage"), "Scale-up")
        pick_chip(field(card, "Trajectory"), "Growing")
        summary = page.get_by_role("button", name="Company type", exact=True).locator(
            "xpath=ancestor::div[contains(@class,'card-header')]"
        )
        assert "size bands" in summary.inner_text(), "the group summary did not follow the choices"

    with step(page, "Location: an area from the geocoder, a radius and a country (FR-144)"):
        card = open_group(page, "Location")
        places = field(card, "Search areas").get_by_placeholder("Town, city or region")
        places.click()
        places.fill(AREA_QUERY)
        # FR-144: the geocoder answering is the good path; it being unreachable
        # is a documented one, and the area then simply carries no radius.
        try:
            card.locator(".dir-ac-item").first.wait_for(state="visible", timeout=8000)
            card.locator(".dir-ac-item").first.click()
            geocoded = True
        except Exception:  # noqa: BLE001 - the fallback is the feature, not a failure
            narrate("  · the geocoder returned nothing; keeping the label as typed")
            card.get_by_role("button", name="Add").click()
            geocoded = False
        area = card.locator(".dir-area").first
        area.wait_for(state="visible")
        if geocoded:
            assert "not geocoded" not in area.inner_text(), (
                "an area picked from the geocoder is marked as not geocoded"
            )
            assert area.locator("input[type='range']").count() == 1
        else:
            assert "not geocoded" in area.inner_text(), (
                "an ungeocoded area must say so — it has no radius filter (FR-144)"
            )
        nudge_slider(area.locator("input[type='range']"), times=3)
        assert "km" in area.inner_text()

        countries = field(card, "Countries").get_by_placeholder("Add")
        countries.fill("NL")
        countries.press("Enter")
        pick_chip(field(card, "Countries"), "BE")

    with step(page, "Work arrangement: hybrid, remote days and travel (FR-145)"):
        card = open_group(page, "Work arrangement")
        pick_chip(field(card, "On-site, hybrid or remote"), "Hybrid")
        pick_chip(field(card, "On-site, hybrid or remote"), "Fully remote")
        remote = field(card, "Minimum remote days per week")
        remote.get_by_role("checkbox").check()
        nudge_slider(remote.locator("input[type='range']"), times=1)
        pick_chip(field(card, "Employment type"), "Full-time")
        pick_chip(field(card, "Contract type"), "Permanent")
        field(card, "Travel tolerance").locator("select").select_option(
            label="Occasional (up to 10%)"
        )

    with step(page, "Compensation: a floor, and the disclosure flag left off (FR-146)"):
        card = open_group(page, "Compensation")
        # FR-146 is explicit that this is off by default, and the badge in the
        # header is the part a person actually sees.
        disclose = card.locator("#dir-disclose-compensation")
        assert not disclose.is_checked(), (
            "FR-146: 'disclose in content' must default to off"
        )
        assert disclose.is_disabled(), (
            "FR-146: the opt-in must be gated by the acknowledgement, not free to tick"
        )
        header = page.get_by_role("button", name="Compensation", exact=True).locator(
            "xpath=ancestor::div[contains(@class,'card-header')]"
        )
        assert "not disclosed" in header.inner_text(), (
            "the compensation header does not say the figure stays out of what is sent"
        )
        assert card.get_by_text(
            "They are never written into a generated CV"
        ).is_visible(), "the FR-146 caution is missing"

        card.get_by_placeholder("No minimum").fill("82000")
        field(card, "Currency").locator("select").select_option("EUR")
        field(card, "Period").locator("select").select_option(label="Per year")
        field(card, "How equity counts").locator("select").select_option(label="Counts partially")
        pick_chip(field(card, "Benefits you will not go without"), "meal vouchers")
        screenshot(page, "Compensation left undisclosed by default")

    with step(page, "The opt-in works, and turning it back off is not locked out"):
        card = group(page, "Compensation")
        disclose = card.locator("#dir-disclose-compensation")
        card.get_by_role("button", name="I understand what disclosing means").click()
        disclose.check()
        assert disclose.is_checked()
        disclose.uncheck()
        assert not disclose.is_checked(), "the flag could not be turned off again"
        assert not disclose.is_disabled(), (
            "the acknowledgement gates the first opt-in, not every later change of mind"
        )

    with step(page, "Write the one free-text field the screen has (FR-141)"):
        notes = page.get_by_placeholder("For example: I would rather join a team")
        notes.fill(
            "I would rather join a team that is rebuilding something than one maintaining it."
        )
        assert page.get_by_text("/ 4000").is_visible()

    with step(page, "Read the pre-launch estimate beside the controls (FR-147)"):
        panel = page.locator(".card").filter(
            has=page.get_by_role("heading", name="Before you launch")
        )
        # The estimate is debounced by 700ms and then recomputed; wait for a
        # number rather than for time to pass.
        panel.get_by_text("Sources", exact=True).wait_for(state="visible", timeout=20_000)
        assert panel.get_by_text("Estimated collection cost").is_visible()
        text = panel.inner_text()
        assert "Queries" in text and "Pages" in text and "Duration" in text, (
            f"the estimate panel is missing its figures: {text[:200]}"
        )

    with step(page, "Save the set"):
        page.get_by_role("button", name="Save directive set").click()
        page.get_by_text("Saved as", exact=False).wait_for(state="visible")
        assert "version 1" in page.get_by_text("Saved as", exact=False).inner_text()

    with step(page, "Reload the saved set from a fresh page load (FR-148)"):
        open_app(page, "/directives")
        saved = page.locator(".dir-setrow").filter(has_text="Lena's AI engineering search")
        saved.wait_for(state="visible")
        assert saved.get_by_text("v1").is_visible()
        saved.get_by_role("button", name="Load").click()
        page.get_by_role("button", name="Job content", exact=True).wait_for(state="visible")
        card = open_group(page, "Job content")
        assert card.locator(".chip.on").filter(has_text=TITLE).count() == 1, (
            "the saved titles did not come back"
        )
        card = open_group(page, "Compensation")
        assert card.get_by_placeholder("No minimum").input_value() == "82000", (
            "the saved compensation floor did not come back"
        )
        assert not card.locator("#dir-disclose-compensation").is_checked(), (
            "the disclosure flag came back on after a reload"
        )

    note_log_evidence(
        page, log_tail, what="the directive set the screen saved appears in the server log"
    )
    expect_clean_console(page)


def _load_the_directive_set(page: Any) -> Any:
    """Open the saved directive set in the editor and return its discretion card."""
    open_screen(page, "Directives")
    ensure_directive_set(page)
    open_app(page, "/directives")
    row = page.locator(".dir-setrow").first
    row.wait_for(state="visible")
    row.get_by_role("button", name="Load").first.click()
    card = page.locator(".dir-discretion")
    card.wait_for(state="visible")
    return card


def _turn_discretion_on(page: Any) -> None:
    """Switch discretion mode on, name the employer, and persist it (FR-385)."""
    card = _load_the_directive_set(page)
    card.get_by_role("checkbox").first.check()
    employer = field(card, "Current employer")
    employer.get_by_placeholder("Employer name").fill(EMPLOYER)
    employer.get_by_placeholder("domain").fill(EMPLOYER_DOMAIN)
    card.get_by_role("button", name="Apply discretion settings now").click()
    page.get_by_text("Discretion settings applied.").wait_for(state="visible")


def _turn_discretion_off(page: Any) -> None:
    """Switch it off again and save, whichever control still works.

    The dedicated "Apply discretion settings now" button is inside the block
    that is made inert while discretion is off, so once the switch is down it
    cannot be pressed — see the test below.  The editor's own save is the way
    out, and the cleanup has to use it or every later test would run with
    discretion still on.
    """
    card = _load_the_directive_set(page)
    if card.get_by_role("checkbox").first.is_checked():
        card.get_by_role("checkbox").first.uncheck()
    page.get_by_role("button", name="Save changes").click()
    page.get_by_text("Saved as", exact=False).wait_for(state="visible")


def test_discretion_mode_can_be_switched_off_with_its_own_control(signed_in: Any) -> None:
    """FR-385: the switch has to work in both directions.

    Turning discretion *on* is the loud half.  Turning it off again is the half
    that matters when the job seeker has changed employer or stopped caring, and
    it goes through the same "Apply discretion settings now" button.
    """
    page = signed_in
    reachable = False
    reason = ""

    with step(page, "Turn discretion mode on and apply it"):
        _turn_discretion_on(page)
        card = page.locator(".dir-discretion")
        assert card.get_by_text("active", exact=True).is_visible()

    try:
        with step(page, "Turn it off again and press the same button"):
            card = page.locator(".dir-discretion")
            card.get_by_role("checkbox").first.uncheck()
            assert card.get_by_text("off", exact=True).is_visible()
            apply_button = card.get_by_role("button", name="Apply discretion settings now")
            assert apply_button.is_enabled(), "the button reports itself as disabled"
            try:
                apply_button.click(timeout=4000)
                page.get_by_text("Discretion settings applied.").wait_for(
                    state="visible", timeout=8000
                )
                reachable = True
            except Exception as exc:  # noqa: BLE001 - the failure is the finding
                reason = str(exc).splitlines()[0]
                narrate(f"  · the apply button could not be used: {reason}")
    finally:
        with step(page, "Leave the account with discretion mode off"):
            _turn_discretion_off(page)
            assert page.locator(".dir-discretion").get_by_text("off", exact=True).is_visible()

    # The button sits inside the block that DiscretionCard makes inert while the
    # switch is down (`pointerEvents: on ? 'auto' : 'none'`), so the one action
    # that would record "I am no longer searching in secret" cannot be taken —
    # and the button does not look disabled either, it is merely dimmed.
    assert reachable, (
        "FR-385: 'Apply discretion settings now' cannot be pressed once discretion "
        "mode is switched off. frontend/src/pages/directives/discretion.jsx wraps "
        "the employer fields, the exclusion lists AND that button in a div with "
        "`pointerEvents: on ? 'auto' : 'none'`, so switching the mode off makes the "
        "control that would save the change inert. It still renders as enabled, so "
        "the click is simply swallowed. "
        + reason
    )
    expect_clean_console(page)


def test_discretion_mode_is_visible_across_the_interface(signed_in: Any) -> None:
    """FR-385: turning discretion on has to be visible everywhere, not just here.

    The point of the feature is that a job seeker who is employed can tell, at a
    glance and on any screen, that the search is being kept away from their
    employer.  A setting you have to go back and check is not that.
    """
    page = signed_in
    shell_bar_seen = False
    overview_notice_seen = False

    with step(page, "Turn discretion mode on and name the current employer"):
        card = _load_the_directive_set(page)
        assert card.get_by_text("What this does, and what it cannot do").is_visible(), (
            "FR-385's honest caution about name matching is missing"
        )
        _turn_discretion_on(page)

    try:
        with step(page, "Ask the rules whether the employer would be caught"):
            card = page.locator(".dir-discretion")
            check = field(card, "Check a company against these rules")
            check.get_by_placeholder("Company name").fill(EMPLOYER)
            check.get_by_role("button", name="Check").click()
            verdict = check.locator(".badge").first
            verdict.wait_for(state="visible")
            assert "excluded" in verdict.inner_text().lower(), (
                f"the named employer is not excluded by its own rules: {verdict.inner_text()}"
            )

        with step(page, "The saved-set list marks the set as discreet"):
            open_app(page, "/directives")
            row = page.locator(".dir-setrow").first
            row.wait_for(state="visible")
            assert row.get_by_text("discreet").is_visible(), (
                "a discreet directive set is not marked in the list"
            )

        with step(page, "Look for the discretion bar on the other screens"):
            for label in ("Where I am", "Campaigns", "Opportunities"):
                open_screen(page, label)
                if page.locator(".discretion-bar").count():
                    shell_bar_seen = True
            open_screen(page, "Where I am")
            overview_notice_seen = page.get_by_text("Discretion mode is on.").count() > 0
            screenshot(page, "Discretion mode, seen from the Overview screen")
    finally:
        with step(page, "Turn discretion mode off again"):
            _turn_discretion_off(page)

    assert overview_notice_seen, "FR-385: the Overview screen did not say discretion mode was on"
    # App.jsx renders a shell-wide discretion bar from `session.discretion_mode`,
    # and GET /api/auth/me never returns that field, so the bar cannot appear on
    # any screen.  Asserted here rather than accommodated: it is the visible half
    # of FR-385.
    assert shell_bar_seen, (
        "FR-385: the application-shell discretion bar never appeared on any screen. "
        "frontend/src/App.jsx:199 renders it from `session.discretion_mode`, but "
        "GET /api/auth/me (backend/dreamjob/api/routers/auth.py::_public) returns "
        "only id, email, display_name, locale, is_admin and created_at — the flag "
        "is never sent, so the bar is unreachable code and the reminder exists only "
        "on the three screens that ask a different endpoint for it."
    )
    expect_clean_console(page)


# ---------------------------------------------------------------------------
# Campaigns (FR-161..166, FR-342, FR-361, NFR-502)
# ---------------------------------------------------------------------------


@pytest.mark.llm
def test_campaign_plan_is_reviewable_before_anything_is_fetched(
    signed_in: Any, log_tail: harness.LogTail
) -> None:
    """FR-163: every source, its query and its cost, and the chance to say no."""
    page = signed_in
    ensure_directive_set(page)

    with step(page, "Open the campaigns screen"):
        open_screen(page, "Campaigns")
        screen_is_alive(page, heading="Campaigns")

    with step(page, "Read the help for this screen"):
        check_help(page, "Campaigns")

    with step(page, "Create a campaign from the saved directives (FR-161)"):
        starter = page.get_by_role("button", name="Create your first campaign")
        (starter if starter.count() else page.get_by_role("button", name="New campaign")).click()
        modal = page.locator(".modal")
        modal.wait_for(state="visible")
        assert modal.get_by_text("Creating a campaign collects nothing").is_visible(), (
            "the create dialog does not say that nothing is fetched yet"
        )
        field(modal, "Name").locator("input").fill("Plan review campaign")
        field(modal, "Maximum pages").locator("input").fill("60")
        modal.get_by_role("button", name="Create and plan").click()
        page.wait_for_url("**/campaigns/*", timeout=SLOW_MS)
        wait_for_ready(page)

    campaign_id = page.url.rstrip("/").rsplit("/", 1)[-1]

    with step(page, "Nothing is planned yet, and the screen says so"):
        empty = page.locator(".empty")
        assert empty.get_by_text("No plan yet").is_visible()
        assert empty.get_by_text("It fetches nothing", exact=False).is_visible(), (
            "the empty plan does not promise that planning fetches nothing"
        )

    with step(page, "Generate the plan (FR-162)"):
        page.get_by_role("button", name="Generate plan").click()
        page.get_by_role("heading", name="Estimated for this plan").wait_for(
            state="visible", timeout=SLOW_MS
        )
        wait_for_ready(page, timeout=SLOW_MS)

    with step(page, "The plan states volume, duration and cost before anything runs"):
        totals = page.locator(".card").filter(
            has=page.get_by_role("heading", name="Estimated for this plan")
        )
        text = totals.inner_text()
        for label in ("Sources to run", "Pages", "Duration", "Cost"):
            assert label in text, f"the plan totals do not show {label!r}: {text[:300]}"
        sources = int(
            totals.locator(".stat")
            .filter(has_text="Sources to run")
            .locator(".stat-value")
            .inner_text()
            .replace(",", "")
        )
        assert sources > 0, "the plan lists no sources at all"
        assert "Nothing has been fetched." in text, (
            "FR-163: the plan does not state that nothing has been fetched yet"
        )
        narrate(f"  · the plan holds {sources} sources")

    with step(page, "Every source carries its own native query (FR-162)"):
        plan = page.locator(".card").filter(
            has=page.get_by_role("heading", name="Source plan")
        )
        items = plan.locator(".cmp-item")
        assert items.count() >= 1, "the source plan is empty"
        first = items.first
        assert first.locator(".cmp-query").count() == 1, "a plan item has no native query"
        query_text = first.locator(".cmp-query").inner_text()
        assert query_text.strip() not in ("", "{}"), (
            f"the first source's native query is empty: {query_text!r}"
        )
        assert "Pages" in first.inner_text()
        screenshot(page, "The source plan, with each source's own query")

    with step(page, "A source can be excluded, and the totals follow (FR-163)"):
        plan = page.locator(".card").filter(
            has=page.get_by_role("heading", name="Source plan")
        )
        target = plan.locator(".cmp-item").first
        name = target.locator("strong").first.inner_text()
        # Clicked rather than unchecked: the box is controlled by the server's
        # answer, so it stays ticked until the PATCH comes back and Playwright's
        # uncheck() would call that a failed click.
        target.get_by_role("checkbox").click()
        plan.locator(".cmp-item.cmp-excluded").filter(has_text=name).first.wait_for(
            state="visible", timeout=SLOW_MS
        )
        totals = page.locator(".card").filter(
            has=page.get_by_role("heading", name="Estimated for this plan")
        )
        after = int(
            totals.locator(".stat")
            .filter(has_text="Sources to run")
            .locator(".stat-value")
            .inner_text()
            .replace(",", "")
        )
        assert after == sources - 1, (
            f"excluding {name!r} did not change the plan totals ({sources} -> {after})"
        )
        assert "1 excluded by you" in totals.inner_text()
        narrate(f"  · excluded {name}")

    with step(page, "The knowledge-base reuse report is there before launch (FR-342)"):
        report = page.locator(".card").filter(
            has=page.get_by_role("heading", name="Reused from the knowledge base")
        )
        report.wait_for(state="visible")
        # The table headings are upper-cased by the stylesheet, and inner_text()
        # returns what is rendered, so the comparison is case-insensitive.
        text = report.inner_text()
        lowered = text.lower()
        assert "not assessed yet" not in lowered, (
            "the reuse assessment did not run with the plan (FR-342)"
        )
        for label in ("collection time saved", "cost saved", "record type"):
            assert label in lowered, f"the reuse report does not report {label!r}"
        assert "reused for this campaign" in lowered and "still to collect" in lowered, (
            "FR-342 asks for the saving per entity type; the table does not show it"
        )
        assert "fresh in the knowledge base" in lowered
        screenshot(page, "Knowledge-base reuse, reported before launch")

    with step(page, "The plan is still reviewable after a reload"):
        open_app(page, f"/campaigns/{campaign_id}")
        page.get_by_role("heading", name="Estimated for this plan").wait_for(state="visible")
        assert page.locator(".cmp-item.cmp-excluded").count() >= 1, (
            "FR-163: the exclusion did not survive a reload"
        )

    note_log_evidence(page, log_tail, what="the plan generation appears in the server log")
    expect_clean_console(page)


@pytest.mark.llm
def test_collection_runs_under_the_seekers_control(
    signed_in: Any, log_tail: harness.LogTail
) -> None:
    """FR-361 and NFR-502: watch it, pause it, resume it, and stop it."""
    page = signed_in
    ensure_directive_set(page)

    with step(page, "Create a campaign to actually run"):
        open_screen(page, "Campaigns")
        starter = page.get_by_role("button", name="Create your first campaign")
        (starter if starter.count() else page.get_by_role("button", name="New campaign")).click()
        modal = page.locator(".modal")
        modal.wait_for(state="visible")
        field(modal, "Name").locator("input").fill("Live collection run")
        # A ceiling wide enough that the run is still moving when the dashboard
        # is looked at, and narrow enough that it is not a crawl (FR-186).
        field(modal, "Maximum pages").locator("input").fill("200")
        modal.get_by_role("button", name="Create and plan").click()
        page.wait_for_url("**/campaigns/*", timeout=SLOW_MS)
        wait_for_ready(page)

    campaign_id = page.url.rstrip("/").rsplit("/", 1)[-1]

    with step(page, "Plan it"):
        page.get_by_role("button", name="Generate plan").click()
        page.get_by_role("heading", name="Estimated for this plan").wait_for(
            state="visible", timeout=SLOW_MS
        )
        wait_for_ready(page, timeout=SLOW_MS)

    with step(page, "The dashboard says nothing has been launched yet"):
        page.get_by_role("button", name="Live dashboard").click()
        assert page.get_by_text("Not launched yet").is_visible(), (
            "the live tab does not distinguish 'not launched' from 'nothing happening'"
        )

    with step(page, "Launch collection, after the confirmation says what it will cost"):
        page.get_by_role("button", name="Launch collection").click()
        modal = page.locator(".modal")
        modal.wait_for(state="visible")
        assert "estimated" in modal.inner_text(), "the launch dialog does not restate the estimate"
        assert "pause, resume or cancel" in modal.inner_text()
        modal.get_by_role("button", name="Launch", exact=True).click()
        modal.wait_for(state="hidden", timeout=SLOW_MS)

    with step(page, "Watch it: progress, per-adapter counts, errors, tokens and cost (FR-361)"):
        dashboard = page.locator(".content-wide")
        page.get_by_text("Records collected").wait_for(state="visible", timeout=SLOW_MS)
        text = dashboard.inner_text()
        for label in (
            "Records collected",
            "Errors",
            "Tokens used",
            "Cost",
            "Elapsed",
            "Estimated remaining",
        ):
            assert label in text, f"the live dashboard does not show {label!r}"
        per_source = page.locator(".card").filter(
            has=page.get_by_role("heading", name="Per source")
        )
        assert per_source.count() == 1, "the per-adapter breakdown is missing (NFR-502)"
        assert per_source.locator(".progress-track").count() >= 1, (
            "no adapter has a progress bar of its own"
        )
        screenshot(page, "The live dashboard while collection is running")

    seen_running = campaign_status(page, campaign_id) == "running"
    narrate(f"  · the campaign was {'still running' if seen_running else 'already finished'}")
    assert seen_running or campaign_status(page, campaign_id) == "completed", (
        "the campaign neither ran nor completed after launch"
    )

    # Pause, resume and cancel are exercised in that order, but a collection
    # that runs out of pages first is not a failure of the controls: what is
    # asserted is that the control was offered while there was something to
    # control, that pressing it was accepted, and that the run ends cleanly.
    with step(page, "Pause it (NFR-502)"):
        pause = page.get_by_role("button", name="Pause", exact=True).first
        if seen_running and pause.count() and pause.is_visible():
            pause.click()
            reached = wait_for_status(page, campaign_id, {"paused", "completed"}, timeout=8)
            assert reached in ("paused", "completed"), (
                f"pause was pressed and the campaign is {reached!r}"
            )
            narrate(f"  · after pause the campaign is {reached}")
        else:
            narrate("  · the run finished before the pause control could be used")

    with step(page, "Resume it (NFR-502)"):
        resume = page.get_by_role("button", name="Resume", exact=True).first
        if resume.count() and resume.is_visible():
            resume.click()
            reached = wait_for_status(page, campaign_id, {"running", "completed"}, timeout=8)
            assert reached in ("running", "completed"), (
                f"resume was pressed and the campaign is {reached!r}"
            )
            narrate(f"  · after resume the campaign is {reached}")
        else:
            narrate("  · nothing to resume: the run was no longer paused")

    with step(page, "Cancel it rather than wait for a full campaign (FR-166)"):
        cancel = page.get_by_role("button", name="Cancel", exact=True).first
        if cancel.count() and cancel.is_visible():
            cancel.click()
            modal = page.locator(".modal")
            modal.wait_for(state="visible")
            assert "Records already collected are kept" in modal.inner_text(), (
                "FR-166: the cancel dialog does not say what happens to what was collected"
            )
            modal.get_by_role("button", name="Cancel collection").click()
            modal.wait_for(state="hidden", timeout=SLOW_MS)
        else:
            narrate("  · the run had already ended; nothing to cancel")

    with step(page, "The job ends cleanly, and the campaign says how"):
        deadline = time.monotonic() + 60
        status = campaign_status(page, campaign_id)
        while status in ("running", "paused") and time.monotonic() < deadline:
            page.wait_for_timeout(1000)
            status = campaign_status(page, campaign_id)
        assert status in ("cancelled", "completed"), (
            f"the collection job did not end cleanly: it is {status!r}"
        )
        narrate(f"  · the run ended as {status}")

        open_app(page, f"/campaigns/{campaign_id}")
        page.get_by_role("button", name="Live dashboard").click()
        wait_for_ready(page)
        shown = page.locator(".content-wide").inner_text()
        assert status in shown, (
            f"the campaign screen does not show the terminal status {status!r}"
        )
        assert page.locator(".progress-track").count() >= 1, (
            "the finished run has no progress bar to show what it managed"
        )
        screenshot(page, f"The run after it ended as {status}")

    with step(page, "The campaigns list records the run"):
        open_screen(page, "Campaigns")
        row = page.locator("tr").filter(has_text="Live collection run").first
        row.wait_for(state="visible")
        row_text = row.inner_text()
        assert status in row_text, f"the list does not show the run's outcome: {row_text}"
        assert "/" in row_text, "the list does not show tokens used against the budget"

    # NFR-701: the run has to be legible from outside the browser too.
    note_log_evidence(page, log_tail, what="the collection run appears in the server log")
    expect_clean_console(page)


# ---------------------------------------------------------------------------
# Browser session (CR-401, FR-201..208)
# ---------------------------------------------------------------------------


def test_browser_session_will_not_start_without_the_terms_acknowledgement(
    signed_in: Any,
) -> None:
    """CR-401: the warning is the first thing on the screen and the gate on the run.

    Nothing here starts automation.  What is checked is that it *cannot* be
    started, and that the reasons are stated rather than implied by a greyed-out
    button.
    """
    page = signed_in
    ensure_campaign(page)

    with step(page, "Open the browser session screen"):
        open_screen(page, "Browser session")
        screen_is_alive(page, heading="Browser session")

    with step(page, "Read the help for this screen"):
        check_help(page, "Browser session")

    with step(page, "The terms warning is the first thing on the screen (CR-401)"):
        warning = page.locator(".alert-warn").first
        warning.wait_for(state="visible")
        text = warning.inner_text()
        assert "LinkedIn prohibits automated access" in text, (
            f"the CR-401 warning is not the first alert on the screen: {text[:200]}"
        )
        assert "your decision to make" in text
        assert "No browser run starts until you accept this" in text, (
            "the warning does not say that it gates the run"
        )
        assert page.get_by_role(
            "button", name="I understand the risk and accept it"
        ).is_visible(), "there is no way to acknowledge the warning"
        assert "Acknowledged." not in text, (
            "a fresh account starts out having already accepted LinkedIn's terms"
        )
        screenshot(page, "CR-401 warning, unacknowledged")

    with step(page, "Without the acknowledgement, the run cannot be started"):
        start = page.get_by_role("button", name="Start run…")
        assert start.is_disabled(), "the run can be started without acknowledging CR-401"
        blockers = page.locator("ul.help-tips").last.inner_text()
        assert "CR-401" in blockers, (
            f"the screen does not name the acknowledgement as a blocker: {blockers}"
        )
        assert "Acknowledge" in blockers

    with step(page, "No browser is attached here, and the screen says so plainly"):
        connection = page.locator(".card").filter(
            has=page.get_by_role("heading", name="Connection")
        )
        connection.wait_for(state="visible")
        state = connection.inner_text()
        assert "No browser attached" in state or "Browser attached" in state, (
            f"the connection card reports neither state: {state[:200]}"
        )
        if "No browser attached" in state:
            # Degraded, as designed: there is no DevTools endpoint in this
            # environment, and FR-201 makes the instructions the answer.
            assert page.get_by_role("heading", name="Launch the browser").is_visible()
            assert connection.get_by_text("Debugging endpoint").is_visible()
            blockers = page.locator("ul.help-tips").last.inner_text()
            assert "No browser is attached" in blockers, (
                "the missing browser is not listed as a reason the run cannot start"
            )
            narrate("  · degraded as designed: no browser is attached to the debugging port")

    with step(page, "Acknowledging removes that one blocker and nothing else"):
        page.get_by_role("button", name="I understand the risk and accept it").click()
        # The withdraw control only exists once the decision is on record, so it
        # is the state change itself rather than a word that was already there.
        page.get_by_role("button", name="Withdraw acknowledgement").wait_for(state="visible")
        blockers = page.locator("ul.help-tips").last.inner_text()
        assert "CR-401" not in blockers, "the acknowledgement was not taken into account"
        assert page.get_by_role("button", name="Start run…").is_disabled(), (
            "acknowledging the terms is not on its own enough to start a run"
        )
        assert "duration estimate first" in blockers, (
            "FR-204: the announced duration must still be confirmed before a run"
        )

    with step(page, "Withdraw it again — the decision has to be reversible (CR-401)"):
        page.get_by_role("button", name="Withdraw acknowledgement").click()
        page.get_by_role("button", name="I understand the risk and accept it").wait_for(
            state="visible"
        )
        blockers = page.locator("ul.help-tips").last.inner_text()
        assert "CR-401" in blockers, "withdrawing the acknowledgement did not re-arm the gate"

    with step(page, "Earlier runs: nothing has ever run here, and the screen teaches"):
        history = page.locator(".card").filter(
            has=page.get_by_role("heading", name="Earlier runs")
        )
        text = history.inner_text()
        if "Getting started" in text:
            assert "Read the terms warning" in text, (
                "the first-run guidance does not repeat what to do first"
            )
        screenshot(page, "Browser session, with no run ever started")

    expect_clean_console(page)


# ---------------------------------------------------------------------------
# What collection produced (FR-341..345, FR-281..285)
# ---------------------------------------------------------------------------


def test_companies_screen_renders_the_knowledge_base_or_teaches(signed_in: Any) -> None:
    """FR-341, FR-345: the shared knowledge base, searchable without a campaign.

    Whatever collection produced is what this reads: the knowledge base belongs
    to no campaign and to no job seeker, so this account sees whatever has ever
    been collected on this installation.  Both outcomes are tested — a list with
    rows, and an empty one that has to teach rather than report.
    """
    page = signed_in

    with step(page, "Open the companies screen"):
        open_screen(page, "Companies")
        screen_is_alive(page, heading="Companies")

    with step(page, "Read the help for this screen"):
        check_help(page, "Companies")

    total = api(page).get(f"{base_of(page)}/api/companies?limit=1").json().get("total", 0)
    narrate(f"  · the shared knowledge base holds {total} companies")

    # An empty list is only a pass if the request behind it worked.
    broken = [r for r in failed_requests(page) if "/api/companies" in r["request_url"]]
    assert not broken, f"the company list request failed: {broken}"

    if total == 0:
        with step(page, "Nothing collected: the empty state has to teach, not report"):
            first_run = page.locator(".first-run")
            first_run.wait_for(state="visible")
            text = first_run.inner_text()
            assert "The knowledge base is empty" in text
            assert "Company profiles arrive when a campaign collects them" in text, (
                "the empty state does not say where company profiles come from"
            )
            assert page.get_by_role("link", name="Run a campaign to fill it").is_visible(), (
                "the empty state offers no way forward"
            )
            assert first_run.locator("ol.help-steps li").count() >= 3, (
                "the empty state lists no steps to work through"
            )
            screenshot(page, "Companies — nothing collected yet")
    else:
        with step(page, "The list shows what was collected, and how old it is (FR-226)"):
            table = page.locator("table")
            table.wait_for(state="visible")
            headers = table.locator("thead").inner_text().lower()
            for column in ("company", "country", "sector", "size", "stage", "trajectory"):
                assert column in headers, f"the list has no {column!r} column (FR-345)"
            assert "freshness" in headers, (
                "FR-226/FR-343: a reused profile has to show how old it is"
            )
            rows = table.locator("tbody tr")
            assert rows.count() >= 1
            first = rows.first.inner_text().lower()
            assert "fresh" in first or "stale" in first, (
                f"the first row carries no freshness mark: {first[:120]}"
            )
            screenshot(page, "Companies — the shared knowledge base")

    with step(page, "Narrow it by size band, stage and trajectory (FR-345)"):
        form = page.locator("form")
        field(form, "Size band").locator("select").select_option("51-200")
        field(form, "Stage").locator("select").select_option("scaleup")
        field(form, "Trajectory").locator("select").select_option("growing")
        wait_for_ready(page)
        assert harness.current_error(page) is None, "filtering produced an error banner"
        assert page.get_by_text("Stage and trajectory narrow").is_visible(), (
            "the screen does not say which filters are page-local and which are not"
        )
        screenshot(page, "Companies — narrowed by size, stage and trajectory")

    with step(page, "Clear the filters again"):
        page.get_by_role("button", name="Clear").click()
        wait_for_ready(page)
        assert harness.current_error(page) is None

    if total:
        with step(page, "Open a company and read its standardised profile"):
            name = page.locator("table tbody tr a").first.inner_text()
            page.locator("table tbody tr a").first.click()
            page.wait_for_url("**/companies/*")
            wait_for_ready(page)
            assert harness.current_error(page) is None, "the company profile failed to load"
            assert page.get_by_role("heading", name=name).first.is_visible(), (
                f"the profile for {name!r} did not render its own name"
            )
            # A tab that carries a count renders it inside the button, so the
            # accessible name is "Financials 5" — matched as a substring.
            for tab in ("Profile", "Financials", "Market and timing"):
                assert page.locator(".tab").filter(has_text=tab).count() >= 1, (
                    f"the company profile has no {tab!r} tab"
                )
            assert page.get_by_role("button", name="Refresh profile").is_visible(), (
                "a stale profile cannot be refreshed from the screen (FR-226)"
            )
            screenshot(page, f"Company profile — {name}")
            page.go_back()
            wait_for_ready(page)

    # Left until last on purpose: it is the one thing on this screen that does
    # not work, and everything above is coverage that would otherwise be lost
    # behind the failure.
    with step(page, "Search for something the knowledge base cannot match"):
        form = page.locator("form")
        field(form, "Search the knowledge base").locator("input").fill(
            "zzq-nothing-matches-this"
        )
        form.get_by_role("button", name="Search").click()
        wait_for_ready(page)
        assert harness.current_error(page) is None, "searching produced an error banner"
        broken = [r for r in failed_requests(page) if "/api/companies" in r["request_url"]]
        assert not broken, f"the search request failed: {broken}"
        screenshot(page, "Companies — a search that matches nothing")
        expect_clean_console(page)
        # A search lands in one of three states, and each has to say something:
        # rows, "no company matches these filters", or "nothing has been
        # collected yet".  Landing in none of them is a blank screen.
        assert page.locator("table, .empty, .first-run").count() >= 1, (
            "a search that matches nothing leaves the screen blank — no rows, no empty "
            "state and no first-run guidance. frontend/src/pages/CompaniesPage.jsx "
            "renders FirstRun only when `total === 0 && !filtered` and Empty only when "
            "`total > 0 && items.length === 0`; a filtered search whose own total is "
            "zero satisfies neither, so the screen shows the form and nothing else"
        )
        assert page.get_by_text("No company matches these filters").is_visible(), (
            "the screen does not say that the filters, rather than the knowledge "
            "base, are why there is nothing to show"
        )


def test_opportunities_screen_renders_the_ranked_list_or_teaches(signed_in: Any) -> None:
    """FR-281..285: the ranked list, its filters, and what it says when it is empty."""
    page = signed_in

    with step(page, "Open the opportunities screen"):
        open_screen(page, "Opportunities")
        screen_is_alive(page, heading="Opportunities")

    with step(page, "Read the help for this screen"):
        check_help(page, "Opportunities")

    with step(page, "The score is advisory, and the screen says so first (NFR-305)"):
        advisory = page.locator(".alert-warn").first
        advisory.wait_for(state="visible")
        assert "advisory" in advisory.inner_text().lower(), (
            "the advisory-score caution is not the first thing on the screen"
        )

    listing = api(page).get(f"{base_of(page)}/api/opportunities?limit=1").json()
    total = listing.get("total", 0)
    narrate(f"  · the ranked list holds {total} opportunities")

    broken = [r for r in failed_requests(page) if "/api/opportunities" in r["request_url"]]
    assert not broken, f"the opportunity list request failed: {broken}"

    if total == 0:
        with step(page, "Nothing ranked: the empty state has to teach, not report"):
            first_run = page.locator(".first-run")
            first_run.wait_for(state="visible")
            text = first_run.inner_text()
            assert "Nothing has been ranked yet" in text
            assert "once a campaign has collected vacancies" in text, (
                "the empty state does not say where opportunities come from"
            )
            assert first_run.locator("ol.help-steps li").count() >= 3
            assert page.get_by_role("button", name="Build the list from this campaign").count() or (
                page.get_by_role("link", name="Open campaigns").count()
            ), "the empty state offers no way forward"
            screenshot(page, "Opportunities — nothing ranked yet")

    with step(page, "Sort, filter and search the list (FR-283)"):
        field(page, "Sort").locator("select").select_option("dream_fit")
        wait_for_ready(page)
        page.get_by_placeholder("e.g. Colruyt, platform, Ghent").fill("engineer")
        page.locator("select").filter(has_text="Vacancies and speculative openings").select_option(
            "speculative"
        )
        nudge_slider(page.locator("input[type='range']").first, times=2)
        wait_for_ready(page)
        assert harness.current_error(page) is None, "filtering produced an error banner"
        body = page.locator(".content-wide").inner_text()
        assert "Nothing matches these filters" in body or "opportunit" in body, (
            "a filtered list says nothing about what it did"
        )
        if "Nothing matches these filters" in body:
            assert page.get_by_role("button", name="Clear the filters").is_visible(), (
                "the filtered-empty state offers no way back"
            )
        screenshot(page, "Opportunities — filtered")

    with step(page, "Hand-ordering explains why it is unavailable (FR-284)"):
        note = page.locator(".alert-info").first
        assert "Hand-ordering needs one campaign" in note.inner_text() or (
            "Drag the ⠿ handle" in note.inner_text()
        ), "the screen does not explain the state of hand-ordering"

    if total:
        with step(page, "Open the first opportunity"):
            page.locator("a[href^='/opportunities/']").first.click()
            page.wait_for_url("**/opportunities/*")
            wait_for_ready(page)
            assert harness.current_error(page) is None
            screenshot(page, "An opportunity in detail")

    expect_clean_console(page)
