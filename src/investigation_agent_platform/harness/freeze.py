"""Freeze validation and trust (§7): preimages, key list, signatures.

Verification order at each boundary: schema-check the freeze, key list,
and record first; recompute the freeze digest excluding the self field;
digest-verify the key list against the freeze-derived `key_list_ref`;
validate the whole key list before resolving anything;
resolve `key_id` through an injected public-key resolver (private keys never
appear in artifacts); recompute the signed preimage
(`domain || 0x00 || canonical bytes` excluding `signature` and
`signed_digest`) and require `signed_digest` to equal its SHA-256 digest;
verify the detached Ed25519 signature; bind via the record's `freeze_hash`
against the independently recomputed freeze digest (never an assumed
`signed_digest == freeze_hash` equality); enforce the use constraint
(freeze validator accepts ONLY `FREEZE_APPROVAL`, probe validator accepts
ONLY `STAGE_C_PROBE_AUTHORIZATION` bound to the same run set and freeze).
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from investigation_agent_platform.harness.canonical import (
    canonical_bytes,
    sha256_digest,
)
from investigation_agent_platform.harness.registry import (
    HarnessValidationError,
    check_record,
)

FREEZE_APPROVAL = "FREEZE_APPROVAL"
PROBE_AUTHORIZATION = "STAGE_C_PROBE_AUTHORIZATION"


def parse_instant(value: str) -> datetime:
    """Timezone-aware instant comparison: only explicit UTC offsets pass.

    Naive timestamps and non-UTC offsets are rejected, never coerced
    (proposal §5).
    """
    if not isinstance(value, str):
        raise HarnessValidationError(f"timestamp must be a string: {value!r}")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise HarnessValidationError(f"invalid timestamp: {value!r}") from None
    if parsed.tzinfo is None:
        raise HarnessValidationError(f"naive timestamp rejected: {value!r}")
    if parsed.utcoffset() != timedelta(0):
        raise HarnessValidationError(f"non-UTC offset rejected: {value!r}")
    return parsed.astimezone(UTC)


def freeze_preimage_bytes(freeze: dict[str, Any]) -> bytes:
    """Canonical bytes of the freeze record excluding its self hash."""
    body = {k: v for k, v in freeze.items() if k != "freeze_hash"}
    return canonical_bytes(body)


def freeze_digest(freeze: dict[str, Any]) -> str:
    """Independently recomputed freeze digest."""
    return sha256_digest(freeze_preimage_bytes(freeze))


def verify_freeze_self_hash(freeze: dict[str, Any]) -> None:
    """The record's `freeze_hash` must equal the recomputed digest."""
    if freeze.get("freeze_hash") != freeze_digest(freeze):
        raise HarnessValidationError("freeze_hash does not match recomputed freeze digest")


def verify_rule_preimage(preimage_bytes: bytes, ref: dict[str, Any]) -> None:
    """A rule preimage's bytes must hash to its bound digest."""
    if sha256_digest(preimage_bytes) != ref.get("digest"):
        raise HarnessValidationError(f"rule preimage digest mismatch for {ref.get('path')!r}")


