"""Typed views over Ring's JSON:API documents.

Every model keeps the original resource in ``raw`` so nothing the API returns is lost
when a field is not modelled yet (the API adds fields often — see the release notes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def ms_to_datetime(value: Any) -> datetime | None:
    """Ring timestamps are epoch milliseconds (sometimes sent as strings)."""
    if value is None or value == "":
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def index_included(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Map ``(type, id)`` to resources in a JSON:API ``included`` array."""
    return {
        (r.get("type"), r.get("id")): r
        for r in document.get("included") or []
        if isinstance(r, dict)
    }


def _related_attributes(
    resource: dict[str, Any], rel: str, included: dict[tuple[str, str], dict[str, Any]]
) -> dict[str, Any] | None:
    data = ((resource.get("relationships") or {}).get(rel) or {}).get("data")
    if not isinstance(data, dict):
        return None
    hit = included.get((data.get("type"), data.get("id")))
    return (hit or {}).get("attributes") if hit else None


@dataclass
class Device:
    id: str
    name: str | None = None
    image_url: str | None = None
    online: bool | None = None
    status: dict[str, Any] | None = None
    capabilities: dict[str, Any] | None = None
    configurations: dict[str, Any] | None = None
    location: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_resource(
        cls, resource: dict[str, Any], included: dict[tuple[str, str], dict[str, Any]] | None = None
    ) -> Device:
        included = included or {}
        attrs = resource.get("attributes") or {}
        status = _related_attributes(resource, "status", included)
        return cls(
            id=resource["id"],
            name=attrs.get("name"),
            image_url=attrs.get("image_url"),
            online=status.get("online") if status else None,
            status=status,
            capabilities=_related_attributes(resource, "capabilities", included),
            configurations=_related_attributes(resource, "configurations", included),
            location=_related_attributes(resource, "location", included),
            raw=resource,
        )

    @property
    def is_chime(self) -> bool | None:
        """True when ``audio.supported_actions`` has ``chime.play`` (docs: Identifying a chime)."""
        if self.capabilities is None:
            return None
        audio = self.capabilities.get("audio") or {}
        return "chime.play" in (audio.get("supported_actions") or [])

    @property
    def is_encrypted(self) -> bool | None:
        """True when TAKE/E2EE is on: video requests return content the partner cannot read."""
        if self.configurations is None:
            return None
        e2e = self.configurations.get("e2e_encryption")
        if isinstance(e2e, dict):
            return bool(e2e.get("enabled"))
        return None


@dataclass
class HistoryEvent:
    id: str
    event_type: str
    start: datetime | None
    end: datetime | None
    device_id: str | None
    is_third_party_reviewed: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> HistoryEvent:
        attrs = resource.get("attributes") or {}
        source = ((resource.get("relationships") or {}).get("source") or {}).get("data") or {}
        return cls(
            id=resource["id"],
            event_type=attrs.get("event_type", "unknown"),
            start=ms_to_datetime(attrs.get("start")),
            end=ms_to_datetime(attrs.get("end")),
            device_id=source.get("id"),
            is_third_party_reviewed=attrs.get("is_third_party_reviewed"),
            raw=resource,
        )


@dataclass
class HistoryPage:
    events: list[HistoryEvent]
    next_key: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class Media:
    """Binary media returned by the image/video download endpoints."""

    content: bytes
    content_type: str | None
    timestamp: datetime | None
    origin: str | None
    length_ms: int | None = None  # X-Media-Length on 206 partial video responses

    @property
    def partial(self) -> bool:
        return self.length_ms is not None

    def save(self, path: str) -> None:
        with open(path, "wb") as fh:
            fh.write(self.content)


@dataclass
class User:
    account_id: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
