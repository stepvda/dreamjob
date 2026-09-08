"""Resend transactional relay (FR-325, FR-326, NFR-302).

The second dispatch backend.  It sends over Resend's HTTP API from the
project's own domain (``stephane@stepvda.com``) and exists for the case where
no personal mailbox has been connected: the job seeker still gets a send path,
``Reply-To`` still points at their address, and a reply still reaches them.

What it cannot do is section 2.4 in full.  Resend is a one-way relay with no
IMAP and no mailbox to poll, so a reply that goes to ``Reply-To`` never passes
through Dream Job, and delivery news arrives instead as a webhook.  Bounce and
complaint handling for this backend therefore lives in
:func:`handle_event`, driven by ``POST /api/mail/resend/webhook``, and the
capability flags say so, so the inbox poller does not try to poll a mailbox
that is not there.

**The API key is not available yet.**  The backend is complete; it simply has
nothing to authenticate with until ``RESEND_API_KEY`` is put in ``.env``.  That
state is reported by :meth:`ResendBackend.status` as ``configured: false`` with
the exact reason, and a send raises :class:`MailBackendNotConfigured` with the
same text rather than failing somewhere deeper.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, ClassVar

import httpx

from dreamjob.config import get_settings
from dreamjob.mail.base import (
    BackendCapabilities,
    MailBackend,
    MailBackendNotConfigured,
    MailBackendUnavailable,
    OutgoingMessage,
    SendResult,
    register_backend,
)

log = logging.getLogger(__name__)

API_ENDPOINT = "https://api.resend.com/emails"

#: The verified sending identity for this installation.
DEFAULT_FROM = "stephane@stepvda.com"

#: Where the webhook signing secret is kept.  It is a rotatable operational
#: secret rather than deployment configuration, so it lives in ``app_setting``
#: and can be changed from the administration screen without a restart.
WEBHOOK_SECRET_KEY = "mail.resend_webhook_secret"

#: Reject a webhook whose timestamp is further away than this (replay window).
WEBHOOK_TOLERANCE_SECONDS = 5 * 60

NOT_CONFIGURED_MESSAGE = (
    "Resend is not configured: RESEND_API_KEY is empty. Create an API key at "
    "https://resend.com/api-keys, verify the sending domain for "
    f"{DEFAULT_FROM}, then add RESEND_API_KEY=re_... to .env and restart. "
    "Until then, connect a Gmail mailbox instead - it is also the only backend "
    "that puts replies in your own inbox."
)

#: Resend event type -> the ``dispatch.delivery_status`` it implies (FR-326).
EVENT_STATUS: dict[str, str] = {
    "email.sent": "sent",
    "email.delivered": "delivered",
    "email.delivery_delayed": "sent",
    "email.bounced": "bounced",
    "email.complained": "bounced",
    "email.failed": "failed",
}


class WebhookVerificationError(RuntimeError):
    """The webhook signature did not verify; the request is rejected with 401."""


@register_backend
class ResendBackend(MailBackend):
    capabilities: ClassVar[BackendCapabilities] = BackendCapabilities(
        key="resend",
        display_name="Resend (stepvda.com relay)",
        # No mailbox of the job seeker's own: Reply-To routes replies to them,
        # but Dream Job never sees them (section 2.4).
        replies_to_seeker_inbox=False,
        bounce_detection="webhook",
        reply_detection="none",
        requires_oauth=False,
        # Resend's documented ceiling for a request including attachments.
        max_message_bytes=40 * 1024 * 1024,
    )

    def __init__(self, account: dict | None = None, *, job_seeker_id: str | None = None):
        super().__init__(account, job_seeker_id=job_seeker_id)

    # -- configuration ----------------------------------------------------
    @property
    def api_key(self) -> str:
        return get_settings().resend_api_key.strip()

    def sender_address(self) -> str:
        return (self.account.get("address") or get_settings().mail_from or DEFAULT_FROM).strip()

    def status(self) -> dict:
        settings = get_settings()
        configured = bool(self.api_key)
        return {
            "backend": self.key,
            "configured": configured,
            "reason": None if configured else NOT_CONFIGURED_MESSAGE,
            "from_address": self.sender_address(),
            "webhook_path": "/api/mail/resend/webhook",
            "webhook_secret_set": bool(webhook_secret()),
            "mail_from_setting": settings.mail_from or DEFAULT_FROM,
            "capabilities": self.capabilities.__dict__,
        }

    # -- sending ----------------------------------------------------------
    def _payload(self, message: OutgoingMessage) -> dict[str, Any]:
        sender = message.from_email or self.sender_address()
        from_header = f"{message.from_name} <{sender}>" if message.from_name else sender
        to_header = (
            f"{message.to_name} <{message.to_email}>" if message.to_name else message.to_email
        )

        # Threading headers travel as ordinary headers; Resend passes them
        # through unchanged, which is what makes FR-326 matching work here too.
        headers: dict[str, str] = {}
        if message.message_id:
            headers["Message-ID"] = message.message_id
        if message.in_reply_to:
            headers["In-Reply-To"] = message.in_reply_to
            headers["References"] = message.references or message.in_reply_to
        elif message.references:
            headers["References"] = message.references
        # See composer.to_mime: an approved application is not automated mail,
        # and saying it is invites the bulk classification RK-05 guards against.
        headers.update(message.extra_headers)

        payload: dict[str, Any] = {
            "from": from_header,
            "to": [to_header],
            "subject": message.subject,
            # RK-05: text only.  Resend will not synthesise an HTML part.
            "text": message.body_text,
            "headers": headers,
        }
        if message.reply_to:
            payload["reply_to"] = message.reply_to
        if message.attachments:
            payload["attachments"] = [
                {
                    "filename": a.filename,
                    "content": base64.b64encode(a.content).decode("ascii"),
                    "content_type": a.content_type,
                }
                for a in message.attachments
            ]
        return payload

    def send(self, message: OutgoingMessage) -> SendResult:
        if not self.api_key:
            raise MailBackendNotConfigured(NOT_CONFIGURED_MESSAGE)
        self.check_message(message)
        if not message.from_email:
            message.from_email = self.sender_address()

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    API_ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        # Same address twice would otherwise be sent twice on a retry.
                        "Idempotency-Key": message.message_id or "",
                    },
                    json=self._payload(message),
                )
        except httpx.HTTPError as exc:
            raise MailBackendUnavailable(f"Could not reach Resend: {exc}") from exc

        if resp.status_code == 401:
            raise MailBackendNotConfigured(
                "Resend rejected the API key (401). Check RESEND_API_KEY in .env; "
                "keys are shown once, at https://resend.com/api-keys."
            )
        if resp.status_code == 403:
            raise MailBackendUnavailable(
                f"Resend refused to send from {message.from_email}: the domain is not verified "
                "for this account. Verify it at https://resend.com/domains."
            )
        if resp.status_code >= 400:
            raise MailBackendUnavailable(
                f"Resend refused the message ({resp.status_code}): {resp.text[:300]}"
            )

        body = resp.json()
        return SendResult(
            # Resend keeps the Message-ID we supplied; its own id is the handle
            # its webhooks arrive under, so it is kept as the thread id.
            message_id=message.message_id,
            thread_id=body.get("id"),
            status="sent",
            backend=self.key,
            detail={"provider_id": body.get("id"), "from": message.from_email},
        )


# ---------------------------------------------------------------------------
# Webhooks (FR-326) - Resend has no IMAP, so this is how delivery news arrives
# ---------------------------------------------------------------------------


def webhook_secret() -> str | None:
    from dreamjob.db.repositories import dispatch as repo  # noqa: PLC0415 - avoids a cycle

    return repo.get_setting(WEBHOOK_SECRET_KEY)


def set_webhook_secret(secret: str) -> None:
    from dreamjob.db.repositories import dispatch as repo  # noqa: PLC0415

    repo.set_setting(WEBHOOK_SECRET_KEY, secret.strip())


def _secret_bytes(secret: str) -> bytes:
    """Resend's signing secrets are ``whsec_`` plus the base64 key material."""
    raw = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
    try:
        return base64.b64decode(raw)
    except Exception:  # noqa: BLE001 - a non-base64 secret is used verbatim
        return secret.encode("utf-8")


