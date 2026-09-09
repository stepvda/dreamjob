#!/usr/bin/env python3
"""Generate the end-to-end test persona (FR-102, FR-103, FR-109, FR-364).

The end-to-end test is only worth running if it exercises the real intake
path, so the fixture it starts from is not a hand-written dict: it is an
invented AI developer whose career is produced by the product's own
``LLMClient`` and then held to the same coherence rules a real profile would
satisfy.  Two calls are made, both under task ids the product already knows,
so the generation shows up in the FR-364 AI call log like any other work:

* ``profile.composite`` - identity, career, education, skills, publications
  and open-source projects.
* ``profile.dreamjob``  - the first-person dream-job statement (FR-109) that
  ``dream_job_model.md`` is later asked to structure.

Nothing the model returns is trusted.  ``validate_career`` re-derives the
timeline, the seniority progression and the provenance of every skill, and
``validate_dream_job`` checks that each deal-breaker is quoted out of the
statement it came from; a rejected answer goes back to the model with the list
of problems until it passes or the attempts run out.  The dates, the durations and the two planted conflicts
are then computed here in Python, never by the model, because the test asserts
on them.

FR-103 conflict detection is the reason two documents exist at all, so two
disagreements are planted deliberately - one date, one employer name - and
recorded under ``planted_conflicts`` with the field path the merge is expected
to raise.  Both are shaped to survive ``profile_intake._match_experience``:
the two readings of a role still have to be recognised as the *same* role
before their difference can be reported, which is why the employer
disagreement is a legal-entity suffix rather than an unrelated name.

    python3 tests/e2e/persona/generate.py             # keep an existing persona
    python3 tests/e2e/persona/generate.py --force     # regenerate from scratch
    python3 tests/e2e/persona/generate.py --force --strong   # reasoning model

The cheap model writes the persona by default. The reasoning model spends its
whole ``max_tokens`` budget on the chain of thought before it reaches an object
this size and returns nothing, so ``--strong`` also doubles the budget.

The result is written to ``tests/e2e/persona/out/persona.json``; ``render.py``
turns it into the CV and the LinkedIn export.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from dreamjob.llm.client import LLMClient, LLMError  # noqa: E402
from dreamjob.pipeline.profile_intake import company_variants  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "out"
PERSONA_PATH = OUT_DIR / "persona.json"

MIN_ROLES, MAX_ROLES = 4, 6
MIN_CAREER_YEARS, MAX_CAREER_YEARS = 8, 12
MIN_SKILLS, MAX_SKILLS = 20, 30
STATEMENT_MIN_WORDS, STATEMENT_MAX_WORDS = 150, 250

_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

# The merge strips these words before comparing employers, so an employer whose
# own name contains one would normalise down to a stub and could be paired with
# an unrelated company (profile_intake._LEGAL_SUFFIXES).
_SUFFIX_WORDS = {
    "nv", "sa", "bv", "bvba", "sprl", "gmbh", "ag", "ltd", "limited", "llc",
    "inc", "plc", "srl", "oy", "ab", "as", "aps", "kft", "spa", "pte", "pty",
    "corp", "corporation", "company", "group", "holding", "solutions",
    "consulting", "services", "international",
}

# A persona that borrowed a real employer would make the fixture look like
# scraped data rather than fiction; these are the names a model reaches for.
_REAL_EMPLOYERS = {
    "google", "alphabet", "microsoft", "amazon", "aws", "meta", "facebook",
    "apple", "ibm", "oracle", "sap", "accenture", "deloitte", "capgemini",
    "kpmg", "pwc", "ey", "infosys", "cognizant", "wipro", "tcs", "nvidia",
    "openai", "anthropic", "deepmind", "huggingface", "databricks", "snowflake",
    "spotify", "booking", "adyen", "asml", "philips", "ing", "kbc", "bnp",
    "proximus", "colruyt", "delhaize", "ahold", "shell", "unilever", "bosch",
    "siemens", "atos", "sopra", "nga", "alight", "fujitsu", "becode",
}

_SENIORITY_RANKS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (0, ("intern", "internship", "trainee", "student", "apprentice")),
    (1, ("junior", "graduate", "associate", "assistant")),
    (2, ("engineer", "developer", "scientist", "analyst", "consultant", "researcher")),
    (3, ("senior", "sr.")),
    (4, ("lead", "staff", "principal", "architect")),
    (5, ("head", "manager", "director", "vp", "chief")),
)

_AI_TOKENS = (
    "ai", "ml", "machine learning", "artificial intelligence", "deep learning",
    "llm", "nlp", "mlops", "data science",
)

_CAREER_SYSTEM = """\
You invent test data for a job-search product. Everything you return is
fiction: the person, the employers, the products, the publications. It has to
read like a real career and survive an automated coherence check, but it must
never borrow a real company, a real person or a deliverable domain name.

