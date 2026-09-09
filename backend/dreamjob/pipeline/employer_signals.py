"""Free employer signals: is the employer named on the advert the employer described in it?

Interim, staffing and selection agencies advertise real jobs for employers they
do not name.  Everything this product does well - profiling the company, reading
five years of its accounts, judging its ability to pay, inferring what it will
hire for next, writing a letter about why you want to work *there* - is computed
against the wrong organisation for such a posting, and looks authoritative while
being wrong.  Measured on the corpus, 17-20% of real rows and 33-37% of the
named Belgian EURES rows are agency postings (``docs/Interim_Agencies_Proposal.md``
section 1).

This module holds the **free** half of the detector: everything that can be read
from postings already in the knowledge base, at zero request and zero token cost
(``docs/Agency_Research_Design.md`` section 2, rung 0b).

What these signals are for, and what they are not for
-----------------------------------------------------

They **order the work queue and contribute evidence.  They never deliver the
verdict alone.**  :data:`DELIVERS_VERDICT` is ``False`` and this module never
returns a ``kind``: the employer/agency/cannot_tell verdict is written by a
corroborating rung (register, EURES section-O probe, website read, human) and
only ever with these signals as one input.  That restriction is not caution for
its own sake - it is the reason the GitLab false positive does not happen.  A
naive diversity test calls GitLab an agency (225 postings, 213 distinct titles,
ratio 0.95, 13 title-derived function groups - every diversity measure ties it
with NOEL FRANKLIN BV, which is one).  What separates them is *voice*, and voice
is a strong prior, not proof:

===========================  ==================  ==================
measure                      GitLab              NOEL FRANKLIN BV
===========================  ==================  ==================
shared-boilerplate Jaccard   0.30                0.0008
own name in its adverts      100%                2%
singular client phrase       0 of 225            55 of 56
score                        -5.5 free           +5.5 free, +11.5 with R1/R2
===========================  ==================  ==================

The signals, with the precision and recall the proposal measured
----------------------------------------------------------------

Per employer, aggregated over its postings.  Section references are to
``docs/Interim_Agencies_Proposal.md``.

``T3`` shared-boilerplate Jaccard (P 0.89 / R 0.85 - the best free signal)
    Mean pairwise Jaccard of 6-word shingles over up to 12 descriptions, ``< 0.02``
    with ``n >= 5``, ``+1``.  A real employer repeats its self-description; an
    agency describes a different client every time.  False positives are terse
    public bodies (Scholengroep 8: 0.0185, Smals: 0.0003), which D4 answers.
``T2`` anonymous posting (P 0.93 / R 0.65)
    Share of postings carrying neither a first-person employer voice nor the
    employer's own name ``>= 0.6``, ``n >= 3``, ``+1.5``.
``T1`` singular client phrasing (P 1.00 / R 0.40)
    Share of postings with a hiring-framed *singular* third party ``>= 0.3``,
    ``+3``; ``>= 0.1``, ``+1.5`` (T1').  The discriminator is grammatical
    number: every false positive of a loose pattern was plural or customer usage
    ("für unsere Kunden", "our client base", "advies aan de klant").  Recall is
    genuinely limited - NOEL FRANKLIN 55 of 56, but 100G BV 0 of 26.
``R4``/``R4b`` name lexicon (P 1.00 / R 0.50-0.57, and an ambiguous tier)
    Agency stem or maintained brand, ``+2``; an ambiguous whole word (*hr,
    people, work, career, search, select, employ*), ``+1`` and **never alone**.
``S0`` title, seniority and sector spread (P 0.60 / R 0.15 - the worst signal)
    Measured and reported, **scored at 0.0**.  Section 2.4 rejected it and
    section 5.2.1 refuses it "at any weight": it flags GitLab, the two federal
    ministries, Deliveroo and every multi-site employer, while most agencies are
    specialised (FORUM JOBS industrial, EDITX IT, Taxtalente tax).  It is kept
    visible because a reader comparing employers will compute it anyway, and
    seeing it tie GitLab with NOEL FRANKLIN is the argument.
``D1``-``D5`` negative signals
    Own name in >= 60% of descriptions with no client voice ``-2.5``;
    first-person voice >= 0.6 with no client voice ``-1``; an anti-agency
    disclaimer ``-2``; a public or non-profit legal form ``-3``; posting on its
    own ATS board ``-2``.  These are what keep direct employers out, and they are
    free, so they belong here rather than with the paid rungs.

``R1`` (register), ``R2`` (EURES section O), ``R3`` (temp-to-hire offering code)
and ``D6`` are not free and are not computed here; :meth:`Signals.with_signals`
is the seam through which the registry pass adds them and the band is recomputed.

Re-measured on 43 hand-labelled employers of the corpus
(``tests/fixtures/agency_labels.json``, 20 agencies / 23 direct, name evidence
restricted to the entries that were not read off this corpus): T1 P 1.00 /
R 0.40, T2 P 0.86 / R 0.60, T3 P 0.89 / R 0.85, R4 P 1.00 / R 0.55, and at band
level P 1.00 at every band with recall 0.55 at *probable* and 0.85 at
*possible* - **no direct employer is flagged anywhere**.  The three agencies the
free signals cannot reach are named in ``tests/unit/test_employer_signals.py``
together with the rung that reaches each.

The points table and the band floors are shared with ``pipeline/employer_kind``,
which scores the verdict for the whole ladder; :func:`as_kind_signals` hands this
measurement to it so that one arithmetic stands behind one badge, and a test
scores all 43 employers both ways to keep the two from drifting apart.

How a verdict is made out of this
--------------------------------

:func:`score_postings` returns a *band*, never a kind.  The mapping in proposal
section 4 - certain/probable to ``agency``, possible to ``unverified``,
unknown/direct-likely to ``direct`` - is applied by the verdict layer to the
score **after** a corroborating rung has been merged with
:meth:`Signals.with_signals`; applied to a free-signal score on its own it would
act on text alone, which is the thing this module refuses to do.  Until then the
band is a queue priority and an evidence list.

``cannot_tell`` is a first-class answer at that layer, and this module feeds it
rather than pre-empting it: :attr:`Signals.next_rung` and
:attr:`Signals.next_rung_note` carry the cheapest step that would settle the
employer - the Belgian register in two requests, one EURES section-O search, a
website read, or "no website is known, add one" - so a company the machine
cannot place is shown with its reason and something to do about it
(``Agency_Research_Design.md`` section 7).

Evidence (NFR-402)
------------------

Every signal carries what was counted, and every quote is verbatim: it is cut
from the posting text, re-verified against it by :func:`verify_quote`, capped at
:data:`QUOTE_MAX_WORDS` words, and stored with the vacancy id, the source URL and
the moment it was established.  A job seeker who disagrees can see why, which is
the whole point - the badge that says "agency" is a claim about a real company.

Untrusted input (NFR-205)
-------------------------

Posting text is scraped from job boards and company sites, so it is exactly the
injection vector NFR-205 is about.  Every signal here is a regex or a set
statistic over that text: instructions planted in a description cannot steer the
outcome, because nothing in this module interprets the text.  Planted
instructions are nevertheless *reported* - :attr:`Signals.anomalies` carries the
verbatim attempt for review, the same property the measured website rung has.

Privacy
-------

Nothing here reads or stores a person's name, address or e-mail (RK-08); the
inputs are the employer's name and its own advertisements, and the outputs are
counts, shares and quotes from those advertisements.
"""

