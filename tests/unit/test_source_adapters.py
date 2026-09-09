"""ATS and job-board source adapters (FR-181..183, FR-261, IR-101, NFR-403, NFR-601).

Nothing here touches the network or an LLM: the egress layer is replaced by a
stub that serves recorded payloads (trimmed copies of the real Greenhouse,
Recruitee and Personio responses), and the LLM fallback is exercised with a
stub client that records the call it was asked to make.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from dreamjob.adapters import load_all
from dreamjob.adapters.ats.common import company_targets
from dreamjob.adapters.ats.detect import adapter_key_for, board_url, detect_ats
from dreamjob.adapters.ats.workday import split_slug
from dreamjob.adapters.base import PlanItem, all_adapters, get_adapter
from dreamjob.adapters.vacancy_source import (
    SourceUnavailable,
    application_route,
    detect_language,
    jobposting_to_fields,
    jsonld_jobpostings,
    normalise_contract_type,
    normalise_work_arrangement,
    parse_datetime,
    vacancy_dedup_key,
)
from dreamjob.config import get_settings
from dreamjob.db.connection import query_all, query_one
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


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

@dataclass
class StubResponse:
    url: str
    text: str
    status_code: int = 200
    raw_document_id: str | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@dataclass
class StubEgress:
    """Serves recorded payloads; records every URL the adapter asked for."""

    pages: dict[str, str]
    calls: list[str] = field(default_factory=list)
    default_status: int = 404

    async def fetch(self, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append(url)
        for pattern, body in self.pages.items():
            if pattern in url:
                return StubResponse(url=url, text=body, raw_document_id="raw-1")
        return StubResponse(url=url, text="", status_code=self.default_status)


class StubBudget:
    def should_degrade(self) -> bool:
        return False


@dataclass
class StubLLM:
    reply: dict
    calls: list[dict] = field(default_factory=list)
    budget: StubBudget = field(default_factory=StubBudget)

    def complete_json(self, task: str, **kwargs: Any) -> dict:
        self.calls.append({"task": task, **kwargs})
        return dict(self.reply)


GREENHOUSE_BODY = json.dumps(
    {
        "jobs": [
            {
                "id": "6136160004",
                "title": "Data Engineer",
                "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/6136160004",
                "company_name": "Acme NV",
                "language": "en",
                "first_published": "2026-08-06T12:50:10-04:00",
                "updated_at": "2026-08-18T18:06:19-04:00",
                "location": {"name": "Hybrid - Brussels"},
                "departments": [{"name": "Data"}],
                "offices": [{"name": "Brussels", "location": "Brussels, Belgium"}],
                "metadata": [{"name": "Employment type", "value": "Permanent"}],
                "content": (
                    "&lt;h2&gt;About the role&lt;/h2&gt;&lt;p&gt;You will build pipelines."
                    "&lt;/p&gt;&lt;h3&gt;Requirements&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;Python and "
                    "SQL&lt;/li&gt;&lt;li&gt;Airflow, dbt and Snowflake&lt;/li&gt;&lt;/ul&gt;"
                ),
            },
            {
                "id": "7",
                "title": "Office Manager",
                "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/7",
                "company_name": "Acme NV",
                "location": {"name": "Brussels"},
                "content": "&lt;p&gt;Front desk.&lt;/p&gt;",
            },
        ],
        "meta": {"total": 2},
    }
)

LISTING_HTML = """
<html><body>
  <table>
    <tr class="job-post"><td><a href="/acme/jobs/1">
      <p class="body--medium">Data Engineer</p>
      <p class="body__secondary">Gent, Belgium</p></a></td></tr>
    <tr class="job-post"><td><a href="/acme/jobs/2">
      <p class="body--medium">Data Analist</p>
      <p class="body__secondary">Antwerpen</p></a></td></tr>
  </table>
