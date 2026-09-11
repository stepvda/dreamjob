"""Canonicalise the values sources supply for seniority and function (FR-261, FR-107).

The ATS vendors each ship their own vocabulary.  Personio says ``experienced``,
``entry-level``, ``student``; Recruitee says ``mid_level``, ``entry_level``,
``student_college``; Teamtailor says nothing at all.  Those strings were stored
and used as if they were the FR-261 field, so ``experienced`` - 13,533 rows, a
career stage the ranking and directive models have never heard of - never
matched a ``senior`` directive, and two spellings of entry level counted as two
levels (CR-405).

This module maps what a source said onto the canonical vocabulary, and nothing
else.  A value it does not recognise returns ``None`` so the caller can fall
back to reading the advertisement text (:func:`opportunities.infer_seniority` /
:func:`infer_function_family`), which is the same path a source that stated
nothing always took.
"""

from __future__ import annotations

import re
from typing import Any

from dreamjob.pipeline.directives import Seniority


def _key(value: Any) -> str:
    """Fold a source value to a comparable key: lowercase, no separators."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


#: Source vocabulary -> canonical FR-261 seniority.  Keys are :func:`_key`ed.
#: ``experienced`` maps to ``medior`` deliberately: it means "not entry level"
#: and is the vendor's broadest non-junior bucket, so reading it as senior
#: would overstate every such posting in a seniority filter.
_SENIORITY_ALIASES: dict[str, Seniority] = {
    "intern": Seniority.INTERN,
    "internship": Seniority.INTERN,
    "stagiair": Seniority.INTERN,
    "stagiaire": Seniority.INTERN,
    "student": Seniority.INTERN,
    "studentcollege": Seniority.INTERN,
    "studentschool": Seniority.INTERN,
    "workingstudent": Seniority.INTERN,
    "junior": Seniority.JUNIOR,
    "jr": Seniority.JUNIOR,
    "entry": Seniority.JUNIOR,
    "entrylevel": Seniority.JUNIOR,
    "graduate": Seniority.JUNIOR,
    "starter": Seniority.JUNIOR,
    "trainee": Seniority.JUNIOR,
    "apprentice": Seniority.JUNIOR,
    "medior": Seniority.MEDIOR,
    "mid": Seniority.MEDIOR,
    "midlevel": Seniority.MEDIOR,
    "experienced": Seniority.MEDIOR,
    "experience": Seniority.MEDIOR,
    "professional": Seniority.MEDIOR,
    "regular": Seniority.MEDIOR,
    "senior": Seniority.SENIOR,
    "sr": Seniority.SENIOR,
    "ervaren": Seniority.SENIOR,
    # `_key` drops the accent, so the source value "confirmé" keys as "confirm".
    "confirm": Seniority.SENIOR,
    "lead": Seniority.LEAD,
    "teamlead": Seniority.LEAD,
    "leidinggevende": Seniority.LEAD,
    "principal": Seniority.PRINCIPAL,
    "staff": Seniority.PRINCIPAL,
    "expert": Seniority.PRINCIPAL,
    "manager": Seniority.MANAGER,
    "management": Seniority.MANAGER,
    "seniormanager": Seniority.DIRECTOR,
    "head": Seniority.DIRECTOR,
    "headoff": Seniority.DIRECTOR,
    "director": Seniority.DIRECTOR,
    "directeur": Seniority.DIRECTOR,
    "executive": Seniority.DIRECTOR,
    "seniorexecutive": Seniority.VP,
    "vp": Seniority.VP,
    "vicepresident": Seniority.VP,
    "clevel": Seniority.C_LEVEL,
    "csuite": Seniority.C_LEVEL,
    "chief": Seniority.C_LEVEL,
    "ceo": Seniority.C_LEVEL,
    "cto": Seniority.C_LEVEL,
    "cfo": Seniority.C_LEVEL,
    "board": Seniority.BOARD,
    "boardmember": Seniority.BOARD,
}

#: Source vocabulary -> canonical function family.  The canonical labels are
#: the ones :data:`opportunities._FUNCTION_FAMILY_PATTERNS` already emits.
_FUNCTION_ALIASES: dict[str, str] = {
    "engineering": "software engineering",
    "software": "software engineering",
    "softwareengineering": "software engineering",
    "technology": "software engineering",
    "tech": "software engineering",
    "development": "software engineering",
    "dev": "software engineering",
    "it": "it operations",
    "informationtechnology": "it operations",
    "itoperations": "it operations",
    "infrastructure": "it operations",
    "data": "data & analytics",
    "analytics": "data & analytics",
    "dataanalytics": "data & analytics",
    "businessintelligence": "data & analytics",
    "machinelearning": "data & analytics",
    "product": "product",
    "productmanagement": "product",
    "security": "information security",
    "informationsecurity": "information security",
    "cyber": "information security",
    "infosec": "information security",
    "finance": "finance",
    "accounting": "finance",
    "financial": "finance",
    "sales": "sales & business development",
    "commercial": "sales & business development",
    "businessdevelopment": "sales & business development",
    "marketing": "marketing & communications",
    "communications": "marketing & communications",
    "marketingcommunications": "marketing & communications",
    "brand": "marketing & communications",
    "hr": "human resources",
    "humanresources": "human resources",
    "people": "human resources",
    "talent": "human resources",
    "recruitment": "human resources",
    "operations": "operations & supply chain",
    "supplychain": "operations & supply chain",
    "logistics": "operations & supply chain",
    "procurement": "operations & supply chain",
    "manufacturing": "engineering & manufacturing",
    "production": "engineering & manufacturing",
    "quality": "engineering & manufacturing",
    "consulting": "consulting & advisory",
    "advisory": "consulting & advisory",
    "legal": "legal & compliance",
    "compliance": "legal & compliance",
    "customersuccess": "customer success & support",
    "customersupport": "customer success & support",
    "customerservice": "customer success & support",
    "support": "customer success & support",
    "generalmanagement": "general management",
    "management": "general management",
}


def canonical_seniority(value: Any) -> str | None:
    """The canonical seniority for a source's own label, or ``None`` if unknown."""
    level = _SENIORITY_ALIASES.get(_key(value))
    return level.value if level is not None else None


