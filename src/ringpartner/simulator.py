"""Signed webhook simulator.

The Ring Developer Playground simulates live-view sessions but does not deliver webhooks,
so there is no way to exercise a motion/button/device-lifecycle handler end to end without
real hardware. This module builds payloads that match the documented v1.1 format byte for
byte in structure and signs them with your HMAC key, so your production handler — including
signature verification and request_id de-duplication — runs unchanged.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from .webhooks import EVENT_TYPES, SIGNATURE_HEADER, sign

_DEVICE_EVENTS = (
    "motion_detected",
    "button_press",
    "device_added",
    "device_removed",
    "device_online",
    "device_offline",
)
_INTEGRATION_EVENTS = ("app_integration_added", "app_integration_removed")
_SUBSCRIPTION_EVENTS = ("subscription_activated", "subscription_deactivated")


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def build_event(
    event_type: str,
    device_id: str | None = None,
    *,
    account_id: str = "ava1.ring.account.SIMULATED",
    sub_type: str | None = None,
    timestamp_ms: int | None = None,
    component_ids: list[str] | None = None,
    request_id: str | None = None,
    extra_attributes: dict[str, Any] | None = None,
    subscription_id: str = "ava1.ring.subscription.SIMULATED",
    plan_id: str | None = None,
    expires_at: str | None = None,
    device_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a v1.1 webhook payload shaped exactly like the documented examples.

    Three documented shapes exist:

    * device events (motion, button, device lifecycle): ``data.id`` is
      ``<device_id>_<segment>_<timestamp>`` where the segment is the motion ``sub_type`` for
      ``motion_detected`` and the event type otherwise (``<device>_button_press_<ts>``);
    * ``app_integration_*``: ``source`` is the account id, ``source_type`` is ``users`` and
      there is no ``relationships`` block;
    * ``subscription_*``: ``source`` is ``"subscriptions"`` with ``source_id``/``plan_id``,
      and covered devices under ``relationships.devices.data``.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event_type!r}; expected one of {EVENT_TYPES}")
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    meta = {
        "version": "1.1",
        "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "request_id": request_id or str(uuid.uuid4()),
        "account_id": account_id,
    }

    if event_type in _INTEGRATION_EVENTS:
        attributes: dict[str, Any] = {"source": account_id, "source_type": "users", "timestamp": ts}
        if extra_attributes:
            attributes.update(extra_attributes)
        data: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "type": event_type,
            "attributes": attributes,
        }
        return {"meta": meta, "data": data}

    if event_type in _SUBSCRIPTION_EVENTS:
        sub = sub_type or "paid"
        covered = list(device_ids or ([device_id] if device_id else []))
        attributes = {
            "source": "subscriptions",
            "source_id": subscription_id,
            "sub_type": sub,
            "plan_id": plan_id if plan_id is not None or sub == "trial" else str(uuid.uuid4()),
        }
        if event_type == "subscription_activated":
            attributes["expires_at"] = expires_at or _iso(ts + 30 * 86_400_000)
        if extra_attributes:
            attributes.update(extra_attributes)
        return {
            "meta": meta,
            "data": {
                "id": str(uuid.uuid4()),
                "type": event_type,
                "attributes": attributes,
                "relationships": {
                    "devices": {"data": [{"type": "device", "id": d} for d in covered]},
                    "subscriptions": {"links": {"self": "/v1/accounts/me/subscriptions"}},
                },
            },
        }

    if not device_id:
        raise ValueError(f"{event_type} needs a device_id")
    attributes = {"source": device_id, "source_type": "devices", "timestamp": ts}
    segment = event_type
    if event_type == "motion_detected":
        sub = sub_type or "motion"  # open vocabulary: any string is passed through
        attributes["timestamp_readable"] = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        attributes["sub_type"] = sub
        segment = sub
    if component_ids:
        attributes["component_ids"] = [str(c) for c in component_ids]
    if extra_attributes:
        attributes.update(extra_attributes)

    return {
        "meta": meta,
        "data": {
            "id": f"{device_id}_{segment}_{ts}",
            "type": event_type,
            "attributes": attributes,
            "relationships": {"devices": {"links": {"self": f"/v1/devices/{device_id}"}}},
        },
    }


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def signed_request(
    payload: dict[str, Any], signing_key: str | bytes
) -> tuple[bytes, dict[str, str]]:
    body = encode(payload)
    return body, {SIGNATURE_HEADER: sign(signing_key, body), "Content-Type": "application/json"}


def deliver(
    url: str,
    payload: dict[str, Any],
    signing_key: str | bytes,
    *,
    timeout: float = 5.0,
    client: httpx.Client | None = None,
) -> httpx.Response:
    """POST a signed payload. Ring treats >5 s as a timeout, so the default mirrors that."""
    body, headers = signed_request(payload, signing_key)
    if client is not None:
        return client.post(url, content=body, headers=headers, timeout=timeout)
    with httpx.Client() as c:
        return c.post(url, content=body, headers=headers, timeout=timeout)
