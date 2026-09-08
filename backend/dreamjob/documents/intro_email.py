"""Introduction email (FR-321, FR-323, NFR-302, NFR-501, CR-405).

The only one of the four generated artefacts that leaves the machine.  Two
requirements shape it and both are enforced here rather than trusted to a
prompt:

**FR-323** - an email for a *speculative* opening is a spontaneous application.
It states the proposed role and the value on offer and must not assert that a
vacancy exists.  The prompt says so, and then ``vacancy_assertions`` re-reads
the finished text for the phrasings that would assert one, in each supported
language.  A hit is a high-severity finding on the package, so the job seeker
has to edit before the email can be approved - the model's word is not taken
for it.

**NFR-302** - the objection sentence is appended by this module to every email
in the language of the opportunity.  The model is told not to write one, so it
is present exactly once and cannot be dropped by a regeneration.

Greeting, sign-off and signature are assembled from the profile, not written by
the model: a contact's name and the job seeker's own details are facts, and a
model that paraphrases them produces exactly the sort of error NFR-206 exists
to catch.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from dreamjob.documents._llm import complete_json
from dreamjob.documents.pdf_builder import normalise_language
from dreamjob.documents.templates import CvDocument
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError
from dreamjob.pipeline.enrichment import load_prompt

log = logging.getLogger(__name__)

PROMPT_NAME = "introduction_email"

# NFR-302: legitimate interest requires an objection route in every email, and
# an objection is honoured by blocking the contact permanently.
OBJECTION_SENTENCE: dict[str, str] = {
    "en": (
        "If you would prefer not to be contacted about this, reply with \"no\" and I will "
        "delete your details and not write again."
    ),
    "nl": (
        "Wilt u hierover liever niet gecontacteerd worden, antwoord dan met \"nee\": ik wis "
        "uw gegevens en schrijf u niet opnieuw."
    ),
    "fr": (
        "Si vous préférez ne pas être contacté à ce sujet, répondez « non » : je supprimerai "
        "vos coordonnées et ne vous écrirai plus."
    ),
    "de": (
        "Wenn Sie dazu nicht kontaktiert werden möchten, antworten Sie mit \"nein\": ich "
        "lösche Ihre Daten und schreibe Ihnen nicht erneut."
    ),
}

GREETING: dict[str, tuple[str, str]] = {
    "en": ("Dear {name},", "Dear Sir or Madam,"),
    "nl": ("Beste {name},", "Geachte heer, mevrouw,"),
    "fr": ("Bonjour {name},", "Madame, Monsieur,"),
    "de": ("Guten Tag {name},", "Sehr geehrte Damen und Herren,"),
}

SIGN_OFF: dict[str, str] = {
    "en": "Kind regards,",
    "nl": "Met vriendelijke groet,",
    "fr": "Bien cordialement,",
    "de": "Mit freundlichen Grüßen,",
}

ATTACHMENT_LINE: dict[str, str] = {
    "en": "My CV is attached.",
    "nl": "Mijn cv gaat als bijlage mee.",
    "fr": "Mon CV est joint à ce message.",
    "de": "Mein Lebenslauf ist angehängt.",
}

SUBJECT: dict[str, dict[str, str]] = {
    "vacancy": {
        "en": "Application - {role}",
        "nl": "Sollicitatie - {role}",
        "fr": "Candidature - {role}",
        "de": "Bewerbung - {role}",
    },
    "speculative": {
        "en": "Speculative application - {role}",
        "nl": "Spontane sollicitatie - {role}",
        "fr": "Candidature spontanée - {role}",
        "de": "Initiativbewerbung - {role}",
    },
}

# FR-323: an email for an opening nobody advertised must not claim one exists.
VACANCY_ASSERTIONS: dict[str, tuple[str, ...]] = {
    "en": (
        r"\byour vacanc(y|ies)\b", r"\bthe vacanc(y|ies)\b", r"\byour job (posting|advert)",
        r"\bthe advertised (role|position|vacancy)\b", r"\byou (have )?advertised\b",
        r"\bin response to your (advertisement|posting|ad)\b", r"\byour opening for\b",
        r"\bapplying for the (advertised|posted|open) ", r"\bthe (role|position) you posted\b",
    ),
    "nl": (
        r"\buw vacature\b", r"\bde vacature\b", r"\bnaar aanleiding van uw vacature\b",
        r"\bde openstaande (functie|positie|vacature)\b", r"\buw advertentie\b",
        r"\bde gepubliceerde (functie|vacature)\b", r"\bzoals geadverteerd\b",
    ),
    "fr": (
        r"\bvotre offre d.emploi\b", r"\bl.offre publiée\b", r"\bvotre annonce\b",
        r"\ble poste vacant\b", r"\bsuite à votre annonce\b", r"\ble poste que vous proposez\b",
        r"\bvotre poste à pourvoir\b",
    ),
    "de": (
        r"\bIhre Stellenanzeige\b", r"\bdie ausgeschriebene Stelle\b", r"\bauf Ihre Anzeige\b",
        r"\bIhre Vakanz\b", r"\bIhre ausgeschriebene Position\b", r"\bwie ausgeschrieben\b",
    ),
}

SPECULATIVE_OPENING_RULE = (
    "This is a SPECULATIVE opening: no vacancy has been advertised. Write a spontaneous "
    "application. Name the role the job seeker proposes and the value they would add, and "
    "never write or imply that a vacancy, posting or opening exists - not even as a "
    "question. Do not thank them for an advertisement."
)
VACANCY_OPENING_RULE = (
    "This is a real, advertised vacancy. Refer to it once, plainly, and move on to what the "
    "job seeker would bring."
)


@dataclass
class EmailDraft:
    subject: str
    body: str
    language: str
    speculative: bool
    recipient_name: str | None = None
    recipient_email: str | None = None
    used_llm: bool = False
    notes: list[str] = field(default_factory=list)
    assertions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "email_subject": self.subject,
            "email_body": self.body,
            "language": self.language,
            "speculative": self.speculative,
            "recipient_name": self.recipient_name,
            "recipient_email": self.recipient_email,
            "email_used_llm": self.used_llm,
            "notes": self.notes,
            "vacancy_assertions": self.assertions,
        }


# ---------------------------------------------------------------------------
# FR-323 check
# ---------------------------------------------------------------------------


def vacancy_assertions(text: str, language: str) -> list[str]:
    """Phrasings that would assert a vacancy exists.  Empty is what FR-323 wants."""
    lang = normalise_language(language)
    patterns = VACANCY_ASSERTIONS.get(lang, ()) + VACANCY_ASSERTIONS["en"]
    hits: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text or "", re.IGNORECASE):
            phrase = match.group(0)
            if phrase.lower() not in {h.lower() for h in hits}:
                hits.append(phrase)
    return hits


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def compose_email(
    inputs: dict[str, Any],
    cv: CvDocument,
    *,
    language: str | None = None,
    contact: dict[str, Any] | None = None,
    llm: LLMClient | None = None,
    instructions: str | None = None,
) -> EmailDraft:
    """The FR-321(d) introduction email, with the tailored CV attached."""
    opportunity = inputs.get("opportunity") or {}
    company = inputs.get("company") or {}
    seeker = inputs.get("seeker") or {}
    lang = normalise_language(language or opportunity.get("language") or seeker.get("locale"))
    speculative = str(opportunity.get("kind") or "") == "speculative"
    role = str(opportunity.get("title") or "")

    recipient = contact or _pick_contact(inputs)
    notes: list[str] = []
    used_llm = False

    subject = SUBJECT["speculative" if speculative else "vacancy"][lang].format(
        role=role or company.get("name") or ""
    )
    body_core = _fallback_body(inputs, cv, lang, speculative)

    if llm is not None:
        try:
            generated = _generate(inputs, cv, lang, speculative, recipient, llm, instructions)
            if generated.get("body"):
                body_core = str(generated["body"]).strip()
                used_llm = True
            if generated.get("subject"):
                subject = str(generated["subject"]).strip()[:120]
            notes += [str(n) for n in generated.get("notes") or [] if str(n).strip()]
        except BudgetExhausted:
            notes.append("Token budget exhausted; the email is the assembled version.")
        except (LLMError, ValueError, KeyError, TypeError) as exc:
            log.warning("Email generation failed for %s: %s", opportunity.get("id"), exc)
            notes.append(f"Email assembled without the model ({exc.__class__.__name__}).")

    body = _assemble(body_core, cv, lang, recipient)
    hits = vacancy_assertions(body, lang) if speculative else []
    if hits:
        notes.append(
            "FR-323: the draft implies an advertised vacancy exists. Edit before sending: "
            + ", ".join(f"'{h}'" for h in hits)
        )
    return EmailDraft(
        subject=subject,
        body=body,
        language=lang,
        speculative=speculative,
        recipient_name=(recipient or {}).get("full_name"),
        recipient_email=(recipient or {}).get("email"),
        used_llm=used_llm,
        notes=notes,
        assertions=hits,
    )


def _pick_contact(inputs: dict[str, Any]) -> dict[str, Any] | None:
    """The best hiring contact: named and validated before generic (FR-304)."""
    contacts = [c for c in (inputs.get("contacts") or []) if not c.get("objected")]
    if not contacts:
        return None
    def rank(contact: dict) -> tuple:
        return (
            0 if contact.get("email_validation") == "valid" else 1,
            1 if contact.get("is_generic_mailbox") else 0,
            0 if contact.get("full_name") else 1,
            -float(contact.get("confidence") or 0),
        )
    return sorted(contacts, key=rank)[0]


def scaffolding(language: str) -> str:
    """The fixed wording this module puts around the body, per language.

    Greeting, attachment line, sign-off and the NFR-302 objection sentence are
    written here, not by a model.  The NFR-206 scan is told so, otherwise the
    system's own boilerplate - "My CV is attached", "Ihre Daten" - is reported
    as content of unknown origin on every clean email.
    """
    lang = normalise_language(language)
    named, generic = GREETING[lang]
    return "\n".join(
        [
            named.replace("{name}", ""),
            generic,
            ATTACHMENT_LINE[lang],
            SIGN_OFF[lang],
            OBJECTION_SENTENCE[lang],
            *(template[lang].replace("{role}", "") for template in SUBJECT.values()),
        ]
    )


def _assemble(core: str, cv: CvDocument, lang: str, recipient: dict | None) -> str:
    """Greeting, body, attachment line, sign-off, signature, objection sentence."""
    named, generic = GREETING[lang]
    name = str((recipient or {}).get("full_name") or "").strip()
    greeting = named.format(name=name) if name else generic

    signature = [cv.contact.name]
    signature += [line for line in (cv.contact.email, cv.contact.phone) if line]
    if cv.contact.linkedin_url:
        signature.append(cv.contact.linkedin_url)

    parts = [
        greeting,
        "",
        core.strip(),
        "",
        ATTACHMENT_LINE[lang],
        "",
        SIGN_OFF[lang],
        "\n".join(signature),
        "",
        "--",
        OBJECTION_SENTENCE[lang],
    ]
    return "\n".join(parts).strip() + "\n"


def _fallback_body(
    inputs: dict[str, Any], cv: CvDocument, lang: str, speculative: bool
) -> str:
    """Assembled from facts already in the CV.  Nothing new is claimed."""
    opportunity = inputs.get("opportunity") or {}
    company = str((inputs.get("company") or {}).get("name") or "")
    role = str(opportunity.get("title") or "")
    current = cv.experience[0] if cv.experience else None
    standing = (
        " - ".join(p for p in (current.title, current.company) if p)
        if current
        else (cv.contact.headline or "")
    ).rstrip(". ")
    skills = ", ".join(cv.skills[:5])

    openings = {
        True: {
            "en": f"I am writing on my own initiative about a possible {role} role at {company}.",
            "nl": (
                f"Ik schrijf u op eigen initiatief over een mogelijke rol als {role} "
                f"bij {company}."
            ),
            "fr": (
                f"Je vous écris de ma propre initiative au sujet d'un poste de {role} "
                f"chez {company}."
            ),
            "de": (
                f"ich schreibe Ihnen aus eigener Initiative zu einer möglichen Rolle "
                f"als {role} bei {company}."
            ),
        },
        False: {
            "en": f"I am applying for the {role} role at {company}.",
            "nl": f"Ik solliciteer naar de functie van {role} bij {company}.",
            "fr": f"Je pose ma candidature au poste de {role} chez {company}.",
            "de": f"ich bewerbe mich auf die Position {role} bei {company}.",
        },
    }
    standing_line = {
        "en": f"I am currently {standing}.",
        "nl": f"Ik ben op dit moment {standing}.",
        "fr": f"Je suis actuellement {standing}.",
        "de": f"Zurzeit bin ich {standing}.",
    }
    value_line = {
        "en": f"What I would bring: {skills}.",
        "nl": f"Wat ik meebreng: {skills}.",
        "fr": f"Ce que j'apporte : {skills}.",
        "de": f"Was ich mitbringe: {skills}.",
    }
    ask_line = {
        "en": "Would a short call in the next two weeks be useful?",
        "nl": "Zou een kort gesprek in de komende twee weken zinvol zijn?",
        "fr": "Un bref échange dans les deux prochaines semaines vous conviendrait-il ?",
        "de": "Wäre ein kurzes Gespräch in den nächsten zwei Wochen sinnvoll?",
    }

    paragraphs = [openings[speculative][lang]]
    if standing:
        paragraphs.append(standing_line[lang])
    if cv.summary:
        paragraphs.append(cv.summary.split("\n")[0][:400])
    elif skills:
        paragraphs.append(value_line[lang])
    paragraphs.append(ask_line[lang])
    return "\n\n".join(p for p in paragraphs if p.strip())


def _generate(
    inputs: dict[str, Any],
    cv: CvDocument,
    lang: str,
    speculative: bool,
    recipient: dict | None,
    llm: LLMClient,
    instructions: str | None,
) -> dict[str, Any]:
    import json  # noqa: PLC0415 - only on the LLM path

    opportunity = inputs.get("opportunity") or {}
    company = inputs.get("company") or {}
    prompt = load_prompt(PROMPT_NAME)
    who = (
        f"{recipient.get('full_name') or 'the hiring contact'}"
        f"{', ' + recipient['role_title'] if recipient and recipient.get('role_title') else ''}"
        if recipient
        else "the hiring team (no named contact was found)"
    )
    system, user = prompt.render(
        language=lang,
        opening_rule=SPECULATIVE_OPENING_RULE if speculative else VACANCY_OPENING_RULE,
        role_title=opportunity.get("title") or "",
        company_name=company.get("name") or "",
        recipient=who,
        cv_summary=_cv_summary(cv),
    )
    if instructions:
        user += f"\n\nThe job seeker asked for this revision:\n{str(instructions)[:2000]}"

    return complete_json(
        llm,
        "generate.email",
        system,
        user,
        untrusted={
            "opening": json.dumps(
                {"opportunity": opportunity, "vacancy": inputs.get("vacancy") or {}},
                ensure_ascii=False,
                default=str,
            )[:12_000],
            "company": json.dumps(company, ensure_ascii=False, default=str)[:12_000],
        },
        entity_type="opportunity",
        entity_id=opportunity.get("id"),
        prompt_template=prompt.name,
        prompt_version=prompt.version,
        schema_hint='{"subject": str, "body": str, "notes": [str]}',
    )


def _cv_summary(cv: CvDocument) -> str:
    lines = [cv.contact.headline or "", cv.summary or ""]
    for job in cv.experience[:3]:
        lines.append(
            f"- {job.title} at {job.company} ({job.period(cv.language)}): "
            + "; ".join(job.bullets[:2])
        )
    if cv.skills:
        lines.append("Skills: " + ", ".join(cv.skills[:10]))
    return "\n".join(line for line in lines if line.strip())[:3000]
