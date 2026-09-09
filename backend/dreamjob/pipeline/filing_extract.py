"""Annual-account extraction (FR-242, DR-103, NFR-404, RK-06).

One filing goes in - XBRL, inline XBRL, the SEC companyfacts JSON, a CSV
export or a scanned-in-spirit PDF - and one :class:`FilingFacts` per financial
year comes out, in the filing's own currency, with the reporting standard and
the period end date attached (DR-103).

The order of preference is deliberate and is what RK-06 asks for:

1. **Structured first.**  XBRL and companyfacts are unambiguous, so a
   concept-to-field map does the whole job with no model in the loop.
2. **Deterministic text second.**  Belgian annual accounts are laid out by
   statutory code (70 turnover, 9901 operating result, 10/15 equity ...), and
   those codes survive a PDF far better than the labels around them.
3. **LLM last** (task ``extract.table``), only for the figures still missing
   after the first two passes, with the filing text passed as untrusted data
   (NFR-205) and every returned number re-checked by the reconciler.

Nothing here guesses.  A figure that is not in the filing stays ``None``; an
abbreviated Belgian account that omits turnover produces facts marked
``is_estimated`` with a ``turnover_not_disclosed`` flag rather than a number
somebody invented (FR-245).  NFR-404's reconciliation - balance-sheet totals,
sign conventions, derived-value consistency - runs on every result and writes
what it found into ``financial_year.reconciliation_flags``.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from dreamjob.config import get_settings
from dreamjob.db.repositories import financials as repo
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError

log = logging.getLogger(__name__)

MAX_PDF_PAGES = 40
MAX_LLM_CHARS = 30_000

#: Monetary fields, in the order a reader of an annual account meets them.
MONEY_FIELDS = (
    "revenue", "gross_profit", "ebit", "ebitda", "net_result", "equity", "cash",
    "total_debt", "total_assets", "current_assets", "current_liabilities",
    "personnel_costs", "capex", "depreciation",
)


# ---------------------------------------------------------------------------
# The extraction result
# ---------------------------------------------------------------------------


@dataclass
class FilingFacts:
    """One financial year of one entity, in the filing's own currency (DR-103)."""

    fiscal_year: int
    period_end: str | None = None
    currency: str = "EUR"
    reporting_standard: str | None = None

    revenue: float | None = None
    gross_profit: float | None = None
    gross_margin: float | None = None          # ratio, gross_profit / revenue
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
    depreciation: float | None = None

    is_estimated: bool = False
    reconciliation_flags: list[dict[str, Any]] = field(default_factory=list)
    source: str | None = None
    filing_document_id: str | None = None

    def known_fields(self) -> list[str]:
        return [f for f in MONEY_FIELDS if getattr(self, f) is not None]

    def flag(self, code: str, detail: str, severity: str = "warning", **extra: Any) -> None:
        """NFR-404: record an inconsistency instead of silently accepting it."""
        entry = {"code": code, "detail": detail, "severity": severity}
        entry.update(extra)
        self.reconciliation_flags.append(entry)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Number parsing
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"\(?-?\s*\d[\d ., ']*\)?-?")
_TRAILING_MINUS = re.compile(r"^(?P<body>[^-]+)-$")


