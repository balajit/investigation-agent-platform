"""Canonical claim-target identity (review H16). Pure; never raises."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

_WS = re.compile(r"\s+")
_SLASHES = re.compile(r"/{2,}")
_TEXT_FIELDS = (
    "symbol", "counterpart", "declaring_type", "commit_subject",
    "value_semantics", "claimed_value", "fact_category",
)


def canonical_text(value: object) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)  # StrEnum members
    text = _WS.sub(" ", str(raw)).strip()
    return text or None


def canonical_path(value: object) -> str | None:
    text = canonical_text(value)
    if text is None:
        return None
    text = _SLASHES.sub("/", text.replace("\\", "/"))
    while text.startswith("./"):
        text = text[2:]
    stripped = text.rstrip("/")
    return stripped or ("/" if text.startswith("/") else None)


def canonical_signature(value: object) -> str | None:
    text = canonical_text(value)
    return _WS.sub("", text) if text else None


def canonical_target(target: Mapping[str, Any] | None) -> dict[str, Any]:
    source = target or {}
    out: dict[str, Any] = {}
    for key in _TEXT_FIELDS:
        value = canonical_text(source.get(key))
        if value is not None:
            out[key] = value
    for key in ("file_path", "scope"):
        value = canonical_path(source.get(key))
        if value is not None:
            out[key] = value
    sig = canonical_signature(source.get("signature"))
    if sig:
        out["signature"] = sig
    commit = canonical_text(source.get("commit_id"))
    if commit:
        out["commit_id"] = commit.casefold()
    for key in ("line_start", "line_end"):
        try:
            number = int(source.get(key))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if number >= 1:
            out[key] = number
    return out


def identity_key(canonical: Mapping[str, Any] | None) -> str:
    """16-hex identity of a canonical target, or ``NO_TARGET`` when it has none."""
    ident = dict(canonical)
    if ident.get("symbol"):
        ident.pop("line_start", None)
        ident.pop("line_end", None)
    if not ident:
        return "NO_TARGET"
    payload = json.dumps(sorted(ident.items()), ensure_ascii=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def stamp_identity(target: Mapping[str, Any] | None) -> dict[str, Any]:
    """The canonical target plus its ``identity_key``; the write-time stamp (LV_H)."""
    canonical = canonical_target(target)
    stamped = dict(canonical)
    if canonical:
        stamped["identity_key"] = identity_key(canonical)
    return stamped
