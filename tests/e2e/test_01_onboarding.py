"""Onboarding, end to end: an empty account to a confirmed dream job.

This is the path every job seeker walks first, and the one that decides whether
they walk any of the others: register, land on "Where I am", import a LinkedIn
export and a CV, settle what the two documents disagree about (FR-103), tidy
the profile, decide what must never leave the building (FR-106), consent to the
transfer that the synthesis needs (CR-410), build the composite profile
(FR-121, FR-125) and turn the free-text statement into a confirmed model
(FR-109, FR-128).

The suite drives the SPA, never the API.  The one thing it asks the persona
files for is the two documents to upload and the two disagreements they were
built to contain, so the conflict assertions test the merge rather than
restating whatever the merge happened to produce.

Ordering matters here in a way it does not in a unit suite: each test continues
the account the one before it left.  ``_signed_in`` registers on first use and
signs the same account back in afterwards, so the file runs top to bottom and a
single test can still be run on its own — it simply starts from an emptier
account and says so through its own assertions.

The three tests that need the model are marked ``llm``: they call DeepSeek and
take one to three minutes each, so ``-m "not llm"`` runs the whole non-LLM half
of onboarding in about fifteen seconds.  The marker is not declared in
``pyproject.toml``'s ``[tool.pytest.ini_options]``; that file belongs to the
project rather than to this suite, and selection works without the
declaration — adding ``markers = ["llm: needs a live model call"]`` there would
only silence a possible strict-marker complaint.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import harness
import pytest
from harness import (
    Persona,
    console_errors,
    expect_no_console_errors,
    failed_requests,
    open_app,
    open_screen,
    register_fresh_account,
    screenshot,
    sign_in,
    step,
    wait_for_ready,
)
from playwright.sync_api import expect

PERSONA_DIR = Path(__file__).resolve().parent / "persona" / "out"
LINKEDIN_PDF = PERSONA_DIR / "persona_linkedin.pdf"
CV_DOCX = PERSONA_DIR / "persona_cv.docx"

#: A composite synthesis measured at ~170 s and a dream-job extraction at ~80 s
#: against DeepSeek's reasoning model, so the usual 15 s locator timeout is not
#: remotely enough.  Generous rather than tight: a slow model is not a defect,
#: and a test that gives up early reports the wrong thing.
LLM_TIMEOUT_MS = 420_000

#: The credentials this file's first test creates, reused by the rest.
ACCOUNT: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------
#
# ``expect_no_console_errors`` matches its ``ignore`` patterns against the
# message *and* its source, which is right for "this call is allowed to fail"
# but too coarse here: several screens ask for a resource that does not exist
# yet (an unbuilt composite, a profile before the first upload) and render
# their first-run card from the 404.  Ignoring the URL would also hide a 500
# from the same endpoint, which is the failure that actually matters.  So the
# status is part of the match.


def _status_of(entry: dict[str, Any]) -> int | None:
    """The HTTP status behind one recorded console error or failed request."""
    if entry.get("status") is not None:
        return int(entry["status"])
    match = re.search(r"status of (\d{3})", entry.get("text", ""))
    return int(match.group(1)) if match else None


def _url_of(entry: dict[str, Any]) -> str:
    return entry.get("request_url") or entry.get("source", "")


def _contracted(entry: dict[str, Any], allow: tuple[tuple[str, int], ...]) -> bool:
    url, status = _url_of(entry), _status_of(entry)
    return any(fragment in url and status == code for fragment, code in allow)


#: ``POST /api/logs/client`` answering 429 is a defect, but not one belonging
#: to any screen this suite drives, and not one the job seeker can see: the
#: browser logger is deliberately silent about its own failures, so a refused
#: shipment costs observability (NFR-701) and nothing else.  The limiter buckets
#: by a hash of the client IP rather than by the ``client_id`` the payload
#: already carries, so every tab on one host — and every e2e suite running
#: beside this one — shares the same 30-batches-a-minute allowance.  It is
#: counted and narrated here rather than failing whichever screen happened to be
#: open when the shared bucket ran out.
CLIENT_LOG_THROTTLED = ("/api/logs/client", 429)


def expect_clean(page, *, allow: tuple[tuple[str, int], ...] = ()) -> None:
    """Fail unless the browser only reported failures this screen contracted for.

    ``allow`` is a list of ``(url fragment, status)`` pairs.  A 404 the SPA
    asks for on purpose is not a defect; a 500 from the same endpoint is, which
    is why the pair and not the URL is the unit.
    """
    allow = (*allow, CLIENT_LOG_THROTTLED)
    throttled = [r for r in failed_requests(page) if _contracted(r, (CLIENT_LOG_THROTTLED,))]
    if throttled:
        harness.narrate(
            f"  · {len(throttled)} client log shipment(s) were refused with 429 — the browser's "
            "own observability was dropped while this screen was working (NFR-701)"
        )

    bad_requests = [r for r in failed_requests(page) if not _contracted(r, allow)]
    bad_console = [e for e in console_errors(page) if not _contracted(e, allow)]

    if bad_requests:
        lines = "\n".join(
            f"  {r['method']} {r['request_url']} -> {r['status'] or r['reason']}"
            f" (on {r['screen']}, during {r['step'] or 'no step'})"
            for r in bad_requests
        )
        raise AssertionError(f"{len(bad_requests)} unexpected failed request(s):\n{lines}")

    # Still run the harness's own check, so the run report records it, with the
    # contracted failures named by the exact source they came from.
    ignore = tuple(
        {e["source"] for e in console_errors(page) if _contracted(e, allow) and e.get("source")}
    )
    expect_no_console_errors(page, ignore=ignore)
    assert not bad_console, f"unexpected client error(s): {bad_console}"


def expect_screen(page, heading: str) -> None:
    """The screen painted its own content — not a spinner, not an error boundary.

    ``exact`` matters: the first-run card on an empty screen is headed
    "Getting started with <screen>", so a substring match would be satisfied by
    the very state this check is meant to tell apart from a rendered screen.
    """
    expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
    banner = harness.current_error(page)
    assert banner is None, f"{heading!r} is showing an error banner: {banner}"
    # ErrorBoundary swaps the whole screen for this; a locator that matched
    # anyway would otherwise let a crashed screen pass.
    assert page.get_by_text("Something went wrong").count() == 0, (
        f"{heading!r} rendered an error boundary rather than the screen"
    )


def expect_help(page, title: str) -> None:
    """The "? Help" control opens a drawer, and the drawer has content in it."""
    page.get_by_role("button", name="Help", exact=True).click()
    drawer = page.get_by_role("dialog", name="Help")
    expect(drawer).to_be_visible()
    expect(drawer.get_by_role("heading", name=title)).to_be_visible()
    expect(drawer.get_by_role("heading", name="Glossary")).to_be_visible()
    body = " ".join(drawer.inner_text().split())
    assert len(body) > 400, f"the help drawer for {title!r} is nearly empty: {body!r}"
    assert "There is no specific guidance for this screen yet" not in body, (
        f"{title!r} has no help written for it"
    )
    screenshot(page, f"help drawer — {title}")
    drawer.get_by_role("button", name="Close help").click()
    expect(drawer).to_have_count(0)


# ---------------------------------------------------------------------------
# The account this file walks through
# ---------------------------------------------------------------------------


def _signed_in(page, persona: Persona) -> dict[str, str]:
    """Sign the suite's account in, registering it the first time.

    Registering once and signing back in afterwards is what lets the file read
    as one narrative while each test still gets the fresh browser context the
    conftest hands out.
    """
    if ACCOUNT:
        sign_in(page, ACCOUNT["email"], ACCOUNT["password"])
    else:
        ACCOUNT.update(register_fresh_account(page, persona))
    return ACCOUNT


def _open_profile_tab(page, label: str) -> None:
    """Open one of the profile workspace's tabs by its visible label."""
    page.get_by_role("button", name=label).first.click()
    wait_for_ready(page)


