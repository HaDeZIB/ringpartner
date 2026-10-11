from __future__ import annotations

import json

import httpx
from conftest import API, CAM, PRESIGNED, history_event

from ringpartner import (
    AsyncReplayTransport,
    AsyncRingClient,
    RecordingTransport,
    ReplayTransport,
    RingClient,
)
from ringpartner.whep import codecs_from_sdp


def _fake_ring(devices_doc: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "download-us-east-1.example-cdn.com":
            assert "authorization" not in request.headers
            return httpx.Response(
                200,
                content=b"\xff\xd8IMG",
                headers={
                    "content-type": "image/jpeg",
                    "x-media-timestamp": "1699457230000",
                    "x-media-origin": "snapshot",
                },
            )
        assert request.headers["authorization"] == "Bearer live-token"
        if path == "/v1/devices":
            return httpx.Response(
                200, json=devices_doc, headers={"content-type": "application/vnd.api+json"}
            )
        if path == f"/v1/history/devices/{CAM}/events":
            return httpx.Response(
                200,
                json={"data": [history_event("e1", "ding", 1)]},
                headers={"content-type": "application/json"},
            )
        if path == "/v1/users/me":
            return httpx.Response(
                200, json={"data": {"type": "users", "id": "ava1.ring.account.A"}}
            )
        if path.endswith("/media/image/download"):
            return httpx.Response(303, headers={"location": PRESIGNED})
        return httpx.Response(404, json={"errors": [{"status": "404", "title": "Not Found"}]})

    return httpx.MockTransport(handler)


def test_record_then_replay_round_trip(tmp_path, devices_doc):
    rec = RecordingTransport(tmp_path, inner=_fake_ring(devices_doc))
    with RingClient("live-token", transport=rec) as ring:
        live_devices = ring.devices()
        ring.history(CAM)
        ring.me()
        live_img = ring.snapshot(CAM, at=1_699_457_230_000)

    raw = (tmp_path / "index.json").read_text()
    assert "live-token" not in raw and "SECRET" not in raw
    for f in (tmp_path / "responses").iterdir():
        assert b"live-token" not in f.read_bytes() and b"SECRET" not in f.read_bytes()
    index = json.loads(raw)
    assert any(
        e["headers"].get("location", "").startswith("https://media.replay.invalid/")
        for e in index
        if e.get("headers")
    )

    with RingClient("anything", transport=ReplayTransport(tmp_path)) as ring:
        devices = ring.devices()
        assert [d.id for d in devices] == [d.id for d in live_devices]
        assert devices[0].online is True and devices[1].is_chime is True
        assert ring.history(CAM).events[0].id == "e1"
        assert ring.me().account_id == "ava1.ring.account.A"
        img = ring.snapshot(CAM, at=1_699_457_230_000)
    assert img.content == live_img.content == b"\xff\xd8IMG"
    assert img.origin == "snapshot" and img.timestamp == live_img.timestamp


async def test_async_replay(tmp_path, devices_doc):
    rec = RecordingTransport(tmp_path, inner=_fake_ring(devices_doc))
    with RingClient("live-token", transport=rec) as ring:
        ring.devices()
    async with AsyncRingClient("x", transport=AsyncReplayTransport(tmp_path)) as ring:
        assert len(await ring.devices()) == 2


def test_replay_missing_fixture_is_404(tmp_path, devices_doc):
    rec = RecordingTransport(tmp_path, inner=_fake_ring(devices_doc))
    with RingClient("live-token", transport=rec) as ring:
        ring.devices()
    import pytest

    from ringpartner.errors import RingNotFound

    with RingClient("x", transport=ReplayTransport(tmp_path)) as ring:
        with pytest.raises(RingNotFound):
            ring.location(CAM)


def test_recorder_ignores_other_hosts(tmp_path):
    inner = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    rec = RecordingTransport(tmp_path, inner=inner)
    with httpx.Client(transport=rec) as c:
        c.get("https://example.com/v1/devices")
        c.get(f"{API}/v1/devices")
    keys = [e["key"] for e in json.loads((tmp_path / "index.json").read_text())]
    assert keys == ["GET /v1/devices"]


ANSWER = """v=0
o=- 0 0 IN IP4 127.0.0.1
s=-
t=0 0
m=video 9 UDP/TLS/RTP/SAVPF 96 97
a=mid:video
a=sendonly
a=rtpmap:96 H264/90000
a=rtpmap:97 rtx/90000
m=audio 9 UDP/TLS/RTP/SAVPF 111
a=mid:audio
a=sendonly
a=rtpmap:111 opus/48000/2
"""


def test_codecs_from_sdp():
    assert codecs_from_sdp(ANSWER) == {"video": "H264/90000", "audio": "opus/48000/2"}


def test_codecs_from_sdp_video_only_and_crlf():
    video_only = ANSWER.split("m=audio")[0].replace("\n", "\r\n")
    assert codecs_from_sdp(video_only) == {"video": "H264/90000", "audio": None}


def test_codecs_from_sdp_rejected_section():
    rejected = ANSWER.replace("m=audio 9", "m=audio 0")
    assert codecs_from_sdp(rejected)["audio"] is None


def test_whep_not_imported_at_package_import():
    import subprocess
    import sys

    code = (
        "import sys, ringpartner; print('aiortc' in sys.modules, 'ringpartner.whep' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False False"
