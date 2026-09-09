"""Five-year financial analysis and the two hiring scores (FR-243..246, RK-06).

A company's financial trajectory is the proxy for two things a job seeker
cannot ask about directly: whether it can pay well, and whether it is investing
in the kind of new activity that creates a role.  This module turns the stored
filings into those two answers.

The arithmetic is deterministic and lives here in full - compound growth of
revenue and headcount, margin trend, personnel cost per FTE, current ratio,
solvency ratio, and the growing/stable/declining/volatile classification with a
confidence that falls with the number of years available and with every
reconciliation flag the extractor raised (FR-243, NFR-404).

The two 0-100 scores (FR-244) are also deterministic: a model that scores
companies would be neither reproducible nor auditable.  What the model writes
is the *rationale*, and it is held to one rule - it must name the figures it
used.  A rationale that cites no numbers is rejected, retried once, and then
replaced by a generated sentence that does cite them.

FR-245: a company with no usable filings is not skipped.  Its profile is built
from secondary signals - funding rounds, headcount growth, press - scored
conservatively, and marked ``is_estimated`` everywhere it surfaces.

FR-246: where a registry exposes the parent/subsidiary relation, the group is
recorded and the consolidated picture is computed alongside the entity-level
one; both are returned, and the difference between them is usually the whole
story of a Belgian subsidiary of a foreign group.
"""

from __future__ import annotations

import json
import logging
import math
import re
import statistics
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

from dreamjob.db.connection import from_json, utcnow
from dreamjob.db.repositories import financials as repo
from dreamjob.db.repositories import knowledge as kb_repo
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError
from dreamjob.pipeline import filing_extract as fx
from dreamjob.pipeline.dedup import normalise_company_name, normalise_legal_id

log = logging.getLogger(__name__)

#: ``job_run.kind`` of the background collection-and-analysis job (FR-185).
JOB_KIND = "financial"

DEFAULT_YEARS = 5

#: Registry adapters to try, in order, per jurisdiction (FR-241).  The first
#: entry resolves identity, the second holds the filings.
REGISTRIES_BY_JURISDICTION: dict[str, list[str]] = {
    "BE": ["registry.kbo", "registry.nbb"],
    "NL": ["registry.kvk"],
    "GB": ["registry.companies_house"],
    "UK": ["registry.companies_house"],
    "US": ["registry.sec_edgar"],
}
#: Tried for any jurisdiction that has no dedicated registry adapter.
FALLBACK_REGISTRIES = ["directory.opencorporates"]

# Bands used to turn a raw figure into a 0..1 sub-score.  They are the
# judgement in this module and are deliberately in one place.
PAY_BAND = (35_000.0, 110_000.0)        # personnel cost per FTE, EUR
SOLVENCY_BAND = (0.10, 0.45)            # equity / total assets
LIQUIDITY_BAND = (0.80, 2.00)           # current assets / current liabilities
MARGIN_BAND = (-0.05, 0.12)             # EBIT / revenue
REVENUE_GROWTH_BAND = (-0.05, 0.15)     # revenue CAGR
HEADCOUNT_GROWTH_BAND = (-0.05, 0.12)   # headcount CAGR
CASH_MONTHS_BAND = (1.0, 12.0)          # cash / monthly personnel cost
LEVERAGE_BAND = (3.0, 0.5)              # debt / equity, inverted: lower is better
CAPEX_BAND = (0.005, 0.08)              # capex / revenue

#: An estimated profile can never claim more than this (FR-245).
ESTIMATED_SCORE_CEILING = 55


# ---------------------------------------------------------------------------
# Metrics (FR-243)
# ---------------------------------------------------------------------------


@dataclass
class YearFigures:
    """One financial year, EUR-normalised (DR-103)."""

    fiscal_year: int
    revenue: float | None = None
    ebit: float | None = None
    ebitda: float | None = None
    net_result: float | None = None
    equity: float | None = None
    cash: float | None = None
    total_debt: float | None = None
    total_assets: float | None = None
    current_assets: float | None = None
    current_liabilities: float | None = None
    headcount_fte: float | None = None
    personnel_costs: float | None = None
    capex: float | None = None
    gross_margin: float | None = None
    currency: str = "EUR"
    is_estimated: bool = False
    flags: list[dict] = field(default_factory=list)

    @property
    def ebit_margin(self) -> float | None:
        if self.revenue and self.ebit is not None:
            return self.ebit / self.revenue
        return None

    @property
    def net_margin(self) -> float | None:
        if self.revenue and self.net_result is not None:
            return self.net_result / self.revenue
        return None

    @property
    def cost_per_fte(self) -> float | None:
        if self.personnel_costs and self.headcount_fte:
            return self.personnel_costs / self.headcount_fte
        return None


@dataclass
class Metrics:
    """The FR-243 picture of a company, plus the FR-244 scores."""

    company_id: str
    scope: str = "entity"                     # entity | consolidated (FR-246)
    years_covered: list[int] = field(default_factory=list)
    revenue_cagr: float | None = None
    headcount_cagr: float | None = None
    margin_trend: str | None = None
    margin_series: list[dict] = field(default_factory=list)
    personnel_cost_per_fte: float | None = None
    current_ratio: float | None = None
    solvency_ratio: float | None = None
    trajectory: str | None = None
    trajectory_confidence: float = 0.0
    ability_to_pay: int | None = None
    ability_to_pay_rationale: str | None = None
    investment_capacity: int | None = None
    investment_capacity_rationale: str | None = None
    is_estimated: bool = False
    basis: str = "filings"                    # filings | secondary_signals
    reconciliation_flags: list[dict] = field(default_factory=list)
    latest: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _band(value: float | None, low: float, high: float) -> float | None:
    """Map a figure onto 0..1 across a band; ``high`` below ``low`` inverts it."""
    if value is None:
        return None
    if high == low:
        return 0.5
    fraction = (value - low) / (high - low)
    return max(0.0, min(1.0, fraction))


def cagr(first: float | None, last: float | None, periods: int) -> float | None:
    """Compound annual growth.  Undefined when the base is not positive."""
    if first is None or last is None or periods <= 0 or first <= 0:
        return None
    if last <= 0:
        return -1.0
    return (last / first) ** (1.0 / periods) - 1.0


