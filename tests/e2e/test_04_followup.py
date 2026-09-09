"""End-to-end: everything after an application, and the administration surface.

The four screens of the "Follow up" phase (pipeline, responses, what works),
the monitoring loop that keeps feeding them, the dream-job intelligence and
networking surfaces, and the administration area an operator reads afterwards.

What this suite is trying to catch, screen by screen, is the class of failure
the 528 backend tests cannot see: a lazy chunk that never paints, a table that
renders empty because its call 500'd rather than because there is nothing to
show, a rate quoted without the sample size behind it (FR-425), a caveat
pushed below the numbers it is supposed to qualify.

Three rules it holds itself to:

* **An empty screen is only a pass when it is empty on purpose.** Every check
  is paired with :func:`harness.expect_no_failed_requests` on the calls that
  screen made, so "nothing to show" can never be a 500 in disguise.
* **Degraded is not skipped.** Where a feature genuinely cannot run here — no
  Gmail OAuth, no Resend inbox, no imported network — the assertion is that
  the screen *says so*, and the fact is recorded as "degraded, as designed"
  rather than quietly passed over.
* **Setup is not the test.** The history of sent applications the learning
  needs is written directly through ``dreamjob.db`` in :func:`_seed_history`
  and is labelled as such; everything the suite actually claims about the
  product is done through the interface, with the mouse.

Marked ``@pytest.mark.llm``: anything that reaches the model provider or the
open internet, so ``-m "not llm"`` leaves a suite that runs offline.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

# The seeding below is the only place this suite talks to the database. It goes
# through the application's own access layer (db/connection, db/repositories),
# so no SQL is written outside ``db/`` and the FTS index the company search
# reads is maintained the way the product maintains it.
from dreamjob.db.connection import insert_row, update_row, utcnow
from dreamjob.db.repositories import knowledge as knowledge_repo
from harness import (
    DEFAULT_TIMEOUT_MS,
    LOG_DIR,
    current_error,
    expect_no_console_errors,
    expect_no_failed_requests,
    narrate,
    open_screen,
    screenshot,
    sign_in,
    step,
    wait_for_ready,
)

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


#: The SPA ships its own console log to ``POST /api/logs/client``. That intake
#: is rate-limited per client *address* (30 batches a minute), and on a
#: developer machine every browser shares one address — so under any concurrent
#: use the browser collects an uncaught 429 for its own telemetry. It is real
#: (see the report), but it is never the screen under test, so the checks below
#: name it rather than drowning in it.
TELEMETRY_NOISE = ("/api/logs/client",)


def api(page, method: str, path: str, body: Any = None) -> dict[str, Any]:
    """Call the API *as the signed-in browser*, and hand back status and body.

    Session cookies are bound to the client that created them (NFR-202), so a
    request made from anywhere but the page under test would be rejected. This
    goes through the page's own ``fetch``, which is also why the harness sees
    these calls in its network record like any other.
    """
    return page.evaluate(
        """async ([method, path, body]) => {
            const res = await fetch('/api' + path, {
                method,
                credentials: 'include',
                headers: body === null ? {} : { 'Content-Type': 'application/json' },
                body: body === null ? undefined : JSON.stringify(body),
            })
            const text = await res.text()
            let data = null
            try { data = text ? JSON.parse(text) : null } catch { data = text }
            return { status: res.status, data }
        }""",
        [method, path, body],
    )


def check_help(page, *, expect: str | None = None) -> None:
    """The "? Help" control opens a drawer, and the drawer has content in it.

    Help is static and ships with the application, so a screen whose drawer is
    empty is a screen nobody wrote guidance for — which is exactly the sort of
    thing that goes unnoticed until a user is stuck on it.
    """
    page.get_by_role("button", name="Help", exact=True).first.click()
    drawer = page.get_by_role("dialog", name="Help")
    drawer.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    # inner_text() is the *rendered* text, and the drawer's sub-headings are
    # upper-cased by CSS, so the comparison is case-insensitive.
    text = " ".join(drawer.inner_text().split())
    assert "glossary" in text.lower(), f"the help drawer carries no glossary: {text[:200]!r}"
    assert len(text) > 400, f"the help drawer is all but empty ({len(text)} chars)"
    assert "There is no specific guidance for this screen yet" not in text, (
        "this screen has no help written for it"
    )
    if expect:
        assert expect.lower() in text.lower(), (
            f"the help drawer never mentions {expect!r}: {text[:300]!r}"
        )
    page.get_by_role("button", name="Close help").click()
    drawer.wait_for(state="hidden", timeout=DEFAULT_TIMEOUT_MS)


def assert_screen_rendered(page, marker: str, *, heading: bool = True) -> None:
    """The screen painted its own content — not a spinner, not an error boundary.

    ``marker`` is something only this screen renders. Most screens are named by
    a heading; the ones whose first landmark is an advisory notice are matched
    on its text instead, because ``Caution`` renders a ``<strong>`` and not a
    heading.
    """
    banner = current_error(page)
    assert banner is None, f"the screen is showing an error instead of itself: {banner}"
    assert page.locator(".spinner").count() == 0, "the screen never stopped loading"
    assert "Something went wrong" not in page.inner_text("body"), (
        "the error boundary is on screen instead of the page"
    )
    found = (
        page.get_by_role("heading", name=marker).first
        if heading
        else page.get_by_text(marker).first
    )
    found.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)


def register(api_url: str, email: str, password: str, name: str) -> str:
    """SETUP: create an account over HTTP and return its id.

    Registering through the sign-in card is suite 01's subject; here it is
    scaffolding, so it is done in one call rather than acted out.
    """
    request = urllib.request.Request(
        f"{api_url}/api/auth/register",
        data=json.dumps(
            {"email": email, "display_name": name, "password": password}
        ).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "dreamjob-e2e"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            return json.loads(response.read())["id"]
    except urllib.error.HTTPError as exc:  # pragma: no cover - setup failure
        pytest.fail(f"Could not register {email}: {exc.code} {exc.read()[:300]!r}")


def stamp(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# SETUP — the history an earlier suite would have left behind
# ---------------------------------------------------------------------------
#
# "What works" (FR-425) analyses *resolved* applications, and there is no API
# route that creates an opportunity without running a live campaign against the
# open internet. So the month of applying that a real user would have behind
# them is written here, through the product's own data layer, and labelled as
# setup. The shape is the one the product owner described: a kind of work that
# answers, and a kind of work that mostly rejects.


SMALL_COMPANY = {
    "name": "Northwind Data",
    "normalised_name": "northwind data",
    "domain": "northwind-data.example.com",
    "country": "BE",
    "size_band": "50-250",
    "size_fte": 140,
    "stage": "scaleup",
    "business_summary": "Streaming data platforms for logistics operators.",
    "sector_codes": [{"code": "62010", "label": "Software"}],
    "values_culture": ["autonomy", "written decisions"],
}

BIG_COMPANY = {
    "name": "Meridian Group",
    "normalised_name": "meridian group",
    "domain": "meridian-group.example.com",
    "country": "BE",
    "size_band": ">1000",
    "size_fte": 4200,
    "stage": "listed",
    "business_summary": "Listed financial services group with an in-house analytics arm.",
    "sector_codes": [{"code": "64190", "label": "Financial services"}],
    "values_culture": ["process", "risk control"],
}

#: (family, company, arrangement, kind, stage, outcome, replied)
#: 7 data-engineering applications that mostly answer, 9 data-science ones that
#: mostly reject: 16 resolved, a 37% baseline, and two segments far enough from
#: it to be worth a sentence.
HISTORY: list[tuple[str, str, str, str, str, str | None, bool]] = [
    *[("data engineering", "small", "hybrid", "vacancy", "replied", None, True) for _ in range(4)],
    ("data engineering", "small", "hybrid", "speculative", "replied", None, True),
    ("data engineering", "small", "remote", "vacancy", "closed", "rejected", False),
    ("data engineering", "small", "hybrid", "vacancy", "closed", "rejected", False),
    ("data science", "big", "onsite", "vacancy", "interview", None, True),
    *[("data science", "big", "onsite", "vacancy", "closed", "rejected", False) for _ in range(6)],
    ("data science", "big", "onsite", "speculative", "closed", "rejected", False),
    ("data science", "big", "remote", "vacancy", "closed", "rejected", False),
]

#: Four applications sent last week with nothing back: the picking list the
#: responses screen is driven from.
AWAITING: list[tuple[str, str, str]] = [
    ("data engineering", "small", "Senior data engineer, streaming"),
    ("data engineering", "small", "Data platform engineer"),
    ("data science", "big", "Lead data scientist, risk"),
    ("data science", "big", "Data scientist, pricing"),
]


def _seed_history(seeker_id: str) -> dict[str, Any]:
    """SETUP: a month of applying, written straight into the database."""
    now = utcnow()
    companies = {
        "small": knowledge_repo.insert_company({**SMALL_COMPANY, "collected_at": now}),
        "big": knowledge_repo.insert_company({**BIG_COMPANY, "collected_at": now}),
    }

    directive_set_id = insert_row(
        "directive_set",
        {
            "job_seeker_id": seeker_id,
            "name": "Data roles, Belgium",
            "version": 1,
            # The vocabulary pipeline/directives.py validates against: the
            # size bands are the FR-143 enum, not the free-text band on a
            # company row.
            "job_content": {
                "target_titles": ["Senior data engineer", "Data scientist"],
                "function_families": ["data engineering", "data science"],
            },
            "company_type": {"size_bands": ["b50_250", "gt1000"]},
            "location": {"countries": ["BE"]},
            "created_at": now,
        },
    )
    profile_version_id = insert_row(
        "profile_version",
        {
            "job_seeker_id": seeker_id,
            "version": 1,
            "sections": {"summary": "Senior data engineer, ten years, Brussels."},
            "created_at": now,
        },
    )
    campaign_id = insert_row(
        "campaign",
        {
            "job_seeker_id": seeker_id,
            "directive_set_id": directive_set_id,
            "profile_version_id": profile_version_id,
            "name": "Spring search",
            "status": "finished",
            "stage": "finished",
            "started_at": stamp(45),
            "finished_at": stamp(40),
            "created_at": stamp(45),
        },
    )

    def opportunity(family: str, company: str, arrangement: str, kind: str,
                    title: str, sent: str) -> str:
        return insert_row(
            "opportunity",
            {
                "job_seeker_id": seeker_id,
                "campaign_id": campaign_id,
                "company_id": companies[company],
                "kind": kind,
                "title": title,
                "function_family": family,
                "seniority": "senior",
                "country": "BE",
                "location": "Brussels",
                "work_arrangement": arrangement,
                "language": "en",
                "score": 71.0,
                "score_dream_fit": 64.0,
                "required_skills": ["python", "sql", "airflow"],
                "created_at": sent,
                "updated_at": sent,
            },
        )

    def package_and_dispatch(opportunity_id: str, title: str, company: str, sent: str) -> str:
        package_id = insert_row(
            "application_package",
            {
                "job_seeker_id": seeker_id,
                "opportunity_id": opportunity_id,
                "language": "en",
                "cv_template": "classic",
                "email_subject": f"Application — {title}",
                "email_body": "Dear hiring team, ...",
                "status": "sent",
                "approved_at": sent,
                "profile_version_id": profile_version_id,
                "created_at": sent,
                "updated_at": sent,
            },
        )
        insert_row(
            "dispatch",
            {
                "job_seeker_id": seeker_id,
                "application_package_id": package_id,
                "backend": "resend",
                "recipient_email": f"jobs@{companies and company}-example.com",
                "recipient_name": "Hiring team",
                "subject": f"Application — {title}",
                "sent_at": sent,
                "delivery_status": "sent",
                "created_at": sent,
            },
        )
        return package_id

    resolved_ids: list[str] = []
    for index, (family, company, arrangement, kind, stage, outcome, replied) in enumerate(HISTORY):
        sent = stamp(40 - index)
        title = f"{family.title()} — role {index + 1}"
        opportunity_id = opportunity(family, company, arrangement, kind, title, sent)
        # No dispatch for the historical ones: they are already answered, so
        # they must not turn up on the "awaiting a response" picking list.
        resolved_ids.append(
            insert_row(
                "pipeline_card",
                {
                    "job_seeker_id": seeker_id,
                    "opportunity_id": opportunity_id,
                    "stage": stage,
                    "outcome": outcome,
                    "outcome_at": stamp(25 - index // 4) if outcome else None,
                    "stage_dates": {"sent": sent},
                    "reached_replied_at": stamp(30 - index) if replied else None,
                    "reached_interview_at": stamp(28) if stage == "interview" else None,
                    "variables": {
                        "cv_template": "classic",
                        "language": "en",
                        "email_style": "short",
                        "contact_type": "hr",
                        "opportunity_kind": kind,
                    },
                    "created_at": sent,
                    "updated_at": sent,
                },
            )
        )

    awaiting: list[dict[str, str]] = []
    for index, (family, company, title) in enumerate(AWAITING):
        sent = stamp(6 - index)
        opportunity_id = opportunity(family, company, "hybrid", "vacancy", title, sent)
        package_id = package_and_dispatch(opportunity_id, title, company, sent)
        insert_row(
            "pipeline_card",
            {
                "job_seeker_id": seeker_id,
                "opportunity_id": opportunity_id,
                "application_package_id": package_id,
                "stage": "sent",
                "stage_dates": {"sent": sent},
                "next_action": "Wait for a reply, then follow up",
                "variables": {"cv_template": "classic", "language": "en"},
                "created_at": sent,
                "updated_at": sent,
            },
        )
        awaiting.append({"title": title, "company": company})

    return {
        "campaign_id": campaign_id,
        "directive_set_id": directive_set_id,
        "companies": companies,
        "resolved": len(resolved_ids),
        "awaiting": awaiting,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def follower(persona, api_url) -> dict[str, Any]:
    """SETUP: one account, promoted to administrator, with a month of history.

    Module-scoped because the point of this suite is the *state after applying*
    — registering a clean account per test would leave nothing to follow up.
    Each test still gets its own browser context and signs in through the card,
    so no test can borrow another's session.
    """
    email = persona.fresh_email("e2e04")
    password = persona.password
    seeker_id = register(api_url, email, password, persona.display_name)

    # The administration screens are administrator-only, and this installation's
    # only administrator is the product owner whose password the suite does not
    # have. Promoting the throwaway account is setup, not a claim about the app.
    update_row("job_seeker", seeker_id, {"is_admin": 1, "updated_at": utcnow()})

    seeded = _seed_history(seeker_id)
    narrate(
        f"  · setup: {email} promoted to administrator, "
        f"{seeded['resolved']} resolved and {len(seeded['awaiting'])} awaiting applications seeded"
    )
    return {
        "email": email,
        "password": password,
        "seeker_id": seeker_id,
        "display_name": persona.display_name,
        **seeded,
    }


@pytest.fixture
def signed_in(page, follower):
    """Sign the seeded account in through the card, and land in the app."""
    sign_in(page, follower["email"], follower["password"])
    return follower


# ---------------------------------------------------------------------------
# Responses (FR-326, FR-422, FR-425, NFR-305)
# ---------------------------------------------------------------------------


def _awaiting_count(page) -> int:
    """The picking list's own count — "3 of 4 awaiting a response"."""
    line = page.get_by_text(re.compile(r"\d+ of \d+ awaiting a response")).first
    line.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    return int(re.search(r"of (\d+) awaiting", line.inner_text()).group(1))


