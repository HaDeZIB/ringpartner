# Changelog

All notable changes to `ringpartner` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). While the version is `0.x`, minor releases may
contain breaking changes.

## [Unreleased]

## [0.1.0] - 2026-10-11

First public release, written during the Amazon "Build, Ship, Shape" hackathon as the SDK
behind DoorLedger.

### Added

- `RingClient` (sync) and `AsyncRingClient` (asyncio) for the Ring Partner API
  (`https://api.amazonvision.com`): devices with `include=status,capabilities,configurations,location`,
  device detail, status, capabilities, configurations, location, `users/me`, event history with
  `event_types` filters and `links.next` pagination, and snapshot / clip downloads.
- JSON:API `included` resolution into typed models (`Device`, `HistoryEvent`, `HistoryPage`,
  `Media`, `User`), including `Device.is_chime` and `Device.is_encrypted` helpers.
- Typed errors (`RingUnauthorized`, `RingForbidden`, `RingNotFound`, `RingRateLimited`,
  `RingDeviceUnavailable`, `RingBadRequest`, `RingAPIError`) and automatic 429 back-off that
  honours `Retry-After`.
- Two-step media download: follows the `303 See Other` to the pre-signed URL **without** the
  `Authorization` header and handles `206 Partial Content`.
- OAuth helpers: `exchange_code`, `refresh` and a thread-safe `RefreshTokenProvider`.
- Server-side WHEP live view (`ringpartner.whep.WhepSession`, optional `[whep]` extra built on
  aiortc and PyAV): full ICE gathering before the offer, `recvonly` transceivers, decoded frames
  as NumPy arrays, optional one-way audio, `component_id` for multi-camera devices, and
  `DELETE <Location>` on exit.
- Webhook verification (`X-Signature: sha256=<hex>`, constant-time compare) and v1.1 payload
  parsing into `WebhookEvent` with a `dedupe_key`.
- Signed webhook simulator (`build_event`, `signed_request`, `deliver`) for all 10 documented
  event types, because the Developer Playground does not deliver webhooks.
- Record / replay transports (`RecordingTransport`, `ReplayTransport`,
  `AsyncReplayTransport`). Fixtures never contain the bearer token, and pre-signed media URLs
  are rewritten so their `security_token` is never written to disk.
- `ringpartner` CLI: `devices`, `history`, `snapshot`, `record`, `capture-fixtures`,
  `simulate`, `verify`.

### Security

- The access token is never printed by the CLI or included in exception messages.

[Unreleased]: https://github.com/HaDeZIB/doorledger/compare/ringpartner-v0.1.0...HEAD
[0.1.0]: https://github.com/HaDeZIB/doorledger/releases/tag/ringpartner-v0.1.0
