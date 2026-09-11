"""Building the introduction e-mail (FR-321, FR-323, NFR-302, RK-05).

Three rules decide everything in this module.

**RK-05 - plain professional formatting.**  The message is ``text/plain``.  No
HTML alternative, no tracking pixel, no image, no marketing furniture: those
are the features spam filters weigh, and a cold application that lands in a
junk folder has failed regardless of how well it was written.  The body is the
text the job seeker approved, wrapped to a readable width, with a signature and
one closing sentence appended.

**FR-321 - the briefing and the motivation document are for the job seeker
only and are never sent.**  :func:`collect_attachments` attaches the tailored
CV and refuses, by path, anything that came from the briefing or motivation
columns of the package.  The check is on the value, not on the caller's good
intentions, so a future caller that passes the whole package dictionary still
cannot leak them.

**NFR-302 - every introduction e-mail carries an objection sentence.**  It is
appended in the language of the message and names the address to write to, so
a recipient's "please stop" reaches a mailbox that is actually read and can be
turned into a permanent block.
"""

from __future__ import annotations

import logging
import re
import textwrap
from datetime import UTC, datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

from dreamjob.config import get_settings
from dreamjob.mail.base import Attachment, MailBackendError, OutgoingMessage

log = logging.getLogger(__name__)

#: FR-321 - package columns whose files must never reach a recipient.
NEVER_ATTACH_FIELDS: tuple[str, ...] = ("briefing_pdf_path", "motivation_pdf_path")

#: The CV, in the order it is preferred.  PDF first: it renders the same
#: everywhere, which is what a hiring contact will open (FR-322).
CV_FIELDS: tuple[str, ...] = ("cv_pdf_path", "cv_docx_path")

SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "nl", "fr", "de")

#: NFR-302 - the objection sentence, in the language of the message.  It states
#: the legitimate-interest basis and the one action that stops all further
#: contact, because that is what the requirement asks the recipient to be given.
OBJECTION_SENTENCE: dict[str, str] = {
    "en": (
        "If you would rather not receive messages like this, reply with "
        "“no thank you” to {address} and I will remove your details "
        "and not contact you again."
    ),
    "nl": (
        "Wilt u liever geen berichten zoals dit ontvangen, antwoord dan met "
        "“liever niet” naar {address}. Ik verwijder uw gegevens dan "
        "en neem geen contact meer op."
    ),
    "fr": (
        "Si vous préférez ne pas recevoir ce type de message, répondez "
        "« non merci » à {address} : je supprimerai "
        "vos coordonnées et ne vous recontacterai pas."
    ),
    "de": (
        "Wenn Sie solche Nachrichten nicht erhalten möchten, antworten Sie "
        "bitte mit „nein danke“ an {address}. Ich lösche Ihre Daten "
        "dann und melde mich nicht wieder."
    ),
}

SIGNATURE_LEAD: dict[str, str] = {
    "en": "Kind regards,",
    "nl": "Met vriendelijke groet,",
    "fr": "Cordialement,",
    "de": "Mit freundlichen Grüßen,",
}

FOLLOW_UP_PREFIX: dict[str, str] = {
    "en": "Re: ",
    "nl": "Re: ",
    "fr": "Re: ",
    "de": "Re: ",
}

WRAP_WIDTH = 78


class AttachmentRefused(MailBackendError):
    """FR-321: something that must never be sent was offered as an attachment."""


def normalise_language(language: str | None) -> str:
    code = (language or "en").strip().lower()[:2]
    return code if code in SUPPORTED_LANGUAGES else "en"


def resolve_path(path: str | Path | None) -> Path | None:
    """Accept absolute paths and paths recorded relative to the data directory."""
    if not path:
        return None
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate if candidate.exists() else None
    settings = get_settings()
    for base in (settings.abs_data_dir, settings.generated_dir, Path.cwd()):
        resolved = base / candidate
        if resolved.exists():
            return resolved
    return None


