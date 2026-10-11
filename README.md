# ringpartner

Unofficial Python SDK for the **Ring Partner API** (Amazon Vision, `https://api.amazonvision.com`).

Ring publishes REST docs and a browser JavaScript WHEP example, but **no official Partner SDK**.
`ringpartner` covers the parts every integration ends up rewriting:

- a typed REST client, sync (`RingClient`) and asyncio (`AsyncRingClient`), with JSON:API
  `included` resolution, typed errors, 429 back-off that honours `Retry-After`, and the
  two-step media download;
- **server-side WHEP live video** (aiortc), which yields decoded frames for computer vision;
- **webhook verification** (`X-Signature: sha256=<hex>`) and v1.1 payload parsing;
- a **signed webhook simulator**, because the Developer Playground does not deliver webhooks;
- **record/replay fixtures**, so you can develop and test offline once your Playground token expires;
- a `ringpartner` CLI.

> Not affiliated with or endorsed by Ring LLC or Amazon. "Ring" is a trademark of its owner.

## Install

```bash
pip install ringpartner            # REST, webhooks, simulator, fixtures (only needs httpx)
pip install 'ringpartner[whep]'    # adds live video: aiortc, PyAV, numpy
```

Python 3.10 or newer.

## Quickstart

### Sync client

```python
from ringpartner import RingClient

with RingClient("ACCESS_TOKEN") as ring:
    for d in ring.devices():                 # ?include=status,capabilities,configurations,location
        print(d.id, d.name, d.online, d.is_chime, d.is_encrypted)

    for ev in ring.iter_history(d.id, event_types=["motion.human", "ding"]):
        print(ev.start, ev.event_type, ev.is_third_party_reviewed)

    img = ring.snapshot(d.id, between=(start_ms, end_ms))   # or at=<epoch ms>
    img.save("front.jpg")
```

`token` can be a string or a zero-argument callable. For linked accounts use
`RefreshTokenProvider(refresh_token, client_id, client_secret)`: it refreshes the 4-hour access
token shortly before it expires and is thread-safe.

Errors are typed: `RingUnauthorized` (401), `RingForbidden` (403), `RingNotFound` (404),
`RingRateLimited` (429, retried automatically up to `max_retries`), `RingDeviceUnavailable` (503),
and `RingAPIError` for everything else. Each carries `.status`, `.title`, `.detail`, `.errors`.

### Async client

```python
from ringpartner import AsyncRingClient

async with AsyncRingClient(token) as ring:
    devices = await ring.devices()
    async for ev in ring.iter_history(devices[0].id, ["motion"]):
        ...
```

`token` may also be an async callable here.

### Live video frames for computer vision (WHEP)

```python
from ringpartner import AsyncRingClient
from ringpartner.whep import WhepSession      # needs the [whep] extra

async with AsyncRingClient(token) as ring:
    async with WhepSession(ring, device_id, max_seconds=25) as live:
        print(live.answer.video_codec)        # read from the SDP answer, e.g. "H264/90000"
        async for frame in live.frames(fps=2):
            bgr = frame.to_ndarray(format="bgr24")   # numpy array, ready for OpenCV / a model
```

`await live.record("clip.mp4")` writes the stream to a file. Pass `audio=True` for one-way audio
and `component_id="1"` to pick a camera on a multi-camera device. The session is closed with
`DELETE <Location>` on exit. `import ringpartner` never imports aiortc; `ringpartner.WhepSession`
is loaded lazily.

### Verifying webhooks

```python
from ringpartner import verify_and_parse, InvalidSignature

@app.post("/webhooks/ring")
async def ring_webhook(request):
    raw = await request.body()               # the raw bytes, not re-serialised JSON
    try:
        event = verify_and_parse(SIGNING_KEY, raw, request.headers.get("X-Signature"))
    except InvalidSignature:
        return Response(status_code=401)
    if seen(event.dedupe_key):               # Ring retries; dedupe on meta.request_id
        return Response(status_code=200)
    if event.type == "motion_detected" and event.sub_type == "human":
        ...
    return Response(status_code=200)         # respond within 5 s
```

`WebhookEvent` exposes `type`, `device_id` (device events only), `device_ids` (subscription
events list covered devices here), `sub_type`, `component_ids`, `account_id`, `request_id`,
`timestamp` and the full `raw` document.

### Simulating webhooks

The Developer Playground simulates live-view sessions but **does not deliver webhooks**, so
without real hardware you cannot exercise your motion or doorbell handler end to end. The
simulator builds payloads in the documented v1.1 shape for all 10 event types, signs them with
your HMAC key, and POSTs them, so your production handler runs unchanged, signature check included.

