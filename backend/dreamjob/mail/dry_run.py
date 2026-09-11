"""The dry-run transport guard (FR-325, RK-05, NFR-702, NFR-302, FR-304).

The product owner asked for the whole application pipeline up to a generated
e-mail with the tailored CV attached, and explicitly **not** the send.  That is
what this module is: a guard that lets every step run for real and stops one
step short of the wire.

Three properties make it a guard rather than a warning.

**It lives in the mail layer, not in a router.**  ``DREAMJOB_MAIL_DRY_RUN``
defaults to *true*, and while it is true :func:`install_transport_guard`
replaces every shipped :class:`~dreamjob.mail.base.MailBackend` in the registry
with a subclass whose ``send`` raises :class:`TransportBlocked` before the
provider is touched.  Every path that obtains a backend goes through that
registry - ``get_backend``, ``backend_for_account`` and therefore
``dispatcher.resolve_backend`` - so a future caller that reaches straight for
``dispatcher.send_package``, or a new router that nobody has reviewed, is
blocked by the same code as the Apply Browser.  A guard in the interface would
only have hidden the button.

**The whole path still runs.**  :func:`send_one` resolves the recipient, runs
the FR-325 / FR-304 / NFR-302 guard rails through
:func:`~dreamjob.mail.dispatcher.check_send_allowed`, composes the message with
:mod:`~dreamjob.mail.composer` - the tailored CV attached, the briefing and the
motivation document refused by FR-321, the NFR-302 objection sentence appended
- asks the backend for its own provider preconditions, and then writes the
assembled RFC 5322 message to ``data/generated/dry_run/`` and records a
dispatch row with ``delivery_status = 'dry_run'``.  What would have gone out is
therefore on disk, byte for byte, and can be opened in any mail client.

**Nothing pretends to have been sent.**  A dry-run dispatch never gets
``sent_at``, so it does not count against the FR-325 daily cap, does not pace
the next send, does not mark the package ``sent`` and does not move the
opportunity to ``applied``.  The API response says, in words, that nothing was
sent and why.

Two refusals are honoured even in a dry run, because they are about the
recipient rather than about the transport: an address that has objected
(NFR-302) and an address FR-304 will not allow.  Assembling a message for
either of those on disk would be building the thing the requirement forbids.
The refusals that only time will fix - the send window, the pacing rule, the
daily cap - do not stop a dry run; they are reported as what *would* have
happened.

Turning the guard off is deliberate and server-side: set
``DREAMJOB_MAIL_DRY_RUN=false`` in ``.env`` and restart the API.  From then on
:func:`send_one` and :func:`send_all` delegate to
:func:`~dreamjob.mail.dispatcher.send_package` unchanged, and the registry
wrapper stops blocking.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from dreamjob.config import get_settings
from dreamjob.db.repositories import dispatch as repo
from dreamjob.mail import base as mail_base
from dreamjob.mail import composer
from dreamjob.mail.base import (
    MailBackend,
    MailBackendError,
    OutgoingMessage,
)
from dreamjob.mail.dispatcher import (
    SendRefused,
    check_send_allowed,
    resolve_backend,
    send_package,
)
from dreamjob.security.audit import log_metric, record_audit

log = logging.getLogger(__name__)

#: The environment variable that arms sending.  Named here so the API can tell
#: the job seeker exactly which switch to throw rather than "check the config".
SETTING_NAME = "DREAMJOB_MAIL_DRY_RUN"

#: ``dispatch.delivery_status`` for a message that was built and not sent.
DRY_RUN_STATUS = "dry_run"

#: Where the assembled messages are written, under ``data/generated``.
DRY_RUN_DIRNAME = "dry_run"

#: Marker on a wrapped backend class, so wrapping is idempotent.
_GUARD_MARKER = "_dreamjob_transport_guarded"

DISARM_INSTRUCTION = (
    f"Set {SETTING_NAME}=false in .env and restart the API to arm sending. "
    "It defaults to true precisely so that no message can leave by accident."
)


class TransportBlocked(MailBackendError):
    """The message was fully assembled and the transport refused to carry it.

    A subclass of :class:`~dreamjob.mail.base.MailBackendError` on purpose: the
    routers already turn that into a 400 with the message shown to the job
    seeker, so a caller that has not heard of the dry run still reports
    something true and actionable instead of a 500.
    """


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


def is_armed() -> bool:
    """True while the dry run is on, i.e. while no message may leave."""
    return bool(get_settings().mail_dry_run)


def blocked_reason(backend_key: str | None = None) -> str:
    where = f" via the {backend_key} backend" if backend_key else ""
    return (
        f"{SETTING_NAME} is on: the message was assembled in full{where} and stopped at the "
        f"transport, so nothing left this machine. {DISARM_INSTRUCTION}"
    )


def assert_transport_allowed(backend_key: str | None = None) -> None:
    """The single decision.  Raises :class:`TransportBlocked` while armed."""
    if is_armed():
        raise TransportBlocked(blocked_reason(backend_key))


def dry_run_dir() -> Path:
    return get_settings().generated_dir / DRY_RUN_DIRNAME


# ---------------------------------------------------------------------------
# The registry wrapper: the guard no caller can route around
# ---------------------------------------------------------------------------


def _guarded(cls: type[MailBackend]) -> type[MailBackend]:
    """A subclass whose ``send`` refuses while the dry run is on."""

    class Guarded(cls):  # type: ignore[valid-type,misc]
        def send(self, message: OutgoingMessage):  # type: ignore[override] # noqa: ANN201
            # Checked at call time, not at import time, so switching the
            # setting off and restarting is the only thing that arms sending -
            # and so a test that flips it needs no re-registration.
            assert_transport_allowed(self.key)
            return super().send(message)

    setattr(Guarded, _GUARD_MARKER, True)
    Guarded.__name__ = cls.__name__
    Guarded.__qualname__ = cls.__qualname__
    Guarded.__module__ = cls.__module__
    Guarded.__doc__ = cls.__doc__
    return Guarded


def install_transport_guard() -> list[str]:
    """Wrap every shipped mail backend so its ``send`` honours the dry run.

    Only classes defined inside :mod:`dreamjob.mail` are wrapped: those are the
    ones that put bytes on a wire, and they are where a new real transport
    would be added.  A backend registered from elsewhere - a fake in a test, a
    recorder in a fixture - is not a transport and is left alone, so wrapping
    cannot change what those are for.

    Idempotent, and safe to call on every request: the marker attribute stops a
    class being wrapped twice, and a backend registered later is picked up the
    next time this runs.
    """
    wrapped: list[str] = []
    # Populates the registry on first use (it imports the concrete backends).
    for capability in mail_base.all_capabilities():
        cls = mail_base.backend_class(capability.key)
        if getattr(cls, _GUARD_MARKER, False):
            continue
        if not (cls.__module__ or "").startswith("dreamjob.mail"):
            continue
        mail_base.register_backend(_guarded(cls))
        wrapped.append(capability.key)
    if wrapped:
        log.info("Dry-run transport guard installed on: %s", ", ".join(wrapped))
    return wrapped


def guarded_backends() -> list[str]:
    """The backend keys whose transport is behind the guard."""
    install_transport_guard()
    return sorted(
        capability.key
        for capability in mail_base.all_capabilities()
        if getattr(mail_base.backend_class(capability.key), _GUARD_MARKER, False)
    )


# ---------------------------------------------------------------------------
# Status (what the Apply Browser shows above the Send button)
# ---------------------------------------------------------------------------


def _backend_state(job_seeker_id: str) -> tuple[MailBackend | None, dict[str, Any]]:
    """The mailbox this seeker would send from, and whether it is usable."""
    try:
        backend, account = resolve_backend(job_seeker_id)
    except MailBackendError as exc:
        # A missing mailbox and an unknown backend key are both "you cannot
        # send yet", and the status endpoint reports either without anyone
        # having to attempt a send to find out (MailBackendNotConfigured is a
        # MailBackendError, so one clause covers both).
        return None, {
            "backend": get_settings().mail_backend,
            "configured": False,
            "reason": str(exc),
        }
    try:
        state = dict(backend.status())
    except Exception as exc:  # noqa: BLE001 - status must never raise at the caller
        state = {"backend": backend.key, "configured": False, "reason": str(exc)}
    state["address"] = account.get("address")
    state["account_id"] = account.get("id")
    return backend, state


def send_status(job_seeker_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Is sending armed, and if not, why not (FR-325, RK-05).

    Everything a person needs before pressing Send: the dry run, whether a
    mailbox is actually configured, how much of today's cap is left, the pacing
    rule, and the send window - which is expressed in the *recipient's* time
    zone, so it is stated as a rule rather than as a clock reading.
    """
    settings = get_settings()
    moment = now or datetime.now(UTC)
    _backend, backend_state = _backend_state(job_seeker_id)

    day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(
        timespec="seconds"
    )
    used = repo.sent_since(job_seeker_id, day_start)
    remaining = max(0, settings.send_daily_cap - used)

    blockers: list[str] = []
    if is_armed():
        blockers.append(
            f"{SETTING_NAME} is on, so no message can leave this machine. {DISARM_INSTRUCTION}"
        )
    if not backend_state.get("configured"):
        blockers.append(
            backend_state.get("reason") or "No mail backend is configured for this job seeker."
        )
    if remaining == 0:
        blockers.append(
            f"The FR-325 daily cap of {settings.send_daily_cap} messages is used up "
            f"({used} today); it resets at midnight."
        )

    return {
        "sending_armed": not blockers,
        "dry_run": is_armed(),
        "blocked_because": blockers,
        "setting": {
            "name": SETTING_NAME,
            "value": is_armed(),
            "default": True,
            "how_to_arm_sending": DISARM_INSTRUCTION,
            "enforced_in": "dreamjob.mail.dry_run (server-side, not the interface)",
        },
        "guarded_backends": guarded_backends(),
        "mail_backend": backend_state,
        "daily_cap": {"cap": settings.send_daily_cap, "used_today": used, "remaining": remaining},
        "rate": {
            "min_interval_seconds": settings.send_min_interval_seconds,
            "last_sent_at": repo.last_sent_at(job_seeker_id),
        },
        "send_window": {
            "start": settings.send_window_start.strftime("%H:%M"),
            "end": settings.send_window_end.strftime("%H:%M"),
            "days": "Monday to Friday",
            "timezone": "the recipient's, derived from the company (FR-325)",
        },
        "dry_run_dir": str(dry_run_dir()),
        "dispatch_counts": repo.status_counts(job_seeker_id),
    }


