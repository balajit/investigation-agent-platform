"""Freeze validation and trust (§7): preimages, key list, signatures."""

from __future__ import annotations

from pathlib import Path

import pytest

from investigation_agent_platform.harness.canonical import (
    canonical_bytes,
    sha256_digest,
)
from investigation_agent_platform.harness.freeze import (
    entry_valid_at,
    freeze_authorization_validator,
    freeze_digest,
    generate_dev_keypair,
    parse_instant,
    probe_authorization_validator,
    resolve_key,
    sign_record,
    validate_keylist,
    verify_freeze_self_hash,
    verify_record_signature,
    verify_rule_preimage,
)
from investigation_agent_platform.harness.registry import HarnessValidationError, load_registry

DOMAIN = "iap:d3-stagec:owner-authorization/v5"


def _keylist(key_id="owner-key-1", **overrides):
    entry = {
        "key_id": key_id,
        "algorithm": "Ed25519",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_until": None,
        "revoked": False,
        "key_reference": "vault:dev-1",
    }
    entry.update(overrides)
    return {"schema": "key-list/v1", "keys": [entry]}


def _authz(freeze_hash, dtype="FREEZE_APPROVAL", **overrides):
    rec = {
        "schema": "owner-authorization/v5",
        "approval_ref": "DEC-DEV-001",
        "run_set_id": "rs-dev-001",
        "freeze_hash": freeze_hash,
        "decision_type": dtype,
        "decision_digest": "sha256:" + "ab" * 32,
        "approver": "owner",
        "approved_at": "2026-10-24T00:00:00Z",
        "evidence_ref": "dev-evidence",
        "key_id": "owner-key-1",
        "algorithm": "Ed25519",
    }
    rec.update(overrides)
    return rec


DOCS = Path(__file__).resolve().parents[2] / "docs"


def _registry():
    return load_registry(DOCS / "IAP-proof-kernel-d3-stageC-harness-schemas-v1.13.json")


def _vectors():
    import json

    return json.loads(
        (DOCS / "IAP-proof-kernel-d3-stageC-schema-vectors-v1.15.json").read_text(encoding="utf-8")
    )


_REGISTRY = None


def _reg():
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _registry()
    return _REGISTRY


def _signed_fixture(dtype="FREEZE_APPROVAL"):
    import copy

    vec = _vectors()
    base = next(v["instance"] for v in vec["vectors"] if v["id"] == "P-freeze")
    priv, pub = generate_dev_keypair()
    keylist = _keylist()
    keylist_digest = sha256_digest(canonical_bytes(keylist))
    freeze = copy.deepcopy(base)
    freeze["run_set_id"] = "rs-dev-001"
    freeze["key_list_ref"] = {"path": "artifacts/keylist.json", "digest": keylist_digest}
    digest = freeze_digest(freeze)
    freeze["freeze_hash"] = digest
    rec = sign_record(DOMAIN, _authz(digest, dtype), priv)
    resolver = lambda kid: pub
    return freeze, digest, keylist, keylist_digest, rec, resolver, priv, pub


def test_freeze_digest_excludes_self_hash() -> None:
    body = {"schema": "freeze/v4", "run_set_id": "r"}
    assert freeze_digest({**body, "freeze_hash": "sha256:" + "00" * 32}) == freeze_digest(
        {**body, "freeze_hash": "sha256:" + "ff" * 32}
    )


def test_verify_freeze_self_hash() -> None:
    freeze = {"a": 1}
    freeze["freeze_hash"] = freeze_digest(freeze)
    verify_freeze_self_hash(freeze)
    with pytest.raises(HarnessValidationError, match="freeze_hash"):
        verify_freeze_self_hash({**freeze, "freeze_hash": "sha256:" + "00" * 32})


def test_verify_rule_preimage() -> None:
    blob = b"rule bytes"
    verify_rule_preimage(blob, {"path": "p", "digest": sha256_digest(blob)})
    with pytest.raises(HarnessValidationError, match="preimage digest mismatch"):
        verify_rule_preimage(b"other", {"path": "p", "digest": sha256_digest(blob)})


def test_validate_keylist_rejects_duplicates() -> None:
    keylist = {"schema": "key-list/v1", "keys": [_keylist()["keys"][0], _keylist()["keys"][0]]}
    with pytest.raises(HarnessValidationError, match="duplicate key_id"):
        validate_keylist(keylist)


def test_validate_keylist_rejects_wrong_algorithm() -> None:
    with pytest.raises(HarnessValidationError, match="algorithm must be Ed25519"):
        validate_keylist(_keylist(algorithm="RSA"))