def to_year_figures(rows: list[dict]) -> list[YearFigures]:
    """``financial_year`` rows -> EUR-normalised figures, oldest first (DR-103)."""
    out: list[YearFigures] = []
    for row in sorted(rows, key=lambda r: int(r["fiscal_year"])):
        eur = fx.eur_values(row)
        # DR-103: a year that could not be priced in EUR is not a year of zeroes
        # and it is certainly not a year at parity - it is an estimated year
        # that says so, and the scores below read it that way (FR-245, NFR-404).
        unconverted = fx.unconverted_flag(row)
        figures = YearFigures(
            fiscal_year=int(row["fiscal_year"]),
            revenue=eur.get("revenue"),
            ebit=eur.get("ebit"),
            ebitda=eur.get("ebitda"),
            net_result=eur.get("net_result"),
            equity=eur.get("equity"),
            cash=eur.get("cash"),
            total_debt=eur.get("total_debt"),
            total_assets=eur.get("total_assets"),
            current_assets=eur.get("current_assets"),
            current_liabilities=eur.get("current_liabilities"),
            personnel_costs=eur.get("personnel_costs"),
            capex=eur.get("capex"),
            headcount_fte=(
                None if row.get("headcount_fte") in (None, "") else float(row["headcount_fte"])
            ),
            gross_margin=(
                None if row.get("gross_margin") in (None, "") else float(row["gross_margin"])
            ),
            currency=str(row.get("currency") or "EUR"),
            is_estimated=bool(row.get("is_estimated")) or unconverted is not None,
            flags=(from_json(row.get("reconciliation_flags"), []) or [])
            + ([unconverted] if unconverted else []),
        )
        out.append(figures)
    return out


def _series(years: list[YearFigures], attribute: str) -> list[tuple[int, float]]:
    out = []
    for year in years:
        value = getattr(year, attribute, None)
        if value is not None:
            out.append((year.fiscal_year, float(value)))
    return out


def _growth_rates(series: list[tuple[int, float]]) -> list[float]:
    rates = []
    for (_, previous), (_, current) in zip(series, series[1:], strict=False):
        if previous > 0:
            rates.append(current / previous - 1.0)
    return rates


def _driving_series(years: list[YearFigures]) -> tuple[list[tuple[int, float]], str]:
    """Revenue if it is disclosed, headcount for abbreviated accounts, else net result."""
    for attribute, basis in (
        ("revenue", "revenue"),
        ("headcount_fte", "headcount"),
        ("net_result", "net result"),
    ):
        series = _series(years, attribute)
        if len(series) >= 2:
            return series, basis
    return [], "no series"


def classify_trajectory(years: list[YearFigures]) -> tuple[str, float, str]:
    """FR-243: growing / stable / declining / volatile, with a confidence.

    Revenue drives the classification; headcount is the stand-in for the
    abbreviated Belgian accounts that never state turnover, and the net result
    is the last resort.  The basis is returned so the rationale can say which
    series it read.
    """
    series, basis = _driving_series(years)
    if len(series) < 2:
        return "unknown", 0.0, basis

    rates = _growth_rates(series)
    if not rates:
        return "unknown", 0.0, basis

    mean = statistics.fmean(rates)
    spread = statistics.pstdev(rates) if len(rates) > 1 else 0.0
    sign_changes = sum(1 for a, b in zip(rates, rates[1:], strict=False) if a * b < 0)

    if spread > 0.25 and sign_changes >= 1:
        label = "volatile"
    elif mean >= 0.05:
        label = "growing"
    elif mean <= -0.03:
        label = "declining"
    else:
        label = "stable"

    # Confidence is about the evidence, not about the verdict.
    confidence = {2: 0.40, 3: 0.60, 4: 0.75}.get(len(series), 0.85)
    if basis != "revenue":
        confidence *= 0.85
    if any(year.is_estimated for year in years):
        confidence *= 0.6
    errors = sum(
        1 for year in years for flag in year.flags if flag.get("severity") == "error"
    )
    confidence *= max(0.5, 1.0 - 0.15 * errors)
    return label, round(max(0.05, min(0.95, confidence)), 2), basis


def margin_trend(years: list[YearFigures]) -> tuple[str | None, list[dict]]:
    """FR-243: the direction of the operating margin, in words a reader can check."""
    series = [(y.fiscal_year, y.ebit_margin) for y in years if y.ebit_margin is not None]
    if len(series) < 2:
        series = [(y.fiscal_year, y.gross_margin) for y in years if y.gross_margin is not None]
        label_name = "gross margin"
    else:
        label_name = "EBIT margin"
    detail = [{"fiscal_year": y, "margin": round(m, 4)} for y, m in series]
    if len(series) < 2:
        return (None, detail)

    first_year, first = series[0]
    last_year, last = series[-1]
    change = last - first
    values = [m for _, m in series]
    spread = statistics.pstdev(values) if len(values) > 1 else 0.0
    if spread > 0.10:
        direction = "volatile"
    elif change >= 0.01:
        direction = "improving"
    elif change <= -0.01:
        direction = "deteriorating"
    else:
        direction = "stable"
    text = (
        f"{direction} ({label_name} {first * 100:.1f}% in {first_year} -> "
        f"{last * 100:.1f}% in {last_year})"
    )
    return text, detail


