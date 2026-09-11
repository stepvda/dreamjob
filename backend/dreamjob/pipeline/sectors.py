"""Directive vocabulary to NACE, so "IT companies in Flanders" is a query (FR-143).

A company register classifies by NACE activity code, not by the words a job
seeker uses.  Without a mapping between the two, a directive that says
"information technology" can only be matched against a company's *name*, which
finds the companies that called themselves something obvious and misses the
rest.

The mapping is deliberately coarse: it maps to NACE **divisions** (the first two
digits), because that is the level the register is reliable at.  A company's
self-declared NACE code is a good filter and a poor fact - a firm that pivoted
five years ago still carries the old one - so this narrows a universe of two
million to a few thousand, and the enrichment does the rest.

Belgium's register uses NACE-BEL, which shares its divisions with NACE Rev. 2.
"""

from __future__ import annotations

import re
from typing import Any

from dreamjob.db.connection import from_json

#: The divisions that make up each area a directive can name.  Kept to what a
#: job seeker would recognise; the register has 88 divisions and most of them
#: are mining and forestry.
SECTOR_DIVISIONS: dict[str, tuple[str, ...]] = {
    # --- technology -------------------------------------------------------
    "information technology": ("62", "63", "58", "61", "95"),
    "software": ("62", "58"),
    "software engineering": ("62", "58"),
    "data & analytics": ("62", "63"),
    "data engineering": ("62", "63"),
    "artificial intelligence": ("62", "63", "72"),
    "cybersecurity": ("62", "63"),
    "cloud": ("62", "63", "61"),
    "telecommunications": ("61",),
    "hardware": ("26", "46.5", "95"),
    "electronics": ("26", "27"),
    "robotics": ("26", "28", "62"),
    "gaming": ("58", "62"),
    "e-commerce": ("47", "62", "63"),
    "marketplace": ("47", "62", "63"),
    # --- professional services -------------------------------------------
    "consulting": ("62", "70", "73", "74"),
    "professional services": ("69", "70", "71", "73", "74"),
    "legal": ("69",),
    "accounting": ("69",),
    "architecture & engineering": ("71",),
    "advertising & marketing": ("73",),
    "design": ("62", "74"),
    "hr technology": ("62", "78"),
    "staffing & recruitment": ("78",),
    "research & development": ("72",),
    "education": ("85",),
    "healthcare": ("86", "87", "88", "72"),
    "life sciences": ("72", "21"),
    "pharmaceutical": ("21",),
    # --- industry ---------------------------------------------------------
    "manufacturing": ("10", "13", "14", "17", "20", "22", "23", "24", "25", "26", "27", "28", "29", "30", "31", "32", "33"),
    "logistics & transport": ("49", "50", "51", "52", "53"),
    "energy & utilities": ("35", "36", "37", "38", "39"),
    "climate & sustainability": ("35", "38", "39", "71"),
    "construction": ("41", "42", "43"),
    "food & beverage": ("10", "11", "12", "56"),
    "retail": ("47",),
    "agriculture": ("01", "02", "03"),
    # --- services and public ---------------------------------------------
    "financial services": ("64", "65", "66"),
    "banking": ("64",),
    "insurance": ("65",),
    "fintech": ("64", "62", "63"),
    "public sector": ("84",),
    "government": ("84",),
    "non-profit": ("85", "86", "87", "88", "91", "94"),
    "media & publishing": ("58", "59", "60", "90"),
    "hospitality & tourism": ("55", "56", "79"),
}

#: Folded forms, so a directive can name a sector however it likes.
_ALIASES: dict[str, str] = {
    "it": "information technology",
    "ict": "information technology",
    "tech": "information technology",
    "technology": "information technology",
    "software development": "software",
    "developer tools": "software",
    "saas": "software",
    "data science": "data & analytics",
    "analytics": "data & analytics",
    "machine learning": "artificial intelligence",
    "ai": "artificial intelligence",
    "security": "cybersecurity",
    "infosec": "cybersecurity",
    "telecom": "telecommunications",
    "engineering": "architecture & engineering",
    "consultancy": "consulting",
    "marketing": "advertising & marketing",
    "hr": "hr technology",
    "recruitment": "staffing & recruitment",
    "r&d": "research & development",
    "health": "healthcare",
    "medtech": "healthcare",
    "biotech": "life sciences",
    "pharma": "pharmaceutical",
    "logistics": "logistics & transport",
    "transport": "logistics & transport",
    "energy": "energy & utilities",
    "climate": "climate & sustainability",
    "sustainability": "climate & sustainability",
    "finance": "financial services",
    "bank": "banking",
    "public": "public sector",
    "ngo": "non-profit",
    "media": "media & publishing",
    "publishing": "media & publishing",
    "hospitality": "hospitality & tourism",
    "tourism": "hospitality & tourism",
    "retail & e-commerce": "e-commerce",
    "e commerce": "e-commerce",
}

_NORMALISE_RE = re.compile(r"[^a-z0-9&+ ]+")


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", _NORMALISE_RE.sub(" ", str(text or "").lower())).strip()


def divisions_for(label: str) -> tuple[str, ...]:
    """The NACE divisions a directive label maps to, or none if unknown."""
    folded = _fold(label)
    if not folded:
        return ()
    key = _ALIASES.get(folded, folded)
    if key in SECTOR_DIVISIONS:
        return SECTOR_DIVISIONS[key]
    # A partial match, so "information technology services" still resolves.
    for name, divisions in SECTOR_DIVISIONS.items():
        if name in folded or folded in name:
            return divisions
    return ()


def directive_divisions(directives: Any) -> list[str]:
    """Every division the directives' industries point at (FR-142, FR-143).

    Reads ``job_content.industries_include`` - the field the directive editor
    already fills - and folds the labels through the alias table.  An empty
    result means "no sector constraint", not "no companies": a seeker who named
    no industry gets the whole universe rather than an empty one.
    """
    if directives is None:
        return []
    content = getattr(directives, "job_content", None)
    if content is None and isinstance(directives, dict):
        content = from_json(directives.get("job_content"), {}) or {}
    labels: list[str] = []
    if isinstance(content, dict):
        labels = list(content.get("industries_include") or [])
    elif content is not None:
        labels = list(getattr(content, "industries_include", []) or [])

    out: list[str] = []
    for label in labels:
        for division in divisions_for(label if isinstance(label, str) else str(label)):
            if division not in out:
                out.append(division)
    return out


def provisional() -> dict[str, Any]:
    """The mapping itself, for the administration screen to show (FR-362)."""
    return {
        "sectors": {name: list(divisions) for name, divisions in SECTOR_DIVISIONS.items()},
        "aliases": dict(_ALIASES),
    }