from __future__ import annotations

import itertools
import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import employer_signals as repo
from dreamjob.llm.client import find_injection_markers

DATA_DIR = Path(__file__).resolve().parent / "data"
LEXICON_PATH = DATA_DIR / "agency_names.json"

#: This module never answers "agency" or "employer".  Rung 0b sets priority and
#: contributes evidence; the verdict needs a corroborating rung (proposal 2.1).
DELIVERS_VERDICT = False

#: The rung these signals belong to in the resolution ladder.
RUNG = "0b"

METHOD = "free_signals"

# ---------------------------------------------------------------------------
# Thresholds, all of them measured (proposal 2.2)
# ---------------------------------------------------------------------------

SHINGLE_SIZE = 6
#: How many of an employer's descriptions the Jaccard mean is taken over.
DESCRIPTION_LIMIT = 12
JACCARD_THRESHOLD = 0.02
JACCARD_MIN_POSTINGS = 5

CLIENT_SHARE_STRONG = 0.3
CLIENT_SHARE_WEAK = 0.1

ANONYMOUS_SHARE = 0.6
ANONYMOUS_MIN_POSTINGS = 3

SELF_NAMED_SHARE = 0.6
VOICE_SHARE = 0.6

#: An employer with fewer than this many postings cannot be pushed past
#: ``possible`` by text signals alone (proposal 2.2, "Cap").
TEXT_ONLY_CAP_MIN_POSTINGS = 3

QUOTE_MAX_WORDS = 30
_QUOTE_RADIUS = 90

#: Points per signal.  Additive; the bands in :data:`BANDS` sit on the sum.
POINTS = {
    "T1": 3.0,
    "T1'": 1.5,
    "T2": 1.5,
    "T3": 1.0,
    "R4": 2.0,
    "R4b": 1.0,
    "S0": 0.0,
    "D1": -2.5,
    "D2": -1.0,
    "D3": -2.0,
    "D4": -3.0,
    "D5": -2.0,
}

#: Band floors (proposal 2.3).  ``certain`` needs two independent strong
#: signals (3 + 3, or 3 + 2); ``possible`` licenses nothing either way;
#: ``direct_likely`` is ``<= -1.5``, so the boundary itself is on the employer's
#: side - the same floors as ``pipeline/employer_kind``, which scores the
#: verdict, because two tables of thresholds is one badge nobody can reproduce.
CERTAIN_SCORE = 5.0
PROBABLE_SCORE = 3.0
POSSIBLE_SCORE = 1.5
DIRECT_LIKELY_SCORE = -1.5

#: The bands, strongest suspicion first.
BANDS: tuple[str, ...] = ("certain", "probable", "possible", "unknown", "direct_likely")

#: Bands an employer may not be put into by text signals alone on a thin history.
_CAPPED_BANDS = ("certain", "probable")


def band_for(score: float) -> str:
    """The band a score falls in (proposal 2.3).  Not a verdict - see the module docstring."""
    if score >= CERTAIN_SCORE:
        return "certain"
    if score >= PROBABLE_SCORE:
        return "probable"
    if score >= POSSIBLE_SCORE:
        return "possible"
    if score <= DIRECT_LIKELY_SCORE:
        return "direct_likely"
    return "unknown"


# ---------------------------------------------------------------------------
# Text handling
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[0-9a-zà-ÿ]+")
_WS = re.compile(r"\s+")

#: Legal forms stripped before an employer name is looked for in its own advert.
LEGAL_FORMS = frozenset(
    {
        "bv", "bvba", "nv", "sa", "srl", "sprl", "cvba", "cv", "vof", "comm",
        "vzw", "asbl", "gmbh", "mbh", "ag", "kg", "ohg", "ug", "se", "ltd",
        "limited", "plc", "inc", "llc", "corp", "sas", "sarl", "sl", "spa",
        "oy", "ab", "as", "aps", "av", "ev",
    }
)

#: Name tokens too common in advertisement prose to prove that an employer named
#: itself.  Matching on one of these is how "Vindevogel" and "LET'S WORK" get
#: read as self-naming when they are not (Agency_Research_Design 3.2, rule 1).
GENERIC_NAME_TOKENS = frozenset(
    {
        "group", "groupe", "groep", "holding", "international", "national",
        "belgium", "belgie", "belgique", "europe", "european", "benelux",
        "nederland", "netherlands", "deutschland", "france", "brussels",
        "services", "service", "solutions", "solution", "consulting",
        "consultancy", "digital", "technologies", "technology", "company",
        "systems", "industries", "partners", "partner", "team", "teams",
        "academy", "institute", "center", "centre", "global", "new", "first",
        "next", "prime", "top", "plus", "unique", "start", "accent", "vind",
        "jobs", "job", "work", "works", "people", "talent", "career", "careers",
        "search", "select", "employ", "staff", "personnel", "the", "and", "van",
        "de", "het", "les", "der", "und",
    }
)


def fold(text: str | None) -> str:
    """Case-folded, accent-stripped text - the comparison form for every match."""
    stripped = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in stripped if not unicodedata.combining(c)).casefold()


def words(text: str | None) -> list[str]:
    return _WORD.findall(fold(text))


def collapse(text: str | None) -> str:
    """Whitespace-collapsed text, so a quote can be verified across line breaks."""
    return _WS.sub(" ", (text or "")).strip()


def verify_quote(quote: str, source: str | None) -> bool:
    """True when ``quote`` really occurs in ``source`` (NFR-402).

    Advertisement text is full of hard line breaks, so the comparison is made on
    the whitespace-collapsed forms - the same reading a person would do when
    checking the quote against the page.
    """
    if not quote:
        return False
    return collapse(quote).casefold() in collapse(source).casefold()