def compute_metrics(
    company_id: str, rows: list[dict], *, scope: str = "entity"
) -> Metrics:
    """The whole FR-243 computation over the stored years."""
    years = to_year_figures(rows)
    metrics = Metrics(company_id=company_id, scope=scope)
    if not years:
        metrics.trajectory = "unknown"
        metrics.is_estimated = True
        metrics.basis = "no filings"
        return metrics

    metrics.years_covered = [y.fiscal_year for y in years]
    metrics.is_estimated = any(y.is_estimated for y in years)
    metrics.reconciliation_flags = [
        dict(flag, fiscal_year=y.fiscal_year) for y in years for flag in y.flags
    ]

    revenue = _series(years, "revenue")
    if len(revenue) >= 2:
        metrics.revenue_cagr = cagr(revenue[0][1], revenue[-1][1], revenue[-1][0] - revenue[0][0])
    headcount = _series(years, "headcount_fte")
    if len(headcount) >= 2:
        metrics.headcount_cagr = cagr(
            headcount[0][1], headcount[-1][1], headcount[-1][0] - headcount[0][0]
        )

    metrics.margin_trend, metrics.margin_series = margin_trend(years)

    latest = years[-1]
    metrics.personnel_cost_per_fte = latest.cost_per_fte
    if metrics.personnel_cost_per_fte is None:
        for year in reversed(years[:-1]):
            if year.cost_per_fte is not None:
                metrics.personnel_cost_per_fte = year.cost_per_fte
                break
    if latest.current_assets is not None and latest.current_liabilities:
        metrics.current_ratio = latest.current_assets / latest.current_liabilities
    if latest.equity is not None and latest.total_assets:
        metrics.solvency_ratio = latest.equity / latest.total_assets

    metrics.trajectory, metrics.trajectory_confidence, basis = classify_trajectory(years)
    metrics.basis = f"filings ({basis})"
    metrics.latest = {
        "fiscal_year": latest.fiscal_year,
        "revenue": latest.revenue,
        "ebit": latest.ebit,
        "ebitda": latest.ebitda,
        "net_result": latest.net_result,
        "equity": latest.equity,
        "cash": latest.cash,
        "total_debt": latest.total_debt,
        "total_assets": latest.total_assets,
        "current_assets": latest.current_assets,
        "current_liabilities": latest.current_liabilities,
        "headcount_fte": latest.headcount_fte,
        "personnel_costs": latest.personnel_costs,
        "capex": latest.capex,
        "ebit_margin": latest.ebit_margin,
        "net_margin": latest.net_margin,
        "currency": "EUR",
    }
    score(metrics, years)
    return metrics


# ---------------------------------------------------------------------------
# The two scores (FR-244)
# ---------------------------------------------------------------------------


def _weighted(components: dict[str, tuple[float | None, float]]) -> tuple[int | None, dict]:
    """Weighted mean over the components that could be computed."""
    usable = {k: (v, w) for k, (v, w) in components.items() if v is not None}
    if not usable:
        return None, {}
    total_weight = sum(w for _, w in usable.values())
    value = sum(v * w for v, w in usable.values()) / total_weight
    detail = {k: round(v, 3) for k, (v, _) in usable.items()}
    # A score built on half the evidence is pulled towards the middle rather
    # than presented as if it were complete.
    coverage = total_weight / sum(w for _, w in components.values())
    value = value * coverage + 0.5 * (1 - coverage)
    return int(round(max(0.0, min(1.0, value)) * 100)), detail


def score(metrics: Metrics, years: list[YearFigures]) -> Metrics:
    """FR-244: ability to pay and investment capacity, on 0-100."""
    latest = years[-1] if years else YearFigures(fiscal_year=0)

    pay = {
        "pay_level": (_band(metrics.personnel_cost_per_fte, *PAY_BAND), 0.35),
        "profitability": (_band(latest.ebit_margin, *MARGIN_BAND), 0.25),
        "solvency": (_band(metrics.solvency_ratio, *SOLVENCY_BAND), 0.20),
        "liquidity": (_band(metrics.current_ratio, *LIQUIDITY_BAND), 0.20),
    }
    metrics.ability_to_pay, pay_detail = _weighted(pay)

    monthly_payroll = (
        latest.personnel_costs / 12.0 if latest.personnel_costs else None
    )
    cash_months = (
        latest.cash / monthly_payroll if latest.cash is not None and monthly_payroll else None
    )
    leverage = (
        latest.total_debt / latest.equity
        if latest.total_debt is not None and latest.equity and latest.equity > 0
        else None
    )
    capex_intensity = (
        latest.capex / latest.revenue
        if latest.capex is not None and latest.revenue
        else None
    )
    ebitda_margin = (
        latest.ebitda / latest.revenue
        if latest.ebitda is not None and latest.revenue
        else latest.ebit_margin
    )
    invest = {
        "revenue_growth": (_band(metrics.revenue_cagr, *REVENUE_GROWTH_BAND), 0.22),
        "headcount_growth": (_band(metrics.headcount_cagr, *HEADCOUNT_GROWTH_BAND), 0.18),
        "cash_runway": (_band(cash_months, *CASH_MONTHS_BAND), 0.22),
        "ebitda_margin": (_band(ebitda_margin, *MARGIN_BAND), 0.18),
        "leverage": (_band(leverage, *LEVERAGE_BAND), 0.12),
        "capex_intensity": (_band(capex_intensity, *CAPEX_BAND), 0.08),
    }
    metrics.investment_capacity, invest_detail = _weighted(invest)

    if metrics.trajectory == "declining":
        metrics.investment_capacity = _shade(metrics.investment_capacity, 0.85)
    elif metrics.trajectory == "volatile":
        metrics.investment_capacity = _shade(metrics.investment_capacity, 0.92)
    if metrics.is_estimated:
        metrics.ability_to_pay = _cap(metrics.ability_to_pay, ESTIMATED_SCORE_CEILING)
        metrics.investment_capacity = _cap(metrics.investment_capacity, ESTIMATED_SCORE_CEILING)

    metrics.evidence = {
        "ability_to_pay": pay_detail,
        "investment_capacity": invest_detail,
        "cash_months_of_payroll": None if cash_months is None else round(cash_months, 1),
        "debt_to_equity": None if leverage is None else round(leverage, 2),
        "ebitda_margin": None if ebitda_margin is None else round(ebitda_margin, 4),
        "capex_intensity": None if capex_intensity is None else round(capex_intensity, 4),
    }
    return metrics


def _shade(value: int | None, factor: float) -> int | None:
    return None if value is None else int(round(value * factor))


def _cap(value: int | None, ceiling: int) -> int | None:
    return None if value is None else min(value, ceiling)


# ---------------------------------------------------------------------------
# Rationales (FR-244): they must name the figures
# ---------------------------------------------------------------------------

_RATIONALE_SYSTEM = (
    "You explain a company's financial position to a job seeker deciding whether to "
    "apply there. You write two or three sentences per score, in plain professional "
    "English. Every sentence names the figures it rests on - the amount, the ratio, the "
    "year - taken verbatim from the data block. You never introduce a figure that is not "
    "in the data, never speculate about the future, and never give advice."
)

