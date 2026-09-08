"""LinkedIn profile advice, grounded in the collected vacancies (FR-443, FR-385, CR-405).

FR-443 asks for suggestions that make recruiters searching for the dream job
find this job seeker: headline, About section, listed skills, keywords and
featured content.  Two constraints shape the whole module.

**The advice is grounded, not generic.**  Every keyword recommended here is
counted in the campaign's own vacancy corpus - the document frequency of the
term across the postings that match the dream job - and every keyword carries
that count.  "Recruiters search for X, and X appears in 14 of your 31 matching
postings" is advice; "use relevant keywords" is not.

**The system never touches LinkedIn.**  Everything produced here is text for
the job seeker to copy, edit and apply by hand.  Under discretion mode
(FR-385) it is not even that: profile edits and an "open to work" banner are
exactly the signals a currently-employed seeker asked the system to suppress,
so the advice is kept, marked as not-to-apply-now, and the banner suggestion is
never made at all.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from dreamjob.db.connection import from_json
from dreamjob.db.repositories import intelligence as repo
from dreamjob.intelligence.text import (
    canonical_skill,
    canonical_skills,
    display_skill,
    skill_held,
    statement_texts,
    tokens,
)
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError
from dreamjob.pipeline.enrichment import default_llm, load_prompt

log = logging.getLogger(__name__)

PROMPT_NAME = "linkedin_profile"

#: LinkedIn's own limits, which the advice has to fit inside.
HEADLINE_MAX_CHARS = 220
ABOUT_MAX_CHARS = 2600
MAX_LISTED_SKILLS = 25
MAX_KEYWORDS = 20
MAX_FEATURED = 5

#: A term has to appear in this share of the corpus before it is worth putting
#: on a profile; below it, it is one recruiter's vocabulary, not the market's.
MIN_TERM_SHARE = 0.15
MIN_TERM_POSTINGS = 2


@dataclass
class Term:
    """One corpus term, with the evidence for recommending it."""

    term: str
    postings: int
    share: float
    held: bool
    kind: str = "keyword"           # keyword | skill | title

    def as_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "postings": self.postings,
            "share": round(self.share, 3),
            "held": self.held,
            "kind": self.kind,
        }


@dataclass
class Corpus:
    """The vacancy corpus the advice is grounded in (FR-443)."""

    postings: int
    skills: list[Term] = field(default_factory=list)
    keywords: list[Term] = field(default_factory=list)
    titles: list[tuple[str, int]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "postings": self.postings,
            "skills": [t.as_dict() for t in self.skills],
            "keywords": [t.as_dict() for t in self.keywords],
            "titles": [{"title": t, "postings": n} for t, n in self.titles],
            "method": (
                "document frequency across the campaign's matching postings; a term is "
                "recommended when it appears in at least "
                f"{int(MIN_TERM_SHARE * 100)}% of them"
            ),
        }


@dataclass
class Advice:
    """FR-443's suggestions, as editable text."""

    job_seeker_id: str
    campaign_id: str | None
    language: str
    headlines: list[dict[str, Any]] = field(default_factory=list)
    about: str = ""
    skills: list[dict[str, Any]] = field(default_factory=list)
    keywords: list[dict[str, Any]] = field(default_factory=list)
    featured: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    corpus: Corpus | None = None
    discretion_mode: bool = False
    generated_by: str = "deterministic"
    advice_id: str | None = None
    edited_text: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.advice_id,
            "job_seeker_id": self.job_seeker_id,
            "campaign_id": self.campaign_id,
            "language": self.language,
            "headlines": self.headlines,
            "about": self.about,
            "skills": self.skills,
            "keywords": self.keywords,
            "featured": self.featured,
            "notes": self.notes,
            "corpus": self.corpus.as_dict() if self.corpus else None,
            "discretion_mode": self.discretion_mode,
            "apply_now": not self.discretion_mode,
            "generated_by": self.generated_by,
            "edited_text": self.edited_text,
            "note": (
                "Suggestions only. Dream Job never signs in to LinkedIn and never changes a "
                "profile; copy what is useful and edit it in your own words (FR-443)."
            ),
        }


# ---------------------------------------------------------------------------
# The corpus (FR-443: grounded in the vacancies actually collected)
# ---------------------------------------------------------------------------


