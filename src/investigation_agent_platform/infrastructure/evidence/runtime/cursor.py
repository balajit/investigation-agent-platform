# src/investigation_agent_platform/infrastructure/evidence/runtime/cursor.py
"""Signed pagination cursor codec for the Elastic runtime adapter (Part 9).

Cursors carry ``search_after`` state bound to the exact normalized query that
produced them (cryptographic query fingerprint) plus tenant/investigation
scope, format version, and expiry. Decoding never returns ``None``: absence
of a cursor means "first page" (handled by the caller); any supplied-but-
invalid cursor raises ``InvalidCursorException`` instead of silently
restarting pagination. Only safe failure metadata is logged, never the token.
"""

import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from investigation_agent_platform.domain.common.exceptions import (
    InvalidCursorException,
    PlatformConfigurationError,
)

logger = logging.getLogger(__name__)

# Cursor format version. Pre-remediation unversioned tokens are rejected as
# invalid (hard invalidate on deploy, never reinterpreted as current format).
CURSOR_VERSION = 1

# Structural sort arity: every search sort clause is exactly [mapping sort
# field, `_id` tiebreak], so every cursor carries exactly two values —
# regardless of mapping. Encode and decode both enforce this; a mismatch is
# corrupt pagination state, never something to truncate or pad through.
CURSOR_SORT_ARITY = 2


def _valid_sort_values(sort_values: Any) -> bool:
    """Sort state must be exactly CURSOR_SORT_ARITY JSON scalars."""
    return (
        isinstance(sort_values, list)
        and len(sort_values) == CURSOR_SORT_ARITY
        and all(v is None or isinstance(v, (str, int, float, bool)) for v in sort_values)
    )


@dataclass(frozen=True)
class ElasticCursorCodec:
    """HMAC-signed, query-bound pagination cursor codec."""

    signing_key: bytes
    ttl_seconds: int = 3600

    def __post_init__(self) -> None:
        if not self.signing_key:
            raise PlatformConfigurationError(
                "Elastic cursor codec requires a signing key; refusing to construct."
            )

    def encode(
        self,
        *,
        sort_values: list[Any],
        tenant_id: str,
        investigation_id: Any,
        query_fingerprint: str,
    ) -> str:
        """Sign sort state into a cursor token.

        Raises ``ValueError`` for non-conforming sort state. This is a
        server-side invariant (provider returned unpaginable sort values),
        not an agent error — callers convert it to a loud provider failure,
        never a silent first-page restart.
        """
        if not _valid_sort_values(sort_values):
            raise ValueError("Sort state must be a 2-element list of JSON scalars to encode.")
        issued_at = datetime.now(UTC).timestamp()
        payload = {
            "v": CURSOR_VERSION,
            "tenant_id": tenant_id,
            "investigation_id": str(investigation_id),
            "query_fingerprint": query_fingerprint,
            "sort": sort_values,
            "issued_at": issued_at,
            "expires_at": issued_at + self.ttl_seconds,
        }
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        sig = hmac.new(self.signing_key, payload_json.encode("utf-8"), hashlib.sha256).hexdigest()
        token = json.dumps({"p": payload_json, "s": sig}, separators=(",", ":"))
        return base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii")

    def decode(
        self,
        *,
        cursor: str,
        expected_query_fingerprint: str,
        tenant_id: str,
        investigation_id: Any,
    ) -> list[Any]:
        """Validate a cursor and return its ``search_after`` state.

        Raises ``InvalidCursorException`` for every invalid shape: undecodable,
        tampered, unversioned, foreign scope, query mismatch, expired, or
        malformed sort state.
        """

        def _invalid(reason: str, version: Any = None) -> InvalidCursorException:
            logger.warning(
                "Invalid pagination cursor rejected",
                extra={"context": {"reason": reason, "cursor_version": version}},
            )
            return InvalidCursorException(
                "The pagination cursor is invalid, expired, or does not match "
                "the current investigation query. Restart pagination without a cursor."
            )

        try:
            raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise _invalid("decode") from exc
        try:
            token: Any = json.loads(raw)
            payload_str: Any = token["p"]
            signature: Any = token["s"]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise _invalid("structure") from exc
        if not isinstance(payload_str, str) or not isinstance(signature, str):
            raise _invalid("structure")
        expected_sig = hmac.new(
            self.signing_key, payload_str.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected_sig, signature):
            raise _invalid("signature")
        try:
            data: Any = json.loads(payload_str)
        except ValueError as exc:
            raise _invalid("payload") from exc
        if not isinstance(data, dict):
            raise _invalid("payload")
        version = data.get("v")
        if version != CURSOR_VERSION:
            raise _invalid("version", version)
        if data.get("tenant_id") != tenant_id or data.get("investigation_id") != str(
            investigation_id
        ):
            raise _invalid("scope", version)
        if data.get("query_fingerprint") != expected_query_fingerprint:
            raise _invalid("query_binding", version)
        expires_at = data.get("expires_at")
        if not isinstance(expires_at, (int, float)) or datetime.now(UTC).timestamp() > expires_at:
            raise _invalid("expired", version)
        sort_values = data.get("sort")
        if not _valid_sort_values(sort_values):
            raise _invalid("sort_state", version)
        return cast("list[Any]", sort_values)