_RATIONALE_SCHEMA = (
    '{"ability_to_pay_rationale": "two or three sentences citing figures", '
    '"investment_capacity_rationale": "two or three sentences citing figures"}'
)

_NUMBER_IN_TEXT = re.compile(r"\d")


def _fmt_money(value: float | None) -> str | None:
    if value is None:
        return None
    for limit, suffix, divisor in ((1e9, "bn", 1e9), (1e6, "m", 1e6), (1e3, "k", 1e3)):
        if abs(value) >= limit:
            return f"EUR {value / divisor:,.1f}{suffix}"
    return f"EUR {value:,.0f}"


def _fmt_pct(value: float | None) -> str | None:
    return None if value is None else f"{value * 100:.1f}%"


def cited_figures(metrics: Metrics) -> dict[str, Any]:
    """The figures a rationale is allowed to cite, formatted as they should appear."""
    latest = metrics.latest
    return {
        "years_covered": metrics.years_covered,
        "latest_fiscal_year": latest.get("fiscal_year"),
        "revenue": _fmt_money(latest.get("revenue")),
        "ebit": _fmt_money(latest.get("ebit")),
        "ebitda": _fmt_money(latest.get("ebitda")),
        "net_result": _fmt_money(latest.get("net_result")),
        "equity": _fmt_money(latest.get("equity")),
        "cash": _fmt_money(latest.get("cash")),
        "total_debt": _fmt_money(latest.get("total_debt")),
        "total_assets": _fmt_money(latest.get("total_assets")),
        "headcount_fte": latest.get("headcount_fte"),
        "personnel_costs": _fmt_money(latest.get("personnel_costs")),
        "personnel_cost_per_fte": _fmt_money(metrics.personnel_cost_per_fte),
        "ebit_margin": _fmt_pct(latest.get("ebit_margin")),
        "revenue_cagr": _fmt_pct(metrics.revenue_cagr),
        "headcount_cagr": _fmt_pct(metrics.headcount_cagr),
        "current_ratio": None if metrics.current_ratio is None else round(metrics.current_ratio, 2),
        "solvency_ratio": _fmt_pct(metrics.solvency_ratio),
        "margin_trend": metrics.margin_trend,
        "trajectory": metrics.trajectory,
        "trajectory_confidence": metrics.trajectory_confidence,
        "ability_to_pay": metrics.ability_to_pay,
        "investment_capacity": metrics.investment_capacity,
        "cash_months_of_payroll": metrics.evidence.get("cash_months_of_payroll"),
        "debt_to_equity": metrics.evidence.get("debt_to_equity"),
        "is_estimated": metrics.is_estimated,
        "basis": metrics.basis,
    }


def cites_figures(text: str | None, figures: dict[str, Any]) -> bool:
    """FR-244: a rationale without numbers in it is not a rationale."""
    if not text or not _NUMBER_IN_TEXT.search(text):
        return False
    digits = len(re.findall(r"\d[\d.,]*", text))
    return digits >= 2


def fallback_rationales(metrics: Metrics, company_name: str) -> tuple[str, str]:
    """Deterministic rationales, used when the model is unavailable or vague.

    They cite the same figures the scores were computed from, which is the
    whole requirement; they simply read less well than the model's version.
    """
    figures = cited_figures(metrics)
    years = (
        f"{metrics.years_covered[0]}-{metrics.years_covered[-1]}"
        if metrics.years_covered
        else "no filed years"
    )

    pay_parts = [
        f"Ability to pay {metrics.ability_to_pay if metrics.ability_to_pay is not None else 'n/a'}"
        f"/100 for {company_name}, from the {years} filings."
    ]
    if figures["personnel_cost_per_fte"] and figures["headcount_fte"]:
        pay_parts.append(
            f"Personnel costs of {figures['personnel_costs']} over "
            f"{figures['headcount_fte']:.0f} FTE give {figures['personnel_cost_per_fte']} per FTE "
            f"in {figures['latest_fiscal_year']}."
        )
    if figures["ebit_margin"] and figures["revenue"]:
        pay_parts.append(
            f"Turnover {figures['revenue']} carried an operating margin of "
            f"{figures['ebit_margin']}."
        )
    if figures["solvency_ratio"] and figures["equity"]:
        pay_parts.append(
            f"Equity {figures['equity']} against total assets {figures['total_assets']} is a "
            f"solvency ratio of {figures['solvency_ratio']}."
        )
    if figures["current_ratio"] is not None:
        pay_parts.append(f"The current ratio stands at {figures['current_ratio']}.")

    invest_parts = [
        f"Investment capacity "
        f"{metrics.investment_capacity if metrics.investment_capacity is not None else 'n/a'}/100."
    ]
    if figures["revenue_cagr"]:
        invest_parts.append(f"Revenue compounded at {figures['revenue_cagr']} a year over {years}.")
    if figures["headcount_cagr"]:
        invest_parts.append(f"Headcount compounded at {figures['headcount_cagr']}.")
    if figures["cash"]:
        months = figures["cash_months_of_payroll"]
        invest_parts.append(
            f"Cash of {figures['cash']}"
            + (f" covers about {months} months of payroll." if months else ".")
        )
    if figures["margin_trend"]:
        invest_parts.append(f"The margin is {figures['margin_trend']}.")
    invest_parts.append(
        f"Trajectory read as {metrics.trajectory} at confidence "
        f"{metrics.trajectory_confidence:.2f}."
    )
    if metrics.is_estimated:
        for parts in (pay_parts, invest_parts):
            parts.append("These figures are estimated: the filings are incomplete (FR-245).")
    return " ".join(pay_parts), " ".join(invest_parts)


