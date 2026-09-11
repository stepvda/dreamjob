"""Loading the Belgian company register into the staged universe (FR-161, FR-341).

Belgium publishes its company register as a monthly open-data bulk extract:
a zip of CSVs, one per aspect of an entity.  That is the lawful, official,
complete answer to "which companies exist", and it is the opposite of the
collection model the rest of the system uses - one download instead of two
million rate-limited lookups, and a population that includes the companies that
have never advertised, which is the only population from which a speculative
opening can be found.

Files read, by their published names:

    enterprise.csv   EnterpriseNumber, Status, JuridicalSituation,
                     TypeOfEnterprise, JuridicalForm, JuridicalFormCAC, StartDate
    denomination.csv EntityNumber, Language, TypeOfDenomination, Denomination
    address.csv      EntityNumber, TypeOfAddress, CountryNL, CountryFR, Zipcode,
                     MunicipalityNL, MunicipalityFR, StreetNL, HouseNumber, ...
    activity.csv     EntityNumber, ActivityGroup, NaceVersion, NaceCode, Classification

The parser is written against those headers rather than their positions, so a
column added by a later extract does not shift everything.

Nothing here promotes a seed into ``company``: staging is cheap and a promotion
is a decision the planner makes (see ``discovery.seed_companies``).  Two million
staged rows are an index; two million company rows would be a liability.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from dreamjob.db.connection import new_id, query_one, to_json, upsert_row, utcnow

log = logging.getLogger(__name__)

SOURCE = "kbo_bulk"

#: Enterprise status codes in the register.  0 is active; the rest are stopped
#: or being wound up, and a stopped company is not a place anyone will be hired.
ACTIVE_STATUS = "AC"

#: ``TypeOfEnterprise``: a company, a public body, or a natural person trading
#: under their own name.  A sole trader's register entry carries a person's
#: name, so they are staged but flagged rather than blended in (NFR-201).
ENTITY_TYPES = {
    "1": "company",
    "2": "public_body",
    "3": "sole_trader",
}

_ACTIVITY_PRIMARY = {"MAIN", "PRIMARY", "1"}


def _rows(path: Path) -> Iterator[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def _column(row: dict[str, str], *names: str) -> str:
    """The first of ``names`` present in the row, wherever the extract put it."""
    for name in names:
        for key in (name, name.lower(), name.upper()):
            if key in row and row[key] is not None:
                return str(row[key]).strip()
    return ""


def _normalised(name: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in name.lower()).split())


def _primary_nace(codes: list[str]) -> str | None:
    """The two-digit division of the first code - how the seed is selected on."""
    for code in codes:
        digits = "".join(ch for ch in str(code) if ch.isdigit())
        if len(digits) >= 2:
            return digits[:2]
    return None


def _read_extract(directory: Path) -> dict[str, Any]:
    """Parse the CSVs into the rows :func:`ingest` stages.  No database access."""
    enterprise = {}
    for row in _rows(directory / "enterprise.csv"):
        number = _column(row, "EnterpriseNumber", "EntityNumber")
        if not number:
            continue
        enterprise[number] = {
            "status": _column(row, "Status"),
            "entity_type": ENTITY_TYPES.get(_column(row, "TypeOfEnterprise"), None),
            "legal_form": _column(row, "JuridicalForm"),
            "start_date": _column(row, "StartDate")[:10] or None,
        }

    names: dict[str, str] = {}
    for row in _rows(directory / "denomination.csv"):
        number = _column(row, "EntityNumber")
        name = _column(row, "Denomination")
        if not number or not name:
            continue
        # Prefer the Dutch name, then French, then whatever came first: the
        # campaigns here are Belgian and the register is bilingual.
        language = _column(row, "Language").upper()
        if number not in names or language in ("NL", "1"):
            names[number] = name

    addresses: dict[str, dict[str, str]] = {}
    for row in _rows(directory / "address.csv"):
        number = _column(row, "EntityNumber")
        if not number or number in addresses:
            continue
        country = _column(row, "CountryNL", "CountryFR")
        addresses[number] = {
            "postcode": _column(row, "Zipcode"),
            "municipality": _column(row, "MunicipalityNL", "MunicipalityFR"),
            # The register writes "België/Belgique/Belgien"; the campaign
            # vocabulary is ISO-2, so anything Belgian becomes BE.
            "country": "BE" if country and "belg" in country.lower() else (country[:2].upper() or "BE"),
        }

    activities: dict[str, list[str]] = {}
    for row in _rows(directory / "activity.csv"):
        number = _column(row, "EntityNumber")
        code = _column(row, "NaceCode")
        if not number or not code:
            continue
        classification = _column(row, "Classification").upper()
        group = _column(row, "ActivityGroup").upper()
        codes = activities.setdefault(number, [])
        if (classification in _ACTIVITY_PRIMARY or group in _ACTIVITY_PRIMARY) and codes:
            codes.insert(0, code)
        else:
            codes.append(code)

    return {
        "enterprise": enterprise,
        "names": names,
        "addresses": addresses,
        "activities": activities,
    }


def ingest(directory: Path | str, *, limit: int | None = None) -> dict[str, Any]:
    """Stage a KBO open-data extract.  Idempotent, so a re-run is a refresh."""
    directory = Path(directory)
    missing = [f for f in ("enterprise.csv", "denomination.csv") if not (directory / f).exists()]
    if missing:
        raise FileNotFoundError(
            f"{directory} does not look like a KBO open-data extract; missing {', '.join(missing)}"
        )

    extract = _read_extract(directory)
    staged = 0
    skipped_no_name = 0
    by_division: dict[str, int] = {}

    for number, facts in extract["enterprise"].items():
        name = extract["names"].get(number)
        if not name:
            skipped_no_name += 1
            continue
        if limit is not None and staged >= limit:
            break
        codes = extract["activities"].get(number, [])
        primary = _primary_nace(codes)
        address = extract["addresses"].get(number, {})
        # Reuse the existing id when the entity is already staged, so a monthly
        # re-run refreshes rather than re-keys - the id is what a promotion
        # records, and changing it would orphan the link.
        existing = query_one(
            "SELECT id FROM company_seed WHERE source = ? AND entity_number = ?",
            (SOURCE, number),
        )
        upsert_row(
            "company_seed",
            {
                "id": existing["id"] if existing else new_id(),
                "source": SOURCE,
                "entity_number": number,
                "name": name,
                "normalised_name": _normalised(name),
                "status": facts.get("status"),
                "entity_type": facts.get("entity_type"),
                "legal_form": facts.get("legal_form"),
                "nace_codes": to_json(codes[:12]),
                "nace_primary": primary,
                "municipality": address.get("municipality"),
                "postcode": address.get("postcode"),
                "country": address.get("country") or "BE",
                "start_date": facts.get("start_date"),
                "collected_at": utcnow(),
            },
            ["source", "entity_number"],
        )
        staged += 1
        if primary:
            by_division[primary] = by_division.get(primary, 0) + 1

    log.info("KBO ingest: staged %d of %d entities", staged, len(extract["enterprise"]))
    return {
        "staged": staged,
        "entities": len(extract["enterprise"]),
        "skipped_without_a_name": skipped_no_name,
        "top_divisions": dict(sorted(by_division.items(), key=lambda kv: -kv[1])[:12]),
    }


def coverage() -> dict[str, Any]:
    """What the staged universe holds, for the operator and the planner (FR-361)."""
    total = query_one("SELECT COUNT(*) AS n FROM company_seed") or {"n": 0}
    active = query_one(
        "SELECT COUNT(*) AS n FROM company_seed WHERE status = 'AC' OR status IS NULL"
    ) or {"n": 0}
    materialised = query_one(
        "SELECT COUNT(*) AS n FROM company_seed WHERE materialised_company_id IS NOT NULL"
    ) or {"n": 0}
    return {
        "seeded": int(total["n"]),
        "active_or_unknown": int(active["n"]),
        "materialised": int(materialised["n"]),
        "sources": [
            dict(row)
            for row in (
                query_one(
                    "SELECT source, COUNT(*) AS n FROM company_seed GROUP BY source"
                ),
            )
            if row
        ],
        "note": (
            "The registry universe is staged, not published: a seed becomes a company row "
            "only when a campaign selects it, so a search never pays for two million "
            "companies it will not look at."
        ),
    }


# ---------------------------------------------------------------------------
# Selecting from the universe, and promoting what is selected
# ---------------------------------------------------------------------------


def seeds_in_scope(
    *,
    country: str = "BE",
    divisions: list[str] | None = None,
    limit: int = 200,
    include_stopped: bool = False,
    include_sole_traders: bool = False,
) -> list[dict[str, Any]]:
    """The staged companies a campaign should consider (FR-143, FR-164).

    ``divisions`` is empty when the seeker named no industry, which means "the
    whole universe" rather than "nothing" - an empty result there would turn a
    missing directive into an empty search.
    """
    from dreamjob.db.connection import query_all  # noqa: PLC0415

    sql = "SELECT * FROM company_seed WHERE country = ?"
    params: list[Any] = [country.upper()]
    if not include_stopped:
        # The register writes AC for active; a null status is a very old extract
        # that did not carry one, and is kept rather than discarded.
        sql += " AND (status = 'AC' OR status IS NULL OR status = '')"
    if not include_sole_traders:
        # A sole trader's register row carries a natural person's name, and the
        # knowledge base is shared with every other job seeker (NFR-201, FR-344).
        sql += " AND (entity_type IS NULL OR entity_type <> 'sole_trader')"
    if divisions:
        marks = ",".join("?" for _ in divisions)
        sql += f" AND nace_primary IN ({marks})"
        params.extend(divisions)
    # Unpromoted first, so a second run continues into fresh ground; then by
    # how long the entity has been established, which is a rough proxy for
    # being a real employer rather than a shelf company.
    sql += " ORDER BY (materialised_company_id IS NOT NULL), start_date ASC LIMIT ?"
    params.append(int(limit))
    return [dict(r) for r in query_all(sql, tuple(params))]


def materialise(seed: dict[str, Any]) -> str:
    """Promote one staged entity into a ``company`` row (FR-341, DR-101).

    Keyed on the register's own enterprise number, so a company already known
    from another campaign is reused rather than duplicated - the register is the
    identity anchor DR-101 asks for.  Returns the company id.
    """
    from dreamjob.db.connection import update_row  # noqa: PLC0415

    number = str(seed["entity_number"])
    existing = query_one(
        "SELECT id FROM company WHERE legal_id = ? AND legal_id_type = 'kbo'", (number,)
    )
    codes = seed.get("nace_codes")
    if isinstance(codes, str):
        from dreamjob.db.connection import from_json  # noqa: PLC0415

        codes = from_json(codes, []) or []
    values: dict[str, Any] = {
        "legal_id": number,
        "legal_id_type": "kbo",
        "name": seed["name"],
        "normalised_name": seed["normalised_name"],
        "country": seed.get("country") or "BE",
        "sector_codes": to_json(
            [{"code": c, "source": "kbo"} for c in (codes or [])]
        ),
        "source": f"{SOURCE}:{number}",
        "access_method": "registry",
        "collected_at": utcnow(),
    }
    if seed.get("municipality"):
        values["locations"] = to_json(
            [{"municipality": seed["municipality"], "postcode": seed.get("postcode")}]
        )

    if existing:
        company_id = str(existing["id"])
        from dreamjob.db.connection import update_row as _update  # noqa: PLC0415

        _update("company", company_id, values)
    else:
        values["id"] = new_id()
        company_id = insert_seed_company(values)
    update_row("company_seed", seed["id"], {"materialised_company_id": company_id})
    return company_id


def insert_seed_company(values: dict[str, Any]) -> str:
    """Insert a materialised company.  Split out so the write is one call."""
    from dreamjob.db.connection import write_tx  # noqa: PLC0415

    payload = {
        k: (to_json(v) if isinstance(v, (dict, list)) else v) for k, v in values.items()
    }
    payload.setdefault("confidence", 0.6)
    cols = ", ".join(payload)
    marks = ", ".join(f":{k}" for k in payload)
    with write_tx() as conn:
        conn.execute(f"INSERT INTO company ({cols}) VALUES ({marks})", payload)
    return str(values["id"])


def select_for_directives(
    directives: Any, *, limit: int = 50, country: str = "BE"
) -> dict[str, Any]:
    """The whole path: directives -> NACE divisions -> staged seeds -> companies.

    This is the seam the product was missing.  Before it, the only companies a
    campaign could consider were the ones a previous scrape had happened to
    find, which excludes every company that has never advertised - the entire
    population a speculative opening is drawn from.
    """
    from dreamjob.pipeline import sectors  # noqa: PLC0415

    divisions = sectors.directive_divisions(directives)
    seeds = seeds_in_scope(country=country, divisions=divisions or None, limit=limit)
    created = 0
    for seed in seeds:
        if seed.get("materialised_company_id"):
            continue
        materialise(seed)
        created += 1
    return {
        "divisions": divisions,
        "selected": len(seeds),
        "materialised": created,
        "country": country.upper(),
        "note": (
            "Divisions come from the directives' industries. No industry named means the "
            "whole universe, not none of it."
        ),
    }