def verify_signature(
    body: bytes, headers: dict[str, str], *, secret: str | None = None, now: float | None = None
) -> None:
    """Verify a Resend (Svix) webhook signature, or raise.

    The signed payload is ``<id>.<timestamp>.<body>``; the header carries one
    or more space-separated ``v1,<base64>`` signatures, because a secret being
    rotated produces two valid ones for a while.  The timestamp is checked
    first so a captured request cannot be replayed later, and the comparison
    is constant time.
    """
    secret = secret or webhook_secret()
    if not secret:
        raise WebhookVerificationError(
            "No Resend webhook secret is configured, so delivery and bounce events cannot be "
            "trusted. Copy the signing secret from https://resend.com/webhooks and set it with "
            "POST /api/mail/resend/webhook-secret."
        )

    lowered = {k.lower(): v for k, v in headers.items()}
    msg_id = lowered.get("svix-id") or lowered.get("webhook-id")
    timestamp = lowered.get("svix-timestamp") or lowered.get("webhook-timestamp")
    signatures = lowered.get("svix-signature") or lowered.get("webhook-signature")
    if not (msg_id and timestamp and signatures):
        raise WebhookVerificationError("Webhook is missing its svix-id/timestamp/signature headers")

    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise WebhookVerificationError("Webhook timestamp is not an integer") from exc
    if abs((now if now is not None else time.time()) - sent_at) > WEBHOOK_TOLERANCE_SECONDS:
        raise WebhookVerificationError("Webhook timestamp is outside the replay window")

    signed = b".".join([msg_id.encode(), timestamp.encode(), body])
    expected = base64.b64encode(hmac.new(_secret_bytes(secret), signed, hashlib.sha256).digest())
    for candidate in signatures.split():
        _, _, value = candidate.partition(",")
        if hmac.compare_digest(expected, value.strip().encode()):
            return
    raise WebhookVerificationError("Webhook signature does not match")


