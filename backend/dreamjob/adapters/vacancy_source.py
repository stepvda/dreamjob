"""Shared machinery for every adapter that produces vacancy records (FR-261, FR-183).

The ATS and job-board adapters differ only in how they *reach* a posting.  What
they do with it afterwards is identical, so it lives here once:

* **FR-183** - deterministic extraction first.  A schema.org ``JobPosting``
  block is by far the most reliable path and most boards emit one, so
  :func:`jsonld_jobpostings` is tried before any CSS selector; LLM extraction
  (:meth:`VacancySourceAdapter.llm_extract`) is the last resort and is skipped
  when the campaign budget is nearly spent (NFR-104).
* **FR-261** - :func:`build_vacancy` maps whatever was extracted onto the
  ``vacancy`` columns: title, skills, location, work arrangement, contract
  type, compensation, posting date, application channel and source.
* **NFR-403** - :meth:`VacancySourceAdapter.report_extraction_rate` writes the
  observed extraction rate back to the source catalogue, which is how a site
  layout change surfaces to the administrator within one campaign.

Language is detected per posting (nl/fr/en/de) because the generated letter and
CV must later be written in the language of the advertisement (NFR-501).
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from dreamjob.adapters.base import NormalisedRecord, RawRecord, SourceAdapter
from dreamjob.db.connection import upsert_row, utcnow
from dreamjob.llm.client import LLMClient

try:  # pragma: no cover - exercised implicitly; the fallback keeps tests portable
    from selectolax.parser import HTMLParser
except ImportError:  # pragma: no cover
    HTMLParser = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

MAX_DESCRIPTION_CHARS = 20_000


# ---------------------------------------------------------------------------
# HTML -> text
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")


def html_to_text(markup: str | None) -> str:
    """Flatten HTML to readable text, keeping block boundaries as newlines."""
    if not markup:
        return ""
    if "<" not in markup:
        return _tidy(html_lib.unescape(markup))
    if HTMLParser is not None:
        tree = HTMLParser(markup)
        for node in tree.css("script, style, noscript"):
            node.decompose()
        text = tree.body.text(separator="\n") if tree.body else tree.text(separator="\n")
        return _tidy(text)
    stripped = _SCRIPT_RE.sub(" ", markup)
    stripped = re.sub(r"<(br|/p|/div|/li|/h[1-6]|/tr)\s*/?>", "\n", stripped, flags=re.IGNORECASE)
    return _tidy(html_lib.unescape(_TAG_RE.sub(" ", stripped)))


def _tidy(text: str) -> str:
    text = text.replace("\xa0", " ").replace("​", "")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _NL_RE.sub("\n\n", text).strip()


def unescape_entities(markup: str | None) -> str:
    """Greenhouse serves its ``content`` field as HTML-escaped HTML."""
    if not markup:
        return ""
    return html_lib.unescape(markup)


# ---------------------------------------------------------------------------
# FR-183: schema.org JobPosting extraction - the most reliable deterministic path
# ---------------------------------------------------------------------------

_LDJSON_RE = re.compile(
    r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


def _iter_ldjson_nodes(payload: Any) -> list[dict]:
    out: list[dict] = []
    if isinstance(payload, list):
        for item in payload:
            out.extend(_iter_ldjson_nodes(item))
    elif isinstance(payload, dict):
        out.append(payload)
        for key in ("@graph", "itemListElement", "mainEntity"):
            if key in payload:
                out.extend(_iter_ldjson_nodes(payload[key]))
    return out


def extract_jsonld(markup: str) -> list[dict]:
    """Every JSON-LD object embedded in a page, with ``@graph`` flattened."""
    blocks: list[dict] = []
    for match in _LDJSON_RE.finditer(markup or ""):
        body = match.group(1).strip()
        # Some CMSs wrap the block in an HTML comment or emit trailing commas.
        body = body.removeprefix("<!--").removesuffix("-->").strip()
        try:
            payload = json.loads(body)
        except ValueError:
            try:
                payload = json.loads(re.sub(r",\s*([}\]])", r"\1", body))
            except ValueError:
                continue
        blocks.extend(_iter_ldjson_nodes(payload))
    return blocks


def _is_jobposting(node: dict) -> bool:
    types = node.get("@type") or node.get("type") or ""
    if isinstance(types, list):
        return any(str(t).lower() == "jobposting" for t in types)
    return str(types).lower() == "jobposting"


def jsonld_jobpostings(markup: str) -> list[dict]:
    """The ``JobPosting`` blocks of a page (FR-183)."""
    return [node for node in extract_jsonld(markup) if _is_jobposting(node)]


# ---------------------------------------------------------------------------
# Language detection (NFR-501): the generated letter follows the advert
# ---------------------------------------------------------------------------

_STOPWORDS: dict[str, set[str]] = {
    "nl": {
        "de", "het", "een", "en", "van", "voor", "met", "je", "jij", "wij", "onze", "ons",
        "werken", "functie", "ervaring", "jaar", "ook", "niet", "bij", "aan", "om", "te",
        "zoeken", "binnen", "wat", "wie", "als", "naar", "door", "over", "waarbij", "jouw",
    },
    "fr": {
        "le", "la", "les", "des", "une", "un", "et", "vous", "nous", "pour", "avec", "votre",
        "notre", "poste", "experience", "dans", "au", "du", "en", "sur", "est", "sont", "que",
        "qui", "plus", "chez", "ainsi", "afin", "sein",
    },
    "en": {
        "the", "and", "you", "for", "with", "our", "your", "we", "are", "will", "work",
        "experience", "team", "to", "of", "in", "as", "on", "that", "this", "have", "is",
        "role", "about", "who",
    },
    "de": {
        "und", "der", "die", "das", "mit", "fur", "sie", "wir", "ihre", "unsere", "eine",
        "den", "bei", "von", "im", "zu", "ist", "sind", "als", "auf", "dem", "des", "werden",
        "unser", "sowie",
    },
}
_WORD_RE = re.compile(r"[a-zà-öø-ÿ]+", re.IGNORECASE)
_DEACCENT = str.maketrans("àâäéèêëïîôöùûüçÿñáíóúãõ", "aaaeeeeiioouuucynaiouao")


def detect_language(text: str | None, default: str = "en") -> str:
    """Stopword heuristic returning ``nl``/``fr``/``en``/``de`` (NFR-501)."""
    if not text:
        return default
    words = [w.lower().translate(_DEACCENT) for w in _WORD_RE.findall(text[:6000])]
    if len(words) < 15:
        return default
    scores = {
        lang: sum(1 for w in words if w in stops) / len(stops)
        for lang, stops in _STOPWORDS.items()
    }
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else default


def normalise_language(code: str | None, text: str | None = None) -> str:
    """Trust an explicit language tag when the source states one."""
    if code:
        head = str(code).strip().lower().replace("_", "-").split("-")[0]
        if head in _STOPWORDS:
            return head
    return detect_language(text)


# ---------------------------------------------------------------------------
# Field normalisation (FR-261)
# ---------------------------------------------------------------------------

_REMOTE_HINTS = ("remote", "telewerk", "thuiswerk", "teletravail", "télétravail", "homeoffice",
                 "home office", "work from home", "à distance", "a distance", "volledig remote")
_HYBRID_HINTS = ("hybrid", "hybride", "deels van thuis", "mix of office", "flexwerk",
                 "2 dagen thuis", "3 dagen thuis")
_ONSITE_HINTS = ("on-site", "onsite", "on site", "ter plaatse", "sur site", "vor ort",
                 "op kantoor", "au bureau")


def normalise_work_arrangement(*signals: Any) -> str | None:
    """``onsite`` | ``hybrid`` | ``remote`` from any mix of flags and free text."""
    haystack = " ".join(str(s).lower() for s in signals if s not in (None, ""))
    if not haystack:
        return None
    if any(h in haystack for h in _HYBRID_HINTS):
        return "hybrid"
    if any(h in haystack for h in _REMOTE_HINTS):
        return "remote"
    if any(h in haystack for h in _ONSITE_HINTS):
        return "onsite"
    return None


_CONTRACT_MAP = {
    # freelance / self-employed
    "contractor": "freelance", "freelance": "freelance", "self-employed": "freelance",
    "zelfstandig": "freelance", "independant": "freelance", "indépendant": "freelance",
    "selbststandig": "freelance", "selbstständig": "freelance", "b2b": "freelance",
    # agency / temporary work
    "temporary": "interim", "interim": "interim", "intérim": "interim",
    "seasonal": "interim", "uitzend": "interim", "interimaire": "interim",
    "intérimaire": "interim", "zeitarbeit": "interim",
    # fixed-term, including internships and student contracts
    "internship": "fixed_term", "intern": "fixed_term", "stage": "fixed_term",
    "stagiair": "fixed_term", "apprenticeship": "fixed_term", "werkstudent": "fixed_term",
    "working student": "fixed_term", "fixed_term": "fixed_term", "fixed term": "fixed_term",
    "bepaalde duur": "fixed_term", "duree determinee": "fixed_term",
    "durée déterminée": "fixed_term", "cdd": "fixed_term", "befristet": "fixed_term",
    # open-ended employment
    "permanent": "permanent", "full_time": "permanent", "full-time": "permanent",
    "fulltime": "permanent", "part_time": "permanent", "part-time": "permanent",
    "parttime": "permanent", "onbepaalde duur": "permanent", "vast contract": "permanent",
    "cdi": "permanent", "duree indeterminee": "permanent", "durée indéterminée": "permanent",
    "unbefristet": "permanent", "regular": "permanent", "vaste benoeming": "permanent",
}
# Longest key first so "fixed term" beats "term" and "working student" beats "student";
# word boundaries stop "intern" from matching "international" (a real false positive).
_CONTRACT_PATTERNS = [
    (re.compile(rf"(?<![\w-]){re.escape(key)}(?![\w-])", re.IGNORECASE), value)
    for key, value in sorted(_CONTRACT_MAP.items(), key=lambda kv: -len(kv[0]))
]


def normalise_contract_type(*signals: Any) -> str | None:
    """``permanent`` | ``fixed_term`` | ``freelance`` | ``interim`` (FR-261)."""
    haystack = " ".join(str(s).lower() for s in signals if s not in (None, ""))
    if not haystack:
        return None
    for pattern, value in _CONTRACT_PATTERNS:
        if pattern.search(haystack):
            return value
    return None


_PCT_RE = re.compile(r"(\d{2,3})\s*%")


def fte_percentage(*signals: Any) -> int | None:
    haystack = " ".join(str(s).lower() for s in signals if s not in (None, ""))
    match = _PCT_RE.search(haystack)
    if match:
        value = int(match.group(1))
        if 10 <= value <= 100:
            return value
    if "part-time" in haystack or "part_time" in haystack or "deeltijds" in haystack:
        return 50
    if "full-time" in haystack or "full_time" in haystack or "voltijds" in haystack:
        return 100
    return None


def fte_from_hours(hours: float | int | None, week: float = 38.0) -> int | None:
    if not hours:
        return None
    try:
        pct = round(float(hours) / week * 100)
    except (TypeError, ValueError):
        return None
    return max(10, min(100, pct)) or None


_COUNTRY_CODES = {
    "belgium": "BE", "belgië": "BE", "belgie": "BE", "belgique": "BE", "belgien": "BE",
    "netherlands": "NL", "nederland": "NL", "pays-bas": "NL", "the netherlands": "NL",
    "france": "FR", "germany": "DE", "deutschland": "DE", "allemagne": "DE",
    "luxembourg": "LU", "luxemburg": "LU",
    "united kingdom": "GB", "great britain": "GB", "england": "GB", "uk": "GB",
    "united states": "US", "united states of america": "US", "usa": "US",
    "ireland": "IE", "spain": "ES", "espagne": "ES", "italy": "IT", "italia": "IT",
    "portugal": "PT", "poland": "PL", "polska": "PL", "sweden": "SE", "denmark": "DK",
    "norway": "NO", "finland": "FI", "austria": "AT", "switzerland": "CH", "suisse": "CH",
    "czech republic": "CZ", "czechia": "CZ", "romania": "RO", "bulgaria": "BG",
    "greece": "GR", "hungary": "HU", "croatia": "HR", "slovakia": "SK", "slovenia": "SI",
    "estonia": "EE", "latvia": "LV", "lithuania": "LT", "canada": "CA", "india": "IN",
}
_CITY_COUNTRY = {
    "brussels": "BE", "bruxelles": "BE", "brussel": "BE", "antwerp": "BE", "antwerpen": "BE",
    "anvers": "BE", "ghent": "BE", "gent": "BE", "gand": "BE", "leuven": "BE", "louvain": "BE",
    "liege": "BE", "liège": "BE", "luik": "BE", "charleroi": "BE", "bruges": "BE",
    "brugge": "BE", "namur": "BE", "namen": "BE", "hasselt": "BE", "mechelen": "BE",
    "kortrijk": "BE", "aalst": "BE", "mons": "BE", "wavre": "BE", "zaventem": "BE",
    "diegem": "BE", "waregem": "BE", "roeselare": "BE",
    "amsterdam": "NL", "rotterdam": "NL", "utrecht": "NL", "eindhoven": "NL",
    "the hague": "NL", "den haag": "NL", "groningen": "NL", "eersel": "NL",
    "paris": "FR", "lyon": "FR", "lille": "FR", "toulouse": "FR", "nantes": "FR",
    "berlin": "DE", "munich": "DE", "münchen": "DE", "hamburg": "DE", "cologne": "DE",
    "köln": "DE", "frankfurt": "DE", "dresden": "DE", "stuttgart": "DE",
    "london": "GB", "manchester": "GB", "dublin": "IE",
}


def country_from_location(*signals: Any) -> str | None:
    """Best-effort ISO-3166 alpha-2 for a free-text location."""
    for signal in signals:
        if not signal:
            continue
        text = str(signal).strip()
        if len(text) == 2 and text.isalpha():
            return text.upper()
        lowered = text.lower()
        for name, code in _COUNTRY_CODES.items():
            if re.search(rf"\b{re.escape(name)}\b", lowered):
                return code
        for city, code in _CITY_COUNTRY.items():
            if re.search(rf"\b{re.escape(city)}\b", lowered):
                return code
    return None


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")
ATS_HOSTS = (
    "greenhouse.io", "grnh.se", "lever.co", "smartrecruiters.com", "ashbyhq.com",
    "recruitee.com", "personio.de", "personio.com", "myworkdayjobs.com", "workable.com",
    "teamtailor.com", "jobvite.com", "bamboohr.com", "successfactors.com", "taleo.net",
    "softgarden.io", "join.com", "homerun.co", "workday.com", "icims.com",
)


def application_route(apply_url: str | None, description: str | None = None) -> tuple[str, str]:
    """``(application_channel, application_target)`` for FR-261."""
    if apply_url and apply_url.lower().startswith("mailto:"):
        return "email", apply_url[7:].split("?")[0]
    host = urlparse(apply_url).netloc.lower() if apply_url else ""
    if host and any(host == h or host.endswith("." + h) for h in ATS_HOSTS):
        return "ats_form", apply_url or ""
    if apply_url:
        return "url", apply_url
    match = _EMAIL_RE.search(description or "")
    if match:
        return "email", match.group(0)
    return "url", ""


_RELATIVE_RE = re.compile(r"(\d+)\s*\+?\s*(day|days|week|weeks|month|months)", re.IGNORECASE)


def parse_datetime(value: Any) -> str | None:
    """Normalise the many date shapes the sources use onto ``utcnow()`` format."""
    if value in (None, "", "None"):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        number = float(value)
        if number > 1e11:  # Lever hands out epoch milliseconds
            number /= 1000.0
        if number <= 0:
            return None
        return datetime.fromtimestamp(number, UTC).isoformat(timespec="seconds")
    text = str(value).strip()
    if text.endswith(" UTC"):  # Recruitee: "2026-08-31 12:04:10 UTC"
        text = text[:-4].strip().replace(" ", "T") + "+00:00"
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return _parse_relative_date(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _parse_relative_date(text: str) -> str | None:
    """Workday states "Posted 27 Days Ago" rather than a date."""
    lowered = text.lower()
    now = datetime.now(UTC)
    if "today" in lowered or "vandaag" in lowered or "aujourd" in lowered:
        return now.isoformat(timespec="seconds")
    if "yesterday" in lowered or "gisteren" in lowered:
        return (now - timedelta(days=1)).isoformat(timespec="seconds")
    match = _RELATIVE_RE.search(lowered)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).rstrip("s")
    days = {"day": 1, "week": 7, "month": 30}[unit] * amount
    return (now - timedelta(days=days)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Skills (FR-261): deterministic lexicon + requirement-section mining
# ---------------------------------------------------------------------------

SKILL_LEXICON: tuple[str, ...] = (
    "python", "java", "javascript", "typescript", "c#", "c++", "golang", "rust",
    "kotlin", "swift", "scala", "ruby", "php", "matlab", "sql", "pl/sql", "t-sql",
    "bash", "powershell", "vba", "abap", "cobol", "perl", "dart", "elixir",
    "django", "flask", "fastapi", "spring", "spring boot", ".net", "asp.net", "node.js",
    "react", "angular", "vue", "svelte", "next.js", "rails", "laravel", "symfony",
    "postgresql", "mysql", "mariadb", "sqlite", "oracle", "sql server", "mongodb",
    "cassandra", "redis", "elasticsearch", "neo4j", "dynamodb", "snowflake", "databricks",
    "bigquery", "redshift", "clickhouse", "duckdb",
    "aws", "azure", "gcp", "google cloud", "kubernetes", "docker", "terraform", "ansible",
    "jenkins", "gitlab ci", "github actions", "argocd", "helm", "openshift", "linux",
    "windows server", "vmware", "ci/cd", "devops", "sre", "observability", "prometheus",
    "grafana", "datadog", "splunk",
    "spark", "hadoop", "kafka", "airflow", "dbt", "flink", "nifi", "talend", "informatica",
    "ssis", "power bi", "tableau", "looker", "qlik", "sas", "etl", "elt", "data warehouse",
    "data lake", "data mesh", "data governance", "master data management",
    "machine learning", "deep learning", "nlp", "computer vision", "pytorch", "tensorflow",
    "scikit-learn", "keras", "xgboost", "pandas", "numpy", "mlops", "llm", "genai",
    "generative ai", "rag", "langchain", "mlflow", "statistics", "econometrics",
    "sap", "s/4hana", "oracle ebs", "dynamics 365", "salesforce", "servicenow", "workday",
    "netsuite", "odoo", "sharepoint", "jira", "confluence",
    "agile", "scrum", "kanban", "safe", "prince2", "pmp", "itil", "togaf", "archimate",
    "bpmn", "uml", "lean", "six sigma", "change management", "stakeholder management",
    "product management", "roadmapping", "business analysis", "requirements engineering",
    "ifrs", "be-gaap", "us-gaap", "consolidation", "controlling", "fp&a", "treasury",
    "internal audit", "risk management", "compliance", "gdpr", "iso 27001", "nis2",
    "sox", "tax", "vat", "payroll", "procurement", "supply chain", "logistics", "wms",
    "erp", "crm", "pricing", "forecasting", "budgeting", "cost accounting",
    "cybersecurity", "penetration testing", "siem", "soc", "iam", "zero trust",
    "network security", "cissp", "ceh", "firewall", "vpn",
    "communication", "leadership", "coaching", "mentoring", "negotiation", "presenting",
    "dutch", "french", "english", "german", "nederlands", "frans", "engels", "duits",
)

_SKILL_PATTERNS = [
    (skill, re.compile(rf"(?<![\w.+#]){re.escape(skill)}(?![\w+#])", re.IGNORECASE))
    for skill in SKILL_LEXICON
]

_REQUIRED_HEADINGS = (
    "requirements", "qualifications", "what you bring", "what we expect", "your profile",
    "who you are", "must have", "profiel", "jouw profiel", "wat wij zoeken", "wat we zoeken",
    "wie ben jij", "vereisten", "profil", "votre profil", "ce que nous recherchons",
    "compétences", "competences", "dein profil", "ihr profil", "anforderungen",
)
_DESIRABLE_HEADINGS = (
    "nice to have", "nice-to-have", "bonus", "preferred", "plus", "pluspunt", "pluspunten",
    "troeven", "atout", "atouts", "un plus", "wünschenswert", "von vorteil", "extra",
)


def _section(text: str, headings: tuple[str, ...]) -> str:
    """Text following the first matching heading, up to the next blank-line heading."""
    lowered = text.lower()
    for heading in headings:
        index = lowered.find(heading)
        if index < 0:
            continue
        tail = text[index + len(heading) : index + len(heading) + 2500]
        return tail
    return ""


def extract_skills(text: str | None, headings: tuple[str, ...] | None = None) -> list[str]:
    """Lexicon hits, restricted to a requirements section when one is identifiable."""
    if not text:
        return []
    scope = _section(text, headings) if headings else ""
    haystack = scope or text
    found: list[str] = []
    for skill, pattern in _SKILL_PATTERNS:
        if pattern.search(haystack):
            found.append(skill)
    return found[:40]


def split_skills(text: str | None) -> tuple[list[str], list[str]]:
    """``(required_skills, desirable_skills)`` for the FR-261 opportunity record."""
    required = extract_skills(text, _REQUIRED_HEADINGS)
    desirable = [s for s in extract_skills(text, _DESIRABLE_HEADINGS) if s not in required]
    if not required:
        required = extract_skills(text)
        desirable = [s for s in desirable if s not in required]
    return required, desirable


_SALARY_RE = re.compile(
    r"(?:€|eur|\$|usd|£|gbp)\s?(\d[\d.,]{2,})"
    r"\s*(?:-|–|to|tot|a|à)\s*"
    r"(?:€|eur|\$|£)?\s?(\d[\d.,]{2,})",
    re.IGNORECASE,
)


def parse_salary_text(text: str | None) -> tuple[float | None, float | None, str | None]:
    """Posted ranges only - never an estimate (FR-261 says "if stated")."""
    if not text:
        return None, None, None
    match = _SALARY_RE.search(text)
    if not match:
        return None, None, None
    currency = "EUR"
    token = match.group(0).lower()
    if "$" in token or "usd" in token:
        currency = "USD"
    elif "£" in token or "gbp" in token:
        currency = "GBP"
    try:
        low = float(match.group(1).replace(".", "").replace(",", "."))
        high = float(match.group(2).replace(".", "").replace(",", "."))
    except ValueError:
        return None, None, None
    return (low, high, currency) if low <= high else (high, low, currency)


_NON_WORD = re.compile(r"[^a-z0-9]+")


def vacancy_dedup_key(
    title: str | None, company: str | None, location: str | None, posted_at: str | None
) -> str:
    """Fuzzy key: title + company + location + posting month (FR-184)."""
    def slug(value: str | None) -> str:
        return _NON_WORD.sub("-", (value or "").lower().translate(_DEACCENT)).strip("-")

    month = (posted_at or "")[:7]
    return "|".join((slug(title), slug(company), slug(location), month))


# ---------------------------------------------------------------------------
# schema.org JobPosting -> vacancy columns
# ---------------------------------------------------------------------------

def _first(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _text_of(value: Any) -> str:
    node = _first(value)
    if node is None:
        return ""
    if isinstance(node, dict):
        for key in ("name", "value", "text", "description", "@value"):
            if node.get(key):
                return str(node[key])
        return ""
    return str(node)


def jobposting_to_fields(posting: dict, source_url: str = "") -> dict[str, Any]:
    """Map a schema.org ``JobPosting`` onto ``vacancy`` columns (FR-183, FR-261)."""
    description_html = str(posting.get("description") or "")
    description = html_to_text(description_html)

    place = _first(posting.get("jobLocation")) or {}
    address = place.get("address") if isinstance(place, dict) else {}
    address = address if isinstance(address, dict) else {}
    locality = _text_of(address.get("addressLocality"))
    region = _text_of(address.get("addressRegion"))
    country_raw = _text_of(address.get("addressCountry"))
    location = ", ".join(p for p in (locality, region) if p) or _text_of(place.get("name"))

    geo = place.get("geo") if isinstance(place, dict) else None
    geo = geo if isinstance(geo, dict) else {}

    salary = posting.get("baseSalary") or {}
    salary = salary if isinstance(salary, dict) else {}
    salary_value = salary.get("value") if isinstance(salary.get("value"), dict) else {}
    salary_min = salary_value.get("minValue") or salary_value.get("value")
    salary_max = salary_value.get("maxValue") or salary_value.get("value")
    currency = salary.get("currency") or salary.get("salaryCurrency")
    if salary_min is None and salary_max is None:
        salary_min, salary_max, currency = parse_salary_text(description[:4000])

    employment = posting.get("employmentType")
    employment_text = (
        ", ".join(str(e) for e in employment)
        if isinstance(employment, list)
        else str(employment or "")
    )

    org = posting.get("hiringOrganization") or {}
    org = org if isinstance(org, dict) else {"name": str(org)}

    apply_url = (
        str(posting.get("url") or "")
        or str(_text_of(posting.get("applicationContact")) or "")
        or source_url
    )
    channel, target = application_route(apply_url, description)

    skills_field = posting.get("skills") or posting.get("qualifications")
    required, desirable = split_skills(description)
    if isinstance(skills_field, str) and skills_field.strip():
        stated = [s.strip() for s in re.split(r"[,;\n•]+", html_to_text(skills_field)) if s.strip()]
        required = ([s for s in stated if len(s) < 60][:40]) or required

    language = normalise_language(posting.get("inLanguage"), description)
    remote_flag = str(posting.get("jobLocationType") or "")

    return {
        "title": str(posting.get("title") or "").strip(),
        "company_name_raw": str(org.get("name") or "").strip() or None,
        "description": description[:MAX_DESCRIPTION_CHARS] or None,
        "required_skills": required,
        "desirable_skills": desirable,
        "location": location or None,
        "country": country_from_location(country_raw, location),
        "latitude": _as_float(geo.get("latitude")),
        "longitude": _as_float(geo.get("longitude")),
        "work_arrangement": normalise_work_arrangement(remote_flag, location, description[:2000]),
        "contract_type": normalise_contract_type(employment_text, description[:2000]),
        "fte_percentage": fte_percentage(employment_text, description[:1500]),
        "salary_min": _as_float(salary_min),
        "salary_max": _as_float(salary_max),
        "salary_currency": currency or None,
        "posted_at": parse_datetime(posting.get("datePosted")),
        "application_channel": channel,
        "application_target": target or None,
        "source_url": source_url or apply_url or None,
        "language": language,
    }


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


VACANCY_COLUMNS = {
    "company_id", "company_name_raw", "title", "function_family", "seniority", "description",
    "required_skills", "desirable_skills", "location", "country", "latitude", "longitude",
    "work_arrangement", "remote_days", "contract_type", "fte_percentage", "salary_min",
    "salary_max", "salary_currency", "posted_at", "application_channel", "application_target",
    "source_url", "source_adapter", "raw_document_id", "dedup_key", "language",
    "collected_at", "access_method", "confidence",
}


def build_vacancy(
    fields: dict[str, Any], *, adapter_key: str, access_method: str, confidence: float = 0.8
) -> dict[str, Any] | None:
    """Finalise a parsed posting into a ``vacancy`` row (FR-261, FR-184)."""
    title = (fields.get("title") or "").strip()
    if not title:
        return None
    row = {k: v for k, v in fields.items() if k in VACANCY_COLUMNS and v not in (None, "", [])}
    row["title"] = title[:300]
    row["source_adapter"] = adapter_key
    row["access_method"] = access_method
    row["collected_at"] = utcnow()
    row.setdefault("confidence", confidence)
    row.setdefault("language", detect_language(fields.get("description")))
    row["dedup_key"] = vacancy_dedup_key(
        title, fields.get("company_name_raw"), fields.get("location"), fields.get("posted_at")
    )
    return row


# ---------------------------------------------------------------------------
# Adapter base
# ---------------------------------------------------------------------------

_LLM_SYSTEM = (
    "You extract a single job advertisement into JSON. Use only what the page states. "
    "Leave a field null when the page does not state it - never guess a salary, a date "
    "or a location. Reply with JSON only."
)
_LLM_SCHEMA = (
    '{"title": str, "company_name_raw": str|null, "description": str, '
    '"required_skills": [str], "desirable_skills": [str], "location": str|null, '
    '"work_arrangement": "onsite|hybrid|remote|null", '
    '"contract_type": "permanent|fixed_term|freelance|interim|null", '
    '"fte_percentage": int|null, "salary_min": number|null, "salary_max": number|null, '
    '"salary_currency": str|null, "posted_at": "YYYY-MM-DD|null", '
    '"application_target": str|null, "language": "nl|fr|en|de"}'
)


class VacancySourceAdapter(SourceAdapter):
    """Base for ATS and job-board adapters: shared normalisation and LLM fallback."""

    #: FR-183 - set False on sources whose pages are always structured.
    llm_fallback: bool = True
    #: Confidence attached to deterministically extracted records (NFR-402).
    base_confidence: float = 0.85

    def __init__(
        self,
        egress: Any = None,
        *,
        llm: LLMClient | None = None,
        campaign_id: str | None = None,
        job_seeker_id: str | None = None,
    ):
        super().__init__(egress)
        self.llm = llm
        self.campaign_id = campaign_id
        self.job_seeker_id = job_seeker_id
        self.llm_extractions = 0

    # -- normalise (FR-261) -------------------------------------------------
    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        row = build_vacancy(
            parsed,
            adapter_key=self.key,
            access_method=self.access_method.value,
            confidence=parsed.get("confidence", self.base_confidence),
        )
        if row is None:
            return None
        row.setdefault("source_url", raw.url)
        if raw.raw_document_id:
            row["raw_document_id"] = raw.raw_document_id
        return NormalisedRecord(
            entity_type="vacancy",
            data=row,
            confidence=float(row.get("confidence", self.base_confidence)),
            provenance={
                "adapter_key": self.key,
                "source_url": row.get("source_url"),
                "access_method": self.access_method.value,
            },
            raw_document_id=raw.raw_document_id,
        )

    # -- FR-183: LLM extraction for pages that resist deterministic parsing --
    def _llm_client(self) -> LLMClient | None:
        if self.llm is not None:
            return self.llm
        try:
            self.llm = LLMClient(campaign_id=self.campaign_id, job_seeker_id=self.job_seeker_id)
        except Exception:  # noqa: BLE001 - extraction must survive a missing key
            log.debug("[%s] no LLM client available for extraction fallback", self.key)
            return None
        return self.llm

    def llm_extract(self, page_text: str, url: str) -> dict[str, Any] | None:
        """Last-resort extraction (FR-183).  Returns None rather than raising."""
        if not self.llm_fallback or not page_text.strip():
            return None
        client = self._llm_client()
        if client is None:
            return None
        try:
            if client.budget.should_degrade():  # NFR-104
                log.info("[%s] skipping LLM extraction: campaign budget nearly spent", self.key)
                return None
            data = client.complete_json(
                "extract.vacancy",
                system=_LLM_SYSTEM,
                user="Extract the job advertisement from the page below.",
                # NFR-205: the page *and* its URL are scraped material, so both are
                # fenced as data - neither is concatenated into the instruction.
                untrusted={"page": page_text[:30_000], "source_url": url},
                schema_hint=_LLM_SCHEMA,
                entity_type="vacancy",
                max_tokens=1800,
            )
        except Exception as exc:  # noqa: BLE001 - never break collection
            log.warning("[%s] LLM extraction failed for %s: %s", self.key, url, exc)
            return None
        if not isinstance(data, dict) or not data.get("title"):
            return None
        self.llm_extractions += 1
        data["confidence"] = 0.6  # NFR-402: extracted, not stated
        data.setdefault("source_url", url)
        data["posted_at"] = parse_datetime(data.get("posted_at"))
        channel, target = application_route(data.get("application_target"), data.get("description"))
        data["application_channel"] = channel
        data["application_target"] = target or None
        return data

    # -- NFR-403: extraction-rate monitoring --------------------------------
    def report_extraction_rate(self) -> float | None:
        """Persist the observed extraction rate so breakage surfaces to the admin."""
        rate = self.extraction_rate
        if rate is None:
            return None
        self.register()
        values: dict[str, Any] = {
            "adapter_key": self.key,
            "display_name": self.display_name or self.key,
            "source_type": self.source_type.value,
            "access_method": self.access_method.value,
            "extraction_success_rate": rate,
            "updated_at": utcnow(),
        }
        if self._extraction_successes:
            values["last_success_at"] = utcnow()
        upsert_row("source_catalogue", values, ["adapter_key"])
        if rate < 0.5:
            log.warning(
                "[%s] extraction rate %.0f%% over %d attempts - adapter may be broken (NFR-403)",
                self.key, rate * 100, self._extraction_attempts,
            )
        return rate


# ---------------------------------------------------------------------------
# Planning hints (FR-162): directives -> native query terms
# ---------------------------------------------------------------------------

def keywords_from(
    directives: dict, composite_profile: dict | None = None, limit: int = 6
) -> list[str]:
    """Search terms for a keyword-capable source, in priority order (FR-162)."""
    out: list[str] = []
    job_content = _as_dict(directives.get("job_content"))
    for key in ("target_titles", "titles", "keywords", "role_families", "functions"):
        out.extend(_as_str_list(job_content.get(key)))
    out.extend(_as_str_list(directives.get("keywords")))
    if not out and composite_profile:
        out.extend(_as_str_list(composite_profile.get("core_competencies"))[:3])
        for role in _as_str_list(composite_profile.get("target_roles"))[:3]:
            out.append(role)
    seen: set[str] = set()
    unique = []
    for term in out:
        term = term.strip()
        if term and term.lower() not in seen:
            seen.add(term.lower())
            unique.append(term)
    return unique[:limit]


def locations_from(directives: dict, limit: int = 5) -> list[str]:
    location = _as_dict(directives.get("location"))
    out: list[str] = []
    for key in ("cities", "regions", "locations", "places"):
        out.extend(_as_str_list(location.get(key)))
    return out[:limit]


def countries_from(directives: dict) -> list[str]:
    location = _as_dict(directives.get("location"))
    codes = [c.upper() for c in _as_str_list(location.get("countries")) if len(c) == 2]
    if codes:
        return codes
    derived = [country_from_location(c) for c in _as_str_list(location.get("countries"))]
    return [c for c in derived if c] or ["BE"]


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _as_str_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        if value.strip().startswith("["):
            try:
                value = json.loads(value)
            except ValueError:
                return [value]
        else:
            return [value]
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                label = item.get("label") or item.get("name") or item.get("title")
                if label:
                    out.append(str(label))
        return out
    return []


def requested_page(native_query: dict | None) -> int | None:
    """The page the collection pipeline is asking for, if any.

    ``pipeline.collection`` drives pagination itself: it re-issues the plan item
    once per page with ``native_query["page"]`` set, so that a crash loses at
    most one page (NFR-401).  An adapter that also paginates internally must
    therefore fetch *only* that page instead of walking the whole source again.
    """
    value = (native_query or {}).get("page")
    try:
        page = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return page if page > 0 else None


def title_matches(title: str, keywords: list[str]) -> bool:
    """Client-side title filter for sources that list a whole board (FR-162)."""
    if not keywords:
        return True
    lowered = (title or "").lower()
    return any(k.lower() in lowered for k in keywords if k)


__all__ = [
    "ATS_HOSTS",
    "MAX_DESCRIPTION_CHARS",
    "SKILL_LEXICON",
    "VacancySourceAdapter",
    "application_route",
    "build_vacancy",
    "countries_from",
    "country_from_location",
    "detect_language",
    "extract_jsonld",
    "extract_skills",
    "fte_from_hours",
    "fte_percentage",
    "html_to_text",
    "jobposting_to_fields",
    "jsonld_jobpostings",
    "keywords_from",
    "locations_from",
    "normalise_contract_type",
    "normalise_language",
    "normalise_work_arrangement",
    "parse_datetime",
    "parse_salary_text",
    "requested_page",
    "split_skills",
    "title_matches",
    "unescape_entities",
    "vacancy_dedup_key",
]
