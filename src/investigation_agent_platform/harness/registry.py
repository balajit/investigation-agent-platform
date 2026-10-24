"""Registry loading, schema validation, and authority-boundary scans (§§5, 8).

The companion registry is the sole machine-readable schema source. Records
are validated with JSON Schema Draft 2020-12; the §8 authority scan runs
*before* schema acceptance and rejects alias/case-variant smuggling that
schema shape checks alone do not name: frozen authority aliases in any key,
a true `declaration_requirement_satisfied`, and out-of-enum verdicts.
NFKC + casefold normalization applies before every comparison (not raw-text
scans). Enum-closed fields are enforced by the registry schemas themselves.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from investigation_agent_platform.harness.canonical import canonical_bytes, sha256_digest

REGISTRY_ID = "iap:d3-stagec:registry/v12"

#: Closed cell vocabulary (mirrors the registry `cell` enum, the single
#: source of truth for cell names). Single home for the tuple so the
#: ledger and metrics modules both depend downward on this module.
CELLS = ("accept-PASS", "accept-FAIL", "nonaccept-PASS", "nonaccept-FAIL")

#: Pinned SHA-256 of the registry file bytes
#: (`docs/IAP-proof-kernel-d3-stageC-harness-schemas-v1.13.json`).
#: Loading verifies this digest, so a modified registry keeping the same
#: `$id` is rejected. Any registry change requires a new identity, a new
#: pin, and review.
REGISTRY_SHA256 = "b1142ed9db57625762667685a416f95c9cd5014993bb025cfa8652df16093546"

#: Digest of the canonical in-memory v12 registry object. Unlike the file-byte
#: digest above, this pins dictionaries supplied directly to public APIs.
REGISTRY_CANONICAL_SHA256 = "d11bb0af0ca927c2d741e305f1f41abda2ed201442cc4d7c7fb07e9bbdfdaa30"

#: Frozen authority-alias list: any key normalizing to one of these fails.
FROZEN_ALIASES = frozenset(
    {
        "authorised",
        "authorized",
        "authz",
        "approve_authority",
        "approval_authority",
        "declaration-authorized",
        "declaration_authorized",
    }
)

#: Closed verdict vocabulary (mirrors registry `reportVerdict`).
#: Lowercase by construction: inputs are NFKC + casefold normalized
#: before the membership check, so `HOLD` and `hold` both pass.
CLOSED_VERDICTS = frozenset({"insufficient_evidence", "hold", "feasibility_demonstrated"})

#: Forbidden authority-claim values, matched as whole normalized tokens in
#: ANY string field at any depth (proposal §8). Whole-token matching keeps
#: legitimate compounds such as "accept-PASS" or "general-path" passing.
FORBIDDEN_VALUES = frozenset(
    {
        "authorised",
        "authorized",
        "authz",
        "approve_authority",
        "approval_authority",
        "declaration-authorized",
        "declaration_authorized",
        "approved",
        "proven",
        "pass",
    }
)


class HarnessValidationError(ValueError):
    """A record failed registry-schema or authority-boundary validation."""


def normalize_token(value: str) -> str:
    """NFKC-normalize then ASCII-casefold a key or value token."""
    return unicodedata.normalize("NFKC", value).casefold()


def load_registry(path: str | Path) -> dict[str, Any]:
    """Load and identity-check the companion registry (strict JSON parse).

    Verifies the `$id` and the pinned file digest, so a modified registry
    retaining the same `$id` is rejected. (Freeze validation re-verifies
    the digest via the manifest independently.)
    """
    raw = Path(path).read_bytes()
    if sha256_digest(raw) != f"sha256:{REGISTRY_SHA256}":
        raise HarnessValidationError(
            "registry digest mismatch: file is not the pinned v12 registry"
        )
    from investigation_agent_platform.harness.canonical import strict_parse

    reg = strict_parse(raw)
    if not isinstance(reg, dict) or reg.get("$id") != REGISTRY_ID:
        raise HarnessValidationError(f"registry identity mismatch: expected {REGISTRY_ID}")
    return reg


def validate_pinned_registry(registry: dict[str, Any]) -> None:
    """Require an in-memory registry to match the pinned v12 identity and content."""
    if not isinstance(registry, dict) or registry.get("$id") != REGISTRY_ID:
        raise HarnessValidationError(f"registry identity mismatch: expected {REGISTRY_ID}")
    try:
        digest = sha256_digest(canonical_bytes(registry))
    except (TypeError, ValueError) as exc:
        raise HarnessValidationError("registry is not a valid canonical JSON object") from exc
    if digest != f"sha256:{REGISTRY_CANONICAL_SHA256}":
        raise HarnessValidationError(
            "registry digest mismatch: object is not the pinned v12 registry"
        )


def validator_for(registry: dict[str, Any], ref: str) -> Draft202012Validator:
    """Validator for one registry `$defs` entry by name."""
    return Draft202012Validator({"$ref": f"#/$defs/{ref}", "$defs": registry["$defs"]})


def validate_record(registry: dict[str, Any], ref: str, obj: dict[str, Any]) -> None:
    """Validate one record against a registry definition; raise on failure."""
    validator = validator_for(registry, ref)
    errors = sorted(validator.iter_errors(obj), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        raise HarnessValidationError(
            f"{ref} schema violation at "
            f"{'/'.join(str(p) for p in first.path) or '<root>'}: "
            f"{first.message}"
        )


def scan_authority(obj: Any, _path: str = "<root>") -> None:
    """Recursive §8 authority scan; runs before schema acceptance.

    Rejects frozen-alias keys anywhere, a true
    `declaration_requirement_satisfied`, out-of-enum `verdict` values,
    forbidden authority-claim values in ANY string field at any depth
    (whole-token match, so compounds like "accept-PASS" still pass), and a
    truthy `verified` used as authority outside a native snapshot (the only
    legitimate `verified` is the boolean `native.verified` field).
    """
    if isinstance(obj, str):
        if normalize_token(obj) in FORBIDDEN_VALUES:
            raise HarnessValidationError(f"forbidden authority value rejected at {_path}: {obj!r}")
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            token = normalize_token(key) if isinstance(key, str) else key
            if token in FROZEN_ALIASES:
                raise HarnessValidationError(f"authority alias key rejected at {_path}: {key!r}")
            if token == "declaration_requirement_satisfied" and value is not False:
                raise HarnessValidationError(
                    f"declaration_requirement_satisfied must be false at {_path}"
                )
            if token == "verdict" and isinstance(value, str):
                if normalize_token(value).replace("-", "_") not in CLOSED_VERDICTS:
                    raise HarnessValidationError(
                        f"forbidden verdict rejected at {_path}: {value!r}"
                    )
            if token == "verified":
                # Only the top-level native snapshot field (perTurn.native)
                # is legitimate: a nested object merely named "native" is a
                # lookalike and gets no exception.
                parent_is_native = _path == "<root>.native"
                if parent_is_native:
                    if not isinstance(value, bool):
                        raise HarnessValidationError(
                            f"non-boolean native verified rejected at {_path}: {value!r}"
                        )
                elif value:
                    raise HarnessValidationError(
                        f"verified used as authority rejected at {_path}: {key!r}"
                    )
            scan_authority(value, f"{_path}.{key}")
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            scan_authority(item, f"{_path}[{index}]")


def check_record(registry: dict[str, Any], ref: str, obj: dict[str, Any]) -> None:
    """Authority scan first, then registry-schema validation."""
    scan_authority(obj)
    validate_record(registry, ref, obj)
