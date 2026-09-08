"""Hiring-contact discovery (FR-301, FR-306, NFR-302, NFR-303, CR-402, RK-08).

FR-301 fixes the priority order, and the whole module is built around it:

1. **the hiring manager of the relevant department**, where the department can
   actually be identified.  ``company.structure`` (FR-223) is the input: the
   opportunity's function family and title are matched against the business
   units and functions the company profile found, and the unit's ``head`` is
   the candidate.  When no unit matches, there is no hiring manager - guessing
   one from a job title would be an invented fact (CR-405).
2. **an HR or talent-acquisition contact**, recognised from the role title in
   the four languages of the target market.
3. **a generic careers mailbox**, which is what most smaller employers publish
   and is always a lawful route (CR-402, CR-403).

Every candidate carries its tier, role, source and confidence, and the ranked
list is what the interface shows (FR-301).  A candidate is only promoted above
its tier by evidence: a published address outranks an inferred one, and a
``valid`` FR-304 verdict outranks an ``unknown`` one.

Three rules constrain what this module is allowed to keep:

* **FR-306 / RK-08** - name, role, company, professional e-mail, source, date.
  ``repositories.contacts.minimise`` is the gate, and this module never
  assembles anything else in the first place.
* **NFR-303** - anything collected through browser automation is written with
  ``shareable = 0``, the owning campaign, and a deletion date equal to the
  campaign's end plus a grace period.  :func:`sweep_retention` deletes them
  when due and is registered as a job the monitoring scheduler can run.
* **NFR-302** - an objection blocks an address permanently, for every job
  seeker.  It is recorded in ``contact_objection`` and enforced by the
  ``usable_contact`` view, so the generation slice inherits the block through
  its query and not through the interface.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from dreamjob.db.connection import from_json, utcnow
from dreamjob.db.repositories import contacts as repo
from dreamjob.db.repositories import knowledge as kb
from dreamjob.egress.client import EgressClient
from dreamjob.jobs.runner import JobContext, runner
from dreamjob.pipeline import email_patterns as patterns
from dreamjob.pipeline import email_validate as validation
from dreamjob.security.audit import record_audit

log = logging.getLogger(__name__)

# --- FR-301 tiers ----------------------------------------------------------
TIER_HIRING_MANAGER = "hiring_manager"
TIER_HR = "hr"
TIER_GENERIC = "generic_mailbox"

#: The FR-301 priority, as a number the ranking can sort on.
TIER_WEIGHT = {TIER_HIRING_MANAGER: 1.0, TIER_HR: 0.7, TIER_GENERIC: 0.4}

#: How much an FR-304 verdict moves a candidate.  ``invalid`` never appears:
#: the ``usable_contact`` view has already removed it.
VALIDATION_WEIGHT = {
    validation.VALID: 1.0,
    validation.RISKY: 0.75,
    validation.UNKNOWN: 0.6,
    validation.INVALID: 0.0,
    None: 0.55,
}

# --- NFR-303 retention -----------------------------------------------------
SETTING_GRACE_DAYS = "contacts.retention_grace_days"
#: OQ-03 - "should contacts collected via browser automation ever be shared
#: across job seekers?"  Default proposed: no.  NFR-303 allows it only with
#: "explicit configuration acknowledging the applicable terms", so it is a
#: setting an administrator has to turn on, never a code path taken by default.
SETTING_SHARE_BROWSER = "contacts.share_browser_collected"
DEFAULT_GRACE_DAYS = 30
RETENTION_JOB_KIND = "contact_retention"

#: Access methods whose records are campaign-scoped rather than shared (NFR-303).
CAMPAIGN_SCOPED_METHODS = frozenset({"browser"})

# --- Role recognition, in the four languages of the target market (DR-104) --
HR_TOKENS = (
    "human resources", "human resource", "hr business partner", "hr manager",
    "hr director", "hrbp", "people operations", "people & culture", "people and culture",
    "chief people", "head of people", "talent acquisition", "talent partner",
    "talent manager", "recruiter", "recruiting", "recruitment", "sourcing specialist",
    "personeelszaken", "personeelsdienst", "hr-medewerker", "hr adviseur",
    "ressources humaines", "responsable rh", "chargé de recrutement", "charge de recrutement",
    "drh", "personalabteilung", "personalreferent", "personalleiter", "recruiting manager",
)

MANAGER_TOKENS = (
    "chief", "ceo", "cto", "cfo", "coo", "cio", "cdo", "founder", "co-founder", "owner",
    "managing director", "general manager", "director", "directeur", "direktor",
    "vp ", "vice president", "head of", "hoofd", "afdelingshoofd", "responsable",
    "manager", "lead", "teamlead", "team lead", "principal", "partner", "president",
    "zaakvoerder", "bestuurder", "gedelegeerd bestuurder", "geschäftsführer",
)

#: Titles that look senior but are not a hiring manager for the seeker's role.
NON_HIRING_TOKENS = ("assistant to", "executive assistant", "intern", "trainee", "student")

#: Function-family synonyms, so "data" matches a "Data & Analytics" unit and an
#: "IT" department alike.  Keys are the tokens found in a job title.
FUNCTION_SYNONYMS: dict[str, tuple[str, ...]] = {
    "data": ("data", "analytics", "analyse", "bi", "business intelligence", "ai", "science"),
    "engineering": ("engineering", "development", "developer", "software", "r&d", "tech", "it"),
    "it": ("it", "ict", "information technology", "infrastructure", "systems"),
    "product": ("product", "productmanagement", "ux", "design"),
    "sales": ("sales", "commercial", "verkoop", "vente", "account", "business development"),
    "marketing": ("marketing", "communication", "communicatie", "brand", "growth"),
    "finance": ("finance", "financial", "controlling", "accounting", "boekhouding", "treasury"),
    "operations": ("operations", "operational", "logistics", "supply", "production", "productie"),
    "hr": ("hr", "human resources", "people", "personeel", "talent"),
    "legal": ("legal", "juridisch", "compliance", "regulatory"),
    "consulting": ("consulting", "advisory", "consultancy", "advies"),
    "customer": ("customer", "support", "service", "klanten"),
    "quality": ("quality", "kwaliteit", "qa", "regulatory"),
    "procurement": ("procurement", "purchasing", "inkoop", "sourcing"),
}

_WORD_RE = re.compile(r"[a-z0-9&+]+")


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


@dataclass
class ContactCandidate:
    """One possible hiring contact, with everything FR-301 asks to be shown."""

    full_name: str | None
    role_title: str | None
    tier: str
    source: str
    confidence: float
    department: str | None = None
    email: str | None = None
    email_source_method: str | None = None
    linkedin_url: str | None = None
    is_generic_mailbox: bool = False
    rationale: str = ""
    validation: str | None = None
    validation_detail: dict[str, Any] = field(default_factory=dict)
    contact_id: str | None = None
    score: float = 0.0
    blocked: bool = False

    def as_contact_row(self, company_id: str | None, scope: dict[str, Any]) -> dict[str, Any]:
        """The FR-306 minimum: name, role, company, address, source, date."""
        row: dict[str, Any] = {
            "company_id": company_id,
            "full_name": self.full_name,
            "role_title": self.role_title,
            "department": self.department,
            "email": self.email,
            "email_source_method": self.email_source_method,
            "is_generic_mailbox": 1 if self.is_generic_mailbox else 0,
            "linkedin_url": self.linkedin_url,
            "source": self.source,
            "collected_at": utcnow(),
            "confidence": round(self.confidence, 3),
        }
        if self.validation:
            row["email_validation"] = self.validation
            row["email_validation_detail"] = self.validation_detail
            row["email_validated_at"] = utcnow()
        row.update(scope)
        return repo.minimise(row)

    def as_public(self) -> dict[str, Any]:
        """What the ranked list shows: role, source and confidence (FR-301)."""
        return {
            "contact_id": self.contact_id,
            "full_name": self.full_name,
            "role_title": self.role_title,
            "department": self.department,
            "tier": self.tier,
            "email": self.email,
            "email_source_method": self.email_source_method,
            "email_validation": self.validation,
            "is_generic_mailbox": self.is_generic_mailbox,
            "linkedin_url": self.linkedin_url,
            "source": self.source,
            "confidence": round(self.confidence, 3),
            "score": round(self.score, 3),
            "rationale": self.rationale,
            "blocked": self.blocked,
        }


def _tokens(text: str | None) -> set[str]:
    return set(_WORD_RE.findall(patterns.strip_accents(text or "").lower()))


def classify_role(role_title: str | None) -> str:
    """Which FR-301 tier a role title belongs to."""
    title = patterns.strip_accents(role_title or "").lower()
    if not title:
        return TIER_GENERIC
    if any(token in title for token in NON_HIRING_TOKENS):
        return TIER_GENERIC
    if any(token in title for token in HR_TOKENS):
        return TIER_HR
    if any(token in title for token in MANAGER_TOKENS):
        return TIER_HIRING_MANAGER
    return TIER_GENERIC


def function_tokens(*texts: str | None) -> set[str]:
    """Expand a role title and function family into comparable function tokens."""
    words = set()
    for text in texts:
        words |= _tokens(text)
    expanded = set(words)
    for family, synonyms in FUNCTION_SYNONYMS.items():
        if words & set(synonyms) or family in words:
            expanded.add(family)
            expanded.update(synonyms)
    return expanded


def department_relevance(department_name: str | None, wanted: set[str]) -> float:
    """How well one department matches the opportunity's function (FR-301).

    Returns 0 when nothing matches, which is the case that stops the pipeline
    from proposing a hiring manager it cannot justify.
    """
    if not department_name or not wanted:
        return 0.0
    dept = _tokens(department_name)
    if not dept:
        return 0.0
    overlap = dept & wanted
    if not overlap:
        return 0.0
    return round(min(1.0, len(overlap) / max(1, len(dept)) + 0.25), 3)


def hiring_managers_from_structure(
    structure: dict[str, Any], key_people: list[dict], wanted: set[str]
) -> list[ContactCandidate]:
    """FR-301 tier 1, read off the FR-223 departmental map.

    Only heads the company profile actually named are returned.  A unit with no
    named head yields nothing: an invented manager would be a fabricated fact
    about a real person (CR-405, CR-402).
    """
    out: list[ContactCandidate] = []
    units = structure.get("business_units") or []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        names = [unit.get("name")] + list(unit.get("functions") or [])
        relevance = max((department_relevance(str(n), wanted) for n in names if n), default=0.0)
        head = unit.get("head") or {}
        if not isinstance(head, dict) or not head.get("name") or relevance <= 0:
            continue
        out.append(
            ContactCandidate(
                full_name=str(head["name"]).strip(),
                role_title=str(head.get("role") or "").strip() or None,
                department=str(unit.get("name") or "").strip() or None,
                tier=TIER_HIRING_MANAGER,
                source=str(unit.get("source") or "company_structure"),
                confidence=round(
                    0.5 + 0.3 * relevance + 0.2 * float(unit.get("confidence") or 0.5), 3
                ),
                rationale=(
                    f"Heads {unit.get('name')}, the department the role belongs to "
                    f"(FR-223 structure, relevance {relevance:.2f})"
                ),
            )
        )

    # Key people are the fallback within tier 1: a named leader whose own title
    # matches the function, when the structure has no unit for it.
    for person in key_people:
        if not isinstance(person, dict) or not person.get("name"):
            continue
        role = str(person.get("role") or "")
        tier = classify_role(role)
        relevance = department_relevance(f"{role} {person.get('unit') or ''}", wanted)
        if tier == TIER_GENERIC and relevance <= 0:
            continue
        out.append(
            ContactCandidate(
                full_name=str(person["name"]).strip(),
                role_title=role or None,
                department=str(person.get("unit") or "").strip() or None,
                linkedin_url=person.get("linkedin_url") or None,
                tier=tier if tier != TIER_GENERIC else TIER_HIRING_MANAGER,
                source=str(person.get("source") or "company_key_people"),
                confidence=round(
                    0.45 + 0.25 * relevance + 0.2 * float(person.get("confidence") or 0.5), 3
                ),
                rationale=(
                    f"Named on the company profile as {role or 'a leader'}"
                    + (f"; function overlap {relevance:.2f}" if relevance else "")
                ),
            )
        )

    # One candidate per person: the structure and the key-people list overlap.
    unique: dict[str, ContactCandidate] = {}
    for candidate in out:
        key = (candidate.full_name or "").lower()
        current = unique.get(key)
        if current is None or candidate.confidence > current.confidence:
            unique[key] = candidate
    return list(unique.values())


def candidates_from_stored(company_id: str, campaign_id: str | None) -> list[ContactCandidate]:
    """Contacts the knowledge base already holds for this company (FR-341 reuse)."""
    out = []
    for row in repo.contacts_for_company(company_id, include_blocked=True):
        tier = classify_role(row.get("role_title"))
        if row.get("is_generic_mailbox"):
            tier = TIER_GENERIC
        blocked = bool(row.get("objected")) or row.get("email_validation") == validation.INVALID
        if row.get("shareable") == 0 and row.get("owning_campaign_id") not in (None, campaign_id):
            continue  # NFR-303: another campaign's scoped record.
        out.append(
            ContactCandidate(
                contact_id=row["id"],
                full_name=row.get("full_name"),
                role_title=row.get("role_title"),
                department=row.get("department"),
                email=row.get("email"),
                email_source_method=row.get("email_source_method"),
                linkedin_url=row.get("linkedin_url"),
                is_generic_mailbox=bool(row.get("is_generic_mailbox")),
                tier=tier,
                source=row.get("source") or "knowledge_base",
                confidence=float(row.get("confidence") or 0.5),
                validation=row.get("email_validation"),
                validation_detail=from_json(row.get("email_validation_detail"), {}) or {},
                rationale="Already in the knowledge base",
                blocked=blocked,
            )
        )
    return out


def generic_mailbox_candidates(
    domain: str, careers_url: str | None, harvested: list[patterns.FoundAddress]
) -> list[ContactCandidate]:
    """FR-301 tier 3: a published careers mailbox, else a conventional guess."""
    out: list[ContactCandidate] = []
    published = {a.email: a for a in harvested if validation.is_role_address(a.email)}
    for address in published.values():
        careers = any(
            address.local_part.startswith(part) for part in patterns.CAREERS_LOCAL_PARTS
        )
        out.append(
            ContactCandidate(
                full_name=None,
                role_title="Careers mailbox" if careers else "General mailbox",
                tier=TIER_GENERIC,
                source=address.source_url or address.method,
                email=address.email,
                email_source_method=address.method,
                is_generic_mailbox=True,
                confidence=round(address.confidence + (0.05 if careers else -0.1), 3),
                rationale="Published by the company on its own site (FR-303 website)",
            )
        )
    if not any(
        c.email and c.email.split("@")[0] in patterns.CAREERS_LOCAL_PARTS for c in out
    ):
        for guess in patterns.generic_candidates(domain, careers_url=careers_url)[:3]:
            out.append(
                ContactCandidate(
                    full_name=None,
                    role_title="Careers mailbox",
                    tier=TIER_GENERIC,
                    source=guess.source_url or "convention",
                    email=guess.email,
                    email_source_method=guess.method,
                    is_generic_mailbox=True,
                    confidence=guess.confidence,
                    rationale="Conventional careers mailbox, unverified (FR-303 inference)",
                )
            )
    return out


# ---------------------------------------------------------------------------
# Ranking (FR-301)
# ---------------------------------------------------------------------------


def score_candidate(candidate: ContactCandidate) -> float:
    """FR-301: tier first, then how good the evidence for the address is."""
    tier = TIER_WEIGHT.get(candidate.tier, 0.3)
    reachable = 0.0
    if candidate.email:
        reachable = VALIDATION_WEIGHT.get(candidate.validation, 0.55)
        method = candidate.email_source_method
        reachable *= {
            patterns.METHOD_MANUAL: 1.0,
            patterns.METHOD_WEBSITE: 0.95,
            patterns.METHOD_VACANCY: 0.95,
            patterns.METHOD_PRESS: 0.9,
            patterns.METHOD_LOOKUP: 0.85,
            patterns.METHOD_PATTERN: 0.65,
        }.get(method or "", 0.7)
    elif candidate.linkedin_url:
        # Reachable through an introduction even without an address (FR-302).
        reachable = 0.35
    return round(0.55 * tier + 0.30 * reachable + 0.15 * min(1.0, candidate.confidence), 4)


def rank(candidates: list[ContactCandidate]) -> list[ContactCandidate]:
    """Rank and de-duplicate, best first (FR-301)."""
    merged: dict[str, ContactCandidate] = {}
    for candidate in candidates:
        key = (candidate.email or "").lower() or f"name:{(candidate.full_name or '').lower()}"
        if not key.strip(":"):
            continue
        current = merged.get(key)
        if current is None:
            merged[key] = candidate
            continue
        # Keep the richer record; a stored contact wins over a fresh guess.
        if (candidate.contact_id and not current.contact_id) or (
            score_candidate(candidate) > score_candidate(current)
        ):
            candidate.contact_id = candidate.contact_id or current.contact_id
            merged[key] = candidate

    ranked = []
    for candidate in merged.values():
        candidate.score = score_candidate(candidate)
        ranked.append(candidate)
    ranked.sort(key=lambda c: (c.blocked, -c.score))
    return ranked


# ---------------------------------------------------------------------------
# NFR-303: campaign scope and retention
# ---------------------------------------------------------------------------


def retention_grace_days() -> int:
    try:
        return max(0, int(kb.get_setting(SETTING_GRACE_DAYS, DEFAULT_GRACE_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_GRACE_DAYS


def retention_deadline(campaign_id: str | None, *, grace_days: int | None = None) -> str:
    """Campaign end plus the grace period (NFR-303).

    A campaign that has not finished is measured from now, and the deadline
    moves forward whenever the record is re-collected, which is what "unless
    re-collected" in NFR-303 means.
    """
    grace = retention_grace_days() if grace_days is None else max(0, int(grace_days))
    base = datetime.now(UTC)
    if campaign_id:
        ends_at = repo.campaign_end(campaign_id)
        if ends_at:
            try:
                parsed = datetime.fromisoformat(ends_at)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                base = max(base, parsed)
            except ValueError:
                log.warning("Campaign %s has an unparseable end date", campaign_id)
    return (base + timedelta(days=grace)).isoformat(timespec="seconds")


def share_browser_collected() -> bool:
    """OQ-03: off unless an administrator has explicitly turned it on (NFR-303)."""
    return kb.get_setting(SETTING_SHARE_BROWSER, False) in (True, 1, "1", "true", "True")


def storage_scope(access_method: str, campaign_id: str | None) -> dict[str, Any]:
    """NFR-303: browser-collected records are campaign-scoped and expire.

    The retention deadline is set regardless of the sharing setting - OQ-03 is
    about who may read the record, not about how long it is kept.
    """
    scope: dict[str, Any] = {"access_method": access_method}
    if access_method in CAMPAIGN_SCOPED_METHODS:
        scope["shareable"] = 1 if share_browser_collected() else 0
        scope["owning_campaign_id"] = campaign_id
        scope["retention_until"] = retention_deadline(campaign_id)
    else:
        scope["shareable"] = 1
    return scope


def sweep_retention(now: str | None = None) -> dict[str, Any]:
    """Delete third-party records whose retention deadline has passed (NFR-303).

    Callable directly, and registered below as a job so the monitoring
    scheduler can run it on its own cadence.
    """
    report = repo.sweep_retention(now)
    if report["contacts"] or report["network_members"]:
        log.info(
            "Retention sweep removed %d contacts and %d network members (NFR-303)",
            report["contacts"],
            report["network_members"],
        )
        record_audit(
            "contacts.retention_sweep",
            entity_type="contact",
            detail=report,
            actor="system",
        )
    return report


async def retention_sweeper(ctx: JobContext) -> dict[str, Any]:
    """Job wrapper around :func:`sweep_retention` (NFR-303)."""
    report = sweep_retention()
    ctx.progress(report["contacts"] + report["network_members"])
    ctx.save_checkpoint(last_sweep=report)
    return report


def register_retention_worker() -> str:
    """Register the sweeper with the job runner; returns its job kind.

    The monitoring slice's scheduler calls this once and then creates a
    ``contact_retention`` job whenever it runs its own housekeeping.
    """
    runner.register_worker(RETENTION_JOB_KIND, retention_sweeper)
    return RETENTION_JOB_KIND


# ---------------------------------------------------------------------------
# NFR-302: objections
# ---------------------------------------------------------------------------


def record_objection(
    email: str | None,
    *,
    linkedin_url: str | None = None,
    reason: str | None = None,
    source: str = "manual",
    job_seeker_id: str | None = None,
) -> dict:
    """Block a contact permanently, for everybody (NFR-302).

    The block is stored separately from the contact row because NFR-303 deletes
    contact rows on a deadline; without it, the next campaign would re-collect
    the same address from the same public page and mail it again.
    """
    objection = repo.record_objection(
        email, linkedin_url=linkedin_url, reason=reason, source=source
    )
    record_audit(
        "contacts.objection_recorded",
        entity_type="contact_objection",
        entity_id=objection["email"],
        seeker_id=job_seeker_id,
        detail={"source": source, "reason": reason},
        actor=job_seeker_id or "system",
    )
    return objection


def is_blocked(email: str | None, linkedin_url: str | None = None) -> bool:
    return repo.is_objected(email, linkedin_url)


# ---------------------------------------------------------------------------
# Discovery (FR-301)
# ---------------------------------------------------------------------------


@dataclass
class DiscoveryReport:
    """What one FR-301 run found, for the interface and the audit trail."""

    opportunity_id: str
    company_id: str | None
    company_name: str | None
    domain: str | None
    candidates: list[ContactCandidate] = field(default_factory=list)
    pattern: dict[str, Any] | None = None
    stored: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "company_id": self.company_id,
            "company_name": self.company_name,
            "domain": self.domain,
            "best": self.candidates[0].as_public() if self.candidates else None,
            "candidates": [c.as_public() for c in self.candidates],
            "email_pattern": self.pattern,
            "stored": self.stored,
            "notes": self.notes,
        }


async def discover_for_opportunity(
    job_seeker_id: str,
    opportunity_id: str,
    *,
    campaign_id: str | None = None,
    crawl_site: bool = True,
    allow_smtp: bool = True,
    use_lookup_service: bool = False,
    max_candidates: int = 8,
    egress: EgressClient | None = None,
    persist: bool = True,
) -> DiscoveryReport:
    """Find and rank the hiring contacts for one opportunity (FR-301, FR-303, FR-304).

    The order of work is the FR-301 priority order, and every step degrades on
    its own: with no site to crawl the pattern inference still runs off stored
    addresses, with no addresses at all the generic mailbox remains, and with
    no network access the stored knowledge base is still ranked and returned.
    """
    context = repo.opportunity_context(opportunity_id, job_seeker_id)
    if context is None:
        raise LookupError(f"No opportunity {opportunity_id} for this job seeker")

    campaign_id = campaign_id or context.get("campaign_id")
    company_id = context.get("company_id")
    domain = patterns.domain_of(
        context.get("company_domain") or context.get("company_careers_url")
    )
    report = DiscoveryReport(
        opportunity_id=opportunity_id,
        company_id=company_id,
        company_name=context.get("company_name"),
        domain=domain or None,
    )
    if not company_id:
        report.notes.append("The opportunity has no company; no contact can be identified.")
        return report

    wanted = function_tokens(context.get("function_family"), context.get("title"))

    # --- FR-301 tiers 1 and 2, from the company profile --------------------
    candidates: list[ContactCandidate] = hiring_managers_from_structure(
        context.get("company_structure") or {}, context.get("company_key_people") or [], wanted
    )
    if not any(c.tier == TIER_HIRING_MANAGER for c in candidates):
        report.notes.append(
            "No department in the company structure matches this role; "
            "falling back to HR and the careers mailbox (FR-301)."
        )
    candidates.extend(candidates_from_stored(company_id, campaign_id))

    # --- FR-303: addresses off the company's own pages ---------------------
    harvested: list[patterns.FoundAddress] = []
    if crawl_site and domain:
        try:
            harvested = await patterns.collect_from_site(
                domain, careers_url=context.get("company_careers_url"), egress=egress
            )
        except Exception as exc:  # noqa: BLE001 - the site is optional evidence
            log.info("Could not read %s for addresses: %s", domain, exc)
            report.notes.append(f"The company website could not be read ({type(exc).__name__}).")

    # An address from the vacancy itself is the strongest evidence there is.
    if context.get("vacancy_application_channel") == "email" and context.get(
        "vacancy_application_target"
    ):
        target = str(context["vacancy_application_target"]).strip().lower()
        if "@" in target:
            harvested.append(
                patterns.FoundAddress(
                    email=target,
                    method=patterns.METHOD_VACANCY,
                    source_url=context.get("vacancy_source_url") or "",
                    confidence=patterns.METHOD_CONFIDENCE[patterns.METHOD_VACANCY],
                )
            )

    # --- FR-303: learn the domain's convention -----------------------------
    inference = None
    if domain:
        observations = [
            (a.full_name, a.email) for a in harvested if a.full_name and not
            validation.is_role_address(a.email)
        ]
        inference = patterns.learn_domain_pattern(domain, observations)
        report.pattern = {
            "domain": domain,
            "pattern": inference.pattern,
            "confidence": inference.confidence,
            "samples": inference.sample_count,
            "supporting": inference.supporting,
        }

    # --- give the named people an address ----------------------------------
    published = {a.email: a for a in harvested}
    for candidate in candidates:
        if candidate.email or not candidate.full_name or not domain:
            continue
        match = _published_for(candidate.full_name, published)
        if match:
            candidate.email = match.email
            candidate.email_source_method = match.method
            candidate.source = match.source_url or candidate.source
            candidate.confidence = max(candidate.confidence, match.confidence)
            continue
        guesses = patterns.candidates_for_person(
            candidate.full_name, domain, inference=inference, limit=3
        )
        if use_lookup_service and patterns.lookup_service_enabled():
            try:
                looked_up = await patterns.lookup_via_service(
                    candidate.full_name, domain, egress=egress
                )
                guesses = looked_up + guesses
            except patterns.LookupServiceDisabled as exc:
                report.notes.append(str(exc))
            except Exception as exc:  # noqa: BLE001
                log.info("Lookup service failed for %s: %s", candidate.full_name, exc)
        if guesses:
            best = await asyncio.to_thread(_first_usable, guesses, allow_smtp=allow_smtp)
            if best is not None:
                address, verdict = best
                candidate.email = address.email
                candidate.email_source_method = address.method
                candidate.confidence = min(candidate.confidence, 0.6)
                candidate.validation = verdict.result
                candidate.validation_detail = verdict.detail
                candidate.rationale += (
                    f"; address inferred from the {inference.pattern} convention"
                    if inference and inference.pattern
                    else "; address inferred from a common convention"
                )

    # --- FR-301 tier 3 -----------------------------------------------------
    if domain:
        candidates.extend(
            generic_mailbox_candidates(domain, context.get("company_careers_url"), harvested)
        )

    # --- FR-304: validate before anything is used --------------------------
    # The MX lookup and the RCPT probe are blocking sockets; off the event loop
    # they would stall every other request for the length of a probe timeout.
    for candidate in candidates:
        if candidate.email and candidate.validation is None:
            verdict = await asyncio.to_thread(
                validation.validate, candidate.email, allow_smtp=allow_smtp
            )
            candidate.validation = verdict.result
            candidate.validation_detail = verdict.detail
            if verdict.detail.get("role"):
                candidate.is_generic_mailbox = True
        if candidate.email and is_blocked(candidate.email, candidate.linkedin_url):
            candidate.blocked = True  # NFR-302

    usable = [
        c
        for c in candidates
        if not c.blocked and c.validation != validation.INVALID and (c.email or c.linkedin_url)
    ]
    report.candidates = rank(usable)[:max_candidates]

    if persist:
        report.stored = _persist(report.candidates, company_id, campaign_id)
        record_audit(
            "contacts.discovered",
            entity_type="opportunity",
            entity_id=opportunity_id,
            seeker_id=job_seeker_id,
            detail={
                "company_id": company_id,
                "candidates": len(report.candidates),
                "stored": report.stored,
                "tiers": sorted({c.tier for c in report.candidates}),
            },
        )
    return report


def _published_for(
    full_name: str, published: dict[str, patterns.FoundAddress]
) -> patterns.FoundAddress | None:
    """Match a published address to a person by the name parts in its local part."""
    parts = patterns.split_name(full_name)
    if parts is None:
        return None
    for address in published.values():
        if validation.is_role_address(address.email):
            continue
        if address.full_name and patterns.split_name(address.full_name) == parts:
            return address
        local = address.local_part.lower()
        for particle in (False, True):
            for pattern in patterns.PATTERN_ORDER:
                if patterns.render_pattern(pattern, parts, particle=particle) == local:
                    return address
    return None


def _first_usable(
    guesses: list[patterns.FoundAddress], *, allow_smtp: bool
) -> tuple[patterns.FoundAddress, validation.ValidationResult] | None:
    """The first inferred address FR-304 does not rule out."""
    fallback: tuple[patterns.FoundAddress, validation.ValidationResult] | None = None
    for guess in guesses:
        verdict = validation.validate(guess.email, allow_smtp=allow_smtp)
        if verdict.result == validation.VALID:
            return guess, verdict
        if verdict.result != validation.INVALID and fallback is None:
            fallback = (guess, verdict)
    return fallback


def _persist(
    candidates: list[ContactCandidate], company_id: str, campaign_id: str | None
) -> int:
    """Store the ranked candidates, minimised to FR-306."""
    stored = 0
    for candidate in candidates:
        if not candidate.email and not candidate.linkedin_url:
            continue
        access_method = "browser" if candidate.source == "browser" else "http"
        scope = storage_scope(access_method, campaign_id)
        contact_id, _created = repo.upsert_contact(
            candidate.as_contact_row(company_id, scope)
        )
        candidate.contact_id = contact_id
        stored += 1
    return stored


async def discover_for_campaign(
    job_seeker_id: str,
    campaign_id: str,
    *,
    limit: int = 50,
    **kwargs: Any,
) -> list[DiscoveryReport]:
    """FR-301 for every selected opportunity of a campaign."""
    reports = []
    async with EgressClient() as client:
        for opportunity in repo.selected_opportunities(job_seeker_id, campaign_id, limit):
            try:
                reports.append(
                    await discover_for_opportunity(
                        job_seeker_id,
                        opportunity["id"],
                        campaign_id=campaign_id,
                        egress=client,
                        **kwargs,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one company must not stop the run
                log.exception("Contact discovery failed for opportunity %s", opportunity["id"])
                reports.append(
                    DiscoveryReport(
                        opportunity_id=opportunity["id"],
                        company_id=opportunity.get("company_id"),
                        company_name=None,
                        domain=None,
                        notes=[f"Discovery failed: {exc}"],
                    )
                )
    return reports


def best_contact(
    company_id: str, *, campaign_id: str | None = None
) -> dict | None:
    """The contact the generation slice should write to (FR-301, NFR-302, FR-304).

    Reads ``usable_contact``, so an objection and an ``invalid`` verdict are
    both already excluded by the query rather than by this function.
    """
    rows = repo.usable_contacts_for_company(company_id, campaign_id=campaign_id)
    if not rows:
        return None
    candidates = []
    for row in rows:
        tier = TIER_GENERIC if row.get("is_generic_mailbox") else classify_role(
            row.get("role_title")
        )
        candidate = ContactCandidate(
            contact_id=row["id"],
            full_name=row.get("full_name"),
            role_title=row.get("role_title"),
            department=row.get("department"),
            email=row.get("email"),
            email_source_method=row.get("email_source_method"),
            linkedin_url=row.get("linkedin_url"),
            is_generic_mailbox=bool(row.get("is_generic_mailbox")),
            tier=tier,
            source=row.get("source") or "knowledge_base",
            confidence=float(row.get("confidence") or 0.5),
            validation=row.get("email_validation"),
        )
        candidates.append(candidate)
    ranked = rank(candidates)
    return ranked[0].as_public() if ranked else None