</body></html>
"""

DETAIL_HTML = """
<html><body>
  <h1>Data Engineer</h1>
  <div class="job__location">Gent, Belgium</div>
  <div class="job__description">
    <p>Je bouwt datapijplijnen voor onze klanten en werkt samen met het team.</p>
    <h3>Jouw profiel</h3>
    <ul><li>Python en SQL</li><li>Ervaring met Airflow en dbt</li></ul>
    <p>Contract van onbepaalde duur, hybride werken.</p>
  </div>
</body></html>
"""

RESISTANT_HTML = (
    "<html><body><div id=x>Vacature: Data Engineer bij Acme te Gent.</div></body></html>"
)

JSONLD_HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org/","@type":"JobPosting",
 "title":"Data Engineer","datePosted":"2026-08-06",
 "employmentType":"FULL_TIME","inLanguage":"nl",
 "hiringOrganization":{"@type":"Organization","name":"Acme NV"},
 "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress",
   "addressLocality":"Gent","addressRegion":"Oost-Vlaanderen","addressCountry":"BE"},
   "geo":{"latitude":51.05,"longitude":3.72}},
 "baseSalary":{"@type":"MonetaryAmount","currency":"EUR",
   "value":{"@type":"QuantitativeValue","minValue":4200,"maxValue":5200,"unitText":"MONTH"}},
 "url":"https://boards.example.com/jobs/1",
 "description":"<p>Je bouwt datapijplijnen.</p><h3>Vereisten</h3><ul><li>Python, SQL</li></ul>"}
</script>
</head><body></body></html>
"""


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Registry and terms-of-service gating
# ---------------------------------------------------------------------------

def test_load_all_registers_every_ats_and_board(db):
    """NFR-601: adapters are discovered by walking the package, not by a list."""
    count = load_all()
    keys = set(all_adapters())
    expected = {
        "ats.greenhouse", "ats.lever", "ats.smartrecruiters", "ats.ashby",
        "ats.recruitee", "ats.personio", "ats.workday",
        "board.generic", "board.vdab", "board.eures", "board.jobat",
        "board.stepstone", "board.indeed", "board.wttj",
    }
    assert expected <= keys
    assert count >= len(expected)

    catalogued = {r["adapter_key"] for r in query_all("SELECT adapter_key FROM source_catalogue")}
    assert expected <= catalogued


def test_restricted_sources_are_disabled_until_acknowledged(db):
    """IR-101: 'terms prohibit automated access' means off by default."""
    load_all()
    assert get_adapter("board.indeed").tos_status.value == "prohibited"
    for key in ("board.indeed", "board.stepstone", "ats.smartrecruiters"):
        adapter = get_adapter(key)
        assert adapter.requires_ack is True
        assert adapter.is_enabled() is False, key

    row = query_one(
        "SELECT tos_status, requires_ack, legal_notes FROM source_catalogue WHERE adapter_key = ?",
        ("board.indeed",),
    )
    assert row["requires_ack"] == 1
    assert row["legal_notes"]

    assert get_adapter("ats.greenhouse").is_enabled() is True
    assert get_adapter("board.eures").is_enabled() is True


# ---------------------------------------------------------------------------
# ATS detection (feeds company.ats_vendor / ats_slug)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("html", "url", "expected"),
    [
        ("", "https://boards.greenhouse.io/vercel", ("greenhouse", "vercel")),
        ("", "https://job-boards.greenhouse.io/acme/jobs/12", ("greenhouse", "acme")),
        ("", "https://jobs.lever.co/leverdemo/681f", ("lever", "leverdemo")),
        ("", "https://careers.smartrecruiters.com/Acme", ("smartrecruiters", "Acme")),
        ("", "https://jobs.ashbyhq.com/ashby/74", ("ashby", "ashby")),
        ("", "https://nmbrs.recruitee.com/o/designer", ("recruitee", "nmbrs")),
        ("", "https://acme.jobs.personio.de/job/123", ("personio", "acme")),
        (
            "",
            "https://acme.wd3.myworkdayjobs.com/en-US/External/job/Brussels/Data_JR1",
            ("workday", "acme.wd3.myworkdayjobs.com/External"),
        ),
        (
            "",
            "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/External/jobs",
            ("workday", "acme.wd3.myworkdayjobs.com/External"),
        ),
        (
            '<a href="https://jobs.lever.co/acme">Open roles</a>',
            "https://acme.com/careers",
            ("lever", "acme"),
        ),
        (
            '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acmecorp"></script>',
            "https://acme.com/jobs",
            ("greenhouse", "acmecorp"),
        ),
        ("<p>We hire by email.</p>", "https://acme.com/careers", (None, None)),
    ],
)
def test_detect_ats(html, url, expected):
    assert detect_ats(html, url) == expected


