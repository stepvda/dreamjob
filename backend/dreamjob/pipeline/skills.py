"""Normalised skill taxonomy with proficiency and recency (FR-107).

FR-107 asks for skills mapped onto ESCO "or a configurable taxonomy".  The
mapping shipped in ``data/skills_taxonomy.json`` is an ESCO-shaped, curated
set of the labels that actually appear in software, data, HR and content CVs,
each with its synonyms.  It is bundled rather than fetched: matching a skill
label is on the critical path of every profile save and every opportunity
score, and a network round trip there would make the taxonomy a availability
dependency of the whole product (NFR-101, IR-102).

Labels the deterministic matcher cannot place are handed to the LLM in one
batch (task ``normalise.skill``), and only then; an unreachable model costs
coverage, never the save.

Proficiency and recency are derived, not asked for: the experience timeline
already says how long a skill has been used and when it was last used, and
FR-107 wants exactly those two numbers for matching.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from dreamjob.pipeline.linkedin_pdf import months_between, year_of

log = logging.getLogger(__name__)

TAXONOMY_PATH = Path(__file__).parent / "data" / "skills_taxonomy.json"

# Labels this short or this generic are noise, not skills.
_STOPWORDS = frozenset(
    {
        "and", "the", "with", "for", "from", "into", "etc", "various", "other",
        "including", "such", "more", "new", "own", "using", "use", "work",
        "working", "years", "experience", "skills", "responsible",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9+#./-]+")


@dataclass(frozen=True)
class TaxonomyEntry:
    code: str
    label: str
    group: str
    synonyms: tuple[str, ...]


@dataclass
class SkillMatch:
    raw_label: str
    normalised_label: str
    taxonomy: str = "esco"
    taxonomy_code: str | None = None
    match_kind: str = "exact"          # exact | synonym | token | llm | verbatim
    confidence: float = 1.0
    years_experience: float | None = None
    last_used_year: int | None = None
    proficiency: int | None = None
    evidence_refs: list[str] = field(default_factory=list)


@lru_cache(maxsize=1)
def load_taxonomy() -> tuple[str, list[TaxonomyEntry]]:
    data = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
    entries = [
        TaxonomyEntry(
            code=item["code"],
            label=item["label"],
            group=item.get("group", ""),
            synonyms=tuple(item.get("synonyms", ())),
        )
        for item in data["skills"]
    ]
    return data.get("taxonomy", "esco"), entries


@lru_cache(maxsize=1)
def _index() -> dict[str, TaxonomyEntry]:
    """Exact-match index over labels and synonyms, on a normalised key."""
    _, entries = load_taxonomy()
    index: dict[str, TaxonomyEntry] = {}
    for entry in entries:
        for form in (entry.label, *entry.synonyms):
            index.setdefault(_key(form), entry)
    return index


def _key(text: str) -> str:
    text = text.lower().replace("&", " and ")
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(r"[^a-z0-9+#./ -]+", " ", text)
    return re.sub(r"\s+", " ", text).strip(" -")


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(_key(text)) if t not in _STOPWORDS and len(t) > 1}


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def normalise_skill(label: str) -> SkillMatch | None:
    """Map one raw label onto the taxonomy, or return ``None`` if unmatched."""
    raw = (label or "").strip(" .;,·•-")
    if len(raw) < 2:
        return None
    taxonomy, entries = load_taxonomy()
    key = _key(raw)
    if not key:
        return None

    entry = _index().get(key)
    if entry is not None:
        kind = "exact" if _key(entry.label) == key else "synonym"
        return SkillMatch(
            raw, entry.label, taxonomy, entry.code, kind, 1.0 if kind == "exact" else 0.95
        )

    # A phrase that contains a taxonomy label ("advanced Python programming")
    # maps to it; the longest label wins so "machine learning" beats "learning".
    tokens = _tokens(raw)
    if not tokens:
        return None
    best: tuple[float, TaxonomyEntry, str] | None = None
    for candidate in entries:
        for form in (candidate.label, *candidate.synonyms):
            form_tokens = _tokens(form)
            if not form_tokens or not form_tokens <= tokens:
                continue
            # A single shared word only identifies a skill in a short phrase;
            # in a long one it is as likely to be incidental ("orchestration"
            # in "container orchestration at scale").
            if len(form_tokens) < 2 and len(tokens) > 3:
                continue
            # Prefer the most specific label that the phrase fully contains.
            score = len(form_tokens) / max(len(tokens), 1)
            weight = len(form_tokens) + score
            if best is None or weight > best[0]:
                best = (weight, candidate, form)
    if best is not None:
        _, candidate, form = best
        coverage = len(_tokens(form)) / len(tokens)
        return SkillMatch(
            raw, candidate.label, taxonomy, candidate.code, "token",
            round(0.55 + 0.35 * coverage, 2),
        )
    return None


def normalise_labels(
    labels: list[str], *, llm: Any = None, keep_unmatched: bool = True
) -> list[SkillMatch]:
    """Normalise a batch of labels, using the LLM only for the leftovers."""
    matches: list[SkillMatch] = []
    unmatched: list[str] = []
    for label in labels:
        match = normalise_skill(label)
        if match is not None:
            matches.append(match)
        elif label.strip():
            unmatched.append(label.strip())

    if unmatched and llm is not None:
        matches.extend(_llm_normalise(unmatched, llm))
        placed = {m.raw_label for m in matches}
        unmatched = [u for u in unmatched if u not in placed]

    if keep_unmatched:
        # An unrecognised label is still the job seeker's own word for a real
        # skill; it is kept verbatim, flagged by its low confidence (NFR-402).
        for label in unmatched:
            if 2 <= len(label) <= 80:
                matches.append(
                    SkillMatch(label, label, "verbatim", None, "verbatim", 0.35)
                )
    return _deduplicate(matches)


def _deduplicate(matches: list[SkillMatch]) -> list[SkillMatch]:
    best: dict[str, SkillMatch] = {}
    for match in matches:
        key = match.normalised_label.lower()
        current = best.get(key)
        if current is None or match.confidence > current.confidence:
            best[key] = match
    return sorted(best.values(), key=lambda m: (-m.confidence, m.normalised_label))


def _llm_normalise(labels: list[str], llm: Any) -> list[SkillMatch]:
    """Ask the model to place labels the bundled taxonomy does not cover."""
    taxonomy, entries = load_taxonomy()
    vocabulary = ", ".join(sorted({e.label for e in entries}))
    try:
        data = llm.complete_json(
            "normalise.skill",
            system=(
                "You map skill labels from a CV onto a fixed taxonomy. For each input label "
                "return the single closest taxonomy label, or null when nothing fits. "
                "Never invent taxonomy labels and never invent skills.\n\n"
                f"Taxonomy labels: {vocabulary}"
            ),
            user="Map these labels.",
            untrusted={"labels": json.dumps(labels, ensure_ascii=False)},
            schema_hint='{"mappings": [{"input": str, "taxonomy_label": str|null}]}',
            prefer_strong=False,
        )
    except Exception as exc:  # noqa: BLE001 - coverage degrades, the save does not
        log.warning("Skill normalisation fell back to verbatim labels: %s", exc)
        return []

    by_label = {e.label.lower(): e for e in entries}
    out: list[SkillMatch] = []
    for row in (data or {}).get("mappings", []) if isinstance(data, dict) else []:
        if not isinstance(row, dict):
            continue
        raw = str(row.get("input") or "").strip()
        target = row.get("taxonomy_label")
        entry = by_label.get(str(target).lower()) if target else None
        if raw and entry is not None:
            out.append(SkillMatch(raw, entry.label, taxonomy, entry.code, "llm", 0.7))
    return out


# ---------------------------------------------------------------------------
# Recency, duration and proficiency
# ---------------------------------------------------------------------------


def _skill_corpus(sections: dict[str, Any]) -> list[str]:
    """Every free-text passage worth mining for skill labels."""
    corpus: list[str] = []
    corpus.extend(str(s) for s in sections.get("top_skills") or [])
    if sections.get("summary"):
        corpus.append(str(sections["summary"]))
    for entry in sections.get("experience") or []:
        corpus.extend(str(v) for v in (entry.get("title"), entry.get("description")) if v)
    for key in ("projects", "certifications", "courses", "publications"):
        for entry in sections.get(key) or []:
            if isinstance(entry, dict):
                corpus.extend(str(v) for v in (entry.get("title"), entry.get("detail")) if v)
            elif entry:
                corpus.append(str(entry))
    for value in (sections.get("other") or {}).values():
        if isinstance(value, list):
            corpus.extend(str(v) for v in value)
        elif value:
            corpus.append(str(value))
    return corpus


def mine_labels(sections: dict[str, Any]) -> list[str]:
    """Candidate skill labels: the stated ones plus taxonomy hits in the prose."""
    labels: list[str] = [
        str(s).strip() for s in (sections.get("top_skills") or []) if str(s).strip()
    ]
    _, entries = load_taxonomy()
    haystack = _key(" \n ".join(_skill_corpus(sections)))
    seen = {_key(entry_label) for entry_label in labels}
    for entry in entries:
        for form in (entry.label, *entry.synonyms):
            form_key = _key(form)
            if len(form_key) < 2 or form_key in seen:
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(form_key)}(?![a-z0-9])", haystack):
                labels.append(entry.label)
                seen.add(_key(entry.label))
                break
    return labels


def _experience_windows(sections: dict[str, Any]) -> list[tuple[str, str | None, str | None]]:
    out = []
    for entry in sections.get("experience") or []:
        fields = (entry.get("title"), entry.get("company"), entry.get("description"))
        text = " ".join(str(v) for v in fields if v)
        out.append((text, entry.get("start"), entry.get("end")))
    return out


def derive_recency(
    match: SkillMatch, sections: dict[str, Any], *, now: datetime | None = None
) -> SkillMatch:
    """Fill ``years_experience``, ``last_used_year`` and ``proficiency``.

    A skill counts towards a role when the role's own text mentions the skill
    or one of its synonyms; the "Top Skills" list on its own carries no dates,
    so a stated-but-unevidenced skill falls back to the most recent role.
    """
    today = now or datetime.now(UTC)
    _, entries = load_taxonomy()
    by_label = {e.label: e for e in entries}
    entry = by_label.get(match.normalised_label)
    forms = [match.raw_label, match.normalised_label]
    if entry is not None:
        forms.extend(entry.synonyms)
    patterns = [
        re.compile(rf"(?<![a-z0-9]){re.escape(_key(f))}(?![a-z0-9])") for f in forms if _key(f)
    ]

    months = 0.0
    last_year: int | None = None
    for text, start, end in _experience_windows(sections):
        haystack = _key(text)
        if not any(p.search(haystack) for p in patterns):
            continue
        months += months_between(start, end, today.year, today.month)
        end_year = year_of(end) or (today.year if start else None)
        if end_year is not None and (last_year is None or end_year > last_year):
            last_year = end_year

    if months:
        match.years_experience = round(months / 12.0, 1)
        match.last_used_year = last_year
    else:
        # Stated without evidence in the timeline: assume it is current, but
        # claim no duration rather than inventing one (CR-405).
        windows = _experience_windows(sections)
        if windows:
            match.last_used_year = max(
                (year_of(end) or today.year for _, _, end in windows), default=None
            )
    match.proficiency = _proficiency(match, today.year)
    return match


def _proficiency(match: SkillMatch, this_year: int) -> int:
    """A 1..5 band from duration and recency, per FR-107."""
    years = match.years_experience or 0.0
    if years >= 8:
        score = 5
    elif years >= 4:
        score = 4
    elif years >= 2:
        score = 3
    elif years > 0:
        score = 2
    else:
        score = 2
    if match.last_used_year is not None:
        stale = this_year - match.last_used_year
        if stale >= 10:
            score -= 2
        elif stale >= 5:
            score -= 1
    return max(1, min(5, score))


def build_skills(
    sections: dict[str, Any], *, llm: Any = None, now: datetime | None = None
) -> list[SkillMatch]:
    """Full FR-107 pass: mine, normalise, then date every skill."""
    matches = normalise_labels(mine_labels(sections), llm=llm)
    return [derive_recency(m, sections, now=now) for m in matches]
