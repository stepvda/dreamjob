"""Posted pay is read as written and scaled to the annual columns (FR-261).

Two corruption bugs lived here: an English-grouped range (``€50,000``) was read
as 50, and a stated period (hour/month) was discarded, so an hourly rate was
stored as an annual figure.
"""

from __future__ import annotations

from dreamjob.adapters.vacancy_source import (
    _as_float,
    annualise_salary,
    jobposting_to_fields,
    parse_salary_text,
)


def test_english_grouping_is_not_read_as_a_decimal() -> None:
    assert parse_salary_text("€50,000 - €70,000") == (50_000.0, 70_000.0, "EUR")


def test_european_grouping_still_works() -> None:
    assert parse_salary_text("€50.000 – €70.000") == (50_000.0, 70_000.0, "EUR")


def test_a_single_decimal_is_read_as_a_decimal() -> None:
    assert parse_salary_text("€60.5 - €70.5") == (60.5, 70.5, "EUR")


def test_an_hourly_range_is_not_published_as_annual() -> None:
    """A rate that needs an hours assumption is refused, not overstated."""
    assert parse_salary_text("€25 - €30 per hour") == (None, None, None)


def test_a_monthly_range_is_annualised() -> None:
    assert parse_salary_text("€3,000 - €4,000 per month") == (36_000.0, 48_000.0, "EUR")


def test_annualise_handles_schema_org_units() -> None:
    assert annualise_salary(3_000, 4_000, "MONTH") == (36_000.0, 48_000.0)
    assert annualise_salary(1_000, 2_000, "WEEK") == (52_000.0, 104_000.0)
    # Sub-weekly needs working-time assumptions the advert does not state.
    assert annualise_salary(30, 40, "HOUR") == (None, None)


def test_as_float_reads_both_grouping_styles() -> None:
    assert _as_float("1,234.56") == 1_234.56
    assert _as_float("1.234,56") == 1_234.56
    assert _as_float("") is None
    assert _as_float(42000) == 42_000.0


def test_schema_org_hourly_salary_is_not_stored_as_annual() -> None:
    fields = jobposting_to_fields(
        {
            "title": "Support Agent",
            "description": "Help customers.",
            "baseSalary": {
                "currency": "EUR",
                "value": {"minValue": 25, "maxValue": 30, "unitText": "HOUR"},
            },
        },
        source_url="https://example.test/job/1",
    )
    assert fields["salary_min"] is None
    assert fields["salary_max"] is None


def test_schema_org_monthly_salary_is_annualised() -> None:
    fields = jobposting_to_fields(
        {
            "title": "Analyst",
            "description": "Analyse.",
            "baseSalary": {
                "currency": "EUR",
                "value": {"minValue": 3000, "maxValue": 4000, "unitText": "MONTH"},
            },
        },
        source_url="https://example.test/job/2",
    )
    assert fields["salary_min"] == 36_000.0
    assert fields["salary_max"] == 48_000.0
