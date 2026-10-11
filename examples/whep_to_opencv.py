"""Pull live Ring video frames over WHEP (server-side) and process them with OpenCV.

Needs the ``whep`` extra plus OpenCV:

    pip install 'ringpartner[whep]' opencv-python-headless
    export RING_ACCESS_TOKEN=...          # e.g. from the Ring Developer Playground
    python whep_to_opencv.py --seconds 20 --fps 2 --out frames/

For each frame it computes a cheap motion score (mean absolute difference on a small grayscale
thumbnail, the same gate DoorLedger uses to skip unchanged frames) and saves the frames that
changed as JPEGs. Swap ``on_frame`` for your own model.

Notes
-----
* Ring caps live sessions at about 30 s (battery) or 60 s (line-powered). Loop with a pause for
  longer watches: re-opening within about 20 s can return HTTP 500.
* The codec is read from the SDP answer (``live.answer.video_codec``), not from capabilities.
* In the Playground, the scene (Package / Vehicle / Motion) is whatever was last clicked in the
  web UI.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import cv2
import numpy as np

from ringpartner import AsyncRingClient
from ringpartner.whep import WhepSession


class MotionGate:
    def __init__(self, threshold: float = 2.5) -> None:
        self.threshold = threshold
        self.prev: np.ndarray | None = None

    def score(self, bgr: np.ndarray) -> float:
        thumb = cv2.cvtColor(cv2.resize(bgr, (64, 36)), cv2.COLOR_BGR2GRAY).astype(np.int16)
        prev, self.prev = self.prev, thumb
        return float("inf") if prev is None else float(np.abs(thumb - prev).mean())


async def run(token: str, device_id: str | None, seconds: float, fps: float, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    gate = MotionGate()
    async with AsyncRingClient(token) as ring:
        if device_id is None:
            devices = [d for d in await ring.devices() if not d.is_chime]
            if not devices:
                raise SystemExit("no camera devices on this account")
            device = devices[0]
            if device.is_encrypted:
                raise SystemExit(f"{device.name} has end-to-end encryption on; video is unreadable")
            device_id = device.id
        async with WhepSession(ring, device_id, max_seconds=seconds) as live:
            print(f"live view open, codec {live.answer.video_codec}")
            n = saved = 0
            async for frame in live.frames(fps=fps):
                bgr = frame.to_ndarray(format="bgr24")  # numpy (H, W, 3), ready for OpenCV
                n += 1
                if on_frame(bgr, gate):
                    saved += 1
                    cv2.imwrite(str(out / f"frame_{n:04d}.jpg"), bgr)
            print(f"{n} frames received, {saved} changed frames saved to {out}/")


def on_frame(bgr: np.ndarray, gate: MotionGate) -> bool:
    """Return True if the frame is worth keeping (or sending to a vision model)."""
    s = gate.score(bgr)
    h, w = bgr.shape[:2]
    print(f"  {w}x{h}  motion={s:6.2f}{'  <- changed' if s >= gate.threshold else ''}")
    return s >= gate.threshold


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--device", help="device id (default: first non-chime device)")
    ap.add_argument("--seconds", type=float, default=20.0, help="session length (<= 27 is safe)")
    ap.add_argument("--fps", type=float, default=2.0, help="frames per second to sample")
    ap.add_argument("--out", type=Path, default=Path("frames"), help="output directory")
    args = ap.parse_args()
    token = os.environ.get("RING_ACCESS_TOKEN")
    if not token:
        print("set RING_ACCESS_TOKEN (a Developer Playground token works)", file=sys.stderr)
        return 2
    asyncio.run(run(token, args.device, args.seconds, args.fps, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
