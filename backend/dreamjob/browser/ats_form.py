"""Pre-filling an ATS application form, then handing over (FR-328, NFR-305).

Some opportunities cannot be applied to by e-mail: the employer's ATS wants a
form.  This module drives the same attached browser session, fills the fields
it can map with confidence, uploads the documents the job seeker approved -
and then **stops**.  It never clicks submit, never dismisses a consent box and
never answers a question the job seeker has not answered: the last action of a
job application belongs to the applicant (NFR-305), and FR-328 says the system
pauses for the user to review and submit.

The mapping decision is a pure function (:func:`match_field`) so the part that
decides "this input is the phone number" is testable against real field names
without a browser, and the part that types is trivial.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from dreamjob.browser import pacing as pacing_mod
from dreamjob.browser.session import BrowserSession, BrowserUnavailable, safe_url
from dreamjob.security.audit import record_audit

log = logging.getLogger(__name__)

#: Hosts that carry application forms for many employers.  A prefill URL must be
#: one of these or the employer's own site: the automation browser is signed in
#: as the job seeker, and a pasted link that is neither would have their name,
#: email, cover letter and CV typed into whatever page answered.
KNOWN_ATS_HOSTS = frozenset(
    {
        "greenhouse.io", "lever.co", "myworkdayjobs.com", "workday.com",
        "personio.com", "personio.de", "teamtailor.com", "recruitee.com",
        "ashbyhq.com", "smartrecruiters.com", "workable.com", "bamboohr.com",
        "jobvite.com", "icims.com", "successfactors.com", "taleo.net",
        "oraclecloud.com", "applytojob.com", "breezy.hr", "jazzhr.com",
        "softgarden.io", "onlyfy.com", "join.com", "welcometothejungle.com",
        "jobs.smartrecruiters.com", "hr.onepagecrm.com",
    }
)


def host_allowed(url: str, extra_hosts: Iterable[str] = ()) -> bool:
    """True when ``url`` is the employer's own site or a known ATS host."""
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower().strip(".")
    if not host:
        return False
    allowed = set(KNOWN_ATS_HOSTS) | {
        str(extra).lower().strip(".") for extra in extra_hosts if extra
    }
    return any(host == base or host.endswith("." + base) for base in allowed)

#: Canonical fields Dream Job knows how to supply, and the words ATS vendors
#: use for them.  Ordered: the first canonical field whose aliases match wins,
#: so the more specific names come first.
FIELD_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("first_name", ("firstname", "first_name", "givenname", "given-name", "voornaam", "prenom")),
    (
        "last_name",
        ("lastname", "last_name", "familyname", "family-name", "surname", "achternaam", "nom"),
    ),
    ("full_name", ("fullname", "full_name", "yourname", "candidatename", "name", "naam")),
    ("email", ("email", "e-mail", "emailaddress", "mail")),
    ("phone", ("phone", "telephone", "tel", "mobile", "gsm", "telefoon")),
    ("linkedin", ("linkedin", "linkedinurl", "linkedin_profile")),
    ("website", ("website", "portfolio", "personalwebsite", "url", "github")),
    ("location", ("location", "city", "town", "woonplaats", "ville", "address")),
    (
        "cover_letter",
        ("coverletter", "cover_letter", "motivation", "motivatie", "lettre", "message"),
    ),
    ("resume", ("resume", "cv", "curriculum", "attachment", "upload")),
    ("notice_period", ("notice", "noticeperiod", "availability", "startdate", "beschikbaarheid")),
    ("salary_expectation", ("salary", "compensation", "expectedsalary", "loon", "salaire")),
)

#: Controls that submit the form.  Listed so they can be *avoided*: nothing in
#: this module clicks them, and the report names them for the user instead.
SUBMIT_TOKENS: tuple[str, ...] = (
    "submit", "apply", "send application", "send", "solliciteer", "postuler", "verstuur",
)

#: Never touched: authentication and consent are the applicant's business.
FORBIDDEN_TYPES = frozenset({"password", "hidden", "submit", "button", "image", "reset"})
FORBIDDEN_TOKENS = ("password", "consent", "gdpr", "privacy", "terms", "agree", "captcha")


def _norm(value: str | None) -> str:
    return "".join(ch for ch in (value or "").lower() if ch.isalnum() or ch in "-_ ")


