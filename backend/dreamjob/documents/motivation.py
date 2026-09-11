"""Motivation and fit PDF (FR-330, FR-331, FR-128, CR-405).

The second document written for the job seeker alone.  FR-330 fixes its five
parts and this module keeps them in that order: why the job seeker wants this
job - linked explicitly to the dream job model (FR-128) and to their career
trajectory; why they fit the job, with evidence from the profile mapped to
*each* stated or inferred requirement; why they fit the company, drawn from the
company's values and culture, its stage, sector and working style; the
objections to expect and how to answer them; and a short set of talking points
to rehearse.

The mapping in "why I fit the job" is the part that has to be honest.  Every
requirement gets a row whether or not the profile supports it, and one that the
profile does not support is marked ``gap`` and says what is closest instead
(CR-405).  A document that quietly drops the requirements the job seeker
cannot meet is worse than useless in an interview room.

Without an LLM the whole document is still produced: requirements are matched
to profile skills and positions by term overlap, the dream-job links come from
the stored model, and the objections come from the gaps the mapping found.

**When the employer is not named** (17-20% of the corpus: an interim, staffing
or selection agency posting for a client it does not identify), the third part
cannot be written truthfully.  "Why I fit the company" is built today from
``company.values_culture``, ``stage``, ``sector``, ``trajectory``, ``size`` and
``ownership`` - all of which then describe the *agency*.  That is exactly how
one approved package came to praise a staffing firm for a Roeselare
machine-builder's "no-nonsense culture and short communication lines", quoted
out of a paragraph headed *"Onze klant: een internationale en vooruitstrevende
machinebouwer"*.

So for an agency row that section is replaced, not softened, by two blocks
that are true:

* **What the posting says about the employer** - the advert's own sentences,
  verbatim, with nothing inferred from them.  No name is guessed: the eight
  richest client descriptions in the corpus were mapped to a NACE code and a
  postcode and run against the Belgian register, and 0 of 8 resolved to one
  company.  A fabricated employer in a letter is read by the recruiter who
  knows the client, and is the same class of error as an invented e-mail
  address (proposal section 5.1).
* **Questions for the recruiter** - who the employer is, why the role is open,
  temp-to-hire or direct placement, when the name is disclosed.  This is
  genuinely the most useful page of the document for an agency application,
  and it is the mechanism by which the missing half becomes knowable.

The model is not asked to be tactful about this.  It is given no company
record, told the employer is not named, and its ``why_fit_company`` output is
discarded if it produces one anyway - a prompt instruction is a request, and
this has to be a guarantee.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dreamjob.db.connection import utcnow
from dreamjob.documents._llm import complete_json
from dreamjob.documents.consistency import fold
from dreamjob.documents.cv_generator import Disclosure
from dreamjob.documents.pdf_builder import (
    PdfBuilder,
    cover_page,
    label,
    meta_from_inputs,
    normalise_language,
)
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError
from dreamjob.pipeline import employer_product as emp_mod
from dreamjob.pipeline.enrichment import load_prompt

log = logging.getLogger(__name__)

PROMPT_NAME = "motivation_fit"

#: FR-331: configurable templates, expressed as an ordered section list.
TEMPLATES: dict[str, tuple[str, ...]] = {
    "full": ("why_job", "fit_job", "fit_company", "objections", "talking_points"),
    "compact": ("why_job", "fit_job", "talking_points"),
    "interview_drill": ("fit_job", "objections", "talking_points"),
}
DEFAULT_TEMPLATE = "full"

STRENGTH_ORDER = {"strong": 0, "partial": 1, "gap": 2}

#: The strings of the undisclosed-employer path.  They live here rather than in
#: ``pdf_builder.LABELS`` because they belong to this one document and to this
#: one case; ``label()`` is still used for everything the document already had.
INTERMEDIARY_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "posting_says": "What the posting says about the employer",
        "employer_not_named": (
            "The employer is not named in this posting. It was placed by {agency}, "
            "an intermediary, for a client it does not identify. Nothing below is "
            "inferred about that employer: the sentences are the posting's own."
        ),
        "nothing_said": (
            "The posting says nothing about the employer beyond the role itself. "
            "The questions on the next page are how to find out."
        ),
        "what_is_known": "What is known about the assignment",
        "recruiter_questions": "Questions for the recruiter",
        "questions_intro": (
            "Ask these before an interview. The answers turn this from an "
            "application to an unknown company into an application to a known one - "
            "and the briefing can then be regenerated against the real employer."
        ),
        "quoted": "quoted from the posting",
        "objection_unknown_employer": (
            "You are applying to a company whose name you do not know."
        ),
        "objection_answer": (
            "Say so plainly: you are applying for the role, you have asked who the "
            "employer is, and you will judge the fit once you know. Do not pretend "
            "to know the company."
        ),
    },
    "nl": {
        "posting_says": "Wat de vacature over de werkgever zegt",
        "employer_not_named": (
            "De werkgever wordt in deze vacature niet genoemd. Ze is geplaatst door "
            "{agency}, een tussenpersoon, voor een klant die niet bij naam genoemd "
            "wordt. Hieronder staat niets dat wij zelf hebben afgeleid: het zijn de "
            "zinnen van de vacature zelf."
        ),
        "nothing_said": (
            "De vacature zegt niets over de werkgever behalve de functie zelf. "
            "De vragen op de volgende pagina zijn de manier om daarachter te komen."
        ),
        "what_is_known": "Wat over de opdracht bekend is",
        "recruiter_questions": "Vragen voor de recruiter",
        "questions_intro": (
            "Stel deze vóór een gesprek. De antwoorden maken van een sollicitatie "
            "bij een onbekend bedrijf een sollicitatie bij een bekend bedrijf - en "
            "daarna kan de briefing opnieuw worden gemaakt."
        ),
        "quoted": "letterlijk uit de vacature",
        "objection_unknown_employer": "U solliciteert bij een bedrijf waarvan u de naam niet kent.",
        "objection_answer": (
            "Zeg het gewoon: u solliciteert voor de functie, u hebt gevraagd wie de "
            "werkgever is en u beoordeelt de match zodra u dat weet. Doe niet alsof "
            "u het bedrijf kent."
        ),
    },
    "fr": {
        "posting_says": "Ce que l'annonce dit de l'employeur",
        "employer_not_named": (
            "L'employeur n'est pas nommé dans cette annonce. Elle a été publiée par "
            "{agency}, un intermédiaire, pour un client qu'elle n'identifie pas. Rien "
            "ci-dessous n'est déduit : ce sont les phrases de l'annonce elle-même."
        ),
        "nothing_said": (
            "L'annonce ne dit rien de l'employeur au-delà du poste lui-même. Les "
            "questions de la page suivante sont le moyen de le savoir."
        ),
        "what_is_known": "Ce que l'on sait de la mission",
        "recruiter_questions": "Questions pour le recruteur",
        "questions_intro": (
            "Posez-les avant un entretien. Les réponses transforment une candidature "
            "auprès d'une entreprise inconnue en une candidature auprès d'une "
            "entreprise connue - le briefing peut alors être regénéré."
        ),
        "quoted": "cité de l'annonce",
        "objection_unknown_employer": (
            "Vous postulez auprès d'une entreprise dont vous ignorez le nom."
        ),
        "objection_answer": (
            "Dites-le simplement : vous postulez pour le poste, vous avez demandé qui "
            "est l'employeur et vous jugerez l'adéquation une fois que vous le saurez. "
            "Ne faites pas semblant de connaître l'entreprise."
        ),
    },
}

#: The questions.  Ordered by what unlocks the most: the name first, because
#: everything the product normally does becomes possible the moment it is said.
#: Phrased to open with a question word rather than with "Which company", which
#: the NFR-206 leak scan reads as the start of an organisation name (a
#: capitalised token followed by a corporate word) and reports as untraceable.
RECRUITER_QUESTIONS: dict[str, tuple[str, ...]] = {
    "en": (
        "Who is the employer, and may I know the name before the interview?",
        "Why is the role open - growth, a replacement, or a new position?",
        "Is this a temp-to-hire assignment or a direct placement with the employer?",
        "Who employs me on paper, on what contract, and for how long?",
        "At what point in the process is the employer's name disclosed?",
        "Who conducts the interviews - you, or the employer?",
    ),
    "nl": (
        "Wie is de werkgever, en mag ik de naam kennen vóór het gesprek?",
        "Waarom staat de functie open - groei, vervanging, of een nieuwe functie?",
        "Is dit een uitzendopdracht met optie vast, of een rechtstreekse aanwerving?",
        "Wie is mijn werkgever op papier, met welk contract en voor hoe lang?",
        "Op welk moment in de procedure wordt de naam van de werkgever gedeeld?",
        "Wie voert de gesprekken - u, of de werkgever?",
    ),
    "fr": (
        "Qui est l'employeur, et puis-je en connaître le nom avant l'entretien ?",
        "Pourquoi le poste est-il ouvert - croissance, remplacement, ou création ?",
        "S'agit-il d'une mission d'intérim avec option ou d'un recrutement direct ?",
        "Qui est mon employeur sur le contrat, sous quel type de contrat, et pour "
        "combien de temps ?",
        "À quel moment de la procédure le nom de l'employeur est-il communiqué ?",
        "Qui mène les entretiens - vous, ou l'employeur ?",
    ),
}


def _ilabel(lang: str, key: str) -> str:
    table = INTERMEDIARY_LABELS.get((lang or "en")[:2].lower()) or INTERMEDIARY_LABELS["en"]
    return table.get(key) or INTERMEDIARY_LABELS["en"][key]


def _questions(lang: str) -> tuple[str, ...]:
    return RECRUITER_QUESTIONS.get((lang or "en")[:2].lower()) or RECRUITER_QUESTIONS["en"]


def employer_tag(inputs: dict[str, Any]) -> emp_mod.EmployerTag:
    """Who is the employer on this opportunity?

    Taken from ``inputs`` when the caller already read it, so a package that
    generates four artefacts does not resolve the same company four times;
    otherwise read here.
    """
    given = inputs.get("employer_tag")
    if isinstance(given, emp_mod.EmployerTag):
        return given
    return emp_mod.tag_for_opportunity(
        inputs.get("opportunity") or {},
        job_seeker_id=(inputs.get("seeker") or {}).get("id"),
        company=inputs.get("company") or {},
        vacancy=inputs.get("vacancy") or {},
    )
_STOPWORDS = {
    "and", "or", "the", "a", "an", "of", "in", "with", "for", "to", "en", "de", "het",
    "een", "van", "met", "voor", "et", "des", "les", "und", "mit", "der", "die", "das",
    "ervaring", "experience", "erfahrung", "expérience", "kennis", "knowledge", "years",
    "jaar", "jahre", "ans", "sterke", "strong", "goede", "good",
}


@dataclass
class MotivationResult:
    path: str
    template: str
    language: str
    generated_at: str
    used_llm: bool
    content: dict[str, Any]
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "motivation_pdf_path": self.path,
            "motivation_template": self.template,
            "language": self.language,
            "generated_at": self.generated_at,
            "motivation_used_llm": self.used_llm,
            "notes": self.notes,
            # FR-282/CR-405: the package records that this document was written
            # without an employer, so nothing downstream has to infer it from
            # the absence of a section.
            "employer_disclosed": bool(self.content.get("employer_disclosed", True)),
            "gaps": [
                row.get("requirement")
                for row in self.content.get("why_fit_job") or []
                if row.get("strength") == "gap"
            ],
        }

    def plain_text(self) -> str:
        """The document as text, for the NFR-206 leak scan."""
        chunks: list[str] = []
        for row in self.content.get("why_this_job") or []:
            chunks += [str(row.get("text") or ""), _human_evidence(row.get("link"))]
        for row in self.content.get("why_fit_job") or []:
            chunks += [
                str(row.get("requirement") or ""), _human_evidence(row.get("evidence")),
                str(row.get("talking_point") or ""),
            ]
        for row in self.content.get("why_fit_company") or []:
            chunks += [str(row.get("text") or ""), _human_evidence(row.get("evidence"))]
        # The undisclosed-employer blocks are text in the document like any
        # other, so they are scanned like any other (NFR-206).
        for row in self.content.get("posting_says_about_employer") or []:
            chunks += [str(row.get("text") or ""), _human_evidence(row.get("evidence"))]
        chunks += [str(q) for q in self.content.get("recruiter_questions") or []]
        if self.content.get("intermediary_note"):
            chunks.append(str(self.content["intermediary_note"]))
        for row in self.content.get("objections") or []:
            chunks += [
                str(row.get("objection") or ""), str(row.get("answer") or ""),
                _human_evidence(row.get("evidence")),
            ]
        chunks += [str(t) for t in self.content.get("talking_points") or []]
        return "\n".join(c for c in chunks if c)


# ---------------------------------------------------------------------------
# Requirements: stated, then inferred
# ---------------------------------------------------------------------------


def requirements(inputs: dict[str, Any]) -> list[str]:
    """Every stated or inferred requirement of the opening (FR-330)."""
    opportunity = inputs.get("opportunity") or {}
    vacancy = inputs.get("vacancy") or {}
    out: list[str] = []
    for source in (
        opportunity.get("required_skills"),
        vacancy.get("required_skills"),
        opportunity.get("desirable_skills"),
        vacancy.get("desirable_skills"),
    ):
        for item in source or []:
            text = str(item).strip()
            if text and text.lower() not in {o.lower() for o in out}:
                out.append(text)
    if not out:
        # A speculative opening has no posting to read requirements from; its
        # own rationale is the nearest thing, and it is text the pipeline
        # already produced rather than something invented here (CR-405).
        out = _inferred_requirements(
            str(opportunity.get("description") or vacancy.get("description") or "")
        ) or _inferred_requirements(str(opportunity.get("speculative_rationale") or ""))
    return out[:18]


def _inferred_requirements(description: str) -> list[str]:
    """Bullet lines of a posting are its requirements, near enough, when no
    structured extraction ran.  Nothing is invented: these are the posting's
    own sentences."""
    lines = [
        re.sub(r"^\s*[-*•–—▪·]\s*", "", line).strip()
        for line in description.splitlines()
        if line.strip()
    ]
    candidates = [
        line for line in lines if 12 <= len(line) <= 180 and not line.endswith(":")
    ]
    return candidates[:10]


# ---------------------------------------------------------------------------
# Deterministic content
# ---------------------------------------------------------------------------


def _terms(text: str) -> set[str]:
    return {w for w in fold(text).split() if len(w) > 2 and w not in _STOPWORDS}


def derive_content(
    inputs: dict[str, Any], lang: str, tag: emp_mod.EmployerTag | None = None
) -> dict[str, Any]:
    """The document without a model: matching, not writing (CR-405)."""
    opportunity = inputs.get("opportunity") or {}
    company = inputs.get("company") or {}
    if tag is None:
        tag = employer_tag(inputs)
    dream = inputs.get("dream_job") or {}
    composite = inputs.get("composite") or {}
    version = inputs.get("profile_version") or {}
    sections = version.get("sections") or {}
    skills = inputs.get("skills") or []

    # -- why this job: the dream job model and the trajectory, as stored -----
    why_job: list[dict[str, str]] = []
    if dream.get("statement"):
        why_job.append(
            {"text": str(dream["statement"])[:600], "link": "dream_job_model.statement"}
        )
    for key in ("target_roles", "responsibilities", "company_characteristics", "culture_values"):
        for item in _statements(dream.get(key))[:2]:
            why_job.append({"text": item, "link": f"dream_job_model.{key}"})
    for item in _statements(composite.get("career_trajectory"))[-2:]:
        why_job.append({"text": item, "link": "composite_profile.career_trajectory"})

    # -- why I fit the job: requirement -> evidence -------------------------
    evidence_pool = _evidence_pool(sections, skills, composite, inputs.get("evidence") or [])
    fit_job: list[dict[str, str]] = []
    for requirement in requirements(inputs):
        best, score = _best_evidence(requirement, evidence_pool)
        strength = "strong" if score >= 0.6 else "partial" if score >= 0.25 else "gap"
        fit_job.append(
            {
                "requirement": requirement,
                "evidence": best or label(lang, "no_evidence"),
                "strength": strength,
                "talking_point": best or "",
            }
        )

    # -- why I fit the company ----------------------------------------------
    # Every line here comes from ``company.*``.  When the employer is not the
    # company on the row, that is the agency's culture, stage and trajectory,
    # and the section is replaced rather than written (FR-330, CR-405).
    fit_company: list[dict[str, str]] = []
    posting_says: list[dict[str, str]] = []
    recruiter_questions: list[str] = []
    if not tag.employer_disclosed:
        posting_says, recruiter_questions = _undisclosed_employer(inputs, lang, tag)
    else:
        for value in _statements(company.get("values_culture"))[:4]:
            fit_company.append({"text": value, "evidence": "company.values_culture"})
        for key, label_key, source in (
            ("stage", "stage", company.get("stage")),
            ("sector_codes", "sector", ", ".join(_statements(company.get("sector_codes"))[:3])),
            ("trajectory", "trajectory", company.get("trajectory")),
            ("size_band", "size", company.get("size_band")),
            ("ownership", "ownership", company.get("ownership")),
        ):
            if source:
                fit_company.append(
                    {"text": f"{label(lang, label_key)}: {source}", "evidence": f"company.{key}"}
                )

    # -- objections: the gaps the mapping found -----------------------------
    objections: list[dict[str, str]] = []
    for row in fit_job:
        if row["strength"] != "gap":
            continue
        objections.append(
            {
                "objection": label(lang, "gap_objection").format(requirement=row["requirement"]),
                "answer": "",
                "evidence": label(lang, "no_evidence"),
            }
        )
    for deal_breaker in _statements(dream.get("deal_breakers"))[:2]:
        objections.append(
            {
                "objection": deal_breaker,
                "answer": "",
                "evidence": "dream_job_model.deal_breakers",
            }
        )
    if not tag.employer_disclosed:
        # The real objection in an agency interview, and the honest answer to
        # it.  It is put first because it is the one that will actually come up.
        objections.insert(
            0,
            {
                "objection": _ilabel(lang, "objection_unknown_employer"),
                "answer": _ilabel(lang, "objection_answer"),
                "evidence": "employer_kind.verdict",
            },
        )

    talking_points = [
        row["talking_point"] for row in fit_job if row["strength"] == "strong"
    ][:6] or _statements(composite.get("achievements"))[:6]

    return {
        "why_this_job": why_job[:6],
        "why_fit_job": fit_job,
        "why_fit_company": fit_company[:8],
        "posting_says_about_employer": posting_says,
        "recruiter_questions": recruiter_questions,
        "employer_disclosed": tag.employer_disclosed,
        "intermediary_note": tag.disclosure_note(lang),
        "objections": objections[:6],
        "talking_points": talking_points,
        "opportunity_title": opportunity.get("title"),
    }


def _undisclosed_employer(
    inputs: dict[str, Any], lang: str, tag: emp_mod.EmployerTag
) -> tuple[list[dict[str, str]], list[str]]:
    """The replacement for "why I fit the company" when there is no company.

    Two lists, both true: the posting's own sentences about the employer, and
    the questions that would make the employer knowable.  No name is guessed
    and no enthusiasm is written for an organisation nobody has identified -
    a sector-and-town guess is a fabricated company in a document a recruiter
    who knows the client will read (proposal section 5.1).
    """
    opportunity = inputs.get("opportunity") or {}
    vacancy = inputs.get("vacancy") or {}
    rows: list[dict[str, str]] = [
        {
            "text": _ilabel(lang, "employer_not_named").format(
                agency=tag.company_name or "an agency"
            ),
            "evidence": "employer_kind.verdict",
        }
    ]
    for sentence in tag.descriptors[:4]:
        rows.append({"text": sentence, "evidence": _ilabel(lang, "quoted")})
    if not tag.descriptors:
        rows.append({"text": _ilabel(lang, "nothing_said"), "evidence": ""})

    # What *is* known, and is a fact about the assignment rather than about an
    # employer: the region, the contract as the posting states it, the working
    # arrangement.  These are the interview's real ground.
    for label_key, value, source in (
        ("location", opportunity.get("location") or vacancy.get("location"), "vacancy.location"),
        (
            "contract",
            opportunity.get("contract_type") or vacancy.get("contract_type"),
            "vacancy.contract_type",
        ),
        (
            "work_arrangement",
            opportunity.get("work_arrangement") or vacancy.get("work_arrangement"),
            "vacancy.work_arrangement",
        ),
    ):
        if value:
            rows.append(
                {"text": f"{label(lang, label_key)}: {value}", "evidence": source}
            )
    return rows, list(_questions(lang))


def _statements(value: Any) -> list[str]:
    """Composite and dream-job blocks are lists of ``{"id", "text"}`` statements."""
    out: list[str] = []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        text = value.get("text") or value.get("name") or value.get("statement")
        return [str(text)] if text else []
    for item in value or []:
        if isinstance(item, dict):
            text = (
                item.get("text")
                or item.get("name")
                or item.get("title")
                or item.get("description")
            )
            if text:
                out.append(str(text))
        elif str(item).strip():
            out.append(str(item))
    return out


def _evidence_pool(
    sections: dict, skills: list[dict], composite: dict, evidence: list[dict]
) -> list[tuple[set[str], str]]:
    pool: list[tuple[set[str], str]] = []
    for row in skills:
        text = str(row.get("normalised_label") or row.get("raw_label") or "")
        if not text:
            continue
        detail = text
        if row.get("years_experience"):
            detail = f"{text} ({row['years_experience']} yr)"
        pool.append((_terms(text), detail))
    for label_text in sections.get("top_skills") or []:
        pool.append((_terms(str(label_text)), str(label_text)))
    for entry in sections.get("experience") or []:
        text = " ".join(
            str(entry.get(k) or "") for k in ("title", "company", "description")
        )
        summary = f"{entry.get('title', '')} - {entry.get('company', '')}".strip(" -")
        pool.append((_terms(text), summary or text[:160]))
    for statement in _statements(composite.get("achievements")) + _statements(
        composite.get("core_competencies")
    ):
        pool.append((_terms(statement), statement))
    for item in evidence:
        text = f"{item.get('title', '')} {item.get('description', '')}"
        pool.append((_terms(text), str(item.get("title") or "")))
    return [entry for entry in pool if entry[0]]


def _best_evidence(
    requirement: str, pool: list[tuple[set[str], str]]
) -> tuple[str | None, float]:
    wanted = _terms(requirement)
    if not wanted:
        return None, 0.0
    best_text, best_score = None, 0.0
    for terms, text in pool:
        overlap = len(wanted & terms)
        if not overlap:
            continue
        score = overlap / len(wanted)
        if score > best_score:
            best_text, best_score = text, score
    return best_text, best_score


# ---------------------------------------------------------------------------
# LLM content
# ---------------------------------------------------------------------------


def _llm_content(
    inputs: dict[str, Any],
    lang: str,
    llm: LLMClient,
    instructions: str | None,
    tag: emp_mod.EmployerTag | None = None,
) -> dict[str, Any]:
    import json  # noqa: PLC0415 - only on the LLM path

    opportunity = inputs.get("opportunity") or {}
    company = inputs.get("company") or {}
    disclosed = tag is None or tag.employer_disclosed
    if not disclosed:
        # The prompt's hard rule 2 is "every statement about the company must be
        # traceable to the supplied company record".  That rule is exactly what
        # licensed a letter praising a staffing firm for its client's culture.
        # The fix is not a softer rule: it is to supply no company record.
        company = {
            "name": None,
            "note": (
                "The employer is NOT named in this posting. It was placed by "
                f"{(tag.company_name if tag else None) or 'an intermediary'}, an "
                "agency, on behalf of an employer it does not identify."
            ),
            "posting_says_about_employer": list(tag.descriptors) if tag else [],
        }
    composite = inputs.get("composite") or {}
    version = inputs.get("profile_version") or {}
    dream = inputs.get("dream_job") or {}

    prompt = load_prompt(PROMPT_NAME)
    # FR-106: a field flagged "do not disclose" must not leave the building, so
    # the payload sent to the model is redacted, not only the rendered document.
    sections = Disclosure(set(inputs.get("do_not_disclose") or set())).redact(
        version.get("sections") or {}
    )
    blocked = set(inputs.get("do_not_disclose") or set())
    composite_keys = ["career_trajectory", "core_competencies", "achievements",
                      "domains", "seniority", "constraints"]
    if "summary" not in blocked:
        # The narrative restates the summary; sending it while the summary is
        # suppressed would put the blocked text in the prompt anyway.
        composite_keys.insert(0, "narrative")
    profile_payload = {
        "summary": sections.get("summary"),
        "experience": sections.get("experience"),
        "education": sections.get("education"),
        "skills": [s.get("normalised_label") for s in inputs.get("skills") or []],
        "composite": {key: composite.get(key) for key in composite_keys},
        "dream_job_model": {
            key: dream.get(key)
            for key in ("statement", "target_roles", "responsibilities",
                        "company_characteristics", "culture_values", "deal_breakers")
        },
    }
    system, user = prompt.render(
        language=lang,
        role_title=opportunity.get("title") or "",
        company_name=company.get("name") or "",
        # The intermediary note travels through the same slot as the
        # speculative one: both say what this opening is not.
        speculative_note=_opening_note(opportunity, tag),
        profile_json=json.dumps(profile_payload, ensure_ascii=False, default=str)[:20_000],
        requirements_json=json.dumps(requirements(inputs), ensure_ascii=False),
    )
    if not disclosed:
        user += (
            "\n\nHARD RULE, overriding rule 2 above: the employer is NOT named. There is "
            "no company record. Make no statement about the employing organisation "
            "beyond what the posting itself says, name no company as the employer, and "
            "return an empty `why_fit_company`. Write about the role, the assignment "
            "and the questions to ask the recruiter instead."
        )
    if instructions:
        user += f"\n\nThe job seeker asked for this revision:\n{str(instructions)[:2000]}"

    return complete_json(
        llm,
        "generate.motivation",
        system,
        user,
        untrusted={
            "company": json.dumps(company, ensure_ascii=False, default=str)[:20_000],
            "opening": json.dumps(
                {"opportunity": opportunity, "vacancy": inputs.get("vacancy") or {}},
                ensure_ascii=False,
                default=str,
            )[:15_000],
        },
        entity_type="opportunity",
        entity_id=opportunity.get("id"),
        prompt_template=prompt.name,
        prompt_version=prompt.version,
        schema_hint=(
            '{"why_this_job": [{"text": str, "link": str}], '
            '"why_fit_job": [{"requirement": str, "evidence": str, "strength": str, '
            '"talking_point": str}], '
            '"why_fit_company": [{"text": str, "evidence": str}], '
            '"objections": [{"objection": str, "answer": str, "evidence": str}], '
            '"talking_points": [str]}'
        ),
    )


def _opening_note(opportunity: dict[str, Any], tag: emp_mod.EmployerTag | None) -> str:
    """What this opening is not, in one clause the prompt already has a slot for."""
    if tag is not None and not tag.employer_disclosed:
        return (
            f", posted by {tag.company_name or 'an agency'} on behalf of an employer "
            "it does not name"
        )
    if str(opportunity.get("kind")) == "speculative":
        return ", a speculative opening that has not been advertised"
    return ""


def _merge(
    derived: dict[str, Any], generated: dict[str, Any], *, employer_disclosed: bool = True
) -> dict[str, Any]:
    """Keep the model's prose, but never lose a requirement it skipped."""
    out = dict(derived)
    mergeable = ["why_this_job", "why_fit_company", "objections", "talking_points"]
    if not employer_disclosed:
        # A prompt instruction is a request; this is the guarantee.  Whatever
        # the model wrote about the employer is discarded, and the derived
        # blocks - the posting's own sentences and the recruiter questions -
        # stand.  Nothing generated can name a company nobody has identified.
        mergeable.remove("why_fit_company")
        out["why_fit_company"] = []
    for key in mergeable:
        if generated.get(key):
            out[key] = generated[key]

    rows = [r for r in generated.get("why_fit_job") or [] if isinstance(r, dict)]
    covered = {fold(r.get("requirement")) for r in rows}
    for row in derived["why_fit_job"]:
        if fold(row["requirement"]) not in covered:
            rows.append(row)
    for row in rows:
        if row.get("strength") not in STRENGTH_ORDER:
            row["strength"] = "partial"
    out["why_fit_job"] = sorted(rows, key=lambda r: STRENGTH_ORDER[r["strength"]])
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def generate_motivation(
    inputs: dict[str, Any],
    *,
    output_dir: Path | str,
    language: str | None = None,
    template: str | None = None,
    llm: LLMClient | None = None,
    instructions: str | None = None,
    basename: str = "motivation",
) -> MotivationResult:
    opportunity = inputs.get("opportunity") or {}
    seeker = inputs.get("seeker") or {}
    company = inputs.get("company") or {}
    lang = normalise_language(language or opportunity.get("language") or seeker.get("locale"))
    sections = TEMPLATES.get(template or DEFAULT_TEMPLATE, TEMPLATES[DEFAULT_TEMPLATE])

    tag = employer_tag(inputs)
    content = derive_content(inputs, lang, tag)
    notes: list[str] = []
    if not tag.employer_disclosed:
        notes.append(
            "The employer is not named in this posting; the company section is what "
            "the posting says plus the questions to ask the recruiter (FR-330)."
        )
    used_llm = False
    if llm is not None:
        try:
            content = _merge(
                content,
                _llm_content(inputs, lang, llm, instructions, tag) or {},
                employer_disclosed=tag.employer_disclosed,
            )
            used_llm = True
        except BudgetExhausted:
            notes.append("Token budget exhausted; the document is the derived mapping.")
        except (LLMError, ValueError, KeyError, TypeError) as exc:
            log.warning("Motivation generation failed: %s", exc)
            notes.append(f"Written without the model ({exc.__class__.__name__}).")
    else:
        notes.append("No LLM configured; requirements matched to the profile mechanically.")

    meta = meta_from_inputs(
        inputs,
        title=label(lang, "motivation_title"),
        subtitle=str(opportunity.get("title") or ""),
        language=lang,
        generated_at=utcnow(),
        seeker_only=True,
    )
    builder = PdfBuilder(meta)
    # The cover names the agency *as the agency*.  Putting its name where the
    # employer's belongs is the first sentence of the same mistake the rest of
    # this module exists to prevent.
    employer_line = (
        company.get("name")
        if tag.employer_disclosed
        else f"{tag.badge(lang)} ({company.get('name') or ''})".strip()
    )
    cover_page(
        builder,
        heading=label(lang, "motivation_title"),
        subheading=(
            f"{opportunity.get('title', '')} · {company.get('name', '')}".strip(" ·")
            if tag.employer_disclosed
            else f"{opportunity.get('title', '')} · {tag.badge(lang)}".strip(" ·")
        ),
        facts=[(label(lang, "motivation_subtitle"), employer_line)],
    )

    renderers = {
        "why_job": _why_job,
        "fit_job": _fit_job,
        "fit_company": _fit_company,
        "objections": _objections,
        "talking_points": _talking_points,
    }
    for name in sections:
        renderers[name](builder, content, lang)

    path = builder.build(Path(output_dir) / f"{basename}.pdf")
    return MotivationResult(
        path=str(path),
        template=template or DEFAULT_TEMPLATE,
        language=lang,
        generated_at=meta.generated_at,
        used_llm=used_llm,
        content=content,
        notes=notes,
    )


