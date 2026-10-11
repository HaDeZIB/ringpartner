"""OAuth helpers for Ring account linking.

Access tokens from account linking last 4 hours (``expires_in: 14400``); the Playground
issues 30-minute tokens with no refresh token. :class:`RefreshTokenProvider` is a callable
you can pass straight to :class:`~ringpartner.client.RingClient` as ``token``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

OAUTH_TOKEN_URL = "https://oauth.ring.com/oauth/token"


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: float
    raw: dict[str, Any]


def exchange_code(code: str, client_id: str, client_secret: str, **extra: str) -> TokenSet:
    """Exchange an authorization code (one-way linking or PKCE flow) for tokens."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        **extra,
    }
    return _token_request(data)


def refresh(refresh_token: str, client_id: str, client_secret: str) -> TokenSet:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    return _token_request(data)


def _token_request(data: dict[str, str]) -> TokenSet:
    resp = httpx.post(OAUTH_TOKEN_URL, data=data, timeout=20)
    resp.raise_for_status()
    body = resp.json()
    return TokenSet(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token"),
        expires_at=time.time() + float(body.get("expires_in", 14400)),
        raw=body,
    )


class RefreshTokenProvider:
    """Thread-safe callable returning a valid access token, refreshing ``skew`` seconds early."""

    def __init__(self, refresh_token: str, client_id: str, client_secret: str, skew: float = 120):
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._skew = skew
        self._tokens: TokenSet | None = None
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            if self._tokens is None or time.time() > self._tokens.expires_at - self._skew:
                self._tokens = refresh(self._refresh_token, self._client_id, self._client_secret)
                if self._tokens.refresh_token:
                    self._refresh_token = self._tokens.refresh_token
            return self._tokens.access_token
