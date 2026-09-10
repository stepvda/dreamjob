"""NBB Central Balance Sheet Office - Belgian annual accounts (FR-241, FR-242, DR-103).

Every Belgian company of any size files its annual accounts with the National
Bank's Central Balance Sheet Office, and the Bank republishes them through the
CBSO Consult API.  For the primary jurisdiction this is *the* source: five
financial years, in one place, filed under the KBO/BCE enterprise number that
:mod:`dreamjob.adapters.registries.kbo` resolved.

The API is used the way it is meant to be, against the key-gated gateway at
``ws.cbso.nbb.be/authentic`` - not the web console's own ``consult.cbso.nbb.be``
back end, which answers 403 to everything and ignores the subscription key:

* ``/legalEntity/{number}/references`` lists the deposits, newest first, with
  the exercise dates that decide which five years are wanted;
* ``/deposit/{reference}/accountingData`` served as ``application/x.jsonxbrl``
  gives the structured form - statutory codes and values, no parsing risk;
* the same route served as ``application/pdf`` is the fall-back for the
  deposits that predate structured filing, and goes through the PDF path in
  :mod:`dreamjob.pipeline.filing_extract` (pdfplumber, statutory codes, then
  LLM assistance for the awkward ones).

The Consult API requires a free subscription key.  Without one the adapter
reports itself unavailable, the pipeline falls back to secondary signals and
the financial profile is marked estimated (FR-245, RK-06) - it does not invent
figures and it does not scrape around the licence.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from dreamjob.adapters.base import AccessMethod, SourceType, register_adapter
from dreamjob.adapters.registries.common import (
    DEFAULT_YEARS,
    RegistryAdapter,
    RegistryResult,
    enterprise_number,
    format_enterprise_number,
)
from dreamjob.pipeline import filing_extract as fx

log = logging.getLogger(__name__)

#: The CBSO gateway that honours ``NBB-CBSO-Subscription-Key``.  Probed live:
#: ``/legalEntity/{n}/references`` and ``/deposit/{r}/accountingData`` answer 401
#: without a key (the route exists), ``/enterprise/...`` and ``/deposit/{r}``
#: answer 404 (they do not).
CONSULT_BASE = "https://ws.cbso.nbb.be/authentic"
JSONXBRL = "application/x.jsonxbrl"


def references_url(number: str) -> str:
    """The deposit list of one legal entity (FR-241)."""
    return f"{CONSULT_BASE}/legalEntity/{number}/references"


def accounting_data_url(reference: str) -> str:
    """One deposit's accounting data; the content type decides JSON-XBRL or PDF."""
    return f"{CONSULT_BASE}/deposit/{reference}/accountingData"


#: Deposit models that carry a full income statement; the abbreviated and
#: micro models omit turnover, which FR-245 handles rather than guesses.
FULL_MODELS = {"f-full", "c-full", "a-full", "full"}


