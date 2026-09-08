"""E-mail validation before use (FR-304, FR-305, CR-402, RK-08).

FR-304 lists five checks and four verdicts.  The checks run cheapest-first and
stop as soon as the verdict is settled, because every step after the first
costs somebody else's resources:

1. **syntax** - local, free, and enough to reject most inferred guesses.
2. **MX lookup** - one DNS query per domain, cached in ``email_domain_state``.
   A domain with neither MX nor a mail-capable A record cannot receive mail, so
   the address is ``invalid``.
3. **disposable / role detection** - bundled lists, no network at all.  A
   disposable domain is ``invalid``; a role mailbox is *not* - ``careers@`` is
   the FR-301 fallback and is often the only lawful route into a company - it
   is flagged and downgraded to ``risky``.
4. **SMTP mailbox verification** - a RCPT probe, never a DATA phase, only where
   the receiving server answers.  Many providers accept every recipient at
   RCPT time, greylist the first attempt, or blackhole verification entirely;
   all three are *inconclusive*, and the rule this module is built around is
   that inconclusive is ``unknown`` and never ``valid``.
5. **catch-all detection** - a probe of a random local part on the same domain.
   If that is accepted too, the server's "yes" carries no information and every
   address on the domain is at best ``risky``.

FR-305 governs the cost of steps 4 and 5: verdicts are cached per address in
``email_validation_cache`` for a configurable period, and probes are limited per
domain by a minimum interval and a daily budget held in ``email_domain_state``,
so a mail server is never asked the same question twice in a row.

The verdicts are ordered ``valid > risky > unknown > invalid``; FR-304 forbids
using an ``invalid`` address and the ``usable_contact`` view enforces it.
"""

from __future__ import annotations

import logging
import random
import re
import secrets
import smtplib
import socket
import string
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from dreamjob.config import get_settings
from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import contacts as repo
from dreamjob.db.repositories import knowledge as kb

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent / "data"
DISPOSABLE_FILE = DATA_DIR / "disposable_email_domains.txt"
ROLE_FILE = DATA_DIR / "role_email_localparts.txt"

VALID = "valid"
RISKY = "risky"
INVALID = "invalid"
UNKNOWN = "unknown"

#: Best-first.  Used to pick between two verdicts for the same contact.
VERDICT_RANK = {VALID: 3, RISKY: 2, UNKNOWN: 1, INVALID: 0}

# --- FR-305 knobs, all administrator-settable ------------------------------
SETTING_CACHE_DAYS = "contacts.validation_cache_days"
SETTING_NEGATIVE_CACHE_DAYS = "contacts.validation_negative_cache_days"
SETTING_SMTP_ENABLED = "contacts.smtp_probe_enabled"
SETTING_SMTP_MIN_INTERVAL = "contacts.smtp_min_interval_seconds"
SETTING_SMTP_DAILY_BUDGET = "contacts.smtp_probes_per_domain_per_day"
SETTING_SMTP_TIMEOUT = "contacts.smtp_timeout_seconds"
SETTING_CATCH_ALL_DAYS = "contacts.catch_all_recheck_days"

DEFAULT_CACHE_DAYS = 30
DEFAULT_NEGATIVE_CACHE_DAYS = 7
DEFAULT_SMTP_MIN_INTERVAL = 60
DEFAULT_SMTP_DAILY_BUDGET = 20
DEFAULT_SMTP_TIMEOUT = 10
DEFAULT_CATCH_ALL_DAYS = 30

#: RFC 5322 in the shape that matters here: no quoted local parts, no comments.
SYNTAX_RE = re.compile(
    r"\A[A-Za-z0-9!#$%&'*+/=?^_`{|}~\-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~\-]+)*"
    r"@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,24}\Z"
)


class _Settings:
    """Reads the FR-305 knobs out of ``app_setting`` with sane defaults."""

    @staticmethod
    def _int(key: str, default: int) -> int:
        try:
            return max(0, int(kb.get_setting(key, default)))
        except (TypeError, ValueError):
            return default

    @property
    def cache_days(self) -> int:
        return self._int(SETTING_CACHE_DAYS, DEFAULT_CACHE_DAYS)

    @property
    def negative_cache_days(self) -> int:
        return self._int(SETTING_NEGATIVE_CACHE_DAYS, DEFAULT_NEGATIVE_CACHE_DAYS)

    @property
    def smtp_enabled(self) -> bool:
        value = kb.get_setting(SETTING_SMTP_ENABLED, True)
        return value not in (False, 0, "0", "false", "False", "no")

    @property
    def min_interval(self) -> int:
        return self._int(SETTING_SMTP_MIN_INTERVAL, DEFAULT_SMTP_MIN_INTERVAL)

    @property
    def daily_budget(self) -> int:
        return self._int(SETTING_SMTP_DAILY_BUDGET, DEFAULT_SMTP_DAILY_BUDGET)

    @property
    def timeout(self) -> int:
        return self._int(SETTING_SMTP_TIMEOUT, DEFAULT_SMTP_TIMEOUT)

    @property
    def catch_all_days(self) -> int:
        return self._int(SETTING_CATCH_ALL_DAYS, DEFAULT_CATCH_ALL_DAYS)