def write_rationales(
    metrics: Metrics, company_name: str, llm: LLMClient | None
) -> Metrics:
    """Ask the model for the two rationales, and hold it to citing the figures."""
    fallback_pay, fallback_invest = fallback_rationales(metrics, company_name)
    metrics.ability_to_pay_rationale = fallback_pay
    metrics.investment_capacity_rationale = fallback_invest
    if llm is None:
        return metrics
    try:
        if llm.budget.should_degrade():
            log.info("Token budget nearly exhausted; keeping the computed rationales (NFR-104)")
            return metrics
    except Exception:  # noqa: BLE001 - a budget lookup failure must not lose the analysis
        pass

    figures = cited_figures(metrics)
    instruction = (
        f"Write the two rationales for {company_name}. Score for ability to pay: "
        f"{metrics.ability_to_pay}/100. Score for investment capacity: "
        f"{metrics.investment_capacity}/100. Use only the figures in the data block, and "
        "name them explicitly - amounts, percentages, ratios and the years they belong to. "
        "If a figure is null, say the filing does not disclose it rather than estimating."
    )
    for attempt in range(2):
        if attempt:
            instruction += (
                "\nThe previous answer named no figures. Every sentence must contain a "
                "number taken from the data block."
            )
        try:
            payload = llm.complete_json(
                "analysis.financial",
                system=_RATIONALE_SYSTEM,
                user=instruction,
                untrusted={"figures": json.dumps(figures, default=str, indent=1)},
                schema_hint=_RATIONALE_SCHEMA,
                entity_type="financial_analysis",
                entity_id=metrics.company_id,
                max_tokens=1400,   # two short paragraphs, plus the reasoner's overhead
            )
        except (LLMError, BudgetExhausted) as exc:
            log.info("Financial rationale unavailable (%s); using the computed one", exc)
            return metrics
        if not isinstance(payload, dict):
            continue
        pay = str(payload.get("ability_to_pay_rationale") or "").strip()
        invest = str(payload.get("investment_capacity_rationale") or "").strip()
        if cites_figures(pay, figures) and cites_figures(invest, figures):
            metrics.ability_to_pay_rationale = pay
            metrics.investment_capacity_rationale = invest
            return metrics
        log.info("Rejected a financial rationale that named no figures (FR-244)")
    return metrics


# ---------------------------------------------------------------------------
# FR-245: secondary signals when the filings are not there
# ---------------------------------------------------------------------------

_AMOUNT_IN_TEXT = re.compile(
    r"(?:EUR|USD|GBP|€|\$|£)\s?([\d.,]+)\s?(k|m|bn|million|billion)?", re.IGNORECASE
)
_SCALE = {"k": 1e3, "m": 1e6, "bn": 1e9, "million": 1e6, "billion": 1e9}

#: Signals that say something about money or about hiring capacity.
_POSITIVE_SIGNALS = {
    "funding": 0.35,
    "headcount_growth": 0.25,
    "new_office": 0.15,
    "product_launch": 0.10,
    "postings": 0.15,
}
_NEGATIVE_SIGNALS = {"reorg": -0.20, "competitor_layoff": -0.05, "leadership_change": -0.05}


def funding_amount(description: str | None) -> float | None:
    """The headline amount of a funding signal, when the description states one."""
    match = _AMOUNT_IN_TEXT.search(description or "")
    if not match:
        return None
    value = fx.parse_amount(match.group(1))
    if value is None:
        return None
    return value * _SCALE.get((match.group(2) or "").lower(), 1.0)


def estimate_from_signals(company_id: str, company: dict | None = None) -> Metrics:
    """FR-245: a conservative, clearly-estimated profile from secondary signals."""
    company = company or repo.company(company_id) or {}
    signals = repo.hiring_signals(company_id)
    metrics = Metrics(
        company_id=company_id,
        is_estimated=True,
        basis="secondary_signals",
        trajectory="unknown",
    )

    weight = 0.0
    funding_total = 0.0
    counted: dict[str, int] = {}
    for signal in signals:
        kind = str(signal.get("signal_type") or "")
        counted[kind] = counted.get(kind, 0) + 1
        strength = float(signal.get("strength") or 0.5)
        weight += _POSITIVE_SIGNALS.get(kind, 0.0) * strength
        weight += _NEGATIVE_SIGNALS.get(kind, 0.0) * strength
        if kind == "funding":
            funding_total += funding_amount(signal.get("description")) or 0.0

    size_fte = company.get("size_fte")
    size_fte = float(size_fte) if size_fte not in (None, "") else None
    stage = str(company.get("stage") or "")

    # A larger, established company can pay; a funded scale-up can invest.
    pay_base = 0.30
    if size_fte:
        pay_base += min(0.25, math.log10(max(size_fte, 1)) / 12.0)
    if stage in ("established", "listed", "public"):
        pay_base += 0.10
    invest_base = 0.30 + max(-0.20, min(0.35, weight))
    if funding_total > 0:
        invest_base += min(0.20, math.log10(max(funding_total, 10)) / 40.0)

    metrics.ability_to_pay = _cap(
        int(round(max(0.0, min(1.0, pay_base)) * 100)), ESTIMATED_SCORE_CEILING
    )
    metrics.investment_capacity = _cap(
        int(round(max(0.0, min(1.0, invest_base)) * 100)), ESTIMATED_SCORE_CEILING
    )
    if counted.get("headcount_growth") or counted.get("funding"):
        metrics.trajectory = "growing"
        metrics.trajectory_confidence = 0.25
    metrics.latest = {
        "headcount_fte": size_fte,
        "signals": counted,
        "funding_total_eur": funding_total or None,
        "currency": "EUR",
    }
    metrics.evidence = {
        "signal_counts": counted,
        "signal_weight": round(weight, 3),
        "funding_total_eur": funding_total or None,
        "size_fte": size_fte,
        "stage": stage or None,
    }
    metrics.reconciliation_flags = [
        {
            "code": "estimated_from_signals",
            "detail": (
                f"No usable filings; the profile rests on {len(signals)} secondary signals "
                f"and a reported headcount of {size_fte if size_fte else 'unknown'} FTE"
            ),
            "severity": "info",
        }
    ]
    return metrics


def signal_rationales(metrics: Metrics, company_name: str) -> tuple[str, str]:
    """FR-244 + FR-245: an estimated rationale still names what it counted."""
    evidence = metrics.evidence
    counts = evidence.get("signal_counts") or {}
    listed = (
        ", ".join(f"{count} x {kind}" for kind, count in sorted(counts.items())) or "no signals"
    )
    size = evidence.get("size_fte")
    funding = evidence.get("funding_total_eur")
    pay = (
        f"Ability to pay {metrics.ability_to_pay}/100 for {company_name} is estimated: no "
        "filings could be retrieved. "
    )
    pay += (
        f"It rests on a reported headcount of {size:.0f} FTE and on "
        if size
        else "No headcount is on record; it rests on "
    )
    pay += f"{listed} in the collected signals ({len(counts)} signal types). Indicative only."
    invest = (
        f"Investment capacity {metrics.investment_capacity}/100, also estimated, from "
        f"{listed}"
        + (f" and disclosed funding of about EUR {funding:,.0f}" if funding else "")
        + f". Signal weight {evidence.get('signal_weight')}; no filed figures were available."
    )
    return pay, invest


