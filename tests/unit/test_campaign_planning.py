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
from dreamjob.adapters.compensation.eurostat import EurostatEarningsAdapter
from dreamjob.config import get_settings
from dreamjob.db.connection import execute, insert_row, query_all, query_one, upsert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import knowledge as kb_repo
from dreamjob.egress import client as egress_client
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


def test_a_campaign_with_no_geography_does_not_plan_foreign_registries(db):
    """FR-164/NFR-403: an all-Belgian corpus must not plan US SEC EDGAR.

    ``select_sources`` treats an empty wanted-set as "every coverage matches",
    so a campaign whose directives name no country planned every registry on
    earth; 379 EDGAR lookups resolved no CIK.
    """
    seeker = _seeker()
    campaign_id = _campaign(seeker, directives={"location": {"countries": []}})
    insert_row(
        "company",
        {
            "name": "Acme NV",
            "normalised_name": "acme",
            "country": "BE",
            "source": "test",
            "access_method": "registry",
            "collected_at": utcnow(),
        },
    )
    _catalogue("kbo", source_type="registry", coverage_countries=["BE"])
    _catalogue("sec_edgar", source_type="registry", coverage_countries=["US"])

    summary = planning.generate_plan(campaign_id, seeker, use_llm=False)

    keys = {i["adapter_key"] for i in summary["items"]}
    assert "kbo" in keys
    assert "sec_edgar" not in keys


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


def test_replan_retires_a_duplicate_row_a_record_points_at_rather_than_deleting_it(db):
    """FR-166: the link holds for the twin too, not only for the row that matched.

    ``persist_plan`` keys the stored rows by plan identity to match a re-plan to
    them, and that dict keeps one row per key.  A campaign planned before the
    collapse net existed can hold twenty rows under one key - one running
    campaign holds 39 such rows with 16,234 provenance rows pointing at them -
    and nineteen of the twenty are invisible in that dict.  Deciding what to
    retire from the survivors alone left them in neither ``ids`` nor ``retired``,
    so the delete swept them: ``provenance.source_plan_item_id`` carries no
    foreign key, so nothing raised and the records simply stopped naming where
    they came from.
    """
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("vdab")
    query = {"queries": ["data engineer"], "locations": ["Gent"]}

    # Two rows asking one question, the shape a pre-net plan left behind.
    twins = [
        campaign_repo.insert_plan_item(
            campaign_id,
            {"adapter_key": "vdab", "native_query": dict(query), "rationale": f"worded {n} ways",
             "estimated_pages": 1},
        )
        for n in range(2)
    ]
    for plan_item_id in twins:
        knowledge_base.KnowledgeBaseWriter(
            adapter_key="vdab", plan_item_id=plan_item_id, campaign_id=campaign_id
        ).write(
            {
                "entity_type": "vacancy",
                "data": {"title": f"Data Engineer {plan_item_id[:4]}", "location": "Gent"},
                "confidence": 0.8,
            }
        )
    pointed_at = {
        r["source_plan_item_id"]
        for r in query_all("SELECT * FROM provenance WHERE source_plan_item_id IS NOT NULL")
    }
    assert pointed_at == set(twins), "both twins carry provenance before the re-plan"

    planning.persist_plan(
        campaign_id,
        [planning.PlannedSource(adapter_key="vdab", native_query=dict(query), estimated_pages=1)],
    )

    live = {i["id"] for i in campaign_repo.list_plan_items(campaign_id)}
    dangling = [
        r["id"]
        for r in query_all("SELECT * FROM provenance WHERE source_plan_item_id IS NOT NULL")
        if r["source_plan_item_id"] not in live
    ]
    assert not dangling, "no record may be left naming a plan item that is gone"
    assert set(twins) <= live, "the twin is retired, not deleted"
    retired = [i for i in campaign_repo.list_plan_items(campaign_id) if i["status"] == "skipped"]
    assert len(retired) == 1 and retired[0]["last_error"] == "no longer part of the plan"


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


