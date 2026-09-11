"""The UK register's bulk staging and promotion (FR-241, DR-101).

Companies House publishes one open CSV for the whole register.  Like the Belgian
extract, it is staged into ``company_seed`` and promoted only when a directive
selects it; these tests pin the real published headers, the SIC-to-division
mapping, the active-only rule, and that promotion keys on the register's own
number under the right identifier type.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import query_one
from dreamjob.db.migrator import migrate
from dreamjob.pipeline import companies_house_bulk as ch


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A throw-away migrated database, so staging never touches another test."""
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()

HEADER = (
    "CompanyName,CompanyNumber,RegAddress.PostTown,RegAddress.PostCode,"
    "CompanyCategory,CompanyStatus,IncorporationDate,"
    "SICCode.SicText_1,SICCode.SicText_2,CompanyStatusDetail\n"
)
ROWS = (
    "Acme Software Ltd,01234567,London,EC1A 1BB,Private Limited Company,"
    "Active,2015-03-01,62020 - Information technology consultancy activities,"
    "62012 - Business and domestic software development,Active\n"
    "Globex Consulting PLC,07654321,Manchester,M1 1AA,Public Limited Company,"
    "Active,2009-06-15,70229 - Management consultancy activities other than financial management,,\n"
    "Dormant Shelf Ltd,09999999,Leeds,LS1 1AA,Private Limited Company,"
    "Dissolved,2018-01-01,82990 - Other business support service activities n.e.c.,,\n"
)


@pytest.fixture()
def ch_csv(tmp_path: Path) -> Path:
    path = tmp_path / "BasicCompanyDataAsOneFile-2026-09-01.csv"
    path.write_text(HEADER + ROWS, encoding="utf-8")
    return path


def test_only_active_companies_are_staged(db, ch_csv):
    result = ch.ingest(ch_csv)
    assert result["staged"] == 2
    assert query_one("SELECT COUNT(*) n FROM company_seed WHERE source = ?", (ch.SOURCE,))["n"] == 2
    assert query_one(
        "SELECT COUNT(*) n FROM company_seed WHERE entity_number = '09999999'"
    )["n"] == 0


def test_sic_becomes_the_sector_division(db, ch_csv):
    ch.ingest(ch_csv)
    acme = query_one(
        "SELECT nace_primary, nace_codes, country, legal_form FROM company_seed "
        "WHERE entity_number = '01234567'"
    )
    assert acme["nace_primary"] == "62"
    assert "62020" in acme["nace_codes"]
    assert acme["country"] == "GB"
    assert acme["legal_form"] == "Private Limited Company"


def test_ingest_is_idempotent(db, ch_csv):
    ch.ingest(ch_csv)
    first = query_one("SELECT id FROM company_seed WHERE entity_number = '01234567'")["id"]
    ch.ingest(ch_csv)
    assert query_one("SELECT id FROM company_seed WHERE entity_number = '01234567'")["id"] == first


def test_promotion_keys_on_the_uk_register_number(db, ch_csv):
    """DR-101: the identifier type follows the source, not a Belgian default."""
    from dreamjob.pipeline import kbo_bulk

    ch.ingest(ch_csv)
    seed = query_one("SELECT * FROM company_seed WHERE entity_number = '01234567'")
    company_id = kbo_bulk.materialise(seed)
    company = query_one("SELECT legal_id, legal_id_type, country FROM company WHERE id = ?", (company_id,))
    assert company["legal_id"] == "01234567"
    assert company["legal_id_type"] == "companies_house"
    assert company["country"] == "GB"


def test_coverage_counts_the_staged_source(db, ch_csv):
    ch.ingest(ch_csv)
    coverage = ch.coverage()
    assert coverage["seeded"] == 2
    assert coverage["source"] == ch.SOURCE
