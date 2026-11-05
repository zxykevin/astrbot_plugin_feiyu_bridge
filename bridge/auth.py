from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Mapping

AUTH_PREFIX = "FEIYU-BRIDGE-V1"
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")


class AuthenticationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class BridgeAuthenticator:
    secret: str
    client_id: str
    clock_skew_seconds: int = 60
    nonce_cache_size: int = 4096
    _nonces: OrderedDict[str, int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.secret = str(self.secret or "")
        self.client_id = str(self.client_id or "").strip()
        self.clock_skew_seconds = max(15, int(self.clock_skew_seconds))
        self.nonce_cache_size = max(128, int(self.nonce_cache_size))
        self._nonces = OrderedDict()

    @property
    def configured(self) -> bool:
        return len(self.secret) >= 32 and bool(self.client_id)

    @staticmethod
    def canonical_message(
        method: str,
        protocol_path: str,
        timestamp: str,
        nonce: str,
        body: bytes,
    ) -> bytes:
        body_hash = hashlib.sha256(body).hexdigest()
        return "\n".join(
            (
                AUTH_PREFIX,
                method.upper(),
                protocol_path,
                timestamp,
                nonce,
                body_hash,
            )
        ).encode("utf-8")

    def sign(
        self,
        method: str,
        protocol_path: str,
        timestamp: str,
        nonce: str,
        body: bytes,
    ) -> str:
        message = self.canonical_message(
            method, protocol_path, timestamp, nonce, body
        )
        return hmac.new(
            self.secret.encode("utf-8"), message, hashlib.sha256
        ).hexdigest()

    def verify(
        self,
        method: str,
        protocol_path: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        now: int | None = None,
    ) -> str:
        if not self.configured:
            raise AuthenticationError(
                "BRIDGE_NOT_CONFIGURED", "bridge authentication is not configured"
            )

        client_id = str(headers.get("X-Feiyu-Client-Id", "")).strip()
        timestamp = str(headers.get("X-Feiyu-Timestamp", "")).strip()
        nonce = str(headers.get("X-Feiyu-Nonce", "")).strip()
        signature = str(headers.get("X-Feiyu-Signature", "")).strip().lower()

        if not hmac.compare_digest(client_id, self.client_id):
            raise AuthenticationError("AUTH_CLIENT_REJECTED", "client id rejected")
        if not NONCE_RE.fullmatch(nonce):
            raise AuthenticationError("AUTH_INVALID_NONCE", "invalid nonce")
        if not SIGNATURE_RE.fullmatch(signature):
            raise AuthenticationError("AUTH_INVALID_SIGNATURE", "invalid signature")
        try:
            timestamp_int = int(timestamp)
        except ValueError as exc:
            raise AuthenticationError(
                "AUTH_INVALID_TIMESTAMP", "invalid timestamp"
            ) from exc

        current = int(time.time()) if now is None else int(now)
        if abs(current - timestamp_int) > self.clock_skew_seconds:
            raise AuthenticationError(
                "AUTH_TIMESTAMP_EXPIRED", "timestamp outside accepted window"
            )

        self._purge_nonces(current)
        nonce_key = f"{client_id}:{nonce}"
        if nonce_key in self._nonces:
            raise AuthenticationError("AUTH_REPLAY", "nonce has already been used")

        expected = self.sign(method, protocol_path, timestamp, nonce, body)
        if not hmac.compare_digest(signature, expected):
            raise AuthenticationError("AUTH_INVALID_SIGNATURE", "invalid signature")

        self._nonces[nonce_key] = timestamp_int
        self._nonces.move_to_end(nonce_key)
        while len(self._nonces) > self.nonce_cache_size:
            self._nonces.popitem(last=False)
        return client_id

    def _purge_nonces(self, now: int) -> None:
        cutoff = now - self.clock_skew_seconds
        stale = [key for key, seen_at in self._nonces.items() if seen_at < cutoff]
        for key in stale:
            self._nonces.pop(key, None)

