"""Shared knowledge base: writer, freshness policy and reuse (FR-341..345).

The knowledge base is the reason a second campaign is cheaper than the first.
Three things live here:

* **The writer** - the single way a collected record enters a shared table.
  It de-duplicates (FR-184), merges rather than overwrites, keeps the FTS
  index in step (FR-345), links the record to the plan item that produced it
  (FR-166) and refuses to store any link back to the job seeker (FR-344).
* **The staleness policy** (FR-343) - per record type, configurable, with the
  specified defaults: vacancies 7 days, company profiles 90 days, filings one
  year.  It is stored in ``app_setting`` so an administrator can change it
  without a deployment.
* **The reuse assessment** (FR-342) - before anything is collected, the plan is
  compared against what the knowledge base already holds fresh, plan items that
  are already satisfied are skipped, and the saving is reported to the user in
  concrete terms: per entity type, how many records were reused rather than
  re-collected, and the collection time and cost that avoided.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from dreamjob.db.connection import from_json, utcnow
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import knowledge as repo
from dreamjob.pipeline import dedup

log = logging.getLogger(__name__)

STALENESS_SETTING_KEY = "kb.staleness_policy"

# FR-343 defaults, in days, per record type.
DEFAULT_STALENESS_DAYS: dict[str, int] = {
    "vacancy": 7,
    "company": 90,
    "contact": 180,
    "financial_year": 365,
    "hiring_signal": 30,
    "competitor_link": 180,
    "event": 30,
}

# Which entity a source of each type contributes to the knowledge base.
ENTITY_FOR_SOURCE_TYPE: dict[str, str | None] = {
    "job_board": "vacancy",
    "ats": "vacancy",
    "directory": "company",
    "registry": "company",
    "website": "company",
    "linkedin": "company",
    "news": "hiring_signal",
    "events": "event",
    "compensation": None,
}

DEFAULT_RECORDS_PER_PAGE = 25


# ---------------------------------------------------------------------------
# Staleness policy (FR-343)
# ---------------------------------------------------------------------------


def get_staleness_policy() -> dict[str, int]:
    stored = repo.get_setting(STALENESS_SETTING_KEY, {}) or {}
    policy = dict(DEFAULT_STALENESS_DAYS)
    if isinstance(stored, dict):
        for key, value in stored.items():
            try:
                policy[str(key)] = max(0, int(value))
            except (TypeError, ValueError):
                log.warning("Ignoring non-numeric staleness policy entry %r=%r", key, value)
    return policy


def set_staleness_policy(updates: dict[str, Any]) -> dict[str, int]:
    """Persist a partial policy change; unknown record types are rejected."""
    policy = get_staleness_policy()
    for key, value in updates.items():
        if key not in DEFAULT_STALENESS_DAYS:
            raise ValueError(f"Unknown record type {key!r} for the staleness policy")
        policy[key] = max(0, int(value))
    repo.set_setting(STALENESS_SETTING_KEY, policy)
    return policy


def cutoff_for(entity_type: str, policy: dict[str, int] | None = None) -> str:
    """The oldest ``collected_at`` that still counts as fresh for this type."""
    days = (policy or get_staleness_policy()).get(entity_type, 30)
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def is_stale(
    entity_type: str, collected_at: str | None, policy: dict[str, int] | None = None
) -> bool:
    if not collected_at:
        return True
    return str(collected_at) < cutoff_for(entity_type, policy)


# ---------------------------------------------------------------------------
# Writer (FR-166, FR-184, FR-341, FR-344)
# ---------------------------------------------------------------------------


@dataclass
class WriteOutcome:
    entity_type: str
    entity_id: str
    created: bool
    matched_on: str | None = None


# Natural keys for the shared tables that have no fuzzy identity of their own.
_CONFLICT_KEYS: dict[str, tuple[str, ...]] = {
    "financial_year": ("company_id", "fiscal_year"),
    "competitor_link": ("company_id", "peer_company_id", "basis"),
    "hiring_signal": ("company_id", "signal_type", "occurred_at"),
    "event": ("name", "starts_at"),
    "contact": ("company_id", "email"),
}

_EMPTY = (None, "", [], {})


class KnowledgeBaseWriter:
    """Writes normalised records into the shared knowledge base.

    One writer is created per source plan item, so every row it produces can be
    traced back to the plan item and adapter that produced it (FR-166).
    """

    def __init__(
        self,
        *,
        adapter_key: str | None = None,
        plan_item_id: str | None = None,
        campaign_id: str | None = None,
        policy: dict[str, int] | None = None,
    ):
        self.adapter_key = adapter_key
        self.plan_item_id = plan_item_id
        self.campaign_id = campaign_id
        self.policy = policy or get_staleness_policy()
        self.stats: dict[str, dict[str, int]] = {}
        self.company_ids: set[str] = set()
        self.people_ids: set[str] = set()

    # -- public API ---------------------------------------------------------
    def write(self, record: Any) -> WriteOutcome | None:
        """Persist one ``NormalisedRecord`` (or an equivalent dict)."""
        entity_type, data, confidence, raw_document_id = _unpack(record)
        if not entity_type or not isinstance(data, dict) or not data:
            return None
        data = self._strip_private(entity_type, dict(data))
        try:
            if entity_type == "company":
                outcome = self._write_company(data, confidence)
            elif entity_type == "vacancy":
                outcome = self._write_vacancy(data, confidence)
            else:
                outcome = self._write_generic(entity_type, data, confidence)
        except Exception:  # noqa: BLE001 - one bad record must not stop a campaign
            log.exception("Knowledge-base write failed for a %s record", entity_type)
            return None
        if outcome is None:
            return None
        self._count(outcome)
        repo.record_provenance(
            outcome.entity_type,
            outcome.entity_id,
            source_plan_item_id=self.plan_item_id,
            raw_document_id=raw_document_id,
            adapter_key=self.adapter_key,
            confidence=confidence,
        )
        return outcome

    def write_many(self, records: list[Any]) -> list[WriteOutcome]:
        return [o for o in (self.write(r) for r in records) if o is not None]

    @property
    def total_written(self) -> int:
        return sum(c["created"] + c["updated"] for c in self.stats.values())

    def summary(self) -> dict[str, Any]:
        return {
            "by_entity": self.stats,
            "companies": len(self.company_ids),
            "people": len(self.people_ids),
            "total": self.total_written,
        }

    # -- internals ----------------------------------------------------------
    def _strip_private(self, entity_type: str, data: dict) -> dict:
        """FR-344: a shared row never carries a link back to a job seeker."""
        for key in list(data):
            if key.lower() in repo.FORBIDDEN_SHARED_COLUMNS:
                log.warning(
                    "Adapter %s tried to store %s on a shared %s row; dropped (FR-344)",
                    self.adapter_key, key, entity_type,
                )
                data.pop(key)
        return data

    def _count(self, outcome: WriteOutcome) -> None:
        bucket = self.stats.setdefault(outcome.entity_type, {"created": 0, "updated": 0})
        bucket["created" if outcome.created else "updated"] += 1
        if outcome.entity_type == "company":
            self.company_ids.add(outcome.entity_id)
        elif outcome.entity_type == "contact":
            self.people_ids.add(outcome.entity_id)

    def _write_company(self, data: dict, confidence: float) -> WriteOutcome | None:
        data = _normalise_company_fields(data)
        if not data.get("name"):
            return None
        if self.adapter_key:
            data.setdefault("source", self.adapter_key)
        existing, matched_on = resolve_company(data)
        now = utcnow()
        if existing:
            merged = _merge(existing, data)
            merged["refreshed_at"] = now
            merged["confidence"] = max(
                float(existing.get("confidence") or 0), float(confidence or 0)
            )
            repo.update_company(existing["id"], merged)
            return WriteOutcome("company", existing["id"], created=False, matched_on=matched_on)
        data.setdefault("collected_at", now)
        data["refreshed_at"] = now
        data.setdefault("confidence", confidence)
        return WriteOutcome("company", repo.insert_company(data), created=True)

    def _write_vacancy(self, data: dict, confidence: float) -> WriteOutcome | None:
        if not data.get("title"):
            return None
        data = dict(data)
        if not data.get("company_id") and data.get("company_name_raw"):
            company, _ = resolve_company(
                {"name": data["company_name_raw"], "country": data.get("country")}
            )
            if company:
                data["company_id"] = company["id"]
        dedup.assign_dedup_key(data)
        if self.adapter_key:
            data.setdefault("source_adapter", self.adapter_key)

        existing = repo.vacancy_by_dedup_key(data["dedup_key"])
        matched_on = "dedup_key" if existing else None
        if existing is None:
            # A key miss still leaves the fuzzy comparison (FR-184): the same
            # posting re-collected across a week boundary keys differently.
            candidates = repo.vacancy_candidates(
                data.get("company_id"),
                data.get("company_name_raw"),
                cutoff_for("vacancy", self.policy),
            )
            existing, score = dedup.best_vacancy_match(data, candidates)
            matched_on = f"fuzzy:{score:.2f}" if existing else None

        now = utcnow()
        if existing:
            merged = _merge(existing, data)
            merged["collected_at"] = now
            merged["confidence"] = max(
                float(existing.get("confidence") or 0), float(confidence or 0)
            )
            repo.update_vacancy(existing["id"], merged)
            return WriteOutcome("vacancy", existing["id"], created=False, matched_on=matched_on)
        data["collected_at"] = now
        data.setdefault("confidence", confidence)
        return WriteOutcome("vacancy", repo.insert_vacancy(data), created=True)

    def _write_generic(
        self, entity_type: str, data: dict, confidence: float
    ) -> WriteOutcome | None:
        if entity_type not in repo.WRITABLE_TABLES:
            log.warning("Ignoring record for unknown shared entity type %r", entity_type)
            return None
        if entity_type == "contact" and not self._prepare_contact(data):
            return None
        keys = _CONFLICT_KEYS.get(entity_type, ())
        existing = None
        if keys and all(data.get(k) is not None for k in keys):
            existing = repo.find_shared(entity_type, {k: data.get(k) for k in keys})
        if "confidence" in repo.columns(entity_type):
            data.setdefault("confidence", confidence)
        if existing:
            merged = _merge(existing, data)
            merged["collected_at"] = utcnow()
            repo.update_shared(entity_type, existing["id"], merged)
            return WriteOutcome(
                entity_type, existing["id"], created=False, matched_on="natural_key"
            )
        data.setdefault("collected_at", utcnow())
        return WriteOutcome(entity_type, repo.insert_shared(entity_type, data), created=True)

    def _prepare_contact(self, data: dict) -> bool:
        """NFR-302/NFR-303: honour objections and keep browser finds campaign-scoped."""
        email = data.get("email")
        if email:
            known = repo.contact_by_email(email)
            if known and known.get("objected"):
                log.info("Skipping contact %s: objection recorded (NFR-302)", email)
                return False
        if data.get("access_method") == "browser" and self.campaign_id:
            data.setdefault("shareable", 0)
            data.setdefault("owning_campaign_id", self.campaign_id)
        return True


def _unpack(record: Any) -> tuple[str, dict, float, str | None]:
    if hasattr(record, "entity_type") and hasattr(record, "data"):
        return (
            getattr(record, "entity_type", ""),
            getattr(record, "data", {}) or {},
            float(getattr(record, "confidence", 0.7) or 0.7),
            getattr(record, "raw_document_id", None),
        )
    if isinstance(record, dict):
        return (
            record.get("entity_type", ""),
            record.get("data", {}) or {},
            float(record.get("confidence", 0.7) or 0.7),
            record.get("raw_document_id"),
        )
    return "", {}, 0.7, None


def _normalise_company_fields(data: dict) -> dict:
    data = dict(data)
    if data.get("website") and not data.get("domain"):
        data["domain"] = data.pop("website")
    domain = dedup.normalise_domain(data.get("domain"))
    if domain:
        data["domain"] = domain
    elif "domain" in data:
        data.pop("domain")
    if data.get("legal_id"):
        data["legal_id"] = dedup.normalise_legal_id(data["legal_id"], data.get("legal_id_type"))
    if data.get("vat_number"):
        data["vat_number"] = dedup.normalise_legal_id(data["vat_number"], "vat")
    data["normalised_name"] = dedup.normalise_company_name(data.get("name"))
    if data.get("country"):
        data["country"] = str(data["country"]).upper()[:2]
    return data


def resolve_company(data: dict) -> tuple[dict | None, str | None]:
    """Find the knowledge-base row this company record belongs to (FR-184).

    Identifier first, then VAT, then domain, then exact normalised name, and
    only then a fuzzy name match inside the same jurisdiction.
    """
    for key in dedup.company_keys(data):
        if key.kind == "legal_id":
            found = repo.company_by_legal_id(key.value, key.extra)
        elif key.kind == "vat":
            found = repo.company_by_vat(key.value)
        elif key.kind == "domain":
            found = repo.company_by_domain(key.value)
        else:
            found = None
            rows = repo.companies_by_normalised_name(key.value, key.extra)
            if rows:
                found = rows[0]
        if found:
            return found, key.kind
    nname = data.get("normalised_name") or dedup.normalise_company_name(data.get("name"))
    if nname:
        first = nname.split(" ")[0]
        if len(first) >= 3:
            candidates = repo.company_name_candidates(first, (data.get("country") or "").upper())
            match, score = dedup.best_company_match(data, candidates)
            if match:
                return match, f"fuzzy_name:{score:.2f}"
    return None, None


def _merge(existing: dict, incoming: dict) -> dict:
    """Incoming values win, except where they would erase what we already know."""
    out: dict[str, Any] = {}
    for key, value in incoming.items():
        if key in ("id", "collected_at"):
            continue
        if value in _EMPTY:
            continue
        current = existing.get(key)
        if current not in _EMPTY and current == value:
            continue
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# Reuse assessment (FR-342)
# ---------------------------------------------------------------------------


@dataclass
class ReuseDecision:
    plan_item_id: str
    adapter_key: str
    entity_type: str | None
    action: str  # collect | collect_partial | skip
    expected_records: int
    reused_records: int
    pages_before: int
    pages_after: int
    seconds_saved: int
    cost_saved_eur: float
    reason: str


@dataclass
class ReuseReport:
    generated_at: str = field(default_factory=utcnow)
    policy: dict[str, int] = field(default_factory=dict)
    per_entity: dict[str, dict[str, int]] = field(default_factory=dict)
    decisions: list[ReuseDecision] = field(default_factory=list)
    seconds_saved: int = 0
    cost_saved_eur: float = 0.0
    fresh_in_knowledge_base: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "policy_days": self.policy,
            "per_entity": self.per_entity,
            "fresh_in_knowledge_base": self.fresh_in_knowledge_base,
            "estimated_seconds_saved": self.seconds_saved,
            "estimated_cost_saved_eur": round(self.cost_saved_eur, 4),
            "decisions": [d.__dict__ for d in self.decisions],
            "headline": self.headline(),
        }

    def headline(self) -> str:
        """The one sentence FR-342 asks to be shown to the job seeker."""
        parts = [
            f"{v['reused']} {k} record(s) reused, {v['scheduled']} still to collect"
            for k, v in sorted(self.per_entity.items())
        ]
        if not parts or not self.seconds_saved:
            return "Knowledge base: nothing reusable yet; the full plan will be collected."
        duration = (
            f"{self.seconds_saved // 60} min"
            if self.seconds_saved >= 90
            else f"{self.seconds_saved} s"
        )
        cost = (
            f"EUR {self.cost_saved_eur:.2f}"
            if self.cost_saved_eur >= 0.01
            else f"EUR {self.cost_saved_eur:.4f}"
        )
        return (
            "Knowledge base: "
            + "; ".join(parts)
            + f" - saving about {duration} of collection and {cost}."
        )


def assess_reuse(
    campaign_id: str,
    *,
    countries: list[str] | None = None,
    apply_decisions: bool = True,
    policy: dict[str, int] | None = None,
) -> ReuseReport:
    """Compare the plan against the knowledge base and skip what is already fresh.

    FR-342: only what is missing or stale is scheduled, and the saving is
    reported.  A source whose fresh contribution already covers the volume its
    plan item would have produced is marked ``skipped``; a partially covered
    source keeps a proportionally smaller page budget.
    """
    policy = policy or get_staleness_policy()
    report = ReuseReport(policy=policy)
    items = campaign_repo.list_plan_items(campaign_id, include_excluded=False)
    catalogue = {c["adapter_key"]: c for c in campaign_repo.list_catalogue(enabled_only=False)}

    fresh_by_type: dict[str, dict[str, int]] = {}
    for entity_type in ("vacancy", "company"):
        cutoff = cutoff_for(entity_type, policy)
        fresh_by_type[entity_type] = repo.fresh_counts_by_adapter(entity_type, cutoff)
        report.fresh_in_knowledge_base[entity_type] = repo.count_fresh(
            entity_type, cutoff, countries
        )
    vacancy_cutoff = cutoff_for("vacancy", policy)
    by_source = repo.fresh_vacancy_counts_by_source(vacancy_cutoff, countries)
    for key, value in by_source.items():
        if key:
            fresh_by_type["vacancy"][key] = max(fresh_by_type["vacancy"].get(key, 0), value)

    for item in items:
        entry = catalogue.get(item["adapter_key"], {})
        entity_type = ENTITY_FOR_SOURCE_TYPE.get(entry.get("source_type") or "", None)
        item_caps = item.get("caps") if isinstance(item.get("caps"), dict) else {}
        # Assess against the page budget the planner set, not against the one a
        # previous assessment already cut: re-running must be idempotent.
        pages = int(item_caps.get("planned_pages") or item.get("estimated_pages") or 1)
        per_page = _records_per_page(entry, item)
        expected = max(1, pages * per_page)
        seconds = int(item_caps.get("planned_seconds") or item.get("estimated_seconds") or 0)
        cost = float(item_caps.get("planned_cost_eur") or item.get("estimated_cost_eur") or 0.0)

        reused = 0
        if entity_type in ("vacancy", "company"):
            reused = min(expected, int(fresh_by_type[entity_type].get(item["adapter_key"], 0)))

        if entity_type is None or reused == 0:
            action, pages_after = "collect", pages
            reason = (
                "no comparable fresh records in the knowledge base"
                if entity_type
                else "source does not feed a reusable record type"
            )
        elif reused >= expected:
            action, pages_after = "skip", 0
            reason = (
                f"{reused} fresh {entity_type} record(s) from this source are within the "
                f"{policy.get(entity_type)}-day staleness window"
            )
        else:
            action = "collect_partial"
            pages_after = max(1, math.ceil((expected - reused) / per_page))
            reason = f"{reused} of about {expected} {entity_type} record(s) already fresh"

        saved_pages = max(0, pages - pages_after)
        seconds_saved = int(seconds * saved_pages / pages) if pages else 0
        cost_saved = cost * saved_pages / pages if pages else 0.0

        decision = ReuseDecision(
            plan_item_id=item["id"],
            adapter_key=item["adapter_key"],
            entity_type=entity_type,
            action=action,
            expected_records=expected,
            reused_records=reused,
            pages_before=pages,
            pages_after=pages_after,
            seconds_saved=seconds_saved,
            cost_saved_eur=round(cost_saved, 4),
            reason=reason,
        )
        report.decisions.append(decision)
        report.seconds_saved += seconds_saved
        report.cost_saved_eur += cost_saved
        if entity_type:
            bucket = report.per_entity.setdefault(entity_type, {"reused": 0, "scheduled": 0})
            bucket["reused"] += reused
            bucket["scheduled"] += max(0, expected - reused) if action != "skip" else 0

        if apply_decisions:
            values: dict[str, Any] = {
                "estimated_pages": pages_after,
                "estimated_seconds": seconds - seconds_saved,
                "estimated_cost_eur": round(cost - cost_saved, 4),
                "caps": {
                    **item_caps,
                    "planned_pages": pages,
                    "planned_seconds": seconds,
                    "planned_cost_eur": cost,
                },
            }
            if action == "skip":
                values["status"] = "skipped"
                values["last_error"] = None
            elif item.get("status") == "skipped":
                values["status"] = "planned"
            campaign_repo.update_plan_item(item["id"], values)

    if apply_decisions:
        campaign_repo.update_campaign(campaign_id, {"reuse_report": report.to_dict()})
    return report


def _records_per_page(entry: dict, item: dict) -> int:
    caps = item.get("caps") or {}
    if isinstance(caps, dict) and caps.get("records_per_page"):
        return max(1, int(caps["records_per_page"]))
    capabilities = entry.get("query_capabilities") or {}
    if isinstance(capabilities, str):
        capabilities = from_json(capabilities, {}) or {}
    value = capabilities.get("max_results_per_query")
    try:
        return max(1, min(100, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_RECORDS_PER_PAGE


def reuse_report(campaign_id: str) -> dict | None:
    campaign = campaign_repo.get_campaign_any(campaign_id)
    return (campaign or {}).get("reuse_report")


# ---------------------------------------------------------------------------
# Campaign-independent browse / search (FR-345)
# ---------------------------------------------------------------------------

_COMPANY_JSON = (
    "products_services", "markets", "sector_codes", "locations", "structure",
    "key_people", "reference_customers", "tech_stack", "values_culture", "news",
)
_VACANCY_JSON = ("required_skills", "desirable_skills")


def _decode_row(row: dict, json_columns: tuple[str, ...]) -> dict:
    out = dict(row)
    for col in json_columns:
        if col in out:
            out[col] = from_json(out[col], None)
    return out


def browse_companies(
    q: str | None = None,
    *,
    country: str | None = None,
    sector: str | None = None,
    size_band: str | None = None,
    limit: int = 25,
    offset: int = 0,
    policy: dict[str, int] | None = None,
) -> dict:
    """Search the shared company knowledge base, independent of any campaign."""
    rows = repo.search_companies(
        q, country=country, sector=sector, size_band=size_band, limit=limit, offset=offset
    )
    policy = policy or get_staleness_policy()
    items = []
    for row in rows:
        item = _decode_row(row, _COMPANY_JSON)
        item.pop("rank", None)
        freshness = row.get("refreshed_at") or row.get("collected_at")
        item["stale"] = is_stale("company", freshness, policy)
        items.append(item)
    return {
        "total": repo.count_companies(q, country=country, sector=sector, size_band=size_band),
        "limit": limit,
        "offset": offset,
        "items": items,
    }


def browse_vacancies(
    q: str | None = None,
    *,
    country: str | None = None,
    company_id: str | None = None,
    fresh_only: bool = False,
    limit: int = 25,
    offset: int = 0,
    policy: dict[str, int] | None = None,
) -> dict:
    """Search the shared vacancy knowledge base, independent of any campaign."""
    policy = policy or get_staleness_policy()
    since = cutoff_for("vacancy", policy) if fresh_only else None
    rows = repo.search_vacancies(
        q, country=country, company_id=company_id, since=since, limit=limit, offset=offset
    )
    items = []
    for row in rows:
        item = _decode_row(row, _VACANCY_JSON)
        item.pop("rank", None)
        item["stale"] = is_stale("vacancy", row.get("collected_at"), policy)
        items.append(item)
    return {
        "total": repo.count_vacancies(q, country=country, company_id=company_id, since=since),
        "limit": limit,
        "offset": offset,
        "items": items,
    }


def company_detail(company_id: str) -> dict | None:
    row = repo.get_company(company_id)
    if row is None:
        return None
    detail = _decode_row(row, _COMPANY_JSON)
    detail["provenance"] = repo.provenance_for("company", company_id)
    return detail


def vacancy_detail(vacancy_id: str) -> dict | None:
    row = repo.get_vacancy(vacancy_id)
    if row is None:
        return None
    detail = _decode_row(row, _VACANCY_JSON)
    detail["provenance"] = repo.provenance_for("vacancy", vacancy_id)
    return detail
