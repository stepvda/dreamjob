"""The national salary-survey source behind FR-264's third rank.

The fixture is a verbatim capture of the Eurostat dissemination API - the same
request :func:`dataset_url` builds - so the JSON-stat decoding is exercised
against the shape the service really answers with, sparse cells and all.

What matters here is the whole path, not the parser alone: an adapter record has
to survive the knowledge-base writer, land in ``compensation_observation``, and
then be found again by the estimator when it prices an opportunity.  That last
hop is the one that was missing, so it is asserted end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dreamjob.adapters.compensation.eurostat import (
    DATASET,
    OCCUPATIONS,
    SOURCE_NAME,
    EurostatEarningsAdapter,
    aggregate,
    dataset_url,
    decode_jsonstat,
)
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_all, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.pipeline import compensation as comp_mod
from dreamjob.pipeline import knowledge_base

FIXTURE = Path(__file__).parent.parent / "fixtures" / "compensation" / "eurostat_earn_ses22_25.json"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def payload() -> dict:
    return json.loads(FIXTURE.read_text())


def _records(payload: dict) -> list:
    adapter = EurostatEarningsAdapter()
    raw = type("Raw", (), {"url": dataset_url(["BE"]), "raw_document_id": None})()
    return [r for r in (adapter.normalise(p, raw) for p in aggregate(decode_jsonstat(payload))) if r]


# ---------------------------------------------------------------------------
# The request, and the JSON-stat cube it answers with
# ---------------------------------------------------------------------------


def test_the_request_names_only_documented_dimensions():
    url = dataset_url(["BE", "NL"], ["OC1"])
    assert url.startswith(
        "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/" + DATASET
    )
    # Gross earnings in euro for both sexes: the four filters that make rows
    # from different countries comparable at all.
    for expected in ("sex=T", "indic_se=ERN", "unit=EUR", "format=JSON"):
        assert expected in url
    assert url.count("geo=") == 2
    assert "isco08=OC1" in url


def test_a_sparse_cube_decodes_to_the_published_figures(payload):
    """Only 51 of the cube's cells carry a value; the rest were never published."""
    rows = decode_jsonstat(payload)
    assert len(rows) == len(payload["value"])
    assert {r["geo"] for r in rows} == {"BE", "NL", "DE"}
    assert {r["isco08"] for r in rows} <= set(OCCUPATIONS)

    # Spot-check one cell against the source: Belgian managers, 50-249 employees.
    belgian_managers = [
        r for r in rows if r["geo"] == "BE" and r["isco08"] == "OC1" and r["sizeclas"] == "50-249"
    ]
    assert [r["value"] for r in belgian_managers] == [8120.0]


def test_the_range_is_the_published_spread_across_employer_size(payload):
    rows = {(r["country"], r["occupation"]): r for r in aggregate(decode_jsonstat(payload))}

    german_managers = rows[("DE", "OC1")]
    assert german_managers["occupation_label"] == "Managers"
    assert german_managers["amount_min"] == 5845.0      # smallest firms
    assert german_managers["amount_max"] == 8789.0      # largest firms
    assert german_managers["amount_min"] < german_managers["amount_median"]
    assert german_managers["size_classes"] == 6

    # Belgium published a single size class for managers, so the "range" is a
    # point.  That is the survey's answer, and it is not padded into a spread.
    assert rows[("BE", "OC1")]["amount_min"] == rows[("BE", "OC1")]["amount_max"] == 8120.0


def test_a_refusal_is_not_mistaken_for_data():
    adapter = EurostatEarningsAdapter()
    raw = type("Raw", (), {"url": "https://example.invalid", "content": json.dumps(
        {"error": [{"status": 400, "label": "INVALID_QUERY_DIMENSION"}]}
    )})()
    assert adapter.parse(raw) == []


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def test_planning_asks_only_for_countries_the_survey_covers():
    adapter = EurostatEarningsAdapter()
    items = adapter.plan({"location": {"countries": ["BE", "NL", "XX"]}}, {}, {})
    assert len(items) == 1
    assert items[0].native_query["countries"] == ["BE", "NL"]

    # A campaign aimed somewhere the survey does not reach plans no work at all
    # rather than a request that can only come back empty.
    assert adapter.plan({"location": {"countries": ["XX"]}}, {}, {}) == []


# ---------------------------------------------------------------------------
# The whole path: adapter -> shared knowledge base -> estimate
# ---------------------------------------------------------------------------


def test_records_reach_the_shared_knowledge_base(db, payload):
    records = _records(payload)
    assert len(records) == 12          # 3 countries x 4 occupation groups
    assert all(r.entity_type == "compensation_observation" for r in records)

    knowledge_base.KnowledgeBaseWriter(adapter_key=EurostatEarningsAdapter.key).write_many(records)

    stored = query_all("SELECT * FROM compensation_observation ORDER BY country, normalised_title")
    assert len(stored) == 12
    row = next(r for r in stored if r["country"] == "BE" and r["normalised_title"] == "OC1")
    assert row["source"] == "salary_survey"
    assert row["source_name"] == SOURCE_NAME
    assert row["period"] == "monthly"
    assert row["currency"] == "EUR"
    assert row["amount_median"] == 8120.0
    # FR-344: a shared row carries no job seeker, and the survey knows no
    # function family - claiming one would make a coarse figure look precise.
    assert row["function_family"] is None
    assert "job_seeker_id" not in row.keys() or row["job_seeker_id"] is None