def test_reuse_assesses_per_target_not_per_adapter(db):
    """FR-342 / N4: one fresh ATS board must not skip every other board.

    The reuse assessment used to compare a single plan item's expectation
    against the *whole corpus* of fresh records for its adapter.  ATS and EURES
    sources are planned one item per target, so the first board that returned a
    page of vacancies made every other board of that vendor look already
    collected - and the ATS harvest never fetched a second company again.
    """
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue(
        "ats.greenhouse",
        source_type="ats",
        query_capabilities={"max_results_per_query": 100, "pagination": False},
    )

    boards = ["acme", "globex", "initech"]
    for slug in boards:
        campaign_repo.insert_plan_item(
            campaign_id,
            {
                "adapter_key": "ats.greenhouse",
                "native_query": {"slug": slug},
                "rationale": f"{slug} board",
                "caps": {"planned_pages": 1, "records_per_page": 100},
                "estimated_pages": 1,
                "estimated_seconds": 8,
                "estimated_cost_eur": 0.0,
            },
        )

    # One board was read, and it returned a full page of vacancies.
    egress_client.record_fetch(
        "ats.greenhouse",
        "greenhouse/acme",
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
        http_status=200,
        record_count=150,
    )
    for n in range(150):
        vacancy_id = kb_repo.insert_vacancy(
            {
                "title": f"Engineer {n}",
                "company_name_raw": "Acme NV",
                "country": "BE",
                "source_adapter": "ats.greenhouse",
                "collected_at": _iso(1),
            }
        )
        kb_repo.record_provenance("vacancy", vacancy_id, adapter_key="ats.greenhouse")

    knowledge_base.assess_reuse(campaign_id, countries=["BE", "NL"])
    by_slug = {
        (i["native_query"] or {}).get("slug"): i
        for i in campaign_repo.list_plan_items(campaign_id)
        if i["adapter_key"] == "ats.greenhouse"
    }
    # The board that was actually read is the only one skipped.
    assert by_slug["acme"]["status"] == "skipped"
    assert by_slug["globex"]["status"] == "planned"
    assert by_slug["initech"]["status"] == "planned"
    assert by_slug["globex"]["estimated_pages"] == 1
    assert by_slug["initech"]["estimated_pages"] == 1


def _ats_items(campaign_id: str, slugs: tuple[str, ...], **values) -> None:
    for slug in slugs:
        campaign_repo.insert_plan_item(
            campaign_id,
            {
                "adapter_key": "ats.greenhouse",
                "native_query": {"slug": slug},
                "rationale": f"{slug} board",
                "caps": {"planned_pages": 1, "records_per_page": 100},
                "estimated_pages": 1,
                "estimated_seconds": 8,
                "estimated_cost_eur": 0.0,
                **values,
            },
        )


def test_reuse_age_unset_keeps_the_staleness_window(db):
    """FR-342: without the option, a target inside the policy window is reused."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue(
        "ats.greenhouse",
        source_type="ats",
        query_capabilities={"max_results_per_query": 100, "pagination": False},
    )
    _ats_items(campaign_id, ("acme", "globex"))
    # Five days old: outside a 3-day reuse cap, but inside the 7-day policy.
    egress_client.record_fetch(
        "ats.greenhouse",
        "greenhouse/acme",
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
        http_status=200,
        record_count=10,
        fetched_at=_iso(5),
    )

    report = knowledge_base.assess_reuse(campaign_id, countries=["BE", "NL"])
    by_slug = {
        (i["native_query"] or {}).get("slug"): i
        for i in campaign_repo.list_plan_items(campaign_id)
        if i["adapter_key"] == "ats.greenhouse"
    }
    assert by_slug["acme"]["status"] == "skipped"
    assert report.refreshed == 0
    decisions = {d.plan_item_id: d for d in report.decisions}
    assert decisions[by_slug["acme"]["id"]].action == "skip"


def test_max_reuse_age_reopens_older_targets_and_reuses_fresh_ones(db):
    """FR-342: the cap re-opens an old read and still reuses a fresh one."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue(
        "ats.greenhouse",
        source_type="ats",
        query_capabilities={"max_results_per_query": 100, "pagination": False},
    )
    _ats_items(
        campaign_id,
        ("acme",),
        status="skipped",
        records_collected=25,
        error_count=2,
        last_error="stale run",
        activity_at=utcnow(),
        activity_kind="done",
    )
    _ats_items(campaign_id, ("globex",))
    egress_client.record_fetch(
        "ats.greenhouse",
        "greenhouse/acme",
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
        http_status=200,
        record_count=10,
        fetched_at=_iso(5),
    )
    egress_client.record_fetch(
        "ats.greenhouse",
        "greenhouse/globex",
        "https://boards-api.greenhouse.io/v1/boards/globex/jobs",
        http_status=200,
        record_count=10,
        fetched_at=_iso(1),
    )

    report = knowledge_base.assess_reuse(
        campaign_id, countries=["BE", "NL"], max_reuse_age_days=3
    )
    by_slug = {
        (i["native_query"] or {}).get("slug"): i
        for i in campaign_repo.list_plan_items(campaign_id)
        if i["adapter_key"] == "ats.greenhouse"
    }
    # The board read five days ago is re-opened, counters and all...
    assert by_slug["acme"]["status"] == "planned"
    assert by_slug["acme"]["records_collected"] == 0
    assert by_slug["acme"]["error_count"] == 0
    assert by_slug["acme"]["last_error"] is None
    assert by_slug["acme"]["activity_at"] is None
    assert by_slug["acme"]["estimated_pages"] == 1
    # ...while the one read yesterday is reused exactly as before.
    assert by_slug["globex"]["status"] == "skipped"
    assert report.refreshed == 1
    assert report.per_entity["vacancy"]["refreshed"] == 1
    assert report.to_dict()["refreshed_items"] == 1
    decisions = {d.plan_item_id: d for d in report.decisions}
    assert decisions[by_slug["acme"]["id"]].action == "collect"
    assert "re-opened" in decisions[by_slug["acme"]["id"]].reason
    assert decisions[by_slug["globex"]["id"]].action == "skip"


