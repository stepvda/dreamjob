"""Shared reportlab layer for the generated PDFs (FR-329, FR-330, FR-331).

Both job-seeker documents - the interview briefing (FR-329) and the motivation
and fit document (FR-330) - are built from the same furniture, and FR-331 fixes
what that furniture has to carry: a configurable template, the language of the
opportunity, the generation date and the profile/company data versions used.
Those three facts are drawn by the canvas on *every* page, so a printed page
that has been separated from its cover still says what it was generated from.

The chart primitives use ``reportlab.graphics`` rather than a plotting library:
FR-329 asks for a five-year financial analysis with charts, and the bar/line
charts here cover that without adding a dependency (CR-408 keeps the stack
small).  Missing years are a normal case in company filings, so every chart
takes ``None`` for "not filed" and renders the gap rather than a zero.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.legends import Legend
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.shapes import Drawing, Line, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import (
    Flowable,
    KeepTogether,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ---------------------------------------------------------------------------
# Language (FR-322, FR-331, NFR-501: content language is per opportunity)
# ---------------------------------------------------------------------------

#: Languages the generators produce content in.  ``en`` is the fallback.
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "nl", "fr", "de")

LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "nl": "Nederlands",
    "fr": "Français",
    "de": "Deutsch",
}

LABELS: dict[str, dict[str, str]] = {
    "en": {
        # page furniture
        "page_of": "Page {page} of {total}",
        "generated_on": "Generated",
        "data_versions": "Data versions",
        "profile_version": "Profile version",
        "company_snapshot": "Company data as of",
        "for_the_job_seeker_only": "Prepared for the job seeker only - not sent to the employer.",
        "prepared_for": "Prepared for",
        "not_available": "not available",
        "estimated": "estimated",
        "source": "Source",
        # CV
        "cv_profile": "Profile",
        "cv_experience": "Experience",
        "cv_education": "Education",
        "cv_skills": "Skills",
        "cv_languages": "Languages",
        "cv_certifications": "Certifications",
        "cv_publications": "Publications",
        "cv_projects": "Projects",
        "cv_honors": "Honours and awards",
        "cv_volunteering": "Volunteering",
        "cv_courses": "Courses",
        "cv_present": "present",
        "cv_curriculum_vitae": "Curriculum vitae",
        "signal": "Signal",
        "description": "Description",
        "when": "When",
        "competitor": "Competitor",
        "basis": "Basis",
        "size": "Size",
        "stage": "Stage",
        "ownership": "Ownership",
        "name": "Name",
        "role": "Role",
        "department": "Department",
        "email": "Email",
        "validation": "Validation",
        "location": "Location",
        "work_arrangement": "Work arrangement",
        "contract": "Contract",
        "seniority": "Seniority",
        "posted": "Posted",
        "compensation": "Compensation",
        "employer_rating": "Employer rating",
        "tech_stack": "Technology",
        "values_culture": "Values and culture",
        "introduction_path": "Introduction path",
        "gross_profit": "Gross profit",
        "sector": "Sector",
        "domain": "Website",
        "plausibility": "Plausibility",
        "why_plausible": "Why this opening is plausible",
        "revenue_cagr": "Revenue CAGR",
        "headcount_cagr": "Headcount CAGR",
        "margin_trend": "Margin trend",
        "personnel_cost_per_fte": "Personnel cost per FTE",
        "current_ratio": "Current ratio",
        "solvency_ratio": "Solvency ratio",
        "estimated_note": (
            "Some figures are estimated from partial filings; treat them as indicative."
        ),
        "watch_out": "Watch out",
        "prepare_answer": "Prepare an answer for this before the interview.",
        "gap_objection": "The profile does not evidence: {requirement}",
        "no_requirements_stated": (
            "The opening states no requirements; nothing has been inferred for you."
        ),
        "working_style": "Working style",
        # briefing (FR-329)
        "briefing_title": "Interview briefing",
        "briefing_subtitle": "Company and job briefing",
        "company_profile": "Company profile",
        "business": "Business",
        "products_services": "Products and services",
        "markets": "Markets",
        "reference_customers": "Reference customers",
        "size_and_locations": "Size and locations",
        "structure": "Structure and departments",
        "key_people": "Key people",
        "financials": "Five-year financial analysis",
        "financial_table": "Reported figures",
        "revenue": "Revenue",
        "ebitda": "EBITDA",
        "ebit": "EBIT",
        "net_result": "Net result",
        "equity": "Equity",
        "headcount": "Headcount (FTE)",
        "personnel_costs": "Personnel costs",
        "fiscal_year": "Fiscal year",
        "trajectory": "Trajectory",
        "ability_to_pay": "Ability to pay",
        "investment_capacity": "Investment capacity",
        "revenue_and_result": "Revenue and result",
        "headcount_trend": "Headcount trend",
        "hiring_signals": "Hiring signals and recent news",
        "news": "Recent news",
        "competitors": "Competitors and market position",
        "the_opening": "The opening",
        "vacancy_full_text": "Vacancy, in full",
        "speculative_opening": "Speculative opening - no vacancy has been advertised",
        "hiring_contact": "Hiring contact and introduction path",
        "interview_topics": "Likely interview topics and questions",
        "questions_to_ask": "Questions to ask them",
        "requirements": "Requirements",
        "desirable": "Nice to have",
        # motivation (FR-330)
        "motivation_title": "Motivation and fit",
        "motivation_subtitle": "Preparation for the interview",
        "why_this_job": "Why I want this job",
        "why_fit_job": "Why I fit the job",
        "why_fit_company": "Why I fit the company",
        "objections": "Anticipated objections and answers",
        "talking_points": "Talking points to rehearse",
        "requirement": "Requirement",
        "evidence": "Evidence from the profile",
        "objection": "Objection",
        "answer": "Answer",
        "dream_job_link": "Link to the dream job",
        "career_trajectory": "Career trajectory",
        "no_evidence": "No evidence in the profile - do not claim this.",
    },
    "nl": {
        "page_of": "Pagina {page} van {total}",
        "generated_on": "Gegenereerd",
        "data_versions": "Dataversies",
        "profile_version": "Profielversie",
        "company_snapshot": "Bedrijfsgegevens per",
        "for_the_job_seeker_only": (
            "Alleen voor de werkzoekende - wordt niet naar de werkgever gestuurd."
        ),
        "prepared_for": "Opgesteld voor",
        "not_available": "niet beschikbaar",
        "estimated": "geschat",
        "source": "Bron",
        "cv_profile": "Profiel",
        "cv_experience": "Werkervaring",
        "cv_education": "Opleiding",
        "cv_skills": "Vaardigheden",
        "cv_languages": "Talen",
        "cv_certifications": "Certificaten",
        "cv_publications": "Publicaties",
        "cv_projects": "Projecten",
        "cv_honors": "Onderscheidingen",
        "cv_volunteering": "Vrijwilligerswerk",
        "cv_courses": "Cursussen",
        "cv_present": "heden",
        "cv_curriculum_vitae": "Curriculum vitae",
        "signal": "Signaal",
        "description": "Omschrijving",
        "when": "Wanneer",
        "competitor": "Concurrent",
        "basis": "Basis",
        "size": "Omvang",
        "stage": "Fase",
        "ownership": "Eigendom",
        "name": "Naam",
        "role": "Rol",
        "department": "Afdeling",
        "email": "E-mail",
        "validation": "Validatie",
        "location": "Locatie",
        "work_arrangement": "Werkvorm",
        "contract": "Contract",
        "seniority": "Niveau",
        "posted": "Gepubliceerd",
        "compensation": "Verloning",
        "employer_rating": "Werkgeversbeoordeling",
        "tech_stack": "Technologie",
        "values_culture": "Waarden en cultuur",
        "introduction_path": "Introductiepad",
        "gross_profit": "Brutomarge",
        "sector": "Sector",
        "domain": "Website",
        "plausibility": "Plausibiliteit",
        "why_plausible": "Waarom deze opening plausibel is",
        "revenue_cagr": "Omzet CAGR",
        "headcount_cagr": "CAGR personeelsbestand",
        "margin_trend": "Margetrend",
        "personnel_cost_per_fte": "Personeelskost per VTE",
        "current_ratio": "Current ratio",
        "solvency_ratio": "Solvabiliteit",
        "estimated_note": (
            "Sommige cijfers zijn geschat uit deelrapportering; lees ze als indicatief."
        ),
        "watch_out": "Let op",
        "prepare_answer": "Bereid hier zelf een antwoord op voor.",
        "gap_objection": "Het profiel toont geen bewijs voor: {requirement}",
        "no_requirements_stated": (
            "De opening vermeldt geen vereisten; er is niets voor u afgeleid."
        ),
        "working_style": "Manier van werken",
        "briefing_title": "Sollicitatiebriefing",
        "briefing_subtitle": "Bedrijfs- en functiebriefing",
        "company_profile": "Bedrijfsprofiel",
        "business": "Activiteiten",
        "products_services": "Producten en diensten",
        "markets": "Markten",
        "reference_customers": "Referentieklanten",
        "size_and_locations": "Omvang en vestigingen",
        "structure": "Structuur en afdelingen",
        "key_people": "Sleutelfiguren",
        "financials": "Financiële analyse over vijf jaar",
        "financial_table": "Gerapporteerde cijfers",
        "revenue": "Omzet",
        "ebitda": "EBITDA",
        "ebit": "EBIT",
        "net_result": "Nettoresultaat",
        "equity": "Eigen vermogen",
        "headcount": "Personeelsbestand (VTE)",
        "personnel_costs": "Personeelskosten",
        "fiscal_year": "Boekjaar",
        "trajectory": "Ontwikkeling",
        "ability_to_pay": "Betaalcapaciteit",
        "investment_capacity": "Investeringscapaciteit",
        "revenue_and_result": "Omzet en resultaat",
        "headcount_trend": "Verloop personeelsbestand",
        "hiring_signals": "Aanwervingssignalen en recent nieuws",
        "news": "Recent nieuws",
        "competitors": "Concurrenten en marktpositie",
        "the_opening": "De opening",
        "vacancy_full_text": "Vacature, volledig",
        "speculative_opening": "Spontane opening - er is geen vacature gepubliceerd",
        "hiring_contact": "Contactpersoon en introductiepad",
        "interview_topics": "Waarschijnlijke gespreksonderwerpen en vragen",
        "questions_to_ask": "Vragen om zelf te stellen",
        "requirements": "Vereisten",
        "desirable": "Pluspunten",
        "motivation_title": "Motivatie en fit",
        "motivation_subtitle": "Voorbereiding op het gesprek",
        "why_this_job": "Waarom ik deze functie wil",
        "why_fit_job": "Waarom ik bij de functie pas",
        "why_fit_company": "Waarom ik bij het bedrijf pas",
        "objections": "Verwachte bezwaren en antwoorden",
        "talking_points": "Gesprekspunten om te oefenen",
        "requirement": "Vereiste",
        "evidence": "Bewijs uit het profiel",
        "objection": "Bezwaar",
        "answer": "Antwoord",
        "dream_job_link": "Link met de droombaan",
        "career_trajectory": "Loopbaanlijn",
        "no_evidence": "Geen bewijs in het profiel - beweer dit niet.",
    },
    "fr": {
        "page_of": "Page {page} sur {total}",
        "generated_on": "Généré le",
        "data_versions": "Versions des données",
        "profile_version": "Version du profil",
        "company_snapshot": "Données entreprise au",
        "for_the_job_seeker_only": "Destiné au candidat uniquement - non envoyé à l'employeur.",
        "prepared_for": "Préparé pour",
        "not_available": "non disponible",
        "estimated": "estimé",
        "source": "Source",
        "cv_profile": "Profil",
        "cv_experience": "Expérience professionnelle",
        "cv_education": "Formation",
        "cv_skills": "Compétences",
        "cv_languages": "Langues",
        "cv_certifications": "Certifications",
        "cv_publications": "Publications",
        "cv_projects": "Projets",
        "cv_honors": "Distinctions",
        "cv_volunteering": "Bénévolat",
        "cv_courses": "Formations suivies",
        "cv_present": "à ce jour",
        "cv_curriculum_vitae": "Curriculum vitae",
        "signal": "Signal",
        "description": "Description",
        "when": "Quand",
        "competitor": "Concurrent",
        "basis": "Base",
        "size": "Taille",
        "stage": "Stade",
        "ownership": "Actionnariat",
        "name": "Nom",
        "role": "Rôle",
        "department": "Département",
        "email": "E-mail",
        "validation": "Validation",
        "location": "Lieu",
        "work_arrangement": "Organisation du travail",
        "contract": "Contrat",
        "seniority": "Niveau",
        "posted": "Publiée le",
        "compensation": "Rémunération",
        "employer_rating": "Note employeur",
        "tech_stack": "Technologies",
        "values_culture": "Valeurs et culture",
        "introduction_path": "Voie d'introduction",
        "gross_profit": "Marge brute",
        "sector": "Secteur",
        "domain": "Site web",
        "plausibility": "Plausibilité",
        "why_plausible": "Pourquoi cette ouverture est plausible",
        "revenue_cagr": "TCAC du chiffre d'affaires",
        "headcount_cagr": "TCAC de l'effectif",
        "margin_trend": "Tendance de marge",
        "personnel_cost_per_fte": "Coût du personnel par ETP",
        "current_ratio": "Ratio de liquidité",
        "solvency_ratio": "Ratio de solvabilité",
        "estimated_note": (
            "Certains chiffres sont estimés à partir de dépôts partiels ; à lire comme indicatifs."
        ),
        "watch_out": "Attention",
        "prepare_answer": "Préparez une réponse à cela avant l'entretien.",
        "gap_objection": "Le profil ne démontre pas : {requirement}",
        "no_requirements_stated": (
            "L'ouverture n'énonce aucune exigence ; rien n'a été déduit pour vous."
        ),
        "working_style": "Manière de travailler",
        "briefing_title": "Briefing d'entretien",
        "briefing_subtitle": "Briefing entreprise et poste",
        "company_profile": "Profil de l'entreprise",
        "business": "Activité",
        "products_services": "Produits et services",
        "markets": "Marchés",
        "reference_customers": "Clients de référence",
        "size_and_locations": "Taille et implantations",
        "structure": "Structure et départements",
        "key_people": "Personnes clés",
        "financials": "Analyse financière sur cinq ans",
        "financial_table": "Chiffres publiés",
        "revenue": "Chiffre d'affaires",
        "ebitda": "EBITDA",
        "ebit": "EBIT",
        "net_result": "Résultat net",
        "equity": "Fonds propres",
        "headcount": "Effectif (ETP)",
        "personnel_costs": "Charges de personnel",
        "fiscal_year": "Exercice",
        "trajectory": "Trajectoire",
        "ability_to_pay": "Capacité à payer",
        "investment_capacity": "Capacité d'investissement",
        "revenue_and_result": "Chiffre d'affaires et résultat",
        "headcount_trend": "Évolution de l'effectif",
        "hiring_signals": "Signaux de recrutement et actualités",
        "news": "Actualités récentes",
        "competitors": "Concurrents et position de marché",
        "the_opening": "Le poste",
        "vacancy_full_text": "Offre d'emploi, texte intégral",
        "speculative_opening": "Candidature spontanée - aucune offre n'a été publiée",
        "hiring_contact": "Contact et voie d'introduction",
        "interview_topics": "Sujets et questions probables",
        "questions_to_ask": "Questions à poser",
        "requirements": "Exigences",
        "desirable": "Atouts",
        "motivation_title": "Motivation et adéquation",
        "motivation_subtitle": "Préparation à l'entretien",
        "why_this_job": "Pourquoi je veux ce poste",
        "why_fit_job": "Pourquoi je corresponds au poste",
        "why_fit_company": "Pourquoi je corresponds à l'entreprise",
        "objections": "Objections anticipées et réponses",
        "talking_points": "Points de discussion à répéter",
        "requirement": "Exigence",
        "evidence": "Preuve tirée du profil",
        "objection": "Objection",
        "answer": "Réponse",
        "dream_job_link": "Lien avec l'emploi idéal",
        "career_trajectory": "Trajectoire de carrière",
        "no_evidence": "Aucune preuve dans le profil - ne pas l'affirmer.",
    },
    "de": {
        "page_of": "Seite {page} von {total}",
        "generated_on": "Erstellt am",
        "data_versions": "Datenversionen",
        "profile_version": "Profilversion",
        "company_snapshot": "Unternehmensdaten vom",
        "for_the_job_seeker_only": (
            "Nur für die bewerbende Person - wird nicht an den Arbeitgeber gesendet."
        ),
        "prepared_for": "Erstellt für",
        "not_available": "nicht verfügbar",
        "estimated": "geschätzt",
        "source": "Quelle",
        "cv_profile": "Profil",
        "cv_experience": "Berufserfahrung",
        "cv_education": "Ausbildung",
        "cv_skills": "Kompetenzen",
        "cv_languages": "Sprachen",
        "cv_certifications": "Zertifikate",
        "cv_publications": "Publikationen",
        "cv_projects": "Projekte",
        "cv_honors": "Auszeichnungen",
        "cv_volunteering": "Ehrenamt",
        "cv_courses": "Weiterbildungen",
        "cv_present": "heute",
        "cv_curriculum_vitae": "Lebenslauf",
        "signal": "Signal",
        "description": "Beschreibung",
        "when": "Wann",
        "competitor": "Wettbewerber",
        "basis": "Grundlage",
        "size": "Größe",
        "stage": "Phase",
        "ownership": "Eigentümer",
        "name": "Name",
        "role": "Rolle",
        "department": "Abteilung",
        "email": "E-Mail",
        "validation": "Prüfung",
        "location": "Ort",
        "work_arrangement": "Arbeitsmodell",
        "contract": "Vertrag",
        "seniority": "Senioritätsstufe",
        "posted": "Veröffentlicht",
        "compensation": "Vergütung",
        "employer_rating": "Arbeitgeberbewertung",
        "tech_stack": "Technologie",
        "values_culture": "Werte und Kultur",
        "introduction_path": "Zugangsweg",
        "gross_profit": "Rohertrag",
        "sector": "Branche",
        "domain": "Website",
        "plausibility": "Plausibilität",
        "why_plausible": "Warum diese Öffnung plausibel ist",
        "revenue_cagr": "Umsatz-CAGR",
        "headcount_cagr": "CAGR Mitarbeitende",
        "margin_trend": "Margenentwicklung",
        "personnel_cost_per_fte": "Personalaufwand je VZÄ",
        "current_ratio": "Liquiditätsgrad",
        "solvency_ratio": "Eigenkapitalquote",
        "estimated_note": (
            "Einzelne Zahlen sind aus Teilabschlüssen geschätzt; als Richtwerte lesen."
        ),
        "watch_out": "Achtung",
        "prepare_answer": "Bereiten Sie darauf eine eigene Antwort vor.",
        "gap_objection": "Das Profil belegt nicht: {requirement}",
        "no_requirements_stated": (
            "Die Öffnung nennt keine Anforderungen; es wurde nichts abgeleitet."
        ),
        "working_style": "Arbeitsweise",
        "briefing_title": "Interview-Briefing",
        "briefing_subtitle": "Unternehmens- und Stellenbriefing",
        "company_profile": "Unternehmensprofil",
        "business": "Geschäftstätigkeit",
        "products_services": "Produkte und Dienstleistungen",
        "markets": "Märkte",
        "reference_customers": "Referenzkunden",
        "size_and_locations": "Größe und Standorte",
        "structure": "Struktur und Abteilungen",
        "key_people": "Schlüsselpersonen",
        "financials": "Fünfjahres-Finanzanalyse",
        "financial_table": "Berichtete Kennzahlen",
        "revenue": "Umsatz",
        "ebitda": "EBITDA",
        "ebit": "EBIT",
        "net_result": "Jahresergebnis",
        "equity": "Eigenkapital",
        "headcount": "Mitarbeitende (VZÄ)",
        "personnel_costs": "Personalaufwand",
        "fiscal_year": "Geschäftsjahr",
        "trajectory": "Entwicklung",
        "ability_to_pay": "Zahlungsfähigkeit",
        "investment_capacity": "Investitionsfähigkeit",
        "revenue_and_result": "Umsatz und Ergebnis",
        "headcount_trend": "Entwicklung der Mitarbeitendenzahl",
        "hiring_signals": "Einstellungssignale und aktuelle Nachrichten",
        "news": "Aktuelle Nachrichten",
        "competitors": "Wettbewerber und Marktposition",
        "the_opening": "Die Position",
        "vacancy_full_text": "Stellenanzeige im Volltext",
        "speculative_opening": "Initiativbewerbung - es ist keine Stelle ausgeschrieben",
        "hiring_contact": "Ansprechpartner und Zugangsweg",
        "interview_topics": "Wahrscheinliche Themen und Fragen",
        "questions_to_ask": "Eigene Fragen",
        "requirements": "Anforderungen",
        "desirable": "Wünschenswert",
        "motivation_title": "Motivation und Passung",
        "motivation_subtitle": "Vorbereitung auf das Gespräch",
        "why_this_job": "Warum ich diese Stelle möchte",
        "why_fit_job": "Warum ich zur Stelle passe",
        "why_fit_company": "Warum ich zum Unternehmen passe",
        "objections": "Erwartete Einwände und Antworten",
        "talking_points": "Gesprächspunkte zum Üben",
        "requirement": "Anforderung",
        "evidence": "Beleg aus dem Profil",
        "objection": "Einwand",
        "answer": "Antwort",
        "dream_job_link": "Bezug zum Traumjob",
        "career_trajectory": "Karriereverlauf",
        "no_evidence": "Kein Beleg im Profil - nicht behaupten.",
    },
}


def normalise_language(language: str | None) -> str:
    """Map anything the opportunity carries onto a supported content language."""
    code = (language or "").strip().lower().replace("_", "-")
    if not code:
        return "en"
    base = code.split("-")[0]
    if base in SUPPORTED_LANGUAGES:
        return base
    return "en"


def label(language: str | None, key: str) -> str:
    lang = normalise_language(language)
    return LABELS[lang].get(key) or LABELS["en"].get(key, key)


# ---------------------------------------------------------------------------
# Document metadata (FR-331)
# ---------------------------------------------------------------------------


@dataclass
class DocumentMeta:
    """What every page of a generated PDF has to state (FR-331)."""

    title: str
    subtitle: str = ""
    language: str = "en"
    generated_at: str = ""
    prepared_for: str = ""
    profile_version: str | None = None
    company_snapshot_at: str | None = None
    seeker_only: bool = True
    extra_versions: dict[str, str] = field(default_factory=dict)

    def version_line(self) -> str:
        parts: list[str] = []
        if self.profile_version:
            parts.append(f"{label(self.language, 'profile_version')} {self.profile_version}")
        if self.company_snapshot_at:
            parts.append(
                f"{label(self.language, 'company_snapshot')} {self.company_snapshot_at[:10]}"
            )
        for key, value in self.extra_versions.items():
            parts.append(f"{key} {value}")
        return " · ".join(parts)

    def footer_line(self) -> str:
        stamp = f"{label(self.language, 'generated_on')} {(self.generated_at or '')[:16]}"
        versions = self.version_line()
        return f"{stamp} · {versions}" if versions else stamp


def meta_from_inputs(
    inputs: dict[str, Any],
    *,
    title: str,
    subtitle: str = "",
    language: str = "en",
    generated_at: str = "",
    seeker_only: bool = True,
) -> DocumentMeta:
    """Build the FR-331 header from a generation-inputs mapping.

    The mapping is the one ``db.repositories.applications.generation_inputs``
    returns; only the version-bearing keys are read, so a partial mapping (a
    unit test's, say) produces a document that simply states less.
    """
    version = inputs.get("profile_version") or {}
    seeker = inputs.get("seeker") or {}
    return DocumentMeta(
        title=title,
        subtitle=subtitle,
        language=normalise_language(language),
        generated_at=generated_at,
        prepared_for=str(seeker.get("display_name") or ""),
        profile_version=(str(version["version"]) if version.get("version") is not None else None),
        company_snapshot_at=inputs.get("company_snapshot_at"),
        seeker_only=seeker_only,
    )


# ---------------------------------------------------------------------------
# Palette and styles
# ---------------------------------------------------------------------------

INK = colors.HexColor("#1d2733")
MUTED = colors.HexColor("#5b6b7c")
RULE = colors.HexColor("#c9d4de")
ACCENT = colors.HexColor("#1f5f8b")
BAND = colors.HexColor("#eef3f7")

#: Categorical series colours; readable side by side and in greyscale print.
SERIES_COLOURS = [
    colors.HexColor("#1f5f8b"),
    colors.HexColor("#c2683a"),
    colors.HexColor("#4d7c53"),
    colors.HexColor("#7a5ba6"),
    colors.HexColor("#8a8f3d"),
]


def stylesheet(accent: colors.Color = ACCENT) -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()["Normal"]
    common = {"fontName": "Helvetica", "textColor": INK}
    return {
        "title": ParagraphStyle(
            "dj-title", parent=base, fontName="Helvetica-Bold", fontSize=24, leading=28,
            textColor=accent, spaceAfter=6,
        ),
        "subtitle": ParagraphStyle(
            "dj-subtitle", parent=base, fontName="Helvetica", fontSize=13, leading=17,
            textColor=MUTED, spaceAfter=18,
        ),
        "h1": ParagraphStyle(
            "dj-h1", parent=base, fontName="Helvetica-Bold", fontSize=14, leading=18,
            textColor=accent, spaceBefore=14, spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "dj-h2", parent=base, fontName="Helvetica-Bold", fontSize=11, leading=14,
            textColor=INK, spaceBefore=10, spaceAfter=4,
        ),
        "h3": ParagraphStyle(
            "dj-h3", parent=base, fontName="Helvetica-Oblique", fontSize=10, leading=13,
            textColor=MUTED, spaceBefore=6, spaceAfter=3,
        ),
        "body": ParagraphStyle(
            "dj-body", parent=base, fontSize=9.5, leading=13.5, spaceAfter=5,
            alignment=TA_JUSTIFY, **common,
        ),
        "bullet": ParagraphStyle(
            "dj-bullet", parent=base, fontSize=9.5, leading=13, spaceAfter=2, **common,
        ),
        "small": ParagraphStyle(
            "dj-small", parent=base, fontSize=8.5, leading=11.5, spaceAfter=2,
            fontName="Helvetica", textColor=INK,
        ),
        "note": ParagraphStyle(
            "dj-note", parent=base, fontName="Helvetica-Oblique", fontSize=8, leading=11,
            textColor=MUTED, spaceAfter=6,
        ),
        "cell": ParagraphStyle(
            "dj-cell", parent=base, fontSize=8.5, leading=11, **common,
        ),
        "cell_head": ParagraphStyle(
            "dj-cell-head", parent=base, fontName="Helvetica-Bold", fontSize=8.5, leading=11,
            textColor=colors.white,
        ),
    }


def escape(text: Any) -> str:
    """Paragraph text is model output; the markup characters must not survive."""
    return (
        str(text if text is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class HorizontalRule(Flowable):
    """A thin rule; used to separate CV sections and briefing blocks."""

    def __init__(self, width: float = 0, thickness: float = 0.6, colour: colors.Color = RULE):
        super().__init__()
        self.width = width
        self.thickness = thickness
        self.colour = colour

    def wrap(self, avail_width: float, avail_height: float) -> tuple[float, float]:
        self.width = self.width or avail_width
        return self.width, self.thickness + 4

    def draw(self) -> None:
        self.canv.setStrokeColor(self.colour)
        self.canv.setLineWidth(self.thickness)
        self.canv.line(0, 2, self.width, 2)


# ---------------------------------------------------------------------------
# Canvas: page numbers, generation date and data versions on every page
# ---------------------------------------------------------------------------


class _FooterCanvas(pdfcanvas.Canvas):
    """Two-pass canvas so "page X of Y" knows Y (FR-331)."""

    meta: DocumentMeta = DocumentMeta(title="")

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._pages: list[dict] = []

    def showPage(self) -> None:  # noqa: N802 - reportlab's API
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._pages)
        for index, state in enumerate(self._pages, start=1):
            self.__dict__.update(state)
            self._draw_footer(index, total)
            super().showPage()
        super().save()

    def _draw_footer(self, page: int, total: int) -> None:
        width, _ = self._pagesize
        meta = self.meta
        self.saveState()
        self.setStrokeColor(RULE)
        self.setLineWidth(0.5)
        self.line(18 * mm, 14 * mm, width - 18 * mm, 14 * mm)
        self.setFont("Helvetica", 7)
        self.setFillColor(MUTED)
        self.drawString(18 * mm, 10 * mm, meta.footer_line()[:150])
        self.drawRightString(
            width - 18 * mm,
            10 * mm,
            label(meta.language, "page_of").format(page=page, total=total),
        )
        if meta.seeker_only:
            self.drawCentredString(
                width / 2, 6.5 * mm, label(meta.language, "for_the_job_seeker_only")
            )
        self.restoreState()


def footer_canvas(meta: DocumentMeta) -> type[_FooterCanvas]:
    """The canvas class bound to one document's metadata (FR-331).

    Templates that need their own ``BaseDocTemplate`` - the two-column CV, for
    one - build with this so their pages carry the same footer as everything
    else the generator produces.
    """
    return type("_BoundFooterCanvas", (_FooterCanvas,), {"meta": meta})


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class PdfBuilder:
    """Accumulates a platypus story and writes it with the shared furniture."""

    def __init__(
        self,
        meta: DocumentMeta,
        *,
        accent: colors.Color = ACCENT,
        pagesize: tuple[float, float] = A4,
        margins_mm: float = 18.0,
    ):
        self.meta = meta
        self.accent = accent
        self.pagesize = pagesize
        self.margin = margins_mm * mm
        self.styles = stylesheet(accent)
        self.story: list[Flowable] = []

    # -- text ---------------------------------------------------------------
    def title(self, text: str, subtitle: str = "") -> None:
        self.story.append(Paragraph(escape(text), self.styles["title"]))
        if subtitle:
            self.story.append(Paragraph(escape(subtitle), self.styles["subtitle"]))

    def h1(self, text: str) -> None:
        self.story.append(Paragraph(escape(text), self.styles["h1"]))
        self.story.append(HorizontalRule(colour=self.accent, thickness=0.8))

    def h2(self, text: str) -> None:
        self.story.append(Paragraph(escape(text), self.styles["h2"]))

    def h3(self, text: str) -> None:
        self.story.append(Paragraph(escape(text), self.styles["h3"]))

    def para(self, text: str, style: str = "body") -> None:
        if text is None or str(text).strip() == "":
            return
        for chunk in str(text).split("\n\n"):
            chunk = chunk.strip()
            if chunk:
                self.story.append(
                    Paragraph(escape(chunk).replace("\n", "<br/>"), self.styles[style])
                )

    def note(self, text: str) -> None:
        self.para(text, style="note")

    def bullets(self, items: Sequence[Any], *, style: str = "bullet") -> None:
        rows = [str(i).strip() for i in items if str(i or "").strip()]
        if not rows:
            return
        self.story.append(
            ListFlowable(
                [
                    ListItem(Paragraph(escape(r), self.styles[style]), leftIndent=12)
                    for r in rows
                ],
                bulletType="bullet",
                start="\u2022",
                bulletFontName="Helvetica",
                bulletFontSize=9,
                bulletColor=self.accent,
                bulletOffsetY=-1,
                leftIndent=12,
                spaceAfter=6,
            )
        )

    def spacer(self, height_mm: float = 4.0) -> None:
        self.story.append(Spacer(1, height_mm * mm))

    def page_break(self) -> None:
        self.story.append(PageBreak())

    def keep_together(self, build: Any) -> None:
        """Run ``build(sub_builder)`` and append its flowables as one block."""
        sub = PdfBuilder(self.meta, accent=self.accent, pagesize=self.pagesize)
        build(sub)
        if sub.story:
            self.story.append(KeepTogether(sub.story))

    # -- tables -------------------------------------------------------------
    @property
    def content_width(self) -> float:
        return self.pagesize[0] - 2 * self.margin

    def key_values(self, pairs: Sequence[tuple[str, Any]], *, key_ratio: float = 0.3) -> None:
        rows = [(k, v) for k, v in pairs if str(v or "").strip()]
        if not rows:
            return
        data = [
            [
                Paragraph(f"<b>{escape(k)}</b>", self.styles["cell"]),
                Paragraph(escape(v).replace("\n", "<br/>"), self.styles["cell"]),
            ]
            for k, v in rows
        ]
        width = self.content_width
        table = Table(data, colWidths=[width * key_ratio, width * (1 - key_ratio)], hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.3, RULE),
                ]
            )
        )
        self.story.append(table)
        self.spacer(2)

    def table(
        self,
        headers: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        col_widths: Sequence[float] | None = None,
        align_right_from: int | None = None,
    ) -> None:
        if not rows:
            return
        head = [Paragraph(escape(h), self.styles["cell_head"]) for h in headers]
        body = [
            [Paragraph(escape(c).replace("\n", "<br/>"), self.styles["cell"]) for c in row]
            for row in rows
        ]
        widths = None
        if col_widths:
            total = sum(col_widths)
            widths = [self.content_width * w / total for w in col_widths]
        table = Table([head, *body], colWidths=widths, hAlign="LEFT", repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), self.accent),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BAND]),
            ("GRID", (0, 0), (-1, -1), 0.25, RULE),
        ]
        if align_right_from is not None:
            style.append(("ALIGN", (align_right_from, 1), (-1, -1), "RIGHT"))
        table.setStyle(TableStyle(style))
        self.story.append(table)
        self.spacer(3)

    # -- charts (FR-329) ----------------------------------------------------
    def bar_chart(
        self,
        title: str,
        categories: Sequence[str],
        series: dict[str, Sequence[float | None]],
        *,
        height_mm: float = 62.0,
        value_scale: float = 1.0,
        unit: str = "",
    ) -> bool:
        """Grouped vertical bars.  Returns False when there is nothing to plot."""
        cats, data, names = _clean_series(categories, series)
        if not cats:
            return False
        drawing, chart = _frame(
            VerticalBarChart(), self.content_width, height_mm * mm, title, unit, names
        )
        chart.data = [[(v / value_scale if v is not None else None) for v in row] for row in data]
        _axes(chart, cats)
        chart.barSpacing = 1
        chart.groupSpacing = 8
        for index in range(len(chart.data)):
            chart.bars[index].fillColor = SERIES_COLOURS[index % len(SERIES_COLOURS)]
            chart.bars[index].strokeColor = None
        self.story.append(drawing)
        self.spacer(2)
        return True

    def line_chart(
        self,
        title: str,
        categories: Sequence[str],
        series: dict[str, Sequence[float | None]],
        *,
        height_mm: float = 55.0,
        unit: str = "",
    ) -> bool:
        cats, data, names = _clean_series(categories, series)
        if not cats or len(cats) < 2:
            return False
        drawing, chart = _frame(
            HorizontalLineChart(), self.content_width, height_mm * mm, title, unit, names
        )
        # HorizontalLineChart cannot skip a point; carry the last known value
        # forward rather than drawing a fictitious zero for an unfiled year.
        chart.data = [_carry_forward(row) for row in data]
        _axes(chart, cats)
        chart.lines.strokeWidth = 1.6
        for index in range(len(chart.data)):
            chart.lines[index].strokeColor = SERIES_COLOURS[index % len(SERIES_COLOURS)]
        self.story.append(drawing)
        self.spacer(2)
        return True

    # -- output -------------------------------------------------------------
    def build(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        doc = SimpleDocTemplate(
            str(target),
            pagesize=self.pagesize,
            leftMargin=self.margin,
            rightMargin=self.margin,
            topMargin=self.margin,
            bottomMargin=self.margin + 6 * mm,
            title=self.meta.title,
            author=self.meta.prepared_for or "Dream Job",
            subject=self.meta.subtitle,
        )
        doc.build(list(self.story), canvasmaker=footer_canvas(self.meta))
        return target


# ---------------------------------------------------------------------------
# Cover page (FR-331: date and data versions, stated up front)
# ---------------------------------------------------------------------------


def cover_page(
    builder: PdfBuilder,
    *,
    heading: str,
    subheading: str = "",
    facts: Sequence[tuple[str, Any]] = (),
    footnote: str = "",
) -> None:
    lang = builder.meta.language
    builder.spacer(28)
    builder.title(heading, subheading)
    builder.story.append(HorizontalRule(colour=builder.accent, thickness=1.2))
    builder.spacer(6)
    rows = list(facts)
    if builder.meta.prepared_for:
        rows.append((label(lang, "prepared_for"), builder.meta.prepared_for))
    rows.append((label(lang, "generated_on"), (builder.meta.generated_at or "")[:16]))
    version_line = builder.meta.version_line()
    if version_line:
        rows.append((label(lang, "data_versions"), version_line))
    builder.key_values(rows, key_ratio=0.32)
    if footnote:
        builder.spacer(6)
        builder.note(footnote)
    if builder.meta.seeker_only:
        builder.spacer(4)
        builder.note(label(lang, "for_the_job_seeker_only"))
    builder.page_break()


# ---------------------------------------------------------------------------
# Chart helpers
# ---------------------------------------------------------------------------


TITLE_BAND = 16.0
AXIS_LABEL_BAND = 17.0
LEGEND_BAND = 18.0


def _frame(
    chart: Any, width: float, plot_height: float, title: str, unit: str, names: Sequence[str]
) -> tuple[Drawing, Any]:
    """Lay a chart out with its title on top and its legend under the axis.

    Drawing coordinates start bottom-left, so the bands are stacked from the
    bottom: legend, category labels, plot area, title.
    """
    legend_band = LEGEND_BAND if len(names) > 1 else 0.0
    total = TITLE_BAND + plot_height + AXIS_LABEL_BAND + legend_band
    drawing = Drawing(width, total)

    heading = f"{title} ({unit})" if unit else title
    drawing.add(
        String(0, total - 11, heading, fontName="Helvetica-Bold", fontSize=9, fillColor=INK)
    )
    chart.x = 44
    chart.y = legend_band + AXIS_LABEL_BAND
    chart.width = width - 58
    chart.height = plot_height
    drawing.add(chart)

    if legend_band:
        drawing.add(_legend(names))
    return drawing, chart


def _axes(chart: Any, categories: Sequence[str]) -> None:
    chart.categoryAxis.categoryNames = list(categories)
    chart.categoryAxis.labels.fontName = "Helvetica"
    chart.categoryAxis.labels.fontSize = 7.5
    chart.categoryAxis.strokeColor = RULE
    chart.valueAxis.labels.fontName = "Helvetica"
    chart.valueAxis.labels.fontSize = 7.5
    chart.valueAxis.strokeColor = RULE
    chart.valueAxis.valueMin = min(0.0, _safe_min(chart.data))
    chart.valueAxis.valueMax = _nice_max(chart.data)


def _legend(names: Sequence[str]) -> Legend:
    legend = Legend()
    legend.x = 44
    legend.y = 1
    legend.alignment = "right"
    legend.fontName = "Helvetica"
    legend.fontSize = 7.5
    legend.columnMaximum = 1
    legend.deltax = 96
    legend.dx = 5
    legend.dy = 5
    legend.dxTextSpace = 4
    legend.boxAnchor = "sw"
    legend.colorNamePairs = [
        (SERIES_COLOURS[i % len(SERIES_COLOURS)], n) for i, n in enumerate(names)
    ]
    return legend


def _clean_series(
    categories: Sequence[str], series: dict[str, Sequence[float | None]]
) -> tuple[list[str], list[list[float | None]], list[str]]:
    """Drop empty series and categories with no data at all."""
    names = [n for n, values in series.items() if any(v is not None for v in values)]
    if not names or not categories:
        return [], [], []
    rows = [list(series[n]) for n in names]
    keep = [i for i in range(len(categories)) if any(r[i] is not None for r in rows if i < len(r))]
    if not keep:
        return [], [], []
    cats = [str(categories[i]) for i in keep]
    data = [[(row[i] if i < len(row) else None) for i in keep] for row in rows]
    return cats, data, names


def _carry_forward(row: Sequence[float | None]) -> list[float]:
    out: list[float] = []
    last = 0.0
    for value in row:
        last = float(value) if value is not None else last
        out.append(last)
    return out


def _safe_min(data: Sequence[Sequence[float | None]]) -> float:
    values = [v for row in data for v in row if v is not None]
    return min(values) if values else 0.0


def _nice_max(data: Sequence[Sequence[float | None]]) -> float:
    values = [v for row in data for v in row if v is not None]
    top = max(values) if values else 1.0
    if top <= 0:
        return 1.0
    magnitude = 10 ** (len(str(int(abs(top)))) - 1)
    return float(int(top / magnitude + 1) * magnitude)


def rule_line(width: float, colour: colors.Color = RULE) -> Drawing:
    drawing = Drawing(width, 3)
    drawing.add(Line(0, 1, width, 1, strokeColor=colour, strokeWidth=0.6))
    return drawing


def format_money(value: float | None, currency: str = "EUR", *, language: str = "en") -> str:
    """Compact money for tables: 12.5M, 840k, or the plain figure."""
    if value is None:
        return label(language, "not_available")
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        body = f"{value / 1_000_000_000:,.2f}bn"
    elif absolute >= 1_000_000:
        body = f"{value / 1_000_000:,.2f}M"
    elif absolute >= 1_000:
        body = f"{value / 1_000:,.0f}k"
    else:
        body = f"{value:,.0f}"
    return f"{body} {currency}".strip()


def format_number(value: float | None, *, digits: int = 1, language: str = "en") -> str:
    if value is None:
        return label(language, "not_available")
    return f"{value:,.{digits}f}".rstrip("0").rstrip(".") if digits else f"{value:,.0f}"
