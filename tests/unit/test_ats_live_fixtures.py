"""ATS adapters against *captured real* board responses (FR-181, FR-261, FR-183).

Every fixture in ``tests/fixtures/ats`` is a verbatim (only truncated) response
from the vendor's public, keyless endpoint, captured live:

    Greenhouse   GET boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=true
    Lever        GET api.lever.co/v0/postings/leverdemo?mode=json
    Ashby        GET api.ashbyhq.com/posting-api/job-board/Ashby
    Recruitee    GET nmbrs.recruitee.com/api/offers/
    Personio     GET personio.jobs.personio.de/xml
    Workday      POST/GET salesforce.wd12.myworkdayjobs.com/wday/cxs/...

``tests/unit/test_source_adapters.py`` already covers the adapters against
hand-written payloads; those cannot notice a vendor changing a field name.
These tests parse the real shapes, so a silent schema drift fails here first.

SmartRecruiters has no fixture on purpose: api.smartrecruiters.com/robots.txt
is ``Disallow: /`` for everyone but LinkedInBot, and FR-182 makes that binding.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from dreamjob.adapters.ats.ashby import AshbyAdapter
from dreamjob.adapters.ats.greenhouse import GreenhouseAdapter
from dreamjob.adapters.ats.lever import LeverAdapter
from dreamjob.adapters.ats.personio import PersonioAdapter
from dreamjob.adapters.ats.recruitee import RecruiteeAdapter
from dreamjob.adapters.ats.workday import WorkdayAdapter
from dreamjob.adapters.base import RawRecord
from dreamjob.adapters.vacancy_source import VACANCY_COLUMNS

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ats"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@dataclass
class StubResponse:
    url: str
    text: str
    status_code: int = 200
    raw_document_id: str | None = "raw-1"

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@dataclass
class RecordingEgress:
    """Serves a captured body for any matching URL and records every call."""

    pages: dict[str, str] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    default_status: int = 404

    async def fetch(self, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append(url)
        for pattern, body in self.pages.items():
            if pattern in url:
                return StubResponse(url=url, text=body)
        return StubResponse(url=url, text="", status_code=self.default_status)


def raw(name: str, *, url: str, content_type: str = "application/json", **meta) -> RawRecord:
    base = {"slug": meta.pop("slug", "acme"), "company_id": None, "company_name": "Acme",
            "max_records": 500, "keywords": [], "title_filter": False}
    return RawRecord(url=url, content=fixture(name), content_type=content_type,
                     raw_document_id="raw-1", meta={**base, **meta})


CASES = [
    pytest.param(
        GreenhouseAdapter(),
        raw("greenhouse_gitlab_jobs.json",
            url="https://boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=true",
            slug="gitlab"),
        id="greenhouse",
    ),
    pytest.param(
        LeverAdapter(),
        raw("lever_leverdemo_postings.json",
            url="https://api.lever.co/v0/postings/leverdemo?mode=json&limit=100&skip=0",
            slug="leverdemo"),
        id="lever",
    ),
    pytest.param(
        AshbyAdapter(),
        raw("ashby_ashby_jobboard.json",
            url="https://api.ashbyhq.com/posting-api/job-board/Ashby?includeCompensation=true",
            slug="Ashby"),
        id="ashby",
    ),
    pytest.param(
        RecruiteeAdapter(),
        raw("recruitee_nmbrs_offers.json", url="https://nmbrs.recruitee.com/api/offers/",
            slug="nmbrs"),
        id="recruitee",
    ),
    pytest.param(
        PersonioAdapter(),
        raw("personio_personio_feed.xml", url="https://personio.jobs.personio.de/xml",
            content_type="application/xml", slug="personio", domain="personio.de", kind="list"),
        id="personio",
    ),
    pytest.param(
        WorkdayAdapter(),
        raw("workday_salesforce_detail.json",
            url="https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/x",
            slug="salesforce.wd12.myworkdayjobs.com/External_Career_Site", kind="detail",
            summary=json.loads(fixture("workday_salesforce_list.json"))["jobPostings"][0]),
        id="workday",
    ),
]


@pytest.mark.parametrize(("adapter", "record"), CASES)
def test_parse_reads_the_live_response_shape(adapter, record):
    """Every posting in a real board response becomes one parsed row with a title."""
    parsed = adapter.parse(record)
    assert parsed, f"{adapter.key} parsed nothing from a real response"
    for row in parsed:
        assert (row.get("title") or "").strip(), f"{adapter.key} produced a posting without a title"
        assert row.get("source_url"), f"{adapter.key} produced a posting without a source URL"


@pytest.mark.parametrize(("adapter", "record"), CASES)
def test_normalise_matches_the_vacancy_columns(adapter, record):
    """FR-261: every normalised key is a real ``vacancy`` column."""
    for parsed in adapter.parse(record):
        normalised = adapter.normalise(parsed, record)
        assert normalised is not None, f"{adapter.key} dropped a posting during normalise"
        assert normalised.entity_type == "vacancy"
        unknown = set(normalised.data) - VACANCY_COLUMNS
        assert not unknown, f"{adapter.key} wrote unknown vacancy columns: {sorted(unknown)}"
        assert normalised.data["source_adapter"] == adapter.key
        assert normalised.data["dedup_key"]
        assert normalised.data["raw_document_id"] == "raw-1"


def test_greenhouse_reads_the_fields_the_live_board_actually_sends():
    """The Greenhouse fields the adapter depends on, pinned to a real posting."""
    adapter = GreenhouseAdapter()
    record = raw("greenhouse_gitlab_jobs.json",
                 url="https://boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=true",
                 slug="gitlab")
    row = adapter.parse(record)[0]
    assert row["company_name_raw"] == "GitLab"          # jobs[].company_name
    assert row["location"]                               # jobs[].location.name
    assert row["function_family"]                        # jobs[].departments[0].name
    assert row["source_url"].startswith("https://")      # jobs[].absolute_url
    assert row["posted_at"]                              # jobs[].first_published
    # `content` is HTML-escaped HTML: unescaped once, then stripped to text.
    assert len(row["description"]) > 500
    assert "<p>" not in row["description"] and "&lt;" not in row["description"]


def test_personio_feed_without_descriptions_still_yields_a_posting():
    """Real Personio feeds routinely ship an empty <jobDescriptions/>.

    The adapter must still produce the posting from the feed's structured
    attributes; the advert text is filled in from the job page by fetch().
    """
    adapter = PersonioAdapter()
    record = raw("personio_personio_feed.xml", url="https://personio.jobs.personio.de/xml",
                 content_type="application/xml", slug="personio", domain="personio.de",
                 kind="list")
    row = adapter.parse(record)[0]
    assert row["title"]
    assert row["description"] == ""      # the live feed really is empty here
    assert row["contract_type"] == "permanent"
    assert row["location"] == "Munich, Berlin"   # office + additionalOffices


def test_workday_detail_uses_jobpostinginfo_not_the_summary():
    adapter = WorkdayAdapter()
    summary = json.loads(fixture("workday_salesforce_list.json"))["jobPostings"][0]
    record = raw("workday_salesforce_detail.json",
                 url="https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/x",
                 slug="salesforce.wd12.myworkdayjobs.com/External_Career_Site",
                 kind="detail", summary=summary)
    row = adapter.parse(record)[0]
    assert row["title"]
    assert len(row["description"]) > 500     # jobPostingInfo.jobDescription, HTML stripped
    assert row["source_url"].startswith("https://")


# ---------------------------------------------------------------------------
# Defects found by review, now fixed and pinned as plain assertions.
# ---------------------------------------------------------------------------


def test_a_plan_item_with_several_board_slugs_reads_every_board():
    """A plan naming three boards must read three boards, not one.

    ``slug_of`` returned ``board_slugs[0]`` and every ``fetch()`` called it
    once, so the second and third boards were never requested and the plan item
    still reported success.
    """
    from dreamjob.adapters.ats.common import ATSAdapter
    from dreamjob.adapters.base import PlanItem

    item = PlanItem("ats.greenhouse", {"vendor": "greenhouse",
                                       "board_slugs": ["gitlab", "stripe", "figma"]})
    assert ATSAdapter.slugs_of(item) == ["gitlab", "stripe", "figma"]

    egress = RecordingEgress({"boards-api.greenhouse.io": fixture(
        "greenhouse_gitlab_jobs.json")})
    adapter = GreenhouseAdapter(egress)
    records = asyncio.run(adapter.run(item))
    assert egress.calls == [
        "https://boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=true",
        "https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true",
        "https://boards-api.greenhouse.io/v1/boards/figma/jobs?content=true",
    ]
    assert len(records) == 3 * len(json.loads(fixture("greenhouse_gitlab_jobs.json"))["jobs"])


def test_a_malformed_board_slug_payload_is_no_slug_rather_than_a_crashed_job():
    """``has_slug`` runs outside every exception handler in the worker.

    A ``board_slugs`` mapping raised ``KeyError: 0`` out of ``slug_of``, out of
    the item loop and out of the job, which failed the whole collection run and
    left every plan item - including the healthy ones behind it - untouched.
    """
    from dreamjob.adapters.ats.common import ATSAdapter

    assert ATSAdapter.has_slug({"native_query": {"board_slugs": {"primary": "gitlab"}}}) is True
    assert ATSAdapter.slugs_of({"native_query": {"board_slugs": {"primary": "gitlab"}}}) == [
        "gitlab"
    ]
    for payload in ({"board_slugs": 7}, {"board_slugs": None}, {"slug": ""}, "not-a-dict", None):
        assert ATSAdapter.has_slug({"native_query": payload}) is False


def test_fetch_accepts_the_plan_row_shape_that_has_slug_accepts():
    """The collection worker reads plan items from the database as plain dicts.

    ``has_slug`` accepted that shape while ``limit_of`` and every ``fetch()``
    still read ``item.native_query`` as an attribute - the recorded failure
    "'dict' object has no attribute 'native_query'" that took down a whole
    collection job.
    """
    row = {"adapter_key": "ats.greenhouse", "native_query": {"slug": "gitlab"}}
    egress = RecordingEgress({"boards-api.greenhouse.io": fixture(
        "greenhouse_gitlab_jobs.json")})
    adapter = GreenhouseAdapter(egress)
    assert adapter.has_slug(row) is True
    assert adapter.limit_of(row) == 500
    records = asyncio.run(adapter.run(row))          # used to raise AttributeError
    assert records and records[0].entity_type == "vacancy"