#: The nearest enclosing ``card`` — matching the class as a whole token, so it
#: is the card and not the ``card-header`` two nodes up from the heading.
_CARD_XPATH = (
    "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' card ')][1]"
)


def _card_for(page, heading: str):
    """The card that owns a heading.

    Cards carry no landmark role, so the heading is the only stable anchor for
    "the block about deal-breakers" as opposed to "the word deal-breaker
    anywhere on a long screen".
    """
    return page.get_by_role("heading", name=heading).first.locator(_CARD_XPATH)


# ---------------------------------------------------------------------------
# 1 — Registration
# ---------------------------------------------------------------------------


def test_the_sign_in_screen_explains_the_password_policy(page, persona):
    """NFR-202: a refused password has to say what would be accepted."""
    with step(page, "Open the application for the first time"):
        open_app(page)
        # The brand on this card is a logo image, so the tagline and the
        # submit button are what say the sign-in card actually painted.
        expect(page.get_by_text("AI-assisted job discovery and application platform")).to_be_visible()
        expect(page.get_by_role("button", name="Sign in", exact=True)).to_be_visible()
        page.get_by_role("button", name="Create an account").click()

    with step(page, "Read the password rule before typing one"):
        hint = page.get_by_text(re.compile("At least 9 characters combining three of"))
        expect(hint).to_be_visible()
        wording = " ".join(hint.inner_text().split())
        for expected in ("lower case", "upper case", "digits", "symbols"):
            assert expected in wording, f"the password hint never mentions {expected}: {wording}"

    with step(page, "Offer a password that is too short"):
        page.locator("input[autocomplete='name']").fill(persona.display_name)
        page.locator("input[type='email']").fill("policy-probe@example.com")
        page.locator("input[type='password']").fill("short1!")
        page.get_by_role("button", name="Create account", exact=True).click()
        refusal = page.locator(".alert-danger")  # the banner carries no role
        expect(refusal).to_be_visible()
        text = " ".join(refusal.inner_text().split())
        assert "9 characters" in text, f"the refusal does not say how long it must be: {text}"

    with step(page, "Offer a long password made of one character class"):
        page.locator("input[type='password']").fill("lowercaseonly")
        page.get_by_role("button", name="Create account", exact=True).click()
        refusal = page.locator(".alert-danger")
        expect(refusal).to_be_visible()
        text = " ".join(refusal.inner_text().split())
        assert "three of" in text, f"the refusal does not say what a strong password is: {text}"
        for expected in ("lower case", "upper case", "digits", "symbols"):
            assert expected in text, f"the refusal never mentions {expected}: {text}"

    # A refused registration is the point of the test, so the 422s are the
    # contract, not a defect.
    expect_clean(page, allow=(("/api/auth/register", 422),))


