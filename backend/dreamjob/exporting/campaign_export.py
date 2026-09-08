"""Self-contained campaign export package (FR-463, FR-106, NFR-301, NFR-702).

The package a job seeker hands to a career coach is one zip file containing:

``campaign.pdf``      the readable bundle - ranked list, company profiles,
                      dream-job intelligence, application history - built with
                      the same :mod:`dreamjob.documents.pdf_builder` furniture
                      as the briefing and the motivation document, so the pages
                      state what they were generated from (FR-331).
``campaign.json``     the same content, machine-readable
                      (:mod:`dreamjob.exporting.json_export`).
``documents/``        the PDFs and DOCX files already generated for this
                      campaign - CVs, briefings, motivation documents - copied
                      in, so the package survives without the application.
``MANIFEST.json``     what is in the package, what was left out and why.

Both faces are rendered from one structure, so they cannot disagree, and that
structure is refused outright if it can reach another job seeker's data
(:func:`json_export.verify_isolation`).
"""

from __future__ import annotations

import json
import logging
import shutil
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dreamjob.config import get_settings
from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import exporting as repo
from dreamjob.documents.pdf_builder import (
    DocumentMeta,
    PdfBuilder,
    cover_page,
    format_money,
    normalise_language,
)
from dreamjob.exporting import json_export

log = logging.getLogger(__name__)

EXPORTS_DIRNAME = "exports"
DOCUMENTS_DIRNAME = "documents"

MAX_COMPANIES_IN_PDF = 40
MAX_OPPORTUNITY_DETAILS = 25


@dataclass
class ExportResult:
    """What the API hands back after an export (FR-463)."""

    export_id: str
    zip_path: Path
    pdf_path: Path
    json_path: Path
    manifest: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.export_id,
            "zip_path": str(self.zip_path),
            "pdf_path": str(self.pdf_path),
            "json_path": str(self.json_path),
            "byte_size": self.zip_path.stat().st_size if self.zip_path.exists() else None,
            "manifest": self.manifest,
        }


# ---------------------------------------------------------------------------
# The PDF bundle
# ---------------------------------------------------------------------------


def _score(value: Any) -> str:
    return "-" if value is None else f"{float(value):.0f}"


def _ranked_list_section(builder: PdfBuilder, payload: dict[str, Any]) -> None:
    opportunities = payload.get("ranked_list") or []
    builder.h1("Ranked list")
    builder.para(
        f"{len(opportunities)} opportunities, in the order the campaign screen shows them: "
        "the job seeker's own order first, then pins, then the computed score. Scores are "
        "advisory (NFR-305)."
    )
    rows = []
    for index, opportunity in enumerate(opportunities, start=1):
        rows.append(
            [
                str(index),
                str(opportunity.get("title") or ""),
                str(opportunity.get("company_name") or ""),
                "speculative" if opportunity.get("kind") == "speculative" else "vacancy",
                _score(opportunity.get("score")),
                _score(opportunity.get("score_dream_fit")),
                str(opportunity.get("user_status") or "new"),
            ]
        )
    builder.table(
        ["#", "Role", "Company", "Kind", "Score", "Dream fit", "Status"],
        rows,
        col_widths=[4, 26, 20, 10, 8, 9, 11],
        align_right_from=4,
    )


def _opportunity_details(builder: PdfBuilder, payload: dict[str, Any]) -> None:
    opportunities = [
        o
        for o in (payload.get("ranked_list") or [])
        if o.get("selected") or o.get("user_status") in ("interested", "applied")
    ][:MAX_OPPORTUNITY_DETAILS]
    if not opportunities:
        return
    builder.page_break()
    builder.h1("Selected opportunities")
    for opportunity in opportunities:
        builder.h2(
            f"{opportunity.get('title')} - {opportunity.get('company_name') or 'unknown company'}"
        )
        builder.key_values(
            [
                ("Score", _score(opportunity.get("score"))),
                ("Dream-job fit", _score(opportunity.get("score_dream_fit"))),
                ("Location", opportunity.get("location")),
                ("Work arrangement", opportunity.get("work_arrangement")),
                ("Status", opportunity.get("user_status")),
                ("Source", opportunity.get("vacancy_source_url")),
            ]
        )
        builder.para(opportunity.get("rationale") or "")
        meter = opportunity.get("dream_fit_detail") or {}
        if isinstance(meter, dict) and meter.get("violated"):
            builder.h3("Dream-job criteria not met")
            builder.bullets(
                [
                    f"{c.get('criterion')}: {c.get('explanation')}"
                    for c in meter.get("violated", [])[:6]
                ]
            )
        builder.spacer(3)


