"""Open-application adverts, and the enrichment coverage view (FR-263, FR-341).

An employer's "Spontaneous Application" posting is a third kind of row: it is
genuinely published - it sits on the company's own ATS board - so it is a
vacancy and not a speculative opening, but it names no role. The ranked list
has to be able to say so, which is what these tests pin. The coverage tests pin
that "is this employer enriched?" has one answer rather than five.
"""

from __future__ import annotations

from dreamjob.db.repositories import companies as company_repo
from dreamjob.pipeline.opportunities import is_open_application


class TestOpenApplication:
    def test_the_titles_employers_actually_use(self) -> None:
        for title in (
            "Spontaneous Application",
            "Spontaneous application - ARCTIK",
            "spontaneous application",
            "Open sollicitatie",
            "Candidature spontanée",
            "General Application for Unicorn Candidates",
            "Talent Pool - Software",
            "Join our talent community",
        ):
            assert is_open_application(title), title

    def test_a_real_role_is_not_an_open_application(self) -> None:
        for title in (
            "Senior Backend Engineer",
            "Front-end Engineer",
            "Founding Engineer",
            "Data Engineer",
            "Software Engineer (AI Code Audit)",
        ):
            assert not is_open_application(title), title

    def test_the_title_is_what_decides_not_a_stray_phrase(self) -> None:
        """An ordinary posting is not reclassified by one phrase in its body."""
        assert not is_open_application(
            "Backend Engineer",
            "Apply via our portal. " + "x" * 500 + " spontaneous application",
        )

    def test_the_opening_lines_of_a_body_can_carry_it(self) -> None:
        """Some boards name the role normally and invite in the first lines."""
        assert is_open_application(
            "Software Engineer", "This is a general application for future roles."
        )

    def test_a_missing_title_is_not_one(self) -> None:
        assert not is_open_application(None)
        assert not is_open_application("")


class TestEnrichmentCoverage:
    def test_the_view_answers_what_is_known_and_what_is_not(self) -> None:
        data = company_repo.enrichment_coverage(limit=5)
        assert "aggregate" in data and "companies" in data
        aggregate = data["aggregate"]
        for key in (
            "companies",
            "with_profile",
            "with_financials",
            "with_signals",
            "with_competitors",
            "with_employer_kind",
        ):
            assert key in aggregate, key
        # Shares are present so a coverage screen can show a percentage.
        assert "share_with_profile" in aggregate

    def test_per_company_rows_carry_each_pass(self) -> None:
        data = company_repo.enrichment_coverage(limit=5)
        for row in data["companies"]:
            for key in (
                "has_profile",
                "financial_years",
                "signals",
                "competitors",
                "employer_kind",
                "opportunities",
            ):
                assert key in row, key

    def test_the_note_explains_an_estimated_financial(self) -> None:
        """'Estimated' must not be read as 'the company has no accounts'."""
        data = company_repo.enrichment_coverage(limit=1)
        assert "estimated" in data["note"]


class TestBusiestCompanies:
    def test_it_returns_ids_not_names(self) -> None:
        ids = company_repo.busiest_companies(limit=3)
        assert isinstance(ids, list)
        assert all(isinstance(i, str) for i in ids)
