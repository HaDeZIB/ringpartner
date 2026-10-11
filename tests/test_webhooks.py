from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid

import httpx
import pytest

from ringpartner import (
    InvalidSignature,
    build_event,
    deliver,
    parse_event,
    verify_and_parse,
    verify_signature,
)
from ringpartner.simulator import encode, signed_request
from ringpartner.webhooks import EVENT_TYPES, sign

KEY = "partner-hmac-key"
DEV = "ava1.ring.device.CAM1"


def test_sign_matches_reference_hmac():
    body = b'{"a":1}'
    expected = hmac.new(KEY.encode(), body, hashlib.sha256).hexdigest()
    assert sign(KEY, body) == f"sha256={expected}"


def test_verify_good_signature():
    body = encode(build_event("button_press", DEV))
    assert verify_signature(KEY, body, sign(KEY, body))


def test_verify_tampered_body_fails():
    body = encode(build_event("motion_detected", DEV, sub_type="human"))
    header = sign(KEY, body)
    tampered = body.replace(b"human", b"vehicle")
    assert not verify_signature(KEY, tampered, header)
    with pytest.raises(InvalidSignature):
        verify_and_parse(KEY, tampered, header)


def test_verify_wrong_key_fails():
    body = b"{}"
    assert not verify_signature("other", body, sign(KEY, body))


def test_missing_prefix_is_accepted_like_reference_verifier():
    # Ring's reference code does received.removeprefix("sha256="), so bare hex verifies.
    body = b'{"x":true}'
    bare = sign(KEY, body).removeprefix("sha256=")
    assert verify_signature(KEY, body, bare)
    assert verify_signature(KEY, body, bare.upper())


def test_other_prefix_and_empty_header_rejected():
    body = b"{}"
    bare = sign(KEY, body).removeprefix("sha256=")
    assert not verify_signature(KEY, body, f"sha1={bare}")
    assert not verify_signature(KEY, body, None)
    assert not verify_signature(KEY, body, "")


def test_verify_and_parse_motion():
    payload = build_event(
        "motion_detected",
        DEV,
        sub_type="human",
        component_ids=["0"],
        request_id="req-1",
        account_id="ava1.ring.account.A",
    )
    body, headers = signed_request(payload, KEY)
    ev = verify_and_parse(KEY, body, headers["X-Signature"])
    assert ev.type == "motion_detected"
    assert ev.device_id == DEV and ev.device_ids == [DEV]
    assert ev.sub_type == "human"
    assert ev.component_ids == ["0"]
    assert ev.account_id == "ava1.ring.account.A"
    assert ev.request_id == "req-1" and ev.dedupe_key == "req-1"
    assert ev.version == "1.1"
    assert ev.timestamp is not None


def test_parse_documented_example_verbatim():
    doc = {
        "meta": {
            "version": "1.1",
            "time": "2026-02-14T00:02:53.027052438Z",
            "request_id": "c83081fc-2f1b-4cb7-8b18-b18a318b8bab",
            "account_id": "ava1.ring.account.XXXYYY",
        },
        "data": {
            "id": f"{DEV}_button_press_1771027372393",
            "type": "button_press",
            "attributes": {"source": DEV, "source_type": "devices", "timestamp": 1771027372393},
            "relationships": {"devices": {"links": {"self": f"/v1/devices/{DEV}"}}},
        },
    }
    ev = parse_event(json.dumps(doc))
    assert ev.device_id == DEV and ev.timestamp.year == 2026


def test_parse_subscription_and_integration_have_no_fake_device_id():
    sub = parse_event(build_event("subscription_activated", device_ids=[DEV, "d2"]))
    assert sub.device_id is None and sub.device_ids == [DEV, "d2"]
    assert sub.sub_type == "paid"
    integ = parse_event(build_event("app_integration_added", account_id="ava1.ring.account.A"))
    assert integ.device_id is None and integ.device_ids == []