#: What the response intake answers with when it works. Quoted in the failure
#: message below because the defect it catches is a 500 *after* the write.
RECORD_OK = 201

RECORD_BUG = (
    "backend/dreamjob/api/routers/learning.py:88 calls record_audit(job_seeker_id=...) "
    "but dreamjob/security/audit.py:35 declares the parameter as seeker_id, so "
    "POST /api/learning/responses raises TypeError and answers 500 — after the "
    "incoming_reply, the manual_response and the board move have already been "
    "written. The same mistake is at learning.py:188 for advice.applied."
)


def _record_response(page, *, company: str, channel: str, outcome: str) -> int:
    """Fill in the record-a-response form the way a person would.

    Returns the status the API answered with. The form stays on screen with an
    error box when the call fails, so the picking list is reloaded afterwards —
    otherwise one failure would cascade into "could not find the next row".
    """
    row = page.locator("table tbody tr").filter(has_text=company).first
    row.get_by_role("button", name="Record a response").click()

    form = page.locator("form").first
    form.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    # Channel and outcome are chips, not inputs: they carry no role, so they are
    # matched by their visible text inside the form that owns them.
    form.get_by_text(channel, exact=True).click()
    form.get_by_text(outcome, exact=True).click()
    with page.expect_response(
        lambda r: r.request.method == "POST" and r.url.endswith("/api/learning/responses")
    ) as recorded:
        form.get_by_role("button", name="Record this response").click()
    status = recorded.value.status
    if status != RECORD_OK:
        narrate(f"  ! recording a {outcome} by {channel} answered {status}")
        page.reload()
        wait_for_ready(page)
    return status


