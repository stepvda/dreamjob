"""Cross-source de-duplication of collected entities (FR-184).

Two record types arrive from many sources at once and must collapse onto one
knowledge-base row:

* **Companies** are matched on the strongest available identifier first - a
  legal identifier (KBO/BCE, Companies House number, LEI), then a VAT number,
  then the web domain, then the normalised trading name.  Only the last step is
  fuzzy, and only within the same jurisdiction.
* **Vacancies** have no identifier at all, so they are matched on a fuzzy
  combination of title, company, location and posting date.  ``dedup_key``
  stores the deterministic part of that comparison so the common case is an
  index lookup; the fuzzy comparison then catches the near misses (a reposted
  vacancy, a different word order, a board that appends the city to the title).

The similarity measure is a token-set ratio built on ``difflib``: word order
and duplicated qualifiers stop mattering, which is what distinguishes
"Data Engineer (m/v) - Gent" from "Gent | Data engineer" as the same posting.
No third-party fuzzy-matching dependency is used.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any

# Legal forms carry no identity: "Acme NV" and "Acme" are the same company.
LEGAL_FORMS = frozenset(
    """
    nv sa bv bvba sprl srl cv cvba cvoa scrl scs sca comm va vof snc gcv se
    vzw asbl ivzw aisbl esv
    ltd limited plc llp lp llc lc inc incorporated corp corporation co company
    gmbh mbh ag kg kgaa ohg ug gbr eg
    sas sasu sarl eurl sci scop sa/nv
    spa srls snc sapa
    oy oyj ab abp as asa aps a/s hf ehf
    bhd sdn pty pte pvt
    holdings
    """.split()
)

# Words that add no identity to a job title.
TITLE_STOPWORDS = frozenset(
    """
    a an the de het een la le les du des der die das el los
    and en et und of in at voor pour for fur to
    m v f d x h w mv mvx fh hf mfd mfx
    job vacancy vacature offre stelle position role opening opportunity
    fulltime full-time parttime part-time fte
    """.split()
)

_DOTTED_ABBREV = re.compile(r"\b(?:[a-z]\.){2,}")
_NON_WORD = re.compile(r"[^0-9a-z]+")
_BRACKETED = re.compile(r"[(\[{][^)\]}]*[)\]}]")
_POSTCODE = re.compile(r"\b\d{4,6}\b")


# ---------------------------------------------------------------------------
# Normalisers
# ---------------------------------------------------------------------------


def strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def tokens(value: str) -> list[str]:
    """Lower-case alphanumeric tokens, accents folded, abbreviations joined."""
    text = strip_accents(value or "").lower()
    text = _DOTTED_ABBREV.sub(lambda m: m.group(0).replace(".", ""), text)
    text = text.replace("&", " and ")
    return [t for t in _NON_WORD.split(text) if t]


def normalise_company_name(name: str | None) -> str:
    """Trading name with legal form, punctuation and case removed (FR-184).

    Falls back to the punctuation-stripped name when a company is called only
    by its legal form (rare, but "The Company Ltd" must not normalise to "").
    """
    parts = tokens(name or "")
    if not parts:
        return ""
    stripped = [t for t in parts if t not in LEGAL_FORMS]
    return " ".join(stripped or parts)


def normalise_domain(value: str | None) -> str | None:
    """Bare registrable host: no scheme, credentials, port, path or ``www.``."""
    if not value:
        return None
    text = str(value).strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/")[0].split("?")[0].split("#")[0]
    text = text.rsplit("@", 1)[-1].split(":")[0].strip(".")
    if text.startswith("www."):
        text = text[4:]
    if not text or "." not in text:
        return None
    return text


def normalise_legal_id(value: str | None, id_type: str | None = None) -> str | None:
    """Upper-case alphanumeric identifier, with the Belgian 10-digit padding.

    KBO/BCE enterprise numbers are frequently written without their leading
    zero ("0123.456.749" vs "123456749"); both must land on the same key.
    """
    if not value:
        return None
    text = re.sub(r"[^0-9A-Za-z]", "", str(value)).upper()
    if not text:
        return None
    kind = (id_type or "").lower()
    if kind in {"kbo", "bce", "kbo_bce", "vat", "btw", "tva"} or text.startswith("BE"):
        digits = text[2:] if text.startswith("BE") else text
        if digits.isdigit() and len(digits) == 9:
            digits = "0" + digits
        text = ("BE" if text.startswith("BE") else "") + digits
    return text


def normalise_location(value: str | None) -> str:
    """City-level location key: postcodes and country suffixes contribute noise."""
    parts = [t for t in tokens(value or "") if not t.isdigit()]
    return " ".join(sorted(set(parts)))


def normalise_title(value: str | None) -> str:
    """Job title reduced to its identifying words, order-independent."""
    text = _BRACKETED.sub(" ", value or "")
    text = _POSTCODE.sub(" ", text)  # a postcode in the title is location, not identity
    # Small numbers are kept: "Support Engineer 2" is not "Support Engineer 3".
    parts = [t for t in tokens(text) if t not in TITLE_STOPWORDS]
    return " ".join(sorted(set(parts)))


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def _ratio(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def token_set_ratio(a: str, b: str) -> float:
    """Order-independent similarity in ``0..1`` (no third-party dependency).

    The shared tokens are compared against each side's full token set, so a
    string that merely adds qualifiers to the other still scores highly.
    """
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0
    common = " ".join(sorted(ta & tb))
    left = " ".join(sorted(ta & tb) + sorted(ta - tb)).strip()
    right = " ".join(sorted(ta & tb) + sorted(tb - ta)).strip()
    return max(_ratio(common, left), _ratio(common, right), _ratio(left, right))


def company_similarity(a: str | None, b: str | None) -> float:
    """Similarity of two trading names after legal-form stripping."""
    na, nb = normalise_company_name(a), normalise_company_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    score = token_set_ratio(na, nb)
    # "Acme" vs "Acme Belgium": a full prefix match is a strong signal.
    if na.startswith(nb + " ") or nb.startswith(na + " "):
        score = max(score, 0.93)
    return score


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def date_proximity(a: Any, b: Any, window_days: int = 14) -> float:
    """1.0 for the same day, decaying to 0.0 at ``window_days`` apart."""
    da, db = _parse_date(a), _parse_date(b)
    if da is None or db is None:
        return 0.5  # unknown posting dates neither confirm nor deny
    delta = abs((da - db).days)
    if delta >= window_days:
        return 0.0
    return 1.0 - delta / window_days


# ---------------------------------------------------------------------------
# Vacancy keys (FR-184)
# ---------------------------------------------------------------------------


def _week_bucket(posted_at: Any) -> str:
    d = _parse_date(posted_at)
    if d is None:
        return "nodate"
    iso = d.isocalendar()
    return f"{iso[0]}w{iso[1]:02d}"


def vacancy_dedup_key(
    title: str | None,
    company: str | None,
    location: str | None = None,
    posted_at: Any = None,
) -> str:
    """Deterministic part of the vacancy identity (written to ``vacancy.dedup_key``).

    Title, company and location are normalised; the posting date is bucketed by
    ISO week so that the same posting re-collected a few days later still keys
    the same.  Across a week boundary the key changes, which is why the writer
    always follows a key miss with the fuzzy comparison below.
    """
    parts = "|".join(
        (
            normalise_title(title),
            normalise_company_name(company),
            normalise_location(location),
            _week_bucket(posted_at),
        )
    )
    return hashlib.sha1(parts.encode("utf-8")).hexdigest()[:32]


def assign_dedup_key(vacancy: dict) -> str:
    """Set and return ``vacancy['dedup_key']`` for a knowledge-base row (FR-184)."""
    key = vacancy_dedup_key(
        vacancy.get("title"),
        vacancy.get("company_name_raw") or vacancy.get("company_name"),
        vacancy.get("location"),
        vacancy.get("posted_at"),
    )
    vacancy["dedup_key"] = key
    return key


VACANCY_MATCH_THRESHOLD = 0.86

# A token-set ratio treats a subset as a perfect match, which is right for
# "Data Engineer" vs "Gent | Data engineer" but wrong for "Data Engineer" vs
# "Senior Data Engineer": those are two openings, not one record seen twice.
LEVEL_MARKERS = frozenset(
    """
    senior sr junior jr medior lead leading principal staff head chief
    intern internship graduate trainee associate assistant deputy
    director manager officer
    i ii iii iv v 1 2 3 4 5
    """.split()
)


def title_similarity(a: str | None, b: str | None) -> float:
    """Title match that refuses to merge two different seniority levels (FR-184)."""
    ta, tb = set(tokens(normalise_title(a))), set(tokens(normalise_title(b)))
    if not ta or not tb:
        return 0.0
    ratio = token_set_ratio(" ".join(sorted(ta)), " ".join(sorted(tb)))
    if (ta ^ tb) & LEVEL_MARKERS:
        return min(0.5, ratio)
    return ratio


def vacancy_similarity(a: dict, b: dict) -> float:
    """Fuzzy title+company+location+posting-date match in ``0..1`` (FR-184)."""
    title = title_similarity(a.get("title"), b.get("title"))
    company = company_similarity(
        a.get("company_name_raw") or a.get("company_name"),
        b.get("company_name_raw") or b.get("company_name"),
    )
    if not company and a.get("company_id") and a.get("company_id") == b.get("company_id"):
        company = 1.0
    location = token_set_ratio(
        normalise_location(a.get("location")), normalise_location(b.get("location"))
    )
    if not a.get("location") or not b.get("location"):
        location = 0.6
    recency = date_proximity(a.get("posted_at"), b.get("posted_at"))
    return 0.45 * title + 0.30 * company + 0.15 * location + 0.10 * recency


def is_duplicate_vacancy(a: dict, b: dict, threshold: float = VACANCY_MATCH_THRESHOLD) -> bool:
    if a.get("dedup_key") and a.get("dedup_key") == b.get("dedup_key"):
        return True
    return vacancy_similarity(a, b) >= threshold


def best_vacancy_match(
    candidate: dict, existing: list[dict], threshold: float = VACANCY_MATCH_THRESHOLD
) -> tuple[dict | None, float]:
    best: dict | None = None
    best_score = 0.0
    for row in existing:
        score = vacancy_similarity(candidate, row)
        if score > best_score:
            best, best_score = row, score
    return (best, best_score) if best_score >= threshold else (None, best_score)


# ---------------------------------------------------------------------------
# Company keys (FR-184, DR-101)
# ---------------------------------------------------------------------------

COMPANY_MATCH_THRESHOLD = 0.92


@dataclass(frozen=True)
class CompanyKey:
    """One identity claim for a company, strongest first."""

    kind: str  # legal_id | vat | domain | normalised_name
    value: str
    extra: str | None = None  # legal_id_type / country


def company_keys(data: dict) -> list[CompanyKey]:
    """Ordered identity claims for a company record (DR-101 priority order)."""
    keys: list[CompanyKey] = []
    legal = normalise_legal_id(data.get("legal_id"), data.get("legal_id_type"))
    if legal:
        keys.append(CompanyKey("legal_id", legal, data.get("legal_id_type")))
    vat = normalise_legal_id(data.get("vat_number"), "vat")
    if vat:
        keys.append(CompanyKey("vat", vat))
    domain = normalise_domain(data.get("domain") or data.get("website") or data.get("careers_url"))
    if domain:
        keys.append(CompanyKey("domain", domain))
    nname = data.get("normalised_name") or normalise_company_name(data.get("name"))
    if nname:
        country = (data.get("country") or "").upper() or None
        keys.append(CompanyKey("normalised_name", nname, country))
    return keys


def best_company_match(
    candidate: dict, existing: list[dict], threshold: float = COMPANY_MATCH_THRESHOLD
) -> tuple[dict | None, float]:
    """Fuzzy fall-back once identifier and domain lookups have missed."""
    country = (candidate.get("country") or "").upper()
    best: dict | None = None
    best_score = 0.0
    for row in existing:
        row_country = (row.get("country") or "").upper()
        if country and row_country and country != row_country:
            continue
        score = company_similarity(candidate.get("name"), row.get("name"))
        if score > best_score:
            best, best_score = row, score
    return (best, best_score) if best_score >= threshold else (None, best_score)
