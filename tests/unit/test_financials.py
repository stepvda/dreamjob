"""Registries and five-year financial analysis (FR-241..246, DR-101, DR-103, NFR-404, RK-06).

Everything here runs against a throwaway SQLite file with no network and no
LLM: the extractors are fed filing fixtures, the registry adapters are only
asked whether they degrade cleanly without their keys, and the rationale check
exercises the deterministic fallback that must still name its figures.
"""

from __future__ import annotations

import base64
import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from dreamjob.config import get_settings

_ENV_KEYS = ("DREAMJOB_DATA_DIR", "DREAMJOB_DB_PATH", "DREAMJOB_MASTER_KEY", "DREAMJOB_ENV")


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_ENV"] = "development"
    get_settings.cache_clear()

    from dreamjob.db.migrator import migrate

    migrate()
    yield

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Fixtures: two filings, one full Belgian scheme and one abbreviated
# ---------------------------------------------------------------------------

FULL_SCHEME = """
Balans na winstverdeling
VLOTTENDE ACTIVA 29/58 4.250.000
Liquide middelen 54/58 1.100.000
TOTAAL VAN DE ACTIVA 20/58 9.800.000
EIGEN VERMOGEN 10/15 3.400.000
Schulden op ten hoogste een jaar 42/48 2.900.000
TOTAAL VAN DE SCHULDEN 17/49 6.400.000
Resultatenrekening
Omzet 70 12.500.000
Bedrijfswinst (verlies) 9901 890.000
Bezoldigingen, sociale lasten en pensioenen 62 4.100.000
Afschrijvingen 630 350.000
Winst (verlies) van het boekjaar 9904 610.000
Gemiddeld personeelsbestand in VTE 9087 62,5
Boekjaar 2024
"""

ABBREVIATED_SCHEME = """
Brutomarge 9900 2.100.000
Bezoldigingen, sociale lasten en pensioenen 62 1.400.000
Bedrijfswinst (verlies) 9901 210.000
EIGEN VERMOGEN 10/15 900.000
Gemiddeld personeelsbestand in VTE 9087 21,0
Boekjaar 2023
"""

COMPANYFACTS = {
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {
                            "start": "2023-01-01", "end": "2023-12-31", "val": 1_000_000.0,
                            "form": "10-K", "filed": "2024-02-01",
                        },
                        {
                            "start": "2022-01-01", "end": "2022-12-31", "val": 800_000.0,
                            "form": "10-K", "filed": "2023-02-01",
                        },
                        {
                            "start": "2023-10-01", "end": "2023-12-31", "val": 260_000.0,
                            "form": "10-K", "filed": "2024-02-01",
                        },
                    ]
                }
            },
            "OperatingIncomeLoss": {
                "units": {
                    "USD": [
                        {
                            "start": "2023-01-01", "end": "2023-12-31", "val": 120_000.0,
                            "form": "10-K", "filed": "2024-02-01",
                        }
                    ]
                }
            },
            "StockholdersEquity": {
                "units": {
                    "USD": [
                        {"end": "2023-12-31", "val": 500_000.0, "form": "10-K",
                         "filed": "2024-02-01"}
                    ]
                }
            },
            "Assets": {
                "units": {
                    "USD": [
                        {"end": "2023-12-31", "val": 1_500_000.0, "form": "10-K",
                         "filed": "2024-02-01"}
                    ]
                }
            },
            "Liabilities": {
                "units": {
                    "USD": [
                        {"end": "2023-12-31", "val": 1_000_000.0, "form": "10-K",
                         "filed": "2024-02-01"}
                    ]
                }
            },
        }
    }
}


# ---------------------------------------------------------------------------
# FR-242: extraction
# ---------------------------------------------------------------------------


def test_parse_amount_handles_every_filing_convention() -> None:
    from dreamjob.pipeline.filing_extract import parse_amount

    assert parse_amount("1.234.567,89") == pytest.approx(1_234_567.89)
    assert parse_amount("1,234,567.89") == pytest.approx(1_234_567.89)
    assert parse_amount("(1 234)") == pytest.approx(-1234.0)
    assert parse_amount("123-") == pytest.approx(-123.0)
    assert parse_amount("") is None
    assert parse_amount(None) is None


