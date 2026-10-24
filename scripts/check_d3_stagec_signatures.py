#!/usr/bin/env python3
"""Semantic signature tests for the Stage C harness trust contract.

Covers SC-V15-01 (real Ed25519 over the full domain-separated payload),
SC-V16-01 (no signed_digest == freeze_hash conflation; separate freeze and
probe authorization validators; injected public-key resolver; strict parsing;
key validity boundaries; duplicate key IDs), and SC-V17-01 (one shared
strict canonicalization path for signing and verification; whole key-list
validation before resolution; timezone-aware instant comparison).

Method:
- Ephemeral Ed25519 test keypairs only. Private keys never enter the
  key-list object; the key-list carries key_id/algorithm/validity/revocation
  plus an opaque key reference, and verification resolves the key_id through
  an injected public-key resolver. The whole key-list is validated
  (shape, unique IDs, pinned algorithm, ordered validity windows) before
  any key is resolved.
- Signed preimage (normative): domain || 0x00 || canonical bytes of the
  record excluding BOTH `signature` and `signed_digest`. `signed_digest` is
  SHA-256 of that preimage. Freeze binding is a separate check:
  the record's `freeze_hash` must equal the independently recomputed freeze
  digest — never an assumed equality of two digests over different data.
- Strict record parsing rejects duplicate JSON keys, floats, and non-NFC
  strings before any cryptographic check.
- Use is constrained by dedicated validators: freeze creation accepts ONLY
  decision_type FREEZE_APPROVAL; Stage C execution requires a distinct,
  signed STAGE_C_PROBE_AUTHORIZATION record bound to the same run set
  and freeze.

Exit 0 iff every positive case verifies and every forgery/misuse case fails.
"""
import base64
import copy
import hashlib
import os
import sys
from datetime import datetime, timezone

# Route signing AND verification through the single shared harness
# canonicalizer (proposal §5) — no divergent local implementation.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"
    ),
)
from investigation_agent_platform.harness import (
    canonical as _harness_canonical,
)

DOMAIN_AUTHZ = "iap:d3-stagec:owner-authorization/v5"
DOMAIN_ATTEST = "iap:d3-stagec:runtime-attestation/v4"

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def canonical_bytes_strict(obj) -> bytes:
    """Single shared canonicalization path for signing AND verification.

    Delegates to the harness package implementation (proposal §5); this
    wrapper exists only to preserve the validator's call sites.
    """
    return _harness_canonical.canonical_bytes(obj)


def canonical_bytes(obj) -> bytes:
    return canonical_bytes_strict(obj)


def strict_parse(raw: bytes) -> dict:
    """Parse record bytes via the shared harness strict parser."""
    obj = _harness_canonical.strict_parse(raw)
    if not isinstance(obj, dict):
        raise ValueError("record must be an object")
    _harness_canonical.canonical_bytes(obj)  # same strict rules as signing
    return obj


def parse_instant(s: str) -> datetime:
    """Parse an RFC 3339 / ISO-8601 timestamp. Timezone-aware required:
    naive timestamps are rejected; instants compare as UTC instants."""
    if not isinstance(s, str):
        raise ValueError("timestamp must be a string")
    text = s.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"invalid timestamp: {s!r}")
    if dt.tzinfo is None:
        raise ValueError(f"naive timestamp rejected (timezone required): {s!r}")
    return dt.astimezone(timezone.utc)