limits = _Settings()


# ---------------------------------------------------------------------------
# Bundled lists (FR-304)
# ---------------------------------------------------------------------------


def _read_list(path: Path) -> frozenset[str]:
    if not path.exists():  # pragma: no cover - the lists ship with the package
        log.warning("Bundled list %s is missing; that check is skipped", path.name)
        return frozenset()
    entries = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip().lower()
        if value and not value.startswith("#"):
            entries.add(value)
    return frozenset(entries)


@lru_cache(maxsize=1)
def disposable_domains() -> frozenset[str]:
    return _read_list(DISPOSABLE_FILE)


@lru_cache(maxsize=1)
def role_local_parts() -> frozenset[str]:
    return _read_list(ROLE_FILE)


def is_disposable(domain: str) -> bool:
    """A throwaway provider, including its subdomains (mail.tempmail.eu)."""
    domain = (domain or "").lower().strip(".")
    known = disposable_domains()
    if domain in known:
        return True
    parts = domain.split(".")
    return any(".".join(parts[i:]) in known for i in range(1, len(parts) - 1))


def is_role_address(email: str) -> bool:
    local = (email or "").rsplit("@", 1)[0].lower()
    if local in role_local_parts():
        return True
    # "jobs-belgium@", "hr.benelux@" - the function is still the recipient.
    head = re.split(r"[.\-+_]", local)[0]
    return head in role_local_parts()


# ---------------------------------------------------------------------------
# DNS (FR-304)
# ---------------------------------------------------------------------------


@dataclass
class MXResult:
    has_mx: bool
    hosts: list[str] = field(default_factory=list)
    error: str | None = None
    from_cache: bool = False


def _resolve_mx(domain: str, timeout: float = 5.0) -> MXResult:
    """One MX lookup, falling back to A/AAAA as RFC 5321 section 5.1 allows."""
    try:
        import dns.resolver  # noqa: PLC0415 - optional at import time
    except ImportError:  # pragma: no cover - dnspython is a declared dependency
        return MXResult(has_mx=False, error="dnspython is not installed")

    resolver = dns.resolver.Resolver()
    resolver.timeout = timeout
    resolver.lifetime = timeout
    try:
        answers = resolver.resolve(domain, "MX")
        hosts = [
            str(r.exchange).rstrip(".")
            for r in sorted(answers, key=lambda r: int(r.preference))
            if str(r.exchange).rstrip(".") not in ("", ".")
        ]
        if hosts:
            return MXResult(has_mx=True, hosts=hosts)
        return MXResult(has_mx=False, error="null MX")
    except Exception as exc:  # noqa: BLE001 - every DNS failure mode is one answer
        mx_error = type(exc).__name__
    try:
        resolver.resolve(domain, "A")
        return MXResult(has_mx=True, hosts=[domain], error=f"no MX ({mx_error}); A record used")
    except Exception as exc:  # noqa: BLE001
        return MXResult(has_mx=False, error=f"{mx_error}/{type(exc).__name__}")


def mx_for(domain: str, *, max_age_days: int = 7, force: bool = False) -> MXResult:
    """MX hosts for a domain, cached in ``email_domain_state`` (FR-305)."""
    domain = (domain or "").lower()
    if not domain:
        return MXResult(has_mx=False, error="no domain")

    state = repo.domain_state(domain)
    if state and not force and state.get("mx_checked_at"):
        if _age_days(state["mx_checked_at"]) < max_age_days:
            return MXResult(
                has_mx=bool(state.get("has_mx")),
                hosts=list(state.get("mx_hosts") or []),
                from_cache=True,
            )

    result = _resolve_mx(domain)
    repo.save_domain_state(
        domain,
        {
            "has_mx": 1 if result.has_mx else 0,
            "mx_hosts": result.hosts,
            "mx_checked_at": utcnow(),
            "is_disposable": 1 if is_disposable(domain) else 0,
        },
    )
    return result