def test_validate_keylist_rejects_inverted_window() -> None:
    with pytest.raises(HarnessValidationError, match="validity window inverted"):
        validate_keylist(
            _keylist(valid_from="2026-06-01T00:00:00Z", valid_until="2026-01-01T00:00:00Z")
        )


def test_parse_instant_rejects_naive() -> None:
    with pytest.raises(HarnessValidationError, match="naive"):
        parse_instant("2026-10-24T00:00:00")
    assert parse_instant("2026-10-24T00:00:00Z").utcoffset() is not None


def test_parse_instant_rejects_lowercase_z() -> None:
    with pytest.raises(HarnessValidationError, match="invalid timestamp"):
        parse_instant("2026-10-24T00:00:00z")


def test_entry_validity_boundaries() -> None:
    entry = _keylist(valid_from="2026-10-24T00:00:00Z", valid_until="2026-10-25T00:00:00Z")["keys"][
        0
    ]
    assert entry_valid_at(entry, "2026-10-24T00:00:00Z") is True
    assert entry_valid_at(entry, "2026-10-25T00:00:00Z") is True
    assert entry_valid_at(entry, "2026-10-26T00:00:00Z") is False
    assert entry_valid_at(entry, "2026-10-23T00:00:00Z") is False
    assert entry_valid_at({**entry, "revoked": True}, "2026-10-24T00:00:00Z") is False


def test_resolve_key_rejects_unknown() -> None:
    entries = validate_keylist(_keylist())
    with pytest.raises(HarnessValidationError, match="exactly one"):
        resolve_key(entries, "ghost", lambda kid: None)


def test_chain_resolves_every_listed_key() -> None:
    freeze, digest, keylist, _, rec, resolver, priv, pub = _signed_fixture()
    entries = _keylist()["keys"] + [_keylist(key_id="owner-key-2")["keys"][0]]
    keylist_two = {"schema": "key-list/v1", "keys": entries}
    keylist_digest = sha256_digest(canonical_bytes(keylist_two))
    freeze = dict(freeze)
    freeze["key_list_ref"] = {"path": "artifacts/keylist.json", "digest": keylist_digest}
    freeze["freeze_hash"] = freeze_digest(freeze)
    rec = sign_record(DOMAIN, _authz(freeze["freeze_hash"]), priv)

    def partial_resolver(kid):
        if kid != "owner-key-1":
            raise LookupError(kid)
        return pub

    with pytest.raises(HarnessValidationError, match="unknown key_id"):
        freeze_authorization_validator(
            record=rec,
            freeze=freeze,
            keylist=keylist_two,
            resolver=partial_resolver,
            registry=_reg(),
        )


def test_sign_verify_round_trip_and_mutation() -> None:
    freeze, digest, keylist, keylist_digest, rec, resolver, priv, pub = _signed_fixture()
    verify_record_signature(DOMAIN, rec, pub)
    mutated = dict(rec, approver="attacker")
    with pytest.raises(HarnessValidationError):
        verify_record_signature(DOMAIN, mutated, pub)


def test_verify_rejects_wrong_key_and_digest_tamper() -> None:
    freeze, digest, keylist, keylist_digest, rec, resolver, priv, pub = _signed_fixture()
    _, other_pub = generate_dev_keypair()
    with pytest.raises(HarnessValidationError):
        verify_record_signature(DOMAIN, rec, other_pub)
    tampered = dict(rec, signed_digest="sha256:" + "00" * 32)
    with pytest.raises(HarnessValidationError, match="signed_digest"):
        verify_record_signature(DOMAIN, tampered, pub)


def test_freeze_validator_accepts_approval_rejects_probe_type() -> None:
    freeze, digest, keylist, keylist_digest, rec, resolver, priv, pub = _signed_fixture()
    freeze_authorization_validator(
        record=rec, freeze=freeze, keylist=keylist, resolver=resolver, registry=_reg()
    )
    probe_rec = sign_record(DOMAIN, _authz(digest, "STAGE_C_PROBE_AUTHORIZATION"), priv)
    with pytest.raises(HarnessValidationError, match="not permitted"):
        freeze_authorization_validator(
            record=probe_rec, freeze=freeze, keylist=keylist, resolver=resolver, registry=_reg()
        )


def test_probe_validator_binds_run_set_and_freeze() -> None:
    freeze, digest, keylist, keylist_digest, rec, resolver, priv, pub = _signed_fixture(
        "STAGE_C_PROBE_AUTHORIZATION"
    )
    probe_authorization_validator(
        record=rec,
        run_set_id="rs-dev-001",
        freeze=freeze,
        keylist=keylist,
        resolver=resolver,
        registry=_reg(),
    )
    with pytest.raises(HarnessValidationError, match="run_set_id"):
        probe_authorization_validator(
            record=rec,
            run_set_id="rs-other",
            freeze=freeze,
            keylist=keylist,
            resolver=resolver,
            registry=_reg(),
        )
    freeze_rec = sign_record(DOMAIN, _authz(digest, "FREEZE_APPROVAL"), priv)
    with pytest.raises(HarnessValidationError, match="not permitted"):
        probe_authorization_validator(
            record=freeze_rec,
            run_set_id="rs-dev-001",
            freeze=freeze,
            keylist=keylist,
            resolver=resolver,
            registry=_reg(),
        )


