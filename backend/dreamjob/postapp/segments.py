"""Outcome rates by what was applied for (extends FR-425, feeds FR-285).

``outcomes.analyse`` answers "which of my *choices about the message* worked" -
email style, CV template, contact type, send time.  This module answers the
question the product owner actually asks after a month of applying: **which
kinds of job and company answer me, and which ignore me.**

The dimensions are the ones a job seeker can act on by changing a directive:

    function_family     the kind of work
    seniority           the level applied for
    size_band           company size
    company_stage       startup / scale-up / established / listed / public
    sector              the company's primary sector code
    work_arrangement    on-site / hybrid / remote
    country             where the role is
    opportunity_kind    advertised vacancy vs speculative opening
    language            the language the application was written in

Every rate is reported with its sample size and a Wilson interval, and a
segment is only called out when there is enough evidence to distinguish it
from the baseline.  Nine applications and one interview is not a pattern, and
this module is written to keep saying so.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from dreamjob.db.connection import from_json, insert_row, utcnow
from dreamjob.db.repositories import learning as repo
from dreamjob.postapp.outcomes import OUTCOMES, outcome_flags, wilson_interval

log = logging.getLogger(__name__)

# A dimension value needs this many resolved applications before it is shown
# at all, and this many before it is allowed to drive advice.
MIN_SEGMENT_TO_REPORT = 3
MIN_SEGMENT_TO_ADVISE = 6

# How far a segment must sit from the baseline before it is worth a sentence.
MATERIAL_LIFT = 0.10

DIMENSIONS: dict[str, str] = {
    "function_family": "kind of work",
    "seniority": "seniority level",
    "size_band": "company size",
    "company_stage": "company stage",
    "sector": "sector",
    "work_arrangement": "work arrangement",
    "country": "country",
    "opportunity_kind": "advertised vs speculative",
    "language": "language of the application",
}


@dataclass
class Segment:
    """One value of one dimension, and how applications to it fared."""

    dimension: str
    value: str
    n: int
    successes: int
    rate: float
    interval: tuple[float, float]
    lift: float                    # rate minus baseline, in absolute points
    evidence: str                  # 'thin' | 'indicative' | 'suggestive'
    rejections: int = 0
    no_response: int = 0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["interval"] = [round(self.interval[0], 3), round(self.interval[1], 3)]
        for k in ("rate", "lift"):
            d[k] = round(getattr(self, k), 3)
        return d

    def sentence(self, outcome: str, baseline: float, baseline_n: int) -> str:
        """Readable, and never without the sample size it rests on."""
        band = f"{self.interval[0]:.0%}–{self.interval[1]:.0%}"
        direction = "above" if self.lift >= 0 else "below"
        return (
            f"{DIMENSIONS.get(self.dimension, self.dimension)} “{self.value}”: "
            f"{self.successes} of {self.n} reached {outcome} ({self.rate:.0%}, "
            f"plausible range {band}) — {abs(self.lift):.0%} points {direction} your "
            f"overall {baseline:.0%} across {baseline_n} resolved applications. "
            f"Evidence: {self.evidence}."
        )


@dataclass
class SegmentAnalysis:
    job_seeker_id: str
    outcome: str
    sample_size: int = 0
    resolved_size: int = 0
    baseline_rate: float = 0.0
    segments: dict[str, list[Segment]] = field(default_factory=dict)
    strongest: list[Segment] = field(default_factory=list)
    weakest: list[Segment] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    computed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_seeker_id": self.job_seeker_id,
            "outcome": self.outcome,
            "sample_size": self.sample_size,
            "resolved_size": self.resolved_size,
            "baseline_rate": round(self.baseline_rate, 3),
            "segments": {
                dim: [s.as_dict() for s in items] for dim, items in self.segments.items()
            },
            "strongest": [s.as_dict() for s in self.strongest],
            "weakest": [s.as_dict() for s in self.weakest],
            "readable": {
                "strongest": [
                    s.sentence(self.outcome, self.baseline_rate, self.resolved_size)
                    for s in self.strongest
                ],
                "weakest": [
                    s.sentence(self.outcome, self.baseline_rate, self.resolved_size)
                    for s in self.weakest
                ],
            },
            "caveats": self.caveats,
            "computed_at": self.computed_at,
            "dimension_labels": DIMENSIONS,
        }

    @property
    def actionable(self) -> bool:
        """Whether there is enough here to propose a change of direction."""
        return self.resolved_size >= MIN_SEGMENT_TO_ADVISE * 2 and bool(
            [s for s in self.weakest if s.n >= MIN_SEGMENT_TO_ADVISE]
        )


def _dimension_values(row: dict) -> dict[str, str | None]:
    """Pull the dimension values off one joined pipeline-card row."""
    sector = None
    codes = from_json(row.get("sector_codes"), []) or []
    if isinstance(codes, list) and codes:
        first = codes[0]
        sector = first.get("label") or first.get("code") if isinstance(first, dict) else str(first)
    elif isinstance(codes, dict):
        sector = codes.get("primary") or next(iter(codes.values()), None)

    variables = row.get("variables") or {}
    if isinstance(variables, str):
        variables = from_json(variables, {}) or {}

    return {
        "function_family": row.get("function_family"),
        "seniority": row.get("seniority"),
        "size_band": row.get("size_band"),
        "company_stage": row.get("company_stage"),
        "sector": sector,
        "work_arrangement": row.get("work_arrangement"),
        "country": row.get("country"),
        "opportunity_kind": row.get("opportunity_kind"),
        "language": row.get("language") or variables.get("language"),
    }


def analyse_segments(
    job_seeker_id: str,
    *,
    outcome: str = "reply",
    silence_days: int = 21,
    limit: int = 1000,
    store: bool = True,
) -> SegmentAnalysis:
    """Outcome rate per dimension value, with intervals and honest caveats."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}")

    rows = repo.segment_rows(job_seeker_id, limit=limit)
    analysis = SegmentAnalysis(
        job_seeker_id=job_seeker_id, outcome=outcome, computed_at=utcnow()
    )
    analysis.sample_size = len(rows)

    # Only resolved applications carry information. One sent yesterday is not
    # evidence of silence.
    resolved: list[tuple[dict, dict]] = []
    for row in rows:
        flags = outcome_flags(row, silence_days=silence_days)
        if flags.get("closed") or flags.get("reply"):
            resolved.append((row, flags))

    analysis.resolved_size = len(resolved)
    if not resolved:
        analysis.caveats.append(
            "No application has resolved yet — nothing here would mean anything."
        )
        return analysis

    total_success = sum(1 for _, f in resolved if f.get(outcome))
    analysis.baseline_rate = total_success / len(resolved)

    buckets: dict[str, dict[str, list[tuple[dict, dict]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row, flags in resolved:
        for dim, value in _dimension_values(row).items():
            if value:
                buckets[dim][str(value)].append((row, flags))

    for dim, values in buckets.items():
        items: list[Segment] = []
        for value, members in values.items():
            n = len(members)
            if n < MIN_SEGMENT_TO_REPORT:
                continue
            successes = sum(1 for _, f in members if f.get(outcome))
            rate = successes / n
            low, high = wilson_interval(successes, n)
            lift = rate - analysis.baseline_rate

            if n < MIN_SEGMENT_TO_ADVISE:
                evidence = "thin"
            elif low > analysis.baseline_rate or high < analysis.baseline_rate:
                evidence = "suggestive"
            else:
                evidence = "indicative"

            items.append(
                Segment(
                    dimension=dim,
                    value=value,
                    n=n,
                    successes=successes,
                    rate=rate,
                    interval=(low, high),
                    lift=lift,
                    evidence=evidence,
                    rejections=sum(1 for _, f in members if f.get("rejection")),
                    no_response=sum(1 for _, f in members if f.get("no_response")),
                )
            )
        if items:
            analysis.segments[dim] = sorted(items, key=lambda s: s.rate, reverse=True)

    ranked = [s for group in analysis.segments.values() for s in group]
    material = [
        s
        for s in ranked
        if s.n >= MIN_SEGMENT_TO_ADVISE and abs(s.lift) >= MATERIAL_LIFT
    ]
    analysis.strongest = sorted(material, key=lambda s: s.lift, reverse=True)[:5]
    analysis.weakest = sorted([s for s in material if s.lift < 0], key=lambda s: s.lift)[:5]

    analysis.caveats = _caveats(analysis)

    if store:
        try:
            insert_row(
                "outcome_segment_run",
                {
                    "job_seeker_id": job_seeker_id,
                    "outcome": outcome,
                    "sample_size": analysis.sample_size,
                    "resolved_size": analysis.resolved_size,
                    "baseline_rate": analysis.baseline_rate,
                    "segments": {
                        d: [s.as_dict() for s in items]
                        for d, items in analysis.segments.items()
                    },
                    "caveats": analysis.caveats,
                    "computed_at": analysis.computed_at,
                },
            )
        except Exception:  # noqa: BLE001 - a stored snapshot is a convenience
            log.exception("Could not store segment run for %s", job_seeker_id)

    return analysis


def _caveats(analysis: SegmentAnalysis) -> list[str]:
    """What these numbers cannot support. Written to be shown, not logged."""
    out: list[str] = []
    n = analysis.resolved_size

    if n < 10:
        out.append(
            f"Only {n} applications have resolved. Differences at this size are "
            "usually noise; treat everything below as a hint about where to look, "
            "not a finding."
        )
    elif n < 25:
        out.append(
            f"{n} resolved applications is enough to notice a pattern but not to "
            "trust one. A segment needs to hold up over another dozen before it "
            "is worth reorganising a search around."
        )

    thin = [s for group in analysis.segments.values() for s in group if s.evidence == "thin"]
    if thin:
        out.append(
            f"{len(thin)} segments are shown for completeness but have fewer than "
            f"{MIN_SEGMENT_TO_ADVISE} applications behind them and are excluded from advice."
        )

    if analysis.baseline_rate == 0:
        out.append(
            "Nothing has reached this outcome yet, so every segment reads 0% and "
            "no comparison between them is meaningful."
        )
    elif analysis.baseline_rate == 1:
        out.append("Every resolved application reached this outcome, so there is nothing to separate.")

    out.append(
        "These are observed rates, not causes. A low rate for a segment may reflect "
        "which companies happened to be in it, how many roles were speculative, or "
        "when the applications went out."
    )
    return out
