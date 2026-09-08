"""Classify incoming replies and draft the answer (FR-422, NFR-205, NFR-305).

Two steps, deliberately separable.  Classification decides what kind of answer
arrived - interest, a request for information, an interview invitation, a
rejection, a referral, or a machine talking - and drafting writes the response
that goes back **in the same thread**, using the interview briefing (FR-329)
and the motivation and fit document (FR-330) as context.

Three properties are load-bearing:

* **Nothing is ever sent.**  This module produces a ``reply_draft`` row with
  status ``draft``.  Approving it is a separate act by the job seeker and
  sending it belongs to the mail slice (NFR-305).
* **The reply body is untrusted.**  It arrived from outside and is passed to
  the model only inside an ``untrusted`` block, never concatenated into the
  instruction (NFR-205).  The same treatment is given to the briefing and
  motivation extracts, which were built from scraped material.
* **It works without the model.**  Every LLM step has a deterministic
  fallback: a multilingual keyword classifier and a template drafter.  That is
  not a stub - a rejection arriving while the token budget is exhausted still
  has to move the card and still has to produce something to send back.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from dreamjob.db.connection import to_json, utcnow
from dreamjob.db.repositories import pipeline_cards as repo
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError
from dreamjob.pipeline.enrichment import load_prompt
from dreamjob.security.audit import record_audit

log = logging.getLogger(__name__)

CLASSIFICATION_PROMPT = "reply_classification"
DRAFT_PROMPT = "reply_draft"

#: The six classes of FR-422, plus ``other`` for a human reply that is none of
#: them.  ``offer`` is not one: an offer arrives as an attachment or a meeting,
#: and the seeker moves that card by hand.
CLASSES: tuple[str, ...] = (
    "interview_invitation",
    "interest",
    "request_for_information",
    "rejection",
    "referral",
    "automatic_reply",
    "other",
)

#: How much of a document is worth showing the model as context.
DOCUMENT_EXCERPT_CHARS = 6000
BODY_EXCERPT_CHARS = 8000


# ---------------------------------------------------------------------------
# Deterministic classifier (the fallback, and the check on the model)
# ---------------------------------------------------------------------------


def _fold(text: str) -> str:
    """Lowercase and strip accents, so 'refusé' and 'refuse' match one rule."""
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _rules(*fragments: str) -> re.Pattern[str]:
    return re.compile("|".join(fragments))


#: Phrase rules in the four supported languages, strongest class first.  The
#: order matters: a rejection that also mentions an interview is a rejection.
_RULES: list[tuple[str, re.Pattern[str], float]] = [
    (
        "automatic_reply",
        _rules(
            r"\bout of (the )?office\b", r"\bautomatic(al)? repl(y|ies)\b", r"\bauto-?reply\b",
            r"\bafwezigheidsassistent\b", r"\bniet aanwezig\b", r"\bautomatisch antwoord\b",
            r"\bmessage automatique\b", r"\babsence du bureau\b", r"\bje suis absent\b",
            r"\babwesenheitsnotiz\b", r"\bautomatische antwort\b", r"\bnicht im buro\b",
            r"\bdo not reply\b", r"\bne pas repondre\b", r"\bniet beantwoorden\b",
            r"\bwe have received your application\b", r"\bwij hebben uw sollicitatie ontvangen\b",
            r"\bnous avons bien recu votre candidature\b",
            r"\bihre bewerbung ist bei uns eingegangen\b",
        ),
        0.85,
    ),
    (
        "rejection",
        _rules(
            r"\bunfortunately\b.{0,60}\b(not|other|another|unable)\b",
            r"\bwe (have )?(decided|chosen) to (proceed|continue|move forward) "
            r"with (another|other)",
            r"\bnot (been )?(selected|shortlisted|successful)\b",
            r"\bwill not be (taking|moving|proceeding)\b",
            r"\bhelaas\b", r"\bniet weerhouden\b", r"\bnegatief\b",
            r"\bandere kandidaat\b", r"\bgeen vervolg\b",
            r"\bmalheureusement\b", r"\bnous ne donnerons pas suite\b",
            r"\bun autre candidat\b", r"\bcandidature n'a pas ete retenue\b",
            r"\bleider\b", r"\bnicht berucksichtigen\b", r"\babsage\b",
            r"\bfur eine andere kandidatin?\b", r"\bnicht weiter\b",
        ),
        0.8,
    ),
    (
        "interview_invitation",
        _rules(
            r"\b(invite|inviting|invitation) (you )?(to|for) (an?|the) "
            r"(interview|meeting|call|chat)",
            r"\b(would|are) you (be )?available\b",
            r"\bschedule (a|an|the) (call|meeting|interview)",
            r"\bbook (a|an|the) (call|slot|meeting)\b", r"\blet'?s (meet|talk|speak)\b",
            r"\bkennismaking(sgesprek)?\b", r"\buitnodigen\b", r"\buitnodiging\b",
            r"\bgesprek (in )?te plannen\b", r"\bwanneer (ben je|bent u) beschikbaar\b",
            r"\bentretien\b", r"\bvous rencontrer\b", r"\bfixer un rendez-vous\b",
            r"\bdisponibilites?\b", r"\bvous convient\b",
            r"\bvorstellungsgesprach\b", r"\bkennenlernen\b", r"\btermin\b",
            r"\bwann (waren|sind) sie verfugbar\b",
        ),
        0.75,
    ),
    (
        "referral",
        _rules(
            r"\bi am not the right person\b",
            r"\bforward(ed|ing)? (your|this) (mail|e-?mail|message) to\b",
            r"\byou (should|can|could) (contact|reach out to|speak to)\b",
            r"\bmy colleague\b", r"\bin copy\b", r"\bin cc\b",
            r"\bniet de juiste persoon\b", r"\bcollega\b.{0,40}\bcontact\b",
            r"\bdoorgestuurd\b", r"\bneem contact op met\b",
            r"\bje ne suis pas la bonne personne\b", r"\bveuillez contacter\b",
            r"\bje transmets\b", r"\bmon collegue\b",
            r"\bnicht die richtige (person|ansprechpartnerin?)\b",
            r"\bwenden sie sich an\b", r"\bmeine kollegin?\b",
        ),
        0.65,
    ),
    (
        "request_for_information",
        _rules(
            r"\b(could|can) you (please )?(send|share|provide|confirm)\b",
            r"\bwe (would )?(need|require)\b", r"\bsalary expectation", r"\bnotice period\b",
            r"\bplease (send|complete|fill)\b", r"\breferences\b",
            r"\bkun(t) (je|u) (ons )?(bezorgen|sturen|bevestigen)\b", r"\bloonverwachting\b",
            r"\bopzegtermijn\b", r"\bgelieve\b.{0,40}\b(te bezorgen|door te sturen)\b",
            r"\bpourriez-vous (nous )?(envoyer|transmettre|confirmer)\b",
            r"\bpretentions salariales\b", r"\bpreavis\b",
            r"\bkonnten sie (uns )?(senden|zusenden|bestatigen)\b",
            r"\bgehaltsvorstellung\b", r"\bkundigungsfrist\b",
        ),
        0.6,
    ),
    (
        "interest",
        _rules(
            r"\b(interesting|interested) (profile|in your (profile|application))\b",
            r"\bthank you for your (application|interest)\b.{0,120}"
            r"\b(will|shall) (be in touch|contact|come back)\b",
            r"\bpassed (it |your (cv|profile|application) )?on to\b",
            r"\bkeep (you|your (cv|profile)) (in mind|on file)\b",
            r"\binteressant profiel\b", r"\bwe nemen contact op\b", r"\bin ons bestand\b",
            r"\bdoorgegeven aan\b",
            r"\bprofil interessant\b", r"\bnous reviendrons vers vous\b",
            r"\bnous vous recontacterons\b", r"\btransmis a\b",
            r"\binteressantes profil\b", r"\bwir melden uns\b", r"\bin unserer datenbank\b",
        ),
        0.55,
    ),
]

#: Language detection is only needed to pick the drafting language, so a small
#: stop-word count is enough and never leaves the machine.
_LANGUAGE_MARKERS: dict[str, tuple[str, ...]] = {
    "nl": ("het", "een", "wij", "met", "voor", "graag", "sollicitatie", "vriendelijke groeten"),
    "fr": ("nous", "vous", "votre", "cordialement", "candidature", "merci", "entretien"),
    "de": ("wir", "ihre", "sehr geehrte", "freundlichen", "bewerbung", "mit freundlichen"),
    "en": ("the", "we", "your", "kind regards", "application", "thank you", "best regards"),
}


def detect_language(text: str, default: str = "en") -> str:
    folded = f" {_fold(text)} "
    scores = {
        code: sum(folded.count(f" {marker} ") for marker in markers)
        for code, markers in _LANGUAGE_MARKERS.items()
    }
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] else default


def classify_heuristically(subject: str | None, body: str | None) -> dict[str, Any]:
    """Rule-based classification: the fallback, in four languages (FR-422)."""
    text = _fold(f"{subject or ''}\n{body or ''}")
    for label, pattern, confidence in _RULES:
        match = pattern.search(text)
        if match:
            return {
                "classification": label,
                "confidence": confidence,
                "reason": f"matched the phrase {match.group(0)!r}",
                "language": detect_language(f"{subject or ''} {body or ''}"),
                "proposed_times": _time_phrases(body or ""),
                "method": "rules",
            }
    return {
        "classification": "other",
        "confidence": 0.3,
        "reason": "no decisive phrase found; a person should read this one",
        "language": detect_language(f"{subject or ''} {body or ''}"),
        "proposed_times": _time_phrases(body or ""),
        "method": "rules",
    }


_TIME_PHRASE = re.compile(
    r"[^.\n]*\b("
    r"\d{1,2}[:/h.]\d{2}"
    r"|monday|tuesday|wednesday|thursday|friday"
    r"|maandag|dinsdag|woensdag|donderdag|vrijdag"
    r"|lundi|mardi|mercredi|jeudi|vendredi"
    r"|montag|dienstag|mittwoch|donnerstag|freitag"
    r"|tomorrow|morgen|demain"
    r"|next week|volgende week|la semaine prochaine|nachste woche"
    r")\b[^.\n]*",
    re.IGNORECASE,
)


def _time_phrases(body: str) -> list[str]:
    """Sentences that mention a day or a time, for FR-423 to resolve."""
    return [m.group(0).strip() for m in _TIME_PHRASE.finditer(_fold(body))][:8]


# ---------------------------------------------------------------------------
# LLM classification
# ---------------------------------------------------------------------------


@dataclass
class Classification:
    classification: str
    confidence: float
    reason: str = ""
    language: str = "en"
    sentiment: str = "neutral"
    proposed_times: list[str] = field(default_factory=list)
    requested_items: list[str] = field(default_factory=list)
    referred_to: dict[str, Any] | None = None
    deadline: str | None = None
    requires_response: bool = True
    method: str = "llm"

    def as_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "confidence": self.confidence,
            "reason": self.reason,
            "language": self.language,
            "sentiment": self.sentiment,
            "proposed_times": self.proposed_times,
            "requested_items": self.requested_items,
            "referred_to": self.referred_to,
            "deadline": self.deadline,
            "requires_response": self.requires_response,
            "method": self.method,
        }


def _coerce(payload: Any, fallback: dict[str, Any]) -> Classification:
    data = payload if isinstance(payload, dict) else {}
    label = str(data.get("classification") or "").strip().lower()
    if label not in CLASSES:
        # NFR-205 output validation: an answer outside the vocabulary is not an
        # answer.  Fall back rather than write a class nothing downstream knows.
        log.warning("LLM returned an unknown reply class %r; using the rule result", label)
        return Classification(**{**fallback, "method": "rules"})
    referred = data.get("referred_to")
    return Classification(
        classification=label,
        confidence=_clamp(data.get("confidence"), fallback.get("confidence", 0.5)),
        reason=str(data.get("reason") or "")[:500],
        language=_language(data.get("language"), fallback.get("language", "en")),
        sentiment=str(data.get("sentiment") or "neutral")[:20],
        proposed_times=[str(t)[:200] for t in (data.get("proposed_times") or [])][:10],
        requested_items=[str(t)[:200] for t in (data.get("requested_items") or [])][:10],
        referred_to=referred if isinstance(referred, dict) and any(referred.values()) else None,
        deadline=(str(data["deadline"])[:100] if data.get("deadline") else None),
        requires_response=bool(data.get("requires_response", True)),
    )


def _clamp(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def _language(value: Any, default: str) -> str:
    code = str(value or "").strip().lower()[:2]
    return code if code in ("en", "nl", "fr", "de") else default


def classify(
    reply: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
    llm: LLMClient | None = None,
) -> Classification:
    """Classify one reply (FR-422).  Falls back to the rules when the model cannot run."""
    fallback = classify_heuristically(reply.get("subject"), reply.get("body"))
    if llm is None:
        return Classification(**fallback)

    template = load_prompt(CLASSIFICATION_PROMPT)
    system, user = template.render()
    try:
        payload = llm.complete_json(
            "classify.reply",
            system=system,
            user=user,
            untrusted={
                "reply": _reply_block(reply),
                "context": to_json(_slim_context(context)) or "{}",
            },
            entity_type="incoming_reply",
            entity_id=reply.get("id"),
            prompt_template=f"{template.name}.md",
            prompt_version=template.version,
            max_tokens=900,
        )
    except (BudgetExhausted, LLMError) as exc:
        log.info("Reply classification degraded to rules: %s", exc)
        return Classification(**fallback)
    return _coerce(payload, fallback)


def _reply_block(reply: dict[str, Any]) -> str:
    return (
        f"From: {reply.get('from_address') or 'unknown'}\n"
        f"Received: {reply.get('received_at') or ''}\n"
        f"Subject: {reply.get('subject') or ''}\n\n"
        f"{(reply.get('body') or '')[:BODY_EXCERPT_CHARS]}"
    )


def _slim_context(context: dict[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {}
    return {
        "role": context.get("title"),
        "company": context.get("company_name"),
        "kind": context.get("kind"),
        "applied_language": context.get("language"),
    }


# ---------------------------------------------------------------------------
# Drafting the answer (FR-422)
# ---------------------------------------------------------------------------

#: Template answers, used when the model is unavailable.  They are short on
#: purpose: an unfinished draft the seeker has to complete is honest, a
#: confident draft written without the model would not be.
_TEMPLATES: dict[str, dict[str, str]] = {
    "interview_invitation": {
        "en": "Dear {name},\n\nThank you for the invitation. I would be glad to meet.\n\n"
              "{slots}\n\nPlease let me know which of these suits you, or propose another "
              "moment and I will make it work.\n\nKind regards,\n{seeker}",
        "nl": "Beste {name},\n\nDank u voor de uitnodiging. Ik kom graag kennismaken.\n\n"
              "{slots}\n\nLaat gerust weten welk moment u past, of stel een ander moment voor.\n\n"
              "Met vriendelijke groeten,\n{seeker}",
        "fr": "Bonjour {name},\n\nMerci pour votre invitation. C'est avec plaisir que je vous "
              "rencontrerai.\n\n{slots}\n\nDites-moi ce qui vous convient, ou proposez un autre "
              "moment.\n\nCordialement,\n{seeker}",
        "de": "Guten Tag {name},\n\nvielen Dank fuer die Einladung. Ich komme gerne zu einem "
              "Gespraech.\n\n{slots}\n\nSagen Sie mir gerne, welcher Termin Ihnen passt.\n\n"
              "Mit freundlichen Gruessen,\n{seeker}",
    },
    "rejection": {
        "en": "Dear {name},\n\nThank you for letting me know, and for taking the time to "
              "consider my application.\n\nIf a role closer to my profile opens up, I would be "
              "glad to hear from you.\n\nKind regards,\n{seeker}",
        "nl": "Beste {name},\n\nDank u voor uw bericht en voor de tijd die u aan mijn "
              "sollicitatie besteedde.\n\nMocht er later een functie zijn die beter aansluit, "
              "dan hoor ik het graag.\n\nMet vriendelijke groeten,\n{seeker}",
        "fr": "Bonjour {name},\n\nMerci de votre retour et du temps consacre a ma candidature."
              "\n\nSi un poste plus proche de mon profil se libere, je serais heureux d'en etre "
              "informe.\n\nCordialement,\n{seeker}",
        "de": "Guten Tag {name},\n\nvielen Dank fuer Ihre Rueckmeldung und die Zeit, die Sie "
              "sich genommen haben.\n\nSollte spaeter eine passendere Position frei werden, "
              "freue ich mich ueber eine Nachricht.\n\nMit freundlichen Gruessen,\n{seeker}",
    },
    "default": {
        "en": "Dear {name},\n\nThank you for your reply.\n\n[Answer their questions here.]\n\n"
              "Kind regards,\n{seeker}",
        "nl": "Beste {name},\n\nDank u voor uw antwoord.\n\n[Beantwoord hier hun vragen.]\n\n"
              "Met vriendelijke groeten,\n{seeker}",
        "fr": "Bonjour {name},\n\nMerci pour votre reponse.\n\n[Repondez ici a leurs questions.]"
              "\n\nCordialement,\n{seeker}",
        "de": "Guten Tag {name},\n\nvielen Dank fuer Ihre Antwort.\n\n[Beantworten Sie hier "
              "ihre Fragen.]\n\nMit freundlichen Gruessen,\n{seeker}",
    },
}


def _template_draft(
    classification: Classification,
    *,
    seeker_name: str,
    correspondent: str | None,
    slots: list[dict] | None = None,
) -> dict[str, Any]:
    family = _TEMPLATES.get(classification.classification, _TEMPLATES["default"])
    body = family.get(classification.language, family["en"])
    slot_text = ""
    if slots:
        slot_text = "\n".join(f"- {s.get('label') or s.get('start')}" for s in slots[:3])
    return {
        "body": body.format(
            name=correspondent or "",
            seeker=seeker_name,
            slots=slot_text or "",
        ).replace("\n\n\n", "\n\n"),
        "open_questions": [
            "This draft was written without the language model; check every sentence "
            "before sending."
        ],
        "tone": "neutral",
        "generated_by": "template",
    }


def _correspondent_name(reply: dict[str, Any]) -> str | None:
    address = str(reply.get("from_address") or "")
    match = re.match(r"\s*\"?([^\"<]+?)\"?\s*<", address)
    if match:
        return match.group(1).strip()
    local = address.split("@")[0].replace(".", " ").replace("_", " ").strip()
    return local.title() or None


def _subject_for(reply: dict[str, Any]) -> str:
    subject = str(reply.get("subject") or "").strip()
    if not subject:
        return "Re: your message"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def _document_excerpt(path: str | None, limit: int = DOCUMENT_EXCERPT_CHARS) -> str:
    """Text of a generated PDF, for context.  A missing file is not an error."""
    if not path:
        return ""
    try:
        from pathlib import Path  # noqa: PLC0415 - only needed on this path

        from pypdf import PdfReader  # noqa: PLC0415 - heavy import, rarely needed

        file = Path(path)
        if not file.exists():
            return ""
        reader = PdfReader(str(file))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:12])
        return text[:limit]
    except Exception:  # noqa: BLE001 - context is optional; the draft is not
        log.debug("Could not read %s for reply context", path)
        return ""


def draft_response(
    reply: dict[str, Any],
    classification: Classification,
    *,
    seeker_name: str,
    context: dict[str, Any] | None = None,
    package: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
    slots: list[dict] | None = None,
    compensation_disclosable: bool = False,
    llm: LLMClient | None = None,
) -> dict[str, Any]:
    """Draft the answer, in the reply's language, from the seeker's documents.

    Never sent: the return value is written to ``reply_draft`` with status
    ``draft`` and waits for a person (NFR-305).
    """
    correspondent = _correspondent_name(reply)
    fallback = _template_draft(
        classification, seeker_name=seeker_name, correspondent=correspondent, slots=slots
    )
    fallback["subject"] = _subject_for(reply)

    if llm is None:
        return fallback

    template = load_prompt(DRAFT_PROMPT)
    system, user = template.render(language=classification.language)
    untrusted = {
        "reply": _reply_block(reply),
        "classification": to_json(classification.as_dict()) or "{}",
        "context": to_json(
            {
                **_slim_context(context),
                "seeker_name": seeker_name,
                "correspondent": correspondent,
                "compensation_disclosable": compensation_disclosable,
            }
        )
        or "{}",
        "slots": to_json(slots or []) or "[]",
    }
    briefing = _document_excerpt((package or {}).get("briefing_pdf_path"))
    motivation = _document_excerpt((package or {}).get("motivation_pdf_path"))
    if briefing:
        untrusted["briefing"] = briefing
    if motivation:
        untrusted["motivation"] = motivation
    if profile:
        untrusted["seeker"] = to_json(profile) or "{}"

    try:
        payload = llm.complete_json(
            "generate.email",
            system=system,
            user=user,
            untrusted=untrusted,
            entity_type="incoming_reply",
            entity_id=reply.get("id"),
            prompt_template=f"{template.name}.md",
            prompt_version=template.version,
            temperature=0.4,
            # The strong model spends tokens reasoning before it answers; a
            # ceiling tight enough to truncate the JSON would silently drop
            # every draft to the template.
            max_tokens=3000,
        )
    except (BudgetExhausted, LLMError) as exc:
        log.info("Reply drafting degraded to a template: %s", exc)
        return fallback

    body = str((payload or {}).get("body") or "").strip()
    if not body:
        return fallback
    return {
        "subject": str((payload or {}).get("subject") or _subject_for(reply))[:300],
        "body": body,
        "open_questions": [str(q)[:300] for q in ((payload or {}).get("open_questions") or [])][:8],
        "tone": str((payload or {}).get("tone") or "neutral")[:20],
        "generated_by": "llm",
    }


# ---------------------------------------------------------------------------
# The whole step, as the API and the scheduler call it
# ---------------------------------------------------------------------------


def process_reply(
    job_seeker_id: str,
    reply_id: str,
    *,
    llm: LLMClient | None = None,
    draft: bool = True,
    move_card: bool = True,
) -> dict[str, Any]:
    """Classify one reply, move its card and draft the answer (FR-421, FR-422).

    Returns what happened, including ``draft_id`` when a draft was written.
    The draft is never sent and the card never closes itself beyond the
    rejection the reply states.
    """
    from dreamjob.postapp import board  # noqa: PLC0415 - board imports outcomes, outcomes not us

    reply = repo.get_reply(reply_id, job_seeker_id)
    if reply is None:
        raise LookupError(f"No incoming reply {reply_id} for this job seeker")

    card = (
        repo.card_for_dispatch(job_seeker_id, reply["dispatch_id"])
        if reply.get("dispatch_id")
        else None
    )
    context = None
    package = None
    if card and card.get("opportunity_id"):
        context = repo.opportunity_context(card["opportunity_id"], job_seeker_id)
        package = repo.package_for_opportunity(card["opportunity_id"], job_seeker_id)

    result = classify(reply, context=context, llm=llm)
    repo.save_classification(
        reply_id,
        {
            "classification": result.classification,
            "classification_confidence": result.confidence,
            "extracted_slots": to_json(
                {"phrases": result.proposed_times, "deadline": result.deadline}
            ),
        },
    )
    record_audit(
        "reply.classified",
        "incoming_reply",
        reply_id,
        seeker_id=job_seeker_id,
        actor="system",
        detail={"classification": result.classification, "method": result.method},
    )

    out: dict[str, Any] = {
        "reply_id": reply_id,
        "classification": result.as_dict(),
        "card_id": card["id"] if card else None,
        "transition": None,
        "draft_id": None,
    }

    if move_card and card:
        moved = board.apply_reply(
            job_seeker_id, {**reply, "id": reply_id}, classification=result.classification
        )
        out["transition"] = moved.as_dict() if moved else None

    if draft and result.requires_response and result.classification != "automatic_reply":
        seeker = repo.seeker(job_seeker_id) or {}
        profile = repo.composite_profile(job_seeker_id)
        body = draft_response(
            reply,
            result,
            seeker_name=str(seeker.get("display_name") or ""),
            context=context,
            package=package,
            profile=_profile_facts(profile),
            llm=llm,
        )
        out["draft_id"] = store_draft(
            job_seeker_id,
            reply=reply,
            card=card,
            classification=result,
            draft=body,
        )
        out["draft"] = body

    return out


def _profile_facts(profile: dict | None) -> dict[str, Any] | None:
    """The blocks a reply may draw on.  Never the whole profile (CR-410)."""
    if not profile:
        return None
    from dreamjob.db.connection import from_json  # noqa: PLC0415 - local decode

    return {
        "narrative": profile.get("narrative"),
        "core_competencies": from_json(profile.get("core_competencies"), None),
        "achievements": from_json(profile.get("achievements"), None),
        "constraints": from_json(profile.get("constraints"), None),
    }


class AlreadySent(RuntimeError):
    """Raised when a draft the mail slice has already sent is approved again."""


def store_draft(
    job_seeker_id: str,
    *,
    reply: dict[str, Any],
    card: dict[str, Any] | None,
    classification: Classification,
    draft: dict[str, Any],
    kind: str = "reply",
    attachments: list[str] | None = None,
) -> str:
    """Persist a draft in ``draft`` status, threaded onto the original (FR-422)."""
    draft_id = repo.create_draft(
        job_seeker_id,
        {
            "incoming_reply_id": reply.get("id"),
            "dispatch_id": reply.get("dispatch_id"),
            "pipeline_card_id": (card or {}).get("id"),
            "kind": kind,
            "classification": classification.classification,
            "language": classification.language,
            "subject": draft.get("subject"),
            "body": draft.get("body"),
            "thread_id": (card or {}).get("thread_id"),
            "in_reply_to": reply.get("message_id"),
            "references_header": _references(reply),
            "to_address": reply.get("from_address"),
            "attachments": to_json(attachments or []),
            "status": "draft",
            "generated_by": draft.get("generated_by", "llm"),
        },
    )
    repo.save_classification(reply["id"], {"draft_response": draft.get("body")})
    return draft_id


def _references(reply: dict[str, Any]) -> str | None:
    """RFC 5322 References for the answer: the thread so far, plus this message."""
    parts = [p for p in (reply.get("in_reply_to"), reply.get("message_id")) if p]
    return " ".join(parts) or None


def approve_draft(job_seeker_id: str, draft_id: str, *, body: str | None = None,
                  subject: str | None = None) -> dict:
    """Mark a draft approved for sending (NFR-305: this is the human decision).

    Approval does not send.  The mail slice picks approved drafts up; until it
    does, an approved draft is still just text in the database.
    """
    draft = repo.get_draft(draft_id, job_seeker_id)
    if draft is None:
        raise LookupError(f"No reply draft {draft_id} for this job seeker")
    # The mail slice drains 'approved'; re-approving something it already sent
    # would put it back in the queue and send it twice.
    if draft.get("status") == "sent" or draft.get("sent_at"):
        raise AlreadySent(f"Reply draft {draft_id} has already been sent")
    values: dict[str, Any] = {"status": "approved", "approved_at": utcnow(),
                              "approved_by": job_seeker_id}
    if body is not None:
        values["body"] = body
    if subject is not None:
        values["subject"] = subject
    repo.update_draft(draft_id, values)
    record_audit(
        "reply_draft.approved",
        "reply_draft",
        draft_id,
        seeker_id=job_seeker_id,
        detail={"edited": body is not None or subject is not None},
    )
    if draft.get("incoming_reply_id"):
        repo.mark_reply_handled(draft["incoming_reply_id"], job_seeker_id)
    return repo.get_draft(draft_id, job_seeker_id) or {}


def edit_draft(job_seeker_id: str, draft_id: str, *, body: str, subject: str | None = None) -> dict:
    if repo.get_draft(draft_id, job_seeker_id) is None:
        raise LookupError(f"No reply draft {draft_id} for this job seeker")
    values: dict[str, Any] = {"body": body, "status": "edited"}
    if subject is not None:
        values["subject"] = subject
    repo.update_draft(draft_id, values)
    return repo.get_draft(draft_id, job_seeker_id) or {}


def discard_draft(job_seeker_id: str, draft_id: str) -> dict:
    if repo.get_draft(draft_id, job_seeker_id) is None:
        raise LookupError(f"No reply draft {draft_id} for this job seeker")
    repo.update_draft(draft_id, {"status": "discarded"})
    return repo.get_draft(draft_id, job_seeker_id) or {}


def process_pending(job_seeker_id: str | None = None, *, limit: int = 25,
                    llm_factory: Any = None) -> dict[str, Any]:
    """Classify every reply that has not been classified yet (FR-422).

    Used by the scheduler.  One failing reply must not stop the rest, so each
    is handled on its own and its error recorded.
    """
    processed: list[dict] = []
    errors: list[dict] = []
    for row in repo.unclassified_replies(job_seeker_id, limit=limit):
        seeker_id = row["job_seeker_id"]
        try:
            llm = llm_factory(seeker_id) if llm_factory else None
            processed.append(process_reply(seeker_id, row["id"], llm=llm))
        except Exception as exc:  # noqa: BLE001 - one bad reply must not stop the sweep
            log.exception("Could not process reply %s", row["id"])
            errors.append({"reply_id": row["id"], "error": str(exc)})
    return {"processed": len(processed), "errors": errors, "results": processed}
