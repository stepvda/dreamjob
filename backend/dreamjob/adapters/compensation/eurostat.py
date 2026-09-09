"""Eurostat Structure of Earnings Survey - national salary figures (FR-264, IR-101).

FR-264 ranks its sources by how defensible they are, and puts "national salary
surveys" third: market-wide rather than company-specific, so they anchor a range
rather than decide it.  This adapter is that source.

Endpoint (keyless, documented, JSON-stat 2.0):

    GET https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/
        earn_ses22_25?format=JSON&lang=EN&sex=T&indic_se=ERN&unit=EUR
        &isco08=OC1&geo=BE

    -> "Mean monthly earnings by sex, occupation and size class of the
        enterprise (2022)"

Why this dataset rather than a scraped salary site:

* **It is a real survey.**  The Structure of Earnings Survey is a statutory
  four-yearly collection run by the national statistical institutes across the
  EU.  Its figures are gross monthly earnings actually paid, not what a
  self-selected sample of visitors chose to type into a form.
* **It is open data.**  Eurostat's re-use policy permits redistribution with
  attribution, which is why - unlike the Glassdoor slice, whose output stays
  campaign-scoped - these rows are written into the *shared* knowledge base.
* **It is coarse, and says so.**  The survey publishes ISCO-08 occupation
  groups ("Managers", "Professionals"), not job titles.  Every row is therefore
  stored with no ``function_family``, and the estimator reads it through
  :data:`~dreamjob.pipeline.compensation.SURVEY_OCCUPATIONS`.  A range built
  from it is an anchor for the right country and broad occupation - never a
  figure for a particular job.

The range stored per occupation is the spread across enterprise size classes:
mean earnings for managers in Belgium run from the small-firm mean to the
large-firm mean, and that dispersion is a real, published property of the
market rather than a confidence interval invented here.

``value`` in JSON-stat is a sparse map keyed by the flat row-major index over
the declared dimensions; :func:`decode_jsonstat` turns those keys back into
dimension codes, which is the whole of the parsing work.
"""

from __future__ import annotations

import json
import logging
import statistics
from typing import Any
from urllib.parse import urlencode

from dreamjob.adapters.base import (
    AccessMethod,
    AdapterCapabilities,
    NormalisedRecord,
    PlanItem,
    RawRecord,
    SourceAdapter,
    SourceType,
    ToSStatus,
    register_adapter,
)
from dreamjob.adapters.query_errors import UnusableQuery

log = logging.getLogger(__name__)

BASE = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"

#: The most recent Structure of Earnings Survey wave, and the year it describes.
DATASET = "earn_ses22_25"
SURVEY_YEAR = "2022"
SOURCE_NAME = "Eurostat Structure of Earnings Survey 2022"

#: Occupation groups worth storing: the white-collar groups the product's users
#: actually work in.  The survey publishes more; collecting groups no directive
#: can ever match would be requests spent for nothing.
OCCUPATIONS: tuple[str, ...] = ("OC1", "OC2", "OC3", "OC4")

#: Countries the survey covers.  ``geo`` is repeated per country in one request.
COVERAGE: tuple[str, ...] = (
    "BE", "NL", "LU", "DE", "FR", "IE", "AT", "FI", "ES", "IT", "PT", "GR",
    "PL", "CZ", "SK", "HU", "RO", "BG", "HR", "SI", "EE", "LV", "LT", "DK",
    "SE", "CY", "MT", "NO", "CH", "IS", "RS", "TR",
)

#: Ceiling on the countries one request may ask for, so a plan item stays small.
MAX_COUNTRIES_PER_ITEM = 12


def dataset_url(countries: list[str], occupations: list[str] | None = None) -> str:
    """The one documented request shape this adapter ever sends."""
    params: list[tuple[str, str]] = [
        ("format", "JSON"),
        ("lang", "EN"),
        ("sex", "T"),              # both sexes; the product does not split on it
        ("indic_se", "ERN"),       # gross earnings, not overtime or shift premia
        ("unit", "EUR"),           # comparable across the euro area and outside it
    ]
    params += [("isco08", code) for code in (occupations or OCCUPATIONS)]
    params += [("geo", code.upper()) for code in countries]
    return f"{BASE}/{DATASET}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# JSON-stat 2.0
