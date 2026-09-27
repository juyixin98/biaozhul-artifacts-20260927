"""Opaque resumable page cursors.

A cursor binds four things, so it cannot be replayed against the wrong data:

* scan id
* epoch (bumped on every explicit reset / version switch)
* last consumed hit ``seq``
* an HMAC over the tuple (keyed by the server secret from config)

Tampering or a cursor from a previous epoch is rejected with a typed error
(stale vs invalid distinguished by the scan service after decoding).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

from .errors import InvalidCursorError


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    try:
        return base64.urlsafe_b64decode(s + pad)
    except Exception as exc:  # noqa: BLE001 - normalized below
        raise InvalidCursorError("cursor is not valid base64url") from exc


@dataclass(frozen=True)
class PageCursor:
    scan_id: str
    epoch: int
    after_seq: int

    def to_token(self, secret: str) -> str:
        body = json.dumps(
            [self.scan_id, self.epoch, self.after_seq],
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
        return _b64u(body) + "." + _b64u(mac)

    @classmethod
    def from_token(cls, token: str, secret: str) -> "PageCursor":
        if not token or token.count(".") != 1:
            raise InvalidCursorError(
                "malformed cursor: expected '<payload>.<mac>'"
            )
        body_s, mac_s = token.split(".", 1)
        body = _b64u_decode(body_s)
        mac = _b64u_decode(mac_s)
        expected = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(mac, expected):
            raise InvalidCursorError("cursor signature mismatch")
        try:
            arr = json.loads(body.decode("utf-8"))
            scan_id, epoch, after_seq = arr
            if not isinstance(scan_id, str) or not isinstance(epoch, int) \
                    or not isinstance(after_seq, int) or epoch < 1 \
                    or after_seq < -1:
                raise ValueError
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise InvalidCursorError("cursor payload has unexpected shape") \
                from exc
        return cls(scan_id=scan_id, epoch=epoch, after_seq=after_seq)
