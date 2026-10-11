"""Exceptions raised by the Ring Partner API client.

Ring returns JSON:API error documents (``{"errors": [{"status", "title", "detail"}]}``)
for most endpoints, including the media download endpoints. Each HTTP status the docs
call out gets its own subclass so callers can branch without parsing strings.
"""

from __future__ import annotations

from typing import Any


class RingError(Exception):
    """Base class for every error raised by ringpartner."""


class RingAPIError(RingError):
    def __init__(
        self,
        status: int,
        title: str | None = None,
        detail: str | None = None,
        errors: list[dict[str, Any]] | None = None,
        method: str | None = None,
        url: str | None = None,
    ) -> None:
        self.status = status
        self.title = title
        self.detail = detail
        self.errors = errors or []
        self.method = method
        self.url = url
        where = f" ({method} {url})" if method and url else ""
        text = " - ".join(p for p in (title, detail) if p) or "no error body"
        super().__init__(f"HTTP {status}: {text}{where}")


class RingBadRequest(RingAPIError):
    """400 — invalid parameters or malformed body."""


class RingUnauthorized(RingAPIError):
    """401 — missing, invalid or expired access token. Playground tokens last ~30 minutes."""


class RingForbidden(RingAPIError):
    """403 — token is valid but the scope or per-app subscription does not cover the device."""


class RingNotFound(RingAPIError):
    """404 — resource does not exist, or the device was removed from the integration."""


class RingRateLimited(RingAPIError):
    """429 — over the per-client rate limit (default 100 TPS)."""

    def __init__(self, *args: Any, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class RingDeviceUnavailable(RingAPIError):
    """503 — the device is offline or the media is not ready yet."""


_BY_STATUS: dict[int, type[RingAPIError]] = {
    400: RingBadRequest,
    401: RingUnauthorized,
    403: RingForbidden,
    404: RingNotFound,
    429: RingRateLimited,
    503: RingDeviceUnavailable,
}


def error_for(
    status: int,
    body: Any,
    *,
    method: str | None = None,
    url: str | None = None,
    retry_after: float | None = None,
) -> RingAPIError:
    errors: list[dict[str, Any]] = []
    if isinstance(body, dict) and isinstance(body.get("errors"), list):
        errors = [e for e in body["errors"] if isinstance(e, dict)]
    first = errors[0] if errors else {}
    cls = _BY_STATUS.get(status, RingAPIError)
    kwargs: dict[str, Any] = dict(
        title=first.get("title"),
        detail=first.get("detail"),
        errors=errors,
        method=method,
        url=url,
    )
    if cls is RingRateLimited:
        return RingRateLimited(status, retry_after=retry_after, **kwargs)
    return cls(status, **kwargs)