def _age_days(timestamp: str | None) -> float:
    if not timestamp:
        return 10**6
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return 10**6
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - moment).total_seconds() / 86400


def _seconds_since(timestamp: str | None) -> float:
    return _age_days(timestamp) * 86400


# ---------------------------------------------------------------------------
# SMTP probing, rate-limited per domain (FR-304, FR-305, CR-402)
# ---------------------------------------------------------------------------

ACCEPTED = "accepted"
REJECTED = "rejected"
INCONCLUSIVE = "inconclusive"


@dataclass
class ProbeResult:
    """One RCPT probe.  ``INCONCLUSIVE`` is the honest answer most of the time."""

    outcome: str
    code: int | None = None
    message: str = ""
    host: str | None = None


def _probe_budget_left(domain: str, *, respect_interval: bool = True) -> tuple[bool, str]:
    """FR-305: minimum interval between probes and a daily budget per domain.

    ``respect_interval=False`` is for the second half of one verification: the
    catch-all probe follows the recipient probe in the same conversation with
    the same server, and holding it back for the full interval would leave the
    catch-all question permanently unanswered - and with it, FR-304's ``valid``
    verdict permanently unreachable.  The daily budget still applies.
    """
    state = repo.domain_state(domain) or {}
    if respect_interval and state.get("last_probe_at"):
        elapsed = _seconds_since(state["last_probe_at"])
        if elapsed < limits.min_interval:
            return False, f"rate limited: {int(limits.min_interval - elapsed)}s to wait"
    window = state.get("probe_window_start")
    count = int(state.get("probe_count") or 0)
    if window and _age_days(window) < 1 and count >= limits.daily_budget:
        return False, f"daily probe budget for {domain} exhausted ({count})"
    return True, ""


def _record_probe(domain: str) -> None:
    state = repo.domain_state(domain) or {}
    window = state.get("probe_window_start")
    count = int(state.get("probe_count") or 0)
    if not window or _age_days(window) >= 1:
        window, count = utcnow(), 0
    repo.save_domain_state(
        domain,
        {"probe_window_start": window, "probe_count": count + 1, "last_probe_at": utcnow()},
    )


def _mail_from() -> str:
    """The envelope sender used in a probe.

    A real, monitored address belonging to the operator: a probe from a
    non-existent sender is itself a small abuse, and several servers reject it.
    """
    settings = get_settings()
    return settings.mail_from or f"postmaster@{socket.getfqdn()}"


def smtp_probe(
    email: str, hosts: list[str], *, timeout: int | None = None, helo: str | None = None
) -> ProbeResult:
    """Ask the receiving server whether it would accept this recipient.

    ``EHLO`` / ``MAIL FROM`` / ``RCPT TO`` / ``QUIT`` - the session is closed
    before DATA, so no message is ever offered.  4xx, connection failures and
    timeouts are inconclusive, not rejections.
    """
    timeout = timeout or limits.timeout
    sender = _mail_from()
    domain = helo or (sender.rsplit("@", 1)[-1] if "@" in sender else socket.getfqdn())
    last: ProbeResult = ProbeResult(INCONCLUSIVE, message="no MX host answered")

    for host in hosts[:2]:
        server: smtplib.SMTP | None = None
        try:
            server = smtplib.SMTP(timeout=timeout, local_hostname=domain)
            server.connect(host, 25)
            server.ehlo_or_helo_if_needed()
            server.mail(sender)
            code, raw = server.rcpt(email)
            message = raw.decode("utf-8", "replace")[:200] if isinstance(raw, bytes) else str(raw)
            if code in (250, 251):
                return ProbeResult(ACCEPTED, code, message, host)
            if 500 <= code < 600:
                return ProbeResult(REJECTED, code, message, host)
            last = ProbeResult(INCONCLUSIVE, code, message, host)
        except (smtplib.SMTPException, OSError) as exc:
            # Blocked port 25, greylisting, TLS-only servers: all inconclusive.
            last = ProbeResult(INCONCLUSIVE, None, f"{type(exc).__name__}: {exc}"[:200], host)
        finally:
            if server is not None:
                try:
                    server.quit()
                except (smtplib.SMTPException, OSError):
                    server.close()
    return last


def _random_local_part() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "dj-" + "".join(secrets.choice(alphabet) for _ in range(12))