def validate_keylist(keylist: dict) -> list:
    """Validate the ENTIRE key-list artifact before any key is resolved:
    shape, unique key IDs, pinned algorithm values, ordered validity windows."""
    if not isinstance(keylist, dict) or not isinstance(keylist.get("keys"), list):
        raise ValueError("key-list must be an object with a keys array")
    seen = set()
    for entry in keylist["keys"]:
        if not isinstance(entry, dict):
            raise ValueError("key-list entry must be an object")
        kid = entry.get("key_id")
        if not isinstance(kid, str) or not kid:
            raise ValueError("key-list entry missing key_id")
        if kid in seen:
            raise ValueError(f"duplicate key_id: {kid!r}")
        seen.add(kid)
        if entry.get("algorithm") != "Ed25519":
            raise ValueError(f"unsupported algorithm for {kid!r}")
        vf, vu = entry.get("valid_from"), entry.get("valid_until")
        if not isinstance(vf, str):
            raise ValueError(f"key {kid!r} missing valid_from")
        start = parse_instant(vf)  # raises on naive/invalid
        if vu is not None:
            if not isinstance(vu, str):
                raise ValueError(f"key {kid!r} has non-string valid_until")
            end = parse_instant(vu)
            if end < start:
                raise ValueError(f"key {kid!r} validity window inverted")
        if not isinstance(entry.get("revoked"), bool):
            raise ValueError(f"key {kid!r} missing revoked flag")
        if (not isinstance(entry.get("key_reference"), str)
                or not entry["key_reference"]):
            raise ValueError(f"key {kid!r} missing key reference")
    return [k for k in keylist["keys"]]


def preimage(domain: str, record: dict) -> bytes:
    body = {k: v for k, v in record.items()
            if k not in ("signature", "signed_digest")}
    return domain.encode() + b"\x00" + canonical_bytes(body)


def payload_digest(domain: str, record: dict) -> str:
    return "sha256:" + hashlib.sha256(preimage(domain, record)).hexdigest()


class PublicKeyResolver:
    """Injected resolver: key_id -> Ed25519 public key. Private keys stay out."""

    def __init__(self):
        self._keys = {}

    def register(self, key_id: str, public_key):
        self._keys[key_id] = public_key

    def resolve(self, key_id: str):
        if key_id not in self._keys:
            raise LookupError(f"unknown key_id: {key_id}")
        return self._keys[key_id]


def valid_at(entry: dict, now: str) -> bool:
    """Instant comparison in UTC. Naive or invalid timestamps fail closed."""
    if entry.get("revoked", False):
        return False
    now_dt = parse_instant(now)
    if parse_instant(entry["valid_from"]) > now_dt:
        return False
    if entry.get("valid_until") is not None and \
            parse_instant(entry["valid_until"]) < now_dt:
        return False
    return True


def verify_record(domain: str, record: dict, keylist: dict,
                   resolver: PublicKeyResolver, now: str) -> dict:
    """Steps 0–3: whole key-list validation, digest match, signature
    verification. The record itself is re-validated through the shared
    strict canonicalization path."""
    entries = validate_keylist(keylist)
    rec = dict(record)
    canonical_bytes_strict({k: v for k, v in rec.items() if k != "signature"})
    sig = base64.b64decode(rec.pop("signature"))
    if rec.get("algorithm") != "Ed25519":
        raise ValueError("algorithm must be Ed25519")
    entries = [k for k in keylist["keys"] if k["key_id"] == rec["key_id"]]
    if len(entries) != 1:
        raise ValueError("key_id must resolve to exactly one key-list entry")
    entry = entries[0]
    if entry.get("algorithm") != "Ed25519":
        raise ValueError("algorithm mismatch")
    if not valid_at(entry, now):
        raise ValueError("key not valid at attestation/approval time")
    if rec.get("signed_digest") != payload_digest(domain, rec):
        raise ValueError("signed_digest != payload digest")
    resolver.resolve(rec["key_id"]).verify(sig, preimage(domain, rec))
    return rec


def freeze_authorization_validator(record: dict, keylist: dict,
                                   resolver: PublicKeyResolver, now: str,
                                   verified_freeze_hash: str) -> dict:
    """Freeze creation accepts ONLY a signed FREEZE_APPROVAL bound to the
    independently verified freeze digest. The signature never substitutes
    for the freeze-hash recomputation performed by the caller."""
    rec = verify_record(DOMAIN_AUTHZ, record, keylist, resolver, now)
    if rec.get("decision_type") != "FREEZE_APPROVAL":
        raise ValueError("freeze creation requires decision_type FREEZE_APPROVAL")
    if rec.get("freeze_hash") != verified_freeze_hash:
        raise ValueError("authorization freeze_hash != independently verified freeze hash")
    return rec