def test_detect_ats_reports_vendors_without_an_adapter():
    """Knowing the ATS is useful profiling even when the board cannot be read."""
    vendor, slug = detect_ats('<iframe src="https://acme.teamtailor.com/jobs"></iframe>', None)
    assert (vendor, slug) == ("teamtailor", "acme")
    assert adapter_key_for(vendor) is None
    assert board_url(vendor, slug) == "https://acme.teamtailor.com/jobs"


def test_board_url_and_adapter_key_round_trip():
    assert adapter_key_for("greenhouse") == "ats.greenhouse"
    assert board_url("greenhouse", "acme") == "https://job-boards.greenhouse.io/acme"
    assert split_slug("acme.wd3.myworkdayjobs.com/External") == (
        "acme.wd3.myworkdayjobs.com", "acme", "External",
    )
    with pytest.raises(ValueError):
        split_slug("acme.wd3.myworkdayjobs.com")


# ---------------------------------------------------------------------------
# Deterministic extraction (FR-183) and normalisation (FR-261)
# ---------------------------------------------------------------------------

def test_jsonld_jobposting_maps_onto_vacancy_columns():
    postings = jsonld_jobpostings(JSONLD_HTML)
    assert len(postings) == 1
    fields = jobposting_to_fields(postings[0], "https://boards.example.com/jobs/1")
    assert fields["title"] == "Data Engineer"
    assert fields["company_name_raw"] == "Acme NV"
    assert fields["location"] == "Gent, Oost-Vlaanderen"
    assert fields["country"] == "BE"
    assert fields["latitude"] == pytest.approx(51.05)
    assert fields["salary_min"] == 4200
    assert fields["salary_max"] == 5200
    assert fields["salary_currency"] == "EUR"
    assert fields["contract_type"] == "permanent"
    assert fields["posted_at"].startswith("2026-08-06")
    assert fields["language"] == "nl"
    assert "python" in fields["required_skills"]
    assert fields["application_channel"] == "url"


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Je bouwt datapijplijnen voor onze klanten en werkt samen met het team van experts "
         "die elke dag met veel plezier aan onze producten werken bij ons in Gent.", "nl"),
        ("Vous rejoindrez notre équipe pour construire des pipelines de données avec nous "
         "dans le cadre de votre poste et de votre expérience chez nos clients.", "fr"),
        ("You will build data pipelines for our clients and work with the team that ships "
         "our product every day, and you will own the roadmap of this role.", "en"),
        ("Sie werden Datenpipelines für unsere Kunden bauen und mit dem Team arbeiten, das "
         "unser Produkt jeden Tag ausliefert und die Roadmap dieser Rolle verantwortet.", "de"),
    ],
)
def test_language_detection(text, language):
    assert detect_language(text) == language


@pytest.mark.parametrize(
    ("signal", "expected"),
    [
        ("We are an international company", None),      # must not match "intern"
        ("Permanent contract, full-time", "permanent"),
        ("Internship of 6 months", "fixed_term"),
        ("Contrat CDD de 12 mois", "fixed_term"),
        ("Freelance / B2B welcome", "freelance"),
        ("Uitzendopdracht via interim", "interim"),
        ("Contract van onbepaalde duur", "permanent"),
    ],
)
def test_contract_type_normalisation(signal, expected):
    assert normalise_contract_type(signal) == expected


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        (("Hybrid - Brussels",), "hybrid"),
        (("Fully remote within the EU",), "remote"),
        (("Op kantoor in Gent",), "onsite"),
        (("Brussels",), None),
    ],
)
def test_work_arrangement_normalisation(signals, expected):
    assert normalise_work_arrangement(*signals) == expected


