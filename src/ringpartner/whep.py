"""Server-side WHEP live video for Ring devices (requires the ``whep`` extra: aiortc + PyAV).

Ring's reference implementation is browser JavaScript. This module does the same from Python,
which is what you need for computer vision pipelines:

* every transceiver is ``recvonly`` (Ring rejects sendrecv/sendonly);
* the offer is POSTed only after ICE gathering completes (aiortc gathers inside
  ``setLocalDescription``) — sending early is the usual cause of an undocumented HTTP 500;
* the codec is read from the SDP *answer*, never from device capabilities;
* the session is closed with ``DELETE <Location>`` and proactively before Ring's cap
  (30 s battery, 60 s line-powered).

Example::

    async with AsyncRingClient(token) as ring:
        async with WhepSession(ring, device_id, max_seconds=25) as live:
            async for frame in live.frames(fps=2):
                image = frame.to_ndarray(format="bgr24")
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from .client import AsyncRingClient, _error_from
from .errors import RingError

if TYPE_CHECKING:  # pragma: no cover
    import av

DEFAULT_ICE = [{"urls": "stun:stun.l.google.com:19302"}]
BATTERY_LIMIT_S = 30
LINE_POWERED_LIMIT_S = 60


class WhepError(RingError):
    pass


@dataclass
class WhepAnswer:
    sdp: str
    session_url: str
    video_codec: str | None
    audio_codec: str | None


def codecs_from_sdp(sdp: str) -> dict[str, str | None]:
    """Return the first negotiated codec per media section, e.g. {'video': 'H264/90000'}."""
    result: dict[str, str | None] = {"video": None, "audio": None}
    current: str | None = None
    for line in sdp.splitlines():
        if line.startswith("m="):
            parts = line[2:].split()
            # Port 0 means the answerer rejected this section (e.g. no microphone).
            current = parts[0] if len(parts) > 1 and parts[1] != "0" else None
            continue
        m = re.match(r"a=rtpmap:\d+ (\S+)", line)
        if m and current in result and result[current] is None:
            result[current] = m.group(1)
    return result


class WhepSession:
    def __init__(
        self,
        ring: AsyncRingClient,
        device_id: str,
        *,
        audio: bool = False,
        component_id: str | None = None,
        max_seconds: float = BATTERY_LIMIT_S - 3,
        ice_servers: list[dict[str, Any]] | None = None,
        connect_timeout: float = 15.0,
    ) -> None:
        try:
            from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise WhepError("install the whep extra: pip install 'ringpartner[whep]'") from exc
        self.ring = ring
        self.device_id = device_id
        self.audio = audio
        self.component_id = component_id
        self.max_seconds = max_seconds
        self.ice_servers = ice_servers or DEFAULT_ICE
        self.connect_timeout = connect_timeout
        self.answer: WhepAnswer | None = None
        self.started_at: float | None = None
        self._pc: Any = None
        self._video_track: asyncio.Future[Any] | None = None
        self._audio_track: asyncio.Future[Any] | None = None
        self._session_open = False

    # -- lifecycle ---------------------------------------------------------------

    async def __aenter__(self) -> WhepSession:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def start(self) -> WhepAnswer:
        from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

        loop = asyncio.get_running_loop()
        self._video_track = loop.create_future()
        self._audio_track = loop.create_future()
        config = RTCConfiguration(iceServers=[RTCIceServer(**s) for s in self.ice_servers])
        pc = RTCPeerConnection(configuration=config)
        self._pc = pc

        # Handlers must exist before createOffer / setRemoteDescription.
        @pc.on("track")
        def _on_track(track: Any) -> None:
            fut = self._video_track if track.kind == "video" else self._audio_track
            if fut is not None and not fut.done():
                fut.set_result(track)

        pc.addTransceiver("video", direction="recvonly")
        if self.audio:
            pc.addTransceiver("audio", direction="recvonly")

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)  # aiortc completes ICE gathering here

        path = f"/v1/devices/{self.device_id}/media/streaming/whep/sessions"
        params = {"component_id": self.component_id} if self.component_id is not None else None
        token = await self.ring.access_token()
        async with httpx.AsyncClient(base_url=self.ring.base_url, timeout=20) as http:
            resp = await http.post(
                path,
                params=params,
                content=pc.localDescription.sdp.encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/sdp"},
            )
        if resp.status_code != 201:
            await pc.close()
            raise _error_from(resp)
        location = resp.headers.get("location")
        if not location:
            await pc.close()
            raise WhepError("WHEP answer missing Location header")
        if location.startswith("/"):
            location = self.ring.base_url.rstrip("/") + location

        codecs = codecs_from_sdp(resp.text)
        # Set before applying the answer so close() can DELETE the Ring session on failure.
        self.answer = WhepAnswer(resp.text, location, codecs["video"], codecs["audio"])
        self._session_open = True
        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=resp.text, type="answer"))
        except Exception:
            await self.close()
            raise
        self.started_at = time.monotonic()
        return self.answer

    async def close(self) -> None:
        if self.answer is not None and self._session_open:
            self._session_open = False
            token = await self.ring.access_token()
            try:
                async with httpx.AsyncClient(timeout=10) as http:
                    await http.delete(
                        self.answer.session_url, headers={"Authorization": f"Bearer {token}"}
                    )
            except httpx.HTTPError:
                pass  # Ring also tears the session down at its cap
        if self._pc is not None:
            await self._pc.close()
            self._pc = None

    # -- media -------------------------------------------------------------------

    @property
    def elapsed(self) -> float:
        return 0.0 if self.started_at is None else time.monotonic() - self.started_at

    async def video_track(self) -> Any:
        assert self._video_track is not None, "call start() first"
        return await asyncio.wait_for(self._video_track, self.connect_timeout)

    async def frames(self, fps: float | None = None) -> AsyncIterator[av.VideoFrame]:
        """Yield decoded frames until ``max_seconds``; ``fps`` down-samples for CV workloads."""
        from aiortc.mediastreams import MediaStreamError

        track = await self.video_track()
        min_gap = 1.0 / fps if fps else 0.0
        last = -1e9
        while self.elapsed < self.max_seconds:
            try:
                frame = await asyncio.wait_for(track.recv(), timeout=5)
            except (asyncio.TimeoutError, MediaStreamError):
                return
            now = time.monotonic()
            if now - last >= min_gap:
                last = now
                yield frame

    async def record(self, path: str, seconds: float | None = None) -> str:
        """Record the live view (video, plus audio if requested) to a file such as ``.mp4``."""
        from aiortc.contrib.media import MediaRecorder

        recorder = MediaRecorder(path)
        recorder.addTrack(await self.video_track())
        if self.audio and self._audio_track is not None:
            try:
                recorder.addTrack(await asyncio.wait_for(self._audio_track, 3))
            except asyncio.TimeoutError:
                pass
        await recorder.start()
        try:
            remaining = max(0.0, self.max_seconds - self.elapsed)
            await asyncio.sleep(min(seconds or self.max_seconds, remaining))
        finally:
            await recorder.stop()
        return path
