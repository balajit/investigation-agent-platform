# Design: Real-World Build-Source Mapping Authority (B3) — PROVISIONAL, DEFERRED

> **Status (2026-10-28, owner decision): DEFERRED.** This design is preserved as a reference for *how* independent build-mapping attestation would work *if* it is ever required. It is **not approved for implementation**: no named consumer or trust boundary currently requires independently verifiable build mapping — IAP investigates its own tenants' code with provider-stamped revisions and honest abstention, and nothing produced is consumed or published anywhere the platform cannot already vouch for. Reopen only on a specific trigger: a named consumer (e.g., customer audit exports must be tamper-evident) or a specific threat (e.g., a mapping provider that must be distrusted). A programmatic "real-world mapping authority" is explicitly out of scope until then. The synthetic test-only authority stands unchanged.

## Context

`BUILD_SOURCE_MAPPING` premises carry `build_identity`, `repository`, `source_revision`, `provenance_ref`, and `mapping_digest`. The rule discharges only when the mapping binds the observed build and `provenance_ref` is in the frozen `APPROVED_BUILD_MAPPING_AUTHORITIES` set; the checker mirrors this through its `mapping_authority.approved_provenance_refs` config. That set contains exactly one member: `synthetic-test-only-authority-001`, approved 2026-10-24 as TEST-ONLY scaffolding with fictional trust assumptions (Amendment A2). Real-world mapping authority is explicitly OPEN, and BEHAVIOR discharge is testable on synthetic vectors only.

This design closes B3: a CI/registry-backed attestation that real builds can carry, verified offline by the kernel, with key rotation, revocation, and a migration path that never weakens the synthetic-only present. No production behavior changes until a separate rollout authorization; this design is the contract + verification logic + registry format.

No `docs/dev/architecture.md` exists in the project.

## References

- `docs/IAP-proof-kernel-g4-component-2-design-v1.0.md` — §D2 states the open authority requirement and the test-only synthetic admission; this design answers it.
- `src/investigation_agent_platform/domain/proof/premises.py` — `BuildSourceMapping` premise shape extended here (optional signature fields only).
- `src/investigation_agent_platform/domain/proof/rules.py` — Producer-side `validate_behavior_premises` authority gate (`APPROVED_BUILD_MAPPING_AUTHORITIES`, bind checks).
- `src/investigation_agent_platform/domain/proof/checker_semantics.py` — Checker-side mirror via `mapping_authority.approved_provenance_refs` config.
- `docs/IAP-proof-kernel-traceability-v1.0.md` — K-scope and A/C/F rationale; C2-R1 carries this exact open item.

## Goals / Non-Goals

**Goals:**

- Real CI-minted build attestations verifiable offline by the kernel (no network in the decision path).
- Frozen authority + key registry, append-only by reviewed contract change (same discipline as the existing frozenset).
- Fail-closed semantics for every non-conforming case, reusing the existing outcome vocabulary (no new outcomes).
- Rotation and revocation without flag days: retired keys abstain, never falsely deny or accept.
- Migration path that keeps synthetic vectors green and production premises honest.
- A closed synthetic-in-production hole: a fabricated synthetic ref must not discharge in production.

**Non-Goals:**

- Production wiring or rollout (B2 track); this design authorizes no authoritative use.
- Runtime behavior attestation (only build→source mapping, never execution claims).
- A new PKI or transparency log — Ed25519 with a frozen key registry, matching the repo's existing frozen-key/digest precedent (decision digests, validator fingerprints, D3 frozen key list).
- CAUSAL or other rule kinds; authority applies to `BUILD_SOURCE_MAPPING` premises only.

## Decisions

### D1: Authority identity stays opaque; metadata lives in a frozen registry

**Decision:** Authority IDs remain opaque short strings in the frozen membership set (zero churn to the hot-path check). All other metadata lives in a frozen JSON registry fixture keyed by authority ID: owner, status (`active`|`retired`), key versions with public keys, and a `requires_signature` flag. The synthetic entry stays with `requires_signature: false`; every real authority has it `true`.

