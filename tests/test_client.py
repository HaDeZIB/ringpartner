from __future__ import annotations

import json

import httpx
import pytest
import respx
from conftest import API, CAM, CHIME, PRESIGNED, history_event, next_link

import ringpartner.client as client_mod
from ringpartner import AsyncRingClient, RingClient
from ringpartner.errors import (
    RingAPIError,
    RingDeviceUnavailable,
    RingForbidden,
    RingNotFound,
    RingRateLimited,
    RingUnauthorized,
)

TOKEN = "test-token-abc"


def err(status: int, title: str, detail: str = "") -> dict:
    return {"errors": [{"status": str(status), "title": title, "detail": detail}]}


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------


@respx.mock
def test_devices_parses_included(devices_doc):
    route = respx.get(f"{API}/v1/devices").mock(return_value=httpx.Response(200, json=devices_doc))
    with RingClient(TOKEN) as ring:
        cam, chime = ring.devices()

    req = route.calls.last.request
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.url.params["include"] == "status,capabilities,configurations,location"

    assert cam.id == CAM and cam.name == "Front Door"
    assert cam.online is True
    assert cam.capabilities["video"]["codecs"] == ["AVC"]
    assert cam.is_chime is False
    assert cam.is_encrypted is True
    assert cam.location == {"country": "US", "state": "WA"}
    assert cam.raw["id"] == CAM

    assert chime.id == CHIME
    assert chime.online is False
    assert chime.is_chime is True
    assert chime.is_encrypted is False


@respx.mock
def test_device_without_included_is_unknown():
    doc = {
        "data": {
            "type": "devices",
            "id": CAM,
            "attributes": {"name": "X"},
            "relationships": {"status": {"data": {"type": "device-status", "id": "s"}}},
        }
    }
    respx.get(f"{API}/v1/devices/{CAM}").mock(return_value=httpx.Response(200, json=doc))
    with RingClient(TOKEN) as ring:
        d = ring.device(CAM, include=None)
    assert d.online is None and d.is_chime is None and d.is_encrypted is None


@respx.mock
def test_token_callable_is_called_per_request(devices_doc):
    respx.get(f"{API}/v1/devices").mock(return_value=httpx.Response(200, json=devices_doc))
    tokens = iter(["t1", "t2"])
    with RingClient(lambda: next(tokens)) as ring:
        ring.devices()
        ring.devices()
    seen = [c.request.headers["authorization"] for c in respx.calls]
    assert seen == ["Bearer t1", "Bearer t2"]


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


@respx.mock
def test_history_event_types_param_and_pagination():
    url = f"{API}/v1/history/devices/{CAM}/events"
    page1 = {
        "data": [
            history_event("e1", "motion", 1_700_000_100_000),
            history_event("e2", "ding", 1_700_000_050_000),
        ],
        "links": next_link("2026-07-06T11:11:00Z"),
    }
    page2 = {
        "data": [history_event("e3", "motion", 1_700_000_000_000)],
        "links": next_link("2026-07-05T00:00:00Z"),
    }
    empty = {"data": [], "links": next_link("2026-07-04T00:00:00Z")}
    route = respx.get(url).mock(
        side_effect=[
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
            httpx.Response(200, json=empty),
        ]
    )
    with RingClient(TOKEN) as ring:
        events = list(ring.iter_history(CAM, ["motion.human", "ding"]))

    assert [e.id for e in events] == ["e1", "e2", "e3"]
    assert events[0].device_id == CAM
    assert events[0].start is not None and events[0].start.year == 2023
    assert route.call_count == 3  # stops on the empty page despite its next link

    params = [c.request.url.params for c in route.calls]
    for p in params:
        assert p["event_types"] == "motion.human,ding"
        assert "event_type" not in p and "filter[event_type]" not in p
    assert "page[key]" not in params[0]
    assert params[1]["page[key]"] == "2026-07-06T11:11:00Z"
    assert params[2]["page[key]"] == "2026-07-05T00:00:00Z"


