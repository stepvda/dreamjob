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

A plausible similarity measure destroys a corpus quietly, because a wrong merge
looks exactly like a successful de-duplication: on real boards the previous
settings merged 18% of genuinely distinct openings and reduced a 227-posting
board to six rows (docs/Data_Gathering_Plan.md section 3 step 9 and section 5,
C4/N6).  Three rules keep the fuzzy comparison honest, and each of them is a
regression test in ``tests/unit/test_dedup_guards.py``: two stated, different
locations veto a merge outright; a differing number is a level or a reference,
never noise; and an unknown location no longer scores high enough to carry a
pair over the threshold by itself.
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
_BRACKETED_TEXT = re.compile(r"[(\[{]([^)\]}]*)[)\]}]")

# A number that introduces a place is a postcode - "9000 Gent", "1012 AB
# Amsterdam" - and says where, not which opening.  A number that stands on its
# own is a level or a reference ("Magazijnier 100234", "Support Engineer 6") and
# is identity, so only the postcode shape is stripped (FR-184).  Benelux
# postcodes are four digits, optionally followed by the Dutch two-letter suffix.
_POSTCODE_BEFORE_PLACE = re.compile(r"\b\d{4}(?:\s?[A-Za-z]{2})?\b(?=\s+[^\W\d_])")

# Contract terms, not identity: "(80%)", "0,8 FTE", "38u/week", "(4/5)".
_WORKLOAD = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:%|(?:fte|vte|etp|uur|uren|hours?|heures|std|u|h)\b)",
    re.IGNORECASE,
)
_WORKLOAD_FRACTION = re.compile(r"\b\d\s*/\s*\d\b")


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


def _bears_digit(token: str) -> bool:
    return any(ch.isdigit() for ch in token)


def _strip_workload(value: str) -> str:
    """Remove the part-time notations that say *how much*, not *which opening*."""
    return _WORKLOAD_FRACTION.sub(" ", _WORKLOAD.sub(" ", value))


def title_numbers(value: str | None) -> set[str]:
    """The numeric tokens a title uses as a level or a reference (FR-184).

    Brackets are read too, because "Support Engineer (Level 6)" and
    "(Level 7)" are two openings and ``normalise_title`` used to throw both the
    bracket and the number away, which merged them at 0.96
    (docs/Data_Gathering_Plan.md section 5, N6).  A postcode and a workload are
    not references, so they are removed before the numbers are read.
    """
    text = _POSTCODE_BEFORE_PLACE.sub(" ", _strip_workload(value or ""))
    return {t for t in tokens(text) if _bears_digit(t)}


