"""Linear webhook signature verification + replay protection."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime


class WebhookVerifyError(ValueError):
    pass


def verify_signature(*, body: bytes, signature_header: str | None, secret: str) -> None:
    """Raises WebhookVerifyError on mismatch.

    Linear sends `Linear-Signature` header with hex-encoded HMAC-SHA256 of the raw body
    using the configured signing secret.
    """
    if not signature_header:
        raise WebhookVerifyError("missing Linear-Signature header")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header.strip()):
        raise WebhookVerifyError("HMAC mismatch")


def reject_replay(webhook_timestamp_ms: int | None, *, max_skew_sec: int = 300) -> None:
    """Reject if the webhook timestamp is older than max_skew (replay defence)."""
    if webhook_timestamp_ms is None:
        raise WebhookVerifyError("missing webhookTimestamp in payload")
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    skew = abs(now_ms - webhook_timestamp_ms) / 1000
    if skew > max_skew_sec:
        raise WebhookVerifyError(f"webhook timestamp skew {skew:.0f}s exceeds {max_skew_sec}s")


def make_delivery_id(payload: dict[str, object]) -> str:
    """Idempotency key from webhook payload — webhookId + webhookTimestamp."""
    wid = payload.get("webhookId") or ""
    wts = payload.get("webhookTimestamp") or ""
    return f"{wid}:{wts}"
