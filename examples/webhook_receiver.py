"""Verify Ring webhooks with only the standard library's http.server, and test it with the
signed webhook simulator (the Developer Playground does not deliver webhooks).

Self-test: start the receiver in a background thread, then send one valid, one duplicate and
one tampered webhook. No token and no network beyond localhost are needed:

    python webhook_receiver.py --self-test

Run the receiver on its own, then send events from another terminal:

    export RING_HMAC_KEY=dev-secret
    python webhook_receiver.py --port 8000
    ringpartner simulate motion_detected --device DEVICE_ID --sub-type human \
        --url http://localhost:8000/webhooks/ring --key "$RING_HMAC_KEY"

Rules this follows (from the Ring webhook docs):

* verify ``X-Signature: sha256=<hex>`` (HMAC-SHA256) over the **raw** body, before parsing;
* dedupe on ``meta.request_id`` (Ring retries deliveries);
* answer within 5 s and do slow work elsewhere;
* treat ``sub_type`` as an open vocabulary.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ringpartner import (
    SIGNATURE_HEADER,
    InvalidSignature,
    build_event,
    deliver,
    verify_and_parse,
)

SEEN: OrderedDict[str, None] = OrderedDict()  # bounded dedupe cache
SEEN_MAX = 10_000


def make_handler(signing_key: str):
    class RingWebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (http.server API)
            if self.path != "/webhooks/ring":
                return self._reply(404, {"error": "not found"})
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                event = verify_and_parse(signing_key, raw, self.headers.get(SIGNATURE_HEADER))
            except InvalidSignature:
                return self._reply(401, {"error": "invalid signature"})
            if event.dedupe_key in SEEN:
                return self._reply(200, {"status": "duplicate"})
            SEEN[event.dedupe_key] = None
            if len(SEEN) > SEEN_MAX:
                SEEN.popitem(last=False)
            # Hand off slow work (fetching a snapshot, opening live view) to a queue or thread.
            print(f"[receiver] {event.type} device={str(event.device_id)[:24]}... "
                  f"sub_type={event.sub_type} at={event.timestamp}")
            return self._reply(200, {"status": "accepted", "type": event.type})

        def _reply(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:  # keep the output readable
            pass

    return RingWebhookHandler


def self_test(signing_key: str) -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(signing_key))
    url = f"http://127.0.0.1:{server.server_address[1]}/webhooks/ring"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        device = "ava1.ring.device.EXAMPLE"
        event = build_event("motion_detected", device, sub_type="package_delivery")
        r1 = deliver(url, event, signing_key)
        r2 = deliver(url, event, signing_key)  # same request_id: a Ring retry
        r3 = deliver(url, event, "wrong-key")  # signature will not match
        ding = deliver(url, build_event("button_press", device), signing_key)
        results = [("valid", r1, 200, "accepted"), ("retry", r2, 200, "duplicate"),
                   ("tampered", r3, 401, None), ("doorbell press", ding, 200, "accepted")]
        ok = True
        for name, resp, status, want in results:
            got = resp.json().get("status")
            passed = resp.status_code == status and (want is None or got == want)
            ok &= passed
            print(f"{'PASS' if passed else 'FAIL'}  {name:<15} -> {resp.status_code} {resp.json()}")
        return 0 if ok else 1
    finally:
        server.shutdown()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--self-test", action="store_true", help="send simulated webhooks and exit")
    args = ap.parse_args()
    key = os.environ.get("RING_HMAC_KEY", "dev-secret" if args.self_test else "")
    if not key:
        print("set RING_HMAC_KEY (the signing key from the Ring Developer Portal)", file=sys.stderr)
        return 2
    if args.self_test:
        return self_test(key)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(key))
    print(f"listening on http://localhost:{args.port}/webhooks/ring (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