def shingles(text: str | None, size: int = SHINGLE_SIZE) -> frozenset[str]:
    """The set of ``size``-word shingles of one description."""
    tokens = words(text)
    if not tokens:
        return frozenset()
    if len(tokens) < size:
        return frozenset({" ".join(tokens)})
    return frozenset(" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1))


def mean_pairwise_jaccard(
    texts: Sequence[str | None],
    size: int = SHINGLE_SIZE,
    limit: int = DESCRIPTION_LIMIT,
) -> float | None:
    """Mean pairwise Jaccard of the shingle sets of up to ``limit`` descriptions.

    ``None`` when fewer than two descriptions carry any words.  Measured:
    GitLab 0.3037, Taxtalente 0.9651 (32 near-identical adverts), Trusteq 0.1754,
    Deliveroo 0.1018 against NOEL FRANKLIN 0.0008, FORUM JOBS 0.0005 and
    100G 0.0029.
    """
    sets = [s for s in (shingles(t, size) for t in list(texts)[:limit]) if s]
    if len(sets) < 2:
        return None
    total = 0.0
    pairs = 0
    for left, right in itertools.combinations(sets, 2):
        union = len(left | right)
        total += (len(left & right) / union) if union else 0.0
        pairs += 1
    return total / pairs


# ---------------------------------------------------------------------------
# T1: the singular client phrase
# ---------------------------------------------------------------------------

_I = re.IGNORECASE

#: Hiring-framed references to a *singular* unnamed third party (proposal 2.2 T1).
#: Grammatical number is the discriminator, and these patterns are deliberately
#: narrow: plural and customer usage ("onze klanten", "unsere Kunden", "our
#: client base", "advies aan de klant") is what a consultancy writes, and a
#: consultancy is an employer (proposal 4.7).
CLIENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("nl.onze_klant", re.compile(r"\bonze\s+klant\b", _I)),
    ("nl.in_opdracht_van", re.compile(r"\bin\s+opdracht\s+van\b", _I)),
    ("nl.onze_opdrachtgever", re.compile(r"\b(?:onze|een)\s+opdrachtgever\b", _I)),
    # "Voor een internationale machinebouwer in Roeselare zoeken wij ..." - the
    # capitalised place is what keeps this off "voor een uitdagende job in een
    # dynamisch team".
    (
        "nl.voor_een_bedrijf_in_plaats",
        re.compile(r"\bvoor\s+(?:een|ons|onze)\s+(?:[\w-]+\s+){0,4}(?:in|te)\s+(?:de\s+regio\s+)?[A-ZÀ-Þ][\w-]+"),
    ),
    (
        "fr.notre_client",
        re.compile(r"\bpour\s+notre\s+client\b|\bnotre\s+client\s*(?:,|:|est\b|recherche\b)", _I),
    ),
    ("en.our_client", re.compile(r"\bour\s+client(?:'s\s+team\b|\s*,?\s*(?:is|based)\b)", _I)),
    ("en.on_behalf_of", re.compile(r"\bon\s+behalf\s+of\s+(?:a|an|our)\b", _I)),
    (
        "de.unser_mandant",
        re.compile(r"\bunser(?:en|em|es)?\s+(?:Mandant|Kunde|Auftraggeber)(?:en|n)?\s*(?:ist\b|,|:)", _I),
    ),
    (
        "de.fuer_ein_unternehmen",
        re.compile(
            r"\bf[üu]r\s+ein\s+(?:etabliertes|renommiertes|innovatives|international(?:es)?|"
            r"mittelst[äa]ndisches|f[üu]hrendes)\s+\w*[Uu]nternehmen\b",
            _I,
        ),
    ),
    (
        "de.vermittlung",
        re.compile(r"\b(?:Direktvermittlung|Arbeitnehmer[üu]berlassung|Personalvermittlung)\b", _I),
    ),
    ("de.vermittelt_an", re.compile(r"\bvermittelt\s+an\b", _I)),
)

#: Phrases a loose reading of T1 fired on and which are *not* client phrases.
#: Kept as data so the exclusion is testable rather than implicit (proposal 2.2 T1).
CLIENT_PATTERN_EXCLUSIONS = (
    "onze klanten", "de klant", "advies aan de klant", "unsere Kunden",
    "our client base", "our clients", "je komt terecht in een", "with a Business",
)

# ---------------------------------------------------------------------------
# T2 / D2: first-person employer voice
# ---------------------------------------------------------------------------

#: An employer introducing itself.  Two families: explicit self-description
#: ("over ons", "über uns") and first-person statements about the organisation's
#: own team, product or customers.  The plural customer forms are here on
#: purpose: "für unsere Kunden" is a consultancy describing where its own staff
#: sit, and that is the sentence that keeps Trusteq an employer (proposal 4.7).
VOICE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "self_description",
        re.compile(
            r"\bover\s+ons\b|\bwie\s+zijn\s+wij\b|\bons\s+bedrijf\b|\bonze\s+organisatie\b"
            r"|\babout\s+us\b|\bwho\s+we\s+are\b|\bour\s+(?:company|story|mission|history)\b"
            r"|\bqui\s+sommes[-\s]nous\b|\bnotre\s+(?:entreprise|société|histoire|mission)\b"
            r"|\b[àa]\s+propos\s+de\s+nous\b"
            r"|\b[üu]ber\s+uns\b|\bunser\s+Unternehmen\b|\bwir\s+[üu]ber\s+uns\b",
            _I,
        ),
    ),
    (
        "first_person_copula",
        re.compile(
            r"\bwij\s+zijn\b|\bwe\s+zijn\b|\bwe\s+are\b|\bwe're\b|\bwe\s+have\s+been\b"
            r"|\bnous\s+sommes\b|\bwir\s+sind\b",
            _I,
        ),
    ),
    (
        # "our team", "onze afdeling bouw", "unsere Kunden", "notre équipe": a
        # first-person possessive about the organisation's own things.  The only
        # exclusions are the *singular* client forms, which are T1's business -
        # so "unsere Kunden" and "our clients" count as employer voice on
        # purpose, and that is what keeps a consultancy an employer (proposal 4.7).
        "first_person_possessive",
        re.compile(
            r"\bour\s+(?!client\b)\w+"
            r"|\bwe\s+(?:build|make|develop|design|deliver|produce|manufacture|create|operate)\b"
            r"|\bons\s+\w+|\bonze\s+(?!klant\b)(?!opdrachtgever\b)\w+|\bbij\s+ons\b"
            r"|\bunser(?:e|en|em|es)?\s+(?!Mandant|Kunde\b|Auftraggeber)\w+|\bbei\s+uns\b"
            r"|\bnotre\s+(?!client\b)\w+|\bnos\s+\w+|\bchez\s+nous\b",
            _I,
        ),
    ),
)