# ---------------------------------------------------------------------------
# FR-246: groups
# ---------------------------------------------------------------------------


def record_group_links(company_id: str, links: list[Any], source: str) -> int:
    """Store the parent/subsidiary relations a registry exposed (FR-246)."""
    written = 0
    for link in links:
        name = getattr(link, "name", None) or (link.get("name") if isinstance(link, dict) else None)
        if not name:
            continue
        legal_id = getattr(link, "legal_id", None) or (
            link.get("legal_id") if isinstance(link, dict) else None
        )
        # A link that names no relation means the plain one: a dict from a
        # registry that only knows "this company is below that one".
        relation = str(
            getattr(link, "relation", None)
            or (link.get("relation") if isinstance(link, dict) else None)
            or "subsidiary"
        )
        ownership = relation in repo.GROUP_RELATIONS or relation in ("parent", "group")
        resolved = None
        if legal_id:
            found = kb_repo.company_by_legal_id(str(normalise_legal_id(str(legal_id)) or legal_id))
            resolved = found["id"] if found else None
        if resolved is None and ownership:
            # A normalised-name hit is good enough to place a named subsidiary,
            # but not to place an alias: "Acme Inc" and "Acme Holdings" fold to
            # the same key, and folding a former name onto a real company is how
            # a one-company group acquires a second set of accounts (FR-246).
            candidates = kb_repo.companies_by_normalised_name(normalise_company_name(str(name)))
            resolved = candidates[0]["id"] if candidates else None

        parent_id, child_id = company_id, resolved
        key = str(legal_id or normalise_company_name(str(name)) or name)
        if relation in ("parent", "group"):
            # The registry named the company's owner, so the edge points the other way.
            if resolved is None:
                continue
            parent_id, child_id = resolved, company_id
            key = company_id
        repo.link_subsidiary(
            parent_id,
            subsidiary_company_id=child_id,
            subsidiary_key=key,
            subsidiary_name=str(name),
            subsidiary_legal_id=str(legal_id) if legal_id else None,
            relation="subsidiary" if relation in ("parent", "group") else str(relation),
            source=source,
        )
        written += 1
    return written


def consolidated_rows(parent_company_id: str, years: int = DEFAULT_YEARS) -> list[dict]:
    """Group figures per year: the parent plus every resolved subsidiary, in EUR."""
    rows = repo.group_financial_years(parent_company_id, limit=years)
    if not rows:
        return []
    summed: dict[int, dict[str, Any]] = {}
    for row in rows:
        year = int(row["fiscal_year"])
        eur = fx.eur_values(row)
        bucket = summed.setdefault(
            year,
            {
                "company_id": parent_company_id,
                "fiscal_year": year,
                "currency": "EUR",
                "fx_rate_to_eur": 1.0,
                "is_estimated": 0,
                "reconciliation_flags": [],
                "entities": 0,
            },
        )
        bucket["entities"] += 1
        for name, value in eur.items():
            if value is None:
                continue
            bucket[name] = (bucket.get(name) or 0.0) + value
        if row.get("headcount_fte") not in (None, ""):
            bucket["headcount_fte"] = (bucket.get("headcount_fte") or 0.0) + float(
                row["headcount_fte"]
            )
        if row.get("is_estimated"):
            bucket["is_estimated"] = 1
    if not any(bucket["entities"] > 1 for bucket in summed.values()):
        # Only the parent filed: there is nothing to consolidate, and showing an
        # identical second picture would imply a group view we do not have.
        return []
    for year, bucket in summed.items():
        bucket["reconciliation_flags"] = [
            {
                "code": "consolidated_sum",
                "detail": f"{bucket['entities']} group entities summed for {year} (FR-246)",
                "severity": "info",
            }
        ]
    return list(summed.values())


# ---------------------------------------------------------------------------
# The public entry point
# ---------------------------------------------------------------------------


def analyse_company(
    company_id: str,
    *,
    llm: LLMClient | None = None,
    years: int = DEFAULT_YEARS,
    persist: bool = True,
) -> dict[str, Any]:
    """FR-243/244/245/246 for one company; writes one ``financial_analysis`` row."""
    company = repo.company(company_id) or {}
    company_name = str(company.get("name") or "this company")
    rows = repo.financial_years(company_id, limit=years)

    if rows:
        metrics = compute_metrics(company_id, rows)
        metrics = write_rationales(metrics, company_name, llm)
    else:
        metrics = estimate_from_signals(company_id, company)
        pay, invest = signal_rationales(metrics, company_name)
        metrics.ability_to_pay_rationale = pay
        metrics.investment_capacity_rationale = invest

    consolidated: Metrics | None = None
    group = repo.subsidiaries(company_id)
    if group:
        group_rows = consolidated_rows(company_id, years)
        if group_rows:
            consolidated = compute_metrics(company_id, group_rows, scope="consolidated")

    if persist:
        repo.upsert_analysis(
            {
                "company_id": company_id,
                "years_covered": metrics.years_covered,
                "revenue_cagr": metrics.revenue_cagr,
                "headcount_cagr": metrics.headcount_cagr,
                "margin_trend": metrics.margin_trend,
                "personnel_cost_per_fte": metrics.personnel_cost_per_fte,
                "current_ratio": metrics.current_ratio,
                "solvency_ratio": metrics.solvency_ratio,
                "trajectory": metrics.trajectory,
                "trajectory_confidence": metrics.trajectory_confidence,
                "ability_to_pay": metrics.ability_to_pay,
                "ability_to_pay_rationale": metrics.ability_to_pay_rationale,
                "investment_capacity": metrics.investment_capacity,
                "investment_capacity_rationale": metrics.investment_capacity_rationale,
                "is_estimated": 1 if metrics.is_estimated else 0,
                "computed_at": utcnow(),
            }
        )
        # FR-343: the company's own trajectory column is what the browse screens read.
        if metrics.trajectory and metrics.trajectory != "unknown":
            kb_repo.update_company(company_id, {"trajectory": metrics.trajectory})

    result = {"entity": metrics.to_dict()}
    if consolidated is not None:
        result["consolidated"] = consolidated.to_dict()
        result["group_size"] = len(group)
    return result


