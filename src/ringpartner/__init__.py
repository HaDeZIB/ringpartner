"""ringpartner — unofficial Python SDK for the Ring Partner API (Amazon Vision).

Ring does not publish an official Partner SDK; this package wraps the documented REST API,
WHEP live video, webhook verification, a signed webhook simulator and fixture replay.

Live video needs the optional ``whep`` extra (aiortc + PyAV) and is imported lazily::

    from ringpartner.whep import WhepSession   # or: ringpartner.WhepSession
"""

from __future__ import annotations

from typing import Any

from . import errors
from .auth import RefreshTokenProvider, TokenSet, exchange_code, refresh
from .client import API_BASE, DEVICE_INCLUDES, AsyncRingClient, RingClient
from .errors import (
    RingAPIError,
    RingBadRequest,
    RingDeviceUnavailable,
    RingError,
    RingForbidden,
    RingNotFound,
    RingRateLimited,
    RingUnauthorized,
)
from .fixtures import AsyncReplayTransport, RecordingTransport, ReplayTransport
from .models import Device, HistoryEvent, HistoryPage, Media, User
from .simulator import build_event, deliver, signed_request
from .webhooks import (
    EVENT_TYPES,
    MOTION_SUB_TYPES,
    SIGNATURE_HEADER,
    InvalidSignature,
    WebhookEvent,
    parse_event,
    sign,
    verify_and_parse,
    verify_signature,
)

__version__ = "0.1.0"

_LAZY_WHEP = ("WhepSession", "WhepAnswer", "WhepError", "codecs_from_sdp")


def __getattr__(name: str) -> Any:
    # Keep `import ringpartner` free of aiortc/av; load the WHEP module on first use.
    if name in _LAZY_WHEP:
        from . import whep

        return getattr(whep, name)
    raise AttributeError(f"module 'ringpartner' has no attribute {name!r}")


__all__ = [
    "API_BASE",
    "DEVICE_INCLUDES",
    "EVENT_TYPES",
    "MOTION_SUB_TYPES",
    "SIGNATURE_HEADER",
    "AsyncReplayTransport",
    "AsyncRingClient",
    "Device",
    "HistoryEvent",
    "HistoryPage",
    "InvalidSignature",
    "Media",
    "RecordingTransport",
    "RefreshTokenProvider",
    "ReplayTransport",
    "RingAPIError",
    "RingBadRequest",
    "RingClient",
    "RingDeviceUnavailable",
    "RingError",
    "RingForbidden",
    "RingNotFound",
    "RingRateLimited",
    "RingUnauthorized",
    "TokenSet",
    "User",
    "WebhookEvent",
    "build_event",
    "deliver",
    "errors",
    "exchange_code",
    "parse_event",
    "refresh",
    "sign",
    "signed_request",
    "verify_and_parse",
    "verify_signature",
    *_LAZY_WHEP,
]
