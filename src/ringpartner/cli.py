"""``ringpartner`` command-line interface.

The access token comes from ``--token`` or ``RING_ACCESS_TOKEN`` and is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

from .client import API_BASE, AsyncRingClient, RingClient
from .errors import RingError
from .fixtures import RecordingTransport
from .models import Device
from .simulator import build_event, deliver
from .webhooks import EVENT_TYPES, parse_event, verify_signature

TOKEN_ENV = "RING_ACCESS_TOKEN"


class CLIError(Exception):
    pass


def _token(args: argparse.Namespace) -> str:
    token = args.token or os.environ.get(TOKEN_ENV)
    if not token:
        raise CLIError(f"no access token: pass --token or set {TOKEN_ENV}")
    return token


def _client(args: argparse.Namespace, **kwargs: Any) -> RingClient:
    return RingClient(_token(args), base_url=args.base_url, **kwargs)


def _fmt(value: Any) -> str:
    return (
        "?"
        if value is None
        else ("yes" if value is True else "no" if value is False else str(value))
    )


def format_table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [
        max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)
    ]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=False))
    sep = "  ".join("-" * w for w in widths)
    body = ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=False)) for r in rows]
    return "\n".join([line, sep, *body])


def device_rows(devices: list[Device]) -> list[list[str]]:
    return [[d.id, d.name or "", _fmt(d.online), _fmt(d.is_encrypted)] for d in devices]


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_devices(args: argparse.Namespace) -> int:
    with _client(args) as ring:
        devices = ring.devices()
    if args.json:
        print(json.dumps([d.raw for d in devices], indent=2))
    else:
        print(format_table(device_rows(devices), ["id", "name", "online", "encrypted"]))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    types = [t.strip() for t in args.types.split(",") if t.strip()] if args.types else None
    rows = []
    with _client(args) as ring:
        for ev in ring.iter_history(args.device_id, types, max_pages=args.pages):
            rows.append(
                [
                    ev.start.isoformat() if ev.start else "?",
                    ev.event_type,
                    _fmt(ev.is_third_party_reviewed),
                    ev.id,
                ]
            )
    print(format_table(rows, ["start (UTC)", "type", "reviewed", "id"]))
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    with _client(args) as ring:
        if args.at_ms is not None:
            media = ring.snapshot(args.device_id, at=args.at_ms, fmt=args.format)
        else:
            now = int(time.time() * 1000)
            media = ring.snapshot(
                args.device_id, between=(now - args.window * 60_000, now), fmt=args.format
            )
    media.save(args.out)
    ts = media.timestamp.isoformat() if media.timestamp else "?"
    print(f"saved {len(media.content)} bytes to {args.out} (frame {ts}, origin {media.origin})")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    from .whep import WhepSession

    async def run() -> None:
        async with AsyncRingClient(_token(args), base_url=args.base_url) as ring:
            async with WhepSession(
                ring,
                args.device_id,
                audio=args.audio,
                component_id=args.component_id,
                max_seconds=args.seconds,
            ) as live:
                assert live.answer is not None
                print(
                    f"session open, codecs video={live.answer.video_codec} "
                    f"audio={live.answer.audio_codec}",
                    file=sys.stderr,
                )
                await live.record(args.out, args.seconds)

    asyncio.run(run())
    print(f"saved {args.out}")
    return 0


def cmd_capture_fixtures(args: argparse.Namespace) -> int:
    transport = RecordingTransport(args.dir)
    ok: list[str] = []
    failed: list[tuple[str, str]] = []

    def attempt(label: str, fn: Callable[[], Any]) -> Any:
        try:
            result = fn()
        except (RingError, KeyError, ValueError, OSError) as exc:
            failed.append((label, f"{type(exc).__name__}: {exc}"))
            return None
        ok.append(label)
        return result

    with _client(args, transport=transport) as ring:
        devices = attempt("devices", ring.devices) or []
        attempt("users/me", ring.me)
        for d in devices:
            attempt(f"device {d.id}", lambda d=d: ring.device(d.id))
            for rel in ("status", "capabilities", "configurations", "location"):
                attempt(f"{rel} {d.id}", lambda d=d, rel=rel: getattr(ring, rel)(d.id))
            attempt(f"history {d.id}", lambda d=d: ring.history(d.id))
        cams = [d for d in devices if d.is_chime is not True]
        if cams:
            now = int(time.time() * 1000)
            target = cams[0].id
            attempt(
                f"snapshot {target}",
                lambda: ring.snapshot(target, between=(now - 10 * 60_000, now)),
            )
    print(f"recorded {len(ok)} calls into {args.dir}")
    for label, err in failed:
        print(f"  FAILED {label}: {err}")
    return 0 if ok else 1


def cmd_simulate(args: argparse.Namespace) -> int:
    payload = build_event(
        args.event_type,
        args.device,
        sub_type=args.sub_type,
        account_id=args.account_id,
        component_ids=args.component_ids.split(",") if args.component_ids else None,
    )
    resp = deliver(args.url, payload, args.key, timeout=args.timeout)
    print(json.dumps(payload, indent=2))
    print(f"-> HTTP {resp.status_code} from {args.url}", file=sys.stderr)
    return 0 if resp.is_success else 1


def cmd_verify(args: argparse.Namespace) -> int:
    body = sys.stdin.buffer.read()
    if not verify_signature(args.key, body, args.signature):
        print("INVALID signature", file=sys.stderr)
        return 1
    ev = parse_event(body)
    print(
        f"valid: {ev.type} device={ev.device_id} sub_type={ev.sub_type} request_id={ev.request_id}"
    )
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--token", help=f"access token (default: ${TOKEN_ENV})")
    common.add_argument("--base-url", default=API_BASE, help=argparse.SUPPRESS)

    p = argparse.ArgumentParser(
        prog="ringpartner", description="Ring Partner API toolkit (unofficial)"
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("devices", parents=[common], help="list devices")
    s.add_argument("--json", action="store_true", help="print raw JSON:API resources")
    s.set_defaults(func=cmd_devices)

    s = sub.add_parser("history", parents=[common], help="device event history")
    s.add_argument("device_id")
    s.add_argument("--types", help="comma-separated event_types, e.g. motion.human,ding")
    s.add_argument("--pages", type=int, default=5)
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("snapshot", parents=[common], help="download a still image")
    s.add_argument("device_id")
    s.add_argument("--out", required=True)
    s.add_argument(
        "--at-ms", type=int, help="epoch ms (default: latest in the last --window minutes)"
    )
    s.add_argument("--window", type=int, default=10, help="minutes for latest_in_range")
    s.add_argument("--format", choices=["jpeg", "png"], default="jpeg")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("record", parents=[common], help="record live view via WHEP (needs [whep])")
    s.add_argument("device_id")
    s.add_argument("--out", required=True)
    s.add_argument("--seconds", type=float, default=25)
    s.add_argument("--audio", action="store_true")
    s.add_argument("--component-id")
    s.set_defaults(func=cmd_record)

    s = sub.add_parser(
        "capture-fixtures", parents=[common], help="record real responses for replay"
    )
    s.add_argument("--dir", required=True)
    s.set_defaults(func=cmd_capture_fixtures)

    s = sub.add_parser("simulate", help="deliver a signed v1.1 webhook to your endpoint")
    s.add_argument("event_type", choices=EVENT_TYPES)
    s.add_argument("--device", help="device id (required for device events)")
    s.add_argument("--url", required=True)
    s.add_argument("--key", required=True, help="HMAC signing key")
    s.add_argument("--sub-type", help="motion: motion|human|vehicle|package_delivery|other_motion")
    s.add_argument("--account-id", default="ava1.ring.account.SIMULATED")
    s.add_argument("--component-ids", help="comma-separated, multi-camera devices")
    s.add_argument("--timeout", type=float, default=5.0)
    s.set_defaults(func=cmd_simulate)

    s = sub.add_parser("verify", help="verify an X-Signature against a body read from stdin")
    s.add_argument("--key", required=True)
    s.add_argument("--signature", required=True)
    s.set_defaults(func=cmd_verify)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except CLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (RingError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