def canonical_function_family(value: Any) -> str | None:
    """The canonical function family for a source's own label, or ``None``."""
    return _FUNCTION_ALIASES.get(_key(value))


def backfill_vacancies(
    *, limit: int | None = None, apply: bool = True, infer_missing: bool = True
) -> dict[str, Any]:
    """Rewrite stored vacancies' seniority/function with canonical values.

    Existing rows were written before the mapping existed, so the corpus still
    carries ``experienced`` and ``mid_level``.  With ``infer_missing`` the rows
    that stated nothing at all - Teamtailor sets no seniority, 28,381 vacancies
    - are read from their title and description, exactly as synthesis does, so
    the corpus is coherent rather than half-labelled.  It updates both the
    vacancy and the opportunity rows that mirror it.
    """
    from dreamjob.db.connection import execute, query_all, update_row  # noqa: PLC0415

    infer = None
    if infer_missing:
        from dreamjob.pipeline import opportunities as opp  # noqa: PLC0415

        infer = (opp.infer_function_family, opp.infer_seniority)

    rows = query_all(
        "SELECT id, title, description, function_family, seniority FROM vacancy"
        + (f" LIMIT {int(limit)}" if limit else "")
    )
    changed = {"vacancies": 0, "opportunities": 0}
    for row in rows:
        family = canonical_function_family(row.get("function_family"))
        level = canonical_seniority(row.get("seniority"))
        if infer is not None and (family is None or level is None):
            head = (row.get("description") or "")[:1500]
            if family is None:
                family = infer[0](row.get("title"), head)
            if level is None:
                level = infer[1](row.get("title"), head)
        values: dict[str, Any] = {}
        if family and family != row.get("function_family"):
            values["function_family"] = family
        if level and level != row.get("seniority"):
            values["seniority"] = level
        if not values:
            continue
        # A dry run reports the same count it would write.
        changed["vacancies"] += 1
        if apply:
            update_row("vacancy", row["id"], values)

    if apply:
        # Mirror onto the opportunities in one set-based statement.  The
        # per-vacancy lookup this replaces was quadratic - 47,000 vacancies
        # each scanning 48,000 opportunities - and would not have finished.
        changed["opportunities"] = execute(
            "UPDATE opportunity SET "
            "  function_family = "
            "    (SELECT v.function_family FROM vacancy v WHERE v.id = opportunity.vacancy_id), "
            "  seniority = "
            "    (SELECT v.seniority FROM vacancy v WHERE v.id = opportunity.vacancy_id) "
            "WHERE vacancy_id IS NOT NULL AND ("
            "  ("
            "    (SELECT v.function_family FROM vacancy v WHERE v.id = opportunity.vacancy_id) "
            "      IS NOT NULL AND IFNULL(function_family, '') <> "
            "    (SELECT v.function_family FROM vacancy v WHERE v.id = opportunity.vacancy_id)"
            "  ) OR ("
            "    (SELECT v.seniority FROM vacancy v WHERE v.id = opportunity.vacancy_id) "
            "      IS NOT NULL AND IFNULL(seniority, '') <> "
            "    (SELECT v.seniority FROM vacancy v WHERE v.id = opportunity.vacancy_id)"
            "  )"
            ")"
        )
    return changed