def test_re_running_the_adapter_refreshes_rather_than_duplicates(db, payload):
    writer = knowledge_base.KnowledgeBaseWriter(adapter_key=EurostatEarningsAdapter.key)
    writer.write_many(_records(payload))
    writer.write_many(_records(payload))

    assert len(query_all("SELECT id FROM compensation_observation")) == 12


def test_the_survey_prices_a_role_that_nothing_else_could(db, payload):
    """FR-264: with no comparable posting anywhere, the survey is the anchor."""
    knowledge_base.KnowledgeBaseWriter(
        adapter_key=EurostatEarningsAdapter.key
    ).write_many(_records(payload))

    company_id = insert_row(
        "company",
        {"name": "Anchor NV", "normalised_name": "anchor nv", "country": "BE",
         "collected_at": utcnow()},
    )
    opportunity = {
        "id": "o-survey",
        "company_id": company_id,
        "company_name": "Anchor NV",
        "title": "Data Engineering Manager",
        "function_family": "data & analytics",
        "seniority": "manager",
        "country": "BE",
        "comp_is_stated": 0,
    }

    result = comp_mod.estimate(opportunity)

    used = [s for s in result.sources if s.used]
    assert [s.kind for s in used] == ["salary_survey"]
    # 8120 EUR a month is annualised at 12; the estimator never assumes a
    # Belgian 13th month, so the year is exactly twelve of them.  Belgium
    # published one size class for managers, so the low and the high are that
    # single figure rather than a spread invented around it.
    assert result.comp_min == result.comp_max == pytest.approx(8120 * 12)
    assert result.confidence <= comp_mod.SOURCE_CONFIDENCE_CEILING["salary_survey"]
    assert result.method == "blended:salary_survey"


def test_a_survey_row_never_outranks_a_real_posted_range(db, payload):
    """FR-264's ranking: what employers actually offer beats a national average."""
    knowledge_base.KnowledgeBaseWriter(
        adapter_key=EurostatEarningsAdapter.key
    ).write_many(_records(payload))

    for index in range(4):
        company = insert_row(
            "company",
            {"name": f"Peer {index}", "normalised_name": f"peer {index}", "country": "BE",
             "collected_at": utcnow()},
        )
        insert_row(
            "vacancy",
            {
                "company_id": company,
                "title": "Data Engineering Manager",
                "function_family": "data & analytics",
                "seniority": "manager",
                "country": "BE",
                "salary_min": 70_000,
                "salary_max": 90_000,
                "salary_currency": "EUR",
                "collected_at": utcnow(),
            },
        )

    result = comp_mod.estimate(
        {
            "id": "o-both",
            "title": "Data Engineering Manager",
            "function_family": "data & analytics",
            "seniority": "manager",
            "country": "BE",
            "comp_is_stated": 0,
        }
    )

    used = [s for s in result.sources if s.used]
    assert {s.kind for s in used} == {"posted_vacancies", "salary_survey"}
    # The survey speaks once, however many rows it happens to hold; the posted
    # corpus may legitimately contribute more than one comparison width.
    assert len([s for s in used if s.kind == "salary_survey"]) == 1
    posted = next(s for s in used if s.kind == "posted_vacancies")
    survey = next(s for s in used if s.kind == "salary_survey")
    assert posted.weight > survey.weight
    # The blend therefore sits nearer the posted ranges than the survey anchor.
    assert abs(result.comp_max - posted.high) < abs(result.comp_max - survey.high)


def test_an_opportunity_with_no_company_is_not_priced_off_the_whole_table(db, payload):
    """An unfiltered observation lookup would return every row ever collected.

    The survey holds figures for three countries and four occupations; a Belgian
    manager must be priced off the Belgian managers row alone, not off the mean
    of Dutch clerks and German technicians as well.
    """
    knowledge_base.KnowledgeBaseWriter(
        adapter_key=EurostatEarningsAdapter.key
    ).write_many(_records(payload))

    result = comp_mod.estimate(
        {
            "id": "o-nocompany",
            "company_id": None,
            "title": "Data Engineering Manager",
            "function_family": "data & analytics",
            "seniority": "manager",
            "country": "BE",
            "comp_is_stated": 0,
        }
    )

    used = [s for s in result.sources if s.used]
    assert len(used) == 1
    assert result.comp_min == result.comp_max == pytest.approx(8120 * 12)


@pytest.mark.parametrize(
    ("seniority", "occupation"),
    [("medior", "OC2"), ("senior", "OC2"), ("manager", "OC1"), ("director", "OC1")],
)
def test_a_seniority_reads_the_occupation_group_that_speaks_for_it(seniority, occupation):
    assert comp_mod.survey_occupations(seniority)[0] == occupation


@pytest.mark.parametrize("seniority", ["intern", "junior"])
def test_a_first_job_is_left_to_the_band_prior(db, payload, seniority):
    """CR-405: the professional mean is a whole-career figure, not a starting salary."""
    knowledge_base.KnowledgeBaseWriter(
        adapter_key=EurostatEarningsAdapter.key
    ).write_many(_records(payload))

    assert comp_mod.survey_occupations(seniority) == []

    result = comp_mod.estimate(
        {
            "id": f"o-{seniority}",
            "title": "Data Engineer",
            "function_family": "data & analytics",
            "seniority": seniority,
            "country": "BE",
            "comp_is_stated": 0,
        }
    )
    assert [s.kind for s in result.sources if s.used] == ["builtin_prior"]
    # Well under the survey's 60k figure for "Professionals".
    assert result.comp_max < 50_000
