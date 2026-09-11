"""Opportunity synthesis from collected vacancies (FR-261, FR-263, CR-405).

A vacancy row is what a *source* said; an opportunity row is what this job
seeker is actually looking at.  The two are deliberately separate:

* ``vacancy`` is shared, re-collected and merged over time (FR-342, FR-344),
  so its fields move under your feet.  ``opportunity`` is private, carries the
  seeker's pins, tags and manual order (FR-284), and must stay stable between
  campaign runs.
* A speculative opening (FR-262) has no vacancy at all, yet has to be rankable
  against real ones.  It can only be if both kinds carry the same normalised
  fields, which is why FR-261's field list lives on ``opportunity``.

Normalisation fills the gaps a job posting leaves.  Roughly one advertisement
in three states neither seniority nor contract type in a structured field but
says both in the first paragraph, so the missing values are inferred from the
title and the body with plain patterns - deterministic, reproducible, free, and
recorded as inferred so nothing downstream mistakes them for stated facts
(CR-405).

Every opportunity carries ``kind``.  For real vacancies it is ``"vacancy"``;
for generated openings ``"speculative"``.  FR-263 hangs off that one field, so
it is never nullable and never guessed - see :mod:`dreamjob.pipeline.speculative`
for the labelling helpers that carry it into generated material.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from dreamjob.db.connection import from_json, query_all
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.db.repositories import opportunities as repo
from dreamjob.pipeline import directives as dir_mod
from dreamjob.pipeline import signals as signals_mod
from dreamjob.pipeline.skills import normalise_labels

log = logging.getLogger(__name__)

KIND_VACANCY = "vacancy"
KIND_SPECULATIVE = "speculative"

#: How far back a knowledge-base vacancy may reach when it was not collected by
#: this campaign itself.  Matches the default vacancy staleness policy (FR-343).
DEFAULT_REUSE_WINDOW_DAYS = 45


# ---------------------------------------------------------------------------
# Field inference (FR-261)
# ---------------------------------------------------------------------------

_FUNCTION_FAMILY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("data & analytics", re.compile(
        r"\b(data (engineer(ing)?|scien(ce|tist)|analyst|architect|platform|governance)|analytics|"
        r"machine learning|ml engineer|mlops|business intelligence|bi developer)\b", re.I)),
    ("software engineering", re.compile(
        r"\b(software|backend|back-end|frontend|front-end|full[- ]?stack|developer|"
        r"programmer|engineer(ing)? manager|architect|sre|devops|platform engineer)\b", re.I)),
    ("product", re.compile(r"\b(product (owner|manager|lead)|product management|cpo)\b", re.I)),
    ("information security", re.compile(
        r"\b(security|ciso|cyber|infosec|soc analyst|penetration test)\b", re.I)),
    ("it operations", re.compile(
        r"\b(system(s)? (administrator|engineer)|network engineer|it support|helpdesk|"
        r"infrastructure)\b", re.I)),
    ("finance", re.compile(
        r"\b(finance|financial|controller|accountant|accounting|treasury|cfo|audit)\b", re.I)),
    ("sales & business development", re.compile(
        r"\b(sales|account (executive|manager)|business development|commercial manager|cro)\b",
        re.I)),
    ("marketing & communications", re.compile(
        r"\b(marketing|communicat|brand|content manager|growth|seo|cmo)\b", re.I)),
    ("human resources", re.compile(
        r"\b(hr\b|human resources|talent acquisition|recruit|people (partner|manager)|chro)\b",
        re.I)),
    ("operations & supply chain", re.compile(
        r"\b(operations|supply chain|logistics|procurement|purchas|warehouse|planner)\b", re.I)),
    ("engineering & manufacturing", re.compile(
        r"\b(mechanical|electrical|process engineer|maintenance|production (manager|engineer)|"
        r"quality (engineer|manager))\b", re.I)),
    ("consulting & advisory", re.compile(r"\b(consultant|consulting|advisory|advisor)\b", re.I)),
    ("legal & compliance", re.compile(
        r"\b(legal|counsel|compliance|dpo|data protection officer|regulatory)\b", re.I)),
    ("customer success & support", re.compile(
        r"\b(customer (success|support|service)|service desk|client (manager|partner))\b", re.I)),
    ("general management", re.compile(
        r"\b(general manager|managing director|ceo|coo|country manager|site manager)\b", re.I)),
]

_SENIORITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (dir_mod.Seniority.BOARD.value, re.compile(
        r"\b(board member|non[- ]executive|bestuurder|administrateur)\b", re.I)),
    (dir_mod.Seniority.C_LEVEL.value, re.compile(
        r"\b(chief [a-z]+ officer|c-level|ceo|cto|cfo|coo|cio|cmo|cdo|ciso|cpo|cro)\b", re.I)),
    (dir_mod.Seniority.VP.value, re.compile(r"\b(vice[- ]president|vp)\b", re.I)),
    (dir_mod.Seniority.DIRECTOR.value, re.compile(
        r"\b(director|directeur|head of|hoofd van|responsable de)\b", re.I)),
    (dir_mod.Seniority.MANAGER.value, re.compile(
        r"\b(manager|teamleider|team ?lead(er)? of|gerant)\b", re.I)),
    (dir_mod.Seniority.PRINCIPAL.value, re.compile(r"\b(principal|staff engineer|expert)\b", re.I)),
    (dir_mod.Seniority.LEAD.value, re.compile(r"\b(lead|leiding|chef d'[ée]quipe)\b", re.I)),
    (dir_mod.Seniority.SENIOR.value, re.compile(r"\b(senior|sr\.?|ervaren|confirm[ée])\b", re.I)),
    (dir_mod.Seniority.MEDIOR.value, re.compile(r"\b(medior|mid[- ]level)\b", re.I)),
    (dir_mod.Seniority.JUNIOR.value, re.compile(
        r"\b(junior|jr\.?|graduate|starter|entry[- ]level|debutant)\b", re.I)),
    (dir_mod.Seniority.INTERN.value, re.compile(r"\b(intern(ship)?|stagiair|stagiaire)\b", re.I)),
]

_ARRANGEMENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("remote", re.compile(
        r"\b(fully remote|100% remote|remote[- ]first|work from anywhere|volledig telewerk|"
        r"t[ée]l[ée]travail complet)\b", re.I)),
    ("hybrid", re.compile(
        r"\b(hybrid|hybride|(\d\s*(days?|dagen|jours)\s*(per|a|/)\s*week\s*)?"
        r"(from )?home|thuiswerk|telewerk|t[ée]l[ée]travail)\b", re.I)),
    ("onsite", re.compile(r"\b(on[- ]?site|op kantoor|sur site|office[- ]based)\b", re.I)),
]

_REMOTE_DAYS_RE = re.compile(
    r"(\d)\s*(?:days?|dagen|jours)\s*(?:a|per|/)\s*(?:week|weeks)\s*"
    r"(?:from\s+)?(?:home|remote|thuis|telewerk)?",
    re.I,
)

_CONTRACT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("freelance", re.compile(r"\b(freelance|zelfstandig|ind[ée]pendant|contractor|b2b)\b", re.I)),
    ("interim", re.compile(r"\b(interim|int[ée]rim|uitzend|temporary agency)\b", re.I)),
    ("fixed_term", re.compile(
        r"\b(fixed[- ]term|bepaalde duur|dur[ée]e d[ée]termin[ée]e|cdd|temporary contract|"
        r"maternity cover)\b", re.I)),
    ("permanent", re.compile(
        r"\b(permanent|onbepaalde duur|dur[ée]e ind[ée]termin[ée]e|cdi|vast contract)\b", re.I)),
]

_FTE_RE = re.compile(r"\b(\d{2,3})\s*%\s*(?:fte|employment|tewerkstelling|temps)?", re.I)

_ATS_HOSTS = (
    "greenhouse.io", "lever.co", "workday", "smartrecruiters.com", "recruitee.com",
    "personio", "ashbyhq.com", "successfactors", "taleo", "icims.com", "teamtailor.com",
)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def infer_function_family(*texts: str | None) -> str | None:
    """FR-261: the function family, from the title first, then the body."""
    for text in texts:
        if not text:
            continue
        for family, pattern in _FUNCTION_FAMILY_PATTERNS:
            if pattern.search(text):
                return family
    return None


def infer_seniority(*texts: str | None) -> str | None:
    """FR-261: seniority, most senior signal first so 'Senior Director' is director."""
    for text in texts:
        if not text:
            continue
        for level, pattern in _SENIORITY_PATTERNS:
            if pattern.search(text):
                return level
    return None


def infer_work_arrangement(*texts: str | None) -> tuple[str | None, int | None]:
    """FR-261: ``(arrangement, remote_days)``; days only when the text states them."""
    joined = " ".join(t for t in texts if t)
    if not joined:
        return None, None
    days: int | None = None
    match = _REMOTE_DAYS_RE.search(joined)
    if match:
        candidate = int(match.group(1))
        if 0 <= candidate <= 5:
            days = candidate
    for arrangement, pattern in _ARRANGEMENT_PATTERNS:
        if pattern.search(joined):
            if arrangement == "hybrid" and days is None:
                return "hybrid", None
            return arrangement, days
    return (None, days)


def infer_contract_type(*texts: str | None) -> str | None:
    joined = " ".join(t for t in texts if t)
    for contract, pattern in _CONTRACT_PATTERNS:
        if pattern.search(joined):
            return contract
    return None


def infer_fte_percentage(*texts: str | None) -> int | None:
    joined = " ".join(t for t in texts if t)
    for match in _FTE_RE.finditer(joined):
        value = int(match.group(1))
        if 10 <= value <= 100:
            return value
    return None


def application_channel(vacancy: dict) -> tuple[str | None, str | None]:
    """FR-261: how an application would actually be sent, and where to.

    The stated channel wins.  Otherwise an ATS host in the posting URL means an
    application form, a mail address in the body means e-mail, and anything else
    is a plain URL the seeker opens.
    """
    channel = (vacancy.get("application_channel") or "").strip().lower() or None
    target = vacancy.get("application_target")
    if channel:
        return channel, target or vacancy.get("source_url")

    url = (vacancy.get("source_url") or "").lower()
    if url and any(host in url for host in _ATS_HOSTS):
        return "ats_form", vacancy.get("source_url")
    body = vacancy.get("description") or ""
    match = _EMAIL_RE.search(body)
    if match:
        return "email", match.group(0)
    if url:
        return "url", vacancy.get("source_url")
    return None, None


def _skill_labels(value: Any, *, use_llm: Any = None) -> list[str]:
    """Normalise a vacancy's skill list onto the shared taxonomy (FR-107).

    Scoring compares these against ``profile_skill.normalised_label``, so the
    two sides have to have been through the same normaliser or the overlap is
    meaningless.
    """
    raw = from_json(value, None) if isinstance(value, str) else value
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    labels: list[str] = []
    for item in raw:
        if isinstance(item, str):
            labels.append(item)
        elif isinstance(item, dict):
            label = item.get("skill") or item.get("label") or item.get("name")
            if label:
                labels.append(str(label))
    if not labels:
        return []
    return [m.normalised_label for m in normalise_labels(labels[:60], llm=use_llm)]


# ---------------------------------------------------------------------------
# FR-261: one vacancy -> one opportunity record
# ---------------------------------------------------------------------------


def normalise_vacancy(
    vacancy: dict, company: dict | None = None, *, use_llm: Any = None
) -> dict:
    """Map a collected vacancy onto the FR-261 opportunity field set.

    Nothing is invented: a field the posting does not state and the text does
    not imply stays ``None``, and the caller can see that in the record.
    """
    title = (vacancy.get("title") or "").strip()
    description = vacancy.get("description") or ""
    head = description[:1500]

    arrangement, remote_days = infer_work_arrangement(vacancy.get("work_arrangement"), head)
    channel, target = application_channel(vacancy)

    salary_min = vacancy.get("salary_min")
    salary_max = vacancy.get("salary_max")
    stated = salary_min is not None or salary_max is not None

    return {
        "kind": KIND_VACANCY,
        "company_id": vacancy.get("company_id") or (company or {}).get("id"),
        "vacancy_id": vacancy.get("id"),
        "title": title or "Untitled role",
        "function_family": vacancy.get("function_family") or infer_function_family(title, head),
        "seniority": vacancy.get("seniority") or infer_seniority(title, head),
        "description": description or None,
        "required_skills": _skill_labels(vacancy.get("required_skills"), use_llm=use_llm),
        "desirable_skills": _skill_labels(vacancy.get("desirable_skills"), use_llm=use_llm),
        "location": vacancy.get("location"),
        "country": (vacancy.get("country") or (company or {}).get("country") or None),
        "latitude": vacancy.get("latitude"),
        "longitude": vacancy.get("longitude"),
        "work_arrangement": vacancy.get("work_arrangement") or arrangement,
        "remote_days": vacancy.get("remote_days") if vacancy.get("remote_days") is not None
        else remote_days,
        "contract_type": vacancy.get("contract_type") or infer_contract_type(title, head),
        "fte_percentage": vacancy.get("fte_percentage") or infer_fte_percentage(head),
        "posted_at": vacancy.get("posted_at"),
        "application_channel": channel,
        "application_target": target,
        "source_url": vacancy.get("source_url"),
        "source_adapter": vacancy.get("source_adapter"),
        "comp_min": salary_min,
        "comp_max": salary_max,
        "comp_currency": vacancy.get("salary_currency") if stated else None,
        "comp_is_stated": 1 if stated else 0,
        "plausibility": None,
        "speculative_rationale": None,
        "language": vacancy.get("language"),
    }


# ---------------------------------------------------------------------------
# Directive post-filter (FR-142..145, FR-385)
# ---------------------------------------------------------------------------


def _joined_text(record: dict) -> str:
    return " ".join(
        str(record.get(k) or "")
        for k in ("title", "description", "location", "function_family")
    )


def rejection_reason(
    record: dict, company: dict | None, directive_set: dir_mod.DirectiveSetLike | None
) -> str | None:
    """Why this vacancy must not become an opportunity, or ``None`` to keep it.

    Only hard exclusions live here.  Soft mismatches - a location a little too
    far, the wrong contract type - are left to the directive-fit sub-score
    (FR-281) so the seeker still sees them, lower down the list (NFR-305).
    """
    if directive_set is None:
        return None
    directives = dir_mod.coerce_directive_set(directive_set)

    if company is not None and dir_mod.is_excluded(directives, company):
        return "excluded_company"      # FR-385 discretion mode

    text = _joined_text(record).lower()
    for keyword in directives.job_content.keywords_to_avoid:
        if keyword.strip() and keyword.strip().lower() in text:
            return f"keyword_to_avoid:{keyword.strip()}"

    if company is not None and directives.job_content.industries_exclude:
        haystack = " ".join(
            str(company.get(k) or "") for k in ("business_summary", "sector_codes", "markets")
        ).lower()
        for industry in directives.job_content.industries_exclude:
            if industry.strip() and industry.strip().lower() in haystack:
                return f"industry_excluded:{industry.strip()}"

    # FR-142: is this the kind of work the seeker wants at all?  Without this
    # gate every vacancy a scraped board held became a ranked opportunity.
    out_of_scope = dir_mod.role_relevance(
        directives,
        record.get("title"),
        record.get("function_family"),
        record.get("description"),
    )
    if out_of_scope:
        return out_of_scope

    remote = str(record.get("work_arrangement") or "").lower() == "remote"
    if not dir_mod.location_matches(
        directives,
        record.get("latitude"),
        record.get("longitude"),
        record.get("country"),
        remote=remote,
    ):
        return "outside_location_directives"
    return None


# ---------------------------------------------------------------------------
# Campaign-level synthesis
# ---------------------------------------------------------------------------


@dataclass
class SynthesisReport:
    """What one synthesis pass did, for the campaign screen and the job log."""

    campaign_id: str
    considered: int = 0
    created: int = 0
    refreshed: int = 0
    rejected: int = 0
    rejections: dict[str, int] = field(default_factory=dict)
    opportunity_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "considered": self.considered,
            "created": self.created,
            "refreshed": self.refreshed,
            "rejected": self.rejected,
            "rejections": self.rejections,
            "opportunity_ids": self.opportunity_ids,
        }


def _reuse_cutoff(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def campaign_vacancy_pool(
    campaign_id: str, *, include_knowledge_base: bool = True, window_days: int | None = None
) -> list[dict]:
    """Vacancies this campaign may turn into opportunities.

    Two sources, in this order of authority: what the campaign's own plan items
    collected, and - because a repeat campaign deliberately re-uses the
    knowledge base instead of re-fetching (FR-342) - recent vacancies belonging
    to companies this campaign touched.
    """
    pool: dict[str, dict] = {v["id"]: v for v in repo.campaign_vacancies(campaign_id)}
    if include_knowledge_base:
        cutoff = _reuse_cutoff(window_days or DEFAULT_REUSE_WINDOW_DAYS)
        for vacancy in repo.vacancies_for_companies(
            repo.campaign_company_ids(campaign_id), since=cutoff
        ):
            pool.setdefault(vacancy["id"], vacancy)
    return list(pool.values())


def synthesise_campaign(
    campaign: dict,
    *,
    include_knowledge_base: bool = True,
    window_days: int | None = None,
    use_llm: Any = None,
) -> SynthesisReport:
    """FR-261: normalise every collected vacancy into an opportunity record.

    Re-runnable.  An opportunity that already exists is refreshed in place, so
    the seeker's pins, tags, manual position and status survive a re-collection
    (FR-284).
    """
    campaign_id = campaign["id"]
    seeker_id = campaign["job_seeker_id"]
    report = SynthesisReport(campaign_id=campaign_id)

    inputs = campaign_repo.load_planning_inputs(campaign)
    directive_row = inputs.get("directives")
    directive_set = dir_mod.coerce_directive_set(directive_row) if directive_row else None

    companies: dict[str, dict] = {}
    for vacancy in campaign_vacancy_pool(
        campaign_id, include_knowledge_base=include_knowledge_base, window_days=window_days
    ):
        report.considered += 1
        company_id = vacancy.get("company_id")
        if company_id and company_id not in companies:
            companies[company_id] = repo.get_company(company_id) or {}
        company = companies.get(company_id) if company_id else None

        record = normalise_vacancy(vacancy, company, use_llm=use_llm)
        reason = rejection_reason(record, company, directive_set)
        if reason:
            report.rejected += 1
            report.rejections[reason] = report.rejections.get(reason, 0) + 1
            continue

        if company_id:
            record["timing_flag"] = signals_mod.timing_flag_for(company_id)

        existing = repo.find_by_vacancy(campaign_id, vacancy["id"], job_seeker_id=seeker_id)
        opportunity_id, created = repo.upsert_synthesised(
            seeker_id, campaign_id, record, existing
        )
        report.opportunity_ids.append(opportunity_id)
        if created:
            report.created += 1
        else:
            report.refreshed += 1

    log.info(
        "Synthesised %d opportunities for campaign %s (%d new, %d refreshed, %d rejected)",
        len(report.opportunity_ids), campaign_id, report.created, report.refreshed,
        report.rejected,
    )
    return report


# ---------------------------------------------------------------------------
# FR-142/FR-144: re-applying the gates to what was already collected
# ---------------------------------------------------------------------------


def _companies_by_id(company_ids: set[str]) -> dict[str, dict]:
    """Bulk-load company rows for a page of opportunities.

    The gate reads a company's name, sectors and markets, so a naive clean-up
    issued one query per row - 50,000 of them. This is one query per page.
    """
    if not company_ids:
        return {}
    out: dict[str, dict] = {}
    ids = [i for i in company_ids if i]
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        marks = ",".join("?" for _ in chunk)
        for row in query_all(
            f"SELECT id, name, country, business_summary, sector_codes, markets "
            f"FROM company WHERE id IN ({marks})",
            tuple(chunk),
        ):
            out[str(row["id"])] = dict(row)
    return out


def _judge_batch(
    batch: list[dict],
    company_ids: set[str],
    directives_model: Any,
    report: PruneReport,
    doomed: list[str],
) -> None:
    """Apply the gates to one chunk, batching the company reads."""
    companies = _companies_by_id(company_ids)
    for row in batch:
        company = companies.get(row.get("company_id"))
        reason = rejection_reason(row, company, directives_model)
        if reason is None:
            continue
        report.reasons[reason] = report.reasons.get(reason, 0) + 1
        doomed.append(str(row["id"]))


@dataclass
class PruneReport:
    """What a relevance clean-up did, for the screen and the job log."""

    campaign_id: str | None = None
    considered: int = 0
    deleted: int = 0
    kept_user_decided: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    dry_run: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "considered": self.considered,
            "deleted": self.deleted,
            "kept_user_decided": self.kept_user_decided,
            "reasons": self.reasons,
            "dry_run": self.dry_run,
        }


def prune_irrelevant(
    job_seeker_id: str,
    campaign_id: str | None = None,
    *,
    dry_run: bool = True,
    page: int = 500,
    use_latest_directives: bool = True,
) -> PruneReport:
    """Remove opportunities the directives no longer admit (FR-142, FR-144).

    The gates in :func:`rejection_reason` run when a vacancy becomes an
    opportunity.  A campaign collected *before* they existed - or before the
    directives were rebuilt - keeps rows those gates would not let through
    today: a Brussels search holding a Norwegian waiting job.  This re-applies
    them and deletes what fails.

    By default the judgement uses the seeker's **latest** directive set, not the
    one the campaign was planned under.  The question this answers is "does this
    row match what I want *now*", and the whole reason to run it is that the
    older directives were wrong; judging by them would reproduce the mistake.

    Two safeguards, because a clean-up is destructive:

    * **A user decision outranks relevance.**  A row that is pinned, selected,
      hand-ranked, tagged, rejected, applied to, or has a pipeline card is
      never removed (NFR-305).
    * **``dry_run`` defaults to true**, so the caller sees the count and the
      reasons before anything goes.
    """
    report = PruneReport(campaign_id=campaign_id, dry_run=dry_run)
    campaigns = (
        [campaign_repo.get_campaign_any(campaign_id)]
        if campaign_id
        else [campaign_repo.get_campaign_any(c["id"]) for c in campaign_repo.list_campaigns(job_seeker_id)]
    )

    latest = None
    if use_latest_directives:
        from dreamjob.db.repositories import directives as directive_repo  # noqa: PLC0415

        ordered = directive_repo.list_for_seeker(job_seeker_id)
        latest = ordered[0] if ordered else None

    for campaign in campaigns:
        if campaign is None:
            continue
        directive_set = latest
        if directive_set is None:
            inputs = campaign_repo.load_planning_inputs(campaign)
            directive_set = inputs.get("directives")
        # Coerce once.  ``rejection_reason`` normalises its argument on every
        # call, which for 50,000 rows is 50,000 parses of the same directive set.
        directives_model = dir_mod.coerce_directive_set(directive_set) if directive_set else None

        # Walk with keyset pagination over the columns the gate reads, batching
        # the company look-ups per chunk.  The wide list query joins the badge
        # and sorts in a temporary B-tree on every page, which made this take
        # minutes over 50,000 rows.
        doomed: list[str] = []
        company_ids: set[str] = set()
        batch: list[dict] = []
        for row in repo.iter_for_relevance(job_seeker_id, campaign["id"], batch=page):
            report.considered += 1
            batch.append(row)
            if row.get("company_id"):
                company_ids.add(row["company_id"])
            if len(batch) >= page:
                _judge_batch(batch, company_ids, directives_model, report, doomed)
                batch, company_ids = [], set()
        if batch:
            _judge_batch(batch, company_ids, directives_model, report, doomed)

        protected = repo.ids_touching_user_decisions(job_seeker_id, doomed)
        report.kept_user_decided += len(protected)
        removable = [i for i in doomed if i not in protected]
        if not dry_run and removable:
            report.deleted += repo.delete_opportunities(job_seeker_id, removable)
        else:
            report.deleted += len(removable)

    log.info(
        "Prune %s: %s considered, %s removable, %s kept (user decided)",
        "preview" if dry_run else "applied",
        report.considered,
        report.deleted,
        report.kept_user_decided,
    )
    return report


# ---------------------------------------------------------------------------
# FR-263: a third kind of row, distinct from both vacancy and speculative
# ---------------------------------------------------------------------------

#: Titles an employer uses for an **open application** advert: a published
#: posting that invites you to write without naming a role.  Companies put
#: these on their own ATS boards (".../o/spontaneous-application"), so they are
#: genuinely published - which is why they are ``kind="vacancy"`` and not
#: speculative - but they advertise no specific job, so a reader deciding what
#: to apply to needs to see that at a glance.
OPEN_APPLICATION_PATTERNS = (
    r"\bspontaneous (application|applications|vacancy|speculative)\b",
    r"\bopen application\b",
    r"\bunsolicited application\b",
    r"\bspeculative application\b",
    r"\bgeneral application\b",
    r"\bopen sollicitatie\b",
    r"\bspontane sollicitatie\b",
    r"\bcandidature spontan[ée]e\b",
    r"\btalent (pool|community|pipeline)\b",
    r"\bjoin our talent\b",
    r"\bevergreen (role|posting)\b",
)
_OPEN_APPLICATION_RE = re.compile("|".join(OPEN_APPLICATION_PATTERNS), re.IGNORECASE)


def is_open_application(title: str | None, description: str | None = None) -> bool:
    """Is this a published posting that names no role? (FR-263, FR-261)

    The distinction the ranked list has to make is three-way, not two-way:

    * **advertised vacancy** - a specific role the employer has published;
    * **open application** - published, but inviting you to write *without* a
      named role.  Real, but not something to score as a fit for a particular
      job;
    * **speculative opening** - inferred by the model, published nowhere.

    Only the title and the opening lines are read: an ordinary posting whose
    body happens to contain the phrase once is not an open application.
    """
    if not title:
        return False
    if _OPEN_APPLICATION_RE.search(title):
        return True
    # Some boards name the role normally and put the invitation in the body.
    return bool(description and _OPEN_APPLICATION_RE.search(description[:200]))