# ---------------------------------------------------------------------------
# Sending, or rather not sending
# ---------------------------------------------------------------------------


def _write_mime(job_seeker_id: str, package_id: str, mime: EmailMessage) -> Path:
    """Put the assembled message on disk so it can be opened and read."""
    directory = dry_run_dir() / job_seeker_id
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")[:-3]
    path = directory / f"{package_id}-{stamp}.eml"
    path.write_bytes(mime.as_bytes())
    return path


def send_one(
    package_id: str,
    job_seeker_id: str,
    *,
    approved_by: str,
    backend_key: str | None = None,
    ignore_window: bool = False,
    now: datetime | None = None,
    require_approval: bool = True,
) -> dict[str, Any]:
    """Send one package - or, while the dry run is on, build it and stop.

    With the guard off this is :func:`~dreamjob.mail.dispatcher.send_package`
    and nothing else; there is no second, divergent send path to keep in step.
    """
    install_transport_guard()
    if not is_armed():
        _retire_dry_runs(package_id, job_seeker_id)
        return send_package(
            package_id,
            job_seeker_id,
            approved_by=approved_by,
            backend_key=backend_key,
            ignore_window=ignore_window,
            now=now,
            require_approval=require_approval,
        )
    return _prepare_only(
        package_id,
        job_seeker_id,
        approved_by=approved_by,
        backend_key=backend_key,
        ignore_window=ignore_window,
        now=now,
        require_approval=require_approval,
    )