def validate_keylist(keylist: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate the ENTIRE key list before any key is resolved.

    Shape, unique key IDs, pinned `Ed25519` algorithm values, well-formed
    ordered validity windows. Any failure voids the run.
    """
    if not isinstance(keylist, dict) or not isinstance(keylist.get("keys"), list):
        raise HarnessValidationError("key-list must be an object with a keys array")
    if not keylist["keys"]:
        raise HarnessValidationError("key-list must not be empty")
    seen: set[str] = set()
    for entry in keylist["keys"]:
        if not isinstance(entry, dict):
            raise HarnessValidationError("key-list entry must be an object")
        kid = entry.get("key_id")
        if not isinstance(kid, str) or not kid:
            raise HarnessValidationError("key-list entry missing key_id")
        if kid in seen:
            raise HarnessValidationError(f"duplicate key_id in key list: {kid!r}")
        seen.add(kid)
        if entry.get("algorithm") != "Ed25519":
            raise HarnessValidationError(f"key {kid!r} algorithm must be Ed25519")
        if not isinstance(entry.get("key_reference"), str) or not entry["key_reference"]:
            raise HarnessValidationError(f"key {kid!r} missing key reference")
        if not isinstance(entry.get("revoked"), bool):
            raise HarnessValidationError(f"key {kid!r} missing revoked flag")
        parse_instant(entry.get("valid_from", ""))
        valid_until = entry.get("valid_until")
        if valid_until is not None:
            start = parse_instant(entry["valid_from"])
            end = parse_instant(valid_until)
            if end < start:
                raise HarnessValidationError(f"key {kid!r} validity window inverted")
    return [dict(entry) for entry in keylist["keys"]]


def entry_valid_at(entry: dict[str, Any], moment: str) -> bool:
    """Validity-window check at attestation/approval time (inclusive bounds)."""
    if entry.get("revoked", False):
        return False
    now = parse_instant(moment)
    if parse_instant(entry["valid_from"]) > now:
        return False
    valid_until = entry.get("valid_until")
    return valid_until is None or parse_instant(valid_until) >= now


def resolve_key(
    entries: list[dict[str, Any]],
    key_id: str,
    resolver: Callable[[str], Ed25519PublicKey],
) -> Ed25519PublicKey:
    """Resolve `key_id` to exactly one validated entry via the resolver."""
    matches = [e for e in entries if e["key_id"] == key_id]
    if len(matches) != 1:
        raise HarnessValidationError(
            f"key_id must resolve to exactly one key-list entry: {key_id!r}"
        )
    try:
        return resolver(key_id)
    except LookupError:
        raise HarnessValidationError(f"unknown key_id: {key_id!r}") from None


def signed_preimage(domain: str, record: dict[str, Any]) -> bytes:
    """`domain || 0x00 || canonical(record minus signature and signed_digest)`."""
    body = {k: v for k, v in record.items() if k not in ("signature", "signed_digest")}
    return domain.encode() + b"\x00" + canonical_bytes(body)


def record_payload_digest(domain: str, record: dict[str, Any]) -> str:
    """SHA-256 of the signed preimage; `signed_digest` must equal this."""
    return sha256_digest(signed_preimage(domain, record))


def verify_record_signature(
    domain: str, record: dict[str, Any], public_key: Ed25519PublicKey
) -> None:
    """Digest match first, then Ed25519 verification with the resolved key."""
    if record.get("signed_digest") != record_payload_digest(domain, record):
        raise HarnessValidationError("signed_digest does not match recomputed payload digest")
    try:
        signature = base64.b64decode(record.get("signature", ""), validate=True)
    except (binascii.Error, ValueError):
        raise HarnessValidationError("signature is not strict standard base64") from None
    try:
        public_key.verify(signature, signed_preimage(domain, record))
    except InvalidSignature:
        raise HarnessValidationError("Ed25519 signature does not verify") from None


def verify_freeze_binding(record: dict[str, Any], digest: str) -> None:
    """Bind via the record's `freeze_hash` to the independently recomputed digest."""
    if record.get("freeze_hash") != digest:
        raise HarnessValidationError("record freeze_hash does not match recomputed freeze digest")


def _authorization_chain(
    *,
    domain: str,
    record: dict[str, Any],
    keylist: dict[str, Any],
    keylist_digest: str,
    resolver: Callable[[str], Ed25519PublicKey],
    freeze_digest_value: str,
    allowed_decision_types: frozenset[str],
    moment_field: str,
) -> None:
    """Full authorization chain (steps 0–5); any failure voids.

    Internal: digest inputs must already be derived from the validated
    freeze — use the public validators, which do that derivation.
    """
    # Step 0: strict record shape (canonical round-trip enforces the
    # shared strict rules) + whole key-list validation + digest binding.
    canonical_bytes({k: v for k, v in record.items() if k != "signature"})
    entries = validate_keylist(keylist)
    if sha256_digest(canonical_bytes(keylist)) != keylist_digest:
        raise HarnessValidationError("key-list digest mismatch against freeze key_list_ref")
    # Step 1: resolve EVERY key-list entry through the injected resolver;
    # a resolution failure for any entry fails validation — the chain
    # never skips to a matching key. Then check the validity window at
    # the record's own attestation/approval instant.
    public_keys = {}
    for listed in entries:
        public_keys[listed["key_id"]] = resolve_key(entries, listed["key_id"], resolver)
    entry = next((e for e in entries if e["key_id"] == record.get("key_id")), None)
    if entry is None:
        raise HarnessValidationError(f"key_id has no key-list entry: {record.get('key_id')!r}")
    moment = record.get(moment_field, "")
    if not entry_valid_at(entry, moment):
        raise HarnessValidationError("key not valid at attestation/approval time")
    public_key = public_keys[entry["key_id"]]
    # Steps 2–3: digest match + signature.
    verify_record_signature(domain, record, public_key)
    # Step 4: independent freeze binding.
    verify_freeze_binding(record, freeze_digest_value)
    # Step 5: use constraint enforced by the entry point.
    if record.get("decision_type") not in allowed_decision_types:
        raise HarnessValidationError(
            f"decision_type {record.get('decision_type')!r} not permitted by this validator"
        )


def _freeze_context(freeze: dict[str, Any], keylist: dict[str, Any]) -> tuple[str, str]:
    """Derive the trust inputs from the freeze itself — never from callers.

    Recomputes and verifies the freeze self-hash, then requires the
    key-list digest to equal the freeze's `key_list_ref.digest`. Returns
    `(freeze_digest, keylist_digest)`.
    """
    verify_freeze_self_hash(freeze)
    digest = freeze_digest(freeze)
    keylist_digest = sha256_digest(canonical_bytes(keylist))
    ref = freeze.get("key_list_ref", {})
    if keylist_digest != ref.get("digest"):
        raise HarnessValidationError("key-list digest does not match freeze key_list_ref")
    return digest, keylist_digest


def generate_dev_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Ephemeral dev keypair (dev scope only; see build authorization §2)."""
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


def sign_record(
    domain: str, record: dict[str, Any], private_key: Ed25519PrivateKey
) -> dict[str, Any]:
    """Attach `signed_digest` + detached Ed25519 `signature` (dev/test helper)."""
    signed = dict(record)
    signed["signed_digest"] = record_payload_digest(domain, signed)
    signed["signature"] = base64.b64encode(
        private_key.sign(signed_preimage(domain, signed))
    ).decode("ascii")
    return signed


def freeze_authorization_validator(
    *,
    record: dict[str, Any],
    freeze: dict[str, Any],
    keylist: dict[str, Any],
    resolver: Callable[[str], Ed25519PublicKey],
    registry: dict[str, Any],
) -> None:
    """Accepts ONLY `decision_type == FREEZE_APPROVAL` for the freeze run set.

    Boundary first: freeze, key list, and record are each schema-checked
    (scan then schema). The record's `run_set_id` must equal the freeze's.
    Digests are derived from the validated freeze itself, never accepted
    from the caller.
    """
    check_record(registry, "freeze", freeze)
    check_record(registry, "keyList", keylist)
    check_record(registry, "ownerAuthorization", record)
    if record.get("run_set_id") != freeze.get("run_set_id"):
        raise HarnessValidationError("authorization run_set_id does not match freeze run_set_id")
    freeze_digest_value, keylist_digest = _freeze_context(freeze, keylist)
    _authorization_chain(
        domain="iap:d3-stagec:owner-authorization/v5",
        record=record,
        keylist=keylist,
        keylist_digest=keylist_digest,
        resolver=resolver,
        freeze_digest_value=freeze_digest_value,
        allowed_decision_types=frozenset({FREEZE_APPROVAL}),
        moment_field="approved_at",
    )


def probe_authorization_validator(
    *,
    record: dict[str, Any],
    run_set_id: str,
    freeze: dict[str, Any],
    keylist: dict[str, Any],
    resolver: Callable[[str], Ed25519PublicKey],
    registry: dict[str, Any],
) -> None:
    """Accepts ONLY `STAGE_C_PROBE_AUTHORIZATION` with three-way run-set equality.

    The record, the explicit argument, and the freeze must all name the
    same run set. Digests are derived from the validated freeze, never
    accepted from the caller.
    """
    check_record(registry, "freeze", freeze)
    check_record(registry, "keyList", keylist)
    check_record(registry, "ownerAuthorization", record)
    if record.get("run_set_id") != run_set_id:
        raise HarnessValidationError("probe authorization run_set_id mismatch")
    if run_set_id != freeze.get("run_set_id"):
        raise HarnessValidationError("probe run_set_id argument does not match freeze run_set_id")
    freeze_digest_value, keylist_digest = _freeze_context(freeze, keylist)
    _authorization_chain(
        domain="iap:d3-stagec:owner-authorization/v5",
        record=record,
        keylist=keylist,
        keylist_digest=keylist_digest,
        resolver=resolver,
        freeze_digest_value=freeze_digest_value,
        allowed_decision_types=frozenset({PROBE_AUTHORIZATION}),
        moment_field="approved_at",
    )


def attestation_validator(
    *,
    record: dict[str, Any],
    freeze: dict[str, Any],
    keylist: dict[str, Any],
    resolver: Callable[[str], Ed25519PublicKey],
    registry: dict[str, Any],
) -> None:
    """Validates a `runtime-attestation/v4` environment attestation.

    Boundary first: freeze, key list, and record are each schema-checked.
    The attestation has no decision type — its contract is schema shape,
    domain separation, the `attested_at` moment, an independent freeze
    binding, and run-set equality with the freeze. Use-constraint: never
    usable as approval or probe authorization.
    """
    check_record(registry, "freeze", freeze)
    check_record(registry, "keyList", keylist)
    check_record(registry, "attestation", record)
    if record.get("run_set_id") != freeze.get("run_set_id"):
        raise HarnessValidationError("attestation run_set_id does not match freeze")
    if "decision_type" in record:
        raise HarnessValidationError("attestation must not carry a decision_type")
    freeze_digest_value, keylist_digest = _freeze_context(freeze, keylist)
    _authorization_chain(
        domain="iap:d3-stagec:runtime-attestation/v4",
        record=record,
        keylist=keylist,
        keylist_digest=keylist_digest,
        resolver=resolver,
        freeze_digest_value=freeze_digest_value,
        allowed_decision_types=frozenset({None}),
        moment_field="attested_at",
    )


__all__ = [
    "FREEZE_APPROVAL",
    "PROBE_AUTHORIZATION",
    "attestation_validator",
    "entry_valid_at",
    "freeze_authorization_validator",
    "freeze_digest",
    "freeze_preimage_bytes",
    "generate_dev_keypair",
    "parse_instant",
    "probe_authorization_validator",
    "record_payload_digest",
    "resolve_key",
    "sign_record",
    "signed_preimage",
    "validate_keylist",
    "verify_freeze_binding",
    "verify_freeze_self_hash",
    "verify_record_signature",
    "verify_rule_preimage",
]