Hard rules:

1. Return one JSON object and nothing else.
2. Dates are "YYYY-MM". Roles never overlap: each role starts strictly after
   the previous one ends. The most recent role is the only current one.
3. Seniority never goes backwards.
4. "skills" holds between {MIN_SKILLS} and {MAX_SKILLS} entries and
   "top_skills" is a subset of it - the top skills are five names copied out of
   "skills", not a summary of them.
5. Build "skills" last, by copying: every entry must already appear, character
   for character, in the "technologies" list of at least one role, or in a
   certification title, or in an education "field". A role's "technologies" is
   everything the role actually ran on - languages, frameworks, platforms,
   practices, ways of working - so put the soft and methodological skills
   there first and only then copy them across. Nothing floats free.
6. Employer names carry no legal form and no generic corporate word: no NV,
   BV, GmbH, Ltd, Group, Solutions, Consulting, Services, International.
7. No health data, political opinion, religion, trade-union membership,
   sexual orientation or ethnicity anywhere in the output.
"""

_CAREER_SCHEMA = """\
{"identity": {"first_name": str, "last_name": str, "city": str,
  "region": str, "country": "Belgium"|"Netherlands", "headline": str},
 "summary": str,
 "experience": [{"employer": str, "title": str, "location": str,
   "start": "YYYY-MM", "end": "YYYY-MM"|null, "current": bool,
   "summary": str, "achievements": [str], "technologies": [str]}],
 "education": [{"school": str, "degree": str, "field": str, "location": str,
   "start_year": int, "end_year": int}],
 "certifications": [{"title": str, "issuer": str, "year": int}],
 "languages": [{"language": str, "proficiency": str}],
 "skills": [str], "top_skills": [str],
 "skill_groups": [{"label": str, "skills": [str]}],
 "publications": [{"title": str, "venue": str, "year": int,
   "kind": "paper"|"talk"}],
 "projects": [{"name": str, "url": str, "description": str,
   "technologies": [str]}]}
"""

_DREAM_SYSTEM = """\
You write in the first person as one specific job seeker, in their voice, from
the career you are given. This is the free-text answer to "describe the job you
actually want" - not a cover letter, not a CV summary. It is allowed to be
opinionated and a little uneven, the way people write about themselves.

Return one JSON object and nothing else. Every "quote" must be copied
character for character out of "statement".
"""

_DREAM_SCHEMA = """\
{"statement": str,
 "deal_breakers": [{"constraint": str, "quote": str, "hard": bool}],
 "target_roles": [str],
 "must_haves": [str]}
"""


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def month_index(value: str) -> int:
    """``"2019-03"`` -> a comparable month number."""
    year, month = value.split("-")
    return int(year) * 12 + int(month) - 1


def month_value(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_label(value: str) -> str:
    """``"2019-03"`` -> ``"March 2019"``, the form both documents print."""
    year, month = value.split("-")
    return f"{_MONTH_NAMES[int(month) - 1]} {int(year)}"


def duration_phrase(start: str, end: str) -> str:
    """LinkedIn's inclusive tenure wording: ``"2 years 6 months"``."""
    months = month_index(end) - month_index(start) + 1
    years, rest = divmod(max(months, 1), 12)
    parts = []
    if years:
        parts.append(f"{years} year" + ("s" if years != 1 else ""))
    if rest or not years:
        parts.append(f"{rest} month" + ("s" if rest != 1 else ""))
    return " ".join(parts)


def _today_month() -> str:
    today = date.today()
    return f"{today.year:04d}-{today.month:02d}"


# ---------------------------------------------------------------------------
# Derived identity
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", ascii_text.lower())


def _derive_identity(identity: dict[str, Any]) -> dict[str, Any]:
    """Fill in the contact details both documents must state identically.

    The merge compares name, email, phone and location across the two sources
    (profile_intake._CONTACT_COMPARED), so any difference here would show up as
    a third conflict and blur the two the test is actually about.  They are
    derived rather than generated for exactly that reason - and the address
    space is the reserved one from RFC 2606, because a fixture must not be
    able to send mail to anybody.
    """
    first, last = identity["first_name"].strip(), identity["last_name"].strip()
    handle = f"{_slug(first)}{_slug(last)}"[:20]
    digits = "".join(str(b % 10) for b in hashlib.sha256(handle.encode()).digest()[:8])
    a, b, c, e, f, g, h, i = digits
    if identity["country"] == "Belgium":
        phone = f"+32 4{a}{b} {c}{e} {f}{g} {h}{i}"
    else:
        phone = f"+31 6 {a}{b}{c}{e} {f}{g}{h}{i}"
    return {
        **identity,
        "name": f"{first} {last}",
        "email": f"{_slug(first)}.{_slug(last)}@example.com",
        "phone": phone,
        "linkedin_url": f"www.linkedin.com/in/{handle}",
        "website": f"{handle}.example.com",
        "location_full": f"{identity['city']}, {identity['region']}, {identity['country']}",
        "location_short": f"{identity['city']}, {identity['country']}",
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _seniority_rank(title: str) -> int:
    """Rough seniority of a job title, so a career cannot go backwards.

    An unmarked title is an individual contributor; a promotion word outranks
    everything else in the title, and a junior word only counts when no
    promotion word is present ("Junior" and "Engineer" both match).
    """
    text = f" {re.sub(r'[^a-z. ]+', ' ', title.lower())} "
    matched = [
        value for value, tokens in _SENIORITY_RANKS if any(f" {t} " in text for t in tokens)
    ]
    senior = [value for value in matched if value >= 3]
    if senior:
        return max(senior)
    junior = [value for value in matched if value <= 1]
    return min(junior) if junior else 2


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def _is_month(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"\d{4}-\d{2}", value))