def test_full_belgian_scheme_yields_every_fr242_figure() -> None:
    from dreamjob.pipeline.filing_extract import extract_from_text

    record = extract_from_text(FULL_SCHEME)
    assert record.fiscal_year == 2024
    assert record.revenue == pytest.approx(12_500_000)
    assert record.ebit == pytest.approx(890_000)
    assert record.net_result == pytest.approx(610_000)
    assert record.equity == pytest.approx(3_400_000)
    assert record.cash == pytest.approx(1_100_000)
    assert record.total_assets == pytest.approx(9_800_000)
    assert record.current_assets == pytest.approx(4_250_000)
    assert record.current_liabilities == pytest.approx(2_900_000)
    assert record.personnel_costs == pytest.approx(4_100_000)
    assert record.headcount_fte == pytest.approx(62.5)
    # EBITDA is derivable from EBIT plus depreciation (FR-242).
    assert record.ebitda == pytest.approx(1_240_000)
    assert record.is_estimated is False


def test_abbreviated_scheme_is_marked_estimated_not_guessed() -> None:
    """FR-245: no turnover in the filing means no turnover in the record."""
    from dreamjob.pipeline.filing_extract import extract_from_text

    record = extract_from_text(ABBREVIATED_SCHEME)
    assert record.revenue is None
    assert record.gross_profit == pytest.approx(2_100_000)
    assert record.is_estimated is True
    codes = {flag["code"] for flag in record.reconciliation_flags}
    assert "turnover_not_disclosed" in codes
    assert "be_abbreviated_scheme" in codes


def test_companyfacts_keeps_annual_periods_only() -> None:
    from dreamjob.pipeline.filing_extract import extract_from_companyfacts

    records = extract_from_companyfacts(COMPANYFACTS)
    assert [r.fiscal_year for r in records] == [2022, 2023]
    latest = records[-1]
    # The Q4 entry has a 92-day span and must not overwrite the annual figure.
    assert latest.revenue == pytest.approx(1_000_000)
    assert latest.ebit == pytest.approx(120_000)
    assert latest.currency == "USD"
    assert latest.total_debt == pytest.approx(1_000_000)


def test_inline_xbrl_applies_scale_and_sign() -> None:
    from dreamjob.pipeline.filing_extract import extract_from_ixbrl

    markup = """
    <html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">
      <xbrli:context id="c1"><xbrli:period>
        <xbrli:endDate>2024-03-31</xbrli:endDate></xbrli:period></xbrli:context>
      <p><ix:nonFraction name="uk-gaap:Turnover" contextRef="c1" scale="3"
         unitRef="GBP">4,200</ix:nonFraction></p>
      <p><ix:nonFraction name="uk-gaap:OperatingProfitLoss" contextRef="c1" scale="3"
         sign="-" unitRef="GBP">150</ix:nonFraction></p>
    </html>
    """
    records = extract_from_ixbrl(markup)
    assert len(records) == 1
    assert records[0].fiscal_year == 2023          # a book year ending in March
    assert records[0].revenue == pytest.approx(4_200_000)
    assert records[0].ebit == pytest.approx(-150_000)


# ---------------------------------------------------------------------------
# NFR-404: reconciliation
# ---------------------------------------------------------------------------


def test_balance_sheet_mismatch_is_flagged_not_accepted() -> None:
    from dreamjob.pipeline.filing_extract import FilingFacts, finalise

    record = FilingFacts(fiscal_year=2024, total_assets=1_000_000, equity=300_000)
    record.total_debt = 500_000                     # 300k + 500k != 1 000k
    finalise(record)
    flags = {flag["code"]: flag for flag in record.reconciliation_flags}
    assert "balance_sheet_mismatch" in flags
    assert flags["balance_sheet_mismatch"]["severity"] == "error"


def test_costs_are_normalised_to_magnitudes_and_flagged() -> None:
    from dreamjob.pipeline.filing_extract import FilingFacts, finalise

    record = FilingFacts(fiscal_year=2024, personnel_costs=-4_000_000, headcount_fte=50)
    finalise(record)
    assert record.personnel_costs == pytest.approx(4_000_000)
    assert any(f["code"] == "sign_normalised" for f in record.reconciliation_flags)


def test_cost_per_fte_out_of_band_is_flagged() -> None:
    from dreamjob.pipeline.filing_extract import FilingFacts, finalise

    record = FilingFacts(fiscal_year=2024, personnel_costs=4_100, headcount_fte=62.5)
    finalise(record)
    assert any(f["code"] == "cost_per_fte_out_of_band" for f in record.reconciliation_flags)