def test_the_persona_registers_and_lands_in_the_application(page, persona, log_tail):
    """FR-101: a new account starts in its own empty private space."""
    with step(page, "Register the persona's account"):
        credentials = _signed_in(page, persona)
        assert harness.is_signed_in(page), "registration did not land in the application shell"

    with step(page, "Check the shell knows who signed in"):
        expect(page.get_by_text(credentials["email"], exact=True)).to_be_visible()
        expect(page.get_by_text(persona.display_name, exact=True).first).to_be_visible()
        assert "/overview" in page.url, f"registration landed on {page.url}, not the overview"

    with step(page, "Check the account and the session were recorded", shot=False):
        # NFR-702: an account coming into existence is exactly the kind of event
        # the audit trail exists for.
        log_tail.assert_contains(
            "action=job_seeker.registered", what="the new account was audited (NFR-702)"
        )
        log_tail.assert_contains(
            "action=session.started", what="the session was audited (NFR-702)"
        )
        # NFR-701: and every response the SPA got is traceable back to a line.
        assert harness.correlation_ids(page), (
            "no response carried a correlation id, so nothing the browser saw can be "
            "matched to a server log line (NFR-701)"
        )

    expect_clean(page)


# ---------------------------------------------------------------------------
# 2 — Where I am
# ---------------------------------------------------------------------------


def test_where_i_am_shows_an_honest_journey_map_for_an_empty_account(page, persona):
    """The map has to say what is ready and what is waiting, and on what."""
    with step(page, "Sign in and land on Where I am"):
        _signed_in(page, persona)
        open_screen(page, "Where I am")
        expect_screen(page, "Where I am")

    with step(page, "Read the whole process on the map"):
        # The map's stage nodes are the only elements in the SPA carrying a
        # `title`, and it is the state label — a semantic anchor that survives
        # restyling far better than the class name beside it.
        ready = page.get_by_title("Ready to start")
        blocked = page.get_by_title("Waiting on an earlier stage")
        assert ready.count() >= 1, "nothing at all is ready on a brand-new account"
        assert blocked.count() >= 8, (
            f"only {blocked.count()} stages are blocked; a fresh account cannot "
            "collect, score or apply yet"
        )
        assert page.get_by_title("Complete").count() == 0, (
            "a brand-new account is showing finished stages"
        )
        first = " ".join(ready.first.inner_text().split())
        assert "Profile intake" in first, f"the first ready stage is {first!r}, not profile intake"

    with step(page, "Check every blocked stage says what it is waiting for"):
        reasons = [" ".join(t.split()) for t in blocked.all_inner_texts()]
        for reason in reasons:
            assert "Needs " in reason, f"a blocked stage gives no reason: {reason!r}"
        joined = " | ".join(reasons)
        assert "Needs a profile first" in joined, joined
        assert "Needs a composite profile first" in joined, joined
        assert "Needs directives first" in joined, joined

    with step(page, "Follow the suggested next action"):
        expect(page.get_by_text("Import your LinkedIn export and CV")).to_be_visible()

    with step(page, "Open the help for this screen"):
        expect_help(page, "Where I am")

    expect_clean(page)


# ---------------------------------------------------------------------------
# 3 — Documents and the planted conflicts
# ---------------------------------------------------------------------------


def _planted_conflicts(persona: Persona) -> list[dict[str, Any]]:
    planted = persona.get("planted_conflicts", default=[]) or []
    assert len(planted) == 2, (
        f"{harness.PERSONA_PATH} records {len(planted)} planted conflicts; this suite "
        "was written against the two the renderer plants (FR-103)"
    )
    return list(planted)