# ---------------------------------------------------------------------------


def decode_jsonstat(payload: dict) -> list[dict[str, Any]]:
    """Flatten a JSON-stat cube into ``{dimension: code, ..., "value": float}``.

    ``value`` is sparse and keyed by the flat row-major offset over ``size``, so
    each key is decoded back into one code per dimension.  Cells the survey did
    not publish are simply absent, which is why the map is walked rather than
    the full product of the dimensions.
    """
    ids: list[str] = payload.get("id") or []
    size: list[int] = payload.get("size") or []
    values: dict[str, Any] = payload.get("value") or {}
    if not ids or len(ids) != len(size) or not values:
        return []

    strides = [1] * len(size)
    for i in range(len(size) - 2, -1, -1):
        strides[i] = strides[i + 1] * size[i + 1]

    # index -> code, per dimension, so a flat offset can be read back.
    codes: list[dict[int, str]] = []
    labels: dict[str, dict[str, str]] = {}
    for dim in ids:
        category = ((payload.get("dimension") or {}).get(dim) or {}).get("category") or {}
        index = category.get("index") or {}
        if isinstance(index, list):
            index = {code: position for position, code in enumerate(index)}
        codes.append({position: code for code, position in index.items()})
        labels[dim] = category.get("label") or {}

    out: list[dict[str, Any]] = []
    for flat, value in values.items():
        if value is None:
            continue
        try:
            offset = int(flat)
        except (TypeError, ValueError):
            continue
        row: dict[str, Any] = {}
        usable = True
        for position, dim in enumerate(ids):
            code = codes[position].get((offset // strides[position]) % size[position])
            if code is None:
                usable = False
                break
            row[dim] = code
        if not usable:
            continue
        try:
            row["value"] = float(value)
        except (TypeError, ValueError):
            continue
        row["labels"] = labels
        out.append(row)
    return out


def aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per country and occupation, spread across enterprise size.

    The low and high are the smallest and largest size-class means and the
    middle is their median - a published dispersion, not a fabricated interval.
    """
    grouped: dict[tuple[str, str], list[float]] = {}
    labels: dict[str, dict[str, str]] = {}
    for row in rows:
        geo, isco = row.get("geo"), row.get("isco08")
        if not geo or not isco:
            continue
        grouped.setdefault((geo, isco), []).append(row["value"])
        labels = row.get("labels") or labels

    out: list[dict[str, Any]] = []
    for (geo, isco), amounts in sorted(grouped.items()):
        usable = [a for a in amounts if a > 0]
        if not usable:
            continue
        out.append(
            {
                "country": geo,
                "occupation": isco,
                "occupation_label": (labels.get("isco08") or {}).get(isco) or isco,
                "amount_min": round(min(usable), 2),
                "amount_median": round(statistics.median(usable), 2),
                "amount_max": round(max(usable), 2),
                "size_classes": len(usable),
            }
        )
    return out


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


@register_adapter
class EurostatEarningsAdapter(SourceAdapter):
    key = "compensation.eurostat_ses"
    display_name = "Eurostat Structure of Earnings Survey"
    source_type = SourceType.COMPENSATION
    access_method = AccessMethod.API
    coverage_countries = list(COVERAGE)
    tos_status = ToSStatus.PERMITTED
    rate_limit_rps = 0.5
    legal_notes = (
        "Eurostat dissemination API, keyless and documented. Eurostat's re-use policy "
        "permits reproduction with attribution to the source, which every stored row "
        "carries in source_name and source_url. Figures are mean gross monthly "
        "earnings by ISCO-08 occupation group and enterprise size class."
    )
    capabilities = AdapterCapabilities(
        keyword_search=False,       # the survey is published per occupation group
        location_filter=True,
        seniority_filter=False,
        pagination=False,
        max_results_per_query=len(OCCUPATIONS) * MAX_COUNTRIES_PER_ITEM,
    )

    # -- plan (FR-162) ------------------------------------------------------
    def plan(self, directives: dict, composite_profile: dict, caps: dict) -> list[PlanItem]:
        countries = _countries_from(directives)
        covered = [c for c in countries if c in COVERAGE]
        if not covered:
            return []
        chunks = [
            covered[i : i + MAX_COUNTRIES_PER_ITEM]
            for i in range(0, len(covered), MAX_COUNTRIES_PER_ITEM)
        ]
        return [
            PlanItem(
                adapter_key=self.key,
                native_query={"countries": chunk, "occupations": list(OCCUPATIONS)},
                rationale=(
                    "Mean gross earnings by occupation group for "
                    f"{', '.join(chunk)}, from the statutory four-yearly EU earnings "
                    "survey - the market anchor FR-264 asks for when no comparable "
                    "posted range exists"
                ),
                estimated_pages=1,
                estimated_seconds=6,
            )
            for chunk in chunks
        ]

    # -- fetch (IR-102, FR-182) ---------------------------------------------
    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        query = item.native_query or {}
        countries = [str(c).upper() for c in (query.get("countries") or []) if c]
        countries = [c for c in countries if c in COVERAGE][:MAX_COUNTRIES_PER_ITEM]
        if not countries:
            raise UnusableQuery(
                self.key,
                "no country this survey covers was named in the plan item",
                expected=("countries",),
            )
        occupations = [str(o).upper() for o in (query.get("occupations") or OCCUPATIONS)]
        url = dataset_url(countries, occupations)
        if self.egress is None:
            raise UnusableQuery(self.key, "no egress client was supplied to the adapter")
        result = await self.egress.fetch(url, headers={"Accept": "application/json"})
        if not result or not result.ok or not (result.text or "").lstrip().startswith("{"):
            log.warning("[%s] %s did not answer JSON", self.key, url)
            return []
        return [
            RawRecord(
                url=url,
                content=result.text,
                content_type="application/json",
                raw_document_id=getattr(result, "raw_document_id", None),
            )
        ]

    # -- parse (FR-183) -----------------------------------------------------
    def parse(self, raw: RawRecord) -> list[dict]:
        payload = json.loads(raw.content)
        if "error" in payload:
            log.warning("[%s] the API refused the query: %s", self.key, payload["error"])
            return []
        return aggregate(decode_jsonstat(payload))

    # -- normalise (FR-264) -------------------------------------------------
    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        if not parsed.get("country") or parsed.get("amount_median") is None:
            return None
        return NormalisedRecord(
            entity_type="compensation_observation",
            data={
                "source": "salary_survey",
                "source_name": SOURCE_NAME,
                "source_url": raw.url,
                "role_title": parsed["occupation_label"],
                # The occupation code is the key the estimator looks rows up by.
                "normalised_title": parsed["occupation"],
                # Left null on purpose: the survey knows occupations, not the
                # product's function families, and inventing one here would make
                # a coarse figure look like a specific one.
                "function_family": None,
                "seniority": None,
                "country": parsed["country"],
                "currency": "EUR",
                "period": "monthly",
                "amount_min": parsed["amount_min"],
                "amount_median": parsed["amount_median"],
                "amount_max": parsed["amount_max"],
                "includes_variable": 0,
                "as_of": SURVEY_YEAR,
                "access_method": self.access_method.value,
                "confidence": 0.7,
            },
            confidence=0.7,
        )


def _countries_from(directives: dict) -> list[str]:
    """ISO-2 codes the directive set asks for, in order and de-duplicated."""
    location = (directives or {}).get("location") or {}
    raw = location.get("countries") or (directives or {}).get("countries") or []
    out: list[str] = []
    for value in raw if isinstance(raw, list) else [raw]:
        code = str(value or "").strip().upper()
        if len(code) == 2 and code not in out:
            out.append(code)
    return out