def build_corpus(rows: list[dict[str, Any]], held: set[str]) -> Corpus:
    """Term frequency across the matching postings, as document frequency.

    Document frequency rather than raw count: a posting that repeats "Python"
    nine times is one employer asking for Python, and a profile is optimised
    against how many employers use a word, not how loudly one of them does.
    """
    if not rows:
        return Corpus(postings=0)

    skill_counts: Counter[str] = Counter()
    keyword_counts: Counter[str] = Counter()
    title_counts: Counter[str] = Counter()

    for row in rows:
        skills = set(
            canonical_skills(row.get("required_skills") or row.get("vacancy_required_skills"))
            + canonical_skills(row.get("desirable_skills") or row.get("vacancy_desirable_skills"))
        )
        skill_counts.update(skills)

        title = str(row.get("title") or row.get("vacancy_title") or "").strip()
        if title:
            title_counts[title] += 1

        text = " ".join(
            str(x)
            for x in (title, row.get("description") or row.get("vacancy_description"))
            if x
        )[:12000]
        keyword_counts.update({w for w in tokens(text) if len(w) > 2})

    total = len(rows)

    def as_terms(counts: Counter[str], kind: str, limit: int) -> list[Term]:
        out: list[Term] = []
        for term, count in counts.most_common(limit * 4):
            share = count / total
            if count < MIN_TERM_POSTINGS or share < MIN_TERM_SHARE:
                continue
            out.append(
                Term(
                    term=term,
                    postings=count,
                    share=share,
                    held=skill_held(canonical_skill(term) or term, held),
                    kind=kind,
                )
            )
            if len(out) >= limit:
                break
        return out

    return Corpus(
        postings=total,
        skills=as_terms(skill_counts, "skill", MAX_LISTED_SKILLS * 2),
        keywords=as_terms(keyword_counts, "keyword", MAX_KEYWORDS * 2),
        titles=title_counts.most_common(8),
    )


# ---------------------------------------------------------------------------
# Deterministic drafts (CR-405: assembled from stored facts only)
# ---------------------------------------------------------------------------


def _current_role(sections: dict[str, Any]) -> tuple[str, str]:
    for entry in sections.get("experience") or []:
        if isinstance(entry, dict) and entry.get("title"):
            return str(entry["title"]), str(entry.get("company") or "")
    return "", ""


def _target_titles(dream: dict[str, Any] | None) -> list[str]:
    out: list[str] = []
    for role in from_json((dream or {}).get("target_roles"), []) or []:
        title = role.get("title") if isinstance(role, dict) else role
        if title and str(title).strip():
            out.append(str(title).strip())
    return out


