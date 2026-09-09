"""Planning, knowledge-base reuse and collection (FR-161..166, FR-181..186, FR-341..345).

Everything here runs against a throw-away SQLite file with no network and no
LLM: the planner is exercised with ``use_llm=False`` (its NFR-104 degradation
path) and collection with a stub adapter registered in the normal registry.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from dreamjob.adapters.base import (
    AdapterCapabilities,
    NormalisedRecord,
    PlanItem,
    RawRecord,
    SourceAdapter,
    SourceType,
    register_adapter,
)
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_all, query_one, upsert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import knowledge as kb_repo
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.pipeline import collection, dedup, knowledge_base, planning


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def _seeker() -> str:
    return insert_row(
        "job_seeker",
        {
            "email": "test@example.com",
            "display_name": "Test Seeker",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )


def _campaign(seeker_id: str, **overrides) -> str:
    directive_id = insert_row(
        "directive_set",
        {
            "job_seeker_id": seeker_id,
            "name": "Benelux data leadership",
            "job_content": {"roles": ["Data Engineering Manager", "Head of Data"]},
            "location": {"countries": ["Belgium", "NL"], "cities": ["Gent", "Antwerp"]},
            "notes_to_ai": "Prefer scale-ups.",
            "created_at": utcnow(),
            **overrides.pop("directives", {}),
        },
    )
    profile_id = insert_row(
        "profile_version",
        {
            "job_seeker_id": seeker_id,
            "version": 1,
            "sections": {
                "experience": [{"company": "Acme NV", "title": "Data Lead"}],
                "education": [{"school": "Ghent University"}],
            },
            "created_at": utcnow(),
        },
    )
    return campaign_repo.create_campaign(
        seeker_id,
        {
            "name": "Autumn search",
            "directive_set_id": directive_id,
            "profile_version_id": profile_id,
            "caps": {"max_pages": 20, "max_pages_per_source": 3},
            **overrides,
        },
    )


def _catalogue(adapter_key: str, **overrides) -> None:
    row = {
        "adapter_key": adapter_key,
        "display_name": adapter_key.title(),
        "source_type": "job_board",
        "coverage_countries": ["BE", "NL"],
        "coverage_industries": [],
        "query_capabilities": AdapterCapabilities().__dict__,
        "access_method": "http",
        "rate_limit_rps": 1.0,
        "cost_per_call_eur": 0.0,
        "tos_status": "permitted",
        "enabled": 1,
        "requires_ack": 0,
        "updated_at": utcnow(),
    }
    row.update(overrides)
    upsert_row("source_catalogue", row, ["adapter_key"])


# ---------------------------------------------------------------------------
# FR-184 de-duplication
# ---------------------------------------------------------------------------


def test_company_name_normaliser_strips_legal_forms():
    assert dedup.normalise_company_name("Acme Belgium N.V.") == "acme belgium"
    assert dedup.normalise_company_name("ACME BELGIUM bvba") == "acme belgium"
    assert dedup.normalise_company_name("Müller & Söhne GmbH") == "muller and sohne"
    # A company named only after its legal form must not normalise to nothing.
    assert dedup.normalise_company_name("The Company Ltd") != ""


def test_token_set_ratio_is_order_independent():
    assert dedup.token_set_ratio("Data Engineer (m/v) - Gent", "Gent | Data engineer") == 1.0
    assert dedup.token_set_ratio("Data Engineer", "Marketing Manager") < 0.5


def test_vacancy_fuzzy_match_and_key():
    a = {
        "title": "Senior Data Engineer (m/v)",
        "company_name_raw": "Acme NV",
        "location": "Gent, Belgium",
        "posted_at": "2026-09-01",
    }
    b = {
        "title": "Data Engineer, Senior",
        "company_name_raw": "ACME",
        "location": "9000 Gent",
        "posted_at": "2026-09-03",
    }
    other = dict(a, title="Marketing Manager")
    assert dedup.is_duplicate_vacancy(a, b)
    assert not dedup.is_duplicate_vacancy(a, other)
    assert dedup.assign_dedup_key(dict(a)) == dedup.vacancy_dedup_key(
        a["title"], a["company_name_raw"], a["location"], a["posted_at"]
    )


def test_seniority_difference_is_not_a_duplicate():
    base = {"company_name_raw": "Acme NV", "location": "Gent", "posted_at": "2026-09-01"}
    junior = dict(base, title="Data Engineer")
    senior = dict(base, title="Senior Data Engineer")
    assert not dedup.is_duplicate_vacancy(junior, senior)
    # The same posting seen twice, with the city appended by one board, is one record.
    assert dedup.is_duplicate_vacancy(junior, dict(base, title="Data Engineer - Gent"))


def test_legal_id_normalisation_pads_belgian_numbers():
    assert dedup.normalise_legal_id("123.456.749", "kbo") == "0123456749"
    assert dedup.normalise_legal_id("BE 0123 456 749", "vat") == "BE0123456749"


# ---------------------------------------------------------------------------
# FR-164 source selection
# ---------------------------------------------------------------------------


def test_source_selection_excludes_out_of_coverage_sources():
    catalogue = [
        {"adapter_key": "vdab", "source_type": "job_board", "coverage_countries": ["BE"],
         "enabled": 1},
        {"adapter_key": "jobsdb_asia", "source_type": "job_board",
         "coverage_countries": ["SG", "HK", "JP"], "enabled": 1},
        {"adapter_key": "indeed", "source_type": "job_board", "coverage_countries": [],
         "enabled": 1},
        {"adapter_key": "linkedin", "source_type": "linkedin", "coverage_countries": [],
         "enabled": 1},
    ]
    selection = planning.select_sources(
        catalogue, countries=["BE", "NL", "LU"], linkedin_allowed=False
    )
    keys = {e["adapter_key"] for e in selection.selected}
    assert keys == {"vdab", "indeed"}
    rejected = {r["adapter_key"]: r["reason"] for r in selection.rejected}
    assert "HK/JP/SG" in rejected["jobsdb_asia"]
    assert "consent" in rejected["linkedin"]


def test_spontaneous_only_mode_drops_vacancy_sources():
    catalogue = [
        {"adapter_key": "vdab", "source_type": "job_board", "coverage_countries": [], "enabled": 1},
        {"adapter_key": "kbo", "source_type": "registry", "coverage_countries": ["BE"],
         "enabled": 1},
    ]
    selection = planning.select_sources(catalogue, countries=["BE"], spontaneous_only=True)
    assert [e["adapter_key"] for e in selection.selected] == ["kbo"]


# ---------------------------------------------------------------------------
# FR-161..166 planning
# ---------------------------------------------------------------------------


def test_generate_plan_persists_estimates_and_honours_exclusions(db):
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("vdab")
    _catalogue("jobsdb_asia", coverage_countries=["SG", "JP"])
    _catalogue("kbo", source_type="registry", coverage_countries=["BE"])

    summary = planning.generate_plan(campaign_id, seeker, use_llm=False)

    keys = {i["adapter_key"] for i in summary["items"]}
    assert keys == {"vdab", "kbo"}  # FR-164
    assert summary["countries"] == ["BE", "NL"]
    assert summary["totals"]["estimated_pages"] > 0
    assert summary["totals"]["estimated_seconds"] > 0
    assert summary["degraded_reason"] is None or "LLM" in summary["degraded_reason"]
    for item in summary["items"]:
        assert item["native_query"], "every plan item carries a native query (FR-162)"
        assert item["rationale"]

    # FR-163: an exclusion survives a re-plan.
    excluded = next(i for i in summary["items"] if i["adapter_key"] == "vdab")
    campaign_repo.update_plan_item(excluded["id"], {"excluded_by_user": 1})
    again = planning.generate_plan(campaign_id, seeker, use_llm=False)
    still_excluded = next(i for i in again["items"] if i["adapter_key"] == "vdab")
    assert still_excluded["excluded_by_user"] == 1


class _RecordingLLM:
    """A stand-in that captures everything the planner would send (CR-410)."""

    def __init__(self, campaign_id: str):
        self.campaign_id = campaign_id
        self.sent = ""

    class budget:  # noqa: N801 - mirrors LLMClient.budget
        @staticmethod
        def should_degrade() -> bool:
            return False

    def complete_json(self, task, *, system, user, untrusted=None, **kw):
        self.sent += json.dumps({"s": system, "u": user, "d": untrusted}, ensure_ascii=False)
        return {
            "plans": [
                {"adapter_key": "vdab", "native_query": {"keywords": ["data"]},
                 "rationale": "r", "estimated_pages": 1}
            ]
        }


def test_nothing_marked_undisclosable_reaches_the_prompt(db):
    """CR-410/FR-127/FR-385: redaction happens before the planner calls out."""
    seeker = _seeker()
    campaign_id = _campaign(
        seeker,
        directives={
            "job_content": {"roles": ["Data Engineer"], "health_notes": ["a condition"]},
            "compensation": {"minimum_gross": 85000},
            "discretion_mode": 1,
            "discretion_excluded_companies": ["Currentemployer NV", "Sister BV"],
        },
    )
    insert_row(
        "disclosure_flag",
        {"job_seeker_id": seeker, "field_path": "minimum_gross",
         "do_not_disclose": 1, "created_at": utcnow()},
    )
    _catalogue("vdab")

    llm = _RecordingLLM(campaign_id)
    planning.generate_plan(campaign_id, seeker, use_llm=True, llm=llm)

    assert llm.sent, "the planner did call the model"
    assert "Currentemployer" not in llm.sent, "FR-385: the current employer is never named"
    assert "Sister BV" not in llm.sent
    assert "a condition" not in llm.sent, "FR-127: special-category data never reaches a prompt"
    assert "85000" not in llm.sent, "CR-410: a do-not-disclose field never reaches a prompt"
    assert "discretion_mode" in llm.sent, "the planner still knows discretion mode is on"


def test_replan_keeps_the_plan_item_a_collected_record_points_at(db):
    """FR-166: the provenance link must survive a re-plan of the same source."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("vdab")
    _catalogue("kbo", source_type="registry")

    first = planning.generate_plan(campaign_id, seeker, use_llm=False)
    item = next(i for i in first["items"] if i["adapter_key"] == "vdab")
    writer = knowledge_base.KnowledgeBaseWriter(
        adapter_key="vdab", plan_item_id=item["id"], campaign_id=campaign_id
    )
    writer.write(
        {
            "entity_type": "vacancy",
            "data": {"title": "Data Engineer", "company_name_raw": "Acme NV", "location": "Gent"},
            "confidence": 0.8,
        }
    )
    # The posting names its employer, and the writer turns that name into a
    # company row so the company-scoped tables have something to hang off.
    assert campaign_repo.collected_counts(campaign_id) == {"vacancy": 1, "company": 1}

    again = planning.generate_plan(campaign_id, seeker, use_llm=False)
    reused = next(i for i in again["items"] if i["adapter_key"] == "vdab")
    assert reused["id"] == item["id"], "the row a record points at is reused, not replaced"
    assert campaign_repo.collected_counts(campaign_id) == {"vacancy": 1, "company": 1}
    live = {i["id"] for i in campaign_repo.list_plan_items(campaign_id)}
    dangling = [
        r
        for r in query_all("SELECT * FROM provenance WHERE source_plan_item_id IS NOT NULL")
        if r["source_plan_item_id"] not in live
    ]
    assert not dangling