def test_max_reuse_age_caps_source_level_reuse(db):
    """FR-342: the cap also moves the corpus-freshness cutoff a source reuses against."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("vdab", query_capabilities={"max_results_per_query": 10, "pagination": True})
    planning.generate_plan(campaign_id, seeker, use_llm=False, assess_knowledge_base=False)

    # 40 vacancies collected five days ago: inside the 7-day policy, outside 3.
    for n in range(40):
        vacancy_id = kb_repo.insert_vacancy(
            {
                "title": f"Data Engineer {n}",
                "company_name_raw": "Acme NV",
                "country": "BE",
                "source_adapter": "vdab",
                "collected_at": _iso(5),
            }
        )
        kb_repo.record_provenance("vacancy", vacancy_id, adapter_key="vdab")
    # Provenance is stamped when the record was written, so a past collection
    # is simulated by dating it too - the freshness queries read both.
    execute(
        "UPDATE provenance SET created_at = ? WHERE entity_type = 'vacancy' "
        "AND adapter_key = 'vdab'",
        (_iso(5),),
    )

    base = knowledge_base.assess_reuse(campaign_id, countries=["BE", "NL"])
    assert [d for d in base.decisions if d.adapter_key == "vdab"][0].action == "skip"
    assert base.refreshed == 0

    capped = knowledge_base.assess_reuse(
        campaign_id, countries=["BE", "NL"], max_reuse_age_days=3
    )
    vdab = [i for i in campaign_repo.list_plan_items(campaign_id) if i["adapter_key"] == "vdab"]
    assert vdab[0]["status"] == "planned", "the cap re-opened what the policy would reuse"
    assert vdab[0]["estimated_pages"] > 0
    assert capped.refreshed == 1


def test_plan_summary_reports_refreshed_and_reused_counts(db, monkeypatch):
    """FR-342: the plan summary carries how much was reused and refreshed."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("vdab", query_capabilities={"max_results_per_query": 10, "pagination": True})
    seen: dict = {}

    def fake_assess(campaign_id, *, countries=None, max_reuse_age_days=None, **kwargs):
        seen["max_reuse_age_days"] = max_reuse_age_days
        report = knowledge_base.ReuseReport()
        report.per_entity["vacancy"] = {"reused": 12, "scheduled": 3, "refreshed": 2}
        report.refreshed = 2
        campaign_repo.update_campaign(campaign_id, {"reuse_report": report.to_dict()})
        return report

    monkeypatch.setattr(knowledge_base, "assess_reuse", fake_assess)
    summary = planning.generate_plan(
        campaign_id, seeker, use_llm=False, max_reuse_age_days=3
    )

    assert seen["max_reuse_age_days"] == 3
    assert summary["reuse"] == {"max_reuse_age_days": 3, "reused": 12, "refreshed": 2}
    assert summary["totals"]["records_reused"] == 12
    assert summary["totals"]["sources_refreshed_by_age"] == 2


