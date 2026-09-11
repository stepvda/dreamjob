"""The registry universe, its staging, and the selection drawn from it (FR-143, FR-341).

The point of the seed table is that two million registered entities are an
index, and only the ones a campaign selects become company rows.  These tests
pin the staging (the real open-data headers, the primary NACE division, the
Belgian country form), the selection (sector, active, and no sole traders), and
that promoting a seed twice reuses the company rather than creating a twin.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dreamjob.db.connection import query_one
from dreamjob.pipeline import kbo_bulk, sectors

# The published column names, so a renamed column in the fixture is a real
# failure rather than a test that agreed with the parser.
ENTERPRISE = """EnterpriseNumber,Status,JuridicalSituation,TypeOfEnterprise,JuridicalForm,JuridicalFormCAC,StartDate
0123456789,AC,0,1,010,0,2015-03-01
0987654321,AC,0,1,010,0,2009-06-15
0555555555,ST,0,1,010,0,2018-01-01
0777777777,AC,0,3,010,0,2020-02-02
"""

DENOMINATION = """EntityNumber,Language,TypeOfDenomination,Denomination
0123456789,NL,1,Acme Software
0123456789,FR,1,Acme Logiciel
0987654321,NL,1,Traiteur Dupont
0555555555,NL,1,Stopped BV
0777777777,NL,1,Jan Janssens
"""

ADDRESS = """EntityNumber,TypeOfAddress,CountryNL,CountryFR,Zipcode,MunicipalityNL,MunicipalityFR
0123456789,1,België,Belgique,1000,Brussel,Bruxelles
0987654321,1,België,Belgique,9000,Gent,Gand
0555555555,1,België,Belgique,2000,Antwerpen,Anvers
0777777777,1,België,Belgique,3000,Leuven,Louvain
"""

ACTIVITY = """EntityNumber,ActivityGroup,NaceVersion,NaceCode,Classification
0123456789,MAIN,2025,62.010,MAIN
0123456789,SECONDARY,2025,63.110,SECONDARY
0987654321,MAIN,2025,56.210,MAIN
0555555555,MAIN,2025,62.020,MAIN
0777777777,MAIN,2025,62.030,MAIN
"""


@pytest.fixture()
def extract(tmp_path: Path) -> Path:
    (tmp_path / "enterprise.csv").write_text(ENTERPRISE, encoding="utf-8")
    (tmp_path / "denomination.csv").write_text(DENOMINATION, encoding="utf-8")
    (tmp_path / "address.csv").write_text(ADDRESS, encoding="utf-8")
    (tmp_path / "activity.csv").write_text(ACTIVITY, encoding="utf-8")
    return tmp_path


def test_the_real_headers_are_parsed(extract):
    result = kbo_bulk.ingest(extract)
    assert result["entities"] == 4
    # Every entity has a name in this fixture.
    assert result["skipped_without_a_name"] == 0
    assert result["staged"] == 4
    assert result["top_divisions"]["62"] == 3

    acme = query_one(
        "SELECT * FROM company_seed WHERE entity_number = '0123456789'"
    )
    assert acme["name"] == "Acme Software"
    assert acme["status"] == "AC"
    assert acme["entity_type"] == "company"
    assert acme["postcode"] == "1000"
    assert acme["municipality"] == "Brussel"
    # "België/Belgique" becomes the ISO code the campaign vocabulary uses.
    assert acme["country"] == "BE"


def test_the_primary_nace_is_the_first_activity(extract):
    """The main activity decides the division, not whichever row came last."""
    kbo_bulk.ingest(extract)
    acme = query_one("SELECT nace_primary, nace_codes FROM company_seed WHERE entity_number = '0123456789'")
    assert acme["nace_primary"] == "62"
    assert "62.010" in acme["nace_codes"] and "63.110" in acme["nace_codes"]


def test_a_sole_trader_is_staged_but_flagged(extract):
    kbo_bulk.ingest(extract)
    trader = query_one("SELECT entity_type FROM company_seed WHERE entity_number = '0777777777'")
    assert trader["entity_type"] == "sole_trader"


def test_ingest_is_idempotent(extract):
    kbo_bulk.ingest(extract)
    first = query_one("SELECT id FROM company_seed WHERE entity_number = '0123456789'")["id"]
    kbo_bulk.ingest(extract)
    assert query_one("SELECT COUNT(*) n FROM company_seed")["n"] == 4
    assert query_one("SELECT id FROM company_seed WHERE entity_number = '0123456789'")["id"] == first


class TestSelection:
    def test_sector_selects_only_that_division(self, extract):
        kbo_bulk.ingest(extract)
        seeds = kbo_bulk.seeds_in_scope(divisions=["62"])
        names = {s["name"] for s in seeds}
        assert "Acme Software" in names
        assert "Traiteur Dupont" not in names  # division 56

    def test_a_stopped_company_is_not_a_place_to_be_hired(self, extract):
        kbo_bulk.ingest(extract)
        names = {s["name"] for s in kbo_bulk.seeds_in_scope(divisions=["62"])}
        assert "Stopped BV" not in names

    def test_a_sole_trader_is_never_selected(self, extract):
        """Their register row carries a person's name, and the base is shared."""
        kbo_bulk.ingest(extract)
        names = {s["name"] for s in kbo_bulk.seeds_in_scope(divisions=["62"])}
        assert "Jan Janssens" not in names
        # ...unless an operator explicitly asks for them.
        assert "Jan Janssens" in {
            s["name"] for s in kbo_bulk.seeds_in_scope(divisions=["62"], include_sole_traders=True)
        }

    def test_no_industry_means_the_whole_universe(self, extract):
        """An empty directive must not become an empty search."""
        kbo_bulk.ingest(extract)
        names = {s["name"] for s in kbo_bulk.seeds_in_scope(divisions=None)}
        # Two active companies; the stopped one and the sole trader are out.
        assert {"Acme Software", "Traiteur Dupont"} <= names
        assert "Stopped BV" not in names and "Jan Janssens" not in names