#: "We are looking for" is a hiring sentence, not a self-description: an agency
#: writes it about somebody else's vacancy.  It never counts as employer voice.
HIRING_VOICE = re.compile(
    r"\bwij\s+zijn\s+op\s+zoek\b|\bwe\s+zijn\s+op\s+zoek\b"
    r"|\bwe\s+are\s+(?:looking|seeking|hiring|searching|recruiting|currently)\b"
    r"|\bwir\s+sind\s+auf\s+der\s+Suche\b|\bnous\s+sommes\s+[àa]\s+la\s+recherche\b",
    _I,
)

#: D3: the advert itself says no agencies (proposal 2.2 D3).
NO_AGENCY_PATTERNS = re.compile(
    r"\bno\s+agenc(?:y|ies)\b|\bagencies?\s+(?:need\s+not|please\s+do\s+not)\b"
    r"|\bno\s+recruiters?\b|\bgeen\s+(?:interim|uitzend|rekruterings?)?\s?(?:bureau|kantoren|bureaus)\b"
    r"|\bkeine\s+(?:Personalvermittler|Personalberater|Zeitarbeit)\b"
    r"|\bpas\s+d[e']\s?agences?\b",
    _I,
)


def client_phrase_hits(text: str | None) -> list[tuple[str, re.Match[str]]]:
    """Every strict T1 pattern that fires in one description, with its match."""
    if not text:
        return []
    return [(name, m) for name, pat in CLIENT_PATTERNS if (m := pat.search(text))]


def employer_voice_hit(text: str | None) -> tuple[str, re.Match[str]] | None:
    """The first first-person employer voice in one description, if any."""
    if not text:
        return None
    for name, pat in VOICE_PATTERNS:
        for match in pat.finditer(text):
            if name == "first_person_copula" and HIRING_VOICE.search(
                text[max(0, match.start() - 5) : match.end() + 40]
            ):
                continue
            return name, match
    return None


def name_in_text(name: str | None, text: str | None) -> re.Match[str] | None:
    """Where the employer named itself in its own advert, if it did.

    The whole normalised name counts; so does any *distinctive* token of it - a
    Belgian employer writes "Korian" far more often than "Korian Belgium".
    Tokens that are common in advertisement prose (:data:`GENERIC_NAME_TOKENS`)
    do not count on their own, which is what stops "LET'S WORK" and "Vind" from
    being read as self-naming every time an advert uses the verb.
    """
    tokens = [t for t in words(name) if t not in LEGAL_FORMS]
    if not tokens or not text:
        return None
    folded = fold(text)
    phrase = " ".join(tokens)
    if len(tokens) > 1 and (match := re.search(re.escape(phrase), folded)):
        return match
    distinctive = [t for t in tokens if t not in GENERIC_NAME_TOKENS and len(t) >= 3]
    # A multi-word name left with only generic tokens ("The Rec Hub", "LET'S
    # WORK") has already had its one honest test above: the whole phrase.  A
    # one-word generic name ("Unique", "Vind") is matched on the word anyway -
    # that reads the advert as self-naming when it may not be, which errs
    # towards calling an employer direct, and hiding a real employer is the
    # expensive mistake (proposal 5.2.1).
    for token in distinctive or ([] if len(tokens) > 1 else tokens):
        if match := re.search(r"\b" + re.escape(token) + r"\b", folded):
            return match
    return None


# ---------------------------------------------------------------------------
# The name lexicon (R4 / R4b / D4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Lexicon:
    """Agency name evidence, loaded from ``pipeline/data/agency_names.json`` (C4)."""

    stems: tuple[tuple[str, str], ...] = ()
    ambiguous_words: tuple[tuple[str, str], ...] = ()
    brands: tuple[tuple[str, str], ...] = ()
    public_legal_forms: tuple[tuple[str, str], ...] = ()
    version: str = ""


@lru_cache(maxsize=1)
def load_lexicon() -> Lexicon:
    data = json.loads(LEXICON_PATH.read_text(encoding="utf-8"))
    return Lexicon(
        stems=tuple((fold(e["stem"]), e.get("source", "")) for e in data.get("stems", [])),
        ambiguous_words=tuple(
            (fold(e["word"]), e.get("source", "")) for e in data.get("ambiguous_words", [])
        ),
        brands=tuple((fold(e["name"]), e.get("source", "")) for e in data.get("brands", [])),
        public_legal_forms=tuple(
            (fold(e["pattern"]), e.get("source", "")) for e in data.get("public_legal_forms", [])
        ),
        version=str(data.get("version", "")),
    )


#: An empty lexicon, for measuring the text signals on the corpus the brand list
#: was itself derived from (see the note in ``agency_names.json``).
EMPTY_LEXICON = Lexicon()


def lexicon_hits(name: str | None, lexicon: Lexicon | None = None) -> dict[str, list[str]]:
    """What the employer's name says about it: brands, stems, ambiguous words, legal forms."""
    lex = load_lexicon() if lexicon is None else lexicon
    folded = fold(name)
    tokens = set(words(name))
    hits: dict[str, list[str]] = {"brand": [], "stem": [], "ambiguous": [], "public_form": []}
    if not folded:
        return hits
    for brand, _source in lex.brands:
        if brand and re.search(r"(?<![a-z0-9])" + re.escape(brand) + r"(?![a-z0-9])", folded):
            hits["brand"].append(brand)
    for stem, _source in lex.stems:
        if stem and stem in folded:
            hits["stem"].append(stem)
    for word, _source in lex.ambiguous_words:
        if word in tokens:
            hits["ambiguous"].append(word)
    for pattern, _source in lex.public_legal_forms:
        if pattern in tokens or (" " in pattern and pattern in folded):
            hits["public_form"].append(pattern)
    return hits


