# src/investigation_agent_platform/domain/common/pagination.py
"""Signed opaque cursor envelopes for paginated collection APIs (Part 11.3D).

Cursors are HMAC-SHA256 authenticated JSON blobs: deterministic ordering is
the caller's responsibility (cursor carries ``sort`` + ``tiebreaker``), while
this module guarantees tamper-evidence. The signing secret is passed by the
caller — domain code never reads environment.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PageCursor(BaseModel):
    """Decoded cursor payload (Part 11.3D)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sort: str = Field(default="created_at", min_length=1, max_length=64)
    tiebreaker: str = Field(default="", max_length=256)
    direction: str = Field(default="asc", min_length=1, max_length=4)
    version: int = Field(default=1, ge=1)


class CursorError(ValueError):
    """Raised when a cursor is malformed, tampered, or version-unknown."""


def encode_cursor(payload: dict[str, Any], secret: str) -> str:
    """Serialize + sign a cursor payload into an opaque token."""
    if not secret:
        raise CursorError("cursor signing secret must not be empty")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(raw + b"." + sig.hex().encode("ascii"))
    return token.decode("ascii")


def decode_cursor(token: str, secret: str) -> dict[str, Any]:
    """Verify + deserialize a cursor token; raises CursorError on any problem."""
    if not secret:
        raise CursorError("cursor signing secret must not be empty")
    try:
        blob = base64.urlsafe_b64decode(token.encode("ascii"))
        raw, sig_hex = blob.rsplit(b".", 1)
    except (ValueError, TypeError) as exc:
        raise CursorError("malformed cursor token") from exc
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    try:
        sig_text = sig_hex.decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise CursorError("cursor signature is not valid") from exc
    if not hmac.compare_digest(sig_text, expected):
        raise CursorError("cursor signature mismatch (tampered cursor)")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CursorError("cursor payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CursorError("cursor payload must be an object")
    return payload