def parse_amount(text: str | None, scale: float = 1.0) -> float | None:
    """Read a filing number: ``1.234.567,89``, ``1,234,567.89``, ``(1 234)``, ``123-``.

    Continental and Anglo-Saxon separators, brackets and trailing minus signs
    all mean the same three things, and every registry uses a different two of
    them.
    """
    if text is None:
        return None
    raw = str(text).strip().replace(" ", " ").replace("'", "")
    if not raw:
        return None
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative, raw = True, raw[1:-1].strip()
    trailing = _TRAILING_MINUS.match(raw)
    if trailing:
        negative, raw = True, trailing.group("body").strip()
    if raw.startswith("-"):
        negative, raw = True, raw[1:].strip()
    raw = raw.replace(" ", "")
    if not raw or not any(c.isdigit() for c in raw):
        return None

    last_dot, last_comma = raw.rfind("."), raw.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        # Whichever separator comes last is the decimal point.
        if last_comma > last_dot:
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif last_comma >= 0:
        decimals = len(raw) - last_comma - 1
        raw = raw.replace(",", "." if decimals in (1, 2) else "")
    elif last_dot >= 0:
        decimals = len(raw) - last_dot - 1
        if decimals == 3 and raw.count(".") >= 1 and len(raw.split(".")[0]) <= 3:
            raw = raw.replace(".", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    return -value * scale if negative else value * scale


def _year_of(period_end: str | None) -> int | None:
    if not period_end:
        return None
    match = re.match(r"(\d{4})", str(period_end))
    return int(match.group(1)) if match else None


def _fiscal_year(period_end: str | None) -> int | None:
    """A book year ending in the first quarter belongs to the previous year."""
    if not period_end:
        return None
    try:
        parsed = date.fromisoformat(str(period_end)[:10])
    except ValueError:
        return _year_of(period_end)
    return parsed.year - 1 if parsed.month <= 3 else parsed.year


# ---------------------------------------------------------------------------
# Concept maps (XBRL / iXBRL / companyfacts)
# ---------------------------------------------------------------------------

#: Local concept name (lower case) -> FilingFacts field.  Covers us-gaap,
#: ifrs-full and the Belgian pfs taxonomy, whose element names are English.
CONCEPT_MAP: dict[str, str] = {
    # revenue
    "revenues": "revenue",
    "revenue": "revenue",
    "revenuefromcontractwithcustomerexcludingassessedtax": "revenue",
    "revenuefromcontractwithcustomerincludingassessedtax": "revenue",
    "salesrevenuenet": "revenue",
    "salesrevenuegoodsnet": "revenue",
    "turnover": "revenue",
    "revenuefromsaleofgoods": "revenue",
    "turnovernetamount": "revenue",
    # gross profit
    "grossprofit": "gross_profit",
    "grossmargin": "gross_profit",
    # operating result
    "operatingincomeloss": "ebit",
    "profitlossfromoperatingactivities": "ebit",
    "operatingprofitloss": "ebit",
    "operatingresult": "ebit",
    # net result
    "netincomeloss": "net_result",
    "profitloss": "net_result",
    "profitlossattributabletoownersofparent": "net_result",
    "netincomelossavailabletocommonstockholdersbasic": "net_result",
    "gainlossfortheperiod": "net_result",
    # equity
    "stockholdersequity": "equity",
    "stockholdersequityincludingportionattributabletononcontrollinginterest": "equity",
    "equity": "equity",
    "equityattributabletoownersofparent": "equity",
    # cash
    "cashandcashequivalentsatcarryingvalue": "cash",
    "cashcashequivalentsrestrictedcashandrestrictedcashequivalents": "cash",
    "cashandcashequivalents": "cash",
    "cashandbankbalances": "cash",
    # debt and totals
    "liabilities": "total_debt",
    "liabilitiesandstockholdersequity": "total_assets",
    "assets": "total_assets",
    "assetscurrent": "current_assets",
    "currentassets": "current_assets",
    "liabilitiescurrent": "current_liabilities",
    "currentliabilities": "current_liabilities",
    # people
    "employeebenefitsexpense": "personnel_costs",
    "laborandrelatedexpense": "personnel_costs",
    "employeerelatedliabilitiescurrent": None,  # explicitly not personnel cost
    "staffcosts": "personnel_costs",
    "numberofemployees": "headcount_fte",
    "averagenumberofemployees": "headcount_fte",
    "entitynumberofemployees": "headcount_fte",
    # investment
    "paymentstoacquirepropertyplantandequipment": "capex",
    "purchaseofpropertyplantandequipment": "capex",
    "additionstopropertyplantandequipment": "capex",
    "depreciationdepletionandamortization": "depreciation",
    "depreciationandamortisationexpense": "depreciation",
    "depreciationamortisationandimpairmentlossreversalofimpairmentloss": "depreciation",
}

#: When two concepts land on the same field in the same filing, the earlier
#: entry in this list wins.  Priority matters most for revenue, where a
#: registrant often tags both the ASC 606 concept and the legacy one.
CONCEPT_PRIORITY = [
    "revenues",
    "revenuefromcontractwithcustomerexcludingassessedtax",
    "revenuefromcontractwithcustomerincludingassessedtax",
    "salesrevenuenet",
    "turnover",
    "revenue",
    "operatingincomeloss",
    "profitlossfromoperatingactivities",
    "netincomeloss",
    "profitloss",
    "stockholdersequity",
    "equity",
    "cashandcashequivalentsatcarryingvalue",
    "cashandcashequivalents",
    "liabilities",
]

#: Debt *components*.  They are summed only when the filing never tags the
#: ``Liabilities`` total - letting a component overwrite the total is how a
#: balance sheet stops balancing (NFR-404).
DEBT_CONCEPTS = {
    "longtermdebt", "longtermdebtnoncurrent", "longtermdebtcurrent",
    "borrowings", "noncurrentborrowings", "currentborrowings",
    "debtcurrent", "debtnoncurrent",
}

#: Forms that carry a full financial year.
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F"}

MONEY_UNITS = ("USD", "EUR", "GBP", "CAD", "CHF", "SEK", "DKK", "NOK")


# ---------------------------------------------------------------------------
# SEC companyfacts (FR-241 US)
# ---------------------------------------------------------------------------


def _concept_priority(concept: str) -> int:
    try:
        return CONCEPT_PRIORITY.index(concept)
    except ValueError:
        return len(CONCEPT_PRIORITY)


def _prefer(filed: str, concept: str, previous: tuple[str, float, str, str]) -> bool:
    """Newest filing wins; within one filing, the higher-priority concept wins."""
    _end, _value, previous_filed, previous_concept = previous
    if filed != previous_filed:
        return filed > previous_filed
    return _concept_priority(concept) < _concept_priority(previous_concept)


def _is_annual(entry: dict) -> bool:
    """Flow items must span a year; instant items (balance sheet) always qualify."""
    start, end = entry.get("start"), entry.get("end")
    if not start or not end:
        return True
    try:
        span = (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return True
    return 300 <= span <= 400


def extract_from_companyfacts(payload: dict, *, years: int = 5) -> list[FilingFacts]:
    """Turn ``data.sec.gov/api/xbrl/companyfacts`` into one record per year.

    Annual figures only: a 10-K/20-F/40-F entry whose period is roughly twelve
    months, or a balance-sheet instant.  Amended filings supersede the original
    because the comparison is on the filing date, and the ``Liabilities`` total
    is never displaced by one of its own components.
    """
    facts = (payload or {}).get("facts") or {}
    chosen: dict[int, dict[str, tuple[str, float, str, str]]] = {}
    debt_parts: dict[int, dict[str, tuple[float, str]]] = {}
    currency = "USD"
    standard = "US-GAAP" if "us-gaap" in facts else "IFRS"

    for taxonomy, concepts in facts.items():
        if taxonomy not in ("us-gaap", "ifrs-full", "dei"):
            continue
        for concept, body in (concepts or {}).items():
            key = concept.lower()
            target = CONCEPT_MAP.get(key)
            is_debt_component = key in DEBT_CONCEPTS
            if target is None and not is_debt_component:
                continue
            for unit, entries in (body.get("units") or {}).items():
                if unit in MONEY_UNITS:
                    currency = unit
                elif not (unit == "pure" and target == "headcount_fte"):
                    continue
                for entry in entries:
                    if entry.get("form") not in ANNUAL_FORMS or not _is_annual(entry):
                        continue
                    end = entry.get("end")
                    fiscal_year = _fiscal_year(end)
                    if fiscal_year is None:
                        continue
                    value = float(entry.get("val") or 0)
                    filed = str(entry.get("filed") or "")
                    if target is None:
                        debt_parts.setdefault(fiscal_year, {})[key] = (value, filed)
                        continue
                    slot = chosen.setdefault(fiscal_year, {})
                    previous = slot.get(target)
                    if previous is None or _prefer(filed, key, previous):
                        slot[target] = (str(end), value, filed, key)

    out: list[FilingFacts] = []
    for fiscal_year in sorted(chosen, reverse=True)[:years]:
        slot = chosen[fiscal_year]
        record = FilingFacts(
            fiscal_year=fiscal_year,
            period_end=max((v[0] for v in slot.values()), default=None),
            currency=currency,
            reporting_standard=standard,
            source="sec_edgar",
        )
        for name, (_end, value, _filed, _concept) in slot.items():
            if hasattr(record, name):
                setattr(record, name, value)
        if record.total_debt is None and fiscal_year in debt_parts:
            components = debt_parts[fiscal_year]
            newest = max(filed for _value, filed in components.values())
            record.total_debt = sum(v for v, filed in components.values() if filed == newest)
            record.flag(
                "debt_summed_from_components",
                "The filing tags no Liabilities total; total debt is the sum of the "
                f"tagged debt components ({', '.join(sorted(components))})",
                severity="info",
            )
        finalise(record)
        out.append(record)
    return sorted(out, key=lambda r: r.fiscal_year)


# ---------------------------------------------------------------------------
# XBRL instance documents and inline XBRL
# ---------------------------------------------------------------------------

_QNAME = re.compile(r"\{(?P<ns>[^}]*)\}(?P<local>.+)")


def _local_name(tag: str) -> str:
    match = _QNAME.match(tag)
    return (match.group("local") if match else tag).lower()


def _contexts(root: Any) -> dict[str, dict[str, Any]]:
    """Context id -> ``{end, instant, dimensional}``.

    Dimensional contexts (segment/scenario) describe a slice - a business
    line, a subsidiary - not the entity total, so they are recorded and then
    skipped.
    """
    out: dict[str, dict[str, Any]] = {}
    for node in root.iter():
        if _local_name(node.tag) != "context":
            continue
        ctx_id = node.get("id")
        if not ctx_id:
            continue
        end = instant = None
        dimensional = False
        for child in node.iter():
            local = _local_name(child.tag)
            if local == "enddate":
                end = (child.text or "").strip()
            elif local == "instant":
                instant = (child.text or "").strip()
            elif local in ("segment", "scenario") and len(child):
                dimensional = True
        out[ctx_id] = {"end": end or instant, "dimensional": dimensional}
    return out


def _units(root: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in root.iter():
        if _local_name(node.tag) != "unit":
            continue
        unit_id = node.get("id")
        measures = [
            (m.text or "").strip().split(":")[-1].upper()
            for m in node.iter()
            if _local_name(m.tag) == "measure"
        ]
        if unit_id and measures:
            out[unit_id] = measures[0]
    return out


def extract_from_xbrl(data: bytes | str, *, years: int = 5) -> list[FilingFacts]:
    """Parse an XBRL instance document (NBB jsonxbrl siblings, IFRS, pfs)."""
    from lxml import etree  # noqa: PLC0415 - optional at import time

    payload = data.encode() if isinstance(data, str) else data
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True)
    root = etree.fromstring(payload, parser=parser)
    if root is None:
        return []

    contexts = _contexts(root)
    units = _units(root)
    by_year: dict[int, FilingFacts] = {}

    for node in root.iter():
        ctx_id = node.get("contextRef")
        if not ctx_id:
            continue
        ctx = contexts.get(ctx_id)
        if not ctx or ctx.get("dimensional"):
            continue
        target = CONCEPT_MAP.get(_local_name(node.tag))
        if target is None:
            continue
        fy = _fiscal_year(ctx.get("end"))
        if fy is None:
            continue
        value = parse_amount(node.text)
        if value is None:
            continue
        currency = units.get(node.get("unitRef") or "", "EUR")
        record = by_year.setdefault(
            fy,
            FilingFacts(
                fiscal_year=fy,
                period_end=ctx.get("end"),
                currency=currency if currency in ("EUR", "USD", "GBP", "CHF") else "EUR",
                reporting_standard="XBRL",
                source="xbrl",
            ),
        )
        if getattr(record, target, None) is None:
            setattr(record, target, value)

    out = [by_year[fy] for fy in sorted(by_year, reverse=True)[:years]]
    for record in out:
        finalise(record)
    return sorted(out, key=lambda r: r.fiscal_year)


_IX_TAG = re.compile(
    r"<ix:(?P<kind>nonFraction|nonNumeric)\b(?P<attrs>[^>]*)>(?P<body>.*?)</ix:(?P=kind)>",
    re.IGNORECASE | re.DOTALL,
)
_ATTR = re.compile(r"(?P<key>[\w:-]+)\s*=\s*[\"'](?P<value>[^\"']*)[\"']")
_TAGS = re.compile(r"<[^>]+>")


def extract_from_ixbrl(markup: bytes | str, *, years: int = 5) -> list[FilingFacts]:
    """Inline XBRL, as Companies House serves UK accounts.

    ``ix:nonFraction`` carries the concept in ``name``, the context in
    ``contextRef``, and a ``scale``/``sign`` pair that the displayed text does
    not include - a value of "1,234" with scale 3 means 1 234 000.
    """
    text = markup.decode("utf-8", errors="replace") if isinstance(markup, bytes) else markup
    contexts: dict[str, str] = {}
    for match in re.finditer(
        r"<xbrli:context[^>]*id=[\"'](?P<id>[^\"']+)[\"'](?P<body>.*?)</xbrli:context>",
        text,
        re.IGNORECASE | re.DOTALL,
    ):
        body = match.group("body")
        if re.search(r"<xbrldi:explicitMember", body, re.IGNORECASE):
            continue
        end = re.search(
            r"<xbrli:(?:endDate|instant)>\s*([0-9-]{8,10})\s*</xbrli:(?:endDate|instant)>",
            body,
            re.IGNORECASE,
        )
        if end:
            contexts[match.group("id")] = end.group(1)

    by_year: dict[int, FilingFacts] = {}
    for match in _IX_TAG.finditer(text):
        attrs = {
            m.group("key").lower(): m.group("value")
            for m in _ATTR.finditer(match.group("attrs"))
        }
        concept = (attrs.get("name") or "").split(":")[-1].lower()
        target = CONCEPT_MAP.get(concept)
        if target is None:
            continue
        period_end = contexts.get(attrs.get("contextref") or "")
        fy = _fiscal_year(period_end)
        if fy is None:
            continue
        scale = 10 ** int(attrs.get("scale") or 0)
        value = parse_amount(_TAGS.sub("", match.group("body")), float(scale))
        if value is None:
            continue
        if (attrs.get("sign") or "") == "-":
            value = -value
        record = by_year.setdefault(
            fy,
            FilingFacts(
                fiscal_year=fy,
                period_end=period_end,
                currency="GBP",
                reporting_standard="UK-GAAP",
                source="ixbrl",
            ),
        )
        if getattr(record, target, None) is None:
            setattr(record, target, value)

    out = [by_year[fy] for fy in sorted(by_year, reverse=True)[:years]]
    for record in out:
        finalise(record)
    return sorted(out, key=lambda r: r.fiscal_year)


# ---------------------------------------------------------------------------
# CSV exports
# ---------------------------------------------------------------------------

_CSV_ALIASES = {
    "revenue": "revenue", "turnover": "revenue", "omzet": "revenue",
    "chiffre d'affaires": "revenue", "net sales": "revenue",
    "gross profit": "gross_profit", "brutomarge": "gross_profit", "marge brute": "gross_profit",
    "ebit": "ebit", "operating profit": "ebit", "operating result": "ebit",
    "bedrijfsresultaat": "ebit", "bedrijfswinst": "ebit", "resultat d'exploitation": "ebit",
    "ebitda": "ebitda",
    "net result": "net_result", "net profit": "net_result", "net income": "net_result",
    "nettoresultaat": "net_result", "winst van het boekjaar": "net_result",
    "equity": "equity", "eigen vermogen": "equity", "capitaux propres": "equity",
    "cash": "cash", "liquide middelen": "cash", "cash and cash equivalents": "cash",
    "total debt": "total_debt", "schulden": "total_debt", "dettes": "total_debt",
    "total assets": "total_assets", "balanstotaal": "total_assets",
    "total du bilan": "total_assets",
    "current assets": "current_assets", "vlottende activa": "current_assets",
    "current liabilities": "current_liabilities",
    "schulden op ten hoogste een jaar": "current_liabilities",
    "headcount": "headcount_fte", "fte": "headcount_fte", "employees": "headcount_fte",
    "personeel": "headcount_fte", "personnel costs": "personnel_costs",
    "personeelskosten": "personnel_costs", "bezoldigingen": "personnel_costs",
    "capex": "capex", "capital expenditure": "capex", "investeringen": "capex",
    "depreciation": "depreciation", "afschrijvingen": "depreciation",
}


def _csv_delimiter(text: str) -> str:
    """European exports are semicolon-separated; sniffing beats assuming."""
    header = (text.splitlines() or [""])[0]
    return max((";", ",", "\t"), key=header.count) if header else ","


def extract_from_csv(text: str, *, years: int = 5) -> list[FilingFacts]:
    """A CSV whose first column names the item and whose header names the years."""
    rows = list(csv.reader(io.StringIO(text), delimiter=_csv_delimiter(text)))
    if len(rows) < 2:
        return []
    header = rows[0]
    year_columns: dict[int, int] = {}
    for index, cell in enumerate(header[1:], start=1):
        match = re.search(r"(19|20)\d{2}", str(cell))
        if match:
            year_columns[int(match.group(0))] = index
    if not year_columns:
        return []

    records = {
        fy: FilingFacts(fiscal_year=fy, period_end=f"{fy}-12-31", source="csv")
        for fy in sorted(year_columns, reverse=True)[:years]
    }
    for row in rows[1:]:
        if not row:
            continue
        label = re.sub(r"[^a-z' ]", " ", str(row[0]).lower()).strip()
        label = re.sub(r"\s+", " ", label)
        target = _CSV_ALIASES.get(label)
        if target is None:
            target = next((v for k, v in _CSV_ALIASES.items() if k and k in label), None)
        if target is None:
            continue
        for fy, index in year_columns.items():
            if fy not in records or index >= len(row):
                continue
            value = parse_amount(row[index])
            if value is not None and getattr(records[fy], target, None) is None:
                setattr(records[fy], target, value)

    out = list(records.values())
    for record in out:
        finalise(record)
    return sorted(out, key=lambda r: r.fiscal_year)


# ---------------------------------------------------------------------------
# PDF filings (FR-242): statutory codes first, labels second, LLM last
# ---------------------------------------------------------------------------

#: Belgian statutory account codes.  ``9900`` is deliberately absent: in the
#: full scheme it is the operating result, in the abbreviated scheme it is the
#: gross margin, and :func:`_resolve_be_9900` decides between them.
BE_CODES: dict[str, str] = {
    "70": "revenue",
    "9901": "ebit",
    "9904": "net_result",
    "10/15": "equity",
    "20/58": "total_assets",
    "29/58": "current_assets",
    "42/48": "current_liabilities",
    "54/58": "cash",
    "17/49": "total_debt",
    "62": "personnel_costs",
    "9087": "headcount_fte",
    "630": "depreciation",
    "8169": "capex",
}

_LABEL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(omzet|chiffre d.affaires|turnover|net sales|revenue)\b", re.I), "revenue"),
    (re.compile(r"\b(brutomarge|marge brute|gross profit|gross margin)\b", re.I), "gross_profit"),
    (
        re.compile(
            r"\b(bedrijfswinst|bedrijfsresultaat|r[ée]sultat d.exploitation"
            r"|operating (profit|result|income))\b",
            re.I,
        ),
        "ebit",
    ),
    (
        re.compile(
            r"\b(winst .verlies. van het boekjaar|net (profit|income|result)"
            r"|r[ée]sultat de l.exercice)\b",
            re.I,
        ),
        "net_result",
    ),
    (
        re.compile(
            r"\b(eigen vermogen|capitaux propres|total equity|shareholders.? funds)\b", re.I
        ),
        "equity",
    ),
    (re.compile(r"\b(liquide middelen|tr[ée]sorerie|cash (and|at) )", re.I), "cash"),
    (re.compile(r"\b(balanstotaal|total du bilan|total assets)\b", re.I), "total_assets"),
    (
        re.compile(r"\b(vlottende activa|actifs circulants|current assets)\b", re.I),
        "current_assets",
    ),
    (
        re.compile(
            r"\b(schulden op ten hoogste .{1,4} jaar|current liabilities"
            r"|dettes . un an au plus)\b",
            re.I,
        ),
        "current_liabilities",
    ),
    (
        re.compile(
            r"\b(bezoldigingen|personeelskosten|r[ée]mun[ée]rations|staff costs"
            r"|personnel (costs|expenses))\b",
            re.I,
        ),
        "personnel_costs",
    ),
    (
        re.compile(
            r"\b(gemiddeld personeelsbestand|effectif moyen"
            r"|average (number of )?(employees|staff))\b",
            re.I,
        ),
        "headcount_fte",
    ),
    (
        re.compile(r"\b(afschrijvingen|amortissements|depreciation and amortisation)\b", re.I),
        "depreciation",
    ),
]

_CODE_LINE = re.compile(
    r"(?P<code>\b(?:\d{1,4}(?:/\d{1,4})?)\b)\s+(?P<value>\(?-?[\d][\d ., ]*\)?-?)\s*$"
)


def pdf_to_text(data: bytes, *, max_pages: int = MAX_PDF_PAGES) -> str:
    """Filing text with table cells flattened onto their own lines."""
    try:
        import pdfplumber  # noqa: PLC0415 - heavy optional import
    except ImportError:  # pragma: no cover - pdfplumber is a declared dependency
        log.warning("pdfplumber is not installed; PDF filings cannot be extracted")
        return ""

    chunks: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:max_pages]:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - a broken page must not lose the filing
                log.debug("Unreadable PDF page in filing")
            for table in page.extract_tables() or []:
                for row in table:
                    cells = [str(c).strip() for c in row if c not in (None, "")]
                    if cells:
                        chunks.append(" ".join(cells))
    return "\n".join(chunks)


