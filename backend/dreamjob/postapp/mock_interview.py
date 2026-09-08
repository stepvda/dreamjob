"""Interactive mock interview per opportunity (FR-424, NFR-205, NFR-305).

A turn-based conversation the interface can render as a chat: ``start`` opens
the session and returns the first question, ``answer`` judges what was said
and returns the next one, ``finish`` closes with the weak spots to rehearse.
Every turn is written to ``mock_interview_session`` as it happens, so a
session survives a reload and a repeat round can open on what went badly last
time (FR-424: "stored and repeatable").

The interviewer is the model, briefed with the company and job briefing
(FR-329) and judging against the motivation and fit document (FR-330).  Both
documents are scraped-and-generated material, so they reach the model only
inside ``untrusted`` blocks (NFR-205).

Without a model the session still runs.  A question bank built from the
vacancy's own required skills asks role-specific questions, the behavioural
questions are the standard ones, and the feedback is structural rather than
semantic - it reports what a strong answer contains and what this answer was
missing (a number, a situation, an outcome), which is worth having and is
honestly labelled as not being the model's judgement.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from dreamjob.db.connection import from_json, to_json, utcnow
from dreamjob.db.repositories import pipeline_cards as repo
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError
from dreamjob.pipeline.enrichment import load_prompt
from dreamjob.security.audit import record_audit

log = logging.getLogger(__name__)

PROMPT_NAME = "mock_interview"

#: Long enough to find weak spots, short enough that a seeker finishes it.
DEFAULT_QUESTION_COUNT = 8
MIN_QUESTIONS = 3
MAX_QUESTIONS = 20

FOCUS_CHOICES = ("mixed", "role", "behavioural")


class SessionClosed(RuntimeError):
    """Raised when a finished session is asked for another turn."""


# ---------------------------------------------------------------------------
# The offline question bank
# ---------------------------------------------------------------------------

_BEHAVIOURAL: dict[str, list[str]] = {
    "en": [
        "Tell me about a time you had to deliver something with less than you needed. "
        "What did you cut, and what happened?",
        "Describe a disagreement with a colleague or a stakeholder that you had to resolve.",
        "What is the piece of work you are proudest of, and what makes it stand out?",
        "Tell me about something that failed. What did you change afterwards?",
        "How do you decide what not to do when everything is urgent?",
    ],
    "nl": [
        "Vertel over een moment waarop u iets moest opleveren met te weinig middelen. "
        "Wat liet u vallen, en wat gebeurde er?",
        "Beschrijf een meningsverschil met een collega dat u hebt moeten oplossen.",
        "Op welk werk bent u het meest trots, en waarom springt het eruit?",
        "Vertel over iets dat is mislukt. Wat hebt u daarna veranderd?",
        "Hoe beslist u wat u niet doet wanneer alles dringend is?",
    ],
    "fr": [
        "Parlez-moi d'une fois ou vous avez du livrer avec moins de moyens que necessaire. "
        "Qu'avez-vous abandonne, et avec quel resultat ?",
        "Decrivez un desaccord avec un collegue que vous avez du resoudre.",
        "De quel travail etes-vous le plus fier, et qu'est-ce qui le distingue ?",
        "Parlez-moi d'un echec. Qu'avez-vous change ensuite ?",
        "Comment decidez-vous ce que vous ne ferez pas quand tout est urgent ?",
    ],
    "de": [
        "Erzaehlen Sie von einer Situation, in der Sie mit weniger Mitteln liefern mussten. "
        "Was haben Sie gestrichen, und was ist passiert?",
        "Beschreiben Sie eine Meinungsverschiedenheit, die Sie loesen mussten.",
        "Auf welche Arbeit sind Sie am stolzesten, und was hebt sie hervor?",
        "Erzaehlen Sie von etwas, das gescheitert ist. Was haben Sie danach geaendert?",
        "Wie entscheiden Sie, was Sie nicht tun, wenn alles dringend ist?",
    ],
}

_ROLE_TEMPLATES: dict[str, str] = {
    "en": "The role asks for {skill}. Take me through the most demanding thing you have "
          "done with it, and what you were responsible for.",
    "nl": "De functie vraagt {skill}. Neem me mee door het lastigste dat u daarmee hebt "
          "gedaan, en waarvoor u verantwoordelijk was.",
    "fr": "Le poste demande {skill}. Racontez-moi la chose la plus exigeante que vous avez "
          "faite avec, et ce dont vous etiez responsable.",
    "de": "Die Stelle verlangt {skill}. Schildern Sie das Anspruchsvollste, das Sie damit "
          "gemacht haben, und wofuer Sie verantwortlich waren.",
}

_MOTIVATION: dict[str, str] = {
    "en": "Why this company, and why now? Be concrete about what drew you to {company}.",
    "nl": "Waarom dit bedrijf, en waarom nu? Wees concreet over wat u aantrok in {company}.",
    "fr": "Pourquoi cette entreprise, et pourquoi maintenant ? Soyez concret sur ce qui "
          "vous attire chez {company}.",
    "de": "Warum dieses Unternehmen, und warum jetzt? Werden Sie konkret, was Sie an "
          "{company} reizt.",
}

_CLOSING: dict[str, str] = {
    "en": "What would you want to know about the team before you said yes?",
    "nl": "Wat zou u over het team willen weten voor u ja zegt?",
    "fr": "Que voudriez-vous savoir sur l'equipe avant de dire oui ?",
    "de": "Was moechten Sie ueber das Team wissen, bevor Sie zusagen?",
}


def _skills_from(context: dict[str, Any]) -> list[str]:
    """Required skills of the opportunity, then of the vacancy behind it."""
    for key in ("required_skills", "vacancy_required", "desirable_skills"):
        value = context.get(key)
        if isinstance(value, str):
            value = from_json(value, None)
        if isinstance(value, list) and value:
            return [str(v)[:80] for v in value if str(v).strip()][:8]
    text = " ".join(
        str(context.get(k) or "") for k in ("description", "vacancy_description")
    )
    return [w for w in re.findall(r"\b[A-Z][A-Za-z+#.]{2,}\b", text)][:6]


def build_plan(
    context: dict[str, Any],
    *,
    language: str = "en",
    focus: str = "mixed",
    count: int = DEFAULT_QUESTION_COUNT,
    previous_weak_spots: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """The question plan: role-specific and behavioural, in the right mix (FR-424)."""
    count = max(MIN_QUESTIONS, min(MAX_QUESTIONS, count))
    language = language if language in _BEHAVIOURAL else "en"
    company = context.get("company_name") or "this company"
    skills = _skills_from(context)
    behavioural = list(_BEHAVIOURAL[language])
    role_template = _ROLE_TEMPLATES[language]

    plan: list[dict[str, Any]] = [
        {"kind": "motivation", "question": _MOTIVATION[language].format(company=company)}
    ]
    for spot in (previous_weak_spots or [])[:2]:
        topic = str(spot.get("topic") or "").strip()
        if topic:
            plan.append(
                {
                    "kind": "challenge",
                    "question": role_template.format(skill=topic),
                    "from_previous_round": True,
                }
            )

    role_questions = [{"kind": "role", "question": role_template.format(skill=s)} for s in skills]
    behaviour_questions = [{"kind": "behavioural", "question": q} for q in behavioural]
    if focus == "role":
        tail = role_questions + behaviour_questions
    elif focus == "behavioural":
        tail = behaviour_questions + role_questions
    else:
        tail = [q for pair in zip(role_questions, behaviour_questions, strict=False) for q in pair]
        tail += role_questions[len(behaviour_questions):] + behaviour_questions[
            len(role_questions):
        ]

    plan.extend(tail)
    plan = plan[: count - 1]
    plan.append({"kind": "practical", "question": _CLOSING[language]})
    return plan[:count]


# ---------------------------------------------------------------------------
# Offline feedback: structural, and honest about what it is
# ---------------------------------------------------------------------------

_STAR_MARKERS = (
    "when", "we", "i led", "i built", "i decided", "result", "because",
    "toen", "wij", "resultaat", "omdat",
    "quand", "nous", "resultat", "parce que",
    "als", "wir", "ergebnis", "weil",
)


def structural_feedback(answer: str, *, language: str = "en") -> dict[str, Any]:
    """What a strong answer contains, and what this one is missing.

    Deliberately not a judgement of content - without the model there is
    nothing to judge content against, and pretending otherwise would be worse
    than saying so.
    """
    text = (answer or "").strip()
    words = len(text.split())
    has_number = bool(re.search(r"\d", text))
    has_situation = any(m in text.lower() for m in _STAR_MARKERS)
    has_outcome = bool(
        re.search(r"\b(result|outcome|led to|resultaat|resultat|ergebnis|impact)\b", text, re.I)
    )

    strengths, gaps = [], []
    (strengths if words >= 60 else gaps).append(
        "the answer has enough substance to work with" if words >= 60
        else "the answer is short; an interviewer would ask you to expand"
    )
    (strengths if has_situation else gaps).append(
        "it starts from a concrete situation" if has_situation
        else "it does not start from a specific situation you were in"
    )
    (strengths if has_number else gaps).append(
        "it contains a figure, which makes it checkable" if has_number
        else "there is no number in it: size, duration, team, budget or effect"
    )
    (strengths if has_outcome else gaps).append(
        "it names an outcome" if has_outcome else "it does not say how it ended"
    )
    score = 1 + sum([words >= 60, has_situation, has_number, has_outcome])
    return {
        "score": score,
        "strengths": strengths,
        "gaps": gaps,
        "improved_answer": None,
        "follow_up_needed": score < 4,
        "method": "structural",
        "note": (
            "Generated without the language model: this checks the shape of the answer "
            "(situation, figure, outcome), not what it says."
        ),
    }


def weak_spots_from(transcript: list[dict], *, language: str = "en") -> list[dict[str, Any]]:
    """The offline closing list: the questions that scored worst (FR-424)."""
    scored = [
        t for t in transcript if t.get("feedback") and t["feedback"].get("score") is not None
    ]
    weakest = sorted(scored, key=lambda t: t["feedback"]["score"])[:4]
    out = []
    for turn in weakest:
        gaps = turn["feedback"].get("gaps") or []
        out.append(
            {
                "topic": (turn.get("question") or "")[:120],
                "why": "; ".join(gaps[:2]) or "the answer was thin",
                "rehearse": (
                    "Rewrite this answer as one situation, one action you took, one number "
                    "and one outcome, then say it out loud in under ninety seconds."
                ),
                "severity": "high" if turn["feedback"]["score"] <= 2 else "medium",
            }
        )
    return out


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


@dataclass
class Turn:
    index: int
    kind: str
    question: str
    rationale: str = ""
    answer: str | None = None
    feedback: dict[str, Any] | None = None
    asked_at: str = ""
    answered_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kind": self.kind,
            "question": self.question,
            "rationale": self.rationale,
            "answer": self.answer,
            "feedback": self.feedback,
            "asked_at": self.asked_at,
            "answered_at": self.answered_at,
        }


@dataclass
class Session:
    id: str
    opportunity_id: str
    status: str
    language: str
    round_number: int
    turns: list[Turn] = field(default_factory=list)
    plan: list[dict] = field(default_factory=list)
    weak_spots: list[dict] = field(default_factory=list)
    summary: str = ""
    generated_by: str = "template"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "opportunity_id": self.opportunity_id,
            "status": self.status,
            "language": self.language,
            "round_number": self.round_number,
            "turns": [t.as_dict() for t in self.turns],
            "questions_planned": len(self.plan),
            "questions_asked": len(self.turns),
            "weak_spots": self.weak_spots,
            "summary": self.summary,
            "generated_by": self.generated_by,
            "current_question": self.turns[-1].question if self.turns else None,
            "awaiting_answer": bool(self.turns and self.turns[-1].answer is None),
        }


def _load(job_seeker_id: str, session_id: str) -> tuple[dict, Session]:
    row = repo.get_session(session_id, job_seeker_id)
    if row is None:
        raise LookupError(f"No mock interview session {session_id} for this job seeker")
    transcript = row.get("transcript") or []
    return row, Session(
        id=row["id"],
        opportunity_id=row["opportunity_id"],
        status=row["status"],
        language=row.get("language") or "en",
        round_number=int(row.get("round_number") or 1),
        turns=[Turn(**{k: v for k, v in t.items() if k in Turn.__annotations__})
               for t in transcript],
        plan=row.get("question_plan") or [],
        weak_spots=row.get("weak_spots") or [],
        summary=row.get("summary") or "",
        generated_by=row.get("generated_by") or "template",
    )


def _save(session: Session, extra: dict[str, Any] | None = None) -> None:
    repo.update_session(
        session.id,
        {
            "transcript": to_json([t.as_dict() for t in session.turns]),
            "feedback": to_json(
                [{"index": t.index, **(t.feedback or {})} for t in session.turns if t.feedback]
            ),
            "weak_spots": to_json(session.weak_spots),
            "question_plan": to_json(session.plan),
            "summary": session.summary or None,
            "status": session.status,
            "generated_by": session.generated_by,
            **(extra or {}),
        },
    )


# ---------------------------------------------------------------------------
# The model turn
# ---------------------------------------------------------------------------


def _untrusted_blocks(
    context: dict[str, Any],
    package: dict[str, Any] | None,
    profile: dict[str, Any] | None,
    session: Session,
    *,
    final: bool,
    previous: list[dict],
) -> dict[str, str]:
    from dreamjob.postapp.reply_classifier import _document_excerpt  # noqa: PLC0415

    blocks = {
        "role": to_json(
            {
                "title": context.get("title"),
                "company": context.get("company_name"),
                "kind": context.get("kind"),
                "seniority": context.get("seniority"),
                "description": (context.get("description") or "")[:4000],
                "required_skills": context.get("required_skills"),
                "vacancy_description": (context.get("vacancy_description") or "")[:4000],
            }
        )
        or "{}",
        "transcript": to_json([t.as_dict() for t in session.turns[-6:]]) or "[]",
        "plan": to_json({"questions": session.plan, "final": final}) or "{}",
        "previous_weak_spots": to_json(previous) or "[]",
    }
    briefing = _document_excerpt((package or {}).get("briefing_pdf_path"))
    motivation = _document_excerpt((package or {}).get("motivation_pdf_path"))
    if briefing:
        blocks["briefing"] = briefing
    if motivation:
        blocks["motivation"] = motivation
    if profile:
        blocks["seeker"] = to_json(profile) or "{}"
    return blocks


def _model_turn(
    session: Session,
    context: dict[str, Any],
    package: dict[str, Any] | None,
    profile: dict[str, Any] | None,
    *,
    llm: LLMClient,
    final: bool,
    previous: list[dict],
) -> dict[str, Any] | None:
    template = load_prompt(PROMPT_NAME)
    system, user = template.render(
        language=session.language,
        turn=len(session.turns) + (0 if final else 1),
        total=len(session.plan),
    )
    try:
        return llm.complete_json(
            "interview.mock",
            system=system,
            user=user,
            untrusted=_untrusted_blocks(
                context, package, profile, session, final=final, previous=previous
            ),
            entity_type="mock_interview_session",
            entity_id=session.id,
            prompt_template=f"{template.name}.md",
            prompt_version=template.version,
            temperature=0.6,
            max_tokens=1600,
        )
    except (BudgetExhausted, LLMError) as exc:
        log.info(
            "Mock interview turn %d degraded to the question bank: %s",
            len(session.turns), exc,
        )
        return None


# ---------------------------------------------------------------------------
# Public API: start / answer / finish
# ---------------------------------------------------------------------------


def start(
    job_seeker_id: str,
    opportunity_id: str,
    *,
    language: str | None = None,
    focus: str = "mixed",
    questions: int = DEFAULT_QUESTION_COUNT,
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    """Open a session and return the first question (FR-424)."""
    context = repo.opportunity_context(opportunity_id, job_seeker_id)
    if context is None:
        raise LookupError(f"No opportunity {opportunity_id} for this job seeker")
    if focus not in FOCUS_CHOICES:
        raise ValueError(f"focus must be one of {', '.join(FOCUS_CHOICES)}")

    seeker = repo.seeker(job_seeker_id) or {}
    lang = (language or context.get("language") or seeker.get("locale") or "en")[:2]
    previous = [
        spot
        for row in repo.weak_spot_history(job_seeker_id, opportunity_id, limit=3)
        for spot in (row.get("weak_spots") or [])
    ]
    plan = build_plan(
        context, language=lang, focus=focus, count=questions, previous_weak_spots=previous
    )
    round_number = repo.next_round_number(job_seeker_id, opportunity_id)

    session_id = repo.create_session(
        job_seeker_id,
        {
            "opportunity_id": opportunity_id,
            "status": "open",
            "language": lang,
            "round_number": round_number,
            "focus": focus,
            "question_plan": to_json(plan),
            "transcript": to_json([]),
            "generated_by": "llm" if llm else "template",
        },
    )
    session = Session(
        id=session_id,
        opportunity_id=opportunity_id,
        status="open",
        language=lang,
        round_number=round_number,
        plan=plan,
        generated_by="llm" if llm else "template",
    )

    question, kind, rationale = plan[0]["question"], plan[0]["kind"], ""
    if llm is not None:
        package = repo.package_for_opportunity(opportunity_id, job_seeker_id)
        profile = _profile_blocks(repo.composite_profile(job_seeker_id))
        payload = _model_turn(
            session, context, package, profile, llm=llm, final=False, previous=previous
        )
        if payload and payload.get("question"):
            question = str(payload["question"])[:1000]
            kind = str(payload.get("question_kind") or kind)[:30]
            rationale = str(payload.get("rationale") or "")[:400]
        else:
            session.generated_by = "template"

    session.turns.append(
        Turn(index=0, kind=kind, question=question, rationale=rationale, asked_at=utcnow())
    )
    _save(session)
    record_audit(
        "mock_interview.started",
        "mock_interview_session",
        session_id,
        seeker_id=job_seeker_id,
        detail={"opportunity_id": opportunity_id, "round": round_number, "focus": focus},
    )
    return session.as_dict()


def answer(
    job_seeker_id: str, session_id: str, text: str, *, llm: LLMClient | None = None
) -> dict[str, Any]:
    """Record an answer, give feedback on it and ask the next question (FR-424)."""
    row, session = _load(job_seeker_id, session_id)
    if session.status != "open":
        raise SessionClosed(f"Session {session_id} is {session.status}")
    if not session.turns or session.turns[-1].answer is not None:
        raise SessionClosed("There is no open question to answer")

    current = session.turns[-1]
    current.answer = (text or "").strip()
    current.answered_at = utcnow()

    context = repo.opportunity_context(session.opportunity_id, job_seeker_id) or {}
    package = repo.package_for_opportunity(session.opportunity_id, job_seeker_id)
    profile = _profile_blocks(repo.composite_profile(job_seeker_id))
    previous = [
        spot
        for r in repo.weak_spot_history(job_seeker_id, session.opportunity_id, limit=3)
        for spot in (r.get("weak_spots") or [])
    ]
    is_last = len(session.turns) >= len(session.plan)

    payload = None
    if llm is not None:
        payload = _model_turn(
            session, context, package, profile, llm=llm, final=is_last, previous=previous
        )

    if payload and isinstance(payload.get("feedback"), dict):
        current.feedback = _clean_feedback(payload["feedback"])
    else:
        current.feedback = structural_feedback(current.answer, language=session.language)

    if is_last:
        _close(session, payload)
    else:
        index = len(session.turns)
        planned = session.plan[index] if index < len(session.plan) else session.plan[-1]
        question = str((payload or {}).get("question") or planned["question"])[:1000]
        kind = str((payload or {}).get("question_kind") or planned["kind"])[:30]
        session.turns.append(
            Turn(
                index=index,
                kind=kind,
                question=question,
                rationale=str((payload or {}).get("rationale") or "")[:400],
                asked_at=utcnow(),
            )
        )

    _save(session, {"finished_at": utcnow()} if session.status == "finished" else None)
    return session.as_dict()


def _clean_feedback(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        score = max(0, min(5, int(round(float(payload.get("score", 0))))))
    except (TypeError, ValueError):
        score = 0
    return {
        "score": score,
        "strengths": [str(s)[:300] for s in (payload.get("strengths") or [])][:5],
        "gaps": [str(s)[:300] for s in (payload.get("gaps") or [])][:5],
        "improved_answer": (
            str(payload["improved_answer"])[:2000] if payload.get("improved_answer") else None
        ),
        "follow_up_needed": bool(payload.get("follow_up_needed", False)),
        "method": "llm",
    }


def _close(session: Session, payload: dict[str, Any] | None) -> None:
    spots = (payload or {}).get("weak_spots")
    if isinstance(spots, list) and spots:
        session.weak_spots = [
            {
                "topic": str(s.get("topic") or "")[:200],
                "why": str(s.get("why") or "")[:400],
                "rehearse": str(s.get("rehearse") or "")[:400],
                "severity": str(s.get("severity") or "medium")[:10],
            }
            for s in spots[:6]
            if isinstance(s, dict)
        ]
    else:
        session.weak_spots = weak_spots_from(
            [t.as_dict() for t in session.turns], language=session.language
        )
    session.summary = str((payload or {}).get("summary") or "")[:2000] or _offline_summary(session)
    session.status = "finished"


def _offline_summary(session: Session) -> str:
    scored = [t.feedback["score"] for t in session.turns if t.feedback]
    if not scored:
        return "The session was closed before any answer was given."
    average = sum(scored) / len(scored)
    return (
        f"{len(scored)} answers given, averaging {average:.1f} out of 5 on structure "
        f"(a concrete situation, a figure, an outcome). The weak spots below are the "
        f"answers that scored lowest; rehearsing them is the fastest gain."
    )


def finish(
    job_seeker_id: str, session_id: str, *, llm: LLMClient | None = None
) -> dict[str, Any]:
    """End a session early and produce the weak spots from what was said (FR-424)."""
    row, session = _load(job_seeker_id, session_id)
    if session.status == "finished":
        return session.as_dict()
    if session.turns and session.turns[-1].answer is None:
        session.turns.pop()  # an unanswered question is not part of the transcript

    payload = None
    if llm is not None and session.turns:
        context = repo.opportunity_context(session.opportunity_id, job_seeker_id) or {}
        package = repo.package_for_opportunity(session.opportunity_id, job_seeker_id)
        profile = _profile_blocks(repo.composite_profile(job_seeker_id))
        payload = _model_turn(
            session, context, package, profile, llm=llm, final=True, previous=[]
        )
    _close(session, payload)
    _save(session, {"finished_at": utcnow()})
    record_audit(
        "mock_interview.finished",
        "mock_interview_session",
        session_id,
        seeker_id=job_seeker_id,
        detail={"turns": len(session.turns), "weak_spots": len(session.weak_spots)},
    )
    return session.as_dict()


def get(job_seeker_id: str, session_id: str) -> dict[str, Any]:
    _, session = _load(job_seeker_id, session_id)
    return session.as_dict()


def history(job_seeker_id: str, *, opportunity_id: str | None = None) -> list[dict]:
    return repo.list_sessions(job_seeker_id, opportunity_id=opportunity_id)


def _profile_blocks(profile: dict | None) -> dict[str, Any] | None:
    """The parts of the composite profile an interviewer would have read."""
    if not profile:
        return None
    return {
        "narrative": profile.get("narrative"),
        "career_trajectory": from_json(profile.get("career_trajectory"), None),
        "core_competencies": from_json(profile.get("core_competencies"), None),
        "achievements": from_json(profile.get("achievements"), None),
        "seniority": profile.get("seniority"),
    }
