"""Machine-readable campaign export (FR-463, FR-106, FR-344, NFR-301, NFR-302).

FR-463 asks for a self-contained package with two faces: a PDF bundle to read
and a machine-readable export with the same content, for use outside the
application or with a career coach.  This module builds the second face, and
the PDF bundle is rendered from exactly this structure, so the two cannot drift
apart.

Two acceptance criteria shape the code more than the schema does.

**No data of any other job seeker.**  Every read starts from this seeker's own
rows and reaches shared rows by id (see
:mod:`dreamjob.db.repositories.exporting`).  On top of that,
:func:`verify_isolation` walks the finished structure and fails the export if
any ``job_seeker_id`` anywhere in it is not the owner's - a belt-and-braces
check that turns the criterion into something a test can assert and a bug
cannot quietly break.

**Do-not-disclose flags are respected.**  The FR-106 field paths are applied to
the profile blocks with the same redactor that guards the LLM egress path, and
the export states which paths were applied so the reader knows the document is
incomplete by request.
"""

from __future__ import annotations

import logging
from typing import Any

from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import exporting as repo
from dreamjob.intelligence import values_match as values_mod
from dreamjob.llm.client import redact
from dreamjob.pipeline import directives as dir_mod

log = logging.getLogger(__name__)

EXPORT_FORMAT = "dreamjob.campaign"
EXPORT_VERSION = "1.0"


class ForeignDataError(RuntimeError):
    """Raised when an export would carry another job seeker's data (FR-463)."""


# ---------------------------------------------------------------------------
# Isolation check (FR-463 acceptance criterion, FR-101, FR-344)
# ---------------------------------------------------------------------------

_SEEKER_KEYS = frozenset({"job_seeker_id", "seeker_id", "owner_id"})


def verify_isolation(payload: Any, job_seeker_id: str, path: str = "$") -> list[str]:
    """Every place in the structure that names a different job seeker."""
    offenders: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            here = f"{path}.{key}"
            if key.lower() in _SEEKER_KEYS and value not in (None, "", job_seeker_id):
                offenders.append(here)
            offenders.extend(verify_isolation(value, job_seeker_id, here))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            offenders.extend(verify_isolation(item, job_seeker_id, f"{path}[{index}]"))
    return offenders


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def _redact_profile(block: Any, paths: set[str]) -> Any:
    """FR-106: drop the fields the seeker never wants to appear in output."""
    if not block:
        return block
    return redact(block, paths)