def _apply(record: FilingFacts, field_name: str, value: float | None) -> None:
    if value is None:
        return
    if getattr(record, field_name, None) is None:
        setattr(record, field_name, value)


def extract_from_text(
    text: str,
    *,
    fiscal_year: int | None = None,
    period_end: str | None = None,
    currency: str = "EUR",
    reporting_standard: str | None = "BE-GAAP",
    source: str = "pdf",
) -> FilingFacts:
    """Deterministic pass over filing text: statutory codes, then labels."""
    year = fiscal_year or _fiscal_year(period_end) or _guess_year(text) or 0
    record = FilingFacts(
        fiscal_year=year,
        period_end=period_end,
        currency=currency,
        reporting_standard=reporting_standard,
        source=source,
    )

    code_values: dict[str, float] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _CODE_LINE.search(stripped)
        if match:
            code = match.group("code")
            value = parse_amount(match.group("value"))
            if value is not None and code not in code_values:
                code_values[code] = value
                target = BE_CODES.get(code)
                if target:
                    _apply(record, target, value)
        for pattern, target in _LABEL_PATTERNS:
            if getattr(record, target, None) is not None:
                continue
            label = pattern.search(stripped)
            if label is None:
                continue
            for candidate in _NUM_RE.findall(stripped[label.end():]):
                value = parse_amount(candidate)
                if value is not None:
                    _apply(record, target, value)
                    break

    _resolve_be_9900(record, code_values)
    finalise(record)
    return record