def _why_job(builder: PdfBuilder, content: dict, lang: str) -> None:
    builder.h1(label(lang, "why_this_job"))
    rows = content.get("why_this_job") or []
    if not rows:
        builder.note(label(lang, "not_available"))
        return
    for row in rows:
        builder.para(str(row.get("text") or ""))
        if row.get("link"):
            builder.note(f"{label(lang, 'dream_job_link')}: {_human_evidence(row['link'])}")


def _fit_job(builder: PdfBuilder, content: dict, lang: str) -> None:
    builder.h1(label(lang, "why_fit_job"))
    rows = content.get("why_fit_job") or []
    if not rows:
        builder.note(label(lang, "no_requirements_stated"))
        return
    builder.table(
        [label(lang, "requirement"), label(lang, "evidence"), ""],
        [
            [
                str(row.get("requirement") or ""),
                _human_evidence(row.get("evidence")),
                str(row.get("strength") or ""),
            ]
            for row in rows
        ],
        col_widths=[3.2, 5.4, 1.4],
    )
    points = [str(r.get("talking_point") or "") for r in rows if r.get("talking_point")]
    if points:
        builder.bullets(points[:8])


_EVIDENCE_PATH_RE = re.compile(
    r"\b(?:company|opening|vacancy|profile|job|requirement|dream(?:_fit(?:_detail)?|_job)?|"
    r"composite|evidence|skills?|signals?|directives?|persona|finding|source|search|"
    r"candidate|employer(?:_kind)?|role|location)\.[A-Za-z0-9_.]+",
    re.IGNORECASE,
)


