"""Record-and-replay for the Ring API.

Playground tokens expire after ~30 minutes and live-view sessions after 30–60 s, which makes
iterating on an integration slow. ``record_fixtures`` captures real responses once; the
``ReplayTransport`` then serves them to :class:`RingClient`/:class:`AsyncRingClient` offline,
so tests and development run without a token.

Fixture layout::

    fixtures/<name>/
        index.json            # [{"key","status","file","content_type","headers"}]
        responses/*.json|jpg|bin
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import httpx


def _slug(text: str) -> str:
    # Ring device ids are ~110 chars, so plain truncation made /status, /capabilities, ...
    # collide. Keep a readable prefix and disambiguate with a short hash of the full key.
    readable = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    if len(readable) <= 100:
        return readable
    digest = hashlib.sha1(text.encode()).hexdigest()[:10]
    return f"{readable[:40]}_{readable[-48:]}_{digest}"


def _key(method: str, path: str, params: dict[str, Any] | None) -> str:
    q = "&".join(f"{k}={params[k]}" for k in sorted(params or {}))
    return f"{method.upper()} {path}{'?' + q if q else ''}"


# Response headers worth keeping. Authorization is a request header and is never stored;
# Location is rewritten (see RecordingTransport) so pre-signed security tokens never land on disk.
_KEEP_HEADERS = ("location", "x-media-timestamp", "x-media-origin", "x-media-length", "retry-after")
_REPLAY_MEDIA_HOST = "media.replay.invalid"


def _is_json(content_type: str | None) -> bool:
    return bool(content_type and "json" in content_type)


def _ext(content_type: str | None) -> str:
    if _is_json(content_type):
        return "json"
    ct = content_type or ""
    for needle, ext in (("jpeg", "jpg"), ("png", "png"), ("mp4", "mp4"), ("sdp", "sdp")):
        if needle in ct:
            return ext
    return "bin"


class FixtureRecorder:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        (self.dir / "responses").mkdir(parents=True, exist_ok=True)
        index_path = self.dir / "index.json"
        self.index: list[dict[str, Any]] = (
            json.loads(index_path.read_text()) if index_path.exists() else []
        )

    def add(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        status: int,
        body: bytes,
        content_type: str | None,
        headers: dict[str, str] | None = None,
    ) -> str:
        key = _key(method, path, params)
        name = f"{_slug(key)}.{_ext(content_type)}"
        out = self.dir / "responses" / name
        written = False
        if _is_json(content_type) and body:
            try:
                out.write_text(json.dumps(json.loads(body), indent=2))
                written = True
            except ValueError:
                pass
        if not written:
            out.write_bytes(body)
        self.index = [e for e in self.index if e["key"] != key]
        entry: dict[str, Any] = {
            "key": key,
            "status": status,
            "file": f"responses/{name}",
            "content_type": content_type,
        }
        if headers:
            entry["headers"] = headers
        self.index.append(entry)
        (self.dir / "index.json").write_text(json.dumps(self.index, indent=2))
        return key


class _Replay:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.entries = {e["key"]: e for e in json.loads((self.dir / "index.json").read_text())}

    def respond(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params) or None
        key = _key(request.method, request.url.path, params)
        entry = self.entries.get(key) or self.entries.get(
            _key(request.method, request.url.path, None)
        )
        if entry is None:
            body = {
                "errors": [
                    {
                        "status": "404",
                        "title": "Not Found",
                        "detail": f"no fixture recorded for {key}",
                    }
                ]
            }
            return httpx.Response(404, json=body, request=request)
        content = (self.dir / entry["file"]).read_bytes()
        headers = {"content-type": entry.get("content_type") or "application/json"}
        headers.update(entry.get("headers") or {})
        return httpx.Response(entry["status"], content=content, headers=headers, request=request)


class ReplayTransport(httpx.BaseTransport):
    """Serve a recorded fixture directory to :class:`RingClient` (no network, no token)."""

    def __init__(self, directory: str | Path) -> None:
        self._replay = _Replay(directory)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._replay.respond(request)


class AsyncReplayTransport(httpx.AsyncBaseTransport):
    """Serve a recorded fixture directory to :class:`AsyncRingClient`."""

    def __init__(self, directory: str | Path) -> None:
        self._replay = _Replay(directory)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._replay.respond(request)


class RecordingTransport(httpx.BaseTransport):
    """Wrap a real transport and write every API response into a fixture directory.

    Media downloads are a 303 to a pre-signed URL on another host. The recorder rewrites the
    stored ``Location`` to ``https://media.replay.invalid/<slug>`` and files the follow-up GET
    under that alias, so replay works offline and the pre-signed security token is never saved.
    """

    def __init__(
        self,
        directory: str | Path,
        inner: httpx.BaseTransport | None = None,
        api_host_suffix: str = "amazonvision.com",
    ) -> None:
        self.recorder = FixtureRecorder(directory)
        self.inner = inner or httpx.HTTPTransport()
        self.api_host_suffix = api_host_suffix
        self._aliases: dict[str, str] = {}  # real pre-signed URL -> alias path

    def _kept_headers(self, response: httpx.Response) -> dict[str, str]:
        return {h: response.headers[h] for h in _KEEP_HEADERS if h in response.headers}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.inner.handle_request(request)
        response.read()
        url = str(request.url)
        ct = response.headers.get("content-type")
        if url in self._aliases:
            alias_path = self._aliases.pop(url)
            headers = self._kept_headers(response)
            headers.pop("location", None)
            self.recorder.add(
                "GET", alias_path, None, response.status_code, response.content, ct, headers
            )
        elif request.url.host.endswith(self.api_host_suffix):
            headers = self._kept_headers(response)
            params = dict(request.url.params) or None
            if "location" in headers and 300 <= response.status_code < 400:
                real = str(request.url.join(headers["location"]))
                alias_path = "/" + _slug(_key(request.method, request.url.path, params))
                self._aliases[real] = alias_path
                headers["location"] = f"https://{_REPLAY_MEDIA_HOST}{alias_path}"
            self.recorder.add(
                request.method,
                request.url.path,
                params,
                response.status_code,
                response.content,
                ct,
                headers,
            )
        return response

    def close(self) -> None:
        self.inner.close()