def _resolve_be_9900(record: FilingFacts, code_values: dict[str, float]) -> None:
    """Belgian code 9900: gross margin in the abbreviated scheme, EBIT in the full one."""
    if "9900" not in code_values:
        return
    value = code_values["9900"]
    if record.ebit is None and "9901" not in code_values and record.revenue is not None:
        record.ebit = value
        return
    if record.gross_profit is None:
        record.gross_profit = value
        record.flag(
            "be_abbreviated_scheme",
            "Code 9900 read as gross margin: the filing follows the abbreviated scheme, "
            "which does not disclose turnover",
            severity="info",
        )


_YEAR_HINT = re.compile(
    r"\b(?:boekjaar|exercice|financial year|year ended)[^\d]{0,20}(20\d{2})\b", re.I
)


def _guess_year(text: str) -> int | None:
    match = _YEAR_HINT.search(text or "")
    if match:
        return int(match.group(1))
    years = [int(y) for y in re.findall(r"\b(20[0-3]\d)\b", text or "")]
    return max(years) if years else None


# ---------------------------------------------------------------------------
# LLM assistance for the awkward filings (FR-242, task "extract.table")
# ---------------------------------------------------------------------------

_LLM_SYSTEM = (
    "You read annual accounts and return the figures exactly as they are printed. "
    "You never estimate, never annualise, never convert currency and never fill a gap "
    "with a plausible number: a figure that is not in the document is null. "
    "Amounts are returned as plain numbers in the filing's own currency unit, with "
    "losses and costs negative where the filing shows them negative."
)

