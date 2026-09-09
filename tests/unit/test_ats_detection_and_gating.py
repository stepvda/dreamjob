"""ATS slug detection and the SmartRecruiters gate (FR-182, FR-222, IR-101).

Two failures of the same kind - the product believing something it never
checked - and both were invisible because the wrong answer looked exactly like
a right one:

* ``detect_ats`` filed two Belgian companies under the board slug
  ``careers-analytics``.  That is Recruitee's analytics host, embedded by every
  careers site it serves; it answers 403 to a board read.  Every campaign
  afterwards planned a board that cannot exist, and each attempt was recorded as
  a source with no vacancies (docs/Data_Gathering_Plan.md N9).
* ``ats.smartrecruiters`` sat in the catalogue as an enabled, keyless API.
  ``api.smartrecruiters.com/robots.txt`` is ``Disallow: /`` for every agent but
  LinkedInBot, so FR-182 means it can never return a single row - but the
  FR-185 dashboard listed it as a working source (plan C6, 6.2).

Nothing here touches the network.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from dreamjob.adapters import load_all
from dreamjob.adapters.ats.detect import (
    detect_ats,
    detect_ats_verified,
    verification_url,
    verify_slug,
)
from dreamjob.adapters.base import get_adapter
from dreamjob.config import get_settings
from dreamjob.db.connection import execute, query_one, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.egress.client import RobotsDisallowed


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()


@dataclass
class StubResponse:
    url: str
    status_code: int = 200
    text: str = "{}"

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@dataclass
class StubEgress:
    """Answers every verification request with one status; records the calls."""

    status_code: int = 200
    raises: Exception | None = None
    calls: list[str] = field(default_factory=list)

    async def fetch(self, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append(url)
        if self.raises is not None:
            raise self.raises
        return StubResponse(url=url, status_code=self.status_code)


#: What a Recruitee-hosted careers page actually serves: the analytics host is
#: loaded first, in the widget bootstrap, and the company's own board second.
RECRUITEE_CAREERS_PAGE = """
<html><head>
  <script src="https://careers-analytics.recruitee.com/careers-site.js"></script>
</head><body>
  <div id="careers"></div>
  <a href="https://acme-belgium.recruitee.com/">See all our openings</a>