```python
# src/investigation_agent_platform/domain/proof/mapping_authorities.py
"""Frozen build-source mapping authority registry (B3).

Append-only by reviewed contract change, same discipline as
APPROVED_BUILD_MAPPING_AUTHORITIES in rules.py. The kernel never
fetches, resolves, or negotiates authority: membership is a set
lookup, signatures verify against frozen bytes.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AuthorityKey(BaseModel):
    """One verification key version for an authority."""

    model_config = ConfigDict(frozen=True)

    key_version: str = Field(description="Opaque key version, e.g. 'k1'. Rotations add versions; never reuse a version string.")
    public_key_hex: str = Field(description="Ed25519 public key, 64 hex chars (32 bytes).")
    status: str = Field(description="One of: active, retired. Retired keys abstain, never deny.")


class MappingAuthority(BaseModel):
    """One admitted build-source mapping authority."""

    model_config = ConfigDict(frozen=True)

    authority_id: str = Field(description="Opaque ID, e.g. 'synthetic-test-only-authority-001' or 'ci:acme:release-pipeline'.")
    owner: str = Field(description="Human/owner identity accountable for the minting pipeline, e.g. team + contact.")
    status: str = Field(description="One of: active, retired. Retired authorities abstain on every premise.")
    requires_signature: bool = Field(description="True for real authorities; False only for the synthetic test-only authority.")
    keys: list[AuthorityKey] = Field(default_factory=list, description="Verification keys. Empty exactly when requires_signature is False.")


# Frozen registry. Updated only by reviewed contract change, in the same
# commit that touches APPROVED_BUILD_MAPPING_AUTHORITIES. The synthetic
# entry is the current registry in full.
MAPPING_AUTHORITY_REGISTRY: dict[str, MappingAuthority] = {}
```

The registry starts containing only the synthetic entry. Real authorities arrive via reviewed contract changes that add the ID to both the frozenset and this registry in one commit.

**Alternative considered:** Structured authority IDs encoding owner/pipeline (`ci:acme:release`). Rejected — parsing IDs in the hot path invites canonicalization bugs; opaque IDs with sidecar metadata keep the check a set lookup.

**Rationale:** The existing check (`provenance_ref not in APPROVED...`) stays byte-identical for every current input. All new semantics key off registry metadata, so the synthetic path cannot change behavior.

**Trade-offs:** Two structures must be updated together (frozenset + registry). The differential test suite pins their agreement, and review discipline (same-commit rule, stated below) makes divergence visible.

---

### D2: Attestation envelope rides in optional premise fields

**Decision:** CI mints one attestation per build mapping: canonical bytes of `(authority_id, build_identity, repository, source_revision, mapping_digest)`, signed with the authority's current key version. The premise carries two new OPTIONAL fields — `key_version` and `signature_hex` — so every existing fixture still validates (`extra="forbid"` is undisturbed; optional declared fields validate fine). The checker recomputes the canonical bytes from the premise itself, never trusting a transmitted digest of them.

```python
# src/investigation_agent_platform/domain/proof/premises.py (extension)
class BuildSourceMapping(PremiseBase):
    """Closed build→source mapping premise (existing fields unchanged)."""

    premise_type: Literal["BUILD_SOURCE_MAPPING"]
    build_identity: NonEmpty
    repository: NonEmpty
    source_revision: FullSha
    provenance_ref: NonEmpty
    mapping_digest: Sha256
    key_version: str | None = Field(
        default=None,
        description="Authority key version that signed this attestation, e.g. 'k1'. Required when the authority requires signatures; must be absent otherwise.",
    )
    signature_hex: str | None = Field(
        default=None,
        description="Ed25519 signature (128 hex chars) over the canonical attestation bytes. Required when the authority requires signatures; must be absent otherwise.",
    )
```

Canonical attestation bytes (exact, both sides implement byte-identically):