# ---------------------------------------------------------------------------
# DR-103: EUR normalisation
# ---------------------------------------------------------------------------


def test_eur_values_apply_the_stored_conversion_rate() -> None:
    from dreamjob.pipeline.filing_extract import eur_values

    row = {"revenue": 1_000_000.0, "equity": 500_000.0, "fx_rate_to_eur": 0.9}
    converted = eur_values(row)
    assert converted["revenue"] == pytest.approx(900_000)
    assert converted["equity"] == pytest.approx(450_000)
    assert converted["cash"] is None


# ---------------------------------------------------------------------------
# FR-243 / FR-244: metrics, trajectory and scores
# ---------------------------------------------------------------------------


def _year(fy: int, revenue: float, headcount: float, **kw: float) -> dict:
    row = {
        "fiscal_year": fy,
        "currency": "EUR",
        "fx_rate_to_eur": 1.0,
        "revenue": revenue,
        "headcount_fte": headcount,
        "ebit": revenue * 0.07,
        "net_result": revenue * 0.05,
        "equity": revenue * 0.30,
        "cash": revenue * 0.10,
        "total_debt": revenue * 0.45,
        "total_assets": revenue * 0.75,
        "current_assets": revenue * 0.35,
        "current_liabilities": revenue * 0.25,
        "personnel_costs": headcount * 62_000,
        "capex": revenue * 0.03,
        "is_estimated": 0,
        "reconciliation_flags": None,
    }
    row.update(kw)
    return row


def test_growth_is_classified_with_a_confidence_that_reflects_the_evidence() -> None:
    from dreamjob.pipeline.financial import compute_metrics

    rows = [
        _year(2020, 8_000_000, 50),
        _year(2021, 9_200_000, 56),
        _year(2022, 10_600_000, 62),
        _year(2023, 12_000_000, 70),
        _year(2024, 13_800_000, 78),
    ]
    metrics = compute_metrics("c1", rows)
    assert metrics.years_covered == [2020, 2021, 2022, 2023, 2024]
    assert metrics.trajectory == "growing"
    assert metrics.trajectory_confidence >= 0.8
    assert metrics.revenue_cagr == pytest.approx(0.1454, abs=0.005)
    assert metrics.headcount_cagr == pytest.approx(0.1183, abs=0.005)
    assert metrics.personnel_cost_per_fte == pytest.approx(62_000)
    assert metrics.current_ratio == pytest.approx(1.4)
    assert metrics.solvency_ratio == pytest.approx(0.4)
    assert 0 <= metrics.ability_to_pay <= 100
    assert 0 <= metrics.investment_capacity <= 100


def test_a_shrinking_company_is_not_called_growing() -> None:
    from dreamjob.pipeline.financial import compute_metrics

    rows = [
        _year(2021, 12_000_000, 80),
        _year(2022, 10_500_000, 74),
        _year(2023, 9_100_000, 66),
        _year(2024, 8_000_000, 58),
    ]
    metrics = compute_metrics("c1", rows)
    assert metrics.trajectory == "declining"
    assert metrics.revenue_cagr < 0


def test_volatility_is_distinguished_from_growth() -> None:
    from dreamjob.pipeline.financial import compute_metrics

    rows = [
        _year(2021, 5_000_000, 30),
        _year(2022, 9_000_000, 34),
        _year(2023, 4_500_000, 29),
        _year(2024, 8_500_000, 33),
    ]
    assert compute_metrics("c1", rows).trajectory == "volatile"


def test_rationales_always_name_the_figures_they_used() -> None:
    """FR-244: a rationale that cites no numbers is a bug, so the fallback cites them."""
    from dreamjob.pipeline.financial import cites_figures, compute_metrics, write_rationales

    rows = [_year(2023, 10_000_000, 60), _year(2024, 11_500_000, 66)]
    metrics = compute_metrics("c1", rows)
    metrics = write_rationales(metrics, "Acme NV", llm=None)   # no LLM available
    figures = {"revenue": metrics.latest["revenue"]}
    assert cites_figures(metrics.ability_to_pay_rationale, figures)
    assert cites_figures(metrics.investment_capacity_rationale, figures)
    assert "Acme NV" in metrics.ability_to_pay_rationale
    assert not cites_figures("The company looks financially healthy.", figures)