def test_the_profile_screen_imports_both_documents(page, persona, log_tail):
    """FR-102, FR-103, DR-102: the export defines the shape, the CV is merged in."""
    _signed_in(page, persona)

    with step(page, "Open the profile workspace"):
        open_screen(page, "Profile")
        expect_screen(page, "Profile")
        expect(page.get_by_text("Getting started with your profile")).to_be_visible()

    with step(page, "Read the CR-410 notice before anything is uploaded"):
        # The wording has to be on screen before the transfer, not after it.
        notice = page.get_by_text("Your profile leaves the EU only if you allow it")
        expect(notice).to_be_visible()
        consent_card = notice.locator("xpath=ancestor::div[contains(@class,'alert')][1]")
        wording = " ".join(consent_card.inner_text().split())
        assert "outside the European Union" in wording, wording
        assert "do not disclose" in wording, wording
        expect(consent_card.get_by_role("button", name="I consent to this transfer")).to_be_visible()

    with step(page, "Upload the LinkedIn export"):
        assert LINKEDIN_PDF.is_file(), f"no persona export at {LINKEDIN_PDF}"
        zone = page.get_by_role("button", name=re.compile("LinkedIn export"))
        zone.locator("input[type='file']").set_input_files(str(LINKEDIN_PDF))
        expect(page.get_by_text(LINKEDIN_PDF.name).first).to_be_visible(timeout=60_000)
        expect(page.get_by_text("version · saved from linkedin_pdf")).to_be_visible()

    with step(page, "Upload the CV"):
        assert CV_DOCX.is_file(), f"no persona CV at {CV_DOCX}"
        zone = page.get_by_role("button", name=re.compile(r"CV \(PDF or DOCX\)"))
        zone.locator("input[type='file']").set_input_files(str(CV_DOCX))
        expect(page.get_by_text(CV_DOCX.name).first).to_be_visible(timeout=60_000)

    with step(page, "Check both originals were retained (DR-102)"):
        originals = _card_for(page, "Retained originals")
        text = " ".join(originals.inner_text().split())
        assert LINKEDIN_PDF.name in text and CV_DOCX.name in text, text
        assert "LinkedIn export" in text and "CV" in text, text

    with step(page, "Check the version history records both imports (FR-105)"):
        history = _card_for(page, "Version history")
        rows = history.locator("tbody tr")
        assert rows.count() >= 2, (
            f"two uploads produced {rows.count()} version(s); FR-105 wants one per import"
        )
        expect(history.get_by_text("current")).to_be_visible()

    with step(page, "Check both imports were logged (NFR-701)", shot=False):
        # No other suite uploads a CV, so a line naming this endpoint written
        # since this test started can only be this test's doing.
        log_tail.assert_contains(
            "POST /api/profile/uploads/linkedin", what="the export import was logged"
        )
        log_tail.assert_contains("POST /api/profile/uploads/cv", what="the CV import was logged")

    with step(page, "Open the help for the profile screen"):
        expect_help(page, "Your profile")

    # GET /api/profile/ 404s until the first upload lands — that is the contract
    # the first-run card is rendered from (ProfilePage.loadProfile).
    expect_clean(page, allow=(("/api/profile/", 404),))