# ---------------------------------------------------------------------------
# Collection (FR-241): registry selection and writing
# ---------------------------------------------------------------------------


def registries_for(company: dict) -> list[str]:
    """The registry adapters that can answer for this company's jurisdiction."""
    code = str(company.get("jurisdiction") or company.get("country") or "").upper()[:2]
    keys = list(REGISTRIES_BY_JURISDICTION.get(code, []))
    keys.extend(k for k in FALLBACK_REGISTRIES if k not in keys)
    return keys


def _store_identity(
    writer: Any, company_id: str | None, identity: dict, adapter_key: str
) -> str | None:
    """Enrich the row we already have, or let the de-duplicator resolve a new one.

    An adapter never chooses a primary key (DR-101, FR-184): when the caller
    already knows which company it is asking about, the registry's answer
    enriches that row; otherwise the knowledge-base writer resolves it on the
    legal identifier the registry just supplied.
    """
    if not company_id:
        outcome = writer.write(
            {"entity_type": "company", "data": identity, "confidence": 0.95}
        )
        return outcome.entity_id if outcome else None

    update = {k: v for k, v in identity.items() if v not in (None, "", [], {})}
    update.pop("id", None)
    update["refreshed_at"] = utcnow()
    kb_repo.update_company(company_id, update)
    kb_repo.record_provenance(
        "company", company_id, adapter_key=adapter_key, confidence=0.95
    )
    return company_id


async def collect_company_financials(
    company: dict,
    *,
    egress: Any = None,
    llm: LLMClient | None = None,
    years: int = DEFAULT_YEARS,
    campaign_id: str | None = None,
    plan_item_id: str | None = None,
) -> dict[str, Any]:
    """Ask the right registries for one company and store what they return.

    Every registry is optional: a missing key, an outage or a company that has
    never filed all end in the same place - what was learned is written, the
    rest is noted, and the profile is marked estimated (FR-245, RK-06).
    """
    from dreamjob.adapters.base import get_adapter  # noqa: PLC0415 - avoids a cycle
    from dreamjob.pipeline.knowledge_base import KnowledgeBaseWriter  # noqa: PLC0415

    if egress is None:
        # Without a client every registry call raised AttributeError inside its
        # own outage handler, and a company that is in the register came back as
        # "not found in the register" - a false negative, reported as a fact
        # (IR-102).  The client is opened here rather than assumed.
        from dreamjob.egress.client import EgressClient  # noqa: PLC0415 - avoids a cycle

        async with EgressClient() as client:
            return await collect_company_financials(
                company,
                egress=client,
                llm=llm,
                years=years,
                campaign_id=campaign_id,
                plan_item_id=plan_item_id,
            )

    company_id = company.get("id")
    report: dict[str, Any] = {
        "company_id": company_id,
        "years_written": 0,
        "identity_updated": False,
        "estimated": False,
        "notes": [],
        "adapters": [],
        "degraded": [],
    }
    #: Whether the registries that actually produced figures were degraded.
    estimated_sources: list[bool] = []

    for key in registries_for(company):
        try:
            adapter = get_adapter(key, egress)
        except KeyError:
            continue
        if not adapter.available():
            report["degraded"].append(key)
            report["notes"].append(adapter.unavailable_reason())
            continue

        try:
            result = await adapter.collect(company, years=years, egress=egress, llm=llm)
        except Exception as exc:  # noqa: BLE001 - one registry must not stop the rest
            log.exception("[%s] collection failed for %s", key, company.get("name"))
            report["notes"].append(f"{key}: {exc}")
            continue

        report["adapters"].append(key)
        report["notes"].extend(result.notes)
        if result.facts:
            estimated_sources.append(result.estimated)
        elif result.estimated:
            report["degraded"].append(key)

        writer = KnowledgeBaseWriter(
            adapter_key=key, plan_item_id=plan_item_id, campaign_id=campaign_id
        )
        if result.identity:
            resolved = _store_identity(writer, company_id, result.identity, key)
            if resolved:
                company_id = resolved
                company = {**company, "id": company_id}
                report["identity_updated"] = True

        if company_id:
            priced = await _price_in_eur(result.facts, egress)
            for entry in priced:
                row = fx.to_financial_year_row(
                    str(company_id),
                    entry["facts"],
                    fx_rate_to_eur=entry["fx_rate_to_eur"],
                    fx_date=entry["fx_date"],
                    source=key,
                )
                if result.estimated:
                    row["is_estimated"] = 1
                if writer.write(
                    {
                        "entity_type": "financial_year",
                        "data": row,
                        "confidence": 0.9,
                        "raw_document_id": entry["facts"].filing_document_id,
                    }
                ):
                    report["years_written"] += 1
            if result.subsidiaries:
                record_group_links(str(company_id), result.subsidiaries, key)

    # A key we do not have somewhere else in the list is not a reason to call a
    # complete set of filings "estimated" (FR-245 is about the figures, not the
    # sources that were skipped).
    report["company_id"] = company_id
    report["estimated"] = report["years_written"] == 0 or any(estimated_sources)
    return report


async def _price_in_eur(facts: list[fx.FilingFacts], egress: Any) -> list[dict[str, Any]]:
    from dreamjob.adapters.registries.common import attach_fx  # noqa: PLC0415 - avoids a cycle

    return await attach_fx(facts, egress=egress)


async def analyse_companies(
    company_ids: list[str],
    *,
    egress: Any = None,
    llm: LLMClient | None = None,
    years: int = DEFAULT_YEARS,
    collect: bool = True,
) -> list[dict[str, Any]]:
    """Collect and analyse a batch; used by the financial job worker (FR-185)."""
    out: list[dict[str, Any]] = []
    for company_id in company_ids:
        company = repo.company(company_id)
        if not company:
            continue
        if collect:
            await collect_company_financials(
                company, egress=egress, llm=llm, years=years
            )
        out.append(analyse_company(company_id, llm=llm, years=years))
    return out


