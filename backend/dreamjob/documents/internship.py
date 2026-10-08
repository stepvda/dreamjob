"""The internship terms an application asks for (Apply Browser).

A job seeker applying for an internship rather than a job states three things
the recipient needs before anything else: how long, from when, and whether it
is paid.  Those are facts the job seeker chose on the Apply screen, not prose,
so they are written here as one fixed sentence per language and appended to the
e-mail body by the generator - the same treatment as the attachment line.

Two consequences follow, and both are why this is not left to the prompt:

* a model that paraphrases "six months from March 2027" can get it wrong, and a
  wrong start date in an application is worse than none;
* the FR-322 / NFR-206 checks reject any figure or year in the e-mail that the
  profile does not contain.  The sentence is recorded on the package and handed
  to those checks as the system's own wording, so a correct internship request
  does not block its own approval.
"""

from __future__ import annotations

import re
from typing import Any

from dreamjob.documents.pdf_builder import normalise_language

PAY_OPTIONS: tuple[str, ...] = ("paid", "unpaid", "either")
MAX_DURATION_MONTHS = 24

_START_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")

MONTHS: dict[str, tuple[str, ...]] = {
    "en": (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ),
    "nl": (
        "januari", "februari", "maart", "april", "mei", "juni",
        "juli", "augustus", "september", "oktober", "november", "december",
    ),
    "fr": (
        "janvier", "février", "mars", "avril", "mai", "juin",
        "juillet", "août", "septembre", "octobre", "novembre", "décembre",
    ),
    "de": (
        "Januar", "Februar", "März", "April", "Mai", "Juni",
        "Juli", "August", "September", "Oktober", "November", "Dezember",
    ),
}

#: ``{pay}`` is the adjective, ``{duration}`` and ``{start}`` the optional clauses.
_SENTENCE: dict[str, str] = {
    "en": "I am looking for {pay}internship{duration}{start}.",
    "nl": "Ik zoek {pay}stage{duration}{start}.",
    "fr": "Je recherche un stage{pay}{duration}{start}.",
    "de": "Ich suche ein {pay}Praktikum{duration}{start}.",
}

_PAY: dict[str, dict[str, str]] = {
    "en": {"paid": "a paid ", "unpaid": "an unpaid ", "either": "an "},
    "nl": {"paid": "een betaalde ", "unpaid": "een onbetaalde ", "either": "een "},
    "fr": {"paid": " rémunéré", "unpaid": " non rémunéré", "either": ""},
    "de": {"paid": "bezahltes ", "unpaid": "unbezahltes ", "either": ""},
}

_EITHER: dict[str, str] = {
    "en": "Paid or unpaid, either would suit me.",
    "nl": "Betaald of onbetaald, beide zijn mogelijk voor mij.",
    "fr": "Rémunéré ou non, les deux me conviennent.",
    "de": "Ob bezahlt oder unbezahlt, beides ist für mich möglich.",
}


def _duration(lang: str, months: int) -> str:
    if lang == "en":
        return f" of {months} month{'s' if months != 1 else ''}"
    if lang == "nl":
        return f" van {months} maand{'en' if months != 1 else ''}"
    if lang == "fr":
        return f" de {months} mois"
    return f" von {months} Monat{'en' if months != 1 else ''}"


def _start(lang: str, start: str) -> str:
    match = _START_RE.match(start)
    if not match:
        return ""
    when = f"{MONTHS[lang][int(match.group(2)) - 1]} {match.group(1)}"
    return {
        "en": f", starting in {when}",
        "nl": f", vanaf {when}",
        "fr": f", à partir de {when}",
        "de": f", ab {when}",
    }[lang]


def normalise(values: dict[str, Any] | None) -> dict[str, Any]:
    """The stored preference in one shape, whatever was stored or sent."""
    values = values or {}
    months = values.get("duration_months")
    try:
        months = int(months) if months not in (None, "") else None
    except (TypeError, ValueError):
        months = None
    if months is not None and not 1 <= months <= MAX_DURATION_MONTHS:
        months = None
    start = str(values.get("start_month") or "").strip()
    pay = str(values.get("pay") or "either")
    return {
        "internship": bool(values.get("internship")),
        "duration_months": months,
        "start_month": start if _START_RE.match(start) else None,
        "pay": pay if pay in PAY_OPTIONS else "either",
    }


def sentence(preference: dict[str, Any] | None, language: str) -> str:
    """The fixed sentence for this preference, or "" when it is not an internship."""
    pref = normalise(preference)
    if not pref["internship"]:
        return ""
    lang = normalise_language(language)
    text = _SENTENCE[lang].format(
        pay=_PAY[lang][pref["pay"]],
        duration=_duration(lang, pref["duration_months"]) if pref["duration_months"] else "",
        start=_start(lang, pref["start_month"] or ""),
    )
    if pref["pay"] == "either":
        text = f"{text} {_EITHER[lang]}"
    return text


def prompt_rule(preference: dict[str, Any] | None) -> str:
    """What the model is told, so the body around the sentence agrees with it."""
    if not normalise(preference)["internship"]:
        return ""
    return (
        "The job seeker is applying for an internship, not a permanent position: write "
        "the e-mail as an internship application. A sentence stating the duration, start "
        "and pay is added by the generator afterwards, so do not mention a duration, a "
        "start date or pay yourself."
    )
