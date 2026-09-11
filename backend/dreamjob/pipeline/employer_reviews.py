"""Employer-review signals from a company's own pages (FR-265, FR-384).

The consuming path to employer reviews already existed - ``compensation`` reads
``employer_review_summary`` and folds the rating into the advisory block, and
``company_profile`` writes review *themes* into ``values_culture`` - but nothing
ever produced a row, so ``opportunity.employer_rating`` was always null.

The review sites that would be the obvious source (Glassdoor, Indeed) forbid
automated access (IR-101, Data_Gathering_Plan section 6), so this adapter reads
the one source the product is already permitted to read: the company's own
website, which the profile crawl has already stored.  Many organisations publish
an ``aggregateRating`` in schema.org JSON-LD, or a review widget with the same
shape, and that is a claim the company itself makes about itself - a weaker
signal than an independent review corpus, and recorded with the confidence that
reflects it.

Nothing here makes a request: it reads pages the crawler already fetched
(FR-183), so it costs no egress and cannot be blocked.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from dreamjob.config import get_settings
from dreamjob.db.repositories import companies as company_repo
from dreamjob.db.repositories import opportunities as opp_repo

log = logging.getLogger(__name__)

SOURCE = "company_site"

#: Confidence in a self-published rating: it is the company's own claim, not an
#: independent sampling of its employees (NFR-402).
SELF_PUBLISHED_CONFIDENCE = 0.4

_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_RATING_TYPES = {"Organization", "Corporation", "LocalBusiness", "EmployerAggregateRating"}


def _number(value: Any) -> float | None:
    try:
        number = float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _walk(node: Any, out: list[dict]) -> None:
    """Find every ``aggregateRating`` in a JSON-LD document, at any depth."""
    if isinstance(node, dict):
        type_value = node.get("@type")
        types = {type_value} if isinstance(type_value, str) else set(type_value or [])
        rating = node.get("aggregateRating")
        if isinstance(rating, dict) and (types & _RATING_TYPES or not types):
            value = _number(rating.get("ratingValue"))
            count = _number(rating.get("reviewCount") or rating.get("ratingCount"))
            if value is not None:
                scale = _number(rating.get("bestRating")) or 5.0
                out.append({"rating": value, "scale": scale, "review_count": int(count or 0)})
        for value in node.values():
            _walk(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk(item, out)


def extract_ratings(html: str | None) -> list[dict]:
    """Every schema.org aggregate rating a page publishes (pure, no DB)."""
    out: list[dict] = []
    for match in _JSONLD_RE.finditer(html or ""):
        try:
            payload = json.loads(match.group(1).strip())
        except (ValueError, TypeError):
            continue
        _walk(payload, out)
    # One page can repeat the same rating in several blocks; keep the first.
    seen: set[tuple[float, float]] = set()
    unique: list[dict] = []
    for item in out:
        key = (item["rating"], item["scale"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def build_for_company(company_id: str) -> dict[str, Any]:
    """Read a company's stored pages and record any rating they publish."""
    pages = company_repo.crawled_pages(company_id, limit=80)
    root = get_settings().abs_data_dir
    for page in pages:
        storage = page.get("storage_path")
        if not storage:
            continue
        try:
            html = (root / str(storage)).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        ratings = extract_ratings(html)
        if not ratings:
            continue
        best = max(ratings, key=lambda r: r.get("review_count") or 0)
        opp_repo.record_employer_review(
            {
                "company_id": company_id,
                "source": SOURCE,
                "source_url": page.get("url"),
                "rating": best["rating"],
                "rating_scale_max": best["scale"],
                "review_count": best["review_count"],
                "themes": json.dumps([]),
                "access_method": "http",
                "confidence": SELF_PUBLISHED_CONFIDENCE,
            }
        )
        return {"company_id": company_id, "rating": best["rating"], "source": SOURCE}
    return {"company_id": company_id, "rating": None}