def _is_ai_role(title: str) -> bool:
    text = f" {re.sub(r'[^a-z ]+', ' ', title.lower())} "
    return any(f" {token} " in text for token in _AI_TOKENS)


def validate_career(data: Any, now: str) -> list[str]:
    """Every coherence rule the persona has to satisfy, as plain problems.

    ``data`` is whatever the model returned, which is not always an object;
    a wrong shape is reported as a problem so the repair loop can fix it
    rather than raising out of the generator.
    """
    if not isinstance(data, dict):
        return [f"the answer must be one JSON object, not a {type(data).__name__}"]
    problems: list[str] = []

    identity = data.get("identity") or {}
    if not isinstance(identity, dict):
        return ["identity must be one JSON object"]
    for key in ("first_name", "last_name", "city", "region", "country", "headline"):
        if not str(identity.get(key) or "").strip():
            problems.append(f"identity.{key} is missing")
    if identity.get("country") not in {"Belgium", "Netherlands"}:
        problems.append("identity.country must be Belgium or the Netherlands")

    roles = [role for role in (data.get("experience") or []) if isinstance(role, dict)]
    if not MIN_ROLES <= len(roles) <= MAX_ROLES:
        problems.append(f"experience must hold {MIN_ROLES}-{MAX_ROLES} roles, got {len(roles)}")
    previous_end: int | None = None
    previous_rank = -1
    previous_title = ""
    for i, role in enumerate(roles):
        where = f"experience[{i}] ({role.get('title')} at {role.get('employer')})"
        start, end = role.get("start"), role.get("end")
        if not isinstance(start, str) or not re.fullmatch(r"\d{4}-\d{2}", start):
            problems.append(f"{where}: start must be YYYY-MM, got {start!r}")
            continue
        is_last = i == len(roles) - 1
        if is_last and end not in (None, ""):
            problems.append(f"{where}: the most recent role must be current, with end null")
        if not is_last:
            if not isinstance(end, str) or not re.fullmatch(r"\d{4}-\d{2}", end):
                problems.append(f"{where}: end must be YYYY-MM, got {end!r}")
                continue
            if month_index(end) < month_index(start):
                problems.append(f"{where}: ends {end} before it starts {start}")
            if month_index(end) - month_index(start) + 1 < 9:
                problems.append(f"{where}: lasts under nine months; lengthen it")
        if previous_end is not None and month_index(start) <= previous_end:
            problems.append(
                f"{where}: starts {start}, which overlaps the role that ends "
                f"{month_value(previous_end)}"
            )
        previous_end = month_index(end) if _is_month(end) else month_index(now)
        rank = _seniority_rank(str(role.get("title") or ""))
        if rank < previous_rank:
            problems.append(
                f"{where}: the title is a step down from {previous_title!r}; "
                "rename it so seniority keeps climbing"
            )
        if rank >= previous_rank:
            previous_title = str(role.get("title") or "")
        previous_rank = max(previous_rank, rank)
        for key in ("employer", "title", "location", "summary"):
            if not str(role.get(key) or "").strip():
                problems.append(f"{where}: {key} is missing")
        if len(role.get("achievements") or []) < 2:
            problems.append(f"{where}: needs at least two specific achievements")
        if len(role.get("technologies") or []) < 3:
            problems.append(f"{where}: needs at least three technologies")
        employer = str(role.get("employer") or "")
        if set(_norm(employer).split()) & _SUFFIX_WORDS:
            problems.append(
                f"{where}: employer {employer!r} carries a legal or generic corporate word"
            )
        if set(_norm(employer).split()) & _REAL_EMPLOYERS:
            problems.append(f"{where}: employer {employer!r} is a real company; invent one")

    if roles and _is_month(roles[0].get("start")):
        span = (month_index(now) - month_index(roles[0]["start"]) + 1) / 12
        if not MIN_CAREER_YEARS <= span <= MAX_CAREER_YEARS:
            problems.append(
                f"the career spans {span:.1f} years, oldest role first; it must be "
                f"{MIN_CAREER_YEARS}-{MAX_CAREER_YEARS}"
            )
    if roles and not _is_ai_role(str(roles[-1].get("title") or "")):
        problems.append("the last role listed must be the current AI/ML engineering one")

    problems.extend(_validate_employers(roles))
    problems.extend(_validate_skills(data))

    education = _dicts(data, "education")
    if not education:
        problems.append("education must hold at least one study")
    for i, study in enumerate(education):
        for key in ("school", "degree", "field", "start_year", "end_year"):
            if not str(study.get(key) or "").strip():
                problems.append(f"education[{i}].{key} is missing")
    if education and roles and _is_month(roles[0].get("start")):
        last_year = max(int(study["end_year"]) for study in education if study.get("end_year"))
        if last_year > int(roles[0]["start"][:4]):
            problems.append(
                f"education ends in {last_year}, after the first role starts "
                f"{roles[0]['start']}"
            )

    if len(data.get("certifications") or []) < 2:
        problems.append("certifications must hold at least two entries")
    if len(data.get("languages") or []) < 2:
        problems.append("languages must hold at least two entries")
    if not 2 <= len(data.get("publications") or []) <= 3:
        problems.append("publications must hold two or three papers or talks")
    if len(data.get("projects") or []) != 2:
        problems.append("projects must hold exactly two open-source projects")
    words = len(str(data.get("summary") or "").split())
    if not 40 <= words <= 160:
        problems.append(f"summary is {words} words; it must be 40-160")
    return problems