```python
# src/investigation_agent_platform/domain/proof/mapping_authorities.py
def attestation_canonical_bytes(
    authority_id: str,
    build_identity: str,
    repository: str,
    source_revision: str,
    mapping_digest: str,
) -> bytes:
    """Canonical bytes covered by an attestation signature.

    Field order and separators are fixed; no JSON (key order risk), no
    whitespace tolerance. Both CI minting and kernel verification must
    produce these exact bytes.
    """
    parts = [authority_id, build_identity, repository, source_revision, mapping_digest]
    return b"\x00".join(p.encode("utf-8") for p in parts)
```

**Alternative considered:** Signature over the whole premise JSON. Rejected — JSON canonicalization is a second standard to pin; the five-field join is auditable in one glance.

**Rationale:** Optional fields preserve every golden vector untouched; signature presence rules (D3) make mixed old/new inputs fail closed in the documented direction.

**Trade-offs:** Premise schema grows two fields; fixture schema + golden vectors gain negative cases (missing/invalid signature). The fingerprint test will flag the source change, which is intended.

---

### D3: Offline verification with a closed outcome mapping

**Decision:** Both the producer validator and the checker implement the same decision table, in the same order. No new outcomes: every row maps to `DISCHARGED`, `INVALID_INFERENCE`, or `UNSUPPORTED_CONSTRUCT`.

```python
# src/investigation_agent_platform/domain/proof/mapping_authorities.py
from enum import Enum


class AttestationVerdict(str, Enum):
    DISCHARGED = "DISCHARGED"
    INVALID_INFERENCE = "INVALID_INFERENCE"
    UNSUPPORTED_CONSTRUCT = "UNSUPPORTED_CONSTRUCT"


def verify_mapping_attestation(
    *,
    authority_id: str,
    build_identity: str,
    repository: str,
    source_revision: str,
    mapping_digest: str,
    key_version: str | None,
    signature_hex: str | None,
    registry: dict[str, MappingAuthority],
) -> tuple[AttestationVerdict, str]:
    """Pure offline attestation check. No I/O, no clock, no network."""
    import hashlib
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    authority = registry.get(authority_id)
    if authority is None or authority.status != "active":
        return (AttestationVerdict.UNSUPPORTED_CONSTRUCT, "mapping authority not approved")
    if not authority.requires_signature:
        # Synthetic test-only path: membership is the whole check, exactly as today.
        return (AttestationVerdict.DISCHARGED, "test-only authority admitted by membership")
    if not key_version or not signature_hex:
        return (AttestationVerdict.UNSUPPORTED_CONSTRUCT, "signature required but absent")
    key = next((k for k in authority.keys if k.key_version == key_version), None)
    if key is None:
        return (AttestationVerdict.UNSUPPORTED_CONSTRUCT, "unknown key version")
    if key.status != "active":
        # Retired keys abstain: the mapping may be genuine but is no
        # longer trusted. Never deny (that would assert falsehood).
        return (AttestationVerdict.UNSUPPORTED_CONSTRUCT, "key version retired")
    try:
        signature = bytes.fromhex(signature_hex)
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(key.public_key_hex))
        public_key.verify(
            signature,
            attestation_canonical_bytes(
                authority_id, build_identity, repository, source_revision, mapping_digest
            ),
        )
    except (ValueError, InvalidSignature):
        return (AttestationVerdict.INVALID_INFERENCE, "attestation signature invalid")
    return (AttestationVerdict.DISCHARGED, "code, runtime, and attested mapping join on the asserted behavior")
```

The existing bind checks (build/repository/revision match against runtime) run before this function and keep their outcomes; a bind mismatch stays `INVALID_INFERENCE` without consulting keys.

**Alternative considered:** New outcomes for crypto states (e.g., `UNTRUSTED_KEY`). Rejected — the closed vocabulary is frozen; `UNSUPPORTED_CONSTRUCT` already means "cannot decide in favor", which is exactly the retired/unknown-key semantics.

**Rationale:** Every failure mode lands in an existing bucket with the honest direction (abstain vs deny). The retired-key row is the subtle one: revocation must withdraw trust, not manufacture a negative proof.

