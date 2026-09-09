"""Compensation estimation and employer-review signals (FR-264, FR-265).

FR-264 asks for an estimated range with a confidence and a source list.  The
sources are ranked by how defensible they are, and that ranking is the whole
design:

1. **What the employer stated.**  A posted range is not an estimate at all, and
   it is used as-is with the estimate marked ``is_stated``.
2. **Posted ranges on comparable vacancies.**  The campaign has already
   collected a vacancy corpus; those are real offers, dated, attributable to a
   URL, and in the right market.  Nothing else the system can reach is as
   defensible, so this source carries the most weight and is tried at several
   widths - the same function family at the same company, then the same family
   and seniority in-country, the same family in-country, and title terms
   in-country - stopping at the first ones that have enough observations.  Every
   width matches on the role, never on the employer alone.
3. **National salary surveys and Glassdoor/Levels observations**, as far as an
   adapter or the browser slice has collected them into
   ``compensation_observation``.  They are market-wide rather than
   company-specific, so they anchor rather than decide.

   Glassdoor is a second case of the same rank.  What its browser run collects
   is opinion gathered under restrictive terms, so - by the deliberate choice
   documented in :mod:`dreamjob.browser.glassdoor` - it is *not* promoted into
   the shared ``compensation_observation`` base.  It is read back per campaign
   instead, through :func:`campaign_advisory`, and enters an estimate as an
   advisory source with its own low weight.  A Glassdoor page is only ever
   matched to an opportunity on company *and* role: what an employer pays its
   warehouse staff says nothing about what it would pay a data director.
4. **A coarse built-in band prior**, used only when nothing above exists.  It is
   labelled as a prior, not as survey data, and capped at low confidence: it
   says "a director in this family in this country is not paid like a junior",
   and nothing more precise than that (CR-405).

FR-265 adds employer-review signals - rating and review themes.  They are
**advisory input only**: they never move the compensation range and they enter
scoring as a small, separately visible component, because a review average is a
self-selected sample, not a measurement.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any

from dreamjob.db.repositories import opportunities as repo
from dreamjob.pipeline import directives as dir_mod

log = logging.getLogger(__name__)

#: Minimum comparable postings before a corpus width is considered usable.
MIN_CORPUS_SIZE = 3

#: Weight per source kind: how much the blended range listens to it (FR-264).
SOURCE_WEIGHTS: dict[str, float] = {
    "company_posted": 1.0,
    "posted_vacancies": 0.9,
    "salary_survey": 0.7,
    "levels": 0.55,
    "glassdoor": 0.45,
    "builtin_prior": 0.15,
}

#: Ceiling on the confidence a source kind can produce on its own.
SOURCE_CONFIDENCE_CEILING: dict[str, float] = {
    "company_posted": 0.85,
    "posted_vacancies": 0.75,
    "salary_survey": 0.65,
    "levels": 0.55,
    "glassdoor": 0.45,
    "builtin_prior": 0.25,
}

DEFAULT_CURRENCY_BY_COUNTRY: dict[str, str] = {
    "BE": "EUR", "NL": "EUR", "FR": "EUR", "DE": "EUR", "LU": "EUR", "ES": "EUR",
    "IT": "EUR", "PT": "EUR", "IE": "EUR", "AT": "EUR", "FI": "EUR",
    "GB": "GBP", "UK": "GBP", "CH": "CHF", "SE": "SEK", "DK": "DKK", "NO": "NOK",
    "PL": "PLN", "CZ": "CZK", "US": "USD", "CA": "CAD",
}

#: Annualisation factors.  Monthly pay is annualised at 12; Belgian 13th-month
#: and holiday pay are *not* assumed, because whether they are included is
#: exactly the thing a posting is vague about (CR-405).
PERIOD_FACTORS: dict[str, float] = {"annual": 1.0, "monthly": 12.0, "daily": 220.0}

# ---------------------------------------------------------------------------
# Built-in band prior (FR-264, source of last resort)
# ---------------------------------------------------------------------------

#: Coarse annual gross full-time bands in EUR for a mid-sized Western-European
#: market, by seniority.  This is an ordering prior, not survey data: it exists
#: so an unpriced opportunity is not silently scored as if compensation were
#: unknown, and it is always reported as ``builtin_prior`` with low confidence.
_SENIORITY_BANDS_EUR: dict[str, tuple[float, float]] = {
    "intern": (9_000, 16_000),
    "junior": (30_000, 42_000),
    "medior": (40_000, 58_000),
    "senior": (52_000, 78_000),
    "lead": (62_000, 92_000),
    "principal": (72_000, 110_000),
    "manager": (65_000, 100_000),
    "director": (90_000, 145_000),
    "vp": (120_000, 190_000),
    "c_level": (140_000, 260_000),
    "board": (30_000, 120_000),
}

#: Family multipliers on the seniority band, again coarse and directional only.
_FAMILY_MULTIPLIERS: dict[str, float] = {
    "data & analytics": 1.05,
    "software engineering": 1.05,
    "information security": 1.10,
    "product": 1.05,
    "finance": 1.0,
    "legal & compliance": 1.05,
    "general management": 1.10,
    "consulting & advisory": 1.0,
    "sales & business development": 0.95,
    "marketing & communications": 0.90,
    "human resources": 0.90,
    "it operations": 0.90,
    "operations & supply chain": 0.90,
    "engineering & manufacturing": 0.95,
    "customer success & support": 0.80,
}

#: Country multipliers relative to the Belgian/Dutch reference market.
_COUNTRY_MULTIPLIERS: dict[str, float] = {
    "BE": 1.0, "NL": 1.05, "LU": 1.15, "DE": 1.05, "FR": 0.95, "GB": 1.05,
    "UK": 1.05, "IE": 1.05, "CH": 1.45, "US": 1.35, "SE": 1.0, "DK": 1.1,
    "ES": 0.7, "IT": 0.7, "PT": 0.6, "PL": 0.55, "CZ": 0.55,
}


class CurrencyMismatch(ValueError):
    """A source is quoted in a currency this estimate cannot convert."""


# ---------------------------------------------------------------------------
# Estimate model
# ---------------------------------------------------------------------------


@dataclass
class SourceContribution:
    """One entry of FR-264's source list."""

    kind: str
    label: str
    low: float
    high: float
    currency: str
    weight: float
    sample_size: int = 0
    used: bool = True
    url: str | None = None
    as_of: str | None = None
    note: str | None = None


