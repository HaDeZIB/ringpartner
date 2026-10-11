"""Shared fixtures: documents shaped like the examples in Ring's Partner API docs."""

from __future__ import annotations

import pytest

API = "https://api.amazonvision.com"
CAM = "ava1.ring.device.CAM1"
CHIME = "ava1.ring.device.CHIME1"
PRESIGNED = (
    "https://download-us-east-1.example-cdn.com/v1/download"
    "?security_token=SECRET&req_type=image_download&req_id=r1&X-Amz-Expires=60"
)


def _rel(device_id: str, rel: str, type_: str) -> dict:
    short = rel if rel != "location" else "location"
    return {
        "data": {"type": type_, "id": f"ava1.ring.device.{short}.{device_id}"},
        "links": {"related": f"/v1/devices/{device_id}/{rel}"},
    }


def device_resource(device_id: str, name: str) -> dict:
    return {
        "type": "devices",
        "id": device_id,
        "attributes": {"name": name, "image_url": "https://example.com/x.png"},
        "relationships": {
            "status": _rel(device_id, "status", "device-status"),
            "capabilities": _rel(device_id, "capabilities", "device-capabilities"),
            "configurations": _rel(device_id, "configurations", "device-configurations"),
            "location": _rel(device_id, "location", "locations"),
        },
    }


def included_for(device_id: str, *, online: bool, chime: bool, e2e: bool) -> list[dict]:
    if chime:
        caps = {
            "video": {"configurations": None, "codecs": None},
            "motion_detection": {"configurations": None},
            "audio": {"customizable_slots": 2, "supported_actions": ["chime.play"]},
        }
    else:
        caps = {
            "video": {"codecs": ["AVC"], "max_resolution": 1080},
            "motion_detection": {"configurations": ["enabled", "motion_zones"]},
        }
    return [
        {
            "type": "device-status",
            "id": f"ava1.ring.device.status.{device_id}",
            "attributes": {"online": online},
        },
        {
            "type": "device-capabilities",
            "id": f"ava1.ring.device.capabilities.{device_id}",
            "attributes": caps,
        },
        {
            "type": "device-configurations",
            "id": f"ava1.ring.device.configurations.{device_id}",
            "attributes": {
                "e2e_encryption": {"enabled": e2e},
                "motion_detection": {"enabled": "on"},
            },
        },
        {
            "type": "locations",
            "id": f"ava1.ring.device.location.{device_id}",
            "attributes": {"country": "US", "state": "WA"},
        },
    ]


@pytest.fixture
def devices_doc() -> dict:
    return {
        "meta": {"time": "2026-02-14T22:57:16Z"},
        "data": [device_resource(CAM, "Front Door"), device_resource(CHIME, "Hallway Chime")],
        "included": included_for(CAM, online=True, chime=False, e2e=True)
        + included_for(CHIME, online=False, chime=True, e2e=False),
    }


def history_event(event_id: str, event_type: str, start: int, device_id: str = CAM) -> dict:
    return {
        "type": "history-events",
        "id": event_id,
        "attributes": {
            "event_type": event_type,
            "is_third_party_reviewed": False,
            "start": start,
            "end": start + 25_000,
        },
        "relationships": {"source": {"data": {"type": "devices", "id": device_id}}},
    }


def next_link(key: str, device_id: str = CAM) -> dict:
    return {"next": f"/v1/history/devices/{device_id}/events?page[key]={key}"}