def test_reuse_headline_is_capped_by_what_the_knowledge_base_holds(db):
    """FR-342: the claimed saving can never exceed the corpus it comes from.

    Every per-target item added its whole expected page yield, so a campaign
    over thousands of boards reported hundreds of thousands of reused records
    against a corpus of tens of thousands.
    """
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue(
        "ats.greenhouse",
        source_type="ats",
        query_capabilities={"max_results_per_query": 100, "pagination": False},
    )
    for slug in ("acme", "globex", "initech"):
        campaign_repo.insert_plan_item(
            campaign_id,
            {
                "adapter_key": "ats.greenhouse",
                "native_query": {"slug": slug},
                "caps": {"planned_pages": 1, "records_per_page": 100},
                "estimated_pages": 1,
                "estimated_seconds": 8,
                "estimated_cost_eur": 0.0,
            },
        )
        egress_client.record_fetch(
            "ats.greenhouse",
            f"greenhouse/{slug}",
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
            http_status=200,
            record_count=100,
        )
    # Three boards look fresh (300 expected), but only 50 rows exist.
    for n in range(50):
        vacancy_id = kb_repo.insert_vacancy(
            {
                "title": f"Engineer {n}",
                "company_name_raw": "Acme NV",
                "country": "BE",
                "source_adapter": "ats.greenhouse",
                "collected_at": _iso(1),
            }
        )
        kb_repo.record_provenance("vacancy", vacancy_id, adapter_key="ats.greenhouse")

    report = knowledge_base.assess_reuse(campaign_id, countries=["BE", "NL"])
    assert report.per_entity["vacancy"]["reused"] == report.fresh_in_knowledge_base["vacancy"]
    assert report.per_entity["vacancy"]["reused"] <= 50


def test_reuse_per_target_key_matches_the_ledger_key_collection_writes(db):
    """The two sides of N4 must agree, or per-target reuse silently reverts."""
    assert planning.target_key("ats.greenhouse", {"slug": "acme"}) == "greenhouse/acme"
    # An explicit (vendor, slug) from a loaded adapter is the same key.
    assert (
        planning.target_key("ats.greenhouse", {"slug": "acme"}, ("greenhouse", "acme"))
        == "greenhouse/acme"
    )
    # Two EURES partitions are two targets, and a re-plan of one is the same one.
    a = {"nuts_codes": ["BE1"], "nace_section": "J", "publication_period": "LAST_MONTH"}
    b = {"nuts_codes": ["BE2"], "nace_section": "J", "publication_period": "LAST_MONTH"}
    assert planning.target_key("board.eures", a) != planning.target_key("board.eures", b)
    assert planning.target_key("board.eures", a) == planning.target_key(
        "board.eures", {**a, "page": 4}
    )


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


@register_adapter
class _GreedyBoard(SourceAdapter):
    """A discovery-stage board that spends a page every time it is asked."""

    key = "greedy_board"
    display_name = "Greedy Board"
    source_type = SourceType.JOB_BOARD
    coverage_countries = ["BE", "NL"]
    pages_fetched: list[int] = []

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        return [PlanItem(adapter_key=self.key, native_query={"keywords": ["data"]})]

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        page = int(item.native_query.get("page", 1))
        type(self).pages_fetched.append(page)
        return [RawRecord(url=f"https://greedy.test/jobs?p={page}", content="<html/>",
                          meta={"page": page})]

    def parse(self, raw: RawRecord) -> list[dict]:
        return [{"title": f"Role {raw.meta['page']}", "company_name_raw": "Greedy NV",
                 "location": "Gent", "country": "BE", "posted_at": "2026-09-01"}]

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord:
        return NormalisedRecord(entity_type="vacancy", data=dict(parsed, source_url=raw.url))


@register_adapter
class _StubAts(SourceAdapter):
    """A harvest-stage ATS board, which is what the reservation protects."""

    key = "ats.stubvendor"
    display_name = "Stub ATS"
    source_type = SourceType.ATS
    vendor = "stubvendor"
    coverage_countries = ["BE", "NL"]
    slugs_fetched: list[str] = []

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        return []

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        slug = str(item.native_query.get("slug") or "")
        type(self).slugs_fetched.append(slug)
        return [RawRecord(url=f"https://ats.test/{slug}", content="<html/>",
                          meta={"slug": slug})]

    def parse(self, raw: RawRecord) -> list[dict]:
        return [{"title": "Platform Engineer", "company_name_raw": f"{raw.meta['slug']} BV",
                 "location": "Gent", "country": "BE", "posted_at": "2026-09-01"}]

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord:
        return NormalisedRecord(entity_type="vacancy", data=dict(parsed, source_url=raw.url))