def probe_authorization_validator(record: dict, keylist: dict,
                                  resolver: PublicKeyResolver, now: str,
                                  run_set_id: str,
                                  verified_freeze_hash: str) -> dict:
    """Stage C execution requires a distinct signed STAGE_C_PROBE_AUTHORIZATION
    bound to the same run set and freeze. A freeze signature is never
    reusable as probe authorization."""
    rec = verify_record(DOMAIN_AUTHZ, record, keylist, resolver, now)
    if rec.get("decision_type") != "STAGE_C_PROBE_AUTHORIZATION":
        raise ValueError("probe execution requires decision_type "
                         "STAGE_C_PROBE_AUTHORIZATION")
    if rec.get("freeze_hash") != verified_freeze_hash:
        raise ValueError("probe authorization freeze_hash mismatch")
    if rec.get("run_set_id") != run_set_id:
        raise ValueError("probe authorization run-set mismatch")
    return rec


def sign_record(domain: str, record: dict, priv: Ed25519PrivateKey) -> dict:
    out = dict(record)
    out["signed_digest"] = payload_digest(domain, out)
    out["signature"] = base64.b64encode(priv.sign(preimage(domain, out))).decode()
    return out


def base_authz(h: str, freeze_hash: str, dtype: str = "FREEZE_APPROVAL") -> dict:
    return {
        "schema": "owner-authorization/v5",
        "approval_ref": "DEC-2026-10-24-A1",
        "run_set_id": "rs-001",
        "freeze_hash": freeze_hash,
        "decision_type": dtype,
        "decision_digest": h,
        "approver": "owner",
        "approved_at": "2026-10-24T00:00:00Z",
        "evidence_ref": "amendment-A1",
        "key_id": "owner-key-1",
        "signed_digest": h,  # placeholder; sign_record recomputes
        "algorithm": "Ed25519",
    }


def attestation(h: str) -> dict:
    return {
        "schema": "runtime-attestation/v4",
        "run_set_id": "rs-001",
        "freeze_hash": h,
        "wrapper_id": "exec-wrapper-1",
        "code_revision": "c" * 40,
        "tree_hash": h,
        "flags": {"IAP_SHADOW_TYPED_DEFINITIONS": "1"},
        "tenant": "probe-tenant",
        "database": "probe-db",
        "dependency_hash": h,
        "key_id": "owner-key-1",
        "signed_digest": h,
        "algorithm": "Ed25519",
        "attested_at": "2026-10-24T00:00:00Z",
    }