def test_estimated_years_cap_the_scores() -> None:
    from dreamjob.pipeline.financial import ESTIMATED_SCORE_CEILING, compute_metrics

    rows = [
        _year(2023, 10_000_000, 60, is_estimated=1),
        _year(2024, 11_500_000, 66, is_estimated=1),
    ]
    metrics = compute_metrics("c1", rows)
    assert metrics.is_estimated is True
    assert metrics.ability_to_pay <= ESTIMATED_SCORE_CEILING
    assert metrics.investment_capacity <= ESTIMATED_SCORE_CEILING


# ---------------------------------------------------------------------------
# Database round trip (FR-241..246)
# ---------------------------------------------------------------------------


def _company(name: str = "Acme NV", **kw: object) -> str:
    from dreamjob.db.repositories import knowledge as kb

    data = {
        "name": name,
        "normalised_name": name.lower().split(" ")[0],
        "country": "BE",
        "jurisdiction": "BE",
        "collected_at": "2026-01-01T00:00:00+00:00",
    }
    data.update(kw)
    return kb.insert_company(data)


def test_analysis_is_stored_and_the_company_trajectory_updated() -> None:
    from dreamjob.db.repositories import financials as repo
    from dreamjob.pipeline.financial import analyse_company

    company_id = _company(legal_id="0123456789", legal_id_type="kbo_bce")
    for row in (
        _year(2021, 8_000_000, 50),
        _year(2022, 9_500_000, 58),
        _year(2023, 11_000_000, 66),
        _year(2024, 12_800_000, 75),
    ):
        repo.upsert_financial_year({**row, "company_id": company_id, "source": "test"})

    assert repo.years_present(company_id) == [2021, 2022, 2023, 2024]

    result = analyse_company(company_id, llm=None)
    stored = repo.get_analysis(company_id)
    assert stored is not None
    assert stored["trajectory"] == "growing"
    assert stored["ability_to_pay"] == result["entity"]["ability_to_pay"]
    assert any(ch.isdigit() for ch in stored["ability_to_pay_rationale"])
    assert any(ch.isdigit() for ch in stored["investment_capacity_rationale"])

    assert repo.company(company_id)["trajectory"] == "growing"


def test_upsert_never_erases_a_figure_a_thinner_filing_omits() -> None:
    from dreamjob.db.repositories import financials as repo

    company_id = _company("Beta BV")
    repo.upsert_financial_year(
        {"company_id": company_id, "fiscal_year": 2024, "revenue": 5_000_000, "equity": 1_000_000}
    )
    repo.upsert_financial_year(
        {"company_id": company_id, "fiscal_year": 2024, "revenue": None, "cash": 250_000}
    )
    row = repo.get_financial_year(company_id, 2024)
    assert row["revenue"] == pytest.approx(5_000_000)
    assert row["cash"] == pytest.approx(250_000)
    assert len(repo.years_present(company_id)) == 1


def test_secondary_signals_produce_an_estimated_profile() -> None:
    """FR-245: no filings does not mean no answer - it means a marked estimate."""
    from dreamjob.db.repositories import financials as repo
    from dreamjob.db.repositories import knowledge as kb
    from dreamjob.pipeline.financial import analyse_company

    company_id = _company("Gamma SA", size_fte=120, stage="scaleup")
    kb.insert_shared(
        "hiring_signal",
        {
            "company_id": company_id,
            "signal_type": "funding",
            "description": "Series B of EUR 25m led by an investor",
            "occurred_at": "2025-11-01",
            "strength": 0.9,
            "collected_at": "2026-01-01T00:00:00+00:00",
        },
    )
    kb.insert_shared(
        "hiring_signal",
        {
            "company_id": company_id,
            "signal_type": "headcount_growth",
            "description": "Headcount up 30% year on year",
            "occurred_at": "2025-12-01",
            "strength": 0.8,
            "collected_at": "2026-01-01T00:00:00+00:00",
        },
    )

    result = analyse_company(company_id, llm=None)
    entity = result["entity"]
    assert entity["is_estimated"] is True
    assert entity["basis"] == "secondary_signals"
    assert entity["evidence"]["funding_total_eur"] == pytest.approx(25_000_000)
    stored = repo.get_analysis(company_id)
    assert stored["is_estimated"] == 1
    assert "25,000,000" in stored["investment_capacity_rationale"]
    assert "120" in stored["ability_to_pay_rationale"]


