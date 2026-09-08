"""Shared text helpers for the dream-job intelligence modules (FR-381..384, FR-443).

The gap analysis, the values comparison and the LinkedIn advice all read the
same two kinds of material - the job seeker's own profile and a corpus of
vacancy text - and all three have to decide whether two phrases mean the same
thing.  Keeping that decision in one place is what makes their answers agree:
a skill counted as "missing" by the gap analysis is the same string the
LinkedIn advice recommends listing.

Skill labels go through the bundled taxonomy (FR-107) rather than through a
lowercase comparison, so "K8s" in a posting and "Kubernetes" in the profile are
one skill.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from dreamjob.pipeline.skills import normalise_skill

WORD_RE = re.compile(r"[a-z0-9+#][a-z0-9+#.-]{1,}")

#: Words that carry no meaning in a job posting or a profile.  Three languages,
#: because the corpus is Belgian: postings arrive in English, Dutch and French.
STOPWORDS = frozenset(
    {
        "and", "the", "with", "for", "from", "into", "our", "you", "your", "are", "will",
        "who", "that", "this", "have", "has", "not", "all", "can", "new", "job", "role",
        "team", "work", "working", "years", "year", "experience", "company", "about",
        "within", "well", "also", "must", "should", "would", "their", "them", "they",
        "een", "van", "met", "voor", "het", "die", "dat", "aan", "als", "zijn",
        "les", "des", "pour", "dans", "nous", "vous", "une", "est", "sur", "avec",
        "candidate", "candidates", "position", "opportunity", "join", "looking",
    }
)


def fold(text: Any) -> str:
    """Lowercase, strip accents, collapse whitespace - the comparison form."""
    raw = str(text or "")
    decomposed = unicodedata.normalize("NFKD", raw)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped.lower()).strip()


def tokens(*texts: Any) -> set[str]:
    """Content words of the given texts, folded and de-duplicated."""
    joined = fold(" ".join(str(t) for t in texts if t))
    return {w for w in WORD_RE.findall(joined) if w not in STOPWORDS and len(w) > 1}


def phrase_in(phrase: str, text: str) -> bool:
    """True when ``phrase`` occurs in ``text`` as a whole word sequence."""
    needle, haystack = fold(phrase), fold(text)
    if not needle or not haystack:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


def canonical_skill(label: Any) -> str:
    """One skill label in the form the profile and the corpus can be compared in.

    Falls back to the folded raw label when the taxonomy does not know it - an
    unrecognised label is still a real skill somebody wrote down (FR-107).
    """
    raw = str(label or "").strip(" .;,·•-")
    if len(raw) < 2:
        return ""
    match = normalise_skill(raw)
    return fold(match.normalised_label if match else raw)


def display_skill(label: Any) -> str:
    """The skill as it should be *written*, not as it is compared.

    ``canonical_skill`` folds case so that "K8s" and "Kubernetes" compare
    equal; a headline needs the taxonomy's own spelling back.
    """
    raw = str(label or "").strip()
    if not raw:
        return ""
    match = normalise_skill(raw)
    if match is not None:
        return match.normalised_label
    return raw if raw[:1].isupper() else raw.title()


def canonical_skills(labels: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for label in labels or []:
        text = label.get("label") if isinstance(label, dict) else label
        canonical = canonical_skill(text)
        if canonical and canonical not in out:
            out.append(canonical)
    return out


def skill_held(skill: str, held: set[str]) -> bool:
    """Whether the profile evidences a skill, allowing for phrasing differences."""
    if not skill:
        return False
    if skill in held:
        return True
    skill_tokens = {t for t in WORD_RE.findall(skill) if t not in STOPWORDS}
    for candidate in held:
        if skill in candidate or candidate in skill:
            return True
        candidate_tokens = {t for t in WORD_RE.findall(candidate) if t not in STOPWORDS}
        if skill_tokens and skill_tokens <= candidate_tokens:
            return True
    return False


def statement_texts(block: Any) -> list[str]:
    """Flatten a composite-profile block into plain strings.

    Composite blocks are lists of ``{"id", "text", ...}`` statements (FR-125),
    but a hand-edited profile may hold bare strings; both are accepted.
    """
    out: list[str] = []
    for item in block or []:
        if isinstance(item, dict):
            text = item.get("text") or item.get("value") or item.get("label")
        else:
            text = item
        if text:
            out.append(str(text))
    return out


def first_sentence(text: str, limit: int = 220) -> str:
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    if not body:
        return ""
    cut = re.split(r"(?<=[.!?])\s", body)[0]
    return cut[:limit].strip()