def _application_history(
    job_seeker_id: str, opportunity_ids: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """FR-321..331 and FR-421..425, assembled per opportunity."""
    packages = repo.application_packages(job_seeker_id, opportunity_ids)
    package_ids = [str(p["id"]) for p in packages]
    dispatches = repo.dispatches(job_seeker_id, package_ids)
    dispatch_ids = [str(d["id"]) for d in dispatches]
    replies = repo.replies(job_seeker_id, dispatch_ids)
    cards = repo.pipeline_cards(job_seeker_id, opportunity_ids)

    by_package: dict[str, list[dict]] = {}
    for dispatch in dispatches:
        by_package.setdefault(str(dispatch["application_package_id"]), []).append(dispatch)
    by_dispatch: dict[str, list[dict]] = {}
    for reply in replies:
        by_dispatch.setdefault(str(reply.get("dispatch_id")), []).append(reply)
    cards_by_opportunity: dict[str, list[dict]] = {}
    for card in cards:
        cards_by_opportunity.setdefault(str(card["opportunity_id"]), []).append(card)

    history: list[dict[str, Any]] = []
    documents: list[str] = []
    for package in packages:
        sent = by_package.get(str(package["id"]), [])
        for path_key in ("cv_pdf_path", "cv_docx_path", "briefing_pdf_path",
                         "motivation_pdf_path"):
            if package.get(path_key):
                documents.append(str(package[path_key]))
        history.append(
            {
                "application_package": package,
                "dispatches": [
                    {**dispatch, "replies": by_dispatch.get(str(dispatch["id"]), [])}
                    for dispatch in sent
                ],
                "pipeline_cards": cards_by_opportunity.get(str(package["opportunity_id"]), []),
            }
        )
    return history, documents


def build(
    job_seeker_id: str,
    campaign_id: str,
    *,
    language: str = "en",
    include_contacts: bool = True,
    include_values_warnings: bool = True,
) -> dict[str, Any]:
    """The complete campaign, as one JSON-serialisable structure (FR-463)."""
    campaign = repo.campaign(campaign_id, job_seeker_id)
    if campaign is None:
        raise LookupError(f"No campaign {campaign_id} for this job seeker")

    seeker = repo.seeker(job_seeker_id) or {}
    directive_set = repo.directive_set(campaign.get("directive_set_id"), job_seeker_id)
    discretion = bool((directive_set or {}).get("discretion_mode"))
    disclosure_paths = repo.disclosure_paths(job_seeker_id)

    profile_version = repo.profile_version(campaign.get("profile_version_id"), job_seeker_id)
    composite = repo.composite_profile(campaign.get("composite_profile_id"), job_seeker_id)
    dream = repo.dream_job_model(campaign.get("dream_job_model_id"), job_seeker_id)

    opportunities = repo.opportunities(job_seeker_id, campaign_id)
    company_ids = sorted({str(o["company_id"]) for o in opportunities if o.get("company_id")})
    vacancy_ids = sorted({str(o["vacancy_id"]) for o in opportunities if o.get("vacancy_id")})
    companies = repo.companies(company_ids)

    # FR-385: a company excluded by discretion mode has no business in a
    # document the job seeker may hand to a third party.
    excluded_companies: list[str] = []
    if directive_set and discretion:
        kept: list[dict[str, Any]] = []
        for company in companies:
            reason = dir_mod.exclusion_reason(directive_set, company)
            if reason:
                excluded_companies.append(str(company["id"]))
                continue
            kept.append(company)
        companies = kept
        opportunities = [
            o for o in opportunities if str(o.get("company_id") or "") not in excluded_companies
        ]
        company_ids = [c for c in company_ids if c not in excluded_companies]

    opportunity_ids = [str(o["id"]) for o in opportunities]
    history, documents = _application_history(job_seeker_id, opportunity_ids)

    values_warnings: list[dict[str, Any]] = []
    if include_values_warnings:
        for company in companies:
            try:
                match = values_mod.for_company(
                    job_seeker_id, str(company["id"]), campaign_id=campaign_id, use_llm=False
                )
            except LookupError:  # pragma: no cover - company deleted mid-export
                continue
            if match.warnings:
                values_warnings.append(match.as_dict())

    payload: dict[str, Any] = {
        "export": {
            "format": EXPORT_FORMAT,
            "version": EXPORT_VERSION,
            "generated_at": utcnow(),
            "language": language,
            "requirement": "FR-463",
            "scope": "one campaign of one job seeker",
        },
        "job_seeker": {
            "id": seeker.get("id"),
            "display_name": seeker.get("display_name"),
            "email": seeker.get("email"),
            "locale": seeker.get("locale"),
        },
        "campaign": campaign,
        "directives": directive_set,
        "profile": {
            "profile_version": _redact_profile(profile_version, disclosure_paths),
            "composite_profile": _redact_profile(composite, disclosure_paths),
            "dream_job_model": dream,
        },
        "ranked_list": opportunities,
        "companies": companies,
        "vacancies": repo.vacancies(vacancy_ids),
        "financials": {
            "years": repo.financial_years(company_ids),
            "analyses": repo.financial_analyses(company_ids),
        },
        "hiring_signals": repo.hiring_signals(company_ids),
        "contacts": repo.contacts(company_ids, campaign_id) if include_contacts else [],
        "introduction_paths": repo.introduction_paths(job_seeker_id, opportunity_ids),
        "applications": history,
        "intelligence": {
            "gap_analysis": repo.gap_analysis(job_seeker_id, campaign_id),
            "stepping_stones": repo.stepping_stones(job_seeker_id, campaign_id),
            "values_warnings": values_warnings,
        },
        "events": repo.event_interests(job_seeker_id),
        "privacy": {
            "do_not_disclose_paths": sorted(disclosure_paths),
            "do_not_disclose_applied": bool(disclosure_paths),
            "discretion_mode": discretion,
            "companies_excluded_by_discretion": len(excluded_companies),
            "contacts_basis": (
                "Third-party contact details are professional contact data processed on a "
                "legitimate-interest basis; contacts who objected are excluded (NFR-302)."
            ),
            "isolation": (
                "This export contains data of one job seeker only. Shared knowledge-base "
                "rows carry no link to any job seeker (FR-344)."
            ),
        },
        "counts": {
            "opportunities": len(opportunities),
            "companies": len(companies),
            "vacancies": len(vacancy_ids),
            "contacts": 0,
            "application_packages": len(history),
            "documents": len(documents),
        },
        "attachments": {"documents": documents},
    }
    payload["counts"]["contacts"] = len(payload["contacts"])

    offenders = verify_isolation(payload, job_seeker_id)
    if offenders:
        # FR-463 is accepted on this being impossible; refuse rather than warn.
        raise ForeignDataError(
            "Export aborted: another job seeker's data was reachable at "
            + ", ".join(offenders[:5])
        )
    return payload


def summarise(payload: dict[str, Any]) -> dict[str, Any]:
    """The one-screen summary of an export, for the API response and the PDF."""
    campaign = payload.get("campaign") or {}
    return {
        "campaign_id": campaign.get("id"),
        "campaign_name": campaign.get("name"),
        "generated_at": (payload.get("export") or {}).get("generated_at"),
        "counts": payload.get("counts"),
        "privacy": payload.get("privacy"),
    }