def _human_evidence(text: Any) -> str:
    """Strip the generator's own field paths from a citation.

    The evidence strings look like ``company.name 'Acme NV'``.  That path is an
    internal key: it must not appear in the document a person reads, where
    ``company.name`` looks like a host, and the NFR-206 scan then read it as a
    leaked domain and hard-blocked the package.  The quoted value is kept.
    """
    cleaned = _EVIDENCE_PATH_RE.sub(" ", str(text or ""))
    return re.sub(r"\s{2,}", " ", cleaned).strip(" ;:")


def _fit_company(builder: PdfBuilder, content: dict, lang: str) -> None:
    """FR-330's third part - or its honest replacement.

    There is no version of "why I fit the company" that can be written about a
    company nobody has named.  What replaces it is not a shorter version of the
    same section: it is two different sections that are true.
    """
    if not content.get("employer_disclosed", True):
        _posting_says(builder, content, lang)
        return
    builder.h1(label(lang, "why_fit_company"))
    rows = content.get("why_fit_company") or []
    if not rows:
        builder.note(label(lang, "not_available"))
        return
    for row in rows:
        builder.para(str(row.get("text") or ""))
        if row.get("evidence"):
            builder.note(f"{label(lang, 'evidence')}: {_human_evidence(row['evidence'])}")