def _retire_dry_runs(package_id: str, job_seeker_id: str) -> int:
    """Close the rehearsals before the real thing goes out.

    ``dispatch.already_sent_to`` treats any dispatch that is not ``failed`` as
    "this package has already gone to this person", which is right for a send
    and wrong for a rehearsal: without this, every package that had ever been
    prepared under the dry run would be refused the moment sending was armed.
    The rows are closed rather than deleted, and the reason says what they
    were, so the send log still shows that the message was built and withheld.
    """
    retired = 0
    for row in repo.dispatches_for_package(package_id, job_seeker_id):
        if row.get("delivery_status") != DRY_RUN_STATUS:
            continue
        repo.update_dispatch(
            row["id"],
            {
                "delivery_status": "failed",
                "last_error": (
                    "prepared as a dry run and never sent; closed when sending was armed"
                ),
                "delivery_detail": {**(row.get("delivery_detail") or {}), "superseded": True},
            },
        )
        retired += 1
    return retired


def _prepare_only(
    package_id: str,
    job_seeker_id: str,
    *,
    approved_by: str,
    backend_key: str | None,
    ignore_window: bool,
    now: datetime | None,
    require_approval: bool,
) -> dict[str, Any]:
    """The full dispatch path with the last step removed."""
    moment = now or datetime.now(UTC)
    notes: list[str] = []

    package = repo.package_for_dispatch(package_id, job_seeker_id)
    if package is None:
        raise SendRefused("No such application package for this job seeker.")
    # FR-324: a person approves generated material before it is dispatched.
    if require_approval and package.get("status") != "approved":
        raise SendRefused(
            f"Package {package_id} is {package.get('status')!r}. FR-324 requires the job seeker "
            "to approve generated material before it is sent."
        )
    # FR-322: a CV that failed the factual-consistency gate is never assembled
    # unless the job seeker approved it with a recorded override (FR-324,
    # migration 146).  That decision is on the row, so it survives to here.
    if package.get("consistency_status") == "fail" and not (
        package.get("consistency_override") or ""
    ).strip():
        raise SendRefused(
            "The factual-consistency check on this CV failed (FR-322); fix or regenerate it, "
            "or approve it with a recorded override, before sending."
        )
    # A previous *dry run* must not look like a send: only a dispatch that
    # actually left counts as "already dispatched", so the job seeker can
    # rebuild and re-inspect the same package as often as they like.
    existing = repo.already_sent_to(job_seeker_id, package.get("contact_email") or "", package_id)
    if existing and existing.get("sent_at"):
        raise SendRefused(
            f"This package was already dispatched to {package.get('contact_email')} "
            f"on {existing['sent_at']}."
        )

    seeker = repo.seeker_identity(job_seeker_id) or {}
    backend, account = _resolve_or_note(job_seeker_id, backend_key, seeker, notes)
    from_email = _sender_address(backend, seeker, notes)

    # The FR-325 / FR-304 / NFR-302 rails, run for real against the same code
    # the live send uses - so the dry run reports the decision that would be
    # taken, not a guess at it.
    decision = check_send_allowed(
        job_seeker_id,
        recipient_email=package.get("contact_email"),
        email_validation=package.get("contact_email_validation"),
        objected=bool(package.get("contact_objected")),
        country=package.get("company_country"),
        locations=package.get("company_locations"),
        jurisdiction=package.get("company_jurisdiction"),
        now=moment,
        ignore_window=ignore_window,
    )
    # A refusal that waiting will not fix is about the recipient, not about the
    # transport: NFR-302 and FR-304 forbid preparing this message at all.
    if not decision.allowed and not decision.deferrable:
        raise SendRefused(decision.reason)

    message = composer.compose(
        package,
        seeker=seeker,
        from_email=from_email,
        language=package.get("language") or package.get("opportunity_language"),
    )
    if backend is not None:
        # Recipient, subject and total size against the provider's own limits.
        # The last thing that happens before the wire - and the wire is where
        # this stops.
        backend.check_message(message)

    mime = composer.to_mime(message)
    path = _write_mime(job_seeker_id, package_id, mime)
    size = path.stat().st_size

    detail = {
        "dry_run": True,
        "setting": SETTING_NAME,
        "mime_path": str(path),
        "mime_bytes": size,
        "would_send_now": decision.allowed,
        "guard_rail_code": decision.code,
        "guard_rail_reason": decision.reason,
        "attachments": message.attachment_names,
        "notes": notes,
    }
    dispatch_id = repo.create_dispatch(
        job_seeker_id,
        {
            "application_package_id": package_id,
            "backend": account.get("backend") or get_settings().mail_backend,
            "mail_account_id": account.get("id"),
            "kind": "application",
            "recipient_email": message.to_email,
            "recipient_name": message.to_name,
            "subject": message.subject,
            "message_id": message.message_id,
            "attachments": message.attachment_paths,
            # No ``sent_at``: nothing left, so nothing may count as having left.
            "delivery_status": DRY_RUN_STATUS,
            "delivery_detail": detail,
            "scheduled_for": decision.retry_at,
            "recipient_timezone": decision.timezone,
            "approved_by": approved_by,
        },
    )

    # NFR-702: a message that was built and withheld is part of the record too.
    record_audit(
        "application.dry_run",
        "dispatch",
        dispatch_id,
        seeker_id=job_seeker_id,
        actor=approved_by,
        detail={
            "recipient": message.to_email,
            "subject": message.subject,
            "attachments": message.attachment_names,
            "mime_path": str(path),
            "application_package_id": package_id,
            "opportunity_id": package.get("opportunity_id"),
            "would_send_now": decision.allowed,
            "guard_rails": {"code": decision.code, "reason": decision.reason,
                            "timezone": decision.timezone},
            "setting": {SETTING_NAME: True},
        },
    )
    log_metric(
        "mail.dry_run",
        job_seeker_id=job_seeker_id,
        dispatch_id=dispatch_id,
        attachments=len(message.attachments),
        bytes=size,
    )

    return {
        "dispatch_id": dispatch_id,
        "status": DRY_RUN_STATUS,
        "sent": False,
        "dry_run": True,
        "message": blocked_reason(account.get("backend")),
        "recipient": message.to_email,
        "recipient_name": message.to_name,
        "subject": message.subject,
        "attachments": message.attachment_names,
        "mime_path": str(path),
        "mime_bytes": size,
        "would_send_now": decision.allowed,
        "guard_rails": {
            "allowed": decision.allowed,
            "code": decision.code,
            "reason": decision.reason,
            "retry_at": decision.retry_at,
            "timezone": decision.timezone,
            "recipient_local_time": decision.recipient_local_time,
            "checks": decision.checks,
        },
        "notes": notes,
    }