</body></html>
"""


# ---------------------------------------------------------------------------
# N9: a vendor's own hosts are not tenants
# ---------------------------------------------------------------------------


def test_the_analytics_host_is_skipped_and_the_real_board_is_found():
    """N9: ``careers-analytics`` was stored as the slug of two Belgian companies.

    Rejecting the name was only half of it: the scan stopped at the first match
    of each pattern, so rejecting ``careers-analytics`` used to throw away the
    Recruitee pattern altogether - and with it the company's real board, which
    is named eight lines further down the same page.
    """
    assert detect_ats(RECRUITEE_CAREERS_PAGE, "https://acme.be/jobs") == (
        "recruitee",
        "acme-belgium",
    )


def test_a_page_that_names_only_the_analytics_host_reports_the_vendor_alone():
    """No slug is a better answer than a slug that 403s: the vendor is still useful."""
    html = '<script src="https://careers-analytics.recruitee.com/careers-site.js"></script>'
    assert detect_ats(html, "https://acme.be/jobs") == ("recruitee", None)


def test_a_board_named_outside_an_attribute_survives_the_analytics_host():
    """The scan reads every match of a pattern, not only the first.

    A Recruitee widget names both hosts inside one script blob, so neither is an
    ``href``/``src`` attribute and the whole-page scan is what has to find the
    board.  Rejecting the analytics host used to reject the Recruitee pattern
    with it, and the page came back as "vendor known, board unknown".
    """
    html = (
        "<script>window.__RECRUITEE_CONFIG = {"
        'analyticsHost: "https://careers-analytics.recruitee.com/v1", '
        'boardHost: "https://acme-belgium.recruitee.com/"'
        "};</script>"
    )
    assert detect_ats(html, "https://acme.be/jobs") == ("recruitee", "acme-belgium")


@pytest.mark.parametrize(
    ("host", "vendor"),
    [
        ("careers-analytics.recruitee.com", "recruitee"),
        ("cdn.teamtailor.com", "teamtailor"),
        ("static.teamtailor.com", "teamtailor"),
        ("assets.teamtailor.com", "teamtailor"),
        ("app.recruitee.com", "recruitee"),
    ],
)
def test_a_vendor_owned_host_is_never_read_as_a_tenant(host, vendor):
    _, slug = detect_ats(f'<img src="https://{host}/logo.png">', "https://acme.be/careers")
    assert slug is None, f"{host} is the vendor's own host, not a board"


# ---------------------------------------------------------------------------
# N9: one request before the slug is believed
# ---------------------------------------------------------------------------


def test_a_detected_slug_is_confirmed_with_exactly_one_request():
    egress = StubEgress(status_code=200)
    found = asyncio.run(
        detect_ats_verified("", "https://job-boards.greenhouse.io/acme", egress)
    )
    assert found == ("greenhouse", "acme")
    assert egress.calls == ["https://boards-api.greenhouse.io/v1/boards/acme"]


def test_a_slug_no_board_answers_to_is_dropped_and_the_vendor_kept():
    """The ``careers-analytics`` case in one line: a real host that is not a board."""
    egress = StubEgress(status_code=403)
    found = asyncio.run(
        detect_ats_verified(
            '<a href="https://careers-analytics.recruitee.com/x">jobs</a>'
            '<a href="https://analytics-only.recruitee.com/">jobs</a>',
            None,
            egress,
        )
    )
    assert found == ("recruitee", None)
    assert egress.calls == ["https://analytics-only.recruitee.com/api/offers/"]


def test_a_verification_that_cannot_be_made_keeps_the_slug():
    """Unverified is not the same claim as wrong; a blocked probe must not delete data."""
    egress = StubEgress(raises=RobotsDisallowed("robots.txt disallows the board"))
    found = asyncio.run(detect_ats_verified("", "https://jobs.lever.co/acme", egress))
    assert found == ("lever", "acme")

    egress = StubEgress(raises=TimeoutError("network went away"))
    assert asyncio.run(verify_slug("lever", "acme", egress)) is None


def test_smartrecruiters_slugs_are_never_probed():
    """FR-182: api.smartrecruiters.com is Disallow:/ for every agent but LinkedInBot.

    Verification must not be the back door that starts requesting it, and the
    product does not present itself as LinkedInBot to get in (plan section 6.2).
    """
    assert verification_url("smartrecruiters", "Acme") is None
    egress = StubEgress(status_code=200)
    found = asyncio.run(
        detect_ats_verified("", "https://careers.smartrecruiters.com/Acme", egress)
    )
    assert found == ("smartrecruiters", "Acme")     # reported, unverified
    assert egress.calls == [], "no request may go to a robots-disallowed host"


def test_a_workday_site_has_no_one_request_proof_and_is_left_alone():
    """Workday lists over POST with a body, so there is no cheap GET that proves a site."""
    assert verification_url("workday", "acme.wd3.myworkdayjobs.com/External") is None
    egress = StubEgress(status_code=404)
    found = asyncio.run(
        detect_ats_verified(
            "", "https://acme.wd3.myworkdayjobs.com/en-US/External/job/Brussels/D_JR1", egress
        )
    )
    assert found == ("workday", "acme.wd3.myworkdayjobs.com/External")
    assert egress.calls == []


def test_detection_without_an_egress_client_is_plain_detection():
    assert asyncio.run(detect_ats_verified("", "https://jobs.lever.co/acme")) == (
        "lever",
        "acme",
    )


# ---------------------------------------------------------------------------
# C6: SmartRecruiters is catalogued as disabled, with the reason
# ---------------------------------------------------------------------------


def test_smartrecruiters_is_catalogued_as_disabled_with_the_reason(db):
    """C6: it advertised itself as a keyless API and could never collect a row."""
    load_all()
    row = query_one(
        "SELECT enabled, requires_ack, acknowledged_at, legal_notes, tos_status "
        "FROM source_catalogue WHERE adapter_key = ?",
        ("ats.smartrecruiters",),
    )
    assert row is not None
    assert row["enabled"] == 0
    assert row["acknowledged_at"] is None
    assert "robots.txt" in row["legal_notes"]
    assert "LinkedInBot" in row["legal_notes"]
    assert get_adapter("ats.smartrecruiters").is_enabled() is False

    # A source that can be read is not caught by the same net.
    assert query_one(
        "SELECT enabled FROM source_catalogue WHERE adapter_key = ?", ("ats.greenhouse",)
    )["enabled"] == 1


def test_an_acknowledged_smartrecruiters_row_is_not_switched_off_again(db):
    """IR-101: an administrator with an arrangement decides, and a restart does not undo it."""
    load_all()
    execute(
        "UPDATE source_catalogue SET enabled = 1, acknowledged_at = ? WHERE adapter_key = ?",
        (utcnow(), "ats.smartrecruiters"),
    )
    get_adapter("ats.smartrecruiters").register()
    assert query_one(
        "SELECT enabled FROM source_catalogue WHERE adapter_key = ?", ("ats.smartrecruiters",)
    )["enabled"] == 1