def _validate_employers(roles: list[dict[str, Any]]) -> list[str]:
    """At least one promotion, no boomerangs, and room to plant a conflict.

    A promotion is what makes the export exercise LinkedIn's grouped-company
    layout and the merge's multi-role pairing.  Two single-role employers are
    what ``plant_conflicts`` needs, since renaming an employer that holds two
    roles would raise two conflicts instead of one.  Distinctness is what keeps
    ``company_variants`` from pairing two different jobs by accident.
    """
    problems: list[str] = []
    names = [str(r.get("employer") or "") for r in roles]
    counts = {name: sum(1 for other in names if _norm(other) == _norm(name)) for name in names}
    repeats = [name for name in dict.fromkeys(names) if counts[name] > 1]
    if not repeats:
        problems.append(
            "one employer must hold two consecutive roles - a promotion - so the export "
            "carries a company with more than one role under it"
        )
    for name in repeats:
        positions = [i for i, n in enumerate(names) if _norm(n) == _norm(name)]
        if len(positions) != 2 or positions[1] - positions[0] != 1:
            problems.append(
                f"the roles at {name!r} must be two, and consecutive: nobody leaves and "
                "comes back in this career"
            )
    singles = [
        name for i, name in enumerate(names) if counts[name] == 1 and i < len(names) - 1
    ]
    if not singles:
        tally = ", ".join(f"{name!r} x{counts[name]}" for name in dict.fromkeys(names))
        problems.append(
            "one employer other than the current one must hold exactly one role, so the CV "
            "can disagree with the export about its name without touching a second role; "
            f"the employers are {tally} - split one of the repeated ones across two "
            "different companies"
        )
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if _norm(a) != _norm(b) and company_variants(a) & company_variants(b):
                problems.append(f"employers {a!r} and {b!r} are too alike to tell apart")
    return problems