def detect_catch_all(
    domain: str, hosts: list[str], *, force: bool = False, respect_interval: bool = True
) -> tuple[bool | None, str]:
    """Probe a random local part; acceptance means the domain accepts anything.

    The verdict is cached per domain (FR-305) because it is a property of the
    server, not of the address, and re-probing it for every contact of the same
    employer is exactly the behaviour FR-305 forbids.
    """
    state = repo.domain_state(domain) or {}
    if (
        not force
        and state.get("catch_all") is not None
        and _age_days(state.get("catch_all_checked_at")) < limits.catch_all_days
    ):
        return bool(state["catch_all"]), "cached"

    allowed, reason = _probe_budget_left(domain, respect_interval=respect_interval)
    if not allowed:
        return None, reason

    probe = f"{_random_local_part()}@{domain}"
    _record_probe(domain)
    result = smtp_probe(probe, hosts)
    if result.outcome == ACCEPTED:
        repo.save_domain_state(
            domain, {"catch_all": 1, "catch_all_checked_at": utcnow(), "smtp_policy": "accepts"}
        )
        return True, f"random recipient accepted ({result.code})"
    if result.outcome == REJECTED:
        repo.save_domain_state(
            domain,
            {"catch_all": 0, "catch_all_checked_at": utcnow(), "smtp_policy": "rejects_unknown"},
        )
        return False, f"random recipient rejected ({result.code})"
    repo.save_domain_state(domain, {"smtp_policy": "blocked"})
    return None, f"inconclusive: {result.message}"


# ---------------------------------------------------------------------------
# The verdict (FR-304)
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """One FR-304 verdict with the evidence behind each of the five checks."""

    email: str
    result: str
    detail: dict[str, Any] = field(default_factory=dict)
    checked_at: str = ""
    from_cache: bool = False

    @property
    def usable(self) -> bool:
        """FR-304: an ``invalid`` address is never used."""
        return self.result != INVALID

    def as_contact_fields(self) -> dict[str, Any]:
        return {
            "email_validation": self.result,
            "email_validation_detail": self.detail,
            "email_validated_at": self.checked_at or utcnow(),
            "is_generic_mailbox": 1 if self.detail.get("role") else 0,
        }


def check_syntax(email: str) -> tuple[bool, str]:
    """Syntax check, using ``email_validator`` when it is installed."""
    address = (email or "").strip()
    if not address or len(address) > 254 or address.count("@") != 1:
        return False, "malformed"
    local, _, domain = address.partition("@")
    if len(local) > 64 or ".." in address or local.startswith(".") or local.endswith("."):
        return False, "malformed local part"
    if not SYNTAX_RE.match(address):
        return False, "does not match RFC 5322 addr-spec"
    try:
        from email_validator import EmailNotValidError, validate_email  # noqa: PLC0415

        try:
            validate_email(address, check_deliverability=False)
        except EmailNotValidError as exc:
            return False, str(exc)[:120]
    except ImportError:  # pragma: no cover - declared dependency
        pass
    return True, f"ok ({domain})"