def test_group_links_give_a_consolidated_picture() -> None:
    """FR-246: subsidiary filings roll up to the parent alongside the entity view."""
    from dreamjob.db.repositories import financials as repo
    from dreamjob.pipeline.financial import analyse_company, record_group_links

    parent = _company("Holding NV")
    child = _company("Werkmaatschappij BV")
    for company_id, scale in ((parent, 1.0), (child, 0.5)):
        for row in (_year(2023, 10_000_000 * scale, 60), _year(2024, 11_000_000 * scale, 66)):
            repo.upsert_financial_year({**row, "company_id": company_id})

    written = record_group_links(
        parent, [{"name": "Werkmaatschappij BV", "relation": "subsidiary"}], "test"
    )
    assert written == 1
    assert repo.subsidiaries(parent)[0]["subsidiary_company_id"] == child

    result = analyse_company(parent, llm=None)
    assert "consolidated" in result
    entity_revenue = result["entity"]["latest"]["revenue"]
    group_revenue = result["consolidated"]["latest"]["revenue"]
    assert group_revenue == pytest.approx(entity_revenue * 1.5)


# ---------------------------------------------------------------------------
# FR-241 / RK-06: the adapters degrade rather than fail
# ---------------------------------------------------------------------------


def test_registry_adapters_are_registered_with_their_coverage() -> None:
    from dreamjob.adapters import base
    from dreamjob.adapters.directories import opencorporates  # noqa: F401
    from dreamjob.adapters.registries import (  # noqa: F401
        companies_house,
        kbo,
        kvk,
        nbb,
        sec_edgar,
    )

    registry = base.all_adapters()
    for key in (
        "registry.kbo",
        "registry.nbb",
        "registry.companies_house",
        "registry.kvk",
        "registry.sec_edgar",
        "directory.opencorporates",
    ):
        assert key in registry
    assert base.get_adapter("registry.nbb").covers({"country": "BE"})
    assert not base.get_adapter("registry.nbb").covers({"country": "US"})


@pytest.mark.asyncio
async def test_keyed_registries_degrade_cleanly_when_the_key_is_absent(monkeypatch) -> None:
    """RK-06: a missing key marks the profile estimated; it never raises."""
    from dreamjob.adapters.base import get_adapter

    for name in ("NBB_CBSO_SUBSCRIPTION_KEY", "COMPANIES_HOUSE_API_KEY", "KVK_API_KEY",
                 "OPENCORPORATES_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    for key in ("registry.nbb", "registry.companies_house", "registry.kvk",
                "directory.opencorporates"):
        adapter = get_adapter(key)
        assert adapter.available() is False
        assert adapter.plan({}, {}, {}) == []
        result = await adapter.collect({"name": "Acme NV", "legal_id": "0123456789"})
        assert result.estimated is True
        assert result.facts == []
        assert result.notes


def test_enterprise_number_normalisation_is_the_dr101_anchor() -> None:
    from dreamjob.adapters.registries.common import enterprise_number, format_enterprise_number

    assert enterprise_number({"legal_id": "0123.456.749"}) == "0123456749"
    assert enterprise_number({"vat_number": "BE 0123 456 749"}) == "0123456749"
    assert enterprise_number({"legal_id": "123456749"}) == "0123456749"
    assert enterprise_number({"name": "Acme"}) is None
    assert format_enterprise_number("0123456749") == "0123.456.749"


def test_nbb_prefers_the_fullest_deposit_per_year() -> None:
    from dreamjob.adapters.registries.nbb import NBBAdapter

    deposits = [
        {"reference": "a", "fiscal_year": 2024, "model": "f-abbrev", "deposit_date": "2025-05"},
        {"reference": "b", "fiscal_year": 2024, "model": "f-full", "deposit_date": "2025-04"},
        {"reference": "c", "fiscal_year": 2023, "model": "f-full", "deposit_date": "2024-05"},
    ]
    chosen = NBBAdapter.select_years(deposits, years=5)
    assert [d["reference"] for d in chosen] == ["b", "c"]


# ---------------------------------------------------------------------------
# NBB structured deposits and the content-type dispatch (FR-241, FR-242)
# ---------------------------------------------------------------------------