@respx.mock
def test_history_empty_page_has_no_next_key():
    respx.get(f"{API}/v1/history/devices/{CAM}/events").mock(
        return_value=httpx.Response(200, json={"data": [], "links": next_link("x")})
    )
    with RingClient(TOKEN) as ring:
        page = ring.history(CAM)
    assert page.events == [] and page.next_key is None


@respx.mock
def test_history_stops_without_next_link():
    route = respx.get(f"{API}/v1/history/devices/{CAM}/events").mock(
        return_value=httpx.Response(200, json={"data": [history_event("e1", "ding", 1)]})
    )
    with RingClient(TOKEN) as ring:
        assert len(list(ring.iter_history(CAM))) == 1
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# media
# ---------------------------------------------------------------------------


@respx.mock
def test_snapshot_follows_303_without_authorization():
    post = respx.post(f"{API}/v1/devices/{CAM}/media/image/download").mock(
        return_value=httpx.Response(303, headers={"location": PRESIGNED})
    )
    get = respx.get(PRESIGNED).mock(
        return_value=httpx.Response(
            200,
            content=b"\xff\xd8JPEG",
            headers={
                "content-type": "image/jpeg",
                "x-media-timestamp": "1699457230000",
                "x-media-origin": "recording",
            },
        )
    )
    with RingClient(TOKEN) as ring:
        media = ring.snapshot(
            CAM, between=(1_699_450_000_000, 1_699_457_230_000), width=640, height=360
        )

    body = json.loads(post.calls.last.request.content)
    assert body == {
        "type": "latest_in_range",
        "start_timestamp": 1_699_450_000_000,
        "end_timestamp": 1_699_457_230_000,
        "image_options": {"format": "jpeg", "resolution": {"width": 640, "height": 360}},
    }
    assert post.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"
    presigned_req = get.calls.last.request
    assert "authorization" not in presigned_req.headers
    assert media.content == b"\xff\xd8JPEG"
    assert media.content_type == "image/jpeg"
    assert media.origin == "recording"
    assert media.timestamp is not None and media.timestamp.year == 2023


@respx.mock
def test_snapshot_at_timestamp_with_component():
    post = respx.post(f"{API}/v1/devices/{CAM}/media/image/download").mock(
        return_value=httpx.Response(303, headers={"location": PRESIGNED})
    )
    respx.get(PRESIGNED).mock(return_value=httpx.Response(200, content=b"x"))
    with RingClient(TOKEN) as ring:
        ring.snapshot(CAM, at=1_787_176_751_000, component_id=1)
    body = json.loads(post.calls.last.request.content)
    assert body["type"] == "at_timestamp" and body["timestamp"] == 1_787_176_751_000
    assert body["components"] == [{"component_id": "1"}]


def test_snapshot_requires_exactly_one_mode():
    with RingClient(TOKEN) as ring:
        with pytest.raises(ValueError):
            ring.snapshot(CAM)
        with pytest.raises(ValueError):
            ring.snapshot(CAM, at=1, between=(1, 2))


@respx.mock
def test_video_partial_content_206_is_success():
    respx.post(f"{API}/v1/devices/{CAM}/media/video/download").mock(
        return_value=httpx.Response(303, headers={"location": PRESIGNED})
    )
    respx.get(PRESIGNED).mock(
        return_value=httpx.Response(
            206,
            content=b"mp4",
            headers={
                "content-type": "video/mp4",
                "x-media-length": "3000",
                "x-media-timestamp": "1699457235000",
            },
        )
    )
    with RingClient(TOKEN) as ring:
        media = ring.video_clip(CAM, 1_699_457_230_000, 10_000)
    assert media.content == b"mp4" and media.length_ms == 3000 and media.partial


@respx.mock
def test_presigned_error_maps_and_hides_token():
    respx.post(f"{API}/v1/devices/{CAM}/media/image/download").mock(
        return_value=httpx.Response(303, headers={"location": PRESIGNED})
    )
    respx.get(PRESIGNED).mock(
        return_value=httpx.Response(503, json=err(503, "Service Unavailable", "media not ready"))
    )
    with RingClient(TOKEN) as ring, pytest.raises(RingDeviceUnavailable) as exc:
        ring.snapshot(CAM, at=1)
    assert "SECRET" not in str(exc.value)
    assert exc.value.detail == "media not ready"