@register_adapter
class NBBAdapter(RegistryAdapter):
    """Belgian annual accounts, five years, structured where the deposit allows."""

    key = "registry.nbb"
    display_name = "NBB Central Balance Sheet Office"
    source_type = SourceType.REGISTRY
    access_method = AccessMethod.API
    coverage_countries = ["BE"]
    jurisdictions = ["BE"]
    secret_name = "NBB_CBSO_SUBSCRIPTION_KEY"
    key_required = True
    reporting_standard = "BE-GAAP"
    default_currency = "EUR"
    rate_limit_rps = 1.0
    legal_notes = (
        "Free subscription key required for the CBSO Consult API; filings are public "
        "documents and are stored raw for re-extraction (DR-102)."
    )

    def _headers(self, accept: str) -> dict[str, str]:
        return {
            "Accept": accept,
            "X-Request-Id": uuid.uuid4().hex,
            "NBB-CBSO-Subscription-Key": self.api_key(),
        }

    # -- registry contract --------------------------------------------------
    async def collect(
        self,
        company: dict,
        *,
        years: int = DEFAULT_YEARS,
        egress: Any = None,
        llm: Any = None,
    ) -> RegistryResult:
        result = RegistryResult(adapter_key=self.key)
        if not self.available():
            result.estimated = True
            result.note(self.unavailable_reason())
            return result

        number = enterprise_number(company)
        if not number:
            result.estimated = True
            result.note("No KBO/BCE enterprise number: the NBB cannot be queried without one")
            return result

        async with self.session(egress) as client:
            deposits = await self.references(number, egress=client)
            if not deposits:
                result.estimated = True
                result.note(
                    f"No deposits found for enterprise {format_enterprise_number(number)}; "
                    "the company may be too young to have filed"
                )
                return result

            wanted = self.select_years(deposits, years)
            for deposit in wanted:
                facts = await self.read_deposit(
                    deposit, egress=client, llm=llm, company_name=company.get("name")
                )
                result.facts.extend(facts)
                if deposit.get("url"):
                    result.documents.append(deposit)

        by_year: dict[int, fx.FilingFacts] = {}
        for record in sorted(result.facts, key=lambda r: (r.fiscal_year, len(r.known_fields()))):
            if record.fiscal_year:
                by_year[record.fiscal_year] = record
        result.facts = [by_year[y] for y in sorted(by_year, reverse=True)[:years]]
        result.facts.sort(key=lambda r: r.fiscal_year)
        if not result.facts:
            result.estimated = True
            result.note("Deposits were listed but no figures could be read from them")
        elif any(r.is_estimated for r in result.facts):
            result.note("At least one year comes from an abbreviated deposit without turnover")
        return result

    # -- API calls ----------------------------------------------------------
    async def references(self, number: str, *, egress: Any) -> list[dict[str, Any]]:
        """The company's deposits, newest exercise first (FR-241)."""
        url = references_url(number)
        try:
            response = await egress.fetch(url, headers=self._headers("application/json"))
        except Exception as exc:  # noqa: BLE001 - a registry outage degrades, never crashes
            log.info("[%s] deposit list unavailable for %s (%s)", self.key, number, exc)
            return []
        if not response.ok:
            log.info("[%s] deposit list returned %s for %s", self.key, response.status_code, number)
            return []
        try:
            payload = json.loads(response.text)
        except ValueError:
            return []
        # The deposit list has no count of its own, so the list itself has to
        # be the evidence: a JSON array is one, and so is a ``References`` key
        # that is still there and still a list.  An object without that key is
        # not an empty deposit list, it is a body we no longer know how to read
        # - and claiming emptiness for it would leave this adapter unable to
        # report its own breakage (FR-181, NFR-403).
        if isinstance(payload, list):
            entries, answered = payload, True
        elif isinstance(payload, dict):
            listed = payload.get("References")
            entries, answered = (listed or []), isinstance(listed, list)
        else:
            return []
        if answered and not entries:
            # The Central Balance Sheet Office answered with a well-formed and
            # empty deposit list: this enterprise has filed nothing.  That is an
            # answer, unlike the outage, the non-2xx and the unparseable body
            # above, which return the same ``[]`` and claim nothing.
            self.record_stated_empty()

        deposits: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            reference = (
                entry.get("ReferenceNumber")
                or entry.get("Reference")
                or entry.get("reference")
                or entry.get("DepositReference")
            )
            dates = entry.get("ExerciseDates") or entry.get("exerciseDates") or {}
            period_end = (
                dates.get("endDate")
                or dates.get("EndDate")
                or entry.get("PeriodEndDate")
                or entry.get("EndDate")
            )
            if not reference:
                continue
            deposits.append(
                {
                    "reference": str(reference),
                    "period_end": str(period_end)[:10] if period_end else None,
                    "fiscal_year": fx._fiscal_year(str(period_end)[:10] if period_end else None),
                    "model": str(entry.get("ModelType") or entry.get("modelType") or "").lower(),
                    "deposit_date": entry.get("DepositDate") or entry.get("depositDate"),
                    "language": entry.get("Language") or entry.get("language"),
                    "url": accounting_data_url(str(reference)),
                }
            )
        deposits.sort(key=lambda d: (d["fiscal_year"] or 0), reverse=True)
        return deposits

    @staticmethod
    def select_years(deposits: list[dict], years: int) -> list[dict]:
        """One deposit per financial year, the most complete model preferred."""
        chosen: dict[int, dict] = {}
        for deposit in deposits:
            year = deposit.get("fiscal_year")
            if not year:
                continue
            current = chosen.get(year)
            if current is None:
                chosen[year] = deposit
                continue
            better_model = deposit["model"] in FULL_MODELS and current["model"] not in FULL_MODELS
            newer = str(deposit.get("deposit_date") or "") > str(current.get("deposit_date") or "")
            if better_model or newer:
                chosen[year] = deposit
        return [chosen[y] for y in sorted(chosen, reverse=True)[:years]]

    async def read_deposit(
        self,
        deposit: dict,
        *,
        egress: Any,
        llm: Any = None,
        company_name: str | None = None,
    ) -> list[fx.FilingFacts]:
        """Structured form first, PDF second (FR-242)."""
        reference = deposit["reference"]
        structured = accounting_data_url(reference)
        try:
            response = await egress.fetch(structured, headers=self._headers(JSONXBRL))
            if response.ok and response.text.strip().startswith(("{", "[")):
                payload = json.loads(response.text)
                facts = fx.extract_from_jsonxbrl(payload)
                if not facts:
                    facts = fx.extract_filing(
                        response.content,
                        JSONXBRL,
                        period_end=deposit.get("period_end"),
                        reporting_standard="BE-GAAP",
                    )
                for record in facts:
                    record.filing_document_id = response.raw_document_id
                    record.period_end = record.period_end or deposit.get("period_end")
                    record.reporting_standard = "BE-GAAP"
                    if not record.fiscal_year and deposit.get("fiscal_year"):
                        record.fiscal_year = int(deposit["fiscal_year"])
                if facts:
                    return [r for r in facts if r.fiscal_year]
        except Exception as exc:  # noqa: BLE001 - fall through to the PDF
            log.info("[%s] structured deposit %s unavailable (%s)", self.key, reference, exc)

        try:
            # The same route, asked for as a PDF: the gateway has no separate
            # document path (``/deposit/{reference}`` answers 404).
            response = await egress.fetch(
                accounting_data_url(reference), headers=self._headers("application/pdf")
            )
        except Exception as exc:  # noqa: BLE001
            log.info("[%s] PDF deposit %s unavailable (%s)", self.key, reference, exc)
            return []
        if not response.ok or not response.content[:5] == b"%PDF-":
            return []
        record = fx.extract_from_pdf(
            response.content,
            fiscal_year=deposit.get("fiscal_year"),
            period_end=deposit.get("period_end"),
            currency="EUR",
            reporting_standard="BE-GAAP",
            llm=llm,
            company_name=company_name,
        )
        record.filing_document_id = response.raw_document_id
        record.source = self.key
        return [record] if record.fiscal_year else []