_LLM_SCHEMA = (
    '{"fiscal_year": int, "period_end": "YYYY-MM-DD", "currency": "EUR", '
    '"revenue": float|null, "gross_profit": float|null, "ebit": float|null, '
    '"net_result": float|null, "equity": float|null, "cash": float|null, '
    '"total_debt": float|null, "total_assets": float|null, "current_assets": float|null, '
    '"current_liabilities": float|null, "headcount_fte": float|null, '
    '"personnel_costs": float|null, "capex": float|null, "depreciation": float|null}'
)

_CORE_FIELDS = ("revenue", "ebit", "net_result", "equity", "total_assets")


def llm_complete_facts(
    record: FilingFacts,
    text: str,
    llm: LLMClient,
    *,
    company_name: str | None = None,
    entity_id: str | None = None,
) -> FilingFacts:
    """Fill the gaps a deterministic pass left, using the filing text as data.

    Only the missing fields are asked for, and every returned value goes
    through :func:`parse_amount` and then the reconciler - the model's output
    is a candidate, not a fact (NFR-205, NFR-404).
    """
    missing = [f for f in (*MONEY_FIELDS, "headcount_fte") if getattr(record, f, None) is None]
    if not missing or not text.strip():
        return record
    if llm.budget.should_degrade():
        record.flag(
            "llm_extraction_skipped",
            "Token budget nearly exhausted; the filing was read deterministically only",
            severity="info",
        )
        return record

    known = {f: getattr(record, f) for f in record.known_fields()}
    instruction = (
        f"Read the annual accounts of {company_name or 'the company'} in the data block and "
        f"report the figures for financial year {record.fiscal_year or 'shown in the document'}.\n"
        f"Already read deterministically (do not contradict these): {json.dumps(known)}\n"
        f"Still needed: {', '.join(missing)}.\n"
        "Return null for anything the document does not state."
    )
    try:
        payload = llm.complete_json(
            "extract.table",
            system=_LLM_SYSTEM,
            user=instruction,
            untrusted={"filing": text[:MAX_LLM_CHARS]},
            schema_hint=_LLM_SCHEMA,
            entity_type="financial_year",
            entity_id=entity_id,
        )
    except (LLMError, BudgetExhausted) as exc:
        log.info("LLM table extraction unavailable: %s", exc)
        record.flag("llm_extraction_failed", str(exc)[:200], severity="info")
        return record

    if not isinstance(payload, dict):
        record.flag("llm_extraction_failed", "Model did not return an object", severity="info")
        return record

    filled: list[str] = []
    for name in missing:
        value = parse_amount(payload.get(name)) if payload.get(name) is not None else None
        if value is None:
            continue
        setattr(record, name, value)
        filled.append(name)
    if payload.get("period_end") and not record.period_end:
        record.period_end = str(payload["period_end"])[:10]
    if payload.get("currency") and record.currency == "EUR":
        record.currency = str(payload["currency"])[:3].upper()
    if filled:
        record.flag(
            "llm_assisted_extraction",
            f"Read from the PDF with model assistance: {', '.join(filled)}",
            severity="info",
        )
    finalise(record)
    return record