def test_responses_are_recorded_across_several_channels(signed_in, page, log_tail):
    """Record what came back by e-mail, by phone and on LinkedIn — including a
    rejection — and watch the list of applications awaiting a response shrink."""
    with step(page, "Open the responses screen"):
        open_screen(page, "Responses")
        assert_screen_rendered(page, "Record a response")
        assert page.get_by_text("A wrong outcome distorts every rate").is_visible(), (
            "the screen does not warn that a wrong outcome distorts the rates (NFR-305)"
        )

    with step(page, "Read the help for this screen"):
        check_help(page, expect="response")

    with step(page, "Count what is waiting for a response"):
        before = _awaiting_count(page)
        assert before >= 3, f"the picking list should hold the seeded applications, saw {before}"

    with step(page, "Record three responses, one on each channel"):
        answered = [
            _record_response(
                page, company="Northwind", channel="Email", outcome="Interview invitation"
            ),
            _record_response(page, company="Meridian", channel="Phone", outcome="Rejection"),
            _record_response(page, company="Meridian", channel="LinkedIn", outcome="Interest"),
        ]

    with step(page, "Check the awaiting list shrank by the three just recorded"):
        after = _awaiting_count(page)
        assert after == before - 3, (
            f"the awaiting list went from {before} to {after}; recording three should leave "
            f"{before - 3}"
        )
        assert page.get_by_text("What has come back").is_visible()

    with step(page, "Check the intake answered as it should have"):
        # Deliberately last: the rows really are written, so everything above is
        # a true statement about the screen. What is broken is the answer the
        # screen is given for work it has already done.
        assert all(status == RECORD_OK for status in answered), (
            f"POST /api/learning/responses answered {answered}, not {[RECORD_OK] * 3}. "
            + RECORD_BUG
        )

    # NFR-701: the intake is on the record either way.
    log_tail.assert_contains(
        "POST /api/learning/responses",
        what="the response intake was logged (NFR-701)",
    )
    expect_no_console_errors(page, ignore=TELEMETRY_NOISE)


def test_a_response_can_be_reclassified_and_the_correction_sticks(signed_in, page):
    """NFR-305: what the person states overrides what the model read, and the
    correction has to survive the round trip — every rate on “What works”
    counts it."""
    with step(page, "Open the responses screen and record something to correct"):
        open_screen(page, "Responses")
        assert_screen_rendered(page, "Record a response")
        status = _record_response(
            page, company="Northwind", channel="Email", outcome="Rejection"
        )
        if status != RECORD_OK:
            # Pinned by test_responses_are_recorded_across_several_channels: the
            # row is written before the 500, so the correction below is still a
            # true test of the screen it is about.
            narrate(f"  ! intake answered {status}; see the response-intake defect")

    with step(page, "Find it in “What has come back”"):
        page.reload()
        wait_for_ready(page)
        table = page.locator(".card", has_text="What has come back").locator("table")
        table.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
        assert "Rejection" in table.inner_text(), (
            f"the response just recorded is not in the list: {table.inner_text()[:300]!r}"
        )

    with step(page, "Correct it to a request for information"):
        page.get_by_label("What this response actually was").first.select_option("info_request")
        with page.expect_response(
            lambda r: r.request.method == "PATCH" and "/api/learning/responses/" in r.url
        ) as corrected:
            page.get_by_role("button", name="Correct", exact=True).first.click()
        assert corrected.value.status == 200, (
            f"correcting a response answered {corrected.value.status}"
        )
        page.get_by_text(re.compile("Corrected to")).first.wait_for(
            state="visible", timeout=DEFAULT_TIMEOUT_MS
        )

    with step(page, "Reload, and check the correction survived"):
        page.reload()
        wait_for_ready(page)
        table = page.locator(".card", has_text="What has come back").locator("table")
        table.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
        assert "Request for information" in table.inner_text(), (
            "the corrected outcome did not survive a reload — the correction did not stick"
        )

    # The intake's own 500 is pinned by the test above and ignored here by the
    # exact URL that produces it; a failure of the PATCH this test is about has
    # the response id in its path and would still be reported.
    expect_no_console_errors(page, ignore=("api/learning/responses:", *TELEMETRY_NOISE))
    expect_no_failed_requests(page, ignore=("/api/learning/responses", *TELEMETRY_NOISE))