def _company_section(builder: PdfBuilder, payload: dict[str, Any]) -> None:
    companies = (payload.get("companies") or [])[:MAX_COMPANIES_IN_PDF]
    if not companies:
        return
    analyses = {
        str(a["company_id"]): a for a in (payload.get("financials") or {}).get("analyses") or []
    }
    years: dict[str, list[dict]] = {}
    for row in (payload.get("financials") or {}).get("years") or []:
        years.setdefault(str(row["company_id"]), []).append(row)
    signals: dict[str, list[dict]] = {}
    for row in payload.get("hiring_signals") or []:
        signals.setdefault(str(row["company_id"]), []).append(row)
    warnings = {
        str(w["company_id"]): w for w in (payload.get("intelligence") or {}).get(
            "values_warnings"
        ) or []
    }

    builder.page_break()
    builder.h1("Company profiles")
    for company in companies:
        company_id = str(company["id"])
        builder.h2(str(company.get("name") or ""))
        builder.key_values(
            [
                ("Domain", company.get("domain")),
                ("Country", company.get("country")),
                ("Size", company.get("size_band") or company.get("size_fte")),
                ("Stage", company.get("stage")),
                ("Ownership", company.get("ownership")),
                ("Trajectory", company.get("trajectory")),
                (
                    "Collected",
                    str(company.get("refreshed_at") or company.get("collected_at") or "")[:10],
                ),
            ]
        )
        builder.para(company.get("business_summary") or "")

        analysis = analyses.get(company_id)
        if analysis:
            builder.h3("Financial reading")
            builder.key_values(
                [
                    ("Trajectory", analysis.get("trajectory")),
                    ("Ability to pay", analysis.get("ability_to_pay")),
                    ("Investment capacity", analysis.get("investment_capacity")),
                    ("Estimated figures", "yes" if analysis.get("is_estimated") else "no"),
                ]
            )
        rows = [
            [
                str(y.get("fiscal_year")),
                format_money(y.get("revenue"), y.get("currency") or "EUR"),
                format_money(y.get("ebitda"), y.get("currency") or "EUR"),
                format_money(y.get("net_result"), y.get("currency") or "EUR"),
                "-" if y.get("headcount_fte") is None else f"{float(y['headcount_fte']):.0f}",
            ]
            for y in sorted(years.get(company_id, []), key=lambda r: r.get("fiscal_year") or 0)
        ]
        if rows:
            builder.table(
                ["Year", "Revenue", "EBITDA", "Net result", "FTE"],
                rows,
                col_widths=[10, 24, 22, 22, 12],
                align_right_from=1,
            )

        company_signals = signals.get(company_id, [])[:6]
        if company_signals:
            builder.h3("Hiring signals")
            builder.bullets(
                [
                    f"{(s.get('occurred_at') or s.get('collected_at') or '')[:10]} "
                    f"{s.get('signal_type')}: {s.get('description')}"
                    for s in company_signals
                ]
            )

        warning = warnings.get(company_id)
        if warning and warning.get("warnings"):
            builder.h3("Values and working style - warnings (FR-384)")
            builder.bullets([w.get("explanation") for w in warning["warnings"][:6]])
        builder.spacer(4)