def collect_attachments(package: dict) -> list[Attachment]:
    """The tailored CV, and only the tailored CV (FR-321).

    The briefing and motivation PDFs are named explicitly and their paths are
    collected first, so that even a CV column that has been pointed at one of
    them by mistake is refused rather than sent.
    """
    forbidden = {
        str(resolve_path(package.get(field)) or package.get(field) or "")
        for field in NEVER_ATTACH_FIELDS
        if package.get(field)
    }

    attachments: list[Attachment] = []
    for field in CV_FIELDS:
        raw = package.get(field)
        if not raw:
            continue
        resolved = resolve_path(raw)
        if resolved is None:
            log.warning(
                "Package %s: %s points at %s, which is missing", package.get("id"), field, raw
            )
            continue
        if str(resolved) in forbidden or str(raw) in forbidden:
            raise AttachmentRefused(
                f"{field} points at a document that FR-321 forbids sending "
                "(the briefing or motivation PDF); refusing to attach it."
            )
        attachments.append(Attachment.from_path(resolved, filename=cv_filename(package, resolved)))
        break  # one CV, not two copies of the same document

    if not attachments:
        log.info("Package %s has no CV file; sending the message without one", package.get("id"))
    return attachments


def cv_filename(package: dict, path: Path) -> str:
    """A recipient-facing filename: the applicant's name, not a UUID."""
    name = (package.get("seeker_display_name") or "").strip()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")
    if not slug:
        return path.name
    return f"CV-{slug}{path.suffix.lower()}"


def objection_sentence(language: str, reply_address: str) -> str:
    """NFR-302, in the language of the message."""
    template = OBJECTION_SENTENCE.get(normalise_language(language), OBJECTION_SENTENCE["en"])
    return template.format(address=reply_address)


def signature(language: str, display_name: str, contact_lines: list[str] | None = None) -> str:
    lead = SIGNATURE_LEAD.get(normalise_language(language), SIGNATURE_LEAD["en"])
    lines = [lead, display_name, *(contact_lines or [])]
    return "\n".join(line for line in lines if line)


def _wrap(text: str) -> str:
    """Wrap prose to a readable width, leaving blank lines and lists intact."""
    out: list[str] = []
    for block in (text or "").replace("\r\n", "\n").split("\n"):
        stripped = block.strip()
        if not stripped:
            out.append("")
        elif stripped.startswith(("-", "*", "•", ">")) or len(block) <= WRAP_WIDTH:
            out.append(block.rstrip())
        else:
            out.extend(textwrap.wrap(block, width=WRAP_WIDTH, break_long_words=False))
    return "\n".join(out).strip()


def build_body(
    body: str,
    *,
    language: str,
    display_name: str,
    reply_address: str,
    contact_lines: list[str] | None = None,
) -> str:
    """Approved text, signature, then the NFR-302 sentence, in that order.

    The objection sentence goes last and is separated by a rule so that it
    reads as a footnote rather than as part of the application - it must be
    present and findable, not prominent.
    """
    parts = [_wrap(body)]
    sig = signature(language, display_name, contact_lines)
    if sig and sig not in body:
        parts.append(sig)
    # The assembled e-mail body already ends with the NFR-302 sentence, so an
    # unconditional append put two differently-worded objection notices - and a
    # second signature - on every message that went out.
    objection = objection_sentence(language, reply_address)
    if objection and objection not in (body or ""):
        parts.append("-- \n" + _wrap(objection))
    return "\n\n".join(p for p in parts if p) + "\n"


def new_message_id(from_email: str) -> str:
    """An RFC 5322 id we own, generated before the send (FR-326)."""
    domain = from_email.rsplit("@", 1)[-1] if "@" in from_email else "dreamjob.local"
    return make_msgid(domain=domain)