def test_the_two_planted_disagreements_are_detected_and_settled(page, persona, log_tail):
    """FR-103: the merge does not choose — it records both values and asks."""
    _signed_in(page, persona)
    planted = _planted_conflicts(persona)

    with step(page, "Open the conflict queue"):
        open_screen(page, "Profile")
        expect_screen(page, "Profile")
        tab = page.get_by_role("button", name=re.compile(r"^Conflicts"))
        assert "2" in tab.inner_text(), (
            f"the Conflicts tab shows {tab.inner_text()!r}; the persona's documents "
            "were built to disagree about exactly two fields"
        )
        tab.click()
        wait_for_ready(page)

    with step(page, "Check each planted disagreement is there, with both readings"):
        banner = " ".join(page.locator(".alert-danger").first.inner_text().split())
        assert "2 disagreements still to settle" in banner, banner
        for conflict in planted:
            path = conflict["expected_field_path"]
            expect(page.get_by_text(path, exact=True)).to_be_visible()
            row = page.locator(".conflict").filter(has_text=path)
            text = " ".join(row.inner_text().split())
            assert conflict["value_linkedin"] in text, (
                f"{path}: the LinkedIn reading {conflict['value_linkedin']!r} is not shown"
            )
            assert conflict["value_cv"] in text, (
                f"{path}: the CV reading {conflict['value_cv']!r} is not shown"
            )
            assert "needs a decision" in text.lower(), (
                f"{path} is not presented as needing a decision: {text}"
            )

    employer, date = planted[0], planted[1]

    with step(page, f"Settle {employer['expected_field_path']} in favour of the CV"):
        # The CV carries the legal entity, which is what an employer's own
        # records will say.
        row = page.locator(".conflict").filter(has_text=employer["expected_field_path"])
        row.get_by_text(employer["value_cv"], exact=True).click()
        expect(row.get_by_text("needs a decision")).to_have_count(0)
        expect(row.get_by_text("cv", exact=True)).to_be_visible()

    with step(page, f"Settle {date['expected_field_path']} in favour of the export"):
        row = page.locator(".conflict").filter(has_text=date["expected_field_path"])
        row.get_by_text(date["value_linkedin"], exact=True).click()
        expect(row.get_by_text("needs a decision")).to_have_count(0)
        expect(row.get_by_text("linkedin", exact=True)).to_be_visible()
        expect(page.get_by_text("2 of 2 settled")).to_be_visible()

    with step(page, "Write the decisions into a new profile version"):
        page.get_by_role("button", name="Write decisions into a new version").click()
        expect(page.get_by_text(re.compile("Written into version"))).to_be_visible(timeout=60_000)

    with step(page, "Check the profile no longer carries a contested value"):
        expect(page.get_by_text("unresolved conflicts")).to_be_visible()
        stat = page.get_by_text("unresolved conflicts").locator("xpath=ancestor::*[1]")
        assert "0" in " ".join(stat.inner_text().split()), (
            f"conflicts were applied but the count still reads {stat.inner_text()!r}"
        )

    with step(page, "Check the chosen employer reached the sections"):
        _open_profile_tab(page, "Sections")
        values = page.locator("input").evaluate_all("els => els.map(e => e.value)")
        assert employer["value_cv"] in values, (
            f"the resolved employer {employer['value_cv']!r} is not in the profile; "
            f"the sections hold {[v for v in values if 'Volta' in str(v)]}"
        )

    with step(page, "Check the decisions were logged (NFR-701)", shot=False):
        log_tail.assert_contains(
            "/api/profile/conflicts/", what="each resolution reached the server"
        )
        log_tail.assert_contains(
            "POST /api/profile/conflicts/apply", what="the new version was written"
        )

    expect_clean(page)


# ---------------------------------------------------------------------------
# 4 — The rest of the profile workspace
# ---------------------------------------------------------------------------


def test_every_profile_tab_renders_its_own_content(page, persona):
    """FR-104..107, FR-441, FR-442: each tab is a workspace, not a placeholder."""
    _signed_in(page, persona)

    with step(page, "Open the profile workspace"):
        open_screen(page, "Profile")
        expect_screen(page, "Profile")

    with step(page, "Sections — the structured profile (FR-104)"):
        _open_profile_tab(page, "Sections")
        for heading in ("Contact", "Summary", "Experience", "Education", "Languages"):
            expect(page.get_by_role("heading", name=heading).first).to_be_visible()
        expect(page.get_by_role("button", name="Save as a new version")).to_be_visible()
        screenshot(page, "profile — sections")

    with step(page, "Skills — normalised, because matching runs on the label (FR-107)"):
        _open_profile_tab(page, "Skills")
        expect(page.get_by_text(re.compile(r"\d+ skills? derived from version"))).to_be_visible()
        table = page.locator("table").first
        expect(table.get_by_text("As written")).to_be_visible()
        expect(table.get_by_text("Normalised")).to_be_visible()
        rows = table.locator("tbody tr")
        assert rows.count() >= 5, f"only {rows.count()} skills were derived from two documents"
        listed = " ".join(table.inner_text().split())
        known = [s for s in persona.skills() if s in listed]
        assert len(known) >= 3, (
            f"the skill list does not look like this persona's: matched {known} out of "
            f"{persona.skills()[:8]}"
        )
        screenshot(page, "profile — skills")

    with step(page, "Evidence — proof a CV can point at (FR-441)"):
        _open_profile_tab(page, "Evidence")
        expect(page.get_by_text("Proof you can point at")).to_be_visible()
        expect(page.get_by_role("button", name="Add evidence")).to_be_visible()
        # Nothing has been attached yet, so the empty state is the correct
        # rendering — and it explains what an evidence item is.
        expect(page.get_by_text("No evidence yet")).to_be_visible()
        screenshot(page, "profile — evidence")

    with step(page, "Personas — one career, more than one honest story (FR-442)"):
        _open_profile_tab(page, "Personas")
        expect(page.get_by_text("One career, more than one honest story")).to_be_visible()
        expect(page.get_by_role("button", name="New persona")).to_be_visible()
        expect(page.get_by_text("No personas yet")).to_be_visible()
        screenshot(page, "profile — personas")

    expect_clean(page)