def test_date_and_dedup_helpers():
    assert parse_datetime(1565990241800).startswith("2019-08-16")
    assert parse_datetime("2026-08-31 12:04:10 UTC") == "2026-08-31T12:04:10+00:00"
    assert parse_datetime("2026-07-13T09:50:21.127Z").startswith("2026-07-13T09:50:21")
    assert parse_datetime("Posted Today") is not None
    assert parse_datetime(None) is None
    assert vacancy_dedup_key("Data Engineer", "Acme NV", "Gent", "2026-08-06T00:00:00+00:00") == (
        "data-engineer|acme-nv|gent|2026-08"
    )


def test_application_route_prefers_the_ats_form_then_email():
    assert application_route("https://job-boards.greenhouse.io/acme/jobs/1")[0] == "ats_form"
    assert application_route("mailto:jobs@acme.com?subject=x") == ("email", "jobs@acme.com")
    assert application_route(None, "Send your CV to jobs@acme.com") == ("email", "jobs@acme.com")
    assert application_route("https://acme.com/apply")[0] == "url"


# ---------------------------------------------------------------------------
# The four-step contract, end to end, against recorded payloads
# ---------------------------------------------------------------------------

def test_greenhouse_plan_fetch_parse_normalise(db):
    """FR-181/FR-261: a board slug becomes vacancy rows with schema column names."""
    load_all()
    egress = StubEgress({"boards-api.greenhouse.io": GREENHOUSE_BODY})
    adapter = get_adapter("ats.greenhouse", egress)

    directives = {"companies": [{"id": "c1", "name": "Acme NV",
                                 "ats_vendor": "greenhouse", "ats_slug": "acme"}]}
    items = adapter.plan(directives, {}, {"max_records_per_source": 50})
    assert len(items) == 1
    assert items[0].native_query["slug"] == "acme"
    assert items[0].rationale

    records = _run(adapter.run(items[0]))
    assert len(records) == 2
    assert egress.calls == [
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    ]

    engineer = next(r for r in records if r.data["title"] == "Data Engineer")
    data = engineer.data
    assert engineer.entity_type == "vacancy"
    assert data["company_id"] == "c1"
    assert data["company_name_raw"] == "Acme NV"
    assert data["location"] == "Hybrid - Brussels"
    assert data["country"] == "BE"
    assert data["work_arrangement"] == "hybrid"
    assert data["contract_type"] == "permanent"
    assert data["function_family"] == "Data"
    assert data["application_channel"] == "ats_form"
    assert data["posted_at"].startswith("2026-08-06")
    assert data["source_adapter"] == "ats.greenhouse"
    assert data["access_method"] == "api"
    assert data["raw_document_id"] == "raw-1"
    assert data["dedup_key"].startswith("data-engineer|acme-nv|")
    assert {"python", "sql", "airflow", "dbt", "snowflake"} <= set(data["required_skills"])
    assert "&lt;" not in data["description"] and "<h2>" not in data["description"]

    # FR-261 says the record must use the knowledge-base columns, so prove it.
    columns = {c["name"] for c in query_all("PRAGMA table_info(vacancy)")}
    assert set(data) <= columns


def test_ats_plan_accepts_every_company_shape():
    """FR-162: the planner may hand companies over in several forms."""
    targets = company_targets(
        {"companies": ["acme", {"name": "Beta", "ats_vendor": "greenhouse", "ats_slug": "beta"},
                       {"name": "Gamma", "ats_vendor": "lever", "ats_slug": "gamma"}],
         "ats": {"greenhouse": ["delta"]}},
        {},
        "greenhouse",
    )
    slugs = [t["slug"] for t in targets]
    assert slugs == ["acme", "beta", "delta"]
    assert next(t for t in targets if t["slug"] == "beta")["company_name"] == "Beta"


