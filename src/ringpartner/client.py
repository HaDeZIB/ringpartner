"""REST client for the Ring Partner API (https://api.amazonvision.com).

Two clients share one set of request builders and response parsers:

* :class:`RingClient` — synchronous, for scripts and CLIs.
* :class:`AsyncRingClient` — asyncio, for services and the WHEP live video helper.

Both handle what the docs ask partners to handle: JSON:API parsing with ``included``,
429 back-off that honours ``Retry-After``, typed errors, and the two-step media download
(``303 See Other`` to a pre-signed URL that must be fetched *without* the bearer token).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from .errors import RingAPIError, RingRateLimited, error_for
from .models import Device, HistoryEvent, HistoryPage, Media, User, index_included, ms_to_datetime

API_BASE = "https://api.amazonvision.com"
USER_AGENT = "ringpartner-python/0.1"

TokenSource = str | Callable[[], str]
AsyncTokenSource = str | Callable[[], str] | Callable[[], Awaitable[str]]

DEVICE_INCLUDES = ("status", "capabilities", "configurations", "location")

_REDIRECTS = (301, 302, 303, 307, 308)
# Video downloads answer 206 Partial Content when the clip runs past "now".
_MEDIA_OK = (200, 206)


@dataclass
class _Spec:
    method: str
    path: str
    params: dict[str, Any] | None = None
    json: Any = None
    media: bool = False
    expect: tuple[int, ...] = (200,)
    extra_headers: dict[str, str] = field(default_factory=dict)


def _to_ms(value: datetime | int | float) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    return int(value)


# ---------------------------------------------------------------------------
# request builders (pure)
# ---------------------------------------------------------------------------


def _spec_devices(include: tuple[str, ...] | None) -> _Spec:
    params = {"include": ",".join(include)} if include else None
    return _Spec("GET", "/v1/devices", params=params)


def _spec_device(device_id: str, include: tuple[str, ...] | None) -> _Spec:
    params = {"include": ",".join(include)} if include else None
    return _Spec("GET", f"/v1/devices/{device_id}", params=params)


def _spec_device_rel(device_id: str, rel: str, component_id: str | None = None) -> _Spec:
    params = {"component_id": component_id} if component_id is not None else None
    return _Spec("GET", f"/v1/devices/{device_id}/{rel}", params=params)


def _spec_history(device_id: str, event_types: list[str] | None, page_key: str | None) -> _Spec:
    params: dict[str, Any] = {}
    if event_types:
        # Plural and comma-separated; supports composite "motion.human". The singular
        # "event_type" and "filter[event_type]" are silently ignored by the API.
        params["event_types"] = ",".join(event_types)
    if page_key:
        params["page[key]"] = page_key
    return _Spec("GET", f"/v1/history/devices/{device_id}/events", params=params or None)


def _spec_image(
    device_id: str,
    at: datetime | int | None,
    between: tuple[datetime | int, datetime | int] | None,
    fmt: Literal["jpeg", "png"],
    width: int | None,
    height: int | None,
    component_id: str | None,
) -> _Spec:
    if (at is None) == (between is None):
        raise ValueError("pass exactly one of `at` or `between`")
    body: dict[str, Any]
    if at is not None:
        body = {"type": "at_timestamp", "timestamp": _to_ms(at)}
    else:
        assert between is not None
        body = {
            "type": "latest_in_range",
            "start_timestamp": _to_ms(between[0]),
            "end_timestamp": _to_ms(between[1]),
        }
    options: dict[str, Any] = {"format": fmt}
    if width and height:
        options["resolution"] = {"width": width, "height": height}
    body["image_options"] = options
    if component_id is not None:
        body["components"] = [{"component_id": str(component_id)}]
    return _Spec("POST", f"/v1/devices/{device_id}/media/image/download", json=body, media=True)


def _spec_video(
    device_id: str, start: datetime | int, duration_ms: int, component_id: str | None
) -> _Spec:
    body: dict[str, Any] = {"timestamp": _to_ms(start), "duration": int(duration_ms)}
    if component_id is not None:
        body["components"] = [{"component_id": str(component_id)}]
    return _Spec("POST", f"/v1/devices/{device_id}/media/video/download", json=body, media=True)


# ---------------------------------------------------------------------------
# response parsers (pure)
# ---------------------------------------------------------------------------


def _parse_devices(doc: dict[str, Any]) -> list[Device]:
    included = index_included(doc)
    data = doc.get("data") or []
    if isinstance(data, dict):
        data = [data]
    return [Device.from_resource(r, included) for r in data]


def _parse_history(doc: dict[str, Any]) -> HistoryPage:
    events = [HistoryEvent.from_resource(r) for r in doc.get("data") or []]
    next_link = (doc.get("links") or {}).get("next")
    next_key = None
    if next_link:
        next_key = (parse_qs(urlparse(next_link).query).get("page[key]") or [None])[0]
    # An empty page still carries a next link; stop paginating when a page is empty.
    if not events:
        next_key = None
    return HistoryPage(events=events, next_key=next_key, raw=doc)


def _parse_media(resp: httpx.Response) -> Media:
    return Media(
        content=resp.content,
        content_type=resp.headers.get("content-type"),
        timestamp=ms_to_datetime(resp.headers.get("x-media-timestamp")),
        origin=resp.headers.get("x-media-origin"),
        length_ms=_int_or_none(resp.headers.get("x-media-length")),
    )


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _error_from(resp: httpx.Response) -> RingAPIError:
    try:
        body = resp.json()
    except ValueError:
        body = None
    retry_after = None
    if "retry-after" in resp.headers:
        try:
            retry_after = float(resp.headers["retry-after"])
        except ValueError:
            retry_after = None
    url = resp.request.url
    if "security_token" in url.query.decode() or "X-Amz-" in url.query.decode():
        url = url.copy_with(query=None)  # never surface pre-signed credentials in messages
    return error_for(
        resp.status_code,
        body,
        method=resp.request.method,
        url=str(url),
        retry_after=retry_after,
    )


def _backoff(attempt: int, err: RingRateLimited) -> float:
    if err.retry_after is not None:
        return err.retry_after
    return min(2**attempt, 30)


# ---------------------------------------------------------------------------
# sync client
# ---------------------------------------------------------------------------


class RingClient:
    """Synchronous Ring Partner API client.

    ``token`` may be a string (e.g. a Playground token) or a zero-arg callable that returns
    a fresh access token (see :class:`ringpartner.auth.RefreshTokenProvider`).
    """

    def __init__(
        self,
        token: TokenSource,
        *,
        base_url: str = API_BASE,
        timeout: float = 20.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = token
        self._max_retries = max_retries
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        # Pre-signed media URLs live on another host and must not receive the bearer token.
        self._media_http = httpx.Client(timeout=timeout * 3, transport=transport)

    def __enter__(self) -> RingClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()
        self._media_http.close()

    def _auth(self) -> dict[str, str]:
        token = self._token() if callable(self._token) else self._token
        return {"Authorization": f"Bearer {token}"}

    def _send(self, spec: _Spec) -> Any:
        attempt = 0
        while True:
            resp = self._http.request(
                spec.method,
                spec.path,
                params=spec.params,
                json=spec.json,
                headers={**self._auth(), **spec.extra_headers},
                follow_redirects=False,
            )
            if spec.media and resp.status_code in _REDIRECTS:
                # Step 2: pre-signed URL, fetched by a client that never carries the token.
                location = urljoin(str(resp.request.url), resp.headers["location"])
                resp = self._media_http.get(location)
                if resp.status_code not in _MEDIA_OK:
                    raise _error_from(resp)
                return resp
            if resp.status_code in spec.expect or (spec.media and resp.status_code in _MEDIA_OK):
                return resp if spec.media else (resp.json() if resp.content else None)
            err = _error_from(resp)
            if isinstance(err, RingRateLimited) and attempt < self._max_retries:
                time.sleep(_backoff(attempt, err))
                attempt += 1
                continue
            raise err

    # -- devices ---------------------------------------------------------------

    def devices(self, include: tuple[str, ...] | None = DEVICE_INCLUDES) -> list[Device]:
        return _parse_devices(self._send(_spec_devices(include)))

    def device(self, device_id: str, include: tuple[str, ...] | None = DEVICE_INCLUDES) -> Device:
        return _parse_devices(self._send(_spec_device(device_id, include)))[0]

    def status(self, device_id: str) -> dict[str, Any]:
        return (self._send(_spec_device_rel(device_id, "status"))["data"] or {}).get(
            "attributes", {}
        )

    def capabilities(self, device_id: str, component_id: str | None = None) -> dict[str, Any]:
        doc = self._send(_spec_device_rel(device_id, "capabilities", component_id))
        return (doc["data"] or {}).get("attributes", {})

    def configurations(self, device_id: str, component_id: str | None = None) -> dict[str, Any]:
        doc = self._send(_spec_device_rel(device_id, "configurations", component_id))
        return (doc["data"] or {}).get("attributes", {})

    def location(self, device_id: str) -> dict[str, Any]:
        return (self._send(_spec_device_rel(device_id, "location"))["data"] or {}).get(
            "attributes", {}
        )

    # -- history ---------------------------------------------------------------

    def history(
        self, device_id: str, event_types: list[str] | None = None, page_key: str | None = None
    ) -> HistoryPage:
        return _parse_history(self._send(_spec_history(device_id, event_types, page_key)))

    def iter_history(
        self, device_id: str, event_types: list[str] | None = None, max_pages: int = 20
    ) -> Iterator[HistoryEvent]:
        key: str | None = None
        for _ in range(max_pages):
            page = self.history(device_id, event_types, key)
            yield from page.events
            if not page.next_key or page.next_key == key:
                return
            key = page.next_key

    # -- media -----------------------------------------------------------------

    def snapshot(
        self,
        device_id: str,
        *,
        at: datetime | int | None = None,
        between: tuple[datetime | int, datetime | int] | None = None,
        fmt: Literal["jpeg", "png"] = "jpeg",
        width: int | None = None,
        height: int | None = None,
        component_id: str | None = None,
    ) -> Media:
        spec = _spec_image(device_id, at, between, fmt, width, height, component_id)
        return _parse_media(self._send(spec))

    def video_clip(
        self,
        device_id: str,
        start: datetime | int,
        duration_ms: int,
        component_id: str | None = None,
    ) -> Media:
        return _parse_media(self._send(_spec_video(device_id, start, duration_ms, component_id)))

    # -- account ---------------------------------------------------------------

    def me(self) -> User:
        doc = self._send(_Spec("GET", "/v1/users/me"))
        return User(account_id=doc["data"]["id"], raw=doc["data"])

    def subscriptions(self) -> list[dict[str, Any]]:
        return self._send(_Spec("GET", "/v1/accounts/me/subscriptions")).get("data") or []

    def raw(self, method: str, path: str, **kwargs: Any) -> Any:
        """Escape hatch for endpoints this SDK does not model yet."""
        return self._send(_Spec(method.upper(), path, **kwargs))


# ---------------------------------------------------------------------------
# async client
# ---------------------------------------------------------------------------


class AsyncRingClient:
    """asyncio twin of :class:`RingClient`. Same methods, awaitable."""

    def __init__(
        self,
        token: AsyncTokenSource,
        *,
        base_url: str = API_BASE,
        timeout: float = 20.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._max_retries = max_retries
        self.base_url = base_url
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        self._media_http = httpx.AsyncClient(timeout=timeout * 3, transport=transport)

    async def __aenter__(self) -> AsyncRingClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._media_http.aclose()

    async def access_token(self) -> str:
        if callable(self._token):
            value = self._token()
            if asyncio.iscoroutine(value):
                value = await value
            return value  # type: ignore[return-value]
        return self._token

    async def _send(self, spec: _Spec) -> Any:
        attempt = 0
        while True:
            token = await self.access_token()
            resp = await self._http.request(
                spec.method,
                spec.path,
                params=spec.params,
                json=spec.json,
                headers={"Authorization": f"Bearer {token}", **spec.extra_headers},
                follow_redirects=False,
            )
            if spec.media and resp.status_code in _REDIRECTS:
                location = urljoin(str(resp.request.url), resp.headers["location"])
                resp = await self._media_http.get(location)
                if resp.status_code not in _MEDIA_OK:
                    raise _error_from(resp)
                return resp
            if resp.status_code in spec.expect or (spec.media and resp.status_code in _MEDIA_OK):
                return resp if spec.media else (resp.json() if resp.content else None)
            err = _error_from(resp)
            if isinstance(err, RingRateLimited) and attempt < self._max_retries:
                await asyncio.sleep(_backoff(attempt, err))
                attempt += 1
                continue
            raise err

    async def devices(self, include: tuple[str, ...] | None = DEVICE_INCLUDES) -> list[Device]:
        return _parse_devices(await self._send(_spec_devices(include)))

    async def device(
        self, device_id: str, include: tuple[str, ...] | None = DEVICE_INCLUDES
    ) -> Device:
        return _parse_devices(await self._send(_spec_device(device_id, include)))[0]

    async def status(self, device_id: str) -> dict[str, Any]:
        doc = await self._send(_spec_device_rel(device_id, "status"))
        return (doc["data"] or {}).get("attributes", {})

    async def capabilities(self, device_id: str, component_id: str | None = None) -> dict[str, Any]:
        doc = await self._send(_spec_device_rel(device_id, "capabilities", component_id))
        return (doc["data"] or {}).get("attributes", {})

    async def configurations(
        self, device_id: str, component_id: str | None = None
    ) -> dict[str, Any]:
        doc = await self._send(_spec_device_rel(device_id, "configurations", component_id))
        return (doc["data"] or {}).get("attributes", {})

    async def location(self, device_id: str) -> dict[str, Any]:
        doc = await self._send(_spec_device_rel(device_id, "location"))
        return (doc["data"] or {}).get("attributes", {})

    async def history(
        self, device_id: str, event_types: list[str] | None = None, page_key: str | None = None
    ) -> HistoryPage:
        return _parse_history(await self._send(_spec_history(device_id, event_types, page_key)))

    async def iter_history(
        self, device_id: str, event_types: list[str] | None = None, max_pages: int = 20
    ) -> AsyncIterator[HistoryEvent]:
        key: str | None = None
        for _ in range(max_pages):
            page = await self.history(device_id, event_types, key)
            for event in page.events:
                yield event
            if not page.next_key or page.next_key == key:
                return
            key = page.next_key

    async def snapshot(
        self,
        device_id: str,
        *,
        at: datetime | int | None = None,
        between: tuple[datetime | int, datetime | int] | None = None,
        fmt: Literal["jpeg", "png"] = "jpeg",
        width: int | None = None,
        height: int | None = None,
        component_id: str | None = None,
    ) -> Media:
        spec = _spec_image(device_id, at, between, fmt, width, height, component_id)
        return _parse_media(await self._send(spec))

    async def video_clip(
        self,
        device_id: str,
        start: datetime | int,
        duration_ms: int,
        component_id: str | None = None,
    ) -> Media:
        spec = _spec_video(device_id, start, duration_ms, component_id)
        return _parse_media(await self._send(spec))

    async def me(self) -> User:
        doc = await self._send(_Spec("GET", "/v1/users/me"))
        return User(account_id=doc["data"]["id"], raw=doc["data"])

    async def subscriptions(self) -> list[dict[str, Any]]:
        return (await self._send(_Spec("GET", "/v1/accounts/me/subscriptions"))).get("data") or []

    async def raw(self, method: str, path: str, **kwargs: Any) -> Any:
        return await self._send(_Spec(method.upper(), path, **kwargs))