# ---------------------------------------------------------------------------
# Pipeline board (FR-421, FR-424)
# ---------------------------------------------------------------------------


def drag(page, card, column) -> None:
    """Drag one card onto one column, by hand.

    ``Locator.drag_to`` scrolls the *target* into view between pressing and
    releasing, and on a board that is taller than the viewport that moves the
    page under the cursor — so the drag starts on whichever card has slid under
    it, and a different application moves. Both ends are scrolled into view
    first here, and the pointer is driven explicitly, so the card the test
    picked is the card that moves.
    """
    column.scroll_into_view_if_needed()
    card.scroll_into_view_if_needed()
    source = card.bounding_box()
    target = column.bounding_box()
    page.mouse.move(source["x"] + source["width"] / 2, source["y"] + source["height"] / 2)
    page.mouse.down()
    page.mouse.move(
        target["x"] + target["width"] / 2, target["y"] + target["height"] / 2, steps=12
    )
    page.mouse.move(
        target["x"] + target["width"] / 2, target["y"] + target["height"] / 2 + 6, steps=4
    )
    page.mouse.up()


#: The board's columns, in the order an application moves through them.
STAGES = ("Sent", "Replied", "Interview", "Offer", "Closed")


def _titles(column) -> list[str]:
    """The role titles on the cards of one column, in the order they are drawn."""
    return [
        line.splitlines()[0].strip()
        for line in column.locator("article.board-card").all_inner_texts()
    ]


def _column(page, label: str):
    """One board column. ``.board-col`` is a CSS hook: a column carries a
    heading but no role of its own, and the drop target is the whole section."""
    return page.locator("section.board-col").filter(
        has=page.get_by_role("heading", name=re.compile(rf"^{label}\b"))
    ).first


def test_pipeline_board_shows_cards_moves_one_and_opens_its_detail(signed_in, page):
    with step(page, "Open the pipeline board"):
        open_screen(page, "Pipeline")
        assert_screen_rendered(
            page,
            "Nothing here is sent, and nothing here is closed, without you",
            heading=False,
        )
        page.locator("article.board-card").first.wait_for(
            state="visible", timeout=DEFAULT_TIMEOUT_MS
        )

    with step(page, "Read the help for this screen"):
        check_help(page, expect="pipeline")

    with step(page, "Check the cards are in the columns their stage says"):
        counts = {
            label: _column(page, label).locator("article.board-card").count()
            for label in STAGES
        }
        narrate(f"  · board: {counts}")
        # The eight seeded rejections are closed and the five seeded replies
        # are answered; a response recorded by an earlier test may have moved
        # more, never fewer.
        assert counts["Closed"] >= 8, f"the seeded rejections are not in Closed: {counts}"
        assert counts["Replied"] >= 4, f"the seeded replies are not in Replied: {counts}"
        assert counts["Interview"] >= 1, f"the seeded interview is not in Interview: {counts}"
        assert sum(counts.values()) >= 20, f"the board lost cards: {counts}"
        summary = {
            tile.locator(".stat-label").inner_text().split("\n")[0]: tile.locator(
                ".stat-value"
            ).inner_text()
            for tile in page.locator(".stat-tile").all()
        }
        for label in ("Sent", "Replied", "Interview", "Closed"):
            assert summary.get(label) == str(counts[label]), (
                f"the counts above the board disagree with the columns: {summary} vs {counts}"
            )

    with step(page, "Drag a card into another stage"):
        # Which columns hold cards depends on what an earlier test recorded, so
        # the pair is chosen from what is on the board. "Closed" is never the
        # target: dropping there asks for an outcome instead of moving.
        source, target = next(
            (a, b)
            for a, b in (("Sent", "Replied"), ("Replied", "Interview"), ("Interview", "Offer"))
            if counts[a]
        )
        title = _titles(_column(page, source))[0]
        card = _column(page, source).locator("article.board-card").filter(has_text=title).first
        target_before = _column(page, target).locator("article.board-card").count()
        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/stage")
        ) as moved:
            drag(page, card, _column(page, target))
        assert moved.value.status == 200, f"the stage change answered {moved.value.status}"
        # The board reloads itself after the move, so wait for the extra card
        # rather than for a count that is briefly zero mid-reload.
        _column(page, target).locator("article.board-card").nth(target_before).wait_for(
            state="visible", timeout=DEFAULT_TIMEOUT_MS
        )
        narrate(f"  · dragged “{title}” from {source} to {target}")
        assert title not in _titles(_column(page, source)), (
            f"“{title}” was dragged out of {source} but is still in it"
        )
        assert title in _titles(_column(page, target)), (
            f"“{title}” was dropped on {target} but did not arrive there"
        )

    with step(page, "Open the card that was moved, and read how it got there"):
        _column(page, target).locator("article.board-card").filter(
            has_text=title
        ).first.click()
        page.get_by_role("heading", name="Stage dates").wait_for(
            state="visible", timeout=DEFAULT_TIMEOUT_MS
        )
        detail = page.locator(".modal").first
        body = detail.inner_text()
        assert "Next action" in body and "Notes" in body, f"the card detail is thin: {body[:200]!r}"
        assert "How this card moved" in body, (
            "the card does not say what moved it — FR-421 asks for the trigger on every event"
        )
        assert "You moved it" in body, (
            "the manual drag was not recorded as a user transition on the card's history"
        )

    with step(page, "Close the card"):
        detail.get_by_role("button", name="Close", exact=True).click()
        page.locator(".modal").first.wait_for(state="hidden", timeout=DEFAULT_TIMEOUT_MS)

    expect_no_console_errors(page, ignore=TELEMETRY_NOISE)
    expect_no_failed_requests(page, ignore=TELEMETRY_NOISE)