def test_a_crowded_discovery_stage_cannot_starve_the_ats_harvest(db):
    """FR-186: the harvest stage is reserved a share of the page budget.

    The budget used to be first-come-first-served in stage order, and the floor
    pass hands every *unit* a page, so a plan with more discovery items than
    pages spent the whole budget before the harvest stage was reached. That is
    the real shape: one run settled 4,429 planned ATS boards as "not started:
    max_pages" without issuing a single request to any of them. The reservation
    is what makes a company's own board reachable in the run that found it.
    """
    _GreedyBoard.pages_fetched = []
    _StubAts.slugs_fetched = []
    seeker = _seeker()
    campaign_id = _campaign(seeker, caps={"max_pages": 6, "max_pages_per_source": 20})
    _catalogue("greedy_board", query_capabilities=AdapterCapabilities().__dict__)
    _catalogue("ats.stubvendor", source_type="ats",
               query_capabilities={"max_results_per_query": 100, "pagination": False})
    planning.generate_plan(campaign_id, seeker, use_llm=False, assess_knowledge_base=False)

    # Ten discovery targets against a six-page budget: more items than pages is
    # what the floor pass turns into "stage one takes everything".
    for n in range(10):
        campaign_repo.insert_plan_item(
            campaign_id,
            {
                "adapter_key": "greedy_board",
                "native_query": {"keywords": [f"data-{n}"]},
                "rationale": f"discovery slice {n}",
                "caps": {"planned_pages": 1},
                "estimated_pages": 1,
                "estimated_seconds": 3,
                "estimated_cost_eur": 0.0,
            },
        )

    # Three ATS boards, planned per target as the board registry plans them.
    for slug in ("alpha", "beta", "gamma"):
        campaign_repo.insert_plan_item(
            campaign_id,
            {
                "adapter_key": "ats.stubvendor",
                "native_query": {"slug": slug},
                "rationale": f"{slug} board",
                "caps": {"stage": campaign_repo.STAGE_HARVEST, "planned_pages": 1},
                "estimated_pages": 1,
                "estimated_seconds": 8,
                "estimated_cost_eur": 0.0,
            },
        )

    job_id = runner.create(collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker)
    ctx = JobContext(job_id=job_id, campaign_id=campaign_id, job_seeker_id=seeker)
    asyncio.run(collection.collection_worker(ctx))

    assert _GreedyBoard.pages_fetched, "the discovery stage still runs"
    assert _StubAts.slugs_fetched, (
        "FR-186: the harvest stage must get its reservation, not the leftovers "
        f"(discovery took {len(_GreedyBoard.pages_fetched)} of 6 pages)"
    )
    # The reservation is a share, not the whole budget: discovery is not starved
    # either, and the run's own ceiling still holds.
    assert _GreedyBoard.pages_fetched
    assert len(_GreedyBoard.pages_fetched) + len(_StubAts.slugs_fetched) <= 6


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


# ---------------------------------------------------------------------------
# FR-162: one model answer per source, many plan items per source
# ---------------------------------------------------------------------------


@register_adapter
class _MultiQueryBoard(SourceAdapter):
    """A board that plans one item per keyword, the way EURES does."""

    key = "stub_multi_query_board"
    display_name = "Stub Multi-Query Board"
    source_type = SourceType.JOB_BOARD
    coverage_countries = ["BE", "NL"]

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        pages = max(1, int(caps.get("max_pages_per_source") or 1))
        return [
            PlanItem(
                adapter_key=self.key,
                native_query={"keyword": word, "results_per_page": 50, "language": "en"},
                rationale=f"Search for {word}",
                estimated_pages=pages,
            )
            for word in ("data engineer", "platform architect", "analytics translator")
        ]

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        return []

    def parse(self, raw: RawRecord) -> list[dict]:
        return []

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord:
        return NormalisedRecord(entity_type="vacancy", data=dict(parsed))