# ---------------------------------------------------------------------------
# Stage re-run (NFR-603)
# ---------------------------------------------------------------------------


def campaign_companies(campaign_id: str, limit: int = 100) -> list[str]:
    """The companies a financial stage should work on, in preference order.

    The campaign's own provenance comes first (FR-166).  It is empty on any
    campaign whose collection wrote nothing, so the knowledge base is asked as
    well for the companies whose filings are still missing - otherwise a
    company that was collected without provenance is never analysed at all,
    which is how ``financial_year`` stayed empty (FR-241, FR-342).
    """
    from dreamjob.db.repositories import opportunities as opp_repo  # noqa: PLC0415 - avoids a cycle

    out: list[str] = []
    try:
        out.extend(opp_repo.campaign_company_ids(campaign_id))
    except Exception:  # noqa: BLE001 - provenance is an optimisation, not a gate
        log.exception("Could not read the campaign's company provenance")
    if len(out) >= limit:
        return out[:limit]
    seen = set(out)
    for row in repo.companies_missing_filings(min_years=DEFAULT_YEARS, limit=limit - len(out)):
        if row["id"] not in seen:
            seen.add(row["id"])
            out.append(str(row["id"]))
    return out[:limit]


async def rerun(campaign_id: str, job_seeker_id: str, **options: Any) -> dict[str, Any]:
    """Collect and analyse the campaign's companies (FR-241..245, NFR-603).

    ``dreamjob.pipeline.collection`` reserves the ``financials`` stage for this
    module.  Re-reading what is already stored is free, so it always happens;
    fetching is what costs, so ``collect`` (default ``True``) is what decides
    whether the registries are asked for the years that are still missing.  A
    campaign whose collection produced no provenance still gets its companies
    analysed - see :func:`campaign_companies`.
    """
    # A stage re-run is interactive: bound it, and let the background worker
    # (JOB_KIND) be the one that walks a hundred companies (FR-185).
    company_ids = campaign_companies(campaign_id, int(options.get("limit") or 25))
    if not company_ids:
        return {
            "companies": 0,
            "analysed": 0,
            "note": "No company is in scope yet; run company discovery before the registries",
        }

    llm = LLMClient(campaign_id=campaign_id, job_seeker_id=job_seeker_id)
    years = int(options.get("years") or DEFAULT_YEARS)
    collect = bool(options.get("collect", True))
    analysed = 0
    estimated = 0
    collected = 0
    egress_cm = _egress_if(collect)
    async with egress_cm as egress:
        for company_id in company_ids:
            try:
                if collect:
                    company = repo.company(company_id)
                    if company:
                        report = await collect_company_financials(
                            company,
                            egress=egress,
                            llm=llm,
                            years=years,
                            campaign_id=campaign_id,
                        )
                        collected += int(report.get("years_written") or 0)
                result = analyse_company(company_id, llm=llm, years=years)
            except Exception:  # noqa: BLE001 - one company must not stop the stage
                log.exception("Financial re-analysis failed for %s", company_id)
                continue
            analysed += 1
            estimated += 1 if result["entity"].get("is_estimated") else 0
    return {
        "companies": len(company_ids),
        "analysed": analysed,
        "estimated": estimated,
        "years_collected": collected,
        "years": years,
    }


@asynccontextmanager
async def _egress_if(wanted: bool) -> AsyncIterator[Any]:
    """An egress client only when the stage is going to fetch something."""
    if not wanted:
        yield None
        return
    from dreamjob.egress.client import EgressClient  # noqa: PLC0415 - avoids a cycle

    async with EgressClient() as client:
        yield client


# ---------------------------------------------------------------------------
# Background execution (FR-185, NFR-401)
# ---------------------------------------------------------------------------


async def financial_worker(ctx: Any) -> None:
    """Job worker for ``job_run.kind = 'financial'``.

    The company list and the position within it live in the job checkpoint, so
    a run interrupted by a restart resumes at the next company rather than
    re-collecting the filings it already has (NFR-401).
    """
    from dreamjob.egress.client import EgressClient  # noqa: PLC0415 - avoids a cycle

    company_ids: list[str] = list(ctx.checkpoint.get("company_ids") or [])
    if not company_ids:
        company_ids = [
            row["id"] for row in repo.companies_missing_filings(min_years=DEFAULT_YEARS, limit=100)
        ]
        ctx.save_checkpoint(company_ids=company_ids, done=0)
    done = int(ctx.checkpoint.get("done") or 0)
    ctx.progress(done, len(company_ids))

    llm = LLMClient(campaign_id=ctx.campaign_id, job_seeker_id=ctx.job_seeker_id)
    async with EgressClient() as egress:
        for index in range(done, len(company_ids)):
            await ctx.checkpoint_barrier()
            company_id = company_ids[index]
            company = repo.company(company_id)
            if company:
                try:
                    await collect_company_financials(company, egress=egress, llm=llm)
                    analyse_company(company_id, llm=llm)
                except Exception as exc:  # noqa: BLE001 - one company must not stop the job
                    log.exception("Financial analysis failed for %s", company_id)
                    ctx.record_error(f"{company_id}: {exc}")
            ctx.save_checkpoint(done=index + 1)
            ctx.progress(index + 1, len(company_ids))


def register_financial_worker() -> None:
    """Wire the worker into the job runner (FR-185).

    Called at import time, the way ``dreamjob.pipeline.collection`` registers
    its own worker: a worker nothing registers is a job kind that can never be
    started, which is what left ``financial_year`` empty.
    """
    from dreamjob.jobs.runner import runner  # noqa: PLC0415 - avoids a cycle

    runner.register_worker(JOB_KIND, financial_worker)


async def launch(job_seeker_id: str, campaign_id: str | None = None) -> str:
    """Start a financial collection job in the background.  Returns the job id."""
    from dreamjob.jobs.runner import runner  # noqa: PLC0415 - avoids a cycle

    register_financial_worker()
    job_id = runner.create(JOB_KIND, job_seeker_id=job_seeker_id, campaign_id=campaign_id)
    await runner.start(job_id)
    return job_id


register_financial_worker()