def extract_from_pdf(
    data: bytes,
    *,
    fiscal_year: int | None = None,
    period_end: str | None = None,
    currency: str = "EUR",
    reporting_standard: str | None = "BE-GAAP",
    llm: LLMClient | None = None,
    company_name: str | None = None,
) -> FilingFacts:
    """Full FR-242 PDF path: pdfplumber tables, statutory codes, then the model."""
    text = pdf_to_text(data)
    record = extract_from_text(
        text,
        fiscal_year=fiscal_year,
        period_end=period_end,
        currency=currency,
        reporting_standard=reporting_standard,
        source="pdf",
    )
    thin = sum(1 for f in _CORE_FIELDS if getattr(record, f) is not None) < 3
    if llm is not None and thin:
        record = llm_complete_facts(record, text, llm, company_name=company_name)
    return record


# ---------------------------------------------------------------------------
# Derivation and reconciliation (FR-242, NFR-404, RK-06)
# ---------------------------------------------------------------------------

#: Personnel cost per FTE outside this band means the two figures are not on
#: the same scale (thousands vs units) or one of them was misread.
PLAUSIBLE_COST_PER_FTE = (8_000.0, 400_000.0)
BALANCE_TOLERANCE = 0.02


def derive(record: FilingFacts) -> FilingFacts:
    """Fill the figures that follow arithmetically from what was read."""
    # Gross profit is never derived: revenue minus EBIT is not a gross margin,
    # and inventing one would defeat the whole point of RK-06.
    if record.gross_margin is None and record.revenue and record.gross_profit is not None:
        record.gross_margin = record.gross_profit / record.revenue
    if record.ebitda is None and record.ebit is not None and record.depreciation is not None:
        record.ebitda = record.ebit + abs(record.depreciation)
    if record.total_debt is None and record.total_assets is not None and record.equity is not None:
        record.total_debt = record.total_assets - record.equity
    if record.total_assets is None and record.total_debt is not None and record.equity is not None:
        record.total_assets = record.total_debt + record.equity
    return record


def reconcile(record: FilingFacts) -> FilingFacts:
    """NFR-404: check what can be checked and flag what does not add up."""
    # Sign conventions: costs and depreciation are magnitudes here.
    for name in ("personnel_costs", "capex", "depreciation"):
        value = getattr(record, name)
        if value is not None and value < 0:
            setattr(record, name, abs(value))
            record.flag(
                "sign_normalised",
                f"{name} was reported negative and has been stored as a magnitude",
                severity="info",
                field=name,
            )
    if record.headcount_fte is not None and record.headcount_fte < 0:
        record.flag("implausible_headcount", "Negative headcount", severity="error")
        record.headcount_fte = None

    # Balance sheet: assets = equity + liabilities.
    if record.total_assets and record.equity is not None and record.total_debt is not None:
        difference = abs(record.total_assets - (record.equity + record.total_debt))
        if difference > abs(record.total_assets) * BALANCE_TOLERANCE:
            record.flag(
                "balance_sheet_mismatch",
                f"Assets {record.total_assets:,.0f} != equity {record.equity:,.0f} + "
                f"liabilities {record.total_debt:,.0f} (off by {difference:,.0f})",
                severity="error",
                difference=difference,
            )
    if record.current_assets is not None and record.total_assets:
        if record.current_assets > record.total_assets * (1 + BALANCE_TOLERANCE):
            record.flag(
                "current_assets_exceed_total",
                f"Current assets {record.current_assets:,.0f} exceed total assets "
                f"{record.total_assets:,.0f}",
                severity="error",
            )
    if record.cash is not None and record.current_assets is not None:
        if record.cash > record.current_assets * (1 + BALANCE_TOLERANCE):
            record.flag(
                "cash_exceeds_current_assets",
                f"Cash {record.cash:,.0f} exceeds current assets {record.current_assets:,.0f}",
                severity="warning",
            )

    # Income statement consistency.
    if record.revenue is not None and record.revenue < 0:
        record.flag("negative_revenue", "Turnover reported negative", severity="error")
    if record.ebitda is not None and record.ebit is not None and record.ebitda < record.ebit:
        record.flag(
            "ebitda_below_ebit",
            f"EBITDA {record.ebitda:,.0f} below EBIT {record.ebit:,.0f}",
            severity="warning",
        )
    if record.revenue and record.ebit is not None and abs(record.ebit) > abs(record.revenue) * 2:
        record.flag(
            "ebit_out_of_scale",
            f"Operating result {record.ebit:,.0f} is implausible against turnover "
            f"{record.revenue:,.0f}",
            severity="warning",
        )

    # Pay-level sanity, which is also the FR-243 proxy's own guard rail.
    if record.personnel_costs and record.headcount_fte:
        per_fte = record.personnel_costs / record.headcount_fte
        low, high = PLAUSIBLE_COST_PER_FTE
        if not low <= per_fte <= high:
            record.flag(
                "cost_per_fte_out_of_band",
                f"Personnel cost per FTE {per_fte:,.0f} outside the plausible band "
                f"{low:,.0f}-{high:,.0f}; the two figures may be on different scales",
                severity="warning",
                value=per_fte,
            )

    # FR-245: an abbreviated account that never states turnover.
    if record.revenue is None and (record.gross_profit is not None or record.equity is not None):
        record.is_estimated = True
        record.flag(
            "turnover_not_disclosed",
            "The filing does not disclose turnover (abbreviated scheme); the financial "
            "profile is marked estimated and secondary signals are used instead",
            severity="info",
        )
    if not record.known_fields():
        record.flag("no_figures_extracted", "No figures could be read from the filing", "error")
    return record