NBB_DEPOSIT = {
    "PeriodEndDate": "2024-12-31",
    "Rubrics": [
        {"Code": "70", "Period": "N", "Value": 12500000},
        {"Code": "9901", "Period": "N", "Value": 890000},
        {"Code": "9904", "Period": "N", "Value": 610000},
        {"Code": "10/15", "Period": "N", "Value": 3400000},
        {"Code": "9087", "Period": "N", "Value": 62.5},
        {"Code": "62", "Period": "N", "Value": 4100000},
        {"Code": "70", "Period": "N-1", "Value": 11200000},
        {"Code": "9901", "Period": "N-1", "Value": 760000},
        {"Code": "10/15", "Period": "N-1", "Value": 3000000},
    ],
}


def test_nbb_deposit_yields_the_year_and_its_comparative() -> None:
    from dreamjob.pipeline.filing_extract import extract_from_jsonxbrl

    records = extract_from_jsonxbrl(NBB_DEPOSIT)
    assert [r.fiscal_year for r in records] == [2023, 2024]
    latest = records[-1]
    assert latest.revenue == pytest.approx(12_500_000)
    assert latest.ebit == pytest.approx(890_000)
    assert latest.headcount_fte == pytest.approx(62.5)
    assert latest.reporting_standard == "BE-GAAP"
    assert records[0].revenue == pytest.approx(11_200_000)


def test_extract_filing_dispatches_on_content_type() -> None:
    import json

    from dreamjob.pipeline.filing_extract import extract_filing

    json_records = extract_filing(json.dumps(COMPANYFACTS).encode(), "application/json")
    assert [r.fiscal_year for r in json_records] == [2022, 2023]

    csv_records = extract_filing(
        b"Item;2023;2024\nRevenue;1000;1200\nEquity;400;470\n", "text/csv"
    )
    assert {r.fiscal_year for r in csv_records} == {2023, 2024}

    text_records = extract_filing(FULL_SCHEME.encode(), "text/plain")
    assert text_records[0].revenue == pytest.approx(12_500_000)


# ---------------------------------------------------------------------------
# FR-241: the collection orchestrator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collection_stores_identity_filings_and_group_links(monkeypatch) -> None:
    from dreamjob.adapters import base
    from dreamjob.adapters.registries.common import (
        RegistryAdapter,
        RegistryResult,
        SubsidiaryLink,
        identity_record,
    )
    from dreamjob.db.repositories import financials as repo
    from dreamjob.pipeline import financial
    from dreamjob.pipeline.filing_extract import FilingFacts

    subsidiary_id = _company("Dochter BV")

    class StubRegistry(RegistryAdapter):
        key = "registry.stub"
        display_name = "Stub registry"
        jurisdictions = ["BE"]

        async def collect(self, company, *, years=5, egress=None, llm=None):
            result = RegistryResult(adapter_key=self.key)
            result.identity = identity_record(
                company,
                name="Moeder NV",
                legal_id="0400378485",
                legal_id_type="kbo_bce",
                country="BE",
                source="stub",
                sector_codes=["62.010"],
            )
            for fiscal_year, revenue in ((2023, 9_000_000.0), (2024, 10_400_000.0)):
                result.facts.append(
                    FilingFacts(
                        fiscal_year=fiscal_year,
                        period_end=f"{fiscal_year}-12-31",
                        currency="EUR",
                        reporting_standard="BE-GAAP",
                        revenue=revenue,
                        ebit=revenue * 0.08,
                        equity=revenue * 0.3,
                        total_assets=revenue * 0.8,
                        total_debt=revenue * 0.5,
                        headcount_fte=40.0 + fiscal_year - 2023,
                        personnel_costs=(40.0 + fiscal_year - 2023) * 61_000,
                    )
                )
            result.subsidiaries.append(SubsidiaryLink(name="Dochter BV"))
            return result

    monkeypatch.setitem(base._REGISTRY, "registry.stub", StubRegistry)
    monkeypatch.setattr(financial, "registries_for", lambda company: ["registry.stub"])

    company_id = _company("Moeder NV")
    report = await financial.collect_company_financials(
        {"id": company_id, "name": "Moeder NV", "country": "BE", "jurisdiction": "BE"}
    )

    assert report["years_written"] == 2
    assert report["identity_updated"] is True
    assert report["estimated"] is False
    assert repo.years_present(company_id) == [2023, 2024]

    enriched = repo.company(company_id)
    assert enriched["legal_id"] == "0400378485"
    assert enriched["legal_id_type"] == "kbo_bce"

    links = repo.subsidiaries(company_id)
    assert [link["subsidiary_company_id"] for link in links] == [subsidiary_id]

    stored = repo.get_financial_year(company_id, 2024)
    assert stored["currency"] == "EUR"
    assert stored["fx_rate_to_eur"] == pytest.approx(1.0)
    assert stored["reporting_standard"] == "BE-GAAP"