def test_replan_retires_a_dropped_source_that_produced_records(db):
    """FR-166: a source the new plan drops is retired, not deleted, when cited."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("vdab")
    first = planning.generate_plan(campaign_id, seeker, use_llm=False)
    item = first["items"][0]
    knowledge_base.KnowledgeBaseWriter(
        adapter_key="vdab", plan_item_id=item["id"], campaign_id=campaign_id
    ).write({"entity_type": "company", "data": {"name": "Acme NV"}, "confidence": 0.8})

    # The administrator disables the source; the next plan cannot use it.
    _catalogue("vdab", enabled=0)
    again = planning.generate_plan(campaign_id, seeker, use_llm=False)
    assert not again["items"] or all(i["status"] == "skipped" for i in again["items"])
    retired = next(i for i in campaign_repo.list_plan_items(campaign_id) if i["id"] == item["id"])
    assert retired["status"] == "skipped"
    assert campaign_repo.collected_counts(campaign_id) == {"company": 1}


def test_linkedin_network_plan_is_capped(db):
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    insert_row(
        "consent_record",
        {
            "job_seeker_id": seeker,
            "kind": "linkedin_automation",
            "granted": 1,
            "granted_at": utcnow(),
        },
    )
    _catalogue("linkedin", source_type="linkedin", coverage_countries=[], access_method="browser")

    summary = planning.generate_plan(campaign_id, seeker, use_llm=False)
    network = [
        i for i in summary["items"]
        if (i["native_query"] or {}).get("strategy") == "network"
    ]
    assert network, "FR-165 network strategy is planned when consent exists"
    query = network[0]["native_query"]
    facets = {f["facet"] for f in query["facets"]}
    assert facets == {"first_degree", "similar_roles", "alumni_employers", "alumni_schools"}
    assert query["max_profiles"] <= 200 and query["max_companies"] > 0


# ---------------------------------------------------------------------------
# FR-341..345 knowledge base
# ---------------------------------------------------------------------------


def test_writer_deduplicates_and_refuses_seeker_links(db):
    writer = knowledge_base.KnowledgeBaseWriter(adapter_key="kbo", plan_item_id=None)
    first = writer.write(
        NormalisedRecord(
            entity_type="company",
            data={
                "name": "Acme Belgium NV",
                "vat_number": "BE 0123.456.749",
                "domain": "https://www.acme.be/about",
                "country": "BE",
                "job_seeker_id": "leak",  # FR-344: must never reach a shared row
            },
        )
    )
    assert first and first.created
    row = kb_repo.get_company(first.entity_id)
    assert row["normalised_name"] == "acme belgium"
    assert row["domain"] == "acme.be"
    assert "job_seeker_id" not in row

    # Same company through a different source, keyed on VAT.
    second = writer.write(
        NormalisedRecord(
            entity_type="company",
            data={"name": "ACME BELGIUM", "vat_number": "BE0123456749", "size_fte": 240},
        )
    )
    assert second and not second.created and second.entity_id == first.entity_id
    assert second.matched_on == "vat"
    assert kb_repo.get_company(first.entity_id)["size_fte"] == 240
    assert len(query_all("SELECT id FROM company")) == 1

    # FR-344 is enforced at the repository layer too.
    with pytest.raises(ValueError):
        kb_repo.sanitise("company", {"name": "X", "job_seeker_id": "leak"})


def test_vacancy_dedup_and_full_text_search(db):
    writer = knowledge_base.KnowledgeBaseWriter(adapter_key="vdab")
    base = {
        "title": "Senior Data Engineer (m/v)",
        "company_name_raw": "Acme NV",
        "location": "Gent, Belgium",
        "country": "BE",
        "posted_at": "2026-09-01",
        "description": "Build streaming pipelines in Python and dbt.",
    }
    first = writer.write(NormalisedRecord(entity_type="vacancy", data=dict(base)))
    repeat = writer.write(
        NormalisedRecord(
            entity_type="vacancy",
            data=dict(base, title="Data Engineer, Senior", location="9000 Gent",
                      posted_at="2026-09-03"),
        )
    )
    assert first and repeat and repeat.entity_id == first.entity_id
    assert len(query_all("SELECT id FROM vacancy")) == 1

    # FR-345: the FTS index is kept in step on write.
    found = knowledge_base.browse_vacancies("streaming pipelines")
    assert found["total"] == 1
    assert found["items"][0]["id"] == first.entity_id
    assert knowledge_base.browse_vacancies("underwater basket weaving")["total"] == 0

    writer.write(
        NormalisedRecord(
            entity_type="company",
            data={"name": "Acme Belgium NV", "country": "BE",
                  "business_summary": "Streaming data platform for retailers."},
        )
    )
    companies = knowledge_base.browse_companies("retailers")
    assert companies["total"] == 1


def test_staleness_policy_defaults_and_override(db):
    policy = knowledge_base.get_staleness_policy()
    assert policy["vacancy"] == 7 and policy["company"] == 90
    assert policy["financial_year"] == 365  # FR-343 filings: one year

    updated = knowledge_base.set_staleness_policy({"vacancy": 3})
    assert updated["vacancy"] == 3
    assert knowledge_base.get_staleness_policy()["vacancy"] == 3
    assert knowledge_base.is_stale("vacancy", _iso(5))
    assert not knowledge_base.is_stale("vacancy", _iso(1))
    with pytest.raises(ValueError):
        knowledge_base.set_staleness_policy({"unicorns": 1})


def test_reuse_report_skips_fresh_sources_and_reports_the_saving(db):
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("vdab", query_capabilities={"max_results_per_query": 10, "pagination": True})
    _catalogue("kbo", source_type="registry", coverage_countries=["BE"],
               query_capabilities={"max_results_per_query": 10, "pagination": True})
    planning.generate_plan(campaign_id, seeker, use_llm=False, assess_knowledge_base=False)

    # 40 fresh vacancies already collected from vdab; nothing from kbo.
    for n in range(40):
        vacancy_id = kb_repo.insert_vacancy(
            {
                "title": f"Data Engineer {n}",
                "company_name_raw": "Acme NV",
                "country": "BE",
                "source_adapter": "vdab",
                "collected_at": _iso(1),
            }
        )
        kb_repo.record_provenance("vacancy", vacancy_id, adapter_key="vdab")

    report = knowledge_base.assess_reuse(campaign_id, countries=["BE", "NL"])
    by_adapter = {d.adapter_key: d for d in report.decisions}
    assert by_adapter["vdab"].action == "skip"
    assert by_adapter["vdab"].reused_records > 0
    assert by_adapter["vdab"].pages_after == 0
    assert by_adapter["kbo"].action == "collect"
    assert report.seconds_saved > 0
    assert report.per_entity["vacancy"]["reused"] > 0
    assert "reused" in report.headline()

    stored = campaign_repo.get_campaign(campaign_id, seeker)["reuse_report"]
    assert stored["estimated_seconds_saved"] == report.seconds_saved

    # Re-running the assessment must not compound its own page cuts (NFR-603).
    again = knowledge_base.assess_reuse(campaign_id, countries=["BE", "NL"])
    assert again.seconds_saved == report.seconds_saved
    skipped = [i for i in campaign_repo.list_plan_items(campaign_id) if i["status"] == "skipped"]
    assert [i["adapter_key"] for i in skipped] == ["vdab"]


# ---------------------------------------------------------------------------
# FR-181..186 collection
# ---------------------------------------------------------------------------


@register_adapter
class _StubBoard(SourceAdapter):
    """Offline stand-in for a job board: three vacancies per page."""

    key = "stub_board"
    display_name = "Stub Board"
    source_type = SourceType.JOB_BOARD
    coverage_countries = ["BE", "NL"]
    pages_fetched: list[int] = []

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        return [PlanItem(adapter_key=self.key, native_query={"keywords": ["data"]})]

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        page = int(item.native_query.get("page", 1))
        type(self).pages_fetched.append(page)
        return [RawRecord(url=f"https://stub.test/jobs?p={page}", content="<html/>",
                          meta={"page": page})]

    TITLES = [
        ["Data Engineer", "Analytics Translator", "Machine Learning Scientist"],
        ["Platform Architect", "Business Intelligence Developer", "Data Governance Officer"],
        ["Streaming Specialist", "Warehouse Modeller", "Integration Consultant"],
    ]

    def parse(self, raw: RawRecord) -> list[dict]:
        page = raw.meta["page"]
        return [
            {"title": title, "company_name_raw": "Acme NV",
             "location": "Gent", "country": "BE", "posted_at": "2026-09-01"}
            for title in self.TITLES[(page - 1) % len(self.TITLES)]
        ]

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord:
        return NormalisedRecord(entity_type="vacancy", data=dict(parsed, source_url=raw.url))


def test_collection_runs_the_plan_and_records_provenance(db):
    _StubBoard.pages_fetched = []
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("stub_board", query_capabilities=AdapterCapabilities().__dict__)
    planning.generate_plan(campaign_id, seeker, use_llm=False, assess_knowledge_base=False)

    item = campaign_repo.list_plan_items(campaign_id)[0]
    campaign_repo.update_plan_item(item["id"], {"estimated_pages": 2})

    job_id = runner.create(
        collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker, total=2
    )
    ctx = JobContext(job_id=job_id, campaign_id=campaign_id, job_seeker_id=seeker)
    asyncio.run(collection.collection_worker(ctx))

    assert _StubBoard.pages_fetched == [1, 2]
    vacancies = query_all("SELECT * FROM vacancy")
    assert len(vacancies) == 6  # FR-184: no duplicates across the two pages

    done = campaign_repo.get_plan_item(item["id"])
    assert done["status"] == "done"
    assert done["records_collected"] == 6
    assert done["error_count"] == 0

    # FR-166: every record is linked to the plan item that produced it - the six
    # postings plus the employer they all name, which the writer creates so that
    # company_id is not NULL on a single one of them.
    provenance = query_all(
        "SELECT * FROM provenance WHERE source_plan_item_id = ?", (item["id"],)
    )
    assert len(provenance) == 7
    assert {p["entity_type"] for p in provenance} == {"vacancy", "company"}
    assert {p["adapter_key"] for p in provenance} == {"stub_board"}
    companies = query_all("SELECT * FROM company")
    assert [c["name"] for c in companies] == ["Acme NV"]
    assert all(v["company_id"] == companies[0]["id"] for v in vacancies)

    # NFR-401: the checkpoint records the last completed page.
    job = query_one("SELECT * FROM job_run WHERE id = ?", (job_id,))
    assert job["progress_done"] == 2
    assert campaign_repo.get_campaign(campaign_id, seeker)["status"] == "completed"

    status = collection.status(campaign_id, seeker)
    assert status["collected"]["vacancy"] == 6
    assert status["sources"][0]["records_collected"] == 6


def test_collection_resumes_from_its_checkpoint(db):
    _StubBoard.pages_fetched = []
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("stub_board")
    planning.generate_plan(campaign_id, seeker, use_llm=False, assess_knowledge_base=False)
    item = campaign_repo.list_plan_items(campaign_id)[0]
    campaign_repo.update_plan_item(item["id"], {"estimated_pages": 3})

    job_id = runner.create(collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker)
    ctx = JobContext(
        job_id=job_id,
        campaign_id=campaign_id,
        job_seeker_id=seeker,
        checkpoint={"completed": {item["id"]: 2}},
    )
    asyncio.run(collection.collection_worker(ctx))
    assert _StubBoard.pages_fetched == [3], "pages already checkpointed are not refetched"


def test_caps_stop_the_run(db):
    _StubBoard.pages_fetched = []
    seeker = _seeker()
    campaign_id = _campaign(seeker, caps={"max_pages": 1, "max_pages_per_source": 5})
    _catalogue("stub_board")
    planning.generate_plan(campaign_id, seeker, use_llm=False, assess_knowledge_base=False)
    item = campaign_repo.list_plan_items(campaign_id)[0]
    campaign_repo.update_plan_item(item["id"], {"estimated_pages": 5})

    job_id = runner.create(collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker)
    ctx = JobContext(job_id=job_id, campaign_id=campaign_id, job_seeker_id=seeker)
    asyncio.run(collection.collection_worker(ctx))

    assert _StubBoard.pages_fetched == [1], "FR-186: the page cap bounds the run"
    stopped = campaign_repo.get_plan_item(item["id"])
    assert stopped["status"] == "planned", "a capped item stays runnable"
    assert "max_pages" in stopped["last_error"]


@register_adapter
class _BrokenBoard(SourceAdapter):
    """A board whose layout changed: every fetch raises."""

    key = "broken_board"
    display_name = "Broken Board"
    source_type = SourceType.JOB_BOARD
    coverage_countries = ["BE", "NL"]

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        return []

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        raise RuntimeError("layout changed")

    def parse(self, raw: RawRecord) -> list[dict]:
        return []

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord:
        return NormalisedRecord(entity_type="vacancy", data=parsed)


def test_a_cap_does_not_erase_an_earlier_failure(db):
    """FR-185/NFR-403: a broken source stays reported as failed, not 'not started'."""
    _StubBoard.pages_fetched = []
    seeker = _seeker()
    campaign_id = _campaign(seeker, caps={"max_pages": 1, "max_pages_per_source": 5})
    _catalogue("broken_board")
    _catalogue("stub_board")
    planning.generate_plan(campaign_id, seeker, use_llm=False, assess_knowledge_base=False)
    for item in campaign_repo.list_plan_items(campaign_id):
        campaign_repo.update_plan_item(item["id"], {"estimated_pages": 5})

    job_id = runner.create(collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker)
    ctx = JobContext(job_id=job_id, campaign_id=campaign_id, job_seeker_id=seeker)
    asyncio.run(collection.collection_worker(ctx))

    by_key = {i["adapter_key"]: i for i in campaign_repo.list_plan_items(campaign_id)}
    broken = by_key["broken_board"]
    assert broken["status"] == "failed"
    assert broken["error_count"] == 1
    assert "layout changed" in broken["last_error"]
    assert by_key["stub_board"]["status"] == "planned"
    assert "max_pages" in by_key["stub_board"]["last_error"]


def test_unknown_adapter_skips_only_its_own_plan_item(db):
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("not_implemented_yet")
    planning.generate_plan(campaign_id, seeker, use_llm=False, assess_knowledge_base=False)

    job_id = runner.create(collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker)
    ctx = JobContext(job_id=job_id, campaign_id=campaign_id, job_seeker_id=seeker)
    asyncio.run(collection.collection_worker(ctx))

    item = campaign_repo.list_plan_items(campaign_id)[0]
    assert item["status"] == "skipped"
    assert "no adapter" in item["last_error"]
    assert campaign_repo.get_campaign(campaign_id, seeker)["status"] == "completed"


def test_stage_registry_exposes_rerunnable_stages(db):
    stages = {s["stage"] for s in collection.available_stages()}
    assert {"planning", "reuse", "collection"} <= stages
    with pytest.raises(KeyError):
        asyncio.run(collection.rerun_stage("nonsense", "x", "y"))


def test_rerun_stage_returns_a_json_object_and_stays_seeker_scoped(db):
    """NFR-603 re-runs report a plain object; FR-101 keeps them to their owner."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("stub_board")
    planning.generate_plan(campaign_id, seeker, use_llm=False, assess_knowledge_base=False)

    report = asyncio.run(collection.rerun_stage("reuse", campaign_id, seeker))
    assert isinstance(report, dict) and "headline" in report

    intruder = insert_row(
        "job_seeker",
        {"email": "intruder@example.com", "display_name": "Intruder",
         "created_at": utcnow(), "updated_at": utcnow()},
    )
    with pytest.raises(LookupError):
        asyncio.run(collection.rerun_stage("reuse", campaign_id, intruder))