def test_freeze_binding_mismatch_rejected() -> None:
    freeze, digest, keylist, keylist_digest, rec, resolver, priv, pub = _signed_fixture()
    with pytest.raises(HarnessValidationError, match="freeze_hash"):
        freeze_authorization_validator(
            record=rec,
            freeze={**freeze, "freeze_hash": "sha256:" + "00" * 32},
            keylist=keylist,
            resolver=resolver,
            registry=_reg(),
        )


def test_naive_approval_instant_rejected() -> None:
    freeze, digest, keylist, keylist_digest, rec, resolver, priv, pub = _signed_fixture()
    rec["approved_at"] = "2026-10-24T00:00:00"
    with pytest.raises(HarnessValidationError, match="naive"):
        freeze_authorization_validator(
            record=rec, freeze=freeze, keylist=keylist, resolver=resolver, registry=_reg()
        )


def test_non_utc_approval_offset_rejected() -> None:
    freeze, digest, keylist, keylist_digest, rec, resolver, priv, pub = _signed_fixture()
    rec["approved_at"] = "2026-10-24T02:00:00+02:00"
    with pytest.raises(HarnessValidationError, match="non-UTC offset"):
        freeze_authorization_validator(
            record=rec, freeze=freeze, keylist=keylist, resolver=resolver, registry=_reg()
        )


def test_non_utc_validity_window_rejected() -> None:
    with pytest.raises(HarnessValidationError, match="non-UTC offset"):
        validate_keylist(_keylist(valid_from="2026-01-01T05:30:00+05:30"))


def test_revoked_key_rejected() -> None:
    import copy

    vec = _vectors()
    base = next(v["instance"] for v in vec["vectors"] if v["id"] == "P-freeze")
    priv, pub = generate_dev_keypair()
    keylist = _keylist(revoked=True)
    keylist_digest = sha256_digest(canonical_bytes(keylist))
    freeze = copy.deepcopy(base)
    freeze["run_set_id"] = "rs-dev-001"
    freeze["key_list_ref"] = {"path": "artifacts/keylist.json", "digest": keylist_digest}
    freeze["freeze_hash"] = freeze_digest(freeze)
    rec = sign_record(DOMAIN, _authz(freeze["freeze_hash"]), priv)
    with pytest.raises(HarnessValidationError, match="not valid"):
        freeze_authorization_validator(
            record=rec, freeze=freeze, keylist=keylist, resolver=lambda kid: pub, registry=_reg()
        )


def test_caller_supplied_freeze_context_ignored() -> None:
    # The validator derives digests from the freeze; a record bound to a
    # different freeze cannot be smuggled in via caller-supplied values.
    freeze, digest, keylist, _, rec, resolver, priv, pub = _signed_fixture()
    other = dict(freeze, harness_version="tampered-wrapper")
    other["freeze_hash"] = freeze_digest(other)
    with pytest.raises(HarnessValidationError, match="freeze_hash"):
        freeze_authorization_validator(
            record=rec, freeze=other, keylist=keylist, resolver=resolver, registry=_reg()
        )


def test_keylist_not_matching_ref_rejected() -> None:
    freeze, digest, keylist, _, rec, resolver, priv, pub = _signed_fixture()
    with pytest.raises(HarnessValidationError, match="key_list_ref"):
        freeze_authorization_validator(
            record=rec,
            freeze=freeze,
            keylist=_keylist(key_id="other-key"),
            resolver=resolver,
            registry=_reg(),
        )


def test_strict_base64_signature_rejected() -> None:
    freeze, digest, keylist, _, rec, resolver, priv, pub = _signed_fixture()
    bad = dict(rec)
    bad["signature"] = rec["signature"][:-2] + "\nX"
    with pytest.raises(HarnessValidationError, match="strict standard base64"):
        freeze_authorization_validator(
            record=bad, freeze=freeze, keylist=keylist, resolver=resolver, registry=_reg()
        )
    bad_pad = dict(rec)
    bad_pad["signature"] = rec["signature"].rstrip("=")
    with pytest.raises(HarnessValidationError, match="strict standard base64"):
        freeze_authorization_validator(
            record=bad_pad, freeze=freeze, keylist=keylist, resolver=resolver, registry=_reg()
        )