def validate(
    email: str,
    *,
    allow_smtp: bool = True,
    force: bool = False,
    contact_id: str | None = None,
) -> ValidationResult:
    """Run the FR-304 checks and return one verdict.

    ``allow_smtp=False`` runs the offline checks only, which is what the unit
    tests and the bulk paths use; the verdict is then ``unknown`` wherever a
    probe would have been needed, never ``valid``.
    """
    address = (email or "").strip().lower()
    detail: dict[str, Any] = {}

    if not force:
        cached = repo.cached_validation(address)
        if cached and not _supersedable(cached, allow_smtp):
            return ValidationResult(
                email=address,
                result=cached["result"],
                detail=cached.get("detail") or {},
                checked_at=cached["checked_at"],
                from_cache=True,
            )

    ok, note = check_syntax(address)
    detail["syntax"] = {"ok": ok, "note": note}
    if not ok:
        return _finish(address, INVALID, detail, contact_id)

    domain = address.rsplit("@", 1)[-1]

    disposable = is_disposable(domain)
    detail["disposable"] = disposable
    if disposable:
        detail["reason"] = "disposable domain"
        return _finish(address, INVALID, detail, contact_id)

    role = is_role_address(address)
    detail["role"] = role

    mx = mx_for(domain, force=force)
    detail["mx"] = {"ok": mx.has_mx, "hosts": mx.hosts[:3], "note": mx.error}
    if not mx.has_mx:
        detail["reason"] = "domain cannot receive mail"
        return _finish(address, INVALID, detail, contact_id)

    if not allow_smtp or not limits.smtp_enabled:
        detail["smtp"] = {"outcome": "skipped", "note": "SMTP verification not attempted"}
        detail["catch_all"] = None
        return _finish(address, RISKY if role else UNKNOWN, detail, contact_id)

    allowed, reason = _probe_budget_left(domain)
    if not allowed:
        detail["smtp"] = {"outcome": "skipped", "note": reason}
        return _finish(address, RISKY if role else UNKNOWN, detail, contact_id)

    _record_probe(domain)
    probe = smtp_probe(address, mx.hosts)
    detail["smtp"] = {
        "outcome": probe.outcome,
        "code": probe.code,
        "note": probe.message,
        "host": probe.host,
    }

    if probe.outcome == REJECTED:
        detail["reason"] = "mailbox rejected by the receiving server"
        repo.save_domain_state(domain, {"smtp_policy": "rejects_unknown"})
        return _finish(address, INVALID, detail, contact_id)

    if probe.outcome == ACCEPTED:
        catch_all, note = detect_catch_all(domain, mx.hosts, respect_interval=False)
        detail["catch_all"] = {"value": catch_all, "note": note}
        if catch_all is True:
            detail["reason"] = "domain accepts every recipient; acceptance proves nothing"
            return _finish(address, RISKY, detail, contact_id)
        if catch_all is False:
            return _finish(address, RISKY if role else VALID, detail, contact_id)
        return _finish(address, RISKY if role else UNKNOWN, detail, contact_id)

    # Greylisting, blocked port 25, a server that refuses verification: the
    # honest answer is "unknown" - never "valid" (FR-304).
    detail["reason"] = "verification inconclusive"
    return _finish(address, RISKY if role else UNKNOWN, detail, contact_id)


def _supersedable(cached: dict, allow_smtp: bool) -> bool:
    """Whether a cached verdict should give way to a real check (FR-304, FR-305).

    An offline run caches ``unknown`` because no probe was attempted, not
    because the mailbox is doubtful.  Letting that stand would mean an address
    first checked offline - which is what the manual-entry route does - could
    never reach ``valid`` for the whole cache period.  Only a verdict that
    never reached the mail server is superseded, so FR-305's rule that a server
    is not asked the same question twice still holds.
    """
    if not allow_smtp or not limits.smtp_enabled:
        return False
    if cached.get("result") not in (UNKNOWN, RISKY):
        return False
    smtp = (cached.get("detail") or {}).get("smtp") or {}
    return smtp.get("outcome") in (None, "skipped")


def _finish(
    email: str, verdict: str, detail: dict[str, Any], contact_id: str | None
) -> ValidationResult:
    """Cache the verdict (FR-305) and write it onto the contact when there is one."""
    days = limits.cache_days if verdict in (VALID, INVALID) else limits.negative_cache_days
    # A little jitter so a batch validated together does not all expire together
    # and re-probe one mail server in lockstep (FR-305).
    expires = datetime.now(UTC) + timedelta(days=days, seconds=random.randint(0, 3600))
    checked = utcnow()
    result = ValidationResult(email=email, result=verdict, detail=detail, checked_at=checked)
    try:
        repo.cache_validation(email, verdict, detail, expires.isoformat(timespec="seconds"))
    except Exception:  # noqa: BLE001 - caching must never fail a validation
        log.exception("Could not cache the validation verdict for %s", email)
    if contact_id:
        repo.set_validation(contact_id, verdict, detail)
    return result


def validate_contact(
    contact: dict, *, allow_smtp: bool = True, force: bool = False
) -> ValidationResult | None:
    """Validate the address on a stored contact and write the verdict back."""
    email = (contact or {}).get("email")
    if not email:
        return None
    return validate(email, allow_smtp=allow_smtp, force=force, contact_id=contact.get("id"))


def validate_many(
    emails: list[str], *, allow_smtp: bool = True, force: bool = False
) -> dict[str, ValidationResult]:
    """Validate a batch, grouped by domain so one server is probed in sequence."""
    ordered = sorted({e.strip().lower() for e in emails if e}, key=lambda e: e.rsplit("@", 1)[-1])
    return {email: validate(email, allow_smtp=allow_smtp, force=force) for email in ordered}


def best_of(results: list[ValidationResult]) -> ValidationResult | None:
    """Pick the strongest verdict from several candidate addresses."""
    if not results:
        return None
    return max(results, key=lambda r: (VERDICT_RANK.get(r.result, 0), -len(r.email)))