def finalise(record: FilingFacts) -> FilingFacts:
    """Derive, then reconcile.  Every extractor ends here (NFR-404)."""
    derive(record)
    return reconcile(record)


# ---------------------------------------------------------------------------
# EUR normalisation (DR-103)
# ---------------------------------------------------------------------------

#: Last-resort rates, used only when the ECB reference feed cannot be reached.
#: Every row that falls back to these is flagged, never presented as exact.
FALLBACK_RATES_TO_EUR: dict[str, float] = {
    "EUR": 1.0, "USD": 0.92, "GBP": 1.17, "CHF": 1.05, "SEK": 0.088,
    "NOK": 0.086, "DKK": 0.134, "PLN": 0.23, "CZK": 0.040, "CAD": 0.68,
}

_ECB_URL = (
    "https://data-api.ecb.europa.eu/service/data/EXR/D.{ccy}.EUR.SP00.A"
    "?startPeriod={start}&endPeriod={end}&format=csvdata&detail=dataonly"
)


@dataclass
class FxQuote:
    """One conversion rate and, just as importantly, where it came from.

    DR-103 wants the conversion date stored beside the rate; NFR-404 wants a
    figure that rests on an approximation to say so rather than to pass as
    exact.  ``origin`` is what carries the second half of that.
    """

    currency: str
    rate: float
    on_date: str
    origin: str          # ecb | fallback_table | unquoted

    @property
    def exact(self) -> bool:
        return self.origin in ("ecb", "same_currency")

    def flag(self) -> dict[str, Any] | None:
        """The reconciliation flag an inexact rate must carry (NFR-404, RK-06)."""
        if self.exact:
            return None
        if self.origin == "unquoted":
            return {
                "code": "fx_rate_missing",
                "detail": (
                    f"No EUR reference rate is published for {self.currency} and none is on "
                    "file; the figures are stored unconverted and must be read in their own "
                    "currency"
                ),
                "severity": "error",
                "currency": self.currency,
            }
        return {
            "code": "fx_rate_approximate",
            "detail": (
                f"The ECB reference feed was unreachable; {self.currency} was converted at the "
                f"built-in indicative rate {self.rate:g} rather than the {self.on_date} "
                "quotation"
            ),
            "severity": "warning",
            "currency": self.currency,
        }


async def fx_quote(currency: str, on_date: str, egress: Any = None) -> FxQuote:
    """Units of EUR per one unit of ``currency``, from the ECB reference feed.

    The ECB publishes the inverse - currency per euro - so the observation is
    inverted here.  A feed that cannot be reached falls back to
    :data:`FALLBACK_RATES_TO_EUR`, and a currency the table does not hold is
    left unconverted at 1.0; both say so through :meth:`FxQuote.flag`, because
    a silently wrong conversion is precisely what RK-06 is about.
    """
    code = (currency or "EUR").upper()
    if code == "EUR":
        return FxQuote("EUR", 1.0, on_date, "same_currency")

    try:
        reference = date.fromisoformat(str(on_date)[:10])
    except ValueError:
        reference = datetime.now(UTC).date()
    # A period end lands on a weekend or a holiday often enough that a single
    # day is the wrong window; ten days back always contains a quotation.
    window_start = date.fromordinal(max(1, reference.toordinal() - 10))
    url = _ECB_URL.format(ccy=code, start=window_start.isoformat(), end=reference.isoformat())

    if egress is not None:
        try:
            result = await egress.fetch(url)
            if result.ok:
                rows = list(csv.DictReader(io.StringIO(result.text)))
                observations = [
                    (r.get("TIME_PERIOD"), float(r["OBS_VALUE"]))
                    for r in rows
                    if r.get("OBS_VALUE")
                ]
                if observations:
                    period, value = observations[-1]
                    if value:
                        return FxQuote(
                            code, 1.0 / value, str(period or reference.isoformat()), "ecb"
                        )
        except Exception as exc:  # noqa: BLE001 - FX must never break a filing
            log.info("ECB rate for %s unavailable (%s); using the fallback table", code, exc)

    fallback = FALLBACK_RATES_TO_EUR.get(code)
    if fallback is None:
        log.warning("No EUR rate on file for %s; the figures stay unconverted", code)
        return FxQuote(code, 1.0, reference.isoformat(), "unquoted")
    return FxQuote(code, fallback, reference.isoformat(), "fallback_table")


async def ecb_rate_to_eur(currency: str, on_date: str, egress: Any = None) -> tuple[float, str]:
    """The rate and its conversion date alone (DR-103), for callers that need no more."""
    quote = await fx_quote(currency, on_date, egress=egress)
    return quote.rate, quote.on_date


def eur_rate(row: dict[str, Any]) -> float | None:
    """The rate that converts this row to EUR, or ``None`` when there is none.

    DR-103 stores the rate beside the figures precisely so that a conversion is
    never assumed.  Treating a missing rate as parity is the one reading that is
    silently wrong: an Apple filing in USD would be presented as though 391 bn
    USD were 391 bn EUR, an 11% overstatement with nothing to show for it.
    """
    rate = row.get("fx_rate_to_eur")
    if rate not in (None, ""):
        try:
            value = float(rate)
        except (TypeError, ValueError):
            value = 0.0
        if value:
            return value
    return 1.0 if str(row.get("currency") or "EUR").upper() == "EUR" else None


def unconverted_flag(row: dict[str, Any]) -> dict[str, Any] | None:
    """The NFR-404 reconciliation flag a row without a usable rate must carry."""
    if eur_rate(row) is not None:
        return None
    currency = str(row.get("currency") or "EUR").upper()
    return {
        "code": "fx_rate_missing",
        "detail": (
            f"{currency} figures are stored with no conversion rate; they are not comparable "
            "in EUR and are left out of the EUR view rather than read as parity (DR-103)"
        ),
        "severity": "error",
        "currency": currency,
    }


def eur_values(row: dict[str, Any]) -> dict[str, float | None]:
    """The EUR-normalised view of a stored ``financial_year`` row (DR-103).

    A non-EUR row with no stored rate yields ``None`` for every figure: what is
    unknown is the EUR value, not the figure, and :func:`unconverted_flag` says
    why.  The row's own currency values remain available on the row itself.
    """
    rate = eur_rate(row)
    out: dict[str, float | None] = {}
    for name in MONEY_FIELDS:
        value = row.get(name)
        out[name] = None if (rate is None or value in (None, "")) else float(value) * rate
    return out