def test_a_field_marked_do_not_disclose_is_recorded_and_stays_recorded(page, persona, log_tail):
    """FR-106: a flag is a guarantee about egress, not a display preference."""
    _signed_in(page, persona)

    with step(page, "Open the privacy tab"):
        open_screen(page, "Profile")
        expect_screen(page, "Profile")
        _open_profile_tab(page, "Privacy")
        expect(page.get_by_text("What a do-not-disclose flag actually does")).to_be_visible()

    with step(page, "Read what the flag promises before using it"):
        promise = page.get_by_text("What a do-not-disclose flag actually does").locator(
            "xpath=ancestor::div[contains(@class,'alert')][1]"
        )
        wording = " ".join(promise.inner_text().split())
        assert "removed from every generated CV" in wording, wording
        assert "stripped out of the request before anything reaches the AI provider" in wording, (
            wording
        )

    with step(page, "Never disclose the phone number"):
        expect(page.get_by_text("Nothing is withheld")).to_be_visible()
        page.get_by_text("Phone number", exact=True).click()
        withheld = _card_for(page, "Withheld fields")
        expect(withheld.get_by_text("contact.phone")).to_be_visible(timeout=30_000)
        expect(withheld.get_by_text("never disclosed")).to_be_visible()

    with step(page, "Leave the screen and come back to prove it was recorded"):
        open_screen(page, "Where I am")
        expect_screen(page, "Where I am")
        open_screen(page, "Profile")
        expect_screen(page, "Profile")
        tab = page.get_by_role("button", name=re.compile(r"^Privacy"))
        assert "1" in tab.inner_text(), (
            f"the Privacy tab reads {tab.inner_text()!r} after a reload; the flag was not kept"
        )
        tab.click()
        wait_for_ready(page)
        expect(_card_for(page, "Withheld fields").get_by_text("contact.phone")).to_be_visible()

    with step(page, "Check the flag was logged (NFR-701)", shot=False):
        log_tail.assert_contains(
            "PUT /api/profile/disclosure-flags", what="the do-not-disclose flag reached the server"
        )

    expect_clean(page)


# ---------------------------------------------------------------------------
# 5 — CR-410: the wording, then the refusal, then the consent
# ---------------------------------------------------------------------------


def test_the_transfer_is_refused_until_the_cr410_consent_is_given(page, persona, log_tail):
    """CR-410: consent is checked at the moment of egress, and said first."""
    _signed_in(page, persona)

    with step(page, "Open the composite profile screen"):
        open_screen(page, "Composite profile")
        expect_screen(page, "Composite profile")
        expect(page.get_by_text("Getting started with composite profile")).to_be_visible()

    with step(page, "Read the transfer notice — before anything is sent"):
        notice = page.get_by_text("Your profile is sent outside the European Union")
        expect(notice).to_be_visible()
        card = notice.locator("xpath=ancestor::div[contains(@class,'alert')][1]")
        wording = " ".join(card.inner_text().split())
        assert "outside the European Union" in wording, wording
        assert "do not disclose" in wording, wording
        assert "Acknowledged." not in wording, (
            "the consent already reads as granted before it was ever given"
        )
        expect(card.get_by_role("button", name="I consent to this transfer")).to_be_visible()

    with step(page, "Try to build the composite profile without consenting"):
        page.get_by_role("button", name="Build the composite profile").click()
        refusal = page.locator(".alert-danger")
        expect(refusal).to_be_visible(timeout=60_000)
        text = " ".join(refusal.inner_text().split())
        assert "CR-410" in text or "onsent" in text, (
            f"the refusal does not explain that consent is missing: {text}"
        )
        screenshot(page, "composite — refused without consent")

    with step(page, "Give the consent"):
        page.get_by_role("button", name="I consent to this transfer").click()
        expect(page.get_by_text("Acknowledged.")).to_be_visible(timeout=60_000)
        expect(page.get_by_role("button", name="I consent to this transfer")).to_have_count(0)

    with step(page, "Check the consent decision was audited (NFR-702)", shot=False):
        # A consent that is not on the record is not a consent.
        log_tail.assert_contains(
            "action=consent.granted", what="the CR-410 decision was written to the audit trail"
        )

    with step(page, "Open the help for the composite screen"):
        expect_help(page, "Composite profile")

    # The 404 is the "no composite yet" contract; the 403 is the refusal this
    # test exists to prove.
    expect_clean(
        page,
        allow=(("/api/enrichment/composite", 404), ("/api/enrichment/composite", 403)),
    )


# ---------------------------------------------------------------------------
# 6 — The composite profile
# ---------------------------------------------------------------------------


