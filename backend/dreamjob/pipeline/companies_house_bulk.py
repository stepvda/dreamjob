"""Loading the UK company register into the staged universe (FR-241, FR-341, DR-101).

Companies House publishes its whole register as one open CSV
(``BasicCompanyDataAsOneFile-*.csv``) under the Open Government Licence.  Like
the Belgian KBO extract, it is staged into ``company_seed`` rather than written
straight into ``company``: the register holds millions of entities, most of
which no campaign will ever look at, and a seed is promoted only when a
directive selects it.

The file is one row per company with the registered name, number, status,
incorporation date, post town/postcode, company category and up to four SIC
codes.  The parser keys on those headers rather than their positions, so a
column added by a later release does not shift everything.

The UK register carries no NACE code, so the SIC division (the first two digits
of the SIC code) is used as ``nace_primary``.  It is a coarser sector key than
Belgium's, which is the honest consequence of SIC being a different taxonomy -
the planner reads it the same way and the same sector filter applies.
"""

from __future__ import annotations

import csv
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from dreamjob.db.connection import new_id, query_one, to_json, upsert_row, utcnow

log = logging.getLogger(__name__)

SOURCE = "companies_house_bulk"

#: ``CompanyStatus`` values that mean the entity can be an employer.  A company
#: in liquidation or dissolved is not somewhere anyone will be hired.
_ACTIVE_PREFIXES = ("active",)

_SIC_RE = re.compile(r"(\d{2,5})")


def _rows(path: Path) -> Iterator[dict[str, str]]:
    """Read the CSV, tolerating the encodings Companies House has published."""
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                yield from csv.DictReader(handle)
            return
        except UnicodeDecodeError:
            continue


def _column(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value:
            return value.strip()
    return ""


def _normalised(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _is_active(status: str) -> bool:
    lowered = (status or "").strip().lower()
    return any(lowered.startswith(prefix) for prefix in _ACTIVE_PREFIXES)


def _sic_codes(row: dict[str, str]) -> list[str]:
    codes: list[str] = []
    for index in range(1, 5):
        text = _column(row, f"SICCode.SicText_{index}")
        match = _SIC_RE.search(text)
        if match and match.group(1) not in codes:
            codes.append(match.group(1))
    return codes


def _division(code: str) -> str | None:
    """The two-digit SIC division, which stands in for a NACE section."""
    return code[:2] if len(code) >= 2 and code[:2].isdigit() else None


def find_csv(path: Path | str) -> Path:
    """The one data file in a directory, or the file itself when given one."""
    path = Path(path)
    if path.is_file():
        return path
    candidates = sorted(path.glob("*.csv"))
    if not candidates:
        raise FileNotFoundError(f"No CSV found in {path}")
    # The published single-file release is named BasicCompanyData*.csv; prefer
    # it, but any CSV will do for a split release.
    for candidate in candidates:
        if "basiccompanydata" in candidate.name.lower():
            return candidate
    return candidates[0]


def ingest(path: Path | str, *, limit: int | None = None) -> dict[str, Any]:
    """Stage a Companies House bulk file.  Idempotent, so a re-run is a refresh."""
    csv_path = find_csv(path)
    staged = 0
    skipped_no_name = 0
    active = 0
    by_division: dict[str, int] = {}

    for row in _rows(csv_path):
        name = _column(row, "CompanyName", "Company Name")
        number = _column(row, "CompanyNumber", "Company Number")
        if not name or not number:
            skipped_no_name += 1
            continue
        if limit is not None and staged >= limit:
            break
        status = _column(row, "CompanyStatus")
        if not _is_active(status):
            continue
        codes = _sic_codes(row)
        primary = _division(codes[0]) if codes else None
        existing = query_one(
            "SELECT id FROM company_seed WHERE source = ? AND entity_number = ?",
            (SOURCE, number),
        )
        population = _column(row, "RegAddress.PostTown", "RegAddress.AddressLine1")
        upsert_row(
            "company_seed",
            {
                "id": existing["id"] if existing else new_id(),
                "source": SOURCE,
                "entity_number": number,
                "name": name,
                "normalised_name": _normalised(name),
                "status": "AC",
                "entity_type": "company",
                "legal_form": _column(row, "CompanyCategory"),
                "nace_codes": to_json(codes[:12]),
                "nace_primary": primary,
                "municipality": population or None,
                "postcode": _column(row, "RegAddress.PostCode") or None,
                "country": "GB",
                "start_date": _column(row, "IncorporationDate") or None,
                "collected_at": utcnow(),
            },
            ["source", "entity_number"],
        )
        staged += 1
        active += 1
        if primary:
            by_division[primary] = by_division.get(primary, 0) + 1

    log.info("Companies House ingest: staged %d entities from %s", staged, csv_path.name)
    return {
        "staged": staged,
        "active": active,
        "skipped_without_a_name": skipped_no_name,
        "top_divisions": dict(sorted(by_division.items(), key=lambda kv: -kv[1])[:12]),
        "file": str(csv_path),
    }


def coverage() -> dict[str, Any]:
    """What the staged UK universe holds, for the operator (FR-361)."""
    counts = query_one(
        "SELECT COUNT(*) AS n, "
        "       SUM(materialised_company_id IS NOT NULL) AS materialised "
        "FROM company_seed WHERE source = ?",
        (SOURCE,),
    ) or {"n": 0, "materialised": 0}
    return {
        "seeded": int(counts["n"] or 0),
        "materialised": int(counts["materialised"] or 0),
        "source": SOURCE,
        "note": (
            "SIC divisions stand in for NACE: the UK register carries no NACE code, "
            "so nace_primary is the two-digit SIC division."
        ),
    }