def _resolve_or_note(
    job_seeker_id: str, backend_key: str | None, seeker: dict, notes: list[str]
) -> tuple[MailBackend | None, dict[str, Any]]:
    """The mailbox, or a note explaining that there is not one yet.

    A dry run whose whole purpose is to show the assembled message must not
    fail because no mailbox has been connected; the missing mailbox is reported
    instead, and ``/send-status`` says the same thing.
    """
    try:
        backend, account = resolve_backend(job_seeker_id, backend_key)
    except MailBackendError as exc:
        notes.append(f"No mailbox is connected: {exc}")
        return None, {
            "id": None,
            "backend": backend_key or get_settings().mail_backend,
            "address": get_settings().mail_from or seeker.get("email") or "",
        }
    return backend, account


def _sender_address(backend: MailBackend | None, seeker: dict, notes: list[str]) -> str:
    settings = get_settings()
    if backend is not None:
        try:
            return backend.sender_address()
        except MailBackendError as exc:
            notes.append(f"The mailbox has no sender address: {exc}")
    fallback = settings.mail_from or seeker.get("email") or "dry-run@dreamjob.local"
    notes.append(f"Composed with {fallback} as the From address for the dry run.")
    return fallback


def send_all(
    job_seeker_id: str,
    *,
    approved_by: str,
    package_ids: list[str] | None = None,
    backend_key: str | None = None,
    ignore_window: bool = False,
    limit: int = 100,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The whole approved selection, one message at a time.

    Resolving "the approved selection" happens here rather than in the router,
    so a caller cannot widen it by handing in a list of its own choosing and
    call that a bulk send: ids that are not approved are refused one by one by
    :func:`send_one`, and with no ids at all the approved packages are the set.
    """
    install_transport_guard()
    from dreamjob.db.repositories import applications as packages_repo  # noqa: PLC0415

    ids = list(package_ids or [])
    if not ids:
        ids = [
            row["id"]
            for row in packages_repo.list_packages(
                job_seeker_id, status="approved", limit=limit, offset=0
            )
        ]

    results: list[dict[str, Any]] = []
    prepared = sent = refused = 0
    for package_id in ids[:limit]:
        try:
            outcome = send_one(
                package_id,
                job_seeker_id,
                approved_by=approved_by,
                backend_key=backend_key,
                ignore_window=ignore_window,
                now=now,
            )
        except MailBackendError as exc:
            # One refusal must not stop the batch; it is reported in its place.
            refused += 1
            results.append({"package_id": package_id, "status": "refused", "reason": str(exc)})
            continue
        outcome["package_id"] = package_id
        results.append(outcome)
        if outcome.get("status") == DRY_RUN_STATUS:
            prepared += 1
        elif outcome.get("status") == "sent":
            sent += 1

    return {
        "dry_run": is_armed(),
        "requested": len(ids),
        "prepared": prepared,
        "sent": sent,
        "refused": refused,
        "message": (
            f"{prepared} message(s) were assembled and written to {dry_run_dir()}. "
            f"None were sent. {DISARM_INSTRUCTION}"
        )
        if is_armed()
        else f"{sent} message(s) were sent.",
        "results": results,
    }


# The guard is armed as soon as this module is imported, which is what makes
# importing it from the Apply Browser router enough to protect every other
# send path in the process as well.
install_transport_guard()