def _intelligence_section(builder: PdfBuilder, payload: dict[str, Any]) -> None:
    intelligence = payload.get("intelligence") or {}
    gap = intelligence.get("gap_analysis")
    stones = intelligence.get("stepping_stones") or []
    if not gap and not stones:
        return
    builder.page_break()
    builder.h1("Dream-job intelligence")

    if gap:
        builder.h2("Gap analysis (FR-381)")
        builder.para(gap.get("summary") or "")
        for item in (gap.get("gaps") or [])[:12]:
            action = item.get("closing_action") or {}
            effort = item.get("effort") or {}
            builder.h3(str(item.get("label") or ""))
            builder.key_values(
                [
                    ("Dimension", item.get("dimension")),
                    ("Severity", item.get("severity")),
                    ("Evidence", item.get("evidence")),
                    ("Closing action", action.get("action")),
                    ("How", action.get("detail")),
                    (
                        "Effort",
                        f"{effort.get('months')} months - {effort.get('detail')}"
                        if effort.get("months")
                        else effort.get("detail"),
                    ),
                    (
                        "Decisive for",
                        f"{item.get('decisive_count', 0)} opportunities "
                        f"({item.get('total_points_lost', 0)} profile-fit points)",
                    ),
                ]
            )
            builder.spacer(2)

    if stones:
        builder.h2("Stepping-stone paths (FR-382)")
        for path in stones:
            builder.h3(str(path.get("name") or ""))
            builder.para(path.get("rationale") or "")
            builder.bullets(
                [
                    f"Step {step.get('position')} (+{step.get('horizon_months')} months): "
                    f"{step.get('role')} - {step.get('rationale')}"
                    for step in (path.get("steps") or [])
                ]
            )
            builder.spacer(2)


def _history_section(builder: PdfBuilder, payload: dict[str, Any]) -> None:
    history = payload.get("applications") or []
    builder.page_break()
    builder.h1("Application history")
    if not history:
        builder.para("No application package has been generated for this campaign yet.")
        return
    rows = []
    for entry in history:
        package = entry.get("application_package") or {}
        dispatches = entry.get("dispatches") or []
        replies = sum(len(d.get("replies") or []) for d in dispatches)
        cards = entry.get("pipeline_cards") or []
        rows.append(
            [
                str(package.get("opportunity_id") or "")[:8],
                str(package.get("language") or ""),
                str(package.get("status") or ""),
                (str(dispatches[0].get("sent_at") or "")[:10] if dispatches else "-"),
                str(len(dispatches)),
                str(replies),
                str(cards[0].get("stage") if cards else "-"),
            ]
        )
    builder.table(
        ["Opportunity", "Lang", "Package", "Sent", "Dispatches", "Replies", "Stage"],
        rows,
        col_widths=[16, 8, 14, 14, 14, 12, 14],
        align_right_from=4,
    )
    documents = (payload.get("attachments") or {}).get("documents") or []
    if documents:
        builder.h2("Documents included in this package")
        builder.bullets([Path(str(d)).name for d in documents])


def _events_section(builder: PdfBuilder, payload: dict[str, Any]) -> None:
    events = payload.get("events") or []
    if not events:
        return
    builder.h1("Events (FR-462)")
    builder.table(
        ["Date", "Event", "Where", "Status"],
        [
            [
                str(e.get("starts_at") or "")[:10],
                str(e.get("name") or ""),
                str(e.get("location") or ""),
                str(e.get("status") or ""),
            ]
            for e in events[:40]
        ],
        col_widths=[12, 40, 26, 12],
    )


def build_pdf(payload: dict[str, Any], path: Path, *, language: str = "en") -> Path:
    """Render the readable half of the package (FR-463, FR-331)."""
    campaign = payload.get("campaign") or {}
    seeker = payload.get("job_seeker") or {}
    profile = (payload.get("profile") or {}).get("profile_version") or {}
    privacy = payload.get("privacy") or {}
    counts = payload.get("counts") or {}

    meta = DocumentMeta(
        title=f"Campaign export - {campaign.get('name') or 'campaign'}",
        subtitle="Ranked list, company profiles, intelligence and application history",
        language=normalise_language(language),
        generated_at=(payload.get("export") or {}).get("generated_at") or utcnow(),
        prepared_for=str(seeker.get("display_name") or ""),
        profile_version=(
            str(profile.get("version")) if profile.get("version") is not None else None
        ),
        seeker_only=True,
    )
    builder = PdfBuilder(meta)
    cover_page(
        builder,
        heading=f"Campaign export: {campaign.get('name') or ''}",
        subheading="Everything this campaign produced, in one document (FR-463)",
        facts=[
            ("Campaign status", campaign.get("status")),
            ("Opportunities", counts.get("opportunities")),
            ("Companies", counts.get("companies")),
            ("Application packages", counts.get("application_packages")),
            ("Contacts", counts.get("contacts")),
            (
                "Do-not-disclose fields applied",
                "yes" if privacy.get("do_not_disclose_applied") else "none set",
            ),
            ("Discretion mode", "on" if privacy.get("discretion_mode") else "off"),
        ],
        footnote=(
            "This package contains the data of one job seeker only. Company, vacancy and "
            "contact records are shared knowledge-base rows and carry no link to any job "
            "seeker (FR-344, FR-463). Third-party contact details are professional contact "
            "data; treat them accordingly (NFR-302)."
        ),
    )
    _ranked_list_section(builder, payload)
    _opportunity_details(builder, payload)
    _intelligence_section(builder, payload)
    _company_section(builder, payload)
    _history_section(builder, payload)
    _events_section(builder, payload)
    return builder.build(path)