def compose(
    package: dict,
    *,
    seeker: dict,
    from_email: str,
    subject: str | None = None,
    body: str | None = None,
    language: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: list[Attachment] | None = None,
    contact_lines: list[str] | None = None,
) -> OutgoingMessage:
    """Turn an approved application package into a sendable message.

    ``in_reply_to``/``references`` are set for a follow-up (FR-327) so that it
    threads under the original in the recipient's client instead of arriving as
    an unrelated second cold e-mail.
    """
    lang = normalise_language(language or package.get("language") or seeker.get("locale"))
    recipient = (package.get("contact_email") or "").strip()
    if not recipient:
        raise MailBackendError("The application package has no recipient address (FR-301).")

    subject_line = (subject or package.get("email_subject") or "").strip()
    if not subject_line:
        raise MailBackendError("The application package has no subject line.")

    text = body if body is not None else (package.get("email_body") or "")
    if not text.strip():
        raise MailBackendError("The application package has no approved e-mail body (FR-324).")

    reply_address = seeker.get("email") or from_email
    display_name = seeker.get("display_name") or reply_address
    package = {**package, "seeker_display_name": display_name}

    return OutgoingMessage(
        to_email=recipient,
        to_name=package.get("contact_name"),
        subject=subject_line,
        body_text=build_body(
            text,
            language=lang,
            display_name=display_name,
            reply_address=reply_address,
            contact_lines=contact_lines,
        ),
        from_email=from_email,
        from_name=display_name,
        reply_to=reply_address if reply_address.lower() != from_email.lower() else None,
        message_id=new_message_id(from_email),
        in_reply_to=in_reply_to,
        references=references,
        language=lang,
        attachments=attachments if attachments is not None else collect_attachments(package),
    )


def follow_up_subject(subject: str, language: str = "en") -> str:
    prefix = FOLLOW_UP_PREFIX.get(normalise_language(language), "Re: ")
    return subject if subject.lower().startswith(prefix.lower()) else prefix + subject


def to_mime(message: OutgoingMessage) -> EmailMessage:
    """Render the message as RFC 5322.

    ``Message-ID``, ``In-Reply-To`` and ``References`` are set here so that a
    reply threads and can be matched back to its dispatch (FR-326).  The body is
    the only text part: no ``multipart/alternative``, no HTML (RK-05).
    """
    mime = EmailMessage()
    mime["Subject"] = message.subject
    mime["From"] = _address(message.from_name, message.from_email)
    mime["To"] = _address(message.to_name, message.to_email)
    if message.reply_to:
        mime["Reply-To"] = message.reply_to
    mime["Date"] = format_datetime(datetime.now(UTC))
    mime["Message-ID"] = message.message_id or new_message_id(message.from_email)
    if message.in_reply_to:
        mime["In-Reply-To"] = message.in_reply_to
        mime["References"] = message.references or message.in_reply_to
    elif message.references:
        mime["References"] = message.references
    # No Auto-Submitted header.  FR-324 means a person reads and approves every
    # one of these before it leaves, so the message is not "generated without
    # human intervention" in RFC 3834's sense; declaring it as such would tell
    # the receiving system to treat a job application as bulk mail, which is
    # the classification RK-05 exists to avoid.
    for key, value in message.extra_headers.items():
        if key.lower() not in {h.lower() for h in mime.keys()}:
            mime[key] = value

    mime.set_content(message.body_text, subtype="plain", charset="utf-8")

    for attachment in message.attachments:
        mime.add_attachment(
            attachment.content,
            maintype=attachment.maintype,
            subtype=attachment.subtype,
            filename=attachment.filename,
        )
    return mime


def _address(display_name: str | None, email: str) -> str | Address:
    if not email or "@" not in email:
        raise MailBackendError(f"Not a usable e-mail address: {email!r}")
    local, _, domain = email.partition("@")
    if display_name:
        return Address(display_name=display_name, username=local, domain=domain)
    return email