def _dicts(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [item for item in (data.get(key) or []) if isinstance(item, dict)]


def _validate_skills(data: dict[str, Any]) -> list[str]:
    """The skills block: traceable to the career, and grouped without loss."""
    problems: list[str] = []
    skills = [str(item) for item in data.get("skills") or []]
    if not MIN_SKILLS <= len(skills) <= MAX_SKILLS:
        problems.append(f"skills must hold {MIN_SKILLS}-{MAX_SKILLS} entries, got {len(skills)}")
    if len({_norm(item) for item in skills}) != len(skills):
        problems.append("skills contains duplicates")

    # A skill nobody used is a claim, not a fact; the career is the only source.
    evidence: set[str] = set()
    for parent in ("experience", "projects"):
        evidence |= {
            _norm(item)
            for entry in _dicts(data, parent)
            for item in entry.get("technologies") or []
        }
    evidence |= {_norm(entry.get("title")) for entry in _dicts(data, "certifications")}
    evidence |= {_norm(entry.get("field")) for entry in _dicts(data, "education")}
    for skill in skills:
        if _norm(skill) not in evidence:
            problems.append(
                f"skill {skill!r} appears in no role, certification or study; "
                "add it to a role's technologies or drop it"
            )

    groups = _dicts(data, "skill_groups")
    if not 3 <= len(groups) <= 8:
        problems.append(f"skill_groups must hold 3-8 families, got {len(groups)}")
    grouped: list[str] = []
    for i, group in enumerate(groups):
        if not str(group.get("label") or "").strip():
            problems.append(f"skill_groups[{i}].label is missing")
        if not group.get("skills"):
            problems.append(f"skill_groups[{i}] ({group.get('label')!r}) holds no skills")
        grouped.extend(str(item) for item in group.get("skills") or [])
    missing = [item for item in skills if _norm(item) not in {_norm(g) for g in grouped}]
    extra = [item for item in grouped if _norm(item) not in {_norm(k) for k in skills}]
    if missing:
        problems.append(
            "skill_groups leaves these skills ungrouped: " + ", ".join(repr(m) for m in missing)
        )
    if extra:
        problems.append(
            "skill_groups names skills that are not in skills: "
            + ", ".join(repr(e) for e in extra)
        )
    seen = Counter(_norm(item) for item in grouped)
    twice = sorted({item for item in grouped if seen[_norm(item)] > 1})
    if twice:
        problems.append(
            "these skills appear in more than one skill_groups family: "
            + ", ".join(repr(item) for item in twice)
        )

    top = [str(item) for item in data.get("top_skills") or []]
    if not 3 <= len(top) <= 5:
        problems.append(f"top_skills must hold 3-5 entries, got {len(top)}")
    known = {_norm(item) for item in skills}
    for skill in top:
        if _norm(skill) not in known:
            problems.append(f"top skill {skill!r} is not in skills")
    return problems


def validate_dream_job(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return [f"the answer must be one JSON object, not a {type(data).__name__}"]
    problems: list[str] = []
    statement = str(data.get("statement") or "").strip()
    words = len(statement.split())
    if not STATEMENT_MIN_WORDS <= words <= STATEMENT_MAX_WORDS:
        problems.append(
            f"statement is {words} words; it must be "
            f"{STATEMENT_MIN_WORDS}-{STATEMENT_MAX_WORDS}"
        )
    if not re.search(r"\bI\b", statement):
        problems.append("statement must be written in the first person")
    breakers = _dicts(data, "deal_breakers")
    if not 2 <= len(breakers) <= 3:
        problems.append(f"deal_breakers must hold two or three entries, got {len(breakers)}")
    haystack = _norm(statement)
    for i, breaker in enumerate(breakers):
        quote = str(breaker.get("quote") or "").strip()
        if not str(breaker.get("constraint") or "").strip():
            problems.append(f"deal_breakers[{i}].constraint is missing")
        if not quote:
            problems.append(f"deal_breakers[{i}].quote is missing")
        elif _norm(quote) not in haystack:
            problems.append(
                f"deal_breakers[{i}].quote is not copied from the statement: {quote!r}"
            )
    if not data.get("target_roles"):
        problems.append("target_roles must name at least one job title")
    return problems


# ---------------------------------------------------------------------------
# Planted conflicts (FR-103)
# ---------------------------------------------------------------------------


def plant_conflicts(persona: dict[str, Any]) -> list[dict[str, Any]]:
    """Make the CV disagree with the export twice, on purpose.

    ``experience`` is already newest-first, which is the order the merge
    presents roles in, so the index used here is the index the conflict rows
    will carry.  Both disagreements are chosen so the two readings of the role
    still pair: the dates move by three months rather than years, and the
    employer picks up the legal form the merge strips before comparing.
    """
    roles = persona["experience"]
    country = persona["identity"]["country"]
    legal_form = "NV" if country == "Belgium" else "BV"

    # A role that ends the group it belongs to would drag its sibling with it,
    # and the current role has no end date to disagree about.
    employers = [r["employer"] for r in roles]
    singletons = [
        i for i, role in enumerate(roles)
        if not role["current"] and employers.count(role["employer"]) == 1
    ]
    if not singletons:
        raise ValueError("the career needs a single-role employer to rename in the CV")
    # Renaming an employer that holds two roles would raise a conflict on each
    # of them, so the employer plant goes on a company with one role.  The date
    # plant only needs a role that has ended and lasted long enough to shorten.
    employer_index = singletons[0]
    date_index = next(
        (
            i for i in reversed(range(len(roles)))
            if i != employer_index
            and not roles[i]["current"]
            and month_index(roles[i]["end"]) - month_index(roles[i]["start"]) >= 8
        ),
        None,
    )
    if date_index is None:
        raise ValueError("the career needs a second finished role to shift the dates of")

    date_role = roles[date_index]
    # Backwards, never forwards: a CV that ends a role early opens a gap, while
    # one that ends it late would manufacture an overlap with the next role.
    shifted = month_value(month_index(date_role["end"]) - 3)
    if month_index(shifted) < month_index(date_role["start"]):
        raise ValueError(f"role {date_role['title']!r} is too short to shift its end date")
    date_role["cv_end"] = shifted
    date_role["cv_dates"] = f"{month_label(date_role['start'])} – {month_label(shifted)}"

    employer_role = roles[employer_index]
    employer_role["employer_cv"] = f"{employer_role['employer']} {legal_form}"

    planted = [
        {
            "kind": "employer",
            "requirement": "FR-103",
            "expected_field_path": f"experience.{employer_index}.company",
            "role_index": employer_index,
            "anchor": {"employer": employer_role["employer"], "title": employer_role["title"]},
            "value_linkedin": employer_role["employer"],
            "value_cv": employer_role["employer_cv"],
            "why": (
                "The CV states the legal entity, the export states the brand. The merge "
                "still pairs the role because company_variants strips the legal form, so "
                "the disagreement is reported rather than read as two separate jobs."
            ),
        },
        {
            "kind": "date",
            "requirement": "FR-103",
            "expected_field_path": f"experience.{date_index}.end",
            "role_index": date_index,
            "anchor": {"employer": date_role["employer"], "title": date_role["title"]},
            "value_linkedin": date_role["end"],
            "value_cv": date_role["cv_end"],
            "why": (
                "The CV ends the role three months before the export does - the ordinary "
                "drift of a CV rewritten from memory. Both readings are month-precise, so "
                "_same_date reports a real disagreement rather than a rounding gap."
            ),
        },
    ]
    _assert_detectable(planted)
    return planted


def _assert_detectable(planted: list[dict[str, Any]]) -> None:
    """Check the plants against the merge's own rules before writing them out.

    A planted conflict the merge would silently swallow is worse than none at
    all: the test would pass while proving nothing.
    """
    for item in planted:
        li, cv = item["value_linkedin"], item["value_cv"]
        if item["kind"] == "employer":
            if not company_variants(li) & company_variants(cv):
                raise ValueError(
                    f"{li!r} and {cv!r} would not be paired as the same employer; "
                    "the disagreement would be read as two different jobs"
                )
            if li == cv:
                raise ValueError("the employer plant produced identical names")
        else:
            if len(li) != 7 or len(cv) != 7:
                raise ValueError("date plants must be month-precise on both sides")
            if li == cv:
                raise ValueError("the date plant produced identical dates")


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_persona(career: dict[str, Any], dream: dict[str, Any], now: str) -> dict[str, Any]:
    """Turn the two model answers into the fixture the renderer consumes.

    Every string either document prints is computed here, so the renderer holds
    no date arithmetic and the two documents cannot drift apart except where a
    conflict was planted.
    """
    identity = _derive_identity(career["identity"])
    # Newest first: the order both documents print and the order the merge
    # sorts into, so a conflict's index means the same thing everywhere.
    roles = [dict(role) for role in reversed(career["experience"])]

    for role in roles:
        end = role.get("end") or None
        role["end"] = end
        role["current"] = end is None
        role["employer_cv"] = role["employer"]
        role["cv_end"] = end
        role["employer_total_duration"] = None
        closing = end or now
        role["linkedin_dates"] = (
            f"{month_label(role['start'])} - "
            f"{'Present' if end is None else month_label(end)} "
            f"({duration_phrase(role['start'], closing)})"
        )
        role["cv_dates"] = (
            f"{month_label(role['start'])} – "
            f"{'Present' if end is None else month_label(end)}"
        )

    # LinkedIn prints one heading per employer with the total tenure under it
    # when several roles share it; the renderer needs that number, not the maths.
    index = 0
    while index < len(roles):
        span = index + 1
        while span < len(roles) and roles[span]["employer"] == roles[index]["employer"]:
            span += 1
        if span - index > 1:
            group = roles[index:span]
            first_start = min(role["start"] for role in group)
            last_end = group[0]["end"] or now
            roles[index]["employer_total_duration"] = duration_phrase(first_start, last_end)
        index = span

    education = []
    for study in career["education"]:
        education.append(
            {
                **study,
                "linkedin_detail": (
                    f"{study['degree']}, {study['field']} "
                    f"· ({study['start_year']} - {study['end_year']})"
                ),
                "cv_dates": f"{study['start_year']} – {study['end_year']}",
            }
        )

    persona = {
        "schema": "dreamjob.e2e.persona/1",
        "generated_at": date.today().isoformat(),
        "as_of": now,
        "requirements": ["FR-102", "FR-103", "FR-109", "FR-364"],
        "notice": (
            "Entirely fictional test data generated for the Dream Job end-to-end test. "
            "The person, the employers, the publications and the projects do not exist; "
            "the address is in the RFC 2606 reserved domain."
        ),
        "identity": identity,
        "summary": career["summary"].strip(),
        "experience": roles,
        "experience_order": "reverse_chronological",
        "education": education,
        "certifications": career["certifications"],
        "languages": career["languages"],
        "skills": career["skills"],
        "skill_groups": career["skill_groups"],
        "top_skills": career["top_skills"],
        "publications": career["publications"],
        "projects": career["projects"],
        "dream_job": {
            "statement": dream["statement"].strip(),
            "deal_breakers": dream["deal_breakers"],
            "target_roles": dream.get("target_roles") or [],
            "must_haves": dream.get("must_haves") or [],
        },
    }
    persona["planted_conflicts"] = plant_conflicts(persona)
    return persona


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _repair_note(problems: list[str], previous: Any) -> str:
    """Hand the rejected answer back with its problems.

    Sending only the problems makes the model start over, and it reliably
    re-introduces a rule it had already satisfied; sending the object it just
    wrote turns the retry into an edit.
    """
    if not problems:
        return ""
    return (
        "\n\nHere is the answer you gave last time:\n"
        + json.dumps(previous, ensure_ascii=False)
        + "\n\nIt was rejected. Return it again in full, leaving everything else "
        "exactly as it is and changing only what these problems require:\n- "
        + "\n- ".join(problems)
    )


def _career_request(now: str, problems: list[str], previous: Any = None) -> str:
    retry = _repair_note(problems, previous)
    return f"""\
Invent one fictional AI developer and the career behind them. Today is {now}.

* They live in Belgium (preferred) or the Netherlands, in a real city, and the
  headline reads like a LinkedIn headline: role, "at" nothing in particular,
  and what they are known for.
* {MIN_ROLES}-{MAX_ROLES} roles listed oldest first, spanning
  {MIN_CAREER_YEARS}-{MAX_CAREER_YEARS} years and ending, today, in an AI or
  machine-learning engineering role that is still running ("end": null).
  Employers are fictional but plausible for the Benelux technology scene:
  software houses, scale-ups, a research spin-off, an industrial group's
  digital arm.
* One employer appears twice, in two consecutive roles - a promotion - with two
  clearly different titles, not the same words with "Senior" bolted on. Nobody
  leaves an employer and comes back later, and at least two employers other
  than the current one hold exactly one role.
* Every role carries a one-sentence summary, two or three achievements with
  numbers in them, and the technologies actually used.
* Education that finishes before the first role starts, two to four
  certifications, at least two languages with proficiencies, two or three
  publications or conference talks, and exactly two open-source projects.
* {MIN_SKILLS}-{MAX_SKILLS} skills and five top skills, assembled only after
  the roles are written and drawn only from what those roles say they used.
* "skill_groups": the *same list* of skills again, only sorted into three to
  six labelled families the way a CV prints them. Copy each entry of "skills"
  into exactly one family and stop: concatenating the families must give back
  "skills" with nothing added, nothing dropped and nothing renamed. Labels are
  what this person would write, not categories in general.
* The arc has to make sense: someone who starts in data engineering and ends up
  building LLM systems, not someone who does a different job every two years.

Return JSON only.{retry}"""


def _dream_request(persona_context: str, problems: list[str], previous: Any = None) -> str:
    retry = _repair_note(problems, previous)
    return f"""\
Here is the career you are speaking from:

{persona_context}

Write "statement": {STATEMENT_MIN_WORDS}-{STATEMENT_MAX_WORDS} words, first
person, present tense, describing the AI developer role this person actually
wants next. Recognisably the same person as the career above - the same
domains, the same tools, the same opinions about how software should be built.
Say what the work looks like day to day, what kind of team and company it sits
in, and what they want to stop doing.

End with two or three deal-breakers stated plainly in the same voice, then
report them in "deal_breakers" with the exact sentence fragment from the
statement in "quote" and "hard": true when it rules a job out outright.

Return JSON only.{retry}"""


def _context_for_dream(career: dict[str, Any]) -> str:
    lines = [career["identity"]["headline"], ""]
    for role in career["experience"]:
        end = role.get("end") or "present"
        lines.append(f"- {role['title']}, {role['employer']} ({role['start']} to {end})")
        lines.append(f"  {role['summary']}")
    lines.append("")
    lines.append("Skills: " + ", ".join(career["skills"]))
    return "\n".join(lines)


def _unparseable(exc: LLMError) -> str:
    """Turn a bad response into a problem the next attempt can act on.

    A long answer that runs into ``max_tokens`` arrives as an unclosed object,
    so the useful instruction is "write less", not "try again".
    """
    return (
        f"the answer was not usable JSON ({exc}); it was most likely cut off, so write "
        "the same object again with shorter role summaries and fewer words per "
        "achievement"
    )


def _generate(llm: LLMClient, *, strong: bool, attempts: int) -> dict[str, Any]:
    now = _today_month()
    # The reasoning model charges its chain of thought against max_tokens, so a
    # budget that is comfortable for the cheap model leaves it with nothing to
    # answer with.
    career_budget, dream_budget = (16000, 6000) if strong else (8000, 3000)
    problems: list[str] = []
    career: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        print(f"  career    attempt {attempt}/{attempts} ...", flush=True)
        try:
            career = llm.complete_json(
                "profile.composite",
                _CAREER_SYSTEM.format(MIN_SKILLS=MIN_SKILLS, MAX_SKILLS=MAX_SKILLS),
                _career_request(now, problems, career),
                schema_hint=_CAREER_SCHEMA,
                prefer_strong=strong,
                temperature=0.2 + 0.2 * (attempt - 1),
                max_tokens=career_budget,
                entity_type="e2e_persona",
            )
        except LLMError as exc:
            problems = [_unparseable(exc)]
            print(f"    rejected: {problems[0]}")
            continue
        problems = validate_career(career, now)
        if not problems:
            break
        for problem in problems:
            print(f"    rejected: {problem}")
    if problems or career is None:
        raise SystemExit(
            "Could not obtain a coherent career after "
            f"{attempts} attempts; last problems:\n  - " + "\n  - ".join(problems)
        )

    context = _context_for_dream(career)
    problems = []
    dream: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        print(f"  dream job attempt {attempt}/{attempts} ...", flush=True)
        try:
            dream = llm.complete_json(
                "profile.dreamjob",
                _DREAM_SYSTEM,
                _dream_request(context, problems, dream),
                schema_hint=_DREAM_SCHEMA,
                prefer_strong=strong,
                temperature=0.4 + 0.2 * (attempt - 1),
                max_tokens=dream_budget,
                entity_type="e2e_persona",
            )
        except LLMError as exc:
            problems = [_unparseable(exc)]
            print(f"    rejected: {problems[0]}")
            continue
        problems = validate_dream_job(dream)
        if not problems:
            break
        for problem in problems:
            print(f"    rejected: {problem}")
    if problems or dream is None:
        raise SystemExit(
            "Could not obtain a usable dream-job statement after "
            f"{attempts} attempts; last problems:\n  - " + "\n  - ".join(problems)
        )
    return build_persona(career, dream, now)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def summarise(persona: dict[str, Any]) -> None:
    identity = persona["identity"]
    print()
    print(f"Persona    : {identity['name']} - {identity['headline']}")
    print(f"Contact    : {identity['email']}  {identity['phone']}")
    print(f"Location   : {identity['location_full']}")
    print(f"As of      : {persona['as_of']}")
    print()
    print("Career     :")
    for role in persona["experience"]:
        marker = "*" if role["current"] else " "
        print(f"  {marker} {role['linkedin_dates']:<44} {role['title']} - {role['employer']}")
    print()
    print(
        f"Education  : {len(persona['education'])}   "
        f"Certifications: {len(persona['certifications'])}   "
        f"Languages: {len(persona['languages'])}"
    )
    print(
        f"Skills     : {len(persona['skills'])} in "
        f"{len(persona['skill_groups'])} families ({len(persona['top_skills'])} top)   "
        f"Publications: {len(persona['publications'])}   "
        f"Projects: {len(persona['projects'])}"
    )
    statement = persona["dream_job"]["statement"]
    print(
        f"Dream job  : {len(statement.split())} words, "
        f"{len(persona['dream_job']['deal_breakers'])} deal-breakers"
    )
    print()
    print("Planted conflicts (FR-103):")
    for item in persona["planted_conflicts"]:
        print(
            f"  {item['kind']:<9} {item['expected_field_path']:<24} "
            f"linkedin={item['value_linkedin']!r} cv={item['value_cv']!r}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the end-to-end test persona")
    parser.add_argument("--force", action="store_true", help="regenerate even if out/ has one")
    parser.add_argument("--strong", action="store_true", help="use the reasoning model")
    parser.add_argument("--attempts", type=int, default=8, help="validation retries per call")
    parser.add_argument("--out", type=Path, default=PERSONA_PATH)
    args = parser.parse_args()

    if args.out.exists() and not args.force:
        persona = json.loads(args.out.read_text(encoding="utf-8"))
        print(f"Kept       : {args.out} (pass --force to regenerate)")
        summarise(persona)
        return 0

    print(f"Generating : {args.out}")
    # FR-364: the two calls are logged like any other, under task ids the
    # product already routes and prices.
    llm = LLMClient(timeout=600.0)
    persona = _generate(llm, strong=args.strong, attempts=max(1, args.attempts))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(persona, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Wrote      : {args.out}")
    summarise(persona)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