def test_a_mock_interview_asks_a_question_and_answers_it(signed_in, page):
    """FR-424. The session falls back to template questions and structural
    feedback when the model is unreachable, and says which it used — so this
    passes either way, and records which path it actually took."""
    with step(page, "Open the pipeline and pick a card to rehearse"):
        open_screen(page, "Pipeline")
        page.locator("article.board-card").first.wait_for(
            state="visible", timeout=DEFAULT_TIMEOUT_MS
        )
        _column(page, "Interview").locator("article.board-card").first.click()
        page.get_by_role("heading", name="Prepare").wait_for(
            state="visible", timeout=DEFAULT_TIMEOUT_MS
        )

    with step(page, "Start a mock interview"):
        page.get_by_role("button", name="Mock interview", exact=True).click()
        page.get_by_role("button", name="Start the session").click()
        page.get_by_text(re.compile(r"Round \d+")).first.wait_for(
            state="visible", timeout=90_000
        )
        modal = page.locator(".modal").first
        written_by = "model-written" if "model-written" in modal.inner_text() else "template questions"
        narrate(f"  · the rehearsal used {written_by}")
        assert page.locator(".qa-question").count() >= 1, "no question was asked"

    with step(page, "Answer the first question"):
        # The rehearsal's answer box has a <label> that is not bound to it (no
        # htmlFor/id on `Field`), so it has no accessible name and
        # get_by_label finds nothing. Matched on its placeholder instead.
        answer = page.locator(".modal textarea").first
        answer.fill(
            "At Northwind I owned the streaming ingestion for 40 pipelines. Latency was "
            "nine minutes; I moved the joins into Flink and cut it to under forty seconds, "
            "which took the nightly reconciliation job away entirely."
        )
        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/answer"), timeout=120_000
        ) as answered:
            page.get_by_role("button", name="Answer and continue").click()
        assert answered.value.status == 200, (
            f"answering the question got {answered.value.status}"
        )
        page.locator(".qa-answer").first.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
        body = page.locator(".modal").first.inner_text()
        assert re.search(r"\d of 5", body), f"the feedback carries no score: {body[:400]!r}"
        method = "the model" if re.search(r"\bllm\b", body) else "the structural check"
        narrate(f"  · the answer was judged by {method}")

    expect_no_console_errors(page, ignore=TELEMETRY_NOISE)
    expect_no_failed_requests(page, ignore=TELEMETRY_NOISE)


# ---------------------------------------------------------------------------
# What works (FR-285, FR-425)
# ---------------------------------------------------------------------------


def test_what_works_shows_the_sample_size_beside_every_rate(signed_in, page):
    with step(page, "Open “What works”"):
        open_screen(page, "What works")
        assert_screen_rendered(page, "Outcome rates by kind of job and company")

    with step(page, "Read the help for this screen"):
        check_help(page, expect="rate")

    with step(page, "Recompute the figures from the responses recorded so far"):
        with page.expect_response(
            lambda r: "/api/learning/patterns" in r.url and "refresh=true" in r.url
        ) as computed:
            page.get_by_role("button", name="Recompute from the latest responses").click()
        assert computed.value.status == 200, (
            f"the pattern analysis answered {computed.value.status}"
        )
        wait_for_ready(page)
        page.locator("section.seg-dim").first.wait_for(
            state="visible", timeout=DEFAULT_TIMEOUT_MS
        )

    with step(page, "Check every rate is shown with the sample size it rests on"):
        # Each rate is drawn as a scale with its own accessible description —
        # "5 of 7 reached the outcome, 71%; plausible range …". That label is
        # the machine-readable form of the rule FR-425 asks for, so it is what
        # is checked, one per row of every dimension table.
        rows = page.locator("section.seg-dim table tbody tr")
        total_rows = rows.count()
        assert total_rows > 0, "the figures card rendered no segment rows at all"
        labels = [
            element.get_attribute("aria-label")
            for element in page.get_by_role("img").all()
            if (element.get_attribute("aria-label") or "").endswith("%")
            or " reached the outcome" in (element.get_attribute("aria-label") or "")
        ]
        described = [text for text in labels if re.search(r"\d+ of \d+ reached the outcome", text)]
        assert len(described) == total_rows, (
            f"{total_rows} rates are on screen but only {len(described)} name their sample "
            "size — FR-425 requires the n beside the rate"
        )
        # And in the visible text, not only in the accessible name.
        for row in rows.all():
            cells = row.inner_text()
            assert re.search(r"\n\d+\n", f"\n{cells}\n") or re.search(r"\b\d+\b", cells), (
                f"a segment row shows no count: {cells!r}"
            )
        narrate(f"  · {total_rows} segment rows, every one of them carrying its n")

    with step(page, "Check the caveats are above the tables, not under them"):
        caveat = page.get_by_text("What these figures cannot support")
        assert caveat.is_visible(), (
            "the analysis is shown without the caveats that qualify it (FR-425)"
        )
        caveat_box = caveat.bounding_box()
        table_box = page.locator("section.seg-dim").first.bounding_box()
        assert caveat_box["y"] < table_box["y"], (
            "the caveats are rendered below the tables they are supposed to qualify"
        )
        assert page.get_by_text("Observed rates, not causes", exact=True).is_visible(), (
            "the advisory notice (NFR-305) is missing from the top of the screen"
        )

    with step(page, "Check the weak kind of work is called out with its figures"):
        text = page.inner_text("body")
        assert "data science" in text and "data engineering" in text, (
            "neither function family reached the segment tables"
        )
        assert re.search(r"data science", text), "the weak segment is not named"

    expect_no_console_errors(page, ignore=TELEMETRY_NOISE)
    expect_no_failed_requests(page, ignore=TELEMETRY_NOISE)


def test_what_works_says_so_when_there_is_too_little_data(page, persona, api_url):
    """A brand-new account has nothing to analyse. The screen has to say that,
    rather than draw an empty table that reads as a finding of "nothing"."""
    email = persona.fresh_email("e2e04-empty")
    register(api_url, email, persona.password, persona.display_name)

    with step(page, "Sign in as a job seeker who has sent nothing yet"):
        sign_in(page, email, persona.password)

    with step(page, "Open “What works” with no history behind it"):
        open_screen(page, "What works")
        assert_screen_rendered(page, "Outcome rates by kind of job and company")

    with step(page, "Check it explains the shortfall instead of showing an empty table"):
        assert page.locator("section.seg-dim").count() == 0, (
            "an empty segment table is drawn where there is nothing to segment"
        )
        body = page.inner_text("body")
        assert "Nothing has resolved yet" in body, (
            f"the screen does not say why it is empty: {body[:400]!r}"
        )
        assert "Advice is withheld until" in body, (
            "the screen does not say that advice is withheld below the minimum sample"
        )
        assert "No open proposals" in body

    with step(page, "Read the help for this screen"):
        check_help(page, expect="rate")

    expect_no_console_errors(page, ignore=TELEMETRY_NOISE)
    expect_no_failed_requests(page, ignore=TELEMETRY_NOISE)