```python
from ringpartner import build_event, deliver

payload = build_event("motion_detected", device_id, sub_type="human", component_ids=["0"])
deliver("http://localhost:8000/webhooks/ring", payload, SIGNING_KEY)
```

Device events get `data.id = <device>_<sub_type>_<ts>` for motion and `<device>_<event_type>_<ts>`
for the rest (e.g. `<device>_button_press_<ts>`). `app_integration_*` uses `source_type: "users"`
with no `relationships`. `subscription_*` uses `source: "subscriptions"`, `source_id`, `plan_id`
and `relationships.devices.data`. `signed_request(payload, key)` returns `(body, headers)` if you
would rather send the request through your own test client.

### Record once, replay offline

Playground access tokens last about **30 minutes** and live sessions 30 to 60 seconds, so
iterating against the live API is slow. Record real responses once and replay them as often as
you like, with no token and no network:

```python
from ringpartner import RingClient, RecordingTransport, ReplayTransport

with RingClient(token, transport=RecordingTransport("fixtures/playground")) as ring:
    ring.devices(); ring.history(device_id); ring.snapshot(device_id, at=ts)

with RingClient("unused", transport=ReplayTransport("fixtures/playground")) as ring:
    ring.devices()        # served from disk
```

`AsyncReplayTransport` does the same for `AsyncRingClient`. Fixtures never contain your bearer
token. The pre-signed media `Location` is rewritten to `https://media.replay.invalid/...`, so its
`security_token` is never written to disk either. A request with no recorded fixture gets a
JSON:API 404.

## CLI

The token comes from `--token` or `RING_ACCESS_TOKEN` and is never printed.

```bash
export RING_ACCESS_TOKEN=...                      # e.g. from the Developer Playground

ringpartner devices                               # id, name, online, encrypted
ringpartner history DEVICE_ID --types motion.human,ding
ringpartner snapshot DEVICE_ID --out front.jpg    # latest image in the last 10 min
ringpartner snapshot DEVICE_ID --out at.jpg --at-ms 1699457230000
ringpartner record DEVICE_ID --out live.mp4 --seconds 25 --audio     # needs [whep]
ringpartner capture-fixtures --dir fixtures/playground

ringpartner simulate motion_detected --device DEVICE_ID --sub-type human \
    --url http://localhost:8000/webhooks/ring --key "$RING_HMAC_KEY"
ringpartner verify --key "$RING_HMAC_KEY" --signature "sha256=..." < body.json
```

`capture-fixtures` calls devices, `users/me`, and for each device its detail, status,
capabilities, configurations, location and history, plus one snapshot. If an individual call
fails it reports the failure and moves on.

## Gotchas from the docs (handled for you)

- **History filters use `event_types`, plural and comma-separated**, with composite values such as
  `motion.human`. The singular `event_type` is silently ignored. Pagination follows
  `links.next` (`page[key]`), and an empty page still carries a `next` link, so the iterator
  stops on the first empty page.
- **Media downloads are two steps.** The POST returns `303 See Other` to a pre-signed URL on
  another host, and that URL must be fetched **without** the `Authorization` header. Video can
  return `206 Partial Content` (`Media.length_ms`, `Media.partial`).
- **WHEP: finish ICE gathering before the POST.** A partial offer causes connectivity failures
  (and usually an undocumented HTTP 500). aiortc gathers inside `setLocalDescription`.
- **WHEP is `recvonly` only.** `sendrecv` and `sendonly` are rejected. The POST is
  `application/sdp`, returns `201` plus a `Location` header, and you close with `DELETE`.
- **Sessions are capped at 30 s for battery devices and 60 s for line-powered ones.** The default
  `max_seconds` is 27. Reconnect if you need longer.
- **Read the live codec from the SDP answer**, not from `capabilities.video.codecs` (an HEVC
  camera may answer H264).
- **`sub_type` is an open vocabulary**: `motion` without Smart Alerts, and `human`, `vehicle`,
  `package_delivery`, `other_motion` with it. Ring can add values, so pass unknown ones through.
- **TAKE/E2EE encrypted devices**: `configurations.e2e_encryption.enabled` is a JSON boolean. When
  it is true, media requests succeed but return content you cannot decode. Check
  `Device.is_encrypted` before running CV.
- **There is no device type field.** A chime is a device whose `audio.supported_actions`
  contains `chime.play` (`Device.is_chime`). Do not parse `image_url`.
- Multi-camera devices (Ring Elite): pass exactly one `component_id` for snapshots and clips.

## Development

```bash
uv venv --python 3.12 && uv pip install -e '.[whep]' pytest pytest-asyncio respx ruff
.venv/bin/pytest && .venv/bin/ruff check .
```

## License

Apache License 2.0. See [LICENSE](LICENSE). Copyright 2026 Brandon Garcia.