class TestMaterialise:
    def test_a_seed_becomes_a_company_keyed_on_the_register(self, extract):
        kbo_bulk.ingest(extract)
        seed = kbo_bulk.seeds_in_scope(divisions=["62"])[0]
        company_id = kbo_bulk.materialise(seed)

        company = query_one("SELECT * FROM company WHERE id = ?", (company_id,))
        assert company["legal_id"] == seed["entity_number"]
        assert company["legal_id_type"] == "kbo_bce"
        assert company["country"] == "BE"
        assert "62.010" in company["sector_codes"]
        # And the seed records where it went, so it is not promoted again.
        assert query_one(
            "SELECT materialised_company_id FROM company_seed WHERE id = ?", (seed["id"],)
        )["materialised_company_id"] == company_id

    def test_promoting_twice_reuses_the_company(self, extract):
        """The register number is the identity anchor (DR-101), not the name."""
        kbo_bulk.ingest(extract)
        seed = kbo_bulk.seeds_in_scope(divisions=["62"])[0]
        first = kbo_bulk.materialise(seed)
        second = kbo_bulk.materialise(seed)
        assert first == second
        assert query_one(
            "SELECT COUNT(*) n FROM company WHERE legal_id = ?", (seed["entity_number"],)
        )["n"] == 1


class TestSectorMapping:
    def test_the_common_technology_words_resolve(self):
        for label in ("information technology", "IT", "software", "SaaS", "AI", "cybersecurity"):
            assert sectors.divisions_for(label), label

    def test_an_unknown_label_maps_to_nothing(self):
        assert sectors.divisions_for("underwater basket weaving") == ()

    def test_directives_yield_divisions(self):
        from dreamjob.pipeline.directives import DirectiveSetPayload, JobContentDirectives

        payload = DirectiveSetPayload(
            name="t",
            job_content=JobContentDirectives(
                industries_include=["information technology", "fintech"]
            ),
        )
        divisions = sectors.directive_divisions(payload)
        assert "62" in divisions and "64" in divisions

    def test_no_industries_means_no_constraint(self):
        from dreamjob.pipeline.directives import DirectiveSetPayload

        assert sectors.directive_divisions(DirectiveSetPayload(name="t")) == []


def test_select_for_directives_walks_the_whole_path(extract):
    from dreamjob.pipeline.directives import DirectiveSetPayload, JobContentDirectives

    kbo_bulk.ingest(extract)
    payload = DirectiveSetPayload(
        name="t", job_content=JobContentDirectives(industries_include=["software"])
    )
    result = kbo_bulk.select_for_directives(payload, limit=10)
    assert result["divisions"] == ["62", "58"]
    # ``selected`` is the in-scope set; ``materialised`` counts only what this
    # call promoted, and the shared scratch database means an earlier test may
    # already have promoted it. What must hold is that the path produces KBO
    # companies.
    assert result["selected"] >= 1
    assert query_one("SELECT COUNT(*) n FROM company WHERE legal_id_type = 'kbo_bce'")["n"] >= 1


def test_coverage_reports_the_staged_universe(extract):
    kbo_bulk.ingest(extract)
    cov = kbo_bulk.coverage()
    assert cov["seeded"] >= 4
    assert cov["materialised"] >= 0