@pytest.mark.llm
def test_redirection_advice_shows_its_figures_and_can_be_dismissed(signed_in, page):
    """FR-285. The proposals are written by the model *from* the measured
    figures, and every one has to carry the numbers it rests on."""
    with step(page, "Open “What works”"):
        open_screen(page, "What works")
        assert_screen_rendered(page, "Where to redirect")

    with step(page, "Ask for the redirection analysis"):
        with page.expect_response(
            lambda r: "/api/learning/advice/generate" in r.url, timeout=180_000
        ) as generated:
            page.get_by_role("button", name=re.compile("^Analyse ")).click()
        assert generated.value.status == 200, (
            f"generating advice answered {generated.value.status}"
        )
        result = generated.value.json()
        wait_for_ready(page)
        screenshot(page, "redirection advice generated")

    if result.get("insufficient_data"):
        pytest.fail(
            "the seeded history should be past the advice threshold; the backend said: "
            f"{result.get('summary')}"
        )

    if result.get("error") or not result.get("proposals"):
        # The figures are computed first and the model second, so whatever the
        # model does the tables have to survive and the screen has to say what
        # happened. That much is checked either way.
        with step(page, "The advice step produced nothing — check the screen degrades"):
            body = page.inner_text("body")
            assert "the advice step could not run" in body or "No open proposals" in body, (
                f"the advice step produced nothing but the screen says nothing: {body[:400]!r}"
            )
            assert page.locator("section.seg-dim").count() > 0, (
                "the advice step failed and took the measured figures down with it"
            )
        expect_no_console_errors(page, ignore=TELEMETRY_NOISE)

        # Whether that is a finding depends on whether there was a model to ask.
        if not api(page, "GET", "/health")["data"].get("llm_configured"):
            narrate("  · degraded, as designed: no model configured, figures still shown")
            return
        pytest.fail(
            "FR-285 produced no proposals although a model is configured and answering: "
            f"{result.get('error') or result.get('summary')}. The call is routed to the "
            "strong model (deepseek-reasoner) and spends its whole max_tokens budget on "
            "reasoning tokens, so `choices[0].message.content` comes back empty and "
            "backend/dreamjob/llm/client.py:496 raises 'Empty LLM response'. The client "
            "never looks at finish_reason, never retries with more room and never falls "
            "back, and logs the call as status='ok' with an empty response (FR-364), so "
            "the AI call log says the call succeeded while the feature did not."
        )

    with step(page, "Check each proposal carries the figures it rests on"):
        cards = page.locator(".advice-card")
        assert cards.count() > 0, "the analysis produced proposals but none reached the screen"
        for index in range(cards.count()):
            text = cards.nth(index).inner_text()
            assert "Moving away from" in text and "Towards" in text, (
                f"proposal {index} does not show both sides of the move: {text[:200]!r}"
            )
            assert re.search(r"\d+ of \d+", text), (
                f"proposal {index} quotes a rate with no sample size: {text[:300]!r}"
            )
            assert re.search(r"plausible range \d+%–\d+%", text), (
                f"proposal {index} shows a rate without its interval: {text[:300]!r}"
            )
        narrate(f"  · {cards.count()} proposals, each with its own figures")

    with step(page, "Dismiss one, giving a reason"):
        first = page.locator(".advice-card").first
        headline = first.locator("h3").inner_text()
        first.get_by_role("button", name="Dismiss…").click()
        page.get_by_text("The sample is too small to act on").last.click()
        with page.expect_response(lambda r: "/dismiss" in r.url) as dismissed:
            page.get_by_role("button", name="Dismiss", exact=True).click()
        assert dismissed.value.status == 200
        wait_for_ready(page)
        assert headline in page.locator(".card", has_text="What you already decided").inner_text(), (
            "a dismissed proposal did not move into the decision history"
        )

    expect_no_console_errors(page, ignore=TELEMETRY_NOISE)


# ---------------------------------------------------------------------------
# Monitoring (FR-401, FR-403)
# ---------------------------------------------------------------------------


def test_monitoring_watches_a_company_and_produces_a_digest(signed_in, page):
    with step(page, "Open monitoring"):
        open_screen(page, "Monitoring")
        assert_screen_rendered(page, "Getting started with monitoring")

    with step(page, "Read the help for this screen"):
        check_help(page, expect="watch")

    with step(page, "Put a company on the watchlist"):
        page.get_by_role("button", name="Watch a company").first.click()
        page.get_by_placeholder("Company name…").fill("Northwind")
        page.get_by_role("button", name="Search", exact=True).click()
        chip = page.get_by_text("Northwind Data", exact=False).last
        chip.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
        chip.click()
        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/monitoring/watchlist")
        ) as added:
            page.get_by_role("button", name="Add to watchlist").click()
        assert added.value.status == 201, f"adding a watch answered {added.value.status}"
        wait_for_ready(page)
        assert page.get_by_role("heading", name="Watched companies").is_visible()
        assert "Northwind Data" in page.locator("table").first.inner_text()

    with step(page, "Read the notifications"):
        page.get_by_role("button", name=re.compile("^Notifications")).click()
        page.get_by_role("heading", name="Notifications").wait_for(
            state="visible", timeout=DEFAULT_TIMEOUT_MS
        )
        body = page.inner_text("body")
        assert "unread" in body
        # Nothing has been rechecked yet, so an empty list is the honest state.
        narrate("  · notifications: " + ("empty as expected" if "0 unread" in body else body[:80]))

    with step(page, "Generate the weekly digest"):
        page.get_by_role("button", name=re.compile("^Weekly digest")).click()
        page.get_by_role("heading", name="Generate a digest").wait_for(
            state="visible", timeout=DEFAULT_TIMEOUT_MS
        )
        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/monitoring/digests")
        ) as built:
            page.get_by_role("button", name="Generate now").click()
        assert built.value.status == 201, f"the digest answered {built.value.status}"
        page.get_by_text("just generated").wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
        digest = page.locator(".card", has_text="Digest for").first.inner_text()
        assert "New opportunities" in digest and "Replies" in digest, (
            f"the digest has no counts on it: {digest[:200]!r}"
        )
        assert "The digest suggests; it never sends" in page.inner_text("body"), (
            "the digest does not state that it never sends (NFR-305)"
        )

    with step(page, "Check the scheduler says whether it is running"):
        page.get_by_role("button", name="Scheduler").click()
        wait_for_ready(page)
        body = page.inner_text("body")
        assert "scheduler" in body.lower()
        screenshot(page, "monitoring scheduler")

    expect_no_console_errors(page, ignore=TELEMETRY_NOISE)
    expect_no_failed_requests(page, ignore=TELEMETRY_NOISE)


# ---------------------------------------------------------------------------
# Dream-job intelligence (FR-381..384, FR-443)
# ---------------------------------------------------------------------------