# ---------------------------------------------------------------------------
# API surface (FR-163, FR-345)
# ---------------------------------------------------------------------------


def _client(seeker_id: str):
    from datetime import timedelta as _td

    from dreamjob.api.routers import campaigns as router_module
    from dreamjob.security.crypto import hash_token
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    insert_row(
        "session",
        {
            "job_seeker_id": seeker_id,
            "token_hash": hash_token("test-token"),
            "expires_at": (datetime.now(UTC) + _td(hours=1)).isoformat(timespec="seconds"),
            "created_at": utcnow(),
        },
    )
    app = FastAPI()
    app.include_router(router_module.router, prefix="/api/campaigns")
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer test-token"})
    return client


def test_campaign_api_plans_edits_and_searches(db):
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("stub_board")
    client = _client(seeker)

    plan = client.post(f"/api/campaigns/{campaign_id}/plan", json={"use_llm": False})
    assert plan.status_code == 200
    item = plan.json()["items"][0]

    # FR-163: the user edits the query and excludes the source.
    edited = client.patch(
        f"/api/campaigns/{campaign_id}/plan/{item['id']}",
        json={"native_query": {"keywords": ["head of data"]}, "estimated_pages": 2,
              "excluded_by_user": True},
    )
    assert edited.status_code == 200
    assert edited.json()["excluded_by_user"] == 1

    summary = client.get(f"/api/campaigns/{campaign_id}/plan").json()
    assert summary["totals"]["sources_excluded"] == 1

    # Launching with everything excluded is a conflict, not a silent no-op.
    assert client.post(f"/api/campaigns/{campaign_id}/launch").status_code == 409

    # FR-345: browse the knowledge base without a campaign.
    kb_repo.insert_company(
        {"name": "Acme Belgium NV", "normalised_name": "acme belgium", "country": "BE",
         "business_summary": "Streaming data platform for retailers.", "collected_at": utcnow()}
    )
    found = client.get("/api/campaigns/knowledge-base/companies", params={"q": "streaming"})
    assert found.status_code == 200 and found.json()["total"] == 1

    # FR-343: the staleness policy is readable and configurable.
    assert client.get("/api/campaigns/staleness-policy").json()["policy_days"]["vacancy"] == 7
    assert client.put(
        "/api/campaigns/staleness-policy", json={"policy": {"vacancy": 14}}
    ).json()["policy_days"]["vacancy"] == 14

    # NFR-603: the re-runnable stages are listed.
    stages = {s["stage"] for s in client.get("/api/campaigns/stages").json()}
    assert {"planning", "reuse", "collection"} <= stages


def test_campaigns_are_isolated_between_job_seekers(db):
    mine = _seeker()
    campaign_id = _campaign(mine)
    other = insert_row(
        "job_seeker",
        {"email": "other@example.com", "display_name": "Other",
         "created_at": utcnow(), "updated_at": utcnow()},
    )
    client = _client(other)
    assert client.get(f"/api/campaigns/{campaign_id}").status_code == 404
    assert campaign_repo.get_campaign(campaign_id, other) is None