def _headlines(
    sections: dict[str, Any],
    dream: dict[str, Any] | None,
    corpus: Corpus,
    composite: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Three headline options, each labelled with what it claims."""
    current_title, current_company = _current_role(sections)
    targets = _target_titles(dream)
    held_terms = [display_skill(t.term) for t in corpus.skills if t.held][:4]
    domains = statement_texts(from_json((composite or {}).get("domains"), []))[:2]

    options: list[dict[str, Any]] = []
    if current_title:
        text = current_title
        if held_terms:
            text += " | " + ", ".join(held_terms[:3])
        options.append(
            {
                "text": text[:HEADLINE_MAX_CHARS],
                "basis": "current role plus the skills the corpus searches for",
                "claims": "only the title you hold today",
            }
        )
    if targets:
        parts = [targets[0]]
        if domains:
            parts.append(domains[0].split(",")[0][:40])
        if held_terms:
            parts.append(", ".join(held_terms[:2]))
        options.append(
            {
                "text": " | ".join(p for p in parts if p)[:HEADLINE_MAX_CHARS],
                "basis": "the dream-job target role, with the domain and skills you evidence",
                "claims": (
                    "the target role as a direction, not as a title you hold - only use it "
                    "if it describes work you already do (CR-405)"
                ),
            }
        )
    if corpus.titles:
        market_title = corpus.titles[0][0]
        text = market_title
        if held_terms:
            text += " | " + ", ".join(held_terms[:3])
        options.append(
            {
                "text": text[:HEADLINE_MAX_CHARS],
                "basis": (
                    f"the most common title in the corpus ({corpus.titles[0][1]} of "
                    f"{corpus.postings} postings) - the words recruiters type"
                ),
                "claims": "a market title; check it is one you would accept being called",
            }
        )
    if not options and current_company:
        options.append(
            {
                "text": f"{current_company}"[:HEADLINE_MAX_CHARS],
                "basis": "the only fact the profile carries",
                "claims": "nothing beyond your current employer",
            }
        )
    return options[:3]


def _about(
    composite: dict[str, Any] | None,
    dream: dict[str, Any] | None,
    corpus: Corpus,
    sections: dict[str, Any],
) -> str:
    """A first draft of the About section, assembled from stored material only."""
    parts: list[str] = []
    narrative = str((composite or {}).get("narrative") or "").strip()
    if narrative:
        parts.append(narrative)
    else:
        summary = str(sections.get("summary") or "").strip()
        if summary:
            parts.append(summary)

    achievements = statement_texts(from_json((composite or {}).get("achievements"), []))[:3]
    if achievements:
        parts.append(
            "What that looks like in practice:\n"
            + "\n".join(f"- {a}" for a in achievements)
        )

    held_terms = [display_skill(t.term) for t in corpus.skills if t.held][:8]
    if held_terms:
        parts.append("I work with: " + ", ".join(held_terms) + ".")

    statement = str((dream or {}).get("statement") or "").strip()
    if statement:
        parts.append("What I am looking for next: " + re.sub(r"\s+", " ", statement)[:400])
    return "\n\n".join(parts)[:ABOUT_MAX_CHARS]


def _skills_advice(corpus: Corpus) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for term in corpus.skills:
        if not term.held:
            continue
        out.append(
            {
                "skill": term.term,
                "label": display_skill(term.term),
                "postings": term.postings,
                "share": round(term.share, 3),
                "action": "list it, and ask a colleague to endorse it",
                "reason": (
                    f"{term.postings} of {corpus.postings} matching postings ask for it and "
                    "your profile already evidences it"
                ),
            }
        )
        if len(out) >= MAX_LISTED_SKILLS:
            break
    return out


def _missing_skills(corpus: Corpus) -> list[dict[str, Any]]:
    return [
        {
            "skill": term.term,
            "label": display_skill(term.term),
            "postings": term.postings,
            "share": round(term.share, 3),
            "action": "do not list it yet",
            "reason": (
                "in demand in this corpus but not evidenced in your profile; it belongs in "
                "the gap analysis (FR-381), not in your skills list"
            ),
        }
        for term in corpus.skills
        if not term.held
    ][:10]


def _featured(evidence: list[dict[str, Any]], composite: dict[str, Any] | None) -> list[dict]:
    out: list[dict[str, Any]] = []
    for row in evidence:
        if not row.get("url"):
            continue
        out.append(
            {
                "title": row.get("title"),
                "url": row.get("url"),
                "kind": row.get("kind"),
                "reason": "already recorded as evidence; featuring it puts it above the fold",
            }
        )
        if len(out) >= MAX_FEATURED:
            break
    if len(out) < MAX_FEATURED:
        for text in statement_texts(from_json((composite or {}).get("public_footprint"), [])):
            out.append(
                {
                    "title": text[:160],
                    "url": None,
                    "kind": "public_footprint",
                    "reason": "named in your composite profile; add the link when you have it",
                }
            )
            if len(out) >= MAX_FEATURED:
                break
    return out


# ---------------------------------------------------------------------------
# LLM refinement (NFR-104, NFR-205, CR-405)
# ---------------------------------------------------------------------------


def _refine(advice: Advice, facts: dict[str, Any], llm: LLMClient) -> bool:
    template = load_prompt(PROMPT_NAME)
    system, user = template.render(language=advice.language)
    try:
        data = llm.complete_json(
            template.task or "generate.linkedin",
            system=system,
            user=user,
            untrusted={
                "facts": json.dumps(facts, ensure_ascii=False)[:12000],
                "corpus": json.dumps(
                    advice.corpus.as_dict() if advice.corpus else {}, ensure_ascii=False
                )[:6000],
            },
            entity_type="linkedin_advice",
            entity_id=advice.campaign_id,
            prompt_template=template.name,
            prompt_version=template.version,
            prefer_strong=False,
            temperature=0.4,
            max_tokens=1800,
        )
    except (BudgetExhausted, LLMError) as exc:
        log.info("LinkedIn advice stays deterministic: %s", exc)
        return False
    if not isinstance(data, dict):
        return False

    headlines = [
        {
            "text": str(item.get("text") or "")[:HEADLINE_MAX_CHARS],
            "basis": str(item.get("basis") or "written from your stored profile facts")[:300],
            "claims": str(item.get("claims") or "")[:300],
        }
        for item in (data.get("headlines") or [])
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    if headlines:
        advice.headlines = headlines[:3]
    about = str(data.get("about") or "").strip()
    if about:
        advice.about = about[:ABOUT_MAX_CHARS]
    for note in data.get("notes") or []:
        if isinstance(note, str) and note.strip():
            advice.notes.append(note.strip()[:300])
    return True


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def suggest(
    job_seeker_id: str,
    campaign_id: str | None = None,
    *,
    language: str = "en",
    use_llm: bool = True,
    persist: bool = True,
    llm: LLMClient | None = None,
) -> Advice:
    """Produce the FR-443 suggestions for one job seeker."""
    campaign = (
        repo.get_campaign(campaign_id, job_seeker_id)
        if campaign_id
        else repo.latest_campaign(job_seeker_id)
    )
    persona_id = (campaign or {}).get("persona_id")
    composite = repo.latest_composite(job_seeker_id, persona_id) or repo.latest_composite(
        job_seeker_id
    )
    dream = repo.latest_dream_model(job_seeker_id, persona_id) or repo.latest_dream_model(
        job_seeker_id
    )
    profile = repo.latest_profile_version(job_seeker_id) or {}
    sections = profile.get("sections") if isinstance(profile.get("sections"), dict) else {}
    evidence = repo.evidence_items(job_seeker_id)
    rows = repo.campaign_opportunities(job_seeker_id, (campaign or {}).get("id"))
    directive_row = repo.directive_set(job_seeker_id, (campaign or {}).get("directive_set_id"))
    discretion = bool((directive_row or {}).get("discretion_mode"))

    held: set[str] = set()
    for row in repo.profile_skills(job_seeker_id):
        for label in (row.get("normalised_label"), row.get("raw_label")):
            canonical = canonical_skill(label)
            if canonical:
                held.add(canonical)
    for key in ("core_competencies", "adjacent_competencies"):
        for text in statement_texts(from_json((composite or {}).get(key), [])):
            canonical = canonical_skill(text)
            if canonical:
                held.add(canonical)

    corpus = build_corpus(rows, held)
    advice = Advice(
        job_seeker_id=job_seeker_id,
        campaign_id=(campaign or {}).get("id"),
        language=language,
        headlines=_headlines(sections, dream, corpus, composite),
        about=_about(composite, dream, corpus, sections),
        skills=_skills_advice(corpus),
        keywords=[t.as_dict() for t in corpus.keywords[:MAX_KEYWORDS]],
        featured=_featured(evidence, composite),
        corpus=corpus,
        discretion_mode=discretion,
    )

    advice.notes.append(
        "Dream Job does not sign in to LinkedIn and does not change your profile. Everything "
        "here is text to copy and edit (FR-443)."
    )
    if corpus.postings:
        advice.notes.append(
            f"Keywords are counted across the {corpus.postings} postings collected for this "
            "campaign, not taken from generic profile advice."
        )
    else:
        advice.notes.append(
            "No vacancies have been collected for this campaign yet, so the keyword advice "
            "is empty rather than generic. Run a collection first (FR-181)."
        )
    missing = _missing_skills(corpus)
    if missing:
        advice.notes.append(
            f"{len(missing)} skill(s) in demand here are not evidenced in your profile. They "
            "are listed under skills_in_demand and belong to the gap analysis, not to your "
            "skills section - listing a skill you cannot demonstrate fails at the first "
            "interview question."
        )
    if discretion:
        # FR-385: applying any of this is a visible signal to the current employer.
        advice.notes.insert(
            0,
            "Discretion mode is on. Editing a headline, an About section or the skills list "
            "notifies your network, and the 'open to work' banner is visible to recruiters at "
            "your own employer. Keep this draft until you are ready; no 'open to work' "
            "suggestion is made while discretion mode is on (FR-385).",
        )

    client = llm
    if use_llm and client is None:
        client = default_llm(job_seeker_id, advice.campaign_id)
    if client is not None:
        facts = {
            "current_role": _current_role(sections),
            "narrative": (composite or {}).get("narrative"),
            "achievements": statement_texts(
                from_json((composite or {}).get("achievements"), [])
            )[:6],
            "domains": statement_texts(from_json((composite or {}).get("domains"), []))[:5],
            "core_competencies": statement_texts(
                from_json((composite or {}).get("core_competencies"), [])
            )[:12],
            "dream_job_statement": (dream or {}).get("statement"),
            "target_roles": _target_titles(dream),
            "discretion_mode": discretion,
        }
        if _refine(advice, facts, client):
            advice.generated_by = "llm+deterministic"

    if persist:
        advice.advice_id = repo.save_advice(
            job_seeker_id,
            {
                "persona_id": persona_id,
                "campaign_id": advice.campaign_id,
                "dream_job_model_id": (dream or {}).get("id"),
                "language": language,
                "headlines": advice.headlines,
                "about": advice.about,
                "skills": {"list": advice.skills, "in_demand_not_held": missing},
                "keywords": advice.keywords,
                "featured": advice.featured,
                "notes": advice.notes,
                "corpus_summary": corpus.as_dict(),
                "discretion_mode": 1 if discretion else 0,
                "generated_by": advice.generated_by,
            },
        )
    return advice


def latest(job_seeker_id: str, campaign_id: str | None = None) -> dict[str, Any] | None:
    return repo.latest_advice(job_seeker_id, campaign_id)


def save_edit(job_seeker_id: str, advice_id: str, text: str) -> dict[str, Any] | None:
    """FR-443: store the seeker's own edit.  Nothing is sent anywhere."""
    return repo.save_advice_edit(advice_id, job_seeker_id, text[:20000])