@register_adapter
class _PagePartitioningBoard(SourceAdapter):
    """A board that wrongly partitions itself on the pipeline's own page key."""

    key = "stub_page_partitioning_board"
    display_name = "Stub Page-Partitioning Board"
    source_type = SourceType.JOB_BOARD
    coverage_countries = ["BE", "NL"]

    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        pages = max(1, int(caps.get("max_pages_per_source") or 1))
        return [
            PlanItem(adapter_key=self.key,
                     native_query={"page": page, "keywords": ["data"]},
                     rationale=f"Page {page}", estimated_pages=1)
            for page in range(1, pages + 1)
        ]

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        return []

    def parse(self, raw: RawRecord) -> list[dict]:
        return []

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord:
        return NormalisedRecord(entity_type="vacancy", data=dict(parsed))


class _OneAnswerLLM:
    """A model that answers once for a source, in the shape it was shown."""

    def __init__(self, campaign_id: str, adapter_key: str, native_query: dict):
        self.campaign_id = campaign_id
        self._plan = {"adapter_key": adapter_key, "native_query": native_query,
                      "rationale": "Belgian and Dutch data roles", "estimated_pages": 4}

    class budget:  # noqa: N801 - mirrors LLMClient.budget
        @staticmethod
        def should_degrade() -> bool:
            return False

    def complete_json(self, task, *, system, user, untrusted=None, **kw):
        return {"plans": [self._plan]}


def test_a_model_answer_never_collapses_the_queries_a_source_planned(db):
    """FR-162: one answer per source must not overwrite what separates its items.

    The model is asked once per adapter, but an adapter plans as many items as
    it has units of work.  Letting that single answer write the discriminating
    key into every item turned many searches into one search repeated: the same
    records fetched again and again, the rest never fetched, and rows nothing
    could tell apart afterwards.
    """
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("stub_multi_query_board", query_capabilities=AdapterCapabilities().__dict__)

    llm = _OneAnswerLLM(campaign_id, "stub_multi_query_board",
                        {"keyword": "data engineer", "results_per_page": 100,
                         "language": "nl"})
    planning.generate_plan(campaign_id, seeker, use_llm=True, llm=llm,
                           assess_knowledge_base=False)

    items = campaign_repo.list_plan_items(campaign_id)
    assert sorted(i["native_query"]["keyword"] for i in items) == [
        "analytics translator", "data engineer", "platform architect",
    ]
    # What every item shares is still the model's to refine.
    assert all(i["native_query"]["language"] == "nl" for i in items)
    assert all(i["native_query"]["results_per_page"] == 100 for i in items)
    # ...and the adapter's own page count stands, rather than the model's
    # per-source estimate being charged once per item.
    assert all(i["estimated_pages"] == 3 for i in items)


def test_a_model_answer_never_collapses_the_companies_a_source_planned():
    """FR-162: the website crawler's per-company target is the adapter's alone.

    ``website.crawl`` plans one item per company site.  A single model answer
    naming one URL once overwrote all 149 of them, so one company was crawled
    149 times and 148 were never crawled at all.
    """
    items = [
        PlanItem(adapter_key="website.crawl",
                 native_query={"url": f"https://{name}.example.com", "company_id": name,
                               "max_pages": 12, "max_depth": 2})
        for name in ("acme", "borealis", "ceres")
    ]
    protected = planning.discriminating_keys(items)
    assert protected == frozenset({"url", "company_id"})

    generated = {"url": "https://dovane.be", "company_id": "dovane", "max_pages": 4}
    enriched = [planning.enrich_native_query(i.native_query, generated, protected)
                for i in items]
    assert [q["url"] for q in enriched] == [
        "https://acme.example.com", "https://borealis.example.com",
        "https://ceres.example.com",
    ]
    assert [q["company_id"] for q in enriched] == ["acme", "borealis", "ceres"]
    # What every item shares is still the model's to refine.
    assert all(q["max_pages"] == 4 for q in enriched)


def test_a_model_may_not_write_the_page_the_pipeline_owns():
    """FR-181: ``page`` belongs to collection, whatever the model answers.

    ``collection._run_page`` re-issues a plan item once per page with its own
    counter in ``native_query["page"]`` (``vacancy_source.requested_page``), so
    a page a model wrote would be overwritten anyway - after having made two
    items look identical in the plan the job seeker reviews.
    """
    native = {"keyword": "data engineer", "page": 3}
    assert planning.enrich_native_query(native, {"keyword": "data lead", "page": 1}) == {
        "keyword": "data lead", "page": 3,
    }