**Trade-offs:** Ed25519 adds a `cryptography`-package dependency to the pure domain layer (already used in D3 signature checks per the traceability record, so no new dependency class).

---

### D4: Trust anchors, rotation, revocation

**Decision:** Public keys enter the frozen registry only via reviewed contract change that names the owner, the CI pipeline audited, and the key-generation ceremony (who generated, where, how the private half is held). Rotation adds a new `key_version` under the same authority ID; the old version moves to `retired` in the same commit once the pipeline has cut over (overlap window stated in the change). Emergency revocation is a contract change that flips a version to `retired` — effective at merge, no deployment needed since verification reads frozen bytes. Retired versions are never deleted (audit trail) and never verify (abstain, per D3).

**Alternative considered:** Online revocation list / OCSP-style freshness. Rejected — the kernel is offline by design; freshness checks would reintroduce network trust into the decision path.

**Rationale:** Rotation without flag days: during overlap both versions verify; after cutover the old one abstains. Nothing ever flips from accept to deny except an invalid signature, which is a forgery signal, not a staleness signal.

**Trade-offs:** Revocation latency equals contract-change latency. Acceptable: revocation withdraws trust (abstain), and the blast radius of a stale key is bounded by the fact that every premise still needs build/repo/revision binding plus a valid signature.

---

### D5: Synthetic authority containment (closes the fabricated-ref hole)

**Decision:** Today the synthetic ID sits in the global frozen set, so a production premise citing it would discharge. The producer's approved set becomes configuration (the same `mapping_authority`-style config the checker already takes), defaulting to the frozen set for tests/dev; production configuration excludes every `synthetic-*` authority. A startup validation test asserts the production config contains no synthetic IDs. The checker path already takes its set from config — no change needed there beyond the same exclusion.

**Alternative considered:** Removing synthetic from the set entirely and special-casing tests. Rejected — golden vectors pin the synthetic path; special-casing the producer by caller would be worse than config-driven sets.

**Rationale:** Admission policy is deployment data, not verifier semantics: the rule behaves identically given a set, and tests pin the test set while production pins the production set. This also resolves the "never env-switched" concern — nothing branches on environment, only on the passed-in set.

**Trade-offs:** A misconfigured production set containing synthetic would silently re-open the hole. Mitigation: the startup test fails closed (refuses to boot the verifier lane), plus the deployment checklist names the exclusion explicitly.

---

### D6: CI minting and registry hosting (operations interface)

**Decision:** CI emits one attestation artifact per build, stored beside build artifacts and referenced by investigations at retrieval time (retrieval populates `key_version`/`signature_hex` into the premise from the artifact; that retrieval-side change is specified here, implemented as B3-impl):
- Artifact: JSON with `authority_id`, `build_identity`, `repository`, `source_revision`, `mapping_digest`, `key_version`, `signature_hex`, `minted_at`.
- The `mapping_digest` is the SHA-256 of the mapping bytes the build publishes (same digest the premise already carries).
- Registry hosting: the frozen registry fixture lives in-repo (`tests/fixtures/proof_kernel/mapping_authorities_v1.json`, mirroring `validator_fingerprints_v1.json` precedent); operational distribution reuses the existing fixture/ledger distribution — no new service.
- Key generation ceremony and private-half custody are owner/ops decisions recorded in the contract-change request, not in this design beyond requiring them to be stated.

**Alternative considered:** A live attestation service queried at verification time. Rejected — violates kernel offline-ness; the whole point is verification from frozen bytes.

**Rationale:** Everything the kernel needs arrives in the premise or the frozen registry. Operations owns minting, storage, and custody; the kernel owns verification. The interface between them is the artifact schema, pinned by vectors.

**Trade-offs:** Artifact availability is an ops concern: a missing artifact yields `UNSUPPORTED_CONSTRUCT` (honest abstention), never a fabricated mapping.

## Data Storage