# ---------------------------------------------------------------------------
# Mapping onto the knowledge base
# ---------------------------------------------------------------------------


def to_financial_year_row(
    company_id: str,
    record: FilingFacts,
    *,
    fx_rate_to_eur: float | None = None,
    fx_date: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """``FilingFacts`` -> a ``financial_year`` row (DR-103, NFR-404)."""
    flags = list(record.reconciliation_flags)
    if record.currency != "EUR" and fx_rate_to_eur is None:
        flags.append(
            {
                "code": "fx_rate_missing",
                "detail": f"No conversion rate obtained for {record.currency}",
                "severity": "warning",
            }
        )
    return {
        "company_id": company_id,
        "fiscal_year": int(record.fiscal_year),
        "period_end": record.period_end,
        "currency": record.currency or "EUR",
        "reporting_standard": record.reporting_standard,
        "fx_rate_to_eur": fx_rate_to_eur if record.currency != "EUR" else 1.0,
        "fx_date": fx_date,
        "revenue": record.revenue,
        "gross_profit": record.gross_profit,
        "gross_margin": record.gross_margin,
        "ebit": record.ebit,
        "ebitda": record.ebitda,
        "net_result": record.net_result,
        "equity": record.equity,
        "cash": record.cash,
        "total_debt": record.total_debt,
        "total_assets": record.total_assets,
        "current_assets": record.current_assets,
        "current_liabilities": record.current_liabilities,
        "headcount_fte": record.headcount_fte,
        "personnel_costs": record.personnel_costs,
        "capex": record.capex,
        "is_estimated": 1 if record.is_estimated else 0,
        "reconciliation_flags": flags,
        "filing_document_id": record.filing_document_id,
        "source": source or record.source,
    }


def load_filing(document_id: str) -> tuple[bytes, str] | None:
    """Re-read a stored filing from disk so extractors can be re-run (DR-102)."""
    row = repo.raw_document(document_id)
    if not row:
        return None
    path = Path(str(row["storage_path"]))
    if not path.is_absolute():
        path = get_settings().abs_data_dir / path
    if not path.exists():
        log.warning("Stored filing %s is missing at %s", document_id, path)
        return None
    return path.read_bytes(), str(row["content_type"] or "")


def extract_filing(
    data: bytes,
    content_type: str,
    *,
    fiscal_year: int | None = None,
    period_end: str | None = None,
    currency: str = "EUR",
    reporting_standard: str | None = None,
    llm: LLMClient | None = None,
    company_name: str | None = None,
    years: int = 5,
) -> list[FilingFacts]:
    """Dispatch on content type: XBRL, iXBRL, JSON, CSV or PDF (FR-242)."""
    ctype = (content_type or "").lower()
    head = data[:4096].decode("utf-8", errors="replace").lstrip()

    if "pdf" in ctype or data[:5] == b"%PDF-":
        return [
            extract_from_pdf(
                data,
                fiscal_year=fiscal_year,
                period_end=period_end,
                currency=currency,
                reporting_standard=reporting_standard or "BE-GAAP",
                llm=llm,
                company_name=company_name,
            )
        ]
    if "json" in ctype or head.startswith(("{", "[")):
        try:
            payload = json.loads(data.decode("utf-8", errors="replace"))
        except ValueError:
            payload = None
        if isinstance(payload, dict) and "facts" in payload:
            return extract_from_companyfacts(payload, years=years)
        if isinstance(payload, dict):
            return extract_from_jsonxbrl(payload, years=years)
    if "csv" in ctype or (";" in (head.splitlines() or [""])[0]):
        return extract_from_csv(data.decode("utf-8", errors="replace"), years=years)
    if "ix:nonfraction" in head.lower() or "<ix:" in data[:200_000].decode(
        "utf-8", errors="replace"
    ).lower():
        return extract_from_ixbrl(data, years=years)
    if head.startswith("<"):
        return extract_from_xbrl(data, years=years)
    return [
        extract_from_text(
            data.decode("utf-8", errors="replace"),
            fiscal_year=fiscal_year,
            period_end=period_end,
            currency=currency,
            reporting_standard=reporting_standard,
            source="text",
        )
    ]


def extract_from_jsonxbrl(payload: dict, *, years: int = 5) -> list[FilingFacts]:
    """The NBB Consult API's ``application/x.jsonxbrl`` accounting data.

    Deposits arrive as ``{"Rubrics": [{"Code": "9901", "Value": 123, "Period": "N"}]}``
    with ``N`` the reported year and ``N-1`` the comparative, so one deposit
    yields two financial years.
    """
    rubrics = payload.get("Rubrics") or payload.get("rubrics") or []
    if not isinstance(rubrics, list) or not rubrics:
        return []
    end = (
        payload.get("PeriodEndDate")
        or payload.get("periodEndDate")
        or (payload.get("Deposit") or {}).get("PeriodEndDate")
    )
    current_year = _fiscal_year(end) or _guess_year(json.dumps(payload)[:4000]) or 0

    buckets: dict[str, dict[str, float]] = {}
    for entry in rubrics:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("Code") or entry.get("code") or "").strip()
        period = str(entry.get("Period") or entry.get("period") or "N").strip().upper()
        value = parse_amount(entry.get("Value", entry.get("value")))
        if not code or value is None:
            continue
        buckets.setdefault(period, {}).setdefault(code, value)

    out: list[FilingFacts] = []
    for period, codes in buckets.items():
        offset = 1 if period in ("N-1", "N_1", "PREVIOUS") else 0
        year = current_year - offset if current_year else 0
        record = FilingFacts(
            fiscal_year=year,
            period_end=end if offset == 0 else None,
            currency="EUR",
            reporting_standard="BE-GAAP",
            source="nbb_jsonxbrl",
        )
        for code, value in codes.items():
            target = BE_CODES.get(code)
            if target:
                _apply(record, target, value)
        _resolve_be_9900(record, codes)
        finalise(record)
        out.append(record)
    out = [r for r in out if r.fiscal_year]
    return sorted(out, key=lambda r: r.fiscal_year)[-years:]