@pytest.mark.llm
def test_the_composite_profile_is_built_and_every_line_carries_a_source(page, persona, log_tail):
    """FR-121, FR-125: a statement without its source is indistinguishable from
    an invented one, so the screen renders the source beside every line."""
    _signed_in(page, persona)

    with step(page, "Open the composite profile screen"):
        open_screen(page, "Composite profile")
        expect_screen(page, "Composite profile")
        expect(page.get_by_text("Acknowledged.")).to_be_visible()

    with step(page, "Build the composite profile"):
        page.get_by_role("button", name="Build the composite profile").click()
        expect(page.get_by_role("heading", name=re.compile(r"^Version \d"))).to_be_visible(
            timeout=LLM_TIMEOUT_MS
        )
        wait_for_ready(page, timeout=LLM_TIMEOUT_MS)
        screenshot(page, "composite — built")

    with step(page, "Check the profile reads as this person's career"):
        body = " ".join(page.locator("#root").inner_text().split())
        for employer in ("Cortexa AI", "Volta Energy", "Dataflow Analytics"):
            assert employer in body, f"the composite never mentions {employer}: {body[:400]}"

    with step(page, "Check every statement names the source that supports it (FR-125)"):
        sourced = page.get_by_title(re.compile(r"^Source: "))
        assert sourced.count() >= 4, (
            f"only {sourced.count()} statement(s) carry a source marker; FR-125 requires "
            "one per statement"
        )
        labels = " | ".join(sourced.all_inner_texts())
        assert re.search(r"Your CV|LinkedIn export|Your profile|You said so", labels), (
            f"no statement is traced to a document the job seeker supplied: {labels}"
        )
        untraceable = page.get_by_text("No verified source")
        if untraceable.count():
            # Not a failure: FR-125 wants an untraced line flagged, not hidden.
            expect(page.get_by_text(re.compile(r"statements not traced")).first).to_be_visible()

    with step(page, "Check the synthesis was logged and charged for (FR-364)", shot=False):
        log_tail.assert_contains(
            "POST /api/enrichment/composite", what="the synthesis request was logged"
        )

    with step(page, "Check the synthesis actually ran, rather than falling back"):
        # The screen looks identical either way, which is the problem: when the
        # model's JSON cannot be parsed the pipeline quietly assembles the
        # composite from the profile alone and nothing on the screen says so.
        # This assertion is what makes that visible.
        narrative = _card_for(page, "Narrative")
        text = " ".join(narrative.inner_text().split())
        assert "no narrative was generated" not in text, (
            "the composite fell back to the structural assembly and the screen says "
            "nothing about it — the synthesis was charged for and produced nothing. "
            "Narrative reads: " + text
        )

    with step(page, "Work the online findings queue"):
        page.get_by_role("button", name=re.compile(r"^Online findings")).click()
        wait_for_ready(page)
        expect(page.get_by_text("Someone else may share your name")).to_be_visible()
        cards = page.locator(".finding")
        if cards.count() == 0:
            # Degraded, as designed: online enrichment is off by default
            # (FR-126), so no finding can be offered to accept or reject. The
            # screen has to say so rather than look like a bug.
            expect(page.get_by_text("Online enrichment is switched off")).to_be_visible()
            expect(page.get_by_text("Nothing in this view")).to_be_visible()
            harness.narrate(
                "  · degraded, as designed: no findings to accept or reject — "
                "online enrichment is off (FR-126)"
            )
        else:
            first = cards.first
            first.get_by_role("button", name=re.compile("Accept|Confirm")).first.click()
            wait_for_ready(page, timeout=120_000)
        screenshot(page, "composite — findings queue")

    # The screen still opens on an account with no composite yet, so the 404 on
    # the first load is the contract the first-run card is rendered from.
    expect_clean(page, allow=(("/api/enrichment/composite", 404),))


# ---------------------------------------------------------------------------
# 7 — The dream job
# ---------------------------------------------------------------------------