# ---------------------------------------------------------------------------
# Inputs and results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Posting:
    """One advertisement, as much of it as the signals read."""

    id: str | None = None
    title: str = ""
    description: str | None = None
    language: str | None = None
    seniority: str | None = None
    function_family: str | None = None
    contract_type: str | None = None
    source_adapter: str | None = None
    source_url: str | None = None
    location: str | None = None
    posted_at: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Posting:
        return cls(
            id=row.get("id"),
            title=row.get("title") or "",
            description=row.get("description"),
            language=row.get("language"),
            seniority=row.get("seniority"),
            function_family=row.get("function_family"),
            contract_type=row.get("contract_type"),
            source_adapter=row.get("source_adapter"),
            source_url=row.get("source_url"),
            location=row.get("location"),
            posted_at=row.get("posted_at"),
        )


@dataclass(frozen=True)
class Evidence:
    """One verbatim quote, verified against the posting it was cut from (NFR-402)."""

    quote: str
    vacancy_id: str | None = None
    url: str | None = None
    matched: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "quote": self.quote,
            "vacancy_id": self.vacancy_id,
            "url": self.url,
            "matched": self.matched,
        }


@dataclass(frozen=True)
class Signal:
    """One signal: what was counted, what it is worth, and the quotes behind it."""

    id: str
    label: str
    fired: bool
    points: float
    value: float | None = None
    detail: str = ""
    precision: float | None = None
    recall: float | None = None
    evidence: tuple[Evidence, ...] = ()
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "signal": self.id,
            "label": self.label,
            "fired": self.fired,
            "points": self.points,
            "value": self.value,
            "detail": self.detail,
            "precision": self.precision,
            "recall": self.recall,
            "quotes": [e.as_dict() for e in self.evidence],
            "source": self.source,
        }


@dataclass(frozen=True)
class Signals:
    """Every free signal for one employer, with the provisional band they add up to.

    ``band`` is a band, not a verdict.  ``kind`` (agency / employer /
    cannot_tell) is written by a corroborating rung; see :data:`DELIVERS_VERDICT`.
    """

    employer_name: str
    company_id: str | None = None
    n_postings: int = 0
    n_descriptions: int = 0
    signals: tuple[Signal, ...] = ()
    score: float = 0.0
    band: str = "unknown"
    capped: bool = False
    notes: tuple[str, ...] = ()
    anomalies: tuple[dict[str, Any], ...] = ()
    next_rung: str = "registry"
    next_rung_note: str = ""
    established_at: str = ""
    method: str = METHOD
    rung: str = RUNG

    @property
    def fired_signals(self) -> tuple[Signal, ...]:
        """The signals that actually counted - what the badge's evidence list shows."""
        return tuple(s for s in self.signals if s.fired)

    @property
    def sort_key(self) -> tuple[float, int]:
        """Work-queue order: strongest suspicion first, then the widest blast radius."""
        return (-self.score, -self.n_postings)

    def signal(self, signal_id: str) -> Signal | None:
        for s in self.signals:
            if s.id == signal_id:
                return s
        return None

    def with_signals(self, extra: Iterable[Signal]) -> Signals:
        """Add the rungs this module cannot see (R1, R2, R3, D6) and rescore.

        This is the only supported way to turn free signals into something
        actionable: the registry pass computes its own signals, hands them here,
        and the band is recomputed over the union.  The text-only cap is lifted
        once a non-free signal has fired, exactly as proposal 2.2 states
        ("R-signals are not capped").
        """
        merged = list(self.signals)
        by_id = {s.id: i for i, s in enumerate(merged)}
        for signal in extra:
            if signal.id in by_id:
                merged[by_id[signal.id]] = signal
            else:
                by_id[signal.id] = len(merged)
                merged.append(signal)
        return _finalise(replace(self, signals=tuple(merged)))

    def evidence_record(self) -> list[dict[str, Any]]:
        """The JSON evidence list stored on the verdict row (NFR-402, N1)."""
        return [
            {**s.as_dict(), "rung": self.rung, "method": self.method,
             "established_at": self.established_at}
            for s in self.signals
            if s.fired
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "employer_name": self.employer_name,
            "n_postings": self.n_postings,
            "n_descriptions": self.n_descriptions,
            "score": self.score,
            "band": self.band,
            "capped": self.capped,
            "delivers_verdict": DELIVERS_VERDICT,
            "next_rung": self.next_rung,
            "next_rung_note": self.next_rung_note,
            "notes": list(self.notes),
            "anomalies": [dict(a) for a in self.anomalies],
            "evidence": self.evidence_record(),
            "established_at": self.established_at,
            "rung": self.rung,
            "method": self.method,
        }


# ---------------------------------------------------------------------------
# Quote cutting
# ---------------------------------------------------------------------------


