"""Hiring signals and timing intelligence (FR-225, FR-402, NFR-402).

FR-225 asks *what happened*: headcount growth, new offices, funding rounds,
product launches and a burst of postings across functions, each with a date and
a source.  FR-402 asks *what it means for the job seeker*: which of those events
typically precede hiring, and when the application should therefore land.

The two halves are deliberately separate.  Detection is deterministic and
evidence-bound - every :func:`detect_all` result carries the source it was read
from and the date it happened, or, for a standing statement on a page rather
than a dated event, the date it was last seen there.  The recommendation on top
of it is an explicit model: each signal type opens a hiring window some weeks
after the event and closes it some months later, weighted by how reliably that
event precedes recruitment.  A funding round is the strongest and slowest
(teams are hired for months afterwards); a
fresh burst of postings is weaker but immediate; a competitor's layoffs open a
short window in which a speculative application meets less internal resistance
and more external competition, so it favours acting quickly.

The window model is data, not code (:data:`TIMING_MODEL`), so that it can be
tuned against observed outcomes (FR-425) without touching the algorithm.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from dreamjob.db.connection import from_json, utcnow
from dreamjob.db.repositories import companies as repo

log = logging.getLogger(__name__)

#: Signal vocabulary, matching ``hiring_signal.signal_type`` in the schema.
SIGNAL_TYPES = (
    "headcount_growth",
    "new_office",
    "funding",
    "product_launch",
    "postings",
    "reorg",
    "leadership_change",
    "fiscal_year_start",
    "competitor_layoff",
)


def _pattern(*fragments: str) -> re.Pattern[str]:
    return re.compile("|".join(fragments), re.IGNORECASE)


#: Signal type -> (pattern, base strength).  Dutch and French phrasings are
#: included because the target market is Belgium and the Netherlands.
NEWS_PATTERNS: list[tuple[str, re.Pattern[str], float]] = [
    (
        "funding",
        _pattern(
            r"\bseries [a-e]\b", r"\bseed round\b", r"\bpre-seed\b", r"\bfunding round\b",
            r"\braises? (?:€|\$|£)?\s?\d", r"\braised (?:€|\$|£)?\s?\d",
            r"\bsecures? (?:€|\$|£)?\s?\d",
            r"\bkapitaalronde\b", r"\bfinancieringsronde\b", r"\bhaalt .{0,20}miljoen op\b",
            r"\blev(?:é|ee) de fonds\b", r"\bventure round\b", r"\bgrowth capital\b",
            r"\binvestment of (?:€|\$|£)", r"\bnew investor\b", r"\bmajority stake\b",
        ),
        0.95,
    ),
    (
        "new_office",
        _pattern(
            r"\bnew office\b", r"\bopens? (?:a |its |our )?(?:new )?office\b", r"\bnieuw kantoor\b",
            r"\bopent .{0,20}vestiging\b", r"\bnouveau bureau\b", r"\bexpands? (?:in)?to\b",
            r"\bopens? (?:a |its )?(?:new )?(?:hub|site|branch|facility|plant)\b",
            r"\bexpansion into\b", r"\bsecond location\b", r"\bnieuwe locatie\b",
        ),
        0.8,
    ),
    (
        "product_launch",
        _pattern(
            r"\blaunch(?:es|ed|ing)?\b", r"\bintroduc(?:es|ed|ing)\b", r"\bunveil(?:s|ed)\b",
            r"\bnew (?:product|platform|service|solution|version|release)\b",
            r"\blancee?rt\b", r"\bnieuwe (?:dienst|product|oplossing)\b", r"\blance\b",
            r"\bgeneral availability\b", r"\bnow available\b",
        ),
        0.6,
    ),
    (
        "reorg",
        _pattern(
            r"\breorganis", r"\brestructur", r"\bmerger\b", r"\bacquisitio\b", r"\bacquires?\b",
            r"\bacquired\b", r"\bspin-?off\b", r"\bnew division\b", r"\bnew business unit\b",
            r"\bherstructurer", r"\bovername\b", r"\bneemt .{0,25}over\b", r"\bfusie\b",
            r"\brachat\b", r"\bnouvelle division\b", r"\bintegration of\b",
        ),
        0.7,
    ),
    (
        "leadership_change",
        _pattern(
            r"\bnew (?:ceo|cto|cfo|coo|cio|cmo|chro|managing director|general manager)\b",
            r"\bappoint(?:s|ed|ment)\b", r"\bjoins? as\b", r"\bnamed (?:as )?(?:ceo|cto|cfo|coo)\b",
            r"\bsteps? down\b", r"\bbenoemd\b", r"\bnieuwe (?:ceo|directeur|algemeen directeur)\b",
            r"\bnomination\b", r"\bnomm(?:é|e)\b",
            r"\bversterkt (?:het|de) (?:management|directie)",
        ),
        0.7,
    ),
    (
        "headcount_growth",
        _pattern(
            r"\bhir(?:es|ing|ed) \d", r"\bgrow(?:s|ing|n) to \d+ (?:employees|people|staff|fte)",
            r"\b\d+ new (?:employees|colleagues|hires|jobs|roles)\b", r"\bteam (?:grew|doubled)\b",
            r"\bnieuwe (?:collega|medewerkers|banen)\b", r"\bwerft .{0,15}aan\b",
            r"\bcre(?:e|é)e? \d+ (?:emplois|postes)\b", r"\bcreat(?:es|ing) \d+ jobs\b",
        ),
        0.85,
    ),
    (
        "competitor_layoff",
        _pattern(
            r"\blay ?offs?\b", r"\bredundanc", r"\bjob cuts\b", r"\bcuts? \d+ jobs\b",
            r"\bontslagen\b", r"\bherstructurering met banenverlies\b", r"\blicenciement",
            r"\bplan social\b", r"\bcollectief ontslag\b", r"\bwet renault\b",
        ),
        0.75,
    ),
]

#: Phrases on a careers page that say the company is actively recruiting.
CAREERS_HINTS = _pattern(
    r"\bwe(?:'re| are) hiring\b", r"\bjoin our team\b", r"\bopen (?:positions|roles|vacancies)\b",
    r"\bwe zijn op zoek\b", r"\bvacatures\b", r"\bwerken bij\b", r"\bnous recrutons\b",
    r"\bcurrent openings\b", r"\bgrowing team\b", r"\bgroeiend team\b",
)


# ---------------------------------------------------------------------------
# FR-402: how long each event stays a reason to apply
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TimingRule:
    """When a signal opens and closes a hiring window, and how much it counts."""

    opens_after_days: int
    closes_after_days: int
    weight: float
    reason: str


#: The model FR-402 rests on.  ``opens_after_days`` is the lag between the event
#: and the moment applications start landing usefully; ``closes_after_days`` is
#: when the effect has washed out.
TIMING_MODEL: dict[str, TimingRule] = {
    "funding": TimingRule(14, 270, 1.00, "hiring plans follow a funding round within weeks"),
    "new_office": TimingRule(0, 180, 0.85, "a new location has to be staffed"),
    "headcount_growth": TimingRule(0, 180, 0.80, "the company is already growing its headcount"),
    "postings": TimingRule(0, 45, 0.75, "vacancies are open across several functions right now"),
    "reorg": TimingRule(30, 210, 0.60, "reorganisations create newly defined roles"),
    "leadership_change": TimingRule(30, 240, 0.60, "new leaders build out their own teams"),
    "product_launch": TimingRule(
        30, 180, 0.55, "a launch is followed by delivery and support hiring"
    ),
    "fiscal_year_start": TimingRule(-30, 90, 0.50, "new budgets release approved headcount"),
    "competitor_layoff": TimingRule(0, 90, 0.45, "talent from a competitor is on the market now"),
}

FLAG_THRESHOLDS = ((0.65, "apply_now"), (0.40, "favourable"), (0.18, "watch"))
DEFAULT_FLAG = "neutral"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_news(title: str, summary: str = "") -> tuple[str | None, float]:
    """Map a news item onto a signal type and a strength (FR-225).

    Called by the RSS adapter for every item it collects.  Precedence follows
    the order of :data:`NEWS_PATTERNS`: a funding announcement that also
    mentions a product is a funding signal, because that is what drives hiring.
    """
    text = f"{title or ''}\n{summary or ''}"
    if not text.strip():
        return None, 0.0
    for signal_type, pattern, strength in NEWS_PATTERNS:
        match = pattern.search(text)
        if match:
            # A hit in the headline is worth more than one buried in the body.
            in_title = bool(pattern.search(title or ""))
            return signal_type, round(strength * (1.0 if in_title else 0.8), 3)
    return None, 0.0


def _iso_date(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)[:19]
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _days_since(value: str | None, now: datetime) -> float | None:
    stamp = _iso_date(value)
    if not stamp:
        return None
    return (now - datetime.fromisoformat(stamp)).total_seconds() / 86_400.0


# ---------------------------------------------------------------------------
# Detection (FR-225)
# ---------------------------------------------------------------------------


def signals_from_news(company_id: str, items: list[Any]) -> list[dict]:
    """Hiring signals read from newsroom items, with their dates and sources."""
    out: list[dict] = []
    for item in items:
        data = item.as_dict() if hasattr(item, "as_dict") else dict(item)
        signal_type, strength = classify_news(data.get("title", ""), data.get("summary", ""))
        if not signal_type:
            continue
        # A layoff at *this* company is a reorganisation, not an opportunity;
        # `competitor_layoff` is only recorded against the company that benefits.
        if signal_type == "competitor_layoff":
            signal_type, strength = "reorg", strength * 0.6
        out.append(
            {
                "company_id": company_id,
                "signal_type": signal_type,
                "description": (data.get("title") or "")[:500],
                "occurred_at": _iso_date(data.get("published_at")),
                "source_url": data.get("url") or data.get("feed_url"),
                "strength": round(strength, 3),
            }
        )
    return out


def signals_from_pages(company_id: str, pages: list[Any]) -> list[dict]:
    """Signals visible on the crawled site itself (careers page, press page).

    A page says what is true *now*, not what happened on a date, so these rows
    carry no ``occurred_at``: their natural key is the page they were read from
    and ``collected_at`` is the moment they were last seen there.  Stamping the
    clock into ``occurred_at`` instead would make every re-crawl a new event and
    let one careers page accumulate a fresh "we are hiring" signal every day.
    """
    out: list[dict] = []
    for page in pages:
        kind = getattr(page, "kind", None) or (page.get("kind") if isinstance(page, dict) else None)
        text = getattr(page, "text", None) or (page.get("text") if isinstance(page, dict) else "")
        url = getattr(page, "url", None) or (page.get("url") if isinstance(page, dict) else "")
        if not text:
            continue
        if kind == "careers" and CAREERS_HINTS.search(text):
            out.append(
                {
                    "company_id": company_id,
                    "signal_type": "postings",
                    "description": "Careers page is actively advertising open roles",
                    "occurred_at": None,
                    "source_url": url,
                    "strength": 0.5,
                }
            )
        if kind in ("news", "about", "careers"):
            signal_type, strength = classify_news("", text[:4000])
            if signal_type and signal_type != "competitor_layoff":
                out.append(
                    {
                        "company_id": company_id,
                        "signal_type": signal_type,
                        "description": f"Stated on the company website ({kind} page)",
                        "occurred_at": None,
                        "source_url": url,
                        "strength": round(strength * 0.6, 3),
                    }
                )
    return out


def signals_from_financials(company_id: str) -> list[dict]:
    """Headcount growth read from the filed figures, when they are present."""
    years = repo.financial_years(company_id, limit=5)
    usable = [
        (int(y["fiscal_year"]), float(y["headcount_fte"]))
        for y in years
        if y.get("fiscal_year") is not None and y.get("headcount_fte")
    ]
    if len(usable) < 2:
        return []
    usable.sort()
    (first_year, first_fte), (last_year, last_fte) = usable[0], usable[-1]
    if first_fte <= 0 or last_fte <= first_fte:
        return []
    span = max(1, last_year - first_year)
    cagr = (last_fte / first_fte) ** (1 / span) - 1
    if cagr < 0.05:
        return []
    return [
        {
            "company_id": company_id,
            "signal_type": "headcount_growth",
            "description": (
                f"Headcount grew from {first_fte:.0f} to {last_fte:.0f} FTE "
                f"between {first_year} and {last_year} ({cagr * 100:.0f}% per year)"
            ),
            "occurred_at": f"{last_year}-12-31T00:00:00+00:00",
            "source_url": None,
            "strength": round(min(0.95, 0.5 + cagr), 3),
        }
    ]


def signals_from_postings(company_id: str, *, window_days: int = 45) -> list[dict]:
    """Recent postings across functions - the most direct signal there is."""
    since = (datetime.now(UTC) - timedelta(days=window_days)).isoformat(timespec="seconds")
    stats = repo.recent_posting_stats(company_id, since)
    if stats["count"] < 2:
        return []
    breadth = min(1.0, stats["families"] / 4.0)
    volume = min(1.0, stats["count"] / 10.0)
    return [
        {
            "company_id": company_id,
            "signal_type": "postings",
            "description": (
                f"{stats['count']} vacancies collected in the last {window_days} days "
                f"across {stats['families'] or 1} function families"
            ),
            "occurred_at": _iso_date(stats["latest"]) or utcnow(),
            "source_url": None,
            "strength": round(0.4 + 0.5 * max(breadth, volume), 3),
        }
    ]


def fiscal_year_start(company: dict) -> date | None:
    """The next start of a financial year, from the filed period ends."""
    years = repo.financial_years(company["id"], limit=3)
    ends = [y.get("period_end") for y in years if y.get("period_end")]
    month, day = 1, 1
    for end in ends:
        parsed = _iso_date(end) or (f"{end}T00:00:00+00:00" if len(str(end)) == 10 else None)
        if not parsed:
            continue
        end_date = datetime.fromisoformat(parsed).date()
        following = end_date + timedelta(days=1)
        month, day = following.month, following.day
        break
    today = datetime.now(UTC).date()
    try:
        candidate = date(today.year, month, day)
    except ValueError:  # 29 February in a non-leap year
        candidate = date(today.year, month, 28)
    if candidate < today - timedelta(days=45):
        try:
            candidate = date(today.year + 1, month, day)
        except ValueError:
            candidate = date(today.year + 1, month, 28)
    return candidate


def signals_from_fiscal_calendar(company: dict) -> list[dict]:
    """FR-402: a new budget year is when approved headcount is released."""
    start = fiscal_year_start(company)
    if start is None:
        return []
    return [
        {
            "company_id": company["id"],
            "signal_type": "fiscal_year_start",
            "description": f"Financial year starts on {start.isoformat()}",
            "occurred_at": f"{start.isoformat()}T00:00:00+00:00",
            "source_url": None,
            "strength": 0.45,
        }
    ]


def signals_from_competitors(company_id: str, *, lookback_days: int = 120) -> list[dict]:
    """A peer shedding staff puts experienced people on the market (FR-402)."""
    since = (datetime.now(UTC) - timedelta(days=lookback_days)).isoformat(timespec="seconds")
    out: list[dict] = []
    for link in repo.list_competitors(company_id, limit=20):
        peer_id = link.get("peer_company_id")
        if not peer_id:
            continue
        peer_name = link.get("peer_resolved_name") or link.get("peer_name") or "a competitor"
        for signal in repo.signals_since(peer_id, since, limit=20):
            description = f"{signal.get('description') or ''}"
            if signal["signal_type"] != "reorg" or not _looks_like_layoff(description):
                continue
            out.append(
                {
                    "company_id": company_id,
                    "signal_type": "competitor_layoff",
                    "description": f"{peer_name}: {description}"[:500],
                    "occurred_at": signal.get("occurred_at") or signal.get("collected_at"),
                    "source_url": signal.get("source_url"),
                    "strength": round(float(signal.get("strength") or 0.5) * 0.8, 3),
                }
            )
    return out


_LAYOFF_PATTERN = NEWS_PATTERNS[-1][1]


def _looks_like_layoff(text: str) -> bool:
    return bool(_LAYOFF_PATTERN.search(text or ""))


def signals_from_profile_news(company: dict) -> list[dict]:
    """Dated indicators the profile extractor read off the crawled pages (FR-225).

    ``company.news`` holds what the site itself reported - "opened a second site
    in Eindhoven in March" - already typed, dated and attributed to a URL.  That
    is a hiring signal in every sense FR-225 means, so it is recorded as one
    instead of staying a display string on the profile.
    """
    out: list[dict] = []
    for item in from_json(company.get("news"), None) or []:
        if not isinstance(item, dict):
            continue
        signal_type = str(item.get("signal_type") or "")
        occurred = _iso_date(item.get("occurred_at"))
        title = str(item.get("title") or "").strip()
        if signal_type not in SIGNAL_TYPES or not occurred or not title:
            continue
        if signal_type == "competitor_layoff":
            continue
        out.append(
            {
                "company_id": company["id"],
                "signal_type": signal_type,
                "description": title[:500],
                "occurred_at": occurred,
                "source_url": item.get("url"),
                "strength": 0.6,
            }
        )
    return out


def detect_all(
    company: dict,
    *,
    pages: list[Any] | None = None,
    news_items: list[Any] | None = None,
) -> list[dict]:
    """Every detector, in one list of ``hiring_signal`` rows (FR-225)."""
    company_id = company["id"]
    found: list[dict] = []
    found.extend(signals_from_news(company_id, news_items or []))
    found.extend(signals_from_pages(company_id, pages or []))
    found.extend(signals_from_profile_news(company))
    found.extend(signals_from_financials(company_id))
    found.extend(signals_from_postings(company_id))
    found.extend(signals_from_fiscal_calendar(company))
    found.extend(signals_from_competitors(company_id))
    return [s for s in found if s.get("signal_type") in SIGNAL_TYPES]


def persist(signals: list[dict]) -> list[str]:
    """Write detected signals to the shared knowledge base, merging repeats."""
    written: list[str] = []
    for signal in signals:
        try:
            written.append(repo.upsert_signal(signal))
        except Exception:  # noqa: BLE001 - one malformed signal must not stop the rest
            log.exception("Could not store hiring signal %r", signal.get("signal_type"))
    return written


async def refresh_signals(
    company: dict,
    *,
    pages: list[Any] | None = None,
    news_items: list[Any] | None = None,
    feeds: list[str] | None = None,
    fetch_news: bool = True,
) -> list[dict]:
    """Collect, classify and store this company's hiring signals (FR-225).

    When ``news_items`` is not supplied the company newsroom is read through the
    RSS adapter; a site without a feed simply contributes nothing, which is why
    the other detectors do not depend on it.  ``feeds`` are the ``rel=alternate``
    feeds the crawl saw declared, which spares the adapter from guessing.
    """
    items = list(news_items or [])
    if fetch_news and not items and (company.get("domain") or company.get("source")):
        from dreamjob.adapters.news import rss  # noqa: PLC0415 - adapter imports the pipeline back

        home = company.get("source") or f"https://{company['domain']}"
        try:
            items = await rss.collect_news(home, declared_feeds=feeds)
        except Exception:  # noqa: BLE001 - the newsroom is optional
            log.debug("Newsroom unavailable for %s", company.get("domain"))
            items = []
    detected = detect_all(company, pages=pages, news_items=items)
    persist(detected)
    return repo.list_signals(company["id"])


# ---------------------------------------------------------------------------
# Timing intelligence (FR-402)
# ---------------------------------------------------------------------------


@dataclass
class TimingRecommendation:
    """A preferred application window, with the evidence behind it (FR-402)."""

    company_id: str
    timing_flag: str
    score: float
    window_start: str | None
    window_end: str | None
    rationale: str
    drivers: list[dict] = field(default_factory=list)
    computed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "timing_flag": self.timing_flag,
            "score": self.score,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "rationale": self.rationale,
            "drivers": self.drivers,
            "computed_at": self.computed_at,
        }


def _decay(days_old: float, half_life: float = 120.0) -> float:
    """Recent evidence counts for more; nothing older than a year counts much."""
    if days_old <= 0:
        return 1.0
    return 0.5 ** (days_old / half_life)


def recommend_window(
    company_id: str, *, signals: list[dict] | None = None, now: datetime | None = None
) -> TimingRecommendation:
    """Recommend when to apply, from the signals on record (FR-402).

    Each signal opens a window per :data:`TIMING_MODEL`.  Signals whose window
    covers today contribute to the score; signals whose window has not opened
    yet set the recommended start date instead, which is what turns
    "they raised money last week" into "apply from the end of the month".
    """
    moment = now or datetime.now(UTC)
    rows = signals if signals is not None else repo.list_signals(company_id, limit=100)

    active: list[tuple[float, dict, datetime, datetime]] = []
    upcoming: list[tuple[datetime, datetime, dict]] = []
    for row in rows:
        rule = TIMING_MODEL.get(row.get("signal_type") or "")
        if rule is None:
            continue
        occurred = _iso_date(row.get("occurred_at") or row.get("collected_at"))
        if not occurred:
            continue
        event = datetime.fromisoformat(occurred)
        opens = event + timedelta(days=rule.opens_after_days)
        closes = event + timedelta(days=rule.closes_after_days)
        if closes < moment:
            continue
        strength = float(row.get("strength") or 0.5)
        if opens <= moment:
            age = (moment - event).total_seconds() / 86_400.0
            contribution = rule.weight * strength * _decay(max(0.0, age))
            active.append((contribution, row, opens, closes))
        else:
            upcoming.append((opens, closes, row))

    # Diminishing returns: three converging signals are strong evidence, ten
    # near-identical press releases are not ten times stronger.
    active.sort(key=lambda a: a[0], reverse=True)
    score = 0.0
    for index, (contribution, _row, _opens, _closes) in enumerate(active):
        score += contribution * (0.6**index)
    score = round(min(1.0, score), 3)

    flag = DEFAULT_FLAG
    for threshold, name in FLAG_THRESHOLDS:
        if score >= threshold:
            flag = name
            break

    window_start: datetime | None = None
    window_end: datetime | None = None
    if active:
        window_start = moment
        window_end = max(closes for _c, _r, _o, closes in active)
    elif upcoming:
        upcoming.sort(key=lambda u: u[0])
        window_start, window_end, _row = upcoming[0]
        flag = "wait"

    drivers = [
        {
            "signal_type": row.get("signal_type"),
            "description": row.get("description"),
            "occurred_at": row.get("occurred_at"),
            "source_url": row.get("source_url"),
            "contribution": round(contribution, 3),
            "reason": TIMING_MODEL[row["signal_type"]].reason,
        }
        for contribution, row, _o, _c in active[:5]
    ]
    if not drivers and upcoming:
        opens, closes, row = upcoming[0]
        drivers = [
            {
                "signal_type": row.get("signal_type"),
                "description": row.get("description"),
                "occurred_at": row.get("occurred_at"),
                "source_url": row.get("source_url"),
                "contribution": 0.0,
                "reason": TIMING_MODEL[row["signal_type"]].reason,
            }
        ]

    return TimingRecommendation(
        company_id=company_id,
        timing_flag=flag,
        score=score,
        window_start=window_start.isoformat(timespec="seconds") if window_start else None,
        window_end=window_end.isoformat(timespec="seconds") if window_end else None,
        rationale=_rationale(flag, drivers, window_start, window_end),
        drivers=drivers,
        computed_at=moment.isoformat(timespec="seconds"),
    )


def _rationale(
    flag: str, drivers: list[dict], start: datetime | None, end: datetime | None
) -> str:
    if not drivers:
        return "No hiring or timing signals on record for this company yet."
    lead = "; ".join(f"{d['signal_type'].replace('_', ' ')} - {d['reason']}" for d in drivers[:3])
    if flag == "wait":
        opens = start.date().isoformat() if start else "later"
        return f"Signal recorded but the useful window opens around {opens}: {lead}."
    closes = end.date().isoformat() if end else "an open date"
    if flag == "apply_now":
        return f"Several converging signals point at active hiring until about {closes}: {lead}."
    if flag == "favourable":
        return f"The moment is favourable until about {closes}: {lead}."
    if flag == "watch":
        return f"Weak but real signals, worth watching until about {closes}: {lead}."
    return f"No decisive timing signal; the record shows: {lead}."


def timing_flag_for(company_id: str) -> str | None:
    """The value the ranked list stores in ``opportunity.timing_flag`` (FR-402).

    Only genuinely favourable moments are flagged, so that a flag on an
    opportunity means something when the job seeker sees it.
    """
    recommendation = recommend_window(company_id)
    if recommendation.timing_flag in ("apply_now", "favourable"):
        return recommendation.timing_flag
    return None


def timing_summary(company_id: str) -> dict[str, Any]:
    """Signals plus recommendation, as the company profile displays them."""
    signals = repo.list_signals(company_id, limit=100)
    recommendation = recommend_window(company_id, signals=signals)
    return {
        "signals": [_decode_signal(s) for s in signals],
        "timing": recommendation.as_dict(),
    }


def _decode_signal(row: dict) -> dict:
    return dict(row, strength=float(row.get("strength") or 0))