def match_field(
    *,
    name: str | None = None,
    label: str | None = None,
    placeholder: str | None = None,
    autocomplete: str | None = None,
    input_type: str | None = None,
) -> str | None:
    """Which canonical field this control is, or ``None`` to leave it alone.

    Pure and ordered: the attributes are searched from the most reliable
    (``name``/``autocomplete``) to the least (``placeholder``), and any control
    that smells of a password, a consent tick or a captcha is refused outright.
    """
    if (input_type or "").lower() in FORBIDDEN_TYPES:
        return None
    haystack = " ".join(_norm(v) for v in (name, autocomplete, label, placeholder) if v)
    if not haystack.strip():
        return None
    if any(token in haystack for token in FORBIDDEN_TOKENS):
        return None
    if any(token in haystack for token in SUBMIT_TOKENS) and (input_type or "") != "text":
        return None
    compact = haystack.replace(" ", "").replace("-", "").replace("_", "")
    for canonical, aliases in FIELD_ALIASES:
        if any(alias.replace("-", "").replace("_", "") in compact for alias in aliases):
            return canonical
    return None


def is_submit_control(
    *, name: str | None = None, value: str | None = None, text: str | None = None,
    input_type: str | None = None,
) -> bool:
    """True for anything that would send the application.  Used to stay away."""
    if (input_type or "").lower() in {"submit", "image"}:
        return True
    haystack = " ".join(_norm(v) for v in (name, value, text) if v)
    return any(token in haystack for token in SUBMIT_TOKENS)


@dataclass
class FormField:
    selector: str
    canonical: str | None
    kind: str                 # text | textarea | select | file | checkbox
    label: str = ""
    name: str = ""
    required: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "canonical": self.canonical,
            "kind": self.kind,
            "label": self.label,
            "name": self.name,
            "required": self.required,
        }


@dataclass
class PrefillReport:
    """FR-328: what was filled, what the user must finish, and that nothing was sent."""

    url: str
    filled: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    submitted: bool = False   # always False - only the applicant submits
    error: str | None = None

    @property
    def instructions(self) -> str:
        return (
            "The form is pre-filled in your browser window. Review every field, complete "
            "anything left blank, attach or replace documents as needed, and press the "
            "employer's submit button yourself. Dream Job never submits an application "
            "for you (FR-328)."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "filled": self.filled,
            "skipped": self.skipped,
            "unmatched": self.unmatched,
            "attachments": self.attachments,
            "submitted": self.submitted,
            "error": self.error,
            "instructions": self.instructions,
        }


_TEXT_INPUT_TYPES = frozenset({"", "text", "email", "tel", "url", "search", "number", "date"})


async def discover_fields(session: BrowserSession) -> list[FormField]:
    """Read the form's controls and decide what each one is (FR-328)."""
    page = await session.page()
    fields: list[FormField] = []
    elements = await page.query_selector_all("input, textarea, select")
    for element in elements:
        try:
            tag = (await element.evaluate("el => el.tagName")).lower()
            input_type = (await element.get_attribute("type") or "").lower()
            name = await element.get_attribute("name") or ""
            element_id = await element.get_attribute("id") or ""
            placeholder = await element.get_attribute("placeholder") or ""
            autocomplete = await element.get_attribute("autocomplete") or ""
            required = await element.get_attribute("required") is not None
            label = await element.evaluate(
                """
                el => {
                  const byFor = el.id
                    ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]')
                    : null;
                  const wrapper = el.closest('label');
                  const node = byFor || wrapper;
                  return node ? (node.innerText || '').trim().slice(0, 120) : '';
                }
                """
            )
        except Exception as exc:  # noqa: BLE001 - a detached node is not a failure
            log.debug("Could not inspect a form control: %s", exc)
            continue

        if tag == "input" and input_type == "file":
            kind = "file"
        elif tag == "textarea":
            kind = "textarea"
        elif tag == "select":
            kind = "select"
        elif tag == "input" and input_type in {"checkbox", "radio"}:
            kind = "checkbox"
        elif tag == "input" and input_type in _TEXT_INPUT_TYPES:
            kind = "text"
        else:
            continue

        if not element_id and not name:
            continue
        selector = f"#{element_id}" if element_id else f'[name="{name}"]'
        fields.append(
            FormField(
                selector=selector,
                canonical=match_field(
                    name=name,
                    label=label,
                    placeholder=placeholder,
                    autocomplete=autocomplete,
                    input_type=input_type,
                ),
                kind=kind,
                label=label or placeholder,
                name=name,
                required=required,
            )
        )
    return fields