# ---------------------------------------------------------------------------
# The package
# ---------------------------------------------------------------------------


def exports_dir() -> Path:
    directory = Path(get_settings().generated_dir) / EXPORTS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _copy_documents(paths: list[str], target: Path) -> list[dict[str, Any]]:
    """Copy the already-generated documents in, keeping a note of the misses."""
    copied: list[dict[str, Any]] = []
    for raw in paths:
        source = Path(str(raw))
        if not source.is_file():
            copied.append({"path": str(source), "included": False, "reason": "file not found"})
            continue
        target.mkdir(parents=True, exist_ok=True)
        destination = target / source.name
        if destination.exists():
            destination = target / f"{source.stem}-{len(copied)}{source.suffix}"
        shutil.copy2(source, destination)
        copied.append(
            {
                "path": str(source),
                "included": True,
                "name": destination.name,
                "bytes": destination.stat().st_size,
            }
        )
    return copied


def build_package(
    job_seeker_id: str,
    campaign_id: str,
    *,
    language: str = "en",
    include_pdf: bool = True,
    include_documents: bool = True,
    include_contacts: bool = True,
) -> ExportResult:
    """Build the zip package for one campaign (FR-463)."""
    payload = json_export.build(
        job_seeker_id, campaign_id, language=language, include_contacts=include_contacts
    )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = exports_dir() / job_seeker_id
    staging = root / f"{campaign_id}-{stamp}"
    staging.mkdir(parents=True, exist_ok=True)

    json_path = staging / "campaign.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    pdf_path = staging / "campaign.pdf"
    if include_pdf:
        build_pdf(payload, pdf_path, language=language)

    documents = (
        _copy_documents(
            list((payload.get("attachments") or {}).get("documents") or []),
            staging / DOCUMENTS_DIRNAME,
        )
        if include_documents
        else []
    )

    manifest = {
        "requirement": "FR-463",
        "generated_at": (payload.get("export") or {}).get("generated_at"),
        "job_seeker_id": job_seeker_id,
        "campaign_id": campaign_id,
        "language": language,
        "counts": payload.get("counts"),
        "privacy": payload.get("privacy"),
        "files": {
            "json": json_path.name,
            "pdf": pdf_path.name if include_pdf else None,
            "documents": documents,
        },
        "isolation_check": {
            "method": "json_export.verify_isolation",
            "result": "passed - no other job seeker's identifier appears in this package",
        },
    }
    (staging / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    zip_path = root / f"{campaign_id}-{stamp}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(staging.rglob("*")):
            if file_path.is_file():
                archive.write(file_path, file_path.relative_to(staging))

    export_id = repo.record_export(
        job_seeker_id,
        {
            "campaign_id": campaign_id,
            "language": language,
            "zip_path": str(zip_path),
            "pdf_path": str(pdf_path) if include_pdf else None,
            "json_path": str(json_path),
            "byte_size": zip_path.stat().st_size,
            "manifest": manifest,
            "status": "ready",
        },
    )
    # NFR-702: an export leaves the machine, so the fact that it was made is
    # part of the audit trail.
    repo.record_audit(
        job_seeker_id,
        "campaign.exported",
        export_id,
        {"campaign_id": campaign_id, "counts": payload.get("counts"), "zip": str(zip_path)},
    )
    return ExportResult(
        export_id=export_id,
        zip_path=zip_path,
        pdf_path=pdf_path,
        json_path=json_path,
        manifest=manifest,
    )


def get_export(job_seeker_id: str, export_id: str) -> dict[str, Any] | None:
    return repo.get_export(export_id, job_seeker_id)


def list_exports(job_seeker_id: str, campaign_id: str | None = None) -> list[dict[str, Any]]:
    return repo.list_exports(job_seeker_id, campaign_id)
