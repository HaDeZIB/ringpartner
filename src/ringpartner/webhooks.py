"""Ring webhook verification and parsing (payload spec v1.1).

Ring signs every webhook with HMAC-SHA256 over the *raw* request body, hex encoded, sent as
``X-Signature: sha256=<hex>``. Verify first, parse second: re-serialising the JSON changes
the bytes and breaks the digest.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import ms_to_datetime

SIGNATURE_HEADER = "X-Signature"

EVENT_TYPES = (
    "motion_detected",
    "button_press",
    "device_added",
    "device_removed",
    "device_online",
    "device_offline",
    "app_integration_added",
    "app_integration_removed",
    "subscription_activated",
    "subscription_deactivated",
)

# Values the docs list for motion_detected.sub_type. Treat as open-ended: Ring may add more.
MOTION_SUB_TYPES = ("motion", "human", "vehicle", "package_delivery", "other_motion")


class InvalidSignature(Exception):
    pass


def sign(signing_key: str | bytes, raw_body: bytes) -> str:
    """Return the ``X-Signature`` header value Ring would send for ``raw_body``."""
    key = signing_key.encode() if isinstance(signing_key, str) else signing_key
    return "sha256=" + hmac.new(key, raw_body, hashlib.sha256).hexdigest()


def verify_signature(signing_key: str | bytes, raw_body: bytes, header_value: str | None) -> bool:
    if not header_value:
        return False
    expected = sign(signing_key, raw_body)
    received = header_value.strip()
    # Ring's reference verifier uses removeprefix("sha256="), so a bare hex digest is accepted;
    # any other algorithm prefix (e.g. "sha1=") will simply fail the comparison.
    if not received.startswith("sha256="):
        received = "sha256=" + received
    return hmac.compare_digest(expected.encode(), received.lower().encode())


@dataclass
class WebhookEvent:
    id: str
    type: str
    device_id: str | None
    timestamp: datetime | None
    sub_type: str | None = None
    component_ids: list[str] = field(default_factory=list)
    device_ids: list[str] = field(default_factory=list)
    account_id: str | None = None
    request_id: str | None = None
    version: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def dedupe_key(self) -> str:
        """Ring retries deliveries; dedupe on request_id, falling back to the event id."""
        return self.request_id or self.id


def parse_event(raw_body: bytes | str | dict[str, Any]) -> WebhookEvent:
    doc = raw_body if isinstance(raw_body, dict) else json.loads(raw_body)
    meta = doc.get("meta") or {}
    data = doc.get("data") or {}
    attrs = data.get("attributes") or {}
    # ``source`` is a device id only for device events. app_integration_* carries the account
    # id (source_type "users"); subscription_* carries the literal "subscriptions" and lists
    # covered devices under relationships.devices.data.
    source_type = attrs.get("source_type")
    source = attrs.get("source")
    device_id = source if source_type == "devices" else None
    rel_devices = ((data.get("relationships") or {}).get("devices") or {}).get("data")
    device_ids = [d["id"] for d in rel_devices or [] if isinstance(d, dict) and "id" in d]
    if device_id and device_id not in device_ids:
        device_ids.insert(0, device_id)
    return WebhookEvent(
        id=data.get("id", ""),
        type=data.get("type", "unknown"),
        device_id=device_id,
        timestamp=ms_to_datetime(attrs.get("timestamp")),
        sub_type=attrs.get("sub_type"),
        component_ids=list(attrs.get("component_ids") or []),
        device_ids=device_ids,
        account_id=meta.get("account_id"),
        request_id=meta.get("request_id"),
        version=meta.get("version"),
        raw=doc,
    )


def verify_and_parse(
    signing_key: str | bytes, raw_body: bytes, header_value: str | None
) -> WebhookEvent:
    if not verify_signature(signing_key, raw_body, header_value):
        raise InvalidSignature("X-Signature does not match the request body")
    return parse_event(raw_body)
