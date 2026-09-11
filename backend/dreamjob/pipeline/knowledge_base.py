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
from dreamjob.egress import client as egress_client
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
    # A board slug is a fact about the world that decays: of the slugs seen in
    # the current Common Crawl 87.5% still answered, against 27.5% of the ones
    # only Wayback remembered (docs/Data_Gathering_Plan.md section 2.3).  A
    # month is short enough to keep the registry honest and long enough that
    # liveness is re-verified in the monthly refresh, not in a campaign.
    "board_registry": 30,
    # A national earnings survey is published every four years and is not
    # superseded in between, so a year-old row is current, not stale (FR-343).
    "compensation_observation": 1460,
    # Whether an employer is an interim agency changes only when the company
    # itself changes trade; six months matches the website rung's own
    # expiry (docs/Interim_Agencies_Proposal.md C6).
    "employer_kind": 180,
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
    "compensation": "compensation_observation",
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


@dataclass
class WriteFailure:
    """A record the writer refused or could not store.

    Counting these is what stops a plan item whose every record was rejected
    from being reported as "done, 0 records, 0 errors" (FR-166, NFR-403): the
    collection worker reads :attr:`KnowledgeBaseWriter.failures` and bumps the
    plan item's error count from it.
    """

    entity_type: str
    reason: str  # unusable_record | rejected | write_error
    detail: str = ""

    def describe(self) -> str:
        return f"{self.entity_type or 'unknown'}: {self.reason}" + (
            f" ({self.detail})" if self.detail else ""
        )


# Natural keys for the shared tables that have no fuzzy identity of their own.
_CONFLICT_KEYS: dict[str, tuple[str, ...]] = {
    "financial_year": ("company_id", "fiscal_year"),
    "competitor_link": ("company_id", "peer_company_id", "basis"),
    "hiring_signal": ("company_id", "signal_type", "occurred_at"),
    "event": ("name", "starts_at"),
    "contact": ("company_id", "email"),
    # One row per occupation per country per survey: re-running the adapter
    # refreshes the figures rather than stacking another copy beside them.
    "compensation_observation": ("source", "source_name", "normalised_title", "country"),
}

_EMPTY = (None, "", [], {})