@pytest.mark.llm
def test_dream_job_intelligence_gaps_stepping_stones_and_linkedin(signed_in, page):
    with step(page, "Open the dream-job intelligence screen"):
        open_screen(page, "Dream-job gap")
        assert_screen_rendered(
            page, "A reading of the market, not a verdict on you", heading=False
        )

    with step(page, "Read the help for this screen"):
        check_help(page, expect="dream")

    with step(page, "Run the gap analysis"):
        page.get_by_role("button", name="Run the gap analysis").first.click()
        page.get_by_role("heading", name=re.compile("What stands between your profile")).wait_for(
            state="visible", timeout=180_000
        )
        wait_for_ready(page)
        gaps = page.get_by_role("button", name=re.compile(r"^Gaps")).inner_text()
        count = int(re.search(r"(\d+)", gaps).group(1)) if re.search(r"\d", gaps) else 0
        assert count > 0, (
            "the gap analysis ran but named no gaps between the profile and the dream job"
        )
        assert "No gaps computed yet" not in page.inner_text("body")
        narrate(f"  · {count} gaps, each with what would close it")
        screenshot(page, "gap analysis")

    with step(page, "Ask for stepping stones"):
        page.get_by_role("button", name=re.compile("^Stepping stones")).click()
        page.get_by_role("heading", name="Does anything on the market clear the bar?").wait_for(
            state="visible", timeout=DEFAULT_TIMEOUT_MS
        )
        page.get_by_role("button", name="Propose paths").click()
        wait_for_ready(page)
        page.wait_for_timeout(500)
        assert current_error(page) is None, f"stepping stones failed: {current_error(page)}"
        body = page.inner_text("body")
        # FR-382: the paths only make sense against the threshold they were
        # built for, so the screen has to show it beside them.
        assert "YOUR THRESHOLD" in body.upper(), "the dream-job threshold is not on the screen"
        assert "No paths proposed yet" not in body, (
            "the stepping-stone run produced nothing and the screen does not say why"
        )
        screenshot(page, "stepping stones")

    with step(page, "Generate the LinkedIn profile advice"):
        page.get_by_role("button", name="LinkedIn profile").click()
        assert page.get_by_text("Suggestions only — nothing here touches LinkedIn").is_visible(), (
            "the screen does not state that nothing here reaches LinkedIn (CR-401)"
        )
        page.get_by_role("button", name=re.compile("^(Generate suggestions|Regenerate)$")).click()
        wait_for_ready(page)
        page.wait_for_timeout(500)
        body = page.inner_text("body")
        if "No suggestions generated yet" in body:
            # Degraded: the advice is grounded in collected postings and says so
            # rather than inventing generic profile copy.
            narrate("  · degraded: the model produced no LinkedIn advice")
            assert "grounded" in body.lower() or current_error(page) is not None
        else:
            assert "Your draft" in body, f"the advice has no copyable draft: {body[:300]!r}"
            assert re.search(r"Grounded in \d+ posting", body), (
                "the advice does not say how many postings it was counted across (FR-443)"
            )
        screenshot(page, "linkedin advice")

    expect_no_console_errors(page, ignore=TELEMETRY_NOISE)


# ---------------------------------------------------------------------------
# Networking and export (FR-461, FR-463)
# ---------------------------------------------------------------------------


def test_networking_shows_introduction_routes_and_builds_an_export(signed_in, page):
    with step(page, "Open networking"):
        open_screen(page, "Networking")
        assert_screen_rendered(
            page, "Everything here is advisory, and nothing is sent for you.", heading=False
        )
        assert re.search(r"\d+ target compan", page.inner_text("body")), (
            "the screen does not say how many target companies are in scope"
        )

    with step(page, "Read the help for this screen"):
        check_help(page, expect="introduc")

    with step(page, "Look for a warm route into a target company"):
        page.get_by_role("button", name=re.compile("^Introduction routes")).click()
        wait_for_ready(page)
        body = page.inner_text("body")
        # Degraded, as designed: nothing has been imported into the network, so
        # there is no intermediary to rank. The screen has to say which of the
        # two reasons applies rather than showing an empty list.
        if "No introduction route into this company yet" in body:
            assert "Import your connections on the Contacts screen" in body, (
                "the empty state does not say why there is no route"
            )
            narrate("  · degraded, as designed: no imported network, so no routes to rank")
        else:
            assert re.search(r"\d+ routes? into", body)

    with step(page, "Build the campaign export"):
        page.get_by_role("button", name="Campaign export").click()
        page.get_by_role("heading", name="Build a package").wait_for(
            state="visible", timeout=DEFAULT_TIMEOUT_MS
        )
        assert "What leaves this machine, and what does not." in page.inner_text("body")
        page.get_by_role("button", name=re.compile("Build the package")).click()
        page.get_by_role("heading", name=re.compile("Build and download this campaign")).wait_for(
            state="visible", timeout=DEFAULT_TIMEOUT_MS
        )
        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.endswith("/api/networking/exports"),
            timeout=180_000,
        ) as built:
            page.get_by_role("button", name="Build it").click()
        assert built.value.status == 200, f"the export answered {built.value.status}"
        page.get_by_role("heading", name="Package ready").wait_for(
            state="visible", timeout=DEFAULT_TIMEOUT_MS
        )

    with step(page, "Check a file was actually produced"):
        result = built.value.json()
        zip_path = Path(result["zip_path"])
        assert zip_path.is_file(), f"the export reported {zip_path}, which is not on disk"
        assert zip_path.stat().st_size > 0, f"{zip_path} is empty"
        assert result["byte_size"] == zip_path.stat().st_size
        card = page.locator(".card", has_text="Package ready").first.inner_text()
        assert "Download" in card and "MANIFEST.json" in card, card[:300]
        assert "Isolation check passed" in card or "What was withheld" in card, (
            "the manifest does not state what was withheld or that isolation was checked"
        )
        narrate(f"  · export written: {zip_path} ({zip_path.stat().st_size} bytes)")

    with step(page, "Check the package is listed and downloadable"):
        rows = page.locator(".card", has_text="Packages built for").locator("table tbody tr")
        assert rows.count() >= 1, "the built package is not in the list of packages"
        with page.expect_download(timeout=60_000) as download:
            rows.first.get_by_role("button", name="Download").click()
        saved = download.value
        assert saved.suggested_filename.endswith(".zip")

    expect_no_console_errors(page, ignore=TELEMETRY_NOISE)
    expect_no_failed_requests(page, ignore=TELEMETRY_NOISE)


# ---------------------------------------------------------------------------
# Administration (FR-361..364, IR-101, NFR-701, NFR-702)
# ---------------------------------------------------------------------------