# ---------------------------------------------------------------------------
# errors / retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "cls"),
    [
        (401, RingUnauthorized),
        (403, RingForbidden),
        (404, RingNotFound),
        (503, RingDeviceUnavailable),
        (500, RingAPIError),
    ],
)
@respx.mock
def test_error_mapping(status, cls):
    respx.get(f"{API}/v1/devices/{CAM}/status").mock(
        return_value=httpx.Response(status, json=err(status, "Nope", "because"))
    )
    with RingClient(TOKEN) as ring, pytest.raises(cls) as exc:
        ring.status(CAM)
    assert exc.value.status == status
    assert exc.value.title == "Nope" and exc.value.detail == "because"
    assert TOKEN not in str(exc.value)


@respx.mock
def test_error_without_json_body():
    respx.get(f"{API}/v1/users/me").mock(return_value=httpx.Response(502, text="<html>"))
    with RingClient(TOKEN) as ring, pytest.raises(RingAPIError) as exc:
        ring.me()
    assert exc.value.status == 502


@respx.mock
def test_429_retries_honoring_retry_after(monkeypatch, devices_doc):
    sleeps: list[float] = []
    monkeypatch.setattr(client_mod.time, "sleep", sleeps.append)
    route = respx.get(f"{API}/v1/devices").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "2"}, json=err(429, "Too Many Requests")),
            httpx.Response(429, json=err(429, "Too Many Requests")),
            httpx.Response(200, json=devices_doc),
        ]
    )
    with RingClient(TOKEN) as ring:
        assert len(ring.devices()) == 2
    assert route.call_count == 3
    assert sleeps == [2.0, 2.0]  # Retry-After first, then exponential (2**1)


@respx.mock
def test_429_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    respx.get(f"{API}/v1/devices").mock(
        return_value=httpx.Response(429, headers={"retry-after": "1"})
    )
    with RingClient(TOKEN, max_retries=2) as ring, pytest.raises(RingRateLimited) as exc:
        ring.devices()
    assert exc.value.retry_after == 1.0
    assert respx.calls.call_count == 3


# ---------------------------------------------------------------------------
# async parity
# ---------------------------------------------------------------------------


@respx.mock
async def test_async_devices_and_history(devices_doc):
    respx.get(f"{API}/v1/devices").mock(return_value=httpx.Response(200, json=devices_doc))
    respx.get(f"{API}/v1/history/devices/{CAM}/events").mock(
        side_effect=[
            httpx.Response(
                200, json={"data": [history_event("e1", "motion", 1)], "links": next_link("k1")}
            ),
            httpx.Response(200, json={"data": [], "links": next_link("k2")}),
        ]
    )

    async def token() -> str:
        return TOKEN

    async with AsyncRingClient(token) as ring:
        devices = await ring.devices()
        events = [e async for e in ring.iter_history(CAM, ["motion.human"])]
    assert [d.is_chime for d in devices] == [False, True]
    assert [e.id for e in events] == ["e1"]
    assert respx.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"


@respx.mock
async def test_async_snapshot_and_retry(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr(client_mod.asyncio, "sleep", fake_sleep)
    respx.post(f"{API}/v1/devices/{CAM}/media/image/download").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "0.5"}),
            httpx.Response(303, headers={"location": PRESIGNED}),
        ]
    )
    get = respx.get(PRESIGNED).mock(return_value=httpx.Response(200, content=b"img"))
    async with AsyncRingClient(TOKEN) as ring:
        media = await ring.snapshot(CAM, at=1)
    assert media.content == b"img"
    assert sleeps == [0.5]
    assert "authorization" not in get.calls.last.request.headers


@respx.mock
async def test_async_error_mapping():
    respx.get(f"{API}/v1/users/me").mock(return_value=httpx.Response(401, json=err(401, "x")))
    async with AsyncRingClient(TOKEN) as ring:
        with pytest.raises(RingUnauthorized):
            await ring.me()
