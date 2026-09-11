"""Deriving a company's website from what we already hold (FR-221).

The guard is the point of this file. A first version accepted any vacancy URL's
host as the employer's site, and wrote ``fgov.be`` as the website of three
different Belgian companies and ``europa.eu`` for two more - the advert's host,
not the employer's. A wrong domain is worse than a missing one: the crawl
profiles a stranger under this company's name.
"""

from __future__ import annotations

from dreamjob.pipeline import domain_resolver as dr


class TestAtsTenant:
    def test_the_tenant_is_the_employer_name(self) -> None:
        assert dr.ats_tenant("aikidosecurity.recruitee.com") == "aikidosecurity"
        assert dr.ats_tenant("craftzing.teamtailor.com") == "craftzing"
        assert dr.ats_tenant("technopolis-group.jobs.personio.de") == "technopolisgroup"

    def test_a_company_host_is_not_an_ats_tenant(self) -> None:
        assert dr.ats_tenant("jobs.monizze.be") is None
        assert dr.ats_tenant("aikidosecurity.com") is None

    def test_ats_hosts_are_recognised(self) -> None:
        assert dr.is_ats_host("aikidosecurity.recruitee.com")
        assert dr.is_ats_host("boards.greenhouse.io")
        assert not dr.is_ats_host("jobs.monizze.be")


class TestAggregatorGuard:
    def test_a_job_board_is_an_aggregator(self) -> None:
        for host in ("europa.eu", "fgov.be", "ictjob.be", "www.indeed.com", "jobat.be"):
            assert dr.is_aggregator(host), host

    def test_a_company_host_is_not(self) -> None:
        for host in ("aikidosecurity.com", "relu.be", "materialise.com", "modjo.com"):
            assert not dr.is_aggregator(host), host

    def test_a_syndicated_advert_is_never_the_company_site(self) -> None:
        """The exact failure: DIGNIFY, IBEXA and MODJO all got ``fgov.be``."""
        company = {"name": "DIGNIFY"}
        for url in (
            "https://fgov.be/nl/vacatures/123",
            "https://europa.eu/jobs/456",
            "https://www.ictjob.be/en/job/data-engineer/1",
        ):
            assert dr.candidates(company, [url]) == [], url

    def test_a_name_matching_host_is_accepted(self) -> None:
        assert "relu.be" in dr.candidates({"name": "Relu"}, ["https://relu.be/careers"])
        assert "modjo.com" in dr.candidates({"name": "MODJO"}, ["https://modjo.com/jobs"])

    def test_a_host_sharing_nothing_with_the_name_is_rejected(self) -> None:
        assert dr.candidates({"name": "DIGNIFY"}, ["https://somethingelse.be/jobs"]) == []


class TestBoardUrl:
    def test_a_board_is_built_from_the_vendor_and_slug(self) -> None:
        assert dr.board_url({"ats_vendor": "recruitee", "ats_slug": "craftzing"}) == (
            "https://craftzing.recruitee.com"
        )
        assert dr.board_url({"ats_vendor": "personio", "ats_slug": "acme"}) == (
            "https://acme.jobs.personio.de"
        )
        assert dr.board_url({"ats_vendor": "greenhouse", "ats_slug": "acme"}) == (
            "https://boards.greenhouse.io/acme"
        )

    def test_no_board_without_a_slug(self) -> None:
        assert dr.board_url({"ats_vendor": "recruitee"}) is None
        assert dr.board_url({"ats_slug": "acme"}) is None
        assert dr.board_url({}) is None


class TestHomeUrlFallback:
    def test_the_crawl_falls_back_to_the_board(self) -> None:
        """Before this the crawl gave up, and 57 of 66 employers stayed bare."""
        from dreamjob.pipeline.company_profile import home_url_for

        assert home_url_for({"ats_vendor": "recruitee", "ats_slug": "craftzing"}) == (
            "https://craftzing.recruitee.com"
        )

    def test_the_company_site_still_wins(self) -> None:
        from dreamjob.pipeline.company_profile import home_url_for

        assert home_url_for(
            {"domain": "aikido.dev", "ats_vendor": "recruitee", "ats_slug": "aikidosecurity"}
        ) == "https://aikido.dev"