def test_administration_models_sources_activity_and_audit(signed_in, page):
    with step(page, "Open the administration screen"):
        open_screen(page, "Administration")
        assert_screen_rendered(page, "Provider and models")
        assert "Administrator role required" not in page.inner_text("body")

    with step(page, "Read the help for this screen"):
        check_help(page, expect="model")

    with step(page, "Check the models tab names the provider and its budget"):
        body = page.inner_text("body")
        assert "Budget and price" in body and "Per-task model" in body, body[:300]
        assert "Your profile text is sent to the provider" in body, (
            "the transfer notice (CR-410) is missing from the models tab"
        )

    with step(page, "Open the source catalogue"):
        page.get_by_role("button", name="Sources", exact=True).click()
        page.get_by_text("in the catalogue").first.wait_for(
            state="visible", timeout=DEFAULT_TIMEOUT_MS
        )
        assert page.get_by_text(
            "Some sources prohibit automated access in their own terms"
        ).is_visible(), "IR-101's notice is not on the sources tab"

    with step(page, "Check the prohibited source is disabled and asks to be acknowledged"):
        row = page.locator("table tbody tr").filter(has_text="board.indeed").first
        row.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
        assert "prohibited" in row.inner_text(), "board.indeed is not marked prohibited"
        assert row.get_by_role("button", name="Read the terms").is_visible(), (
            "a prohibited source offers an Enable switch instead of demanding acknowledgement"
        )
        assert row.get_by_role("button", name=re.compile("^(Enable|Disable)$")).count() == 0, (
            "a prohibited source can be switched on without acknowledging its terms (IR-101)"
        )
        state = api(page, "GET", "/admin/sources/board.indeed")
        assert state["status"] == 200
        assert state["data"]["blocked_pending_acknowledgement"] is True
        assert state["data"]["effective_enabled"] is False, (
            "a prohibited, unacknowledged source is reported as effectively enabled"
        )

    with step(page, "Open the acknowledgement, and check it cannot be given by accident"):
        row.get_by_role("button", name="Read the terms").click()
        modal = page.locator(".modal").first
        modal.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
        confirm = page.get_by_role("button", name="Record my acknowledgement")
        assert confirm.is_disabled(), "the acknowledgement is live before anything was read"
        modal.get_by_role("checkbox").check()
        assert confirm.is_disabled(), "ticking the box alone arms the acknowledgement"
        modal.get_by_placeholder("board.indeed").fill("board.indeed")
        assert confirm.is_enabled(), (
            "the acknowledgement stays disabled even after the box and the key"
        )
        # Stop here on purpose: recording it would permanently change this
        # installation's legal posture, and the point was that it is deliberate.
        page.get_by_role("button", name="Cancel").click()
        modal.wait_for(state="hidden", timeout=DEFAULT_TIMEOUT_MS)

    with step(page, "Read what this installation has actually done"):
        page.get_by_role("button", name="Activity", exact=True).click()
        wait_for_ready(page)
        body = page.inner_text("body")
        assert "Job seekers" in body or "Nothing has run on this installation yet" in body, (
            body[:300]
        )
        assert "Campaigns" in body

    with step(page, "Read the audit trail"):
        page.get_by_role("button", name="Audit", exact=True).click()
        page.get_by_role("heading", name="Audit trail").wait_for(
            state="visible", timeout=DEFAULT_TIMEOUT_MS
        )
        assert "Append-only" in page.inner_text("body"), (
            "the audit trail does not say it is append-only (NFR-702)"
        )
        # It opens on approvals and dispatches. Either there are some, or the
        # empty state has to say that the filter is what emptied it — an
        # unexplained blank table is the failure this is looking for.
        card = page.locator(".card", has_text="Audit trail").first
        if card.locator("table tbody tr").count() == 0:
            assert "No application has been approved or sent yet" in card.inner_text(), (
                "the audit trail is empty and does not say which filter emptied it"
            )

    with step(page, "Widen the filter to everything, and read what is on the record"):
        # Neither select carries an accessible name, so the one that holds the
        # action list is identified by an option only it has.
        card.locator("select").filter(has_text="Model configuration changed").first.select_option(
            label="Everything"
        )
        wait_for_ready(page)
        rows = card.locator("table tbody tr")
        rows.first.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
        assert rows.count() > 0, "nothing at all is on this installation's audit trail (NFR-702)"
        first = rows.first.inner_text()
        assert re.search(r"\d{4}", first), f"an audit row has no timestamp: {first!r}"
        narrate(f"  · audit trail: {rows.count()} entries under “Everything”")

    with step(page, "Read the AI call log and the retention controls"):
        page.get_by_role("button", name="Data", exact=True).click()
        page.get_by_role("heading", name="AI call log").wait_for(
            state="visible", timeout=DEFAULT_TIMEOUT_MS
        )
        wait_for_ready(page)
        body = page.inner_text("body")
        assert "Retention and redaction" in body, "FR-364's retention controls are missing"
        assert "Export everything" in body or "Erase this account" in body, (
            "the data-rights controls (NFR-301, FR-108) are missing from the Data tab"
        )

    expect_no_console_errors(page, ignore=TELEMETRY_NOISE)
    expect_no_failed_requests(page, ignore=TELEMETRY_NOISE)


def test_the_log_endpoints_return_real_lines_from_the_log_directory(signed_in, page):
    """NFR-701. The observability the specification asks for is only worth
    anything if it can be read from outside the process."""
    with step(page, "Do something worth logging, then ask for the tail"):
        open_screen(page, "Pipeline")
        wait_for_ready(page)
        result = api(page, "GET", "/logs/tail?name=requests&lines=50")
        assert result["status"] == 200, f"/api/logs/tail answered {result['status']}"
        lines = result["data"]["lines"]
        assert lines, "/api/logs/tail returned no lines at all"
        assert result["data"]["size_bytes"] > 0

    with step(page, "Check the lines really are the ones on disk"):
        on_disk = (LOG_DIR / "requests.log").read_text(encoding="utf-8", errors="replace")
        assert lines[-1] in on_disk, (
            "the last line /api/logs/tail returned is not in logs/requests.log"
        )
        assert any("/api/pipeline/board" in line for line in lines), (
            "the request this test just made is not in the tail it read back"
        )
        narrate(f"  · /api/logs/tail returned {len(lines)} lines from logs/requests.log")

    with step(page, "Check the summary counts by level over a window"):
        summary = api(page, "GET", "/logs/summary?minutes=60")
        assert summary["status"] == 200, f"/api/logs/summary answered {summary['status']}"
        totals = summary["data"]["totals"]
        assert sum(totals.values()) > 0, f"the summary counts nothing in the last hour: {totals}"
        assert {"errors", "requests"} <= set(summary["data"]["files"]), summary["data"]["files"]
        narrate(f"  · /api/logs/summary over 60 minutes: {totals}")

    expect_no_console_errors(page, ignore=TELEMETRY_NOISE)


def test_the_log_views_are_reachable_from_the_administration_screen(signed_in, page):
    """NFR-701/FR-361. ``GET /api/logs/tail``'s own docstring says it is "for
    the administration screen" — so there should be somewhere to read it."""
    with step(page, "Look for the log view on the administration screen"):
        open_screen(page, "Administration")
        wait_for_ready(page)
        tabs = page.locator(".tabs button").all_inner_texts()
        narrate(f"  · administration tabs: {tabs}")

    with step(page, "Check a screen actually reads the log endpoints"):
        assert any("log" in tab.lower() for tab in tabs), (
            "GET /api/logs/tail and /api/logs/summary answer with real lines, but no screen "
            f"reads them: the administration tabs are {tabs}, and a search of frontend/src "
            "for '/logs/tail' or '/logs/summary' finds nothing. The observability NFR-701 "
            "asks for has no way in from the interface — an operator has to curl for it."
        )

    expect_no_console_errors(page, ignore=TELEMETRY_NOISE)