ATT_DOMAIN = "iap:d3-stagec:runtime-attestation/v4"


def _attestation(freeze_hash, **overrides):
    rec = {
        "schema": "runtime-attestation/v4",
        "run_set_id": "rs-dev-001",
        "freeze_hash": freeze_hash,
        "wrapper_id": "dev-wrapper-1",
        "code_revision": "c" * 40,
        "tree_hash": "sha256:" + "aa" * 32,
        "flags": {"IAP_SHADOW_TYPED_DEFINITIONS": "1"},
        "tenant": "dev-tenant",
        "database": "dev-db",
        "dependency_hash": "sha256:" + "bb" * 32,
        "key_id": "owner-key-1",
        "algorithm": "Ed25519",
        "attested_at": "2026-10-24T00:00:00Z",
    }
    rec.update(overrides)
    return rec


def test_attestation_validator_accepts_and_binds() -> None:
    freeze, digest, keylist, _, _, _, priv, pub = _signed_fixture()
    rec = sign_record(ATT_DOMAIN, _attestation(digest), priv)
    from investigation_agent_platform.harness.freeze import attestation_validator

    attestation_validator(
        record=rec, freeze=freeze, keylist=keylist, resolver=lambda kid: pub, registry=_reg()
    )
    wrong_run = sign_record(ATT_DOMAIN, _attestation(digest, run_set_id="rs-other"), priv)
    with pytest.raises(HarnessValidationError, match="run_set_id"):
        attestation_validator(
            record=wrong_run,
            freeze=freeze,
            keylist=keylist,
            resolver=lambda kid: pub,
            registry=_reg(),
        )
    with pytest.raises(HarnessValidationError, match="decision_type"):
        attestation_validator(
            record=sign_record(ATT_DOMAIN, _attestation(digest, decision_type="X"), priv),
            freeze=freeze,
            keylist=keylist,
            resolver=lambda kid: pub,
            registry=_reg(),
        )


def test_attestation_not_usable_as_freeze_approval() -> None:
    freeze, digest, keylist, _, _, _, priv, pub = _signed_fixture()
    rec = sign_record(ATT_DOMAIN, _attestation(digest), priv)
    with pytest.raises(HarnessValidationError):
        freeze_authorization_validator(
            record=rec, freeze=freeze, keylist=keylist, resolver=lambda kid: pub, registry=_reg()
        )


def test_freeze_approval_rejects_other_run_set() -> None:
    freeze, digest, keylist, _, rec, resolver, priv, pub = _signed_fixture()
    other_run = sign_record(DOMAIN, _authz(digest, run_set_id="rs-other"), priv)
    with pytest.raises(HarnessValidationError, match="run_set_id"):
        freeze_authorization_validator(
            record=other_run, freeze=freeze, keylist=keylist, resolver=resolver, registry=_reg()
        )


def test_probe_rejects_freeze_run_set_mismatch() -> None:
    import copy

    freeze, digest, keylist, _, rec, resolver, priv, pub = _signed_fixture(
        "STAGE_C_PROBE_AUTHORIZATION"
    )
    other_freeze = copy.deepcopy(freeze)
    other_freeze["run_set_id"] = "rs-other"
    other_freeze["freeze_hash"] = freeze_digest(other_freeze)
    with pytest.raises(HarnessValidationError, match="run_set_id"):
        probe_authorization_validator(
            record=rec,
            run_set_id="rs-dev-001",
            freeze=other_freeze,
            keylist=keylist,
            resolver=resolver,
            registry=_reg(),
        )


def test_boundary_rejects_unshaped_freeze() -> None:
    freeze, digest, keylist, _, rec, resolver, priv, pub = _signed_fixture()
    thin = {"schema": "freeze/v4", "run_set_id": "rs-dev-001"}
    with pytest.raises(HarnessValidationError, match="schema violation"):
        freeze_authorization_validator(
            record=rec, freeze=thin, keylist=keylist, resolver=resolver, registry=_reg()
        )


def test_boundary_rejects_unshaped_record_and_keylist() -> None:
    freeze, digest, keylist, _, rec, resolver, priv, pub = _signed_fixture()
    bad_rec = dict(rec, extra_field="x")
    with pytest.raises(HarnessValidationError, match="schema violation"):
        freeze_authorization_validator(
            record=bad_rec, freeze=freeze, keylist=keylist, resolver=resolver, registry=_reg()
        )
    bad_keys = {"schema": "key-list/v1", "keys": [dict(_keylist()["keys"][0], extra_field="x")]}
    with pytest.raises(HarnessValidationError, match="schema violation"):
        freeze_authorization_validator(
            record=rec, freeze=freeze, keylist=bad_keys, resolver=resolver, registry=_reg()
        )
