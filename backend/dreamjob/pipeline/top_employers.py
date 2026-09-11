"""Prioritising collection towards notable employers (FR-162, FR-164).

A search that treats every company in the shared knowledge base as equally
worth collecting spends its budget alphabetically: it reads the careers page of
a random Dutch bicycle shop before it reads the one of the largest technology
employer in Brussels.  The knowledge base is shared and global, so the planner
needs to know which companies to look at *first*.

This module supplies that ordering from a curated list of notable employers
(:mod:`dreamjob.pipeline.data.top_employers`).  Three deliberate choices:

* **Curated and cited, not scraped.**  The rankings that matter most for an IT
  search - LinkedIn Top Companies, Glassdoor Best Places to Work - forbid
  automated access, and a scraped list would also be unfalsifiable.  Every
  entry names the ranking it came from.
* **A hint, never a fact.**  A name here only reorders work and can seed a
  company row with a name and a domain.  It is never written into a directive
  set, never merged into a profile, and never presented as researched - the
  company is still profiled from its own website like any other (FR-221).
* **Honest about coverage.**  Only the countries in the file get a ranking;
  everywhere else keeps the existing recency order rather than being sorted by
  a list that does not cover it.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DATA_FILE = Path(__file__).parent / "data" / "top_employers.json"


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A missing or malformed list must never break planning; it only means
        # there is no priority to apply.
        log.warning("Top-employer list unavailable at %s; planning without it", DATA_FILE)
        return {}


def _entries(country: str | None) -> list[dict]:
    data = _load()
    by_country = data.get("countries") or {}
    code = (country or "").strip().upper()
    entries = list(by_country.get(code) or [])
    # Remote-friendly employers are relevant to a seeker who accepts remote work
    # wherever they are, so they join every country's list at the tail.
    entries += list(data.get("remote_friendly") or [])
    return entries


def rank_index(countries: list[str] | None) -> dict[str, int]:
    """A name/domain -> priority map for the campaign's target countries.

    Lower is more notable.  Combined across the target countries, keeping the
    best rank a company has in any of them.
    """
    ranks: dict[str, int] = {}
    for country in countries or []:
        for entry in _entries(country):
            rank = int(entry.get("rank") or 999)
            for key in _keys(entry):
                if key and (key not in ranks or rank < ranks[key]):
                    ranks[key] = rank
    return ranks


def _keys(entry: dict) -> list[str]:
    name = str(entry.get("name") or "").strip().lower()
    domain = str(entry.get("domain") or "").strip().lower()
    keys = [name]
    if domain:
        keys.append(domain)
        # "boards.greenhouse.io/acme" and "acme.com" name the same company, so
        # the registrable label is a key too.
        keys.append(domain.split("/")[0].removeprefix("www.").split(".")[0])
    return [k for k in keys if k]


def priority_of(company: dict, ranks: dict[str, int]) -> int | None:
    """This company's rank in the curated lists, or ``None`` if not listed."""
    for key in _keys(company):
        if key in ranks:
            return ranks[key]
    return None


def prioritise(companies: list[dict], countries: list[str] | None) -> list[dict]:
    """Sort a company list so notable employers are planned first.

    A stable sort: everything not in the list keeps its incoming order, so the
    planner's existing recency ordering is preserved underneath the priorities
    rather than replaced by it.
    """
    ranks = rank_index(countries)
    if not ranks:
        return companies
    listed = [c for c in companies if priority_of(c, ranks) is not None]
    rest = [c for c in companies if priority_of(c, ranks) is None]
    listed.sort(key=lambda c: priority_of(c, ranks) or 999)
    if listed:
        log.info("Prioritised %d notable employer(s) ahead of %d others", len(listed), len(rest))
    return listed + rest


def seeds(countries: list[str] | None, limit: int = 25) -> list[dict]:
    """Company seeds from the list, for companies the knowledge base lacks.

    Returns name/domain/source rows the caller may add as collection targets.
    They carry no ``id``, so they are discovery targets, not knowledge-base
    records - the website crawler establishes the record as it does for any
    company (FR-221).
    """
    out: list[dict] = []
    seen: set[str] = set()
    for country in countries or []:
        for entry in _entries(country):
            name = str(entry.get("name") or "").strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            out.append(
                {
                    "name": name,
                    "domain": entry.get("domain"),
                    "country": country.upper(),
                    "top_employer_source": entry.get("source"),
                }
            )
            if len(out) >= limit:
                return out
    return out
