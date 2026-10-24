"""Shared strict canonicalization path for the Stage C harness (proposal §5).

Signing and verification MUST use this single implementation — no divergent
reimplementation is permitted. Rules: UTF-8 JSON, sorted object keys,
compact separators, NFC strings, duplicate keys and floats rejected, arrays
in defined order, no trailing newline. Timeouts, floats, and non-NFC input
fail closed at parse time, before any cryptographic check.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata

_CANONICAL_SEPARATORS = (",", ":")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key: {key!r}")
        seen.add(key)
        out[key] = value
    return out


def _reject_float(value: str) -> float:
    raise ValueError(f"float literal rejected: {value!r}")


def _reject_constant(value: str) -> float:
    # NaN / Infinity / -Infinity bypass parse_float via parse_constant.
    raise ValueError(f"non-standard constant rejected: {value!r}")


def _assert_nfc(obj: object) -> None:
    if isinstance(obj, str):
        if unicodedata.normalize("NFC", obj) != obj:
            raise ValueError("non-NFC string rejected")
    elif isinstance(obj, dict):
        for key, value in obj.items():
            _assert_nfc(key)
            _assert_nfc(value)
    elif isinstance(obj, list):
        for item in obj:
            _assert_nfc(item)


def _check_serializable(obj: object) -> None:
    """Pre-serialization walk: only JSON-native values with NFC strings.

    Rejects floats (including NaN/Infinity, which are float instances),
    non-string keys, non-NFC strings, and any non-JSON-native value, so
    invalid values can never enter a signing or digest path via hashing
    serialized output directly.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise ValueError(  # noqa: TRY004 — validators contract on ValueError.
                    f"non-string key rejected: {key!r}"
                )
            _check_serializable(key)
            _check_serializable(value)
    elif isinstance(obj, list):
        for item in obj:
            _check_serializable(item)
    elif isinstance(obj, str):
        if unicodedata.normalize("NFC", obj) != obj:
            raise ValueError("non-NFC string rejected")
    elif isinstance(obj, float):
        raise ValueError(  # noqa: TRY004 — validators contract on ValueError.
            f"float rejected: {obj!r}"
        )
    elif not isinstance(obj, (int, bool)) and obj is not None:
        raise ValueError(f"non-JSON-native value rejected: {type(obj)!r}")


def strict_parse(raw: bytes) -> object:
    """Parse JSON bytes under the strict contract; fail closed on violation.

    Valid JSON whitespace before the document is accepted; trailing data
    after it is rejected.
    """
    text = raw.decode("utf-8").lstrip(" \t\n\r")
    decoder = json.JSONDecoder(
        object_pairs_hook=_reject_duplicate_keys,
        parse_float=_reject_float,
        parse_constant=_reject_constant,
    )
    obj, end = decoder.raw_decode(text)
    # Only JSON's four whitespace characters may trail the document;
    # str.strip() would also accept e.g. non-breaking space.
    if text[end:].strip(" \t\n\r") != "":
        raise ValueError("trailing data after JSON document rejected")
    _assert_nfc(obj)
    return obj


def canonical_bytes(obj: object) -> bytes:
    """Canonical bytes: sorted keys, compact separators, UTF-8, no trailing newline.

    Values are validated before serialization (NFC, no floats, no NaN /
    Infinity, JSON-native only); `allow_nan=False` backs the float check.
    """
    _check_serializable(obj)
    return json.dumps(
        obj,
        sort_keys=True,
        separators=_CANONICAL_SEPARATORS,
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_digest(data: bytes) -> str:
    """Hex digest in registry `sha256` shape (`sha256:<64 hex>`)."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def formula_canonical_bytes(text: str) -> bytes:
    """Canonical formula bytes: LF endings with exactly one trailing newline.

    CRLF and standalone CR both normalize to LF; multiple trailing
    newlines collapse to exactly one.
    """
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    return (body.rstrip("\n") + "\n").encode("utf-8")


def domain_digest(domain: bytes, payload: bytes) -> str:
    """Domain-separated digest: `domain || 0x00 || payload`."""
    return "sha256:" + hashlib.sha256(domain + b"\x00" + payload).hexdigest()


def formula_digest(text: str) -> str:
    """Digest of the metric-formula preimage (domain `iap:d3-stagec:metric-formula/v1`)."""
    return domain_digest(b"iap:d3-stagec:metric-formula/v1", formula_canonical_bytes(text))