def parse_event(payload: dict) -> dict:
    """Normalise one Resend event into the fields FR-326 records."""
    data = payload.get("data") or {}
    event_type = str(payload.get("type") or "")
    to = data.get("to")
    recipient = (to[0] if isinstance(to, list) and to else to) or ""
    bounce = data.get("bounce") or {}
    headers = {str(k).lower(): v for k, v in (data.get("headers") or {}).items()}
    if isinstance(data.get("headers"), list):
        headers = {
            str(h.get("name", "")).lower(): h.get("value")
            for h in data["headers"]
            if isinstance(h, dict)
        }
    return {
        "event_type": event_type,
        "status": EVENT_STATUS.get(event_type),
        "provider_message_id": data.get("email_id") or data.get("id"),
        "message_id": headers.get("message-id"),
        "recipient": _address_only(str(recipient)),
        "subject": data.get("subject"),
        "occurred_at": payload.get("created_at") or data.get("created_at"),
        "bounce_type": bounce.get("type"),
        "bounce_subtype": bounce.get("subType") or bounce.get("sub_type"),
        "reason": bounce.get("message") or data.get("reason"),
        #: A hard bounce or a spam complaint means "never write here again"
        #: (NFR-302 for the complaint, plain hygiene for the bounce).
        "permanent": event_type == "email.complained"
        or str(bounce.get("type", "")).lower() == "permanent",
        "raw": payload,
    }


def _address_only(value: str) -> str:
    if "<" in value and ">" in value:
        return value[value.index("<") + 1 : value.index(">")].strip().lower()
    return value.strip().lower()


def handle_event(body: bytes, headers: dict[str, str], *, verify: bool = True) -> dict:
    """Verify, de-duplicate and apply one webhook delivery.

    Delivered to :mod:`dreamjob.mail.inbox`, which owns the rule for how a
    delivery status changes a dispatch, so the Gmail and Resend paths update
    the send log through exactly the same code.
    """
    if verify:
        verify_signature(body, headers)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookVerificationError("Webhook body is not JSON") from exc

    from dreamjob.mail import inbox  # noqa: PLC0415 - avoids an import cycle

    event = parse_event(payload)
    delivery_id = (
        {k.lower(): v for k, v in headers.items()}.get("svix-id")
        or event.get("provider_message_id")
        or ""
    )
    return inbox.apply_provider_event(event, delivery_id=str(delivery_id), provider="resend")