def plan_prefill(
    fields: list[FormField], values: dict[str, str]
) -> tuple[list[FormField], list[FormField]]:
    """Split discovered fields into "we can fill this" and "the user must".

    Pure, so the safety property - a checkbox, a consent tick or an unmapped
    required field is never filled by the machine - is testable.
    """
    fillable: list[FormField] = []
    leave: list[FormField] = []
    for field_ in fields:
        value = values.get(field_.canonical or "")
        if field_.kind == "checkbox" or not field_.canonical or value in (None, ""):
            leave.append(field_)
        else:
            fillable.append(field_)
    return fillable, leave


async def prefill(
    url: str,
    values: dict[str, str],
    *,
    attachments: dict[str, str] | None = None,
    session: BrowserSession | None = None,
    pacing: pacing_mod.Pacing | None = None,
    job_seeker_id: str | None = None,
    opportunity_id: str | None = None,
    allowed_hosts: Iterable[str] | None = None,
) -> PrefillReport:
    """Open the ATS form, fill what can be filled, and pause for the user (FR-328).

    ``attachments`` maps a canonical file field (``resume``, ``cover_letter``)
    to a local path.  The session is left open and raised to the front: the
    user reviews and submits.  ``allowed_hosts`` pins the destination to the
    employer's own site or a known ATS; a caller that passes none accepts any
    URL, which only the tests do.
    """
    report = PrefillReport(url=safe_url(url))
    if allowed_hosts is not None and not host_allowed(url, allowed_hosts):
        report.error = (
            "This page is neither the employer's own site nor a known applicant-tracking "
            "site, so nothing was filled. Open the employer's application page and try again."
        )
        return report
    pacing = pacing or pacing_mod.Pacing.from_settings()
    own_session = session is None
    session = session or BrowserSession()
    try:
        if own_session:
            await session.connect()
        load = await session.goto(url)
        html = await session.content()
        verdict = pacing_mod.detect_challenge(load.url, html, status=load.status, title=load.title)
        if verdict.detected:
            report.error = f"The ATS showed a {verdict.kind} page; nothing was filled (FR-203)."
            return report

        fields = await discover_fields(session)
        fillable, leave = plan_prefill(fields, values)
        page = await session.page()

        for form_field in fillable:
            value = values[form_field.canonical or ""]
            try:
                if form_field.kind == "select":
                    await page.select_option(form_field.selector, label=value)
                else:
                    await page.fill(form_field.selector, value)
                report.filled.append({**form_field.as_dict(), "value_length": len(value)})
            except Exception as exc:  # noqa: BLE001 - one field is not the form
                log.info("Could not fill %s: %s", form_field.selector, exc)
                report.skipped.append({**form_field.as_dict(), "reason": str(exc)[:200]})
            await pacing.wait()  # FR-203: type at human pace

        for form_field in fields:
            if form_field.kind != "file" or not attachments:
                continue
            path = attachments.get(form_field.canonical or "") or attachments.get("resume")
            if not path:
                continue
            try:
                await page.set_input_files(form_field.selector, path)
                report.attachments.append(path)
            except Exception as exc:  # noqa: BLE001
                log.info("Could not attach %s: %s", path, exc)
                report.skipped.append({**form_field.as_dict(), "reason": str(exc)[:200]})

        report.unmatched = [f.as_dict() for f in leave]
        await session.bring_to_front()  # hand the window back to the user
    except BrowserUnavailable as exc:
        report.error = str(exc)
    finally:
        # The session stays open on purpose: closing it would take the
        # half-completed application away from the user.
        if own_session and report.error:
            await session.close()

    record_audit(
        "application.form_prefilled",
        "opportunity",
        opportunity_id,
        seeker_id=job_seeker_id,
        detail={
            "url": report.url,
            "filled": [f["canonical"] for f in report.filled],
            "attachments": len(report.attachments),
            "submitted": False,  # FR-328: never
            "error": report.error,
        },
    )
    return report