@pytest.mark.llm
def test_the_dream_job_statement_becomes_a_model_the_seeker_confirms(page, persona, log_tail):
    """FR-109, FR-128: free text in, a structure the search acts on out — but
    only once the job seeker has read it and said yes."""
    _signed_in(page, persona)
    statement = str(persona.get("dream_job.statement", default="")).strip()
    assert statement, f"{harness.PERSONA_PATH} has no dream-job statement to paste"

    with step(page, "Open the dream job screen"):
        open_screen(page, "Dream job")
        expect_screen(page, "Dream job")
        expect(page.get_by_text("Getting started with dream job")).to_be_visible()

    with step(page, "Write the statement in the persona's own words (FR-109)"):
        editor = page.get_by_placeholder(re.compile("The work itself"))
        expect(editor).to_be_visible()
        editor.fill(statement)
        expect(page.get_by_text(re.compile(r"\d+ words"))).to_be_visible()
        expect(page.get_by_text("Unsaved")).to_be_visible()
        screenshot(page, "dream job — statement written")

    with step(page, "Build the structured model"):
        page.get_by_role("button", name="Build the structured model").click()
        expect(
            page.get_by_role("heading", name=re.compile(r"Structured model, version \d"))
        ).to_be_visible(timeout=LLM_TIMEOUT_MS)
        wait_for_ready(page, timeout=LLM_TIMEOUT_MS)
        expect(page.get_by_text("Not confirmed")).to_be_visible()
        screenshot(page, "dream job — model extracted")

    with step(page, "Check the target roles are about an AI developer"):
        roles = _card_for(page, "Target roles")
        text = " ".join(roles.inner_text().split())
        assert re.search(r"\b(AI|ML|LLM|Machine Learning)\b", text), (
            f"the target roles do not look like an AI developer's: {text}"
        )
        assert re.search(r"Lead|Staff|Principal|Senior", text), (
            f"the statement asks for technical leadership; the roles say: {text}"
        )

    with step(page, "Check the deal-breakers came back, and as hard vetoes"):
        breakers = _card_for(page, "Deal-breakers")
        text = " ".join(breakers.inner_text().split()).lower()
        wanted = ["engineering ownership", "data infrastructure", "gpu"]
        found = [w for w in wanted if w in text]
        assert len(found) >= 2, (
            f"the statement names three deal-breakers; the model kept {found}: {text}"
        )
        assert "hard veto" in text, f"no deal-breaker is a hard veto: {text}"

    with step(page, "Check the values it read out of the prose"):
        values = _card_for(page, "Culture and values")
        text = " ".join(values.inner_text().split())
        assert "Nothing extracted" not in text, f"no values were read from the statement: {text}"
        assert "seek" in text.lower() or "avoid" in text.lower(), (
            f"the values carry no polarity, so nothing can act on them: {text}"
        )
        screenshot(page, "dream job — values and deal-breakers")

    with step(page, "Confirm the model, because nothing acts on it until then"):
        page.get_by_role("button", name="Confirm this model").click()
        dialog = page.get_by_text("Confirm this model?")
        expect(dialog).to_be_visible()
        expect(page.get_by_text(re.compile("hard one vetoes an opportunity"))).to_be_visible()
        page.get_by_role("button", name=re.compile("Confirm — use it for matching")).click()
        expect(page.get_by_text("Confirmed by you")).to_be_visible(timeout=120_000)
        expect(page.get_by_role("button", name="Confirm this model")).to_have_count(0)
        screenshot(page, "dream job — confirmed")

    with step(page, "Check the statement, the extraction and the approval were logged", shot=False):
        log_tail.assert_contains(
            "PUT /api/profile/dream-job", what="the FR-109 statement was versioned"
        )
        log_tail.assert_contains(
            "POST /api/enrichment/dream-job", what="the FR-128 extraction ran"
        )
        log_tail.assert_contains("/confirm", what="the job seeker's approval was recorded")

    with step(page, "Open the help for the dream job screen"):
        expect_help(page, "Dream job")

    # Both endpoints 404 until the statement and the model exist.
    expect_clean(
        page,
        allow=(("/api/enrichment/dream-job", 404), ("/api/profile/dream-job", 404)),
    )


# ---------------------------------------------------------------------------
# 8 — Back to the map
# ---------------------------------------------------------------------------


@pytest.mark.llm
def test_the_journey_map_now_reads_the_profile_phase_as_done(page, persona):
    """The map is only useful if it moves: three finished stages must show."""
    _signed_in(page, persona)

    with step(page, "Go back to Where I am"):
        open_screen(page, "Where I am")
        expect_screen(page, "Where I am")
        screenshot(page, "where I am — after onboarding")

    with step(page, "Check the Profile phase is complete"):
        done = page.get_by_title("Complete")
        labels = " | ".join(" ".join(t.split()) for t in done.all_inner_texts())
        for stage in ("Profile intake", "Composite profile", "Dream job"):
            assert stage in labels, (
                f"{stage} is not marked complete after onboarding; the map shows: {labels}"
            )

    with step(page, "Check the map now points at the next phase"):
        expect(page.get_by_text("Set your search directives")).to_be_visible()
        blocked = page.get_by_title("Waiting on an earlier stage")
        reasons = " | ".join(" ".join(t.split()) for t in blocked.all_inner_texts())
        assert "Needs a profile first" not in reasons, (
            f"a stage still says it is waiting on the profile: {reasons}"
        )
        assert "Needs a composite profile first" not in reasons, (
            f"a stage still says it is waiting on the composite profile: {reasons}"
        )

    with step(page, "Check the counts read as an account that has not searched yet"):
        expect(page.get_by_text("Opportunities", exact=True).first).to_be_visible()
        expect(page.get_by_text("nothing sent yet")).to_be_visible()

    expect_clean(page)