@dataclass
class CompensationEstimate:
    """FR-264: a range, a confidence and the sources it was built from."""

    currency: str = "EUR"
    comp_min: float | None = None
    comp_max: float | None = None
    confidence: float = 0.0
    is_stated: bool = False
    method: str = "none"
    sources: list[SourceContribution] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # FR-265: advisory only, never folded into the range.
    employer_rating: float | None = None
    employer_review_themes: list[dict] = field(default_factory=list)

    def as_columns(self) -> dict[str, Any]:
        """The ``opportunity`` columns this estimate writes."""
        return {
            "comp_min": self.comp_min,
            "comp_max": self.comp_max,
            "comp_currency": self.currency if self.comp_max is not None else None,
            "comp_confidence": round(self.confidence, 3),
            "comp_is_stated": 1 if self.is_stated else 0,
            "comp_sources": {
                "method": self.method,
                "notes": self.notes,
                "sources": [asdict(s) for s in self.sources],
            },
            "employer_rating": self.employer_rating,
            "employer_review_themes": self.employer_review_themes or None,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def default_currency(country: str | None) -> str:
    return DEFAULT_CURRENCY_BY_COUNTRY.get((country or "").upper(), "EUR")


def annualise(amount: float | None, period: str | None) -> float | None:
    if amount is None:
        return None
    factor = PERIOD_FACTORS.get((period or "annual").lower())
    if factor is None:
        return None
    return amount * factor


def _median_pair(rows: list[tuple[float, float]]) -> tuple[float, float]:
    """Median low and median high - robust to a single wild posting."""
    lows = sorted(r[0] for r in rows)
    highs = sorted(r[1] for r in rows)
    return statistics.median(lows), statistics.median(highs)


def _title_terms(title: str | None) -> list[str]:
    """The two or three words of a title worth matching another posting on."""
    stop = {
        "senior", "junior", "medior", "lead", "principal", "head", "of", "the", "and",
        "a", "an", "m/f", "m/v", "h/f", "fulltime", "full-time", "part-time", "remote",
        "hybrid", "manager", "director",
    }
    words = [w.strip("()[]-/,.").lower() for w in (title or "").split()]
    terms = [w for w in words if len(w) > 3 and w not in stop]
    return terms[:3]


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def _corpus_contribution(
    rows: list[dict], *, label: str, kind: str, currency: str, exclude_vacancy_id: str | None
) -> SourceContribution | None:
    usable: list[tuple[float, float]] = []
    for row in rows:
        if exclude_vacancy_id and row.get("id") == exclude_vacancy_id:
            continue
        row_currency = (row.get("salary_currency") or currency).upper()
        if row_currency != currency:
            continue
        low, high = row.get("salary_min"), row.get("salary_max")
        if low is None or high is None or high <= 0:
            continue
        if low > high:
            low, high = high, low
        # Annual figures only; a monthly posted range would distort the median.
        if high < 5_000:
            continue
        usable.append((float(low), float(high)))
    if len(usable) < MIN_CORPUS_SIZE:
        return None
    low, high = _median_pair(usable)
    return SourceContribution(
        kind=kind,
        label=f"{label} (n={len(usable)})",
        low=round(low, 2),
        high=round(high, 2),
        currency=currency,
        weight=SOURCE_WEIGHTS[kind],
        sample_size=len(usable),
    )


def posted_range_sources(
    opportunity: dict, *, currency: str, exclude_vacancy_id: str | None = None
) -> list[SourceContribution]:
    """FR-264's most defensible source: what comparable postings actually offer.

    At most one same-company set and then the market sets, each tried from
    narrowest to broadest, keeping the first two that clear ``MIN_CORPUS_SIZE``
    - so the estimate says which comparison set it rests on rather than silently
    blending everything.  A width only ever compares roles of the same function
    family or with the same title terms; "everything this employer posted" is
    not a comparable set.
    """
    country = opportunity.get("country")
    family = opportunity.get("function_family")
    seniority = opportunity.get("seniority")
    company_id = opportunity.get("company_id")
    terms = _title_terms(opportunity.get("title"))

    def corpus(kind: str, label: str, **query: Any) -> SourceContribution | None:
        rows = repo.posted_salary_corpus(
            function_family=query.get("function_family"),
            seniority=query.get("seniority"),
            country=query.get("country"),
            title_terms=query.get("title_terms"),
            company_id=query.get("company_id"),
        )
        return _corpus_contribution(
            rows, label=label, kind=kind, currency=currency,
            exclude_vacancy_id=exclude_vacancy_id,
        )

    found: list[SourceContribution] = []

    # Same company, but only for a *comparable* role.  What an employer pays its
    # warehouse staff says nothing about what it would pay a data director, and
    # this source carries the heaviest weight of them all - so a company-wide
    # average would be the most confidently wrong number in the estimate.
    company_widths: list[tuple[str, dict[str, Any]]] = []
    if company_id and family and seniority:
        company_widths.append(
            (f"posted ranges for {seniority} {family} at this company",
             {"company_id": company_id, "function_family": family, "seniority": seniority})
        )
    if company_id and family:
        company_widths.append(
            (f"posted ranges for {family} at this company",
             {"company_id": company_id, "function_family": family})
        )
    if company_id and terms:
        company_widths.append(
            (f"posted ranges at this company for titles like {' '.join(terms)}",
             {"company_id": company_id, "title_terms": terms})
        )
    for label, query in company_widths:
        contribution = corpus("company_posted", label, **query)
        if contribution is not None:
            found.append(contribution)
            break

    market_widths: list[tuple[str, dict[str, Any]]] = []
    if family and seniority:
        market_widths.append(
            (f"posted ranges for {seniority} {family} in {country or 'market'}",
             {"function_family": family, "seniority": seniority, "country": country})
        )
    if family:
        market_widths.append(
            (f"posted ranges for {family} in {country or 'market'}",
             {"function_family": family, "country": country})
        )
    if terms:
        market_widths.append(
            (f"posted ranges for titles like {' '.join(terms)}",
             {"country": country, "title_terms": terms})
        )
    for label, query in market_widths:
        if len(found) >= 2:
            break
        contribution = corpus("posted_vacancies", label, **query)
        if contribution is not None:
            found.append(contribution)
    return found[:2]


#: FR-264: which survey occupation groups speak for a given seniority.
#:
#: National earnings surveys are published per ISCO-08 occupation group, not per
#: the product's own function families, so a row can only be found through the
#: occupation it was published under.  The mapping is deliberately coarse - it
#: is the honest resolution of the source - and it is why these rows anchor a
#: range rather than setting one.
#: An occupation group is a *kind of work*, not a career stage, so a seniority is
#: mapped only where the group genuinely speaks for it.  ``intern`` and
#: ``junior`` map to nothing on purpose: the published figure for
#: "Professionals" is a whole-career mean and would price a first job far above
#: what it pays, and the built-in band prior describes those two far better
#: (CR-405).  An unmapped seniority therefore falls through to that prior.
SURVEY_OCCUPATIONS: dict[str, tuple[str, ...]] = {
    "intern": (),
    "junior": (),
    "medior": ("OC2",),
    "senior": ("OC2",),
    "principal": ("OC2", "OC1"),
    "lead": ("OC1", "OC2"),
    "manager": ("OC1",),
    "director": ("OC1",),
    "vp": ("OC1",),
    "c_level": ("OC1",),
    "board": ("OC1",),
}


def survey_occupations(seniority: str | None) -> list[str]:
    """The occupation codes worth reading for this seniority, best match first."""
    key = (seniority or "").lower()
    if key in SURVEY_OCCUPATIONS:
        return list(SURVEY_OCCUPATIONS[key])
    # An unrecognised seniority is not a licence to guess a band: read the two
    # white-collar groups and let the blend sit between them.
    return ["OC1", "OC2"]


def observation_sources(opportunity: dict, *, currency: str) -> list[SourceContribution]:
    """Salary surveys and Glassdoor/Levels rows collected by other slices (FR-264)."""
    country = opportunity.get("country")
    rows = repo.compensation_observations(
        function_family=opportunity.get("function_family"),
        seniority=opportunity.get("seniority"),
        country=country,
    )
    # Guarded: an unfiltered call returns the whole table, so an opportunity
    # with no company linked would be priced off every observation ever
    # collected, for every country and occupation.
    if opportunity.get("company_id"):
        rows += repo.compensation_observations(company_id=opportunity["company_id"])
    # National survey rows carry an occupation rather than a function family, so
    # they are unreachable by the lookup above and are asked for by occupation.
    # Only the best-matching occupation is taken: two groups from one survey are
    # one source read twice, and would weigh on the blend as if they were two.
    for occupation in survey_occupations(opportunity.get("seniority")):
        survey_rows = repo.compensation_observations(
            country=country, normalised_titles=[occupation], market_wide=True
        )
        if survey_rows:
            rows += survey_rows
            break

    seen: set[str] = set()
    out: list[SourceContribution] = []
    for row in rows:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        kind = row.get("source") or "salary_survey"
        if kind not in SOURCE_WEIGHTS:
            kind = "salary_survey"
        if (row.get("currency") or currency).upper() != currency:
            out.append(
                SourceContribution(
                    kind=kind, label=row.get("source_name") or kind,
                    low=0.0, high=0.0, currency=(row.get("currency") or "").upper(),
                    weight=0.0, used=False,
                    note="not used: quoted in a different currency",
                )
            )
            continue
        low = annualise(
            row.get("amount_p25") if row.get("amount_p25") is not None else row.get("amount_min"),
            row.get("period"),
        )
        high = annualise(
            row.get("amount_p75") if row.get("amount_p75") is not None else row.get("amount_max"),
            row.get("period"),
        )
        median = annualise(row.get("amount_median"), row.get("period"))
        if low is None and median is not None:
            low = median * 0.9
        if high is None and median is not None:
            high = median * 1.1
        if low is None or high is None or high <= 0:
            continue
        out.append(
            SourceContribution(
                kind=kind,
                label=row.get("source_name") or f"{kind} observation",
                low=round(float(low), 2),
                high=round(float(high), 2),
                currency=currency,
                weight=SOURCE_WEIGHTS[kind],
                sample_size=int(row.get("sample_size") or 0),
                url=row.get("source_url"),
                as_of=row.get("as_of") or row.get("collected_at"),
            )
        )
    return out


def campaign_advisory(campaign_id: str | None) -> dict[str, Any]:
    """The campaign's Glassdoor snapshots, or empty when there has been no run.

    Imported lazily and defensively: the estimator is pure corpus work and must
    stay importable, and usable, on an installation where the optional browser
    slice cannot load at all.
    """
    if not campaign_id:
        return {"employers": [], "salaries": []}
    try:
        from dreamjob.browser import glassdoor  # noqa: PLC0415

        snapshots = glassdoor.snapshots_for_campaign(campaign_id)
    except Exception:  # pragma: no cover - the browser slice is optional
        log.debug("no Glassdoor snapshots available for campaign %s", campaign_id)
        return {"employers": [], "salaries": []}
    return {
        "employers": snapshots.get("employers") or [],
        "salaries": snapshots.get("salaries") or [],
    }


def _same_company(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    from dreamjob.pipeline.dedup import normalise_company_name  # noqa: PLC0415

    a, b = normalise_company_name(left), normalise_company_name(right)
    return bool(a) and a == b


def advisory_sources(
    opportunity: dict, *, currency: str, advisory: dict[str, Any] | None
) -> list[SourceContribution]:
    """Glassdoor salary pages this campaign collected, for *this* role (FR-264).

    Campaign-scoped by design (see the module docstring): the rows are read back
    from the browser run rather than from the shared base, and a page counts
    only when it is both the same employer and a comparable title.
    """
    salaries = (advisory or {}).get("salaries") or []
    if not salaries:
        return []
    company_name = opportunity.get("company_name")
    terms = set(_title_terms(opportunity.get("title")))

    matched: list[tuple[float, float]] = []
    samples = 0
    url: str | None = None
    as_of: str | None = None
    for row in salaries:
        if not _same_company(company_name, row.get("company_name")):
            continue
        # Company alone is not a comparison set; the title has to line up too.
        if terms and not (terms & set(_title_terms(row.get("job_title")))):
            continue
        if (row.get("currency") or currency).upper() != currency:
            continue
        low, high = row.get("low"), row.get("high")
        if low is None or high is None or high <= 0:
            continue
        low, high = float(low), float(high)
        if low > high:
            low, high = high, low
        # Annual figures only - a monthly or hourly page would distort the median.
        if high < 5_000:
            continue
        matched.append((low, high))
        samples += int(row.get("sample_size") or 0)
        url = url or row.get("url")
        as_of = as_of or row.get("collected_at")

    if not matched:
        return []
    low, high = _median_pair(matched)
    return [
        SourceContribution(
            kind="glassdoor",
            label=f"Glassdoor salary pages for this role at this company (n={len(matched)})",
            low=round(low, 2),
            high=round(high, 2),
            currency=currency,
            weight=SOURCE_WEIGHTS["glassdoor"],
            sample_size=samples,
            url=url,
            as_of=as_of,
            note=(
                "advisory: self-reported figures collected for this campaign, kept out of "
                "the shared knowledge base"
            ),
        )
    ]


def builtin_prior(opportunity: dict, *, currency: str) -> SourceContribution | None:
    """The last-resort band prior.  Directional only, and always labelled as such."""
    seniority = (opportunity.get("seniority") or "").lower()
    band = _SENIORITY_BANDS_EUR.get(seniority)
    if band is None or currency != "EUR":
        return None
    multiplier = _FAMILY_MULTIPLIERS.get((opportunity.get("function_family") or "").lower(), 1.0)
    multiplier *= _COUNTRY_MULTIPLIERS.get((opportunity.get("country") or "BE").upper(), 0.9)
    fte = opportunity.get("fte_percentage")
    if isinstance(fte, int) and 10 <= fte < 100:
        multiplier *= fte / 100
    return SourceContribution(
        kind="builtin_prior",
        label=f"built-in band prior for {seniority}",
        low=round(band[0] * multiplier, 2),
        high=round(band[1] * multiplier, 2),
        currency=currency,
        weight=SOURCE_WEIGHTS["builtin_prior"],
        note=(
            "coarse ordering prior, not survey data: use only to tell seniority bands "
            "apart, never as a market figure"
        ),
    )


# ---------------------------------------------------------------------------
# FR-265: employer-review signals, advisory only
# ---------------------------------------------------------------------------


def _advisory_employer_rows(
    advisory: dict[str, Any] | None, company_name: str | None
) -> list[dict]:
    """Glassdoor employer pages for this company, shaped like a review summary row."""
    rows: list[dict] = []
    for snapshot in (advisory or {}).get("employers") or []:
        if not _same_company(company_name, snapshot.get("company_name")):
            continue
        themes: list[dict] = []
        raw = snapshot.get("themes") or {}
        if isinstance(raw, dict):
            for polarity, items in raw.items():
                themes.extend(
                    {"theme": str(t), "polarity": polarity} for t in (items or []) if t
                )
        rows.append(
            {
                "rating": snapshot.get("rating"),
                "rating_scale_max": 5,
                "review_count": snapshot.get("review_count"),
                "themes": themes,
                "source": snapshot.get("source") or "glassdoor",
            }
        )
    return rows


def employer_signal(
    company_id: str | None,
    *,
    advisory: dict[str, Any] | None = None,
    company_name: str | None = None,
) -> tuple[float | None, list[dict]]:
    """Rating normalised to 0-5 and the review themes (FR-265).

    Advisory input: this never touches the compensation range, and scoring gives
    it a small, separately visible weight.  Shared ``employer_review_summary``
    rows and the campaign's own Glassdoor snapshots are both read; the snapshots
    stay campaign-scoped and are never written back into the shared base.
    """
    rows = repo.employer_reviews(company_id) if company_id else []
    rows = [*rows, *_advisory_employer_rows(advisory, company_name)]
    if not rows:
        return None, []
    ratings: list[tuple[float, float]] = []
    themes: list[dict] = []
    for row in rows:
        rating = row.get("rating")
        scale = float(row.get("rating_scale_max") or 5) or 5
        if rating is not None:
            weight = float(row.get("review_count") or 1)
            ratings.append((float(rating) / scale * 5.0, weight))
        for theme in row.get("themes") or []:
            if isinstance(theme, dict):
                themes.append({**theme, "source": row.get("source")})
    if not ratings:
        return None, themes[:20]
    total_weight = sum(w for _, w in ratings) or 1.0
    weighted = sum(r * w for r, w in ratings) / total_weight
    return round(weighted, 2), themes[:20]


# ---------------------------------------------------------------------------
# The estimate (FR-264)
# ---------------------------------------------------------------------------


def _blend(sources: list[SourceContribution]) -> tuple[float, float]:
    total = sum(s.weight for s in sources if s.used) or 1.0
    low = sum(s.low * s.weight for s in sources if s.used) / total
    high = sum(s.high * s.weight for s in sources if s.used) / total
    return low, high


def _confidence(sources: list[SourceContribution]) -> float:
    used = [s for s in sources if s.used]
    if not used:
        return 0.0
    best = max(SOURCE_CONFIDENCE_CEILING.get(s.kind, 0.3) for s in used)
    # Independent sources that agree raise confidence; sources that disagree by
    # more than half the range lower it.
    agreement = 0.0
    if len(used) > 1:
        mids = [(s.low + s.high) / 2 for s in used]
        spread = (max(mids) - min(mids)) / max(statistics.mean(mids), 1.0)
        agreement = 0.10 if spread < 0.20 else (0.0 if spread < 0.45 else -0.15)
    sample = min(0.08, 0.01 * sum(min(s.sample_size, 8) for s in used) / max(len(used), 1))
    value = best + agreement + sample
    # ``SOURCE_CONFIDENCE_CEILING`` is what a kind can reach *on its own*: a big
    # sample of the same kind of evidence is still that kind of evidence, so it
    # may not lift a source past its own ceiling.  Only genuinely independent
    # kinds corroborating each other may.
    if len({s.kind for s in used}) == 1:
        value = min(value, best)
    return round(max(0.05, min(0.95, value)), 3)


def estimate(
    opportunity: dict,
    *,
    currency: str | None = None,
    include_prior: bool = True,
    advisory: dict[str, Any] | None = None,
) -> CompensationEstimate:
    """FR-264: an estimated range with confidence and a source list.

    A stated employer range is reported as stated and is not blended with market
    data; the market sources are still listed, unused, so a negotiation brief
    (FR-444) can see what the posting is worth against the market.

    ``advisory`` carries this campaign's Glassdoor snapshots when there are any;
    pass the payload from :func:`campaign_advisory`.
    """
    target_currency = (
        currency or opportunity.get("comp_currency") or default_currency(opportunity.get("country"))
    ).upper()
    result = CompensationEstimate(currency=target_currency)

    market: list[SourceContribution] = []
    market.extend(
        posted_range_sources(
            opportunity, currency=target_currency,
            exclude_vacancy_id=opportunity.get("vacancy_id"),
        )
    )
    market.extend(observation_sources(opportunity, currency=target_currency))
    market.extend(advisory_sources(opportunity, currency=target_currency, advisory=advisory))
    if include_prior and not [s for s in market if s.used]:
        prior = builtin_prior(opportunity, currency=target_currency)
        if prior is not None:
            market.append(prior)

    stated_min, stated_max = opportunity.get("comp_min"), opportunity.get("comp_max")
    if opportunity.get("comp_is_stated") and (stated_min is not None or stated_max is not None):
        low = float(stated_min if stated_min is not None else stated_max)
        high = float(stated_max if stated_max is not None else stated_min)
        result.comp_min, result.comp_max = min(low, high), max(low, high)
        result.is_stated = True
        result.method = "stated_by_employer"
        result.confidence = 0.95
        result.sources = [
            SourceContribution(
                kind="stated",
                label="range stated in the posting",
                low=result.comp_min,
                high=result.comp_max,
                currency=opportunity.get("comp_currency") or target_currency,
                weight=1.0,
                url=opportunity.get("source_url"),
                as_of=opportunity.get("posted_at"),
            ),
            *[SourceContribution(**{**asdict(s), "used": False,
                                    "note": s.note or "market context, not used in the range"})
              for s in market],
        ]
    elif [s for s in market if s.used]:
        low, high = _blend(market)
        result.comp_min, result.comp_max = round(low, 2), round(high, 2)
        result.confidence = _confidence(market)
        result.method = "blended:" + "+".join(
            sorted({s.kind for s in market if s.used})
        )
        result.sources = market
    else:
        result.method = "insufficient_data"
        result.notes.append("No comparable posted ranges, survey rows or observations found.")
        result.sources = market

    rating, themes = employer_signal(
        opportunity.get("company_id"),
        advisory=advisory,
        company_name=opportunity.get("company_name"),
    )
    result.employer_rating = rating
    result.employer_review_themes = themes
    if rating is not None:
        result.notes.append(
            "Employer review rating is advisory input only (FR-265); it does not move "
            "the compensation range."
        )
    return result


def fit_against_directives(
    estimate_result: CompensationEstimate, directive_set: dir_mod.DirectiveSetLike | None
) -> dict[str, Any]:
    """How the estimated range sits against the seeker's minimum (FR-146, FR-281).

    Returns the compensation sub-score input: ``score`` in 0..1 plus the reason,
    so the ranked list can explain it without recomputing anything.
    """
    if directive_set is None:
        return {"score": None, "reason": "no compensation directive set"}
    comp = dir_mod.coerce_directive_set(directive_set).compensation
    minimum = comp.minimum_package
    if minimum is None:
        return {"score": None, "reason": "no minimum package stated"}
    factor = PERIOD_FACTORS.get(comp.period.value, 1.0)
    target = minimum * factor
    if estimate_result.comp_max is None:
        return {"score": None, "reason": "no compensation estimate available"}
    if (estimate_result.currency or "EUR") != (comp.currency or "EUR").upper():
        return {
            "score": None,
            "reason": f"estimate in {estimate_result.currency}, directive in {comp.currency}",
        }

    low = estimate_result.comp_min if estimate_result.comp_min is not None else \
        estimate_result.comp_max
    high = estimate_result.comp_max
    if low >= target:
        score = 1.0
        reason = "the whole estimated range clears the stated minimum"
    elif high >= target:
        # Partial credit for the fraction of the range above the minimum.
        span = max(high - low, 1.0)
        score = round(0.55 + 0.4 * ((high - target) / span), 3)
        reason = "the upper part of the estimated range clears the stated minimum"
    else:
        shortfall = (target - high) / target
        score = round(max(0.0, 0.5 - shortfall), 3)
        reason = f"the estimate falls about {shortfall:.0%} short of the stated minimum"
    return {
        "score": max(0.0, min(1.0, score)),
        "reason": reason,
        "target": target,
        "confidence": estimate_result.confidence,
        "is_stated": estimate_result.is_stated,
    }


# ---------------------------------------------------------------------------
# Campaign pass
# ---------------------------------------------------------------------------


def enrich_opportunity(
    opportunity: dict, *, advisory: dict[str, Any] | None = None
) -> CompensationEstimate:
    """Estimate and persist the compensation block for one opportunity.

    ``advisory`` is looked up from the opportunity's own campaign when the caller
    does not already hold it, so a single-opportunity refresh sees the same
    Glassdoor snapshots a whole-campaign pass would.
    """
    if advisory is None:
        advisory = campaign_advisory(opportunity.get("campaign_id"))
    result = estimate(opportunity, advisory=advisory)
    repo.save_compensation(
        opportunity["id"], result.as_columns(),
        job_seeker_id=opportunity.get("job_seeker_id"),
    )
    return result


def enrich_campaign(job_seeker_id: str, campaign_id: str, *, limit: int = 1000) -> dict[str, Any]:
    """FR-264/FR-265 over a whole campaign.  No network, no LLM: pure corpus work."""
    priced = 0
    stated = 0
    unpriced = 0
    rows = repo.list_opportunities(
        job_seeker_id, campaign_id=campaign_id, limit=limit, respect_manual_order=False
    )
    # Read once for the campaign rather than once per opportunity.
    advisory = campaign_advisory(campaign_id)
    for opportunity in rows:
        result = enrich_opportunity(opportunity, advisory=advisory)
        if result.is_stated:
            stated += 1
        elif result.comp_max is not None:
            priced += 1
        else:
            unpriced += 1
    return {
        "campaign_id": campaign_id,
        "opportunities": len(rows),
        "stated": stated,
        "estimated": priced,
        "unpriced": unpriced,
        # So the screen can say why the advisory source contributed nothing when
        # no Glassdoor run has been made for this campaign.
        "advisory_salary_pages": len(advisory.get("salaries") or []),
    }