def test_marking_a_company_estimated_covers_every_stored_year() -> None:
    """FR-245: the 'estimated' label is applied to the whole profile, not one year."""
    from dreamjob.db.repositories import financials as repo

    company_id = _company("Delta NV")
    for fiscal_year in (2023, 2024):
        repo.upsert_financial_year(
            {"company_id": company_id, "fiscal_year": fiscal_year, "revenue": 1_000_000}
        )
    assert repo.mark_years_estimated(company_id) == 2
    assert all(row["is_estimated"] == 1 for row in repo.financial_years(company_id))


def test_no_consolidated_view_is_invented_when_only_the_parent_filed() -> None:
    """FR-246: a group picture identical to the entity picture is not a group picture."""
    from dreamjob.db.repositories import financials as repo
    from dreamjob.pipeline.financial import analyse_company, record_group_links

    parent = _company("Solo Holding NV")
    _company("Stille Dochter BV")
    for row in (_year(2023, 6_000_000, 40), _year(2024, 6_400_000, 42)):
        repo.upsert_financial_year({**row, "company_id": parent})
    record_group_links(parent, [{"name": "Stille Dochter BV"}], "test")

    result = analyse_company(parent, llm=None)
    assert "consolidated" not in result


# ---------------------------------------------------------------------------
# Regressions found in verification
# ---------------------------------------------------------------------------


def test_an_alias_is_never_summed_into_the_group() -> None:
    """FR-246: SEC formerNames are identity, not ownership, and must not consolidate."""
    from dreamjob.adapters.registries.common import SubsidiaryLink
    from dreamjob.db.repositories import financials as repo
    from dreamjob.pipeline.financial import analyse_company, record_group_links

    company_id = _company("Acme Inc", normalised_name="acme", country="US", jurisdiction="US")
    namesake = _company("Acme Holdings", normalised_name="acme", country="US", jurisdiction="US")
    for target in (company_id, namesake):
        for row in (_year(2023, 5_000_000, 40), _year(2024, 5_500_000, 44)):
            repo.upsert_financial_year({**row, "company_id": target})

    record_group_links(
        company_id,
        [SubsidiaryLink(name="Acme Inc", relation="former_name")],
        "registry.sec_edgar",
    )
    # The alias is kept for identity, but it is not a group member.
    assert repo.group_aliases(company_id)
    assert repo.subsidiaries(company_id) == []

    result = analyse_company(company_id, llm=None)
    assert "consolidated" not in result
    assert result["entity"]["latest"]["revenue"] == pytest.approx(5_500_000)


def test_a_self_referential_link_does_not_double_the_group() -> None:
    """FR-246: a KvK establishment that resolves to its own parent is not a second entity."""
    from dreamjob.db.repositories import financials as repo
    from dreamjob.pipeline.financial import analyse_company

    company_id = _company("Vestiging BV")
    for row in (_year(2023, 3_000_000, 20), _year(2024, 3_300_000, 22)):
        repo.upsert_financial_year({**row, "company_id": company_id})
    repo.link_subsidiary(
        company_id,
        subsidiary_company_id=company_id,
        subsidiary_key="branch-1",
        subsidiary_name="Vestiging BV",
        relation="branch",
        source="registry.kvk",
    )

    result = analyse_company(company_id, llm=None)
    assert "consolidated" not in result
    assert result["entity"]["latest"]["revenue"] == pytest.approx(3_300_000)