def _posting_says(builder: PdfBuilder, content: dict, lang: str) -> None:
    """What the posting says about the employer, and what to ask the recruiter."""
    builder.h1(_ilabel(lang, "posting_says"))
    for row in content.get("posting_says_about_employer") or []:
        builder.para(str(row.get("text") or ""))
        if row.get("evidence"):
            builder.note(f"{label(lang, 'evidence')}: {_human_evidence(row['evidence'])}")

    questions = [str(q) for q in content.get("recruiter_questions") or [] if str(q).strip()]
    if not questions:
        return
    builder.h1(_ilabel(lang, "recruiter_questions"))
    builder.para(_ilabel(lang, "questions_intro"))
    builder.bullets(questions)


def _objections(builder: PdfBuilder, content: dict, lang: str) -> None:
    builder.h1(label(lang, "objections"))
    rows = content.get("objections") or []
    if not rows:
        builder.note(label(lang, "not_available"))
        return
    for row in rows:
        builder.keep_together(lambda sub, row=row: _objection_block(sub, row, lang))


def _objection_block(sub: PdfBuilder, row: dict, lang: str) -> None:
    sub.h2(f"{label(lang, 'objection')}: {row.get('objection', '')}")
    answer = str(row.get("answer") or "").strip()
    sub.para(
        f"{label(lang, 'answer')}: {answer}" if answer else label(lang, "prepare_answer")
    )
    if row.get("evidence"):
        sub.note(f"{label(lang, 'evidence')}: {_human_evidence(row['evidence'])}")


def _talking_points(builder: PdfBuilder, content: dict, lang: str) -> None:
    builder.h1(label(lang, "talking_points"))
    points = [str(p) for p in content.get("talking_points") or [] if str(p).strip()]
    if not points:
        builder.note(label(lang, "not_available"))
        return
    builder.bullets(points)