def normalise_title(value: str | None) -> str:
    """Job title reduced to its identifying words, order-independent.

    Numbers are identity (FR-184): "Magazijnier 100234" and "Magazijnier
    100567" are two openings with two references, and stripping every 4-6 digit
    group as a postcode collapsed a whole board onto one row per role name.
    Only a postcode that introduces a place ("9000 Gent") and a workload
    ("(80%)", "4/5", "0,8 FTE") are dropped; every other number survives,
    including one inside a bracket that the prose part of this function throws
    away.
    """
    text = _strip_workload(value or "")
    numbers = title_numbers(value)
    text = _BRACKETED.sub(" ", text)
    text = _POSTCODE_BEFORE_PLACE.sub(" ", text)
    parts = [t for t in tokens(text) if t not in TITLE_STOPWORDS]
    return " ".join(sorted(set(parts) | numbers))


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def _ratio(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _vocabulary_overlap(ta: set[str], tb: set[str]) -> float:
    """How much of the *identity-bearing* vocabulary the two sides share.

    Noise words - gender markers, articles, "fulltime" - are excluded, so
    "Data Engineer (m/v) - Gent" and "Gent | Data engineer" still overlap
    completely, while "Product Manager" and "Product Marketing Manager" do not.
    """
    sa, sb = ta - TITLE_STOPWORDS, tb - TITLE_STOPWORDS
    if sa == sb:
        return 1.0
    union = sa | sb
    if not union:
        return 1.0
    return len(sa & sb) / len(union)


def token_set_ratio(a: str, b: str) -> float:
    """Order-independent similarity in ``0..1`` (no third-party dependency).

    The shared tokens are compared against each side's full token set, so word
    order and duplicated qualifiers stop mattering.  That string comparison
    alone scores a *subset* as a perfect match - "product manager" against
    "product marketing manager" is 1.000 - which merged distinct openings onto
    one row (FR-184), so it is scaled by how much identity-bearing vocabulary
    the two sides actually share.
    """
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0
    common = " ".join(sorted(ta & tb))
    left = " ".join(sorted(ta & tb) + sorted(ta - tb)).strip()
    right = " ".join(sorted(ta & tb) + sorted(tb - ta)).strip()
    ratio = max(_ratio(common, left), _ratio(common, right), _ratio(left, right))
    return ratio * _vocabulary_overlap(ta, tb)


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


# Measured on real boards (docs/Data_Gathering_Plan.md section 3 step 9 and
# section 5 C4): 0.86 merged 18% of genuinely distinct openings, 0.92 retains
# 95.5%.  Nothing that matters is lost by raising it - an exact repost still
# collapses on ``dedup_key`` without ever reaching the fuzzy comparison.
VACANCY_MATCH_THRESHOLD = 0.92

# A subset is not an identity: "Data Engineer" vs "Gent | Data engineer" is one
# posting, but "Data Engineer" vs "Senior Data Engineer" - or vs "Data Engineer,
# Unstructured AI" - is two openings, not one record seen twice.
#
# Numerals beyond v are covered by the numeric guard in ``title_similarity``;
# "x" is deliberately absent because Benelux boards use it as a gender marker
# ("m/v/x"), not as a level.
LEVEL_MARKERS = frozenset(
    """
    senior sr junior jr medior lead leading principal staff head chief
    intern internship graduate trainee associate assistant deputy
    director manager officer
    i ii iii iv v vi vii viii ix
    1 2 3 4 5
    """.split()
)

# Ceiling for a title that names something the other title does not.  On a
# single employer's board, company, location and recency can all score 1.0 by
# construction, which hands any pair 0.55 for free; 0.45 * 0.5 + 0.55 = 0.775
# keeps such a pair below VACANCY_MATCH_THRESHOLD however perfectly the rest of
# the record agrees.
DISTINCT_TITLE_CEILING = 0.5


def title_qualifiers(value: str | None) -> set[str]:
    """Identity-bearing words inside brackets.

    ``normalise_title`` throws bracketed text away, which is right for
    "(m/v)" and wrong for "Implementation Specialist - Americas (EST)" against
    "... (PST)", or "Account Executive - Americas (West)" against "(East)":
    those are two openings on one real board, and dropping the bracket made
    them identical.
    """
    out: set[str] = set()
    for inner in _BRACKETED_TEXT.findall(value or ""):
        out |= {t for t in tokens(inner) if t not in TITLE_STOPWORDS and not t.isdigit()}
    return out


def title_similarity(
    a: str | None, b: str | None, *, ignore: frozenset[str] | set[str] = frozenset()
) -> float:
    """Title match that refuses to merge two different openings (FR-184).

    ``ignore`` carries the words a board may glue onto a title without changing
    which opening it is - the city and the employer's own name - so
    "Data Engineer - Gent" still matches "Data Engineer" at that employer in
    Gent, while "Senior AI Engineer, Unstructured AI" does not match
    "Senior AI Engineer": "unstructured" names a different role.
    """
    ta, tb = set(tokens(normalise_title(a))), set(tokens(normalise_title(b)))
    if not ta or not tb:
        return 0.0
    ratio = token_set_ratio(" ".join(sorted(ta)), " ".join(sorted(tb)))
    difference = (ta ^ tb) | (title_qualifiers(a) ^ title_qualifiers(b))
    if difference & LEVEL_MARKERS or any(_bears_digit(t) for t in difference):
        # Seniority is never contextual noise, whatever the employer is called,
        # and neither is a number: a differing digit marks a level or a
        # reference ("Support Engineer 6" / "7", "Magazijnier 100234" /
        # "100567"), never the city or the employer that ``ignore`` covers.
        return min(DISTINCT_TITLE_CEILING, ratio)
    distinguishing = difference - TITLE_STOPWORDS - set(ignore)
    if not distinguishing:
        return 1.0
    return min(DISTINCT_TITLE_CEILING, ratio)


def contextual_tokens(*records: dict) -> set[str]:
    """Words that identify the employer or the place, not the opening."""
    out: set[str] = set()
    for record in records:
        out |= set(tokens(normalise_location(record.get("location"))))
        out |= set(
            tokens(
                normalise_company_name(
                    record.get("company_name_raw") or record.get("company_name")
                )
            )
        )
        out |= set(tokens(str(record.get("country") or "")))
    return out


# An unknown location is a missing fact, not a match.  It used to score 0.6,
# which at the old 0.86 threshold carried a pair over the line on its own
# (0.45 + 0.25 + 0.15 + 0.025 = 0.875): two postings that agreed on title and
# employer merged purely because neither said where it was.  At 0.5 the best a
# pair with an unknown location can reach is 0.875, below the threshold, so an
# absent location can no longer decide a merge (docs/Data_Gathering_Plan.md
# section 5, N6).
UNKNOWN_LOCATION_SIMILARITY = 0.5


def locations_conflict(a: str | None, b: str | None) -> bool:
    """True when two records name places that cannot be the same place (FR-184).

    Only a *stated* difference counts.  "Gent, Belgium" against "9000 Gent" is
    one place said twice - one side merely adds the country - so it is not a
    conflict; "Brussels" against "Ghent" is.  An unknown location on either side
    conflicts with nothing, because it claims nothing.
    """
    la, lb = set(tokens(normalise_location(a))), set(tokens(normalise_location(b)))
    if not la or not lb:
        return False
    return not (la <= lb or lb <= la)


def location_similarity(a: str | None, b: str | None) -> float:
    """Place match that tells "same place, said twice" from "two places".

    One side naming the country the other omits - "Gent, Belgium" against
    "9000 Gent" - is the same opening.  Two sides that each name a place the
    other does not - "Remote, USA" against "Remote, Europe" - are two openings
    at the same employer, which is why ``vacancy_dedup_key`` puts the location
    in the key in the first place.
    """
    la, lb = set(tokens(normalise_location(a))), set(tokens(normalise_location(b)))
    if not la or not lb:
        return UNKNOWN_LOCATION_SIMILARITY  # unknown: neither confirms nor denies
    if la == lb:
        return 1.0
    if la <= lb or lb <= la:
        return 0.9
    return token_set_ratio(" ".join(sorted(la)), " ".join(sorted(lb)))


def vacancy_similarity(a: dict, b: dict) -> float:
    """Fuzzy title+company+location+posting-date match in ``0..1`` (FR-184).

    The weights carry a rule the deterministic key already states: at one
    employer, two postings in different places are two openings.  Title,
    employer and posting date alone reach 0.75, so the location is given enough
    weight (0.25) that a genuine conflict keeps the pair under the threshold -
    Collibra's "Senior Product Security Engineer" in Remote/USA, Remote/Europe
    and Raleigh are three jobs, and used to collapse into one.

    Two stated, different places are a veto rather than a low score: "Data
    Engineer" in Brussels and "Data Engineer" in Ghent are two vacancies at one
    employer, and no agreement on title, employer and date may outvote that
    (docs/Data_Gathering_Plan.md section 5, N6).
    """
    if locations_conflict(a.get("location"), b.get("location")):
        return 0.0
    title = title_similarity(a.get("title"), b.get("title"), ignore=contextual_tokens(a, b))
    company = company_similarity(
        a.get("company_name_raw") or a.get("company_name"),
        b.get("company_name_raw") or b.get("company_name"),
    )
    if not company and a.get("company_id") and a.get("company_id") == b.get("company_id"):
        company = 1.0
    location = location_similarity(a.get("location"), b.get("location"))
    recency = date_proximity(a.get("posted_at"), b.get("posted_at"))
    return 0.45 * title + 0.25 * company + 0.25 * location + 0.05 * recency


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