def test_generic_html_board_is_driven_by_stored_selectors(db):
    """NFR-601: adding a board is a configuration row, not code."""
    load_all()
    egress = StubEgress({"/jobs/1": DETAIL_HTML, "/jobs/2": DETAIL_HTML,
                         "example.com/vacatures": LISTING_HTML})
    adapter = get_adapter("board.generic", egress)
    adapter.llm = StubLLM({"title": "should not be used"})

    item = PlanItem(
        adapter_key="board.generic",
        native_query={
            "start_urls": ["https://example.com/vacatures"],
            "max_records": 5,
            "country": "BE",
            "selectors": {
                "list_item": "tr.job-post",
                "url": "a@href",
                "title": "p.body--medium",
                "location": "p.body__secondary",
            },
            "detail_selectors": {
                "title": "h1",
                "location": ".job__location",
                "description": ".job__description",
            },
        },
    )
    records = _run(adapter.run(item))
    assert len(records) == 2
    assert adapter.llm.calls == []                       # deterministic path only
    first = records[0].data
    assert first["title"] == "Data Engineer"
    assert first["country"] == "BE"
    assert first["language"] == "nl"
    assert first["contract_type"] == "permanent"
    assert first["work_arrangement"] == "hybrid"
    assert first["source_url"] == "https://example.com/acme/jobs/1"
    assert "python" in first["required_skills"]
    assert adapter.extraction_rate == 1.0


def test_llm_extraction_is_the_fallback_for_pages_that_resist(db):
    """FR-183: deterministic first, LLM only when nothing else worked."""
    load_all()
    egress = StubEgress({"/jobs/1": RESISTANT_HTML, "example.com/vacatures": LISTING_HTML})
    adapter = get_adapter("board.generic", egress)
    adapter.llm = StubLLM(
        {
            "title": "Data Engineer",
            "company_name_raw": "Acme",
            "description": "Vacature bij Acme te Gent.",
            "required_skills": ["python"],
            "location": "Gent",
            "language": "nl",
        }
    )
    item = PlanItem(
        adapter_key="board.generic",
        native_query={
            "start_urls": ["https://example.com/vacatures"],
            "max_records": 1,
            "selectors": {"list_item": "tr.job-post", "url": "a@href",
                          "title": "p.body--medium"},
            "detail_selectors": {},
        },
    )
    records = _run(adapter.run(item))
    assert len(records) == 1
    assert len(adapter.llm.calls) == 1
    call = adapter.llm.calls[0]
    assert call["task"] == "extract.vacancy"
    # NFR-205: the page and its URL are isolated untrusted data, never instructions.
    assert set(call["untrusted"]) == {"page", "source_url"}
    assert "example.com" not in call["user"]
    assert records[0].data["confidence"] == 0.6
    assert records[0].data["title"] == "Data Engineer"


def test_extraction_rate_is_reported_to_the_catalogue(db):
    """NFR-403: adapter breakage surfaces within one campaign."""
    load_all()
    adapter = get_adapter("ats.greenhouse", StubEgress({}))
    adapter.record_extraction(4, 1)
    rate = adapter.report_extraction_rate()
    assert rate == pytest.approx(0.25)
    row = query_one(
        "SELECT extraction_success_rate, last_success_at FROM source_catalogue "
        "WHERE adapter_key = ?",
        ("ats.greenhouse",),
    )
    assert row["extraction_success_rate"] == pytest.approx(0.25)
    assert row["last_success_at"]