No new database tables in v1. The authority registry is a frozen JSON fixture (`tests/fixtures/proof_kernel/mapping_authorities_v1.json`), updated only by reviewed contract change — consistent with the `validator_fingerprints` / fixture-schema precedent. If a future phase needs operational registry reads (rotation automation), that storage is a separate design; the kernel continues to verify from frozen bytes regardless of where ops stages them.

Structural placeholder note: none — nothing here is a placeholder; the registry fixture is the mechanism.

## Data Structures

Input schema (CI-emitted attestation artifact — new file, exact):

```python
# docs/IAP-proof-kernel-mapping-authority-design — schema reference (implemented as JSON Schema in fixtures)
# attestation artifact, version "mapping-attestation/v1"
{
    "format": "mapping-attestation/v1",
    "authority_id": "ci:acme:release-pipeline",
    "build_identity": "release-2026-10-31+abc123",
    "repository": "acme/service",
    "source_revision": "54d73240d6b5f039bcaf0e0c4e1cdc31be9fd",
    "mapping_digest": "sha256:…64 hex…",
    "key_version": "k1",
    "signature_hex": "…128 hex…",
    "minted_at": "2026-10-31T00:00:00Z"
}
```

Registry entry schema (frozen fixture):

```python
# mapping_authorities_v1.json entry
{
    "authority_id": "ci:acme:release-pipeline",
    "owner": "Acme Release Engineering <releng@acme.example>",
    "status": "active",
    "requires_signature": True,
    "keys": [
        {"key_version": "k1", "public_key_hex": "…64 hex…", "status": "active"}
    ]
}
```

Premise extension (output side — the `BuildSourceMapping` fields from D2, optional, backward compatible).

Verification outcomes reuse `RuleOutcome` / `CheckerOutcome` (`DISCHARGED`, `INVALID_INFERENCE`, `UNSUPPORTED_CONSTRUCT`) — no new codes.

## Interfaces

No REST, MCP, TUI, or GUI changes. Two operator surfaces:

### CLI Commands

| Command | Arguments | Description |
| ------- | --------- | ----------- |
| `check-attestation` | `--artifact PATH [--registry PATH]` | Offline verify one attestation artifact against the frozen registry; prints DISCHARGED / UNSUPPORTED_CONSTRUCT / INVALID_INFERENCE with reason; exit 0 on DISCHARGED, 2 otherwise |