# An employer name read off a job advert identifies a company, but weakly: it
# has no identifier, no domain and no jurisdiction beyond the vacancy's own.
VACANCY_COMPANY_CONFIDENCE = 0.5


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
        # Records that never reached a table.  A silent drop is a failure, and
        # a failure has to be visible to the plan item that produced it.
        self.failures: list[WriteFailure] = []

    # -- public API ---------------------------------------------------------
    def write(self, record: Any) -> WriteOutcome | None:
        """Persist one ``NormalisedRecord`` (or an equivalent dict).

        Returns ``None`` when nothing was stored - and records *why* in
        :attr:`failures`, so a source whose every record was refused can never
        look like a source that simply found nothing (FR-166, NFR-403).
        """
        entity_type, data, confidence, raw_document_id = _unpack(record)
        if not entity_type or not isinstance(data, dict) or not data:
            self._fail(
                entity_type,
                "unusable_record",
                f"expected an entity_type and a non-empty data dict, got {type(record).__name__}",
            )
            return None
        data = self._strip_private(entity_type, dict(data))
        try:
            if entity_type == "company":
                outcome = self._write_company(data, confidence)
            elif entity_type == "vacancy":
                outcome = self._write_vacancy(data, confidence, raw_document_id)
            else:
                outcome = self._write_generic(entity_type, data, confidence)
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop a campaign
            log.exception("Knowledge-base write failed for a %s record", entity_type)
            self._fail(entity_type, "write_error", f"{type(exc).__name__}: {exc}")
            return None
        if outcome is None:
            self._fail(entity_type, "rejected", "the record carried no usable identity")
            return None
        self._count(outcome)
        self._record_provenance(outcome, raw_document_id, confidence)
        return outcome

    def _record_provenance(
        self, outcome: WriteOutcome, raw_document_id: str | None, confidence: float
    ) -> None:
        repo.record_provenance(
            outcome.entity_type,
            outcome.entity_id,
            source_plan_item_id=self.plan_item_id,
            raw_document_id=raw_document_id,
            adapter_key=self.adapter_key,
            confidence=confidence,
        )

    def _fail(self, entity_type: str, reason: str, detail: str = "") -> None:
        failure = WriteFailure(entity_type or "", reason, detail)
        self.failures.append(failure)
        log.warning("Knowledge-base write dropped a record - %s", failure.describe())

    def write_many(self, records: list[Any]) -> list[WriteOutcome]:
        return [o for o in (self.write(r) for r in records) if o is not None]

    @property
    def total_written(self) -> int:
        return sum(c["created"] + c["updated"] for c in self.stats.values())

    @property
    def total_failed(self) -> int:
        return len(self.failures)

    @property
    def last_failure(self) -> str | None:
        return self.failures[-1].describe() if self.failures else None

    def summary(self) -> dict[str, Any]:
        return {
            "by_entity": self.stats,
            "companies": len(self.company_ids),
            "people": len(self.people_ids),
            "total": self.total_written,
            "failed": self.total_failed,
            "failures": [f.describe() for f in self.failures[:20]],
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

    def _write_vacancy(
        self, data: dict, confidence: float, raw_document_id: str | None = None
    ) -> WriteOutcome | None:
        if not data.get("title"):
            return None
        data = dict(data)
        if not data.get("company_id") and data.get("company_name_raw"):
            data["company_id"] = self._company_for_vacancy(data, raw_document_id)
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
            # FR-184: a merge enriches the posting we already hold, it does
            # not re-label it.  A different title is a different opening, so the
            # surviving row keeps the title it was created with - overwriting it
            # is what turns a bad match into an invisible one.
            merged.pop("title", None)
            merged["collected_at"] = now
            merged["confidence"] = max(
                float(existing.get("confidence") or 0), float(confidence or 0)
            )
            repo.update_vacancy(existing["id"], merged)
            return WriteOutcome("vacancy", existing["id"], created=False, matched_on=matched_on)
        data["collected_at"] = now
        data.setdefault("confidence", confidence)
        return WriteOutcome("vacancy", repo.insert_vacancy(data), created=True)

    def _company_for_vacancy(self, data: dict, raw_document_id: str | None) -> str | None:
        """The knowledge-base company this posting belongs to, creating it if new.

        A vacancy names its employer, and until this ran nothing turned that
        name into a ``company`` row: the ATS and board adapters - the ones that
        actually retrieve - wrote vacancies with ``company_id`` NULL, so
        ``financial_year``, ``hiring_signal`` and ``competitor_link`` (all
        ``company_id NOT NULL``) could never be populated and no company ever
        acquired an ``ats_vendor``/``ats_slug`` for the next campaign to read.
        """
        # The identity claim is the employer's name and nothing else.  A
        # vacancy's country is where the *job* is, not where the company is
        # registered: stamping it on the company row makes one employer with
        # openings in five countries five companies.
        seed: dict[str, Any] = {"name": data.get("company_name_raw")}
        if data.get("access_method"):
            seed["access_method"] = data["access_method"]  # FR-207: how it was obtained
        company, _ = resolve_company(seed)
        if company:
            return str(company["id"])
        # A name off an advert is a weaker claim than a registry or a website
        # crawl, so it enters with a low confidence and is merged, not trusted.
        outcome = self._write_company(dict(seed), VACANCY_COMPANY_CONFIDENCE)
        if outcome is None:
            return None
        self._count(outcome)
        self._record_provenance(outcome, raw_document_id, VACANCY_COMPANY_CONFIDENCE)
        return outcome.entity_id

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

    # FR-342, N4: how many plan items each source carries decides *which*
    # question freshness is.  A keyword source is planned once, so "has this
    # source been read" and "has this item been read" are the same question and
    # the corpus count answers both.  A per-target source - every ATS board,
    # every EURES partition - is planned thousands of times, and the corpus
    # count then answers a question nobody asked: one Greenhouse board read on
    # Monday marked all 3,400 of them fresh, so every later campaign skipped
    # boards it had never once fetched.  Those are asked of the fetch ledger,
    # which holds one row per target actually read.
    items_per_adapter: dict[str, int] = {}
    for item in items:
        items_per_adapter[item["adapter_key"]] = items_per_adapter.get(item["adapter_key"], 0) + 1
    fresh_targets_cache: dict[str, set[str]] = {}

    def fresh_targets_for(adapter_key: str, entity_type: str) -> set[str]:
        if adapter_key not in fresh_targets_cache:
            days = policy.get(entity_type) or DEFAULT_STALENESS_DAYS.get(entity_type) or 7
            try:
                fresh_targets_cache[adapter_key] = egress_client.fresh_targets(
                    adapter_key, float(days)
                )
            except Exception:  # noqa: BLE001 - no ledger is "nothing is fresh", never a failure
                log.warning("Could not read the fetch ledger for %s", adapter_key, exc_info=True)
                fresh_targets_cache[adapter_key] = set()
        return fresh_targets_cache[adapter_key]

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
        per_target = entity_type is not None and items_per_adapter[item["adapter_key"]] > 1
        target_fresh = False
        if per_target:
            target = _target_key_for(item)
            target_fresh = target in fresh_targets_for(item["adapter_key"], entity_type)
            reused = expected if target_fresh else 0
        elif entity_type in ("vacancy", "company"):
            reused = min(expected, int(fresh_by_type[entity_type].get(item["adapter_key"], 0)))

        if entity_type is None:
            action, pages_after = "collect", pages
            reason = "source does not feed a reusable record type"
        elif per_target:
            if target_fresh:
                action, pages_after = "skip", 0
                reason = (
                    f"this target was read inside the {policy.get(entity_type)}-day staleness "
                    "window"
                )
            else:
                action, pages_after = "collect", pages
                reason = "this target has not been read inside the staleness window"
        elif reused == 0:
            action, pages_after = "collect", pages
            reason = "no comparable fresh records in the knowledge base"
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

    # FR-342: the headline "reused" figure is bounded by what the knowledge base
    # actually holds.  A per-target plan item used to add its whole expected page
    # yield to the total, so one campaign over 4,500 boards reported 526,595
    # reused vacancies against a 55,057-row corpus - a claimed saving larger than
    # the entire knowledge base.  Capping the aggregate at the measured fresh
    # count keeps the headline one a person can check.
    for entity_type, bucket in report.per_entity.items():
        cap = int(report.fresh_in_knowledge_base.get(entity_type) or 0)
        if cap and bucket.get("reused", 0) > cap:
            bucket["reused"] = cap

    if apply_decisions:
        campaign_repo.update_campaign(campaign_id, {"reuse_report": report.to_dict()})
    return report


def _target_key_for(item: dict) -> str:
    """The fetch-ledger key of one plan item (N4).

    ``planning`` owns the key and imports this module, so it is imported here
    rather than at the top: one definition, no cycle.
    """
    from dreamjob.pipeline import planning  # noqa: PLC0415 - avoids a circular import

    return planning.target_key(item["adapter_key"], item.get("native_query"))


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
    ats_vendor: str | None = None,
    limit: int = 25,
    offset: int = 0,
    policy: dict[str, int] | None = None,
) -> dict:
    """Search the shared company knowledge base, independent of any campaign."""
    rows = repo.search_companies(
        q, country=country, sector=sector, size_band=size_band, ats_vendor=ats_vendor,
        limit=limit, offset=offset,
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
        "total": repo.count_companies(
            q, country=country, sector=sector, size_band=size_band, ats_vendor=ats_vendor
        ),
        "limit": limit,
        "offset": offset,
        "items": items,
        # FR-345: where this inventory came from, so a corpus that looks healthy
        # at a thousand rows but was reached through one source says so.
        "facets": repo.company_facets(),
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