def test_paginating_adapters_fetch_only_the_page_the_pipeline_asks_for(db):
    """NFR-401: the pipeline re-issues a plan item per page; the adapter must obey."""
    load_all()
    lever_page = json.dumps(
        [{"text": "Data Engineer", "hostedUrl": "https://jobs.lever.co/acme/1",
          "applyUrl": "https://jobs.lever.co/acme/1/apply", "createdAt": 1565990241800,
          "country": "BE", "workplaceType": "remote",
          "categories": {"location": "Gent", "team": "Data", "commitment": "Full-time"},
          "descriptionPlain": "You build pipelines with Python and SQL."}]
    )
    egress = StubEgress({"api.lever.co": lever_page})
    adapter = get_adapter("ats.lever", egress)
    _run(adapter.run(PlanItem("ats.lever", {"slug": "acme", "max_records": 500, "page": 3})))
    assert egress.calls == [
        "https://api.lever.co/v0/postings/acme?mode=json&limit=100&skip=200"
    ]

    board = get_adapter("board.generic", StubEgress({}))
    urls = board.listing_urls(
        {"url_template": "https://b.tld/jobs?q={query}&p={page}", "queries": ["data"],
         "pages": 5, "page": 4}
    )
    assert urls == ["https://b.tld/jobs?q=data&p=4"]


def test_a_dead_board_fails_its_plan_item_instead_of_reporting_nothing(db):
    """FR-185: "every request was refused" is not "this source had no vacancies".

    The campaign still survives - ``collection_worker`` catches this per plan
    item - but the plan item is recorded as failed with the HTTP status against
    its name instead of ``done`` with zero records and zero errors, which is
    what the whole live database looked like.
    """
    load_all()
    egress = StubEgress({}, default_status=403)
    for key, native in (
        ("ats.greenhouse", {"slug": "acme"}),
        ("board.jobat", {"queries": ["data"], "locations": [""], "pages": 1}),
        ("board.eures", {"keyword": "data", "country_codes": ["BE"], "pages": 1}),
    ):
        adapter = get_adapter(key, egress)
        with pytest.raises(SourceUnavailable) as raised:
            _run(adapter.run(PlanItem(key, native)))
        assert "403" in str(raised.value), key
        assert key in str(raised.value)


def test_a_source_blocked_by_robots_txt_fails_its_own_plan_item(db):
    """FR-182/IR-101: a robots.txt block must reach the worker, not a log line.

    ``collection_worker`` has an ``except RobotsDisallowed`` handler that was
    dead code for every adapter here, because each one caught the exception and
    returned an empty list.
    """
    load_all()

    class BlockedEgress:
        async def fetch(self, url: str, **kwargs: Any) -> StubResponse:
            raise RobotsDisallowed(f"robots.txt disallows {url}")

    adapter = get_adapter("ats.smartrecruiters", BlockedEgress())
    with pytest.raises(RobotsDisallowed):
        _run(adapter.run(PlanItem("ats.smartrecruiters", {"slug": "Ubisoft"})))


def test_a_jsonld_listing_is_collected_without_a_request_per_advert(db):
    """FR-183: a listing that already carries JobPosting blocks needs no detail fetch."""
    load_all()
    postings = []
    for number, title in ((1, "Data Engineer"), (2, "Data Analist")):
        postings.append(
            json.dumps(
                {
                    "@context": "https://schema.org/",
                    "@type": "JobPosting",
                    "title": title,
                    "datePosted": "2026-08-06",
                    "hiringOrganization": {"@type": "Organization", "name": "Acme NV"},
                    "jobLocation": {
                        "@type": "Place",
                        "address": {"@type": "PostalAddress", "addressLocality": "Gent",
                                    "addressCountry": "BE"},
                    },
                    "url": f"https://example.com/jobs/{number}",
                    "description": "<p>Je bouwt datapijplijnen met Python en SQL.</p>",
                }
            )
        )
    listing = "<html><body>" + "".join(
        f'<script type="application/ld+json">{p}</script>' for p in postings
    ) + "</body></html>"

    egress = StubEgress({"example.com/vacatures": listing})
    adapter = get_adapter("board.generic", egress)
    adapter.llm = StubLLM({"title": "should not be used"})
    records = _run(
        adapter.run(
            PlanItem(
                adapter_key="board.generic",
                native_query={"start_urls": ["https://example.com/vacatures"],
                              "max_records": 5, "country": "BE", "detail": True},
            )
        )
    )
    assert [r.data["title"] for r in records] == ["Data Engineer", "Data Analist"]
    assert egress.calls == ["https://example.com/vacatures"]   # no per-advert request
    assert adapter.llm.calls == []