```python
# scripts/check_mapping_attestation.py
"""Offline attestation check (B3 ops aid, stdlib + cryptography only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Verify a mapping attestation offline.")
    parser.add_argument("--artifact", required=True, help="Path to attestation artifact JSON.")
    parser.add_argument(
        "--registry",
        default="tests/fixtures/proof_kernel/mapping_authorities_v1.json",
        help="Path to frozen authority registry.",
    )
    args = parser.parse_args(argv[1:])
    _check(args.artifact, args.registry)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

(The `_check` body loads both files, calls the kernel's `verify_mapping_attestation`, and prints the verdict; full body at implementation. Exit codes: 0 DISCHARGED, 2 otherwise, 1 on malformed input.)

### Accessibility

No interactive user interfaces are added or changed. The CLI follows standard conventions: `--help` text for every flag, machine-readable exit codes (0/1/2 as above), and errors that name the file and the failing check rather than bare codes. No color-dependent output.

### `docs/dev/architecture.md` changes

No `docs/dev/architecture.md` exists in the project — no changes required.

## Implementation Detail

Implementation order within B3-impl (after owner GO on this design):

1. Premise extension (`key_version`, `signature_hex` optional) + fixture schema bump + golden vectors for the new fields (present-but-ignored-until-D3-lands is forbidden — wire verification in the same commit).
2. `mapping_authorities.py`: registry models, canonical bytes, `verify_mapping_attestation`, closed outcome mapping.
3. Producer validator + checker mirror with differential agreement tests (same pattern as the existing producer/checker independence suite).
4. Producer approved-set configuration (D5): config-driven set, default frozen; startup test asserting production config excludes `synthetic-*`.
5. `check-attestation` CLI script + ops docs (minting ceremony template, rotation procedure, revocation procedure).
6. Registry fixture `mapping_authorities_v1.json` seeded with the synthetic entry only (real authorities arrive via later contract changes, each with key ceremony record).
7. Migration note recorded: synthetic vectors untouched; production premises require real authority (enforced by config, tested by startup test).

## Migrations

- Premise schema: additive optional fields only; all existing fixtures validate unchanged. Fixture schema version bump with the new optional fields documented.
- Frozen set + registry: append-only; the synthetic entry is never modified except a future retirement (which would itself be a reviewed change).
- No database migration in v1. No config format break: the checker's `mapping_authority` config gains no required keys (producer side gains an optional approved-set override defaulting to the frozen set).
- Compatibility: old certificates without signature fields verify exactly as today (their authority — synthetic — does not require signatures).

## Testing Philosophy

### Outcome matrix

Every row of the D3 decision table gets a direct unit test: valid signature discharges; missing signature where required abstains; wrong-key and tampered-bytes signatures deny as invalid inference; unknown key version abstains; retired key and retired authority abstain; synthetic path discharges by membership exactly as today (pinned, unchanged).

### Producer/checker agreement

Extend the existing differential suite: for every attestation vector, producer and checker must agree on outcome and on the abstain-vs-deny direction. Any divergence fails the suite — the correlated-defect concern from G4 is addressed by keeping the two implementations textually independent while pinning behavioral identity.

### Tamper and replay

Tamper with each signed field independently (authority ID swap, revision swap, digest swap, key-version downgrade) and assert denial or abstention as specified — never discharge. Replay a valid attestation against a different claim (different build identity) and assert the bind checks reject it.

### Rotation and revocation

Registry fixtures with retired keys and unknown versions drive the abstain paths; a rotation scenario (k1 retired, k2 active, artifact signed by each) asserts k1 abstains while k2 discharges. Emergency-revocation path is a registry edit plus the full matrix re-run.

### Config containment

Startup test asserts the production mapping-authority configuration contains no `synthetic-*` ID; a test with production config and a synthetic premise asserts non-discharge. The dev/test default set is pinned separately so the two cannot be confused.

## Documentation Plan

### `docs/IAP-proof-kernel-mapping-authority-design-v1.0.md` (this file)

**Audience:** Owner (approval), implementer (build).

The B3 contract: authority model, attestation envelope, offline verification, trust/rotation/revocation, synthetic containment, CI/ops interfaces. Approval of this document is the B3-design gate; implementation needs a separate GO.

### `docs/IAP-proof-kernel-g4-disposition-package-v1.0.md` (existing, decision recorded)

**Audience:** Owner.

No changes required by this design. On B3 implementation close, the C2-R1 residual line updates (mapping authority no longer open) via the normal disposition-update path, not by editing history.

## Risks / Trade-offs

### Private-key compromise

**Risk:** A CI signing key leaks; an attacker mints attestations for malicious builds.

**Mitigation:** Blast radius is bounded: every premise still needs build/repo/revision binding against observed runtime evidence, and the attestation only asserts the mapping, never the verdict. Emergency revocation is a registry contract change (retire the version → abstain). Key custody requirements are part of every contract-change request.

### Registry staleness

**Risk:** The frozen registry lags reality (new pipeline, rotated keys not yet recorded).

**Mitigation:** Staleness fails closed to abstention (`UNSUPPORTED_CONSTRUCT`), which is honest and visible in battery attribution. There is no path from stale registry to false discharge.

### Synthetic-in-production regression

**Risk:** A future change re-admits synthetic IDs to the production set, silently reopening the fabricated-ref hole.

**Mitigation:** The startup test fails closed (refuses the verifier lane), the deployment checklist names the exclusion, and the differential suite pins dev vs production sets separately. Three independent barriers.

### Scope creep into runtime attestation

**Risk:** Pressure to extend the same mechanism to runtime/execution claims ("the service observed X").

**Mitigation:** Out of scope by design (see Non-Goals). The attestation covers build→source mapping only; runtime observations keep their existing proof rules. Any extension is a new design, not an amendment.