def main() -> int:
    h = "sha256:" + "ab" * 32
    passed = failed = 0

    def check(name, ok, detail=""):
        nonlocal passed, failed
        print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
        passed, failed = passed + ok, failed + (not ok)

    priv = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    resolver = PublicKeyResolver()
    resolver.register("owner-key-1", priv.public_key())
    resolver.register("other-key-9", other.public_key())
    keylist = {"schema": "key-list/v1", "keys": [{
        "key_id": "owner-key-1", "algorithm": "Ed25519",
        "valid_from": "2026-01-01T00:00:00Z", "valid_until": None,
        "revoked": False, "key_reference": "vault:owner-signing-key-1"}]}
    now = "2026-10-24T00:00:00Z"
    freeze_hash = "sha256:" + "cd" * 32  # independently recomputed freeze digest

    rec = sign_record(DOMAIN_AUTHZ, base_authz(h, freeze_hash), priv)
    try:
        freeze_authorization_validator(rec, keylist, resolver, now, freeze_hash)
        check("P-valid-freeze-approval verifies", True)
    except Exception as e:  # noqa: BLE001
        check("P-valid-freeze-approval verifies", False, str(e))

    for field, bad in {
            "decision_type": "STAGE_C_PROBE_AUTHORIZATION",
            "approver": "attacker",
            "run_set_id": "rs-002",
            "decision_digest": "sha256:" + "ff" * 32,
            "freeze_hash": "sha256:" + "00" * 32,
            "signed_digest": "sha256:" + "11" * 32,
            "approved_at": "2026-10-25T00:00:00Z",
            "evidence_ref": "other-evidence"}.items():
        mutated = dict(rec)
        mutated[field] = bad
        try:
            freeze_authorization_validator(mutated, keylist, resolver, now,
                                           freeze_hash)
            check(f"N-mutate-{field} rejected", False, "verified!")
        except Exception:
            check(f"N-mutate-{field} rejected", True)

    replay = dict(rec)
    replay["run_set_id"] = "rs-999"
    try:
        freeze_authorization_validator(replay, keylist, resolver, now, freeze_hash)
        check("N-cross-run-set-replay rejected", False, "verified!")
    except Exception:
        check("N-cross-run-set-replay rejected", True)

    # The signature is over the authorization payload, NOT over the freeze
    # digest: changing the independently verified freeze hash must fail the
    # freeze *binding* check even though the signature still verifies.
    try:
        freeze_authorization_validator(rec, keylist, resolver, now,
                                       "sha256:" + "99" * 32)
        check("N-invalid-freeze-binding rejected", False, "verified!")
    except Exception:
        check("N-invalid-freeze-binding rejected", True)

    # signed_digest == freeze_hash is NOT required and must NOT be assumed:
    # a record whose signed preimage digest differs from the freeze hash
    # verifies fine as a signature; binding is checked separately above.
    if rec["signed_digest"] == rec["freeze_hash"]:
        check("N-no-digest-conflation assumed", False,
              "test record accidentally equates the two digests")
    else:
        try:
            verify_record(DOMAIN_AUTHZ, rec, keylist, resolver, now)
            check("P-signed-digest-independent-of-freeze-hash", True)
        except Exception as e:  # noqa: BLE001
            check("P-signed-digest-independent-of-freeze-hash", False, str(e))

    wrong = dict(keylist)
    wrong["keys"] = [dict(keylist["keys"][0])]
    wrong["keys"][0]["key_reference"] = "vault:other"
    r2 = PublicKeyResolver()
    r2.register("owner-key-1", other.public_key())
    try:
        freeze_authorization_validator(rec, wrong, r2, now, freeze_hash)
        check("N-wrong-key rejected", False, "verified!")
    except Exception:
        check("N-wrong-key rejected", True)

    for name, kl in {
        "N-revoked-key": {"revoked": True},
        "N-expired-key": {"valid_until": "2026-01-02T00:00:00Z"},
        "N-not-yet-valid-key": {"valid_from": "2027-01-01T00:00:00Z"},
    }.items():
        mod = {"schema": "key-list/v1", "keys": [dict(keylist["keys"][0])]}
        mod["keys"][0].update(kl)
        try:
            freeze_authorization_validator(rec, mod, resolver, "2026-10-24T00:00:00Z",
                                           freeze_hash)
            check(f"{name} rejected", False, "verified!")
        except Exception:
            check(f"{name} rejected", True)

    try:
        freeze_authorization_validator(rec, {"schema": "key-list/v1", "keys": []},
                                       resolver, now, freeze_hash)
        check("N-unknown-key rejected", False, "verified!")
    except Exception:
        check("N-unknown-key rejected", True)

    dup = {"schema": "key-list/v1", "keys": [
        dict(keylist["keys"][0]), dict(keylist["keys"][0])]}
    try:
        freeze_authorization_validator(rec, dup, resolver, now, freeze_hash)
        check("N-duplicate-key-id rejected", False, "verified!")
    except Exception:
        check("N-duplicate-key-id rejected", True)

    # Boundary: exact valid_from instant is valid; one second before is not.
    edge = {"schema": "key-list/v1", "keys": [dict(keylist["keys"][0])]}
    edge["keys"][0]["valid_from"] = "2026-10-24T00:00:00Z"
    try:
        freeze_authorization_validator(rec, edge, resolver, "2026-10-24T00:00:00Z",
                                       freeze_hash)
        check("P-validity-lower-bound-inclusive", True)
    except Exception as e:  # noqa: BLE001
        check("P-validity-lower-bound-inclusive", False, str(e))
    try:
        freeze_authorization_validator(rec, edge, resolver, "2026-10-23T23:59:59Z",
                                       freeze_hash)
        check("N-before-validity rejected", False, "verified!")
    except Exception:
        check("N-before-validity rejected", True)

    # Strict parsing: duplicates, floats, non-NFC, trailing data.
    raw = canonical_bytes_str(rec)
    dup_raw = raw[:-1] + ', "extra": 1, "extra": 2}'
    for name, payload in {
        "N-duplicate-json-key": dup_raw.encode(),
        "N-float": raw.replace('"attempt": 1', '"attempt": 1.0').encode()
        if '"attempt": 1' in raw else b'{"a": 1.0}',
        "N-non-nfc": b'{"a": "e\xcc\x81"}',  # e + U+0301 UTF-8, NFD
        "N-trailing-data": (raw + ' {"trailing": true}').encode(),
    }.items():
        try:
            strict_parse(payload)
            check(f"{name} rejected", False, "parsed!")
        except Exception:
            check(f"{name} rejected", True)

    # Shared strict path: signing rejects floats / non-NFC in dicts too.
    for name, bad_rec in {
        "N-sign-float": dict(rec, evidence_ref=1.5),
        "N-sign-non-nfc": dict(rec, evidence_ref="e\u0301"),  # NFD e + combining acute
        "N-sign-non-native": dict(rec, evidence_ref=object()),
    }.items():
        try:
            sign_record(DOMAIN_AUTHZ, bad_rec, priv)
            check(f"{name} rejected", False, "signed!")
        except Exception:
            check(f"{name} rejected", True)

    # Unknown decision_type is accepted by NEITHER validator.
    weird = sign_record(DOMAIN_AUTHZ,
                        dict(base_authz(h, freeze_hash),
                             decision_type="WHATEVER"), priv)
    for vname, vfn, args in (
            ("freeze", freeze_authorization_validator, (freeze_hash,)),
            ("probe", probe_authorization_validator, ("rs-001", freeze_hash))):
        try:
            vfn(weird, keylist, resolver, now, *args)
            check(f"N-unknown-decision-type rejected by {vname}", False,
                  "verified!")
        except Exception:
            check(f"N-unknown-decision-type rejected by {vname}", True)

    # Key-list validation: shape, algorithm pin, window ordering, duplicates.
    def bad_keylist(mut):
        kl = {"schema": "key-list/v1", "keys": [dict(keylist["keys"][0])]}
        mut(kl["keys"][0])
        return kl

    keylist_cases = {
        "N-keylist-missing-key-id":
            lambda e: e.pop("key_id"),
        "N-keylist-nonstring-key-id":
            lambda e: e.update(key_id=7),
        "N-keylist-bad-algorithm":
            lambda e: e.update(algorithm="RSA"),
        "N-keylist-inverted-window":
            lambda e: e.update(valid_from="2027-01-01T00:00:00Z",
                               valid_until="2026-01-01T00:00:00Z"),
        "N-keylist-missing-revoked":
            lambda e: e.pop("revoked"),
        "N-keylist-missing-reference":
            lambda e: e.update(key_reference=""),
    }
    for name, mut in keylist_cases.items():
        try:
            kl = bad_keylist(mut)
            freeze_authorization_validator(rec, kl, resolver, now, freeze_hash)
            check(f"{name} rejected", False, "verified!")
        except Exception:
            check(f"{name} rejected", True)

    # Timestamp semantics: naive and invalid formats rejected; offsets
    # compare as instants; upper bound inclusive.
    for name, vf, vu, at, ok in [
        ("N-naive-valid-from", "2026-01-01T00:00:00", None,
         "2026-10-24T00:00:00Z", False),
        ("N-invalid-timestamp", "not-a-time", None,
         "2026-10-24T00:00:00Z", False),
        ("P-offset-equivalent-accepted", "2026-01-01T00:00:00Z", None,
         "2026-10-24T02:00:00+02:00", True),
        ("N-offset-before-start", "2026-01-01T00:00:00Z", None,
         "2025-12-31T23:59:59+00:00", False),
        ("P-upper-bound-inclusive", "2026-01-01T00:00:00Z",
         "2026-10-24T00:00:00Z", "2026-10-24T00:00:00Z", True),
        ("N-after-upper-bound", "2026-01-01T00:00:00Z",
         "2026-10-24T00:00:00Z", "2026-10-24T00:00:01Z", False),
    ]:
        kl = {"schema": "key-list/v1", "keys": [dict(keylist["keys"][0])]}
        kl["keys"][0]["valid_from"] = vf
        kl["keys"][0]["valid_until"] = vu
        try:
            freeze_authorization_validator(rec, kl, resolver, at, freeze_hash)
            check(name + (" accepted" if ok else " rejected"),
                  ok, "" if ok else "verified!")
        except Exception as e:  # noqa: BLE001
            check(name + (" rejected" if not ok else " accepted"),
                  not ok, "" if not ok else str(e))

    # Use constraint via dedicated validators (non-tautological).
    try:
        freeze_authorization_validator(rec, keylist, resolver, now, freeze_hash)
        freeze_ok = True
    except Exception:
        freeze_ok = False
    check("U-freeze-approval accepted for freeze creation", freeze_ok)
    try:
        probe_authorization_validator(rec, keylist, resolver, now, "rs-001",
                                      freeze_hash)
        check("U-freeze-record NOT usable as probe authorization", False,
              "accepted!")
    except Exception:
        check("U-freeze-record NOT usable as probe authorization", True)

    probe = base_authz(h, freeze_hash, "STAGE_C_PROBE_AUTHORIZATION")
    probe["approval_ref"] = "DEC-PROBE-001"
    probe = sign_record(DOMAIN_AUTHZ, probe, priv)
    try:
        probe_authorization_validator(probe, keylist, resolver, now, "rs-001",
                                      freeze_hash)
        check("P-distinct-probe-authorization verifies", True)
    except Exception as e:  # noqa: BLE001
        check("P-distinct-probe-authorization verifies", False, str(e))
    try:
        freeze_authorization_validator(probe, keylist, resolver, now, freeze_hash)
        check("U-probe-record NOT usable as freeze approval", False, "accepted!")
    except Exception:
        check("U-probe-record NOT usable as freeze approval", True)

    # Attestation: every runtime field bound.
    att = sign_record(DOMAIN_ATTEST, attestation(h), priv)
    try:
        verify_record(DOMAIN_ATTEST, att, keylist, resolver, now)
        check("P-attestation verifies", True)
    except Exception as e:  # noqa: BLE001
        check("P-attestation verifies", False, str(e))
    for field, bad in {"tenant": "other-tenant", "database": "other-db",
                       "wrapper_id": "other-wrapper",
                       "freeze_hash": "sha256:" + "00" * 32,
                       "code_revision": "d" * 40,
                       "flags": {"IAP_SHADOW_TYPED_DEFINITIONS": "0"}}.items():
        mutated = dict(att)
        mutated[field] = bad
        try:
            verify_record(DOMAIN_ATTEST, mutated, keylist, resolver, now)
            check(f"N-attestation-{field} rejected", False, "verified!")
        except Exception:
            check(f"N-attestation-{field} rejected", True)

    print(f"{passed} passed, {failed} failed, "
          f"validator=cryptography Ed25519 ephemeral keypairs")
    return 0 if failed == 0 else 1


def canonical_bytes_str(record: dict) -> str:
    return _harness_canonical.canonical_bytes(
        {k: v for k, v in record.items() if k != "signature"}
    ).decode("utf-8")


if __name__ == "__main__":
    sys.exit(main())