def test_an_indicative_fx_rate_is_flagged_not_presented_as_exact() -> None:
    """NFR-404 / DR-103: a rate that is not an ECB quotation says so on the row."""
    import asyncio

    from dreamjob.adapters.registries.common import attach_fx
    from dreamjob.pipeline import filing_extract as fx

    record = fx.FilingFacts(
        fiscal_year=2024, currency="USD", period_end="2024-12-31", revenue=1_000_000.0
    )
    # No egress client: the ECB feed is unreachable, so the fallback table is used.
    paired = asyncio.run(attach_fx([record], egress=None))[0]
    assert paired["fx_rate_to_eur"] == pytest.approx(fx.FALLBACK_RATES_TO_EUR["USD"])

    row = fx.to_financial_year_row(
        "c1",
        paired["facts"],
        fx_rate_to_eur=paired["fx_rate_to_eur"],
        fx_date=paired["fx_date"],
        source="test",
    )
    codes = {flag["code"] for flag in row["reconciliation_flags"]}
    assert "fx_rate_approximate" in codes
    assert row["is_estimated"] == 0


def test_an_unquoted_currency_is_an_error_not_a_one_to_one_conversion() -> None:
    """RK-06: 1 JPY is not 1 EUR, and a conversion that cannot be made says so."""
    import asyncio

    from dreamjob.adapters.registries.common import attach_fx
    from dreamjob.pipeline import filing_extract as fx

    record = fx.FilingFacts(
        fiscal_year=2024, currency="JPY", period_end="2024-12-31", revenue=500_000_000.0
    )
    paired = asyncio.run(attach_fx([record], egress=None))[0]
    row = fx.to_financial_year_row(
        "c1",
        paired["facts"],
        fx_rate_to_eur=paired["fx_rate_to_eur"],
        fx_date=paired["fx_date"],
        source="test",
    )
    flags = {flag["code"]: flag for flag in row["reconciliation_flags"]}
    assert flags["fx_rate_missing"]["severity"] == "error"
    assert row["is_estimated"] == 1


def test_sec_years_point_back_at_the_stored_companyfacts_document() -> None:
    """FR-241 / DR-102: every extracted year keeps the filing it was read from."""
    import asyncio

    from dreamjob.adapters.registries.sec_edgar import SECEdgarAdapter

    payload = {
        "name": "Nathans Famous Inc",
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "start": "2023-04-01", "end": "2024-03-31", "val": 138_000_000,
                                "form": "10-K", "filed": "2024-06-01",
                            }
                        ]
                    }
                }
            }
        },
    }

    class _Response:
        ok = True
        status_code = 200
        raw_document_id = "doc-companyfacts-1"

        def __init__(self, body: str) -> None:
            self.text = body

    class _Egress:
        async def fetch(self, url: str, **kw: object) -> _Response:
            import json as _json

            if "companyfacts" in url:
                return _Response(_json.dumps(payload))
            if "submissions" in url:
                return _Response(_json.dumps({"name": "Nathans Famous Inc", "sic": "5812"}))
            return _Response("{}")

    adapter = SECEdgarAdapter()
    company = {"name": "Nathans Famous Inc", "legal_id": "69733", "legal_id_type": "sec_cik"}
    result = asyncio.run(adapter.collect(company, years=5, egress=_Egress()))

    assert result.facts, "companyfacts should have produced a year"
    assert all(r.filing_document_id == "doc-companyfacts-1" for r in result.facts)


def test_the_financials_stage_is_reachable_from_the_rerun_surface() -> None:
    """NFR-603: the stage collection.py reserves for this module actually resolves."""
    from dreamjob.pipeline import collection

    assert "financials" in {stage["stage"] for stage in collection.available_stages()}


def test_a_link_without_a_named_relation_is_still_a_subsidiary() -> None:
    """FR-246: a registry that only says 'below this company' means 'subsidiary'."""
    from dreamjob.db.repositories import financials as repo
    from dreamjob.pipeline.financial import analyse_company, record_group_links

    parent = _company("Moeder NV")
    child = _company("Dochter BV")
    for company_id, scale in ((parent, 1.0), (child, 0.5)):
        for row in (_year(2023, 8_000_000 * scale, 50), _year(2024, 8_800_000 * scale, 55)):
            repo.upsert_financial_year({**row, "company_id": company_id})

    assert record_group_links(parent, [{"name": "Dochter BV"}], "test") == 1
    links = repo.subsidiaries(parent)
    assert [link["relation"] for link in links] == ["subsidiary"]

    result = analyse_company(parent, llm=None)
    assert result["consolidated"]["latest"]["revenue"] == pytest.approx(
        result["entity"]["latest"]["revenue"] * 1.5
    )