@dataclass
class SequencedEgress:
    """Like :class:`StubEgress`, but serves the next body per matching call."""

    pages: dict[str, list[str]]
    calls: list[str] = field(default_factory=list)

    async def fetch(self, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append(url)
        for pattern, bodies in self.pages.items():
            if pattern in url:
                seen = sum(1 for call in self.calls[:-1] if pattern in call)
                return StubResponse(
                    url=url, text=bodies[min(seen, len(bodies) - 1)], raw_document_id="raw-1"
                )
        return StubResponse(url=url, text="", status_code=404)


def test_eures_does_not_emit_a_vacancy_twice_when_paging(db):
    """A vacancy detailed from page two must not also come back as a summary.

    The shapes below are the live ones: a search answers ``{"numberRecords",
    "jvs"}`` whose entries carry ``locationMap`` and no employer, and a detail
    is ``GET /jv/id/{id}`` answering ``{"jvProfiles": {"<lang>": {...}}}``.
    """
    load_all()

    def summary(jv_id: str, title: str) -> dict:
        return {"id": jv_id, "title": title, "employer": None,
                "locationMap": {"BE": ["BE211"]},
                "creationDate": 1788494411399, "description": "Python en SQL."}

    def detail(jv_id: str, title: str) -> dict:
        return {"id": jv_id, "preferredLanguage": "nl", "creationDate": 1788494411399,
                "jvProfiles": {"nl": {"title": title, "description": "Python en SQL.",
                                      "employer": {"name": "Acme"},
                                      "locations": [{"cityName": "Gent",
                                                     "countryCode": "be"}]}}}

    egress = SequencedEgress(
        {
            "jv-search/search": [
                json.dumps({"numberRecords": 2, "jvs": [summary("1", "Data Engineer")]}),
                json.dumps({"numberRecords": 2, "jvs": [summary("2", "Data Analist")]}),
            ],
            "jv/id/1": [json.dumps(detail("1", "Data Engineer"))],
            "jv/id/2": [json.dumps(detail("2", "Data Analist"))],
        }
    )
    adapter = get_adapter("board.eures", egress)
    records = _run(
        adapter.run(
            PlanItem(
                adapter_key="board.eures",
                native_query={"keyword": "data", "country_codes": ["BE"], "pages": 2,
                              "results_per_page": 1, "fetch_details": True},
            )
        )
    )
    assert sorted(r.data["title"] for r in records) == ["Data Analist", "Data Engineer"]
    # The detail document is where the employer and the city live.
    assert {r.data["company_name_raw"] for r in records} == {"Acme"}
    assert {r.data["location"] for r in records} == {"Gent, BE"}
    assert {r.data["country"] for r in records} == {"BE"}


def test_workday_summary_keeps_the_listing_as_its_provenance(db):
    """FR-183/NFR-402: a posting whose detail page is unreadable still cites a raw document."""
    load_all()
    listing = json.dumps(
        {
            "total": 1,
            "jobPostings": [
                {
                    "title": "Data Engineer",
                    "externalPath": "/job/Brussels/Data-Engineer_R1",
                    "locationsText": "Brussels",
                    "postedOn": "Posted 3 Days Ago",
                }
            ],
        }
    )
    egress = StubEgress({"/wday/cxs/acme/External/jobs": listing})   # detail 404s
    adapter = get_adapter("ats.workday", egress)
    records = _run(
        adapter.run(
            PlanItem(
                adapter_key="ats.workday",
                native_query={"slug": "acme.wd3.myworkdayjobs.com/External", "max_records": 20},
            )
        )
    )
    assert len(records) == 1
    assert records[0].data["title"] == "Data Engineer"
    assert records[0].raw_document_id == "raw-1"
    assert records[0].data["raw_document_id"] == "raw-1"


# ---------------------------------------------------------------------------
# ATS plan items with no resolvable board slug are skipped, not failed (FR-186)
# ---------------------------------------------------------------------------


def _ats_item(adapter_key: str = "ats.greenhouse", **native: object) -> PlanItem:
    return PlanItem(adapter_key=adapter_key, native_query=native)


class TestAtsSlugResolution:
    def test_adapter_native_slug_is_read(self) -> None:
        from dreamjob.adapters.ats.common import ATSAdapter

        item = _ats_item(slug="acme")
        assert ATSAdapter.slug_of(item) == "acme"

    def test_llm_board_slugs_first_entry_is_read(self) -> None:
        """A plan the LLM produced as ``board_slugs`` must resolve too."""
        from dreamjob.adapters.ats.common import ATSAdapter

        item = _ats_item(vendor="greenhouse", board_slugs=["acme"])
        assert ATSAdapter.slug_of(item) == "acme"

    def test_empty_board_slugs_has_no_slug(self) -> None:
        """``has_slug`` is the graceful check the collection layer uses."""
        from dreamjob.adapters.ats.common import ATSAdapter

        item = _ats_item(vendor="greenhouse", board_slugs=[])
        assert ATSAdapter.has_slug(item) is False

    def test_slug_helpers_accept_a_plain_dict_plan_row(self) -> None:
        """The collection worker reads plan items from the DB as plain dicts.

        ``has_slug`` must work on that shape too - the row's ``native_query`` is
        the payload, and passing a dict in place of a ``PlanItem`` is exactly
        what the collection worker does.  A ``PlanItem``-only check crashed the
        whole collection job with an AttributeError instead of skipping.
        """
        from dreamjob.adapters.ats.common import ATSAdapter

        # The shape the worker sees: {"native_query": {...}, "status": ...}
        row = {"adapter_key": "ats.greenhouse", "native_query": {"board_slugs": [], "vendor": "greenhouse"}}
        assert ATSAdapter.has_slug(row) is False
        row2 = {"adapter_key": "ats.greenhouse", "native_query": {"slug": "acme"}}
        assert ATSAdapter.has_slug(row2) is True
        assert ATSAdapter.slug_of(row2) == "acme"

    def test_missing_slug_raises_in_slug_of_but_says_so(self) -> None:
        from dreamjob.adapters.ats.common import ATSAdapter

        item = _ats_item({})  # native_query is not a dict-like with slug
        assert ATSAdapter.has_slug(item) is False
        with pytest.raises(ValueError, match="missing a board slug"):
            ATSAdapter.slug_of(item)


class TestCollectionSkipGuard:
    def _adapter(self, key: str) -> object:
        from dreamjob.adapters import load_all
        from dreamjob.adapters.base import get_adapter

        load_all()
        return get_adapter(key)

    def test_ats_plan_item_without_slug_is_skipped(self) -> None:
        from dreamjob.pipeline.collection import _has_ats_slug, _is_ats

        adapter = self._adapter("ats.greenhouse")
        item = _ats_item(vendor="greenhouse", board_slugs=[])
        assert _is_ats(adapter) is True
        assert _has_ats_slug(adapter, item) is False

    def test_ats_plan_item_with_slug_proceeds(self) -> None:
        from dreamjob.pipeline.collection import _has_ats_slug, _is_ats

        adapter = self._adapter("ats.greenhouse")
        item = _ats_item(slug="acme")
        assert _is_ats(adapter) is True
        assert _has_ats_slug(adapter, item) is True

    def test_job_board_is_not_treated_as_ats(self) -> None:
        from dreamjob.pipeline.collection import _is_ats

        adapter = self._adapter("board.vdab")
        assert _is_ats(adapter) is False