def _quote(text: str, match: re.Match[str], posting: Posting, matched: str) -> Evidence | None:
    """A verbatim window around a match, capped and re-verified (NFR-402)."""
    start = max(0, match.start() - _QUOTE_RADIUS)
    end = min(len(text), match.end() + _QUOTE_RADIUS)
    fragment = collapse(text[start:end])
    parts = fragment.split(" ")
    if len(parts) > QUOTE_MAX_WORDS:
        # Keep the window centred on the match rather than its left edge.
        centre = len(collapse(text[start : match.start()]).split(" "))
        first = max(0, centre - QUOTE_MAX_WORDS // 2)
        parts = parts[first : first + QUOTE_MAX_WORDS]
        fragment = " ".join(parts)
    if not verify_quote(fragment, text):  # pragma: no cover - defensive
        return None
    return Evidence(
        quote=fragment,
        vacancy_id=posting.id,
        url=posting.source_url,
        matched=matched,
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _share(count: int, total: int) -> float:
    return (count / total) if total else 0.0


def _finalise(signals: Signals) -> Signals:
    """Sum the points, apply the thin-history cap and set the band."""
    score = round(sum(s.points for s in signals.signals if s.fired), 3)
    band = band_for(score)
    capped = False
    notes = list(signals.notes)
    # R4b is the one R signal that cannot lift the cap: an ambiguous word never
    # counts alone, so it can never be the evidence that makes a thin history safe.
    strong_ids = {
        s.id
        for s in signals.fired_signals
        if s.id.startswith("R") and s.id != "R4b" and s.points > 0
    }
    if (
        band in _CAPPED_BANDS
        and not strong_ids
        and signals.n_postings < TEXT_ONLY_CAP_MIN_POSTINGS
    ):
        band = "possible"
        capped = True
        note = (
            f"capped at possible: {signals.n_postings} posting(s) and no register or "
            "name evidence - text signals alone cannot carry a thin history (proposal 2.2)"
        )
        if note not in notes:
            notes.append(note)
    return replace(signals, score=score, band=band, capped=capped, notes=tuple(notes))


def _next_rung(
    country: str | None,
    domain: str | None,
    postings: Sequence[Posting],
    *,
    registry_resolved: bool = False,
) -> tuple[str, str]:
    """The cheapest rung that could turn these signals into a verdict.

    ``cannot_tell`` is shown with its reason and the cheapest next rung
    (Agency_Research_Design 7.3); this is that rung, chosen by cost: the Belgian
    register answers in two requests, the EURES section-O probe in one POST, a
    website read needs a confirmed domain.  A register that has already resolved
    this company is not offered again - it has said what it has to say, and the
    silence of a register is not negative evidence (proposal 2.2 D6).
    """
    market = (country or "").upper()
    if not registry_resolved and market in {"BE", "GB", "UK", "NL"}:
        return "registry", (
            f"the {market} register answers in two requests: a 78.x activity code is definitive"
        )
    if any((p.source_adapter or "").startswith("board.eures") for p in postings):
        return "eures_sector_o", (
            "one EURES EMPLOYER search with sectorCodes=['O'] says whether the board files "
            "this employer under staffing"
        )
    if domain:
        return "website", "read the employer-facing pages of the site and quote what they sell"
    return "no_domain", (
        "no register and no website is known for this employer: the cheapest next step is "
        "adding its website"
    )


def score_postings(
    employer_name: str,
    postings: Sequence[Posting],
    *,
    company_id: str | None = None,
    country: str | None = None,
    domain: str | None = None,
    registry_resolved: bool = False,
    lexicon: Lexicon | None = None,
) -> Signals:
    """Compute every free signal for one employer.  Pure: no database, no network.

    ``postings`` is the employer's own advertisements, newest first; only the
    first :data:`DESCRIPTION_LIMIT` descriptions are compared for T3, but every
    posting counts towards the shares.
    """
    postings = list(postings)
    described = [p for p in postings if (p.description or "").strip()]
    n = len(described)
    lex = load_lexicon() if lexicon is None else lexicon
    established_at = utcnow()

    anomalies: list[dict[str, Any]] = []
    for posting in described:
        for marker in find_injection_markers(posting.description or ""):
            anomalies.append(
                {
                    "kind": "instruction_in_posting",
                    "quote": collapse(marker)[:200],
                    "vacancy_id": posting.id,
                    "url": posting.source_url,
                    "note": (
                        "instruction-shaped text in an advertisement; it changed no signal "
                        "(NFR-205) and is reported for review"
                    ),
                }
            )

    signals: list[Signal] = []

    # --- T1 / T1': the singular client phrase -----------------------------
    client_hits: list[Evidence] = []
    client_count = 0
    for posting in described:
        hits = client_phrase_hits(posting.description)
        if not hits:
            continue
        client_count += 1
        if len(client_hits) < 3:
            name, match = hits[0]
            if ev := _quote(posting.description or "", match, posting, name):
                client_hits.append(ev)
    client_share = _share(client_count, n)
    strong_client = client_share >= CLIENT_SHARE_STRONG
    weak_client = CLIENT_SHARE_WEAK <= client_share < CLIENT_SHARE_STRONG
    signals.append(
        Signal(
            id="T1",
            label="Speaks of a single unnamed client",
            fired=bool(strong_client or weak_client),
            points=POINTS["T1"] if strong_client else (POINTS["T1'"] if weak_client else 0.0),
            value=round(client_share, 4),
            detail=(
                f"{client_count} of {n} advertisements introduce a single unnamed third party "
                f"(“onze klant”, “notre client”, “unser Mandant”)"
                + (f" - share {client_share:.2f} ≥ {CLIENT_SHARE_STRONG}" if strong_client
                   else (f" - share {client_share:.2f} ≥ {CLIENT_SHARE_WEAK} (T1', half points)"
                         if weak_client else ""))
            ),
            precision=1.00,
            recall=0.40,
            evidence=tuple(client_hits),
            source="Interim_Agencies_Proposal.md 2.2 T1",
        )
    )

    # --- T2: nobody introduces themselves ---------------------------------
    anonymous = 0
    named = 0
    voiced = 0
    anonymous_evidence: list[Evidence] = []
    named_evidence: list[Evidence] = []
    voice_evidence: list[Evidence] = []
    for posting in described:
        text = posting.description or ""
        name_hit = name_in_text(employer_name, text)
        voice_hit = employer_voice_hit(text)
        if name_hit is not None:
            named += 1
            if len(named_evidence) < 2 and (ev := _quote(text, name_hit, posting, "employer_name")):
                named_evidence.append(ev)
        if voice_hit is not None:
            voiced += 1
            if len(voice_evidence) < 2 and (
                ev := _quote(text, voice_hit[1], posting, voice_hit[0])
            ):
                voice_evidence.append(ev)
        if name_hit is None and voice_hit is None:
            anonymous += 1
            if len(anonymous_evidence) < 2:
                head = re.match(r"\s*\S[\s\S]{0,200}", text)
                if head and (ev := _quote(text, head, posting, "anonymous_opening")):
                    anonymous_evidence.append(ev)
    anonymous_share = _share(anonymous, n)
    named_share = _share(named, n)
    voice_share = _share(voiced, n)
    t2_fires = anonymous_share >= ANONYMOUS_SHARE and n >= ANONYMOUS_MIN_POSTINGS
    signals.append(
        Signal(
            id="T2",
            label="Advertisements name no employer and speak in nobody's voice",
            fired=t2_fires,
            points=POINTS["T2"] if t2_fires else 0.0,
            value=round(anonymous_share, 4),
            detail=(
                f"{anonymous} of {n} advertisements carry neither the employer's own name nor a "
                f"first-person employer voice (name in {named}, voice in {voiced})"
            ),
            precision=0.93,
            recall=0.65,
            evidence=tuple(anonymous_evidence),
            source="Interim_Agencies_Proposal.md 2.2 T2",
        )
    )

    # --- T3: no shared self-description -----------------------------------
    jaccard = mean_pairwise_jaccard([p.description for p in described])
    t3_fires = (
        jaccard is not None and jaccard < JACCARD_THRESHOLD and n >= JACCARD_MIN_POSTINGS
    )
    compared = min(n, DESCRIPTION_LIMIT)
    signals.append(
        Signal(
            id="T3",
            label="No self-description is repeated between advertisements",
            fired=t3_fires,
            points=POINTS["T3"] if t3_fires else 0.0,
            value=None if jaccard is None else round(jaccard, 4),
            detail=(
                "mean pairwise Jaccard of 6-word shingles over "
                f"{compared} advertisements is "
                + ("not computable (fewer than two descriptions)" if jaccard is None
                   else f"{jaccard:.4f} (threshold {JACCARD_THRESHOLD})")
            ),
            precision=0.89,
            recall=0.85,
            source="Interim_Agencies_Proposal.md 2.2 T3",
        )
    )

    # --- R4 / R4b: what the name says -------------------------------------
    hits = lexicon_hits(employer_name, lex)
    name_hits = hits["brand"] + hits["stem"]
    signals.append(
        Signal(
            id="R4",
            label="The name says what the business is",
            fired=bool(name_hits),
            points=POINTS["R4"] if name_hits else 0.0,
            value=float(len(name_hits)),
            detail=(
                "name matches " + ", ".join(f"“{h}”" for h in name_hits)
                if name_hits
                else "no agency stem or known brand in the name"
            ),
            precision=1.00,
            recall=0.55,
            evidence=(
                (Evidence(quote=employer_name, matched=name_hits[0]),) if name_hits else ()
            ),
            source=f"Interim_Agencies_Proposal.md 2.2 R4; lexicon {lex.version or 'none'}",
        )
    )
    ambiguous = hits["ambiguous"]

    # --- S0: the spread family, measured and deliberately worth nothing ----
    titles = [p.title for p in postings if p.title]
    distinct_titles = len({fold(t) for t in titles})
    seniorities = len({p.seniority for p in postings if p.seniority})
    families = len({p.function_family for p in postings if p.function_family})
    title_ratio = _share(distinct_titles, len(titles))
    signals.append(
        Signal(
            id="S0",
            label="Spread of titles, seniority and function families",
            fired=False,
            points=POINTS["S0"],
            value=round(title_ratio, 4),
            detail=(
                f"{distinct_titles} distinct titles in {len(titles)} postings (ratio "
                f"{title_ratio:.2f}), {seniorities} seniority level(s), {families} function "
                "family/families - reported, never scored: it ties GitLab (0.95) with an agency "
                "and flags every large employer (P 0.60 / R 0.15)"
            ),
            precision=0.60,
            recall=0.15,
            source="Interim_Agencies_Proposal.md 2.4 and 5.2.1",
        )
    )

    # --- D1..D5: what says this is a direct employer ----------------------
    # "No client voice" is T1 not firing, not "not one advert said it".  A single
    # strict hit in 225 postings must not cancel D1 and D2: GitLab has 28 loose
    # hits in 225 adverts, and it is the employer-level majority that keeps it
    # out of the agency bands (proposal 5.2.9).
    no_client_voice = client_share < CLIENT_SHARE_WEAK
    no_agency_name = not name_hits
    d1 = named_share >= SELF_NAMED_SHARE and no_client_voice and no_agency_name
    signals.append(
        Signal(
            id="D1",
            label="Introduces itself by name, with no client voice",
            fired=d1,
            points=POINTS["D1"] if d1 else 0.0,
            value=round(named_share, 4),
            detail=f"the employer's own name appears in {named} of {n} advertisements",
            evidence=tuple(named_evidence) if d1 else (),
            source="Interim_Agencies_Proposal.md 2.2 D1",
        )
    )
    d2 = voice_share >= VOICE_SHARE and no_client_voice
    signals.append(
        Signal(
            id="D2",
            label="Speaks in the first person about itself",
            fired=d2,
            points=POINTS["D2"] if d2 else 0.0,
            value=round(voice_share, 4),
            detail=f"a first-person employer voice in {voiced} of {n} advertisements",
            evidence=tuple(voice_evidence) if d2 else (),
            source="Interim_Agencies_Proposal.md 2.2 D2",
        )
    )
    disclaimer: Evidence | None = None
    for posting in described:
        if match := NO_AGENCY_PATTERNS.search(posting.description or ""):
            disclaimer = _quote(posting.description or "", match, posting, "no_agencies")
            break
    signals.append(
        Signal(
            id="D3",
            label="The advertisement itself says no agencies",
            fired=disclaimer is not None,
            points=POINTS["D3"] if disclaimer is not None else 0.0,
            detail=(
                "an advertisement asks agencies not to apply"
                if disclaimer
                else "no anti-agency disclaimer"
            ),
            evidence=(disclaimer,) if disclaimer else (),
            source="Interim_Agencies_Proposal.md 2.2 D3",
        )
    )
    public_forms = hits["public_form"]
    signals.append(
        Signal(
            id="D4",
            label="A public or non-profit legal form",
            fired=bool(public_forms),
            points=POINTS["D4"] if public_forms else 0.0,
            detail=(
                "the name carries " + ", ".join(f"“{h}”" for h in public_forms)
                if public_forms
                else "no public or non-profit legal form in the name"
            ),
            evidence=(
                (Evidence(quote=employer_name, matched=public_forms[0]),) if public_forms else ()
            ),
            source="Interim_Agencies_Proposal.md 2.2 D4",
        )
    )
    own_board = sorted(
        {p.source_adapter for p in postings if (p.source_adapter or "").startswith("ats.")}
    )
    signals.append(
        Signal(
            id="D5",
            label="Posts on its own applicant-tracking board",
            fired=bool(own_board),
            points=POINTS["D5"] if own_board else 0.0,
            detail=(
                "collected from " + ", ".join(own_board)
                if own_board
                else "no posting on an own ATS board"
            ),
            source="Interim_Agencies_Proposal.md 2.2 D5",
        )
    )

    # R4b is decided last, because "never alone" is a statement about the rest
    # of the evidence.  It also never counts beside R4: one name is one piece of
    # evidence, and "JOB TALENT" would otherwise be paid twice for the same
    # word (proposal 2.2 R4b).  "Another signal" is read here as another signal
    # *for* an agency: Swiss Life Select, SelectLine and the Digital Career
    # Institute are the corpus's false positives for this word list, and an
    # employer whose only other evidence says "direct" must not collect an
    # agency point for having "select" in its name.
    counts = bool(ambiguous) and not name_hits and any(
        s.fired and s.points > 0 for s in signals
    )
    signals.append(
        Signal(
            id="R4b",
            label="An ambiguous word in the name",
            fired=counts,
            points=POINTS["R4b"] if counts else 0.0,
            value=float(len(ambiguous)),
            detail=(
                "name contains " + ", ".join(f"“{h}”" for h in ambiguous)
                + (
                    ""
                    if counts
                    else " - it counts only alongside another signal and never beside a "
                    "staffing term in the same name, so it scores 0"
                )
                if ambiguous
                else "no ambiguous word in the name"
            ),
            evidence=(
                (Evidence(quote=employer_name, matched=ambiguous[0]),) if ambiguous else ()
            ),
            source="Interim_Agencies_Proposal.md 2.2 R4b",
        )
    )

    rung, rung_note = _next_rung(
        country, domain, postings, registry_resolved=registry_resolved
    )
    notes: list[str] = []
    if n < JACCARD_MIN_POSTINGS:
        notes.append(
            f"{n} description(s): T3 needs {JACCARD_MIN_POSTINGS} and T2 needs "
            f"{ANONYMOUS_MIN_POSTINGS}"
        )
    if anomalies:
        notes.append(
            f"{len(anomalies)} advertisement(s) contain instruction-shaped text; no signal "
            "was changed by it (NFR-205)"
        )
    return _finalise(
        Signals(
            employer_name=employer_name,
            company_id=company_id,
            n_postings=len(postings),
            n_descriptions=n,
            signals=tuple(signals),
            notes=tuple(notes),
            anomalies=tuple(anomalies),
            next_rung=rung,
            next_rung_note=rung_note,
            established_at=established_at,
        )
    )


# ---------------------------------------------------------------------------
# The knowledge-base entry points
# ---------------------------------------------------------------------------


def score_employer(company_id: str, *, lexicon: Lexicon | None = None) -> Signals:
    """Score one company from the knowledge base (FR-341: shared, not campaign state)."""
    company = repo.employer(company_id) or {}
    postings = [Posting.from_row(r) for r in repo.employer_postings(company_id)]
    return score_postings(
        company.get("name") or "",
        postings,
        company_id=company_id,
        country=company.get("country"),
        domain=company.get("domain"),
        registry_resolved=bool(company.get("legal_id")),
        lexicon=lexicon,
    )


def _matched(signal: Signal | None) -> str | None:
    """The lexicon entry a name signal matched on, if it matched one."""
    if signal is None or not signal.evidence:
        return None
    return signal.evidence[0].matched or None


def as_kind_signals(signals: Signals) -> Any | None:
    """These measurements in ``pipeline/employer_kind``'s aggregate vocabulary.

    That module holds the same per-employer aggregate seen from the verdict's
    side, and its :func:`~dreamjob.pipeline.employer_kind.score_signals` applies
    the same points table as :func:`score_postings`.  Handing the measurement
    across rather than measuring twice is what keeps one set of numbers behind
    one badge - and the test that scores both ways over the labelled set is
    what would catch the two tables drifting apart, which is a badge that says
    "agency" for a reason nobody can reproduce.

    ``None`` when the verdict vocabulary is not part of this build; the free
    signals stand on their own without it.
    """
    try:
        from dreamjob.pipeline import employer_kind  # noqa: PLC0415 - optional seam
    except ImportError:  # pragma: no cover - both ship in the same release
        return None

    def value(signal_id: str) -> float | None:
        signal = signals.signal(signal_id)
        return None if signal is None else signal.value

    def fired(signal_id: str) -> bool:
        signal = signals.signal(signal_id)
        return bool(signal and signal.fired)

    quotes = {
        signal.id: employer_kind.Quote(
            text=signal.evidence[0].quote,
            url=signal.evidence[0].url,
            source="vacancy",
        )
        for signal in signals.signals
        if signal.evidence
    }
    return employer_kind.Signals(
        postings=signals.n_descriptions,
        name_stem=_matched(signals.signal("R4")),
        name_ambiguous_word=_matched(signals.signal("R4b")),
        client_phrase_share=value("T1"),
        anonymous_share=value("T2"),
        shingle_jaccard=value("T3"),
        self_named_share=value("D1"),
        first_person_share=value("D2"),
        anti_agency_disclaimer=fired("D3"),
        public_legal_form=_matched(signals.signal("D4")),
        own_ats_board=fired("D5"),
        quotes=quotes,
    )


#: Band -> the hint vocabulary the resolver's rung-0b snapshot speaks
#: (``employer_resolver._hint_for``).  It is deliberately not the verdict
#: vocabulary: "suspected_agency" cannot be mistaken for "agency".
HINTS = {
    "certain": "suspected_agency",
    "probable": "suspected_agency",
    "direct_likely": "suspected_direct",
}


def signal_snapshot(company: Any) -> dict[str, Any]:
    """Rung 0b for the employer-kind ladder (``pipeline/employer_resolver.py``).

    Takes the ladder's ``RungContext`` (anything with ``company_id``, ``name``,
    ``domain`` and ``vacancy_count``) and answers in the snapshot shape the
    ladder stores: ``vacancy_count``, ``hint``, ``tier``, ``score`` and the
    ``signals`` evidence list.

    It answers with a **dict and never a verdict object** on purpose.  The
    ladder accepts either, and a rung that returned a verdict would have its
    verdict demoted to evidence a few lines later; making that impossible at
    the source is better than relying on the demotion, because the demotion is
    a branch somebody can delete and this is a shape that cannot carry a kind
    at all (proposal 2.1, research note section 2 rung 0b).

    Instruction-shaped text found in the advertisements travels with the
    evidence as ``{"type": "anomaly"}`` so the attempt reaches the review queue
    rather than stopping at this module (NFR-205).
    """
    company_id = str(getattr(company, "company_id", "") or "")
    name = str(getattr(company, "name", "") or "")
    if company_id:
        signals = score_employer(company_id)
    else:
        signals = score_postings(
            name,
            (),
            domain=str(getattr(company, "domain", "") or "") or None,
        )
    evidence: list[dict[str, Any]] = signals.evidence_record()
    evidence += [{"type": "anomaly", **anomaly} for anomaly in signals.anomalies]
    return {
        "vacancy_count": signals.n_postings or int(getattr(company, "vacancy_count", 0) or 0),
        "hint": HINTS.get(signals.band, "none"),
        "tier": signals.band,
        "score": signals.score,
        "signals": evidence,
    }


def work_queue(
    *, min_postings: int = 1, limit: int = 200, lexicon: Lexicon | None = None
) -> list[Signals]:
    """Employers ordered by how much a verdict on them is worth.

    Suspicion first, then blast radius: an agency with 56 postings costs the job
    seeker 56 wrong company profiles, one with three costs three.  This is the
    only thing the free signals decide on their own.
    """
    scored = [
        score_employer(row["id"], lexicon=lexicon)
        for row in repo.employers_with_postings(min_postings=min_postings, limit=limit)
    ]
    return sorted(scored, key=lambda s: s.sort_key)