# ---------------------------------------------------------------------------
# simulator payload shape (v1.1)
# ---------------------------------------------------------------------------


def _assert_meta(meta: dict) -> None:
    assert meta["version"] == "1.1"
    uuid.UUID(meta["request_id"])
    assert meta["account_id"].startswith("ava1.ring.account.")
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", meta["time"])
    assert meta["time"].endswith("Z")


def test_simulator_motion_shape():
    p = build_event(
        "motion_detected", DEV, sub_type="human", timestamp_ms=1786715596787, component_ids=[0]
    )
    _assert_meta(p["meta"])
    assert set(p) == {"meta", "data"}
    d = p["data"]
    assert d["id"] == f"{DEV}_human_1786715596787"
    assert d["type"] == "motion_detected"
    a = d["attributes"]
    assert a["source"] == DEV and a["source_type"] == "devices"
    assert a["timestamp"] == 1786715596787
    # Docs render it without an offset (their example is local time); the simulator uses UTC.
    assert a["timestamp_readable"] == "2026-08-14 13:53:16"
    assert a["sub_type"] == "human"
    assert a["component_ids"] == ["0"]
    assert d["relationships"] == {"devices": {"links": {"self": f"/v1/devices/{DEV}"}}}


def test_simulator_motion_default_and_open_vocabulary():
    assert build_event("motion_detected", DEV, timestamp_ms=1)["data"]["id"] == f"{DEV}_motion_1"
    p = build_event("motion_detected", DEV, sub_type="animal", timestamp_ms=1)
    assert p["data"]["attributes"]["sub_type"] == "animal"


def test_simulator_button_press_shape():
    p = build_event("button_press", DEV, timestamp_ms=1771027372393)
    _assert_meta(p["meta"])
    d = p["data"]
    assert d["id"] == f"{DEV}_button_press_1771027372393"
    assert d["attributes"] == {"source": DEV, "source_type": "devices", "timestamp": 1771027372393}


@pytest.mark.parametrize(
    "etype", ["device_added", "device_removed", "device_online", "device_offline"]
)
def test_simulator_device_lifecycle_ids(etype):
    p = build_event(etype, DEV, timestamp_ms=5)
    assert p["data"]["id"] == f"{DEV}_{etype}_5"


def test_simulator_integration_shape():
    p = build_event("app_integration_removed", account_id="ava1.ring.account.A", timestamp_ms=9)
    assert "relationships" not in p["data"]
    assert p["data"]["attributes"] == {
        "source": "ava1.ring.account.A",
        "source_type": "users",
        "timestamp": 9,
    }


def test_simulator_subscription_shapes():
    act = build_event("subscription_activated", DEV, sub_type="trial")
    a = act["data"]["attributes"]
    assert a["source"] == "subscriptions" and a["sub_type"] == "trial" and a["plan_id"] is None
    assert "expires_at" in a
    assert act["data"]["relationships"]["devices"]["data"] == [{"type": "device", "id": DEV}]
    deact = build_event("subscription_deactivated", DEV)
    assert "expires_at" not in deact["data"]["attributes"]
    assert deact["data"]["attributes"]["plan_id"]


def test_simulator_rejects_unknown_and_missing_device():
    with pytest.raises(ValueError):
        build_event("doorbell_rang", DEV)
    with pytest.raises(ValueError):
        build_event("button_press")
    assert len(EVENT_TYPES) == 10


def test_deliver_posts_signed_raw_body():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["sig"] = request.headers["x-signature"]
        captured["ct"] = request.headers["content-type"]
        return httpx.Response(200)

    payload = build_event("motion_detected", DEV, sub_type="vehicle")
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        resp = deliver("https://partner.example/webhooks/ring", payload, KEY, client=c)
    assert resp.status_code == 200
    assert captured["ct"] == "application/json"
    assert verify_signature(KEY, captured["body"], captured["sig"])
    assert json.loads(captured["body"]) == payload