def test_an_adapter_that_partitions_on_the_pipelines_page_is_caught_at_plan_time(db, caplog):
    """FR-162, FR-166: two items with one question are one unit of work.

    ``board.actiris`` and ``board.arbeitnow`` planned one item per page, but
    collection overwrites ``page`` with its own counter, so all twenty items
    fetched page 1: the same adverts twenty times over, and pages 2-20 never
    read.  The identity of a plan item ignores ``page`` for exactly that reason,
    so items like these collapse to one - loudly, because a plan that quietly
    shrinks from twenty rows to one is a plan nobody can review.
    """
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("stub_page_partitioning_board",
               query_capabilities=AdapterCapabilities().__dict__)

    with caplog.at_level("WARNING"):
        planning.generate_plan(campaign_id, seeker, use_llm=False,
                               assess_knowledge_base=False)

    items = campaign_repo.list_plan_items(campaign_id)
    assert len(items) == 1, "one question is one plan item"
    assert "Dropped 2 duplicate plan item(s) for stub_page_partitioning_board" in caplog.text


def test_persisting_a_plan_refuses_to_write_the_same_question_twice(db, caplog):
    """FR-162, FR-166: two items with one query would fetch the same records twice."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("vdab")
    query = {"queries": ["data engineer"], "locations": ["Gent"], "page": 1}
    planned = [
        planning.PlannedSource(adapter_key="vdab", native_query=dict(query),
                               rationale=f"worded {n} ways", estimated_pages=1)
        for n in range(3)
    ]

    with caplog.at_level("WARNING"):
        ids = planning.persist_plan(campaign_id, planned)

    assert len(ids) == 1, "three identical questions are one unit of work"
    assert len(campaign_repo.list_plan_items(campaign_id)) == 1
    assert "Dropped 2 duplicate plan item(s) for vdab" in caplog.text


def test_a_source_that_partitions_on_geography_keeps_every_partition(db, caplog):
    """FR-162, FR-166, N4: the collapse net may only drop work, never coverage.

    ``compensation.eurostat_ses`` can ask the survey for twelve countries at a
    time, so it plans one item per twelve-country chunk and ``countries`` is the
    only key those items differ in.  While the identity of a plan item ignored
    ``countries``, a nineteen-country campaign had its second chunk dropped as a
    duplicate: seven countries silently lost their salary anchor, and the two
    chunks shared one ``target_key``, so per-target reuse reported a chunk
    fetched that no run had ever fetched.  A key that names *what* an item
    reads can never be volatile, however ambient it looks.
    """
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("compensation.eurostat_ses", source_type="compensation")
    countries = ["BE", "NL", "LU", "DE", "FR", "IE", "AT", "FI", "ES", "IT",
                 "PT", "GR", "PL", "CZ", "SK", "DK", "SE", "NO", "CH"]

    items = EurostatEarningsAdapter(egress=None).plan(
        {"location": {"countries": countries}}, {}, {}
    )
    assert len(items) == 2, "nineteen covered countries are two twelve-country requests"

    with caplog.at_level("WARNING"):
        ids = planning.persist_plan(campaign_id, [
            planning.PlannedSource(
                adapter_key=i.adapter_key, native_query=dict(i.native_query),
                rationale=i.rationale, estimated_pages=i.estimated_pages,
            )
            for i in items
        ])

    assert len(ids) == 2
    rows = campaign_repo.list_plan_items(campaign_id)
    stored = {c for r in rows for c in (r["native_query"] or {}).get("countries", [])}
    assert stored == set(countries), "no country may be dropped as a duplicate"
    assert "Dropped" not in caplog.text

    # ...and the fetch ledger sees two targets, so reuse cannot call one fetched
    # on the strength of the other (N4).
    assert planning.target_key("compensation.eurostat_ses", items[0].native_query) != (
        planning.target_key("compensation.eurostat_ses", items[1].native_query)
    )


def test_two_items_differing_only_in_page_are_still_one_unit_of_work():
    """FR-181: the keys identity ignores are ceilings on a walk, never its scope.

    The counterpart to the test above: ``page`` and the budget keys around it
    must stay ignored, or ``board.actiris``'s twenty page-partitioned items - all
    of which collection would have re-issued as page 1 - come back.
    """
    base = {"language": "nl", "keywords": ["data engineer"], "offers_per_page": 50}
    key = planning._plan_key("board.actiris", base)
    for volatile in ("page", "pages", "max_records", "max_pages"):
        assert planning._plan_key("board.actiris", {**base, volatile: 7}) == key
    # Geography is not one of them.
    assert planning._plan_key("board.actiris", {**base, "countries": ["BE"]}) != key
