"""D3 shadow runtime helpers: mode gate, envelope, pairing, projection, audit (N21/N22/N24/N25/N26 + F05-F09)."""

from __future__ import annotations

import pytest

from investigation_agent_platform.domain.proof.definition_shadow import (
    _SDK_IDENTITY_FIELDS,
    attach_shadow_observations,
    audit_envelope_join,
    build_correlation_envelope,
    pair_receipt,
    project_envelope_for_telemetry,
    project_trace_for_telemetry,
    resolve_definition_mode,
    run_shadow_turn,
)
from investigation_agent_platform.domain.proof.dispatch import (
    BundleApproval,
    DispatchTable,
    ResolverRegistry,
)

R40 = "a" * 40


def _loc(fp="app.py", ls=10, le=20, rev=R40):
    from types import SimpleNamespace

    return SimpleNamespace(file_path=fp, line_start=ls, line_end=le, revision=rev)


def _row(eid="ev-001", snippet="def serve():\n    pass\n", loc=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        evidence_id=eid,
        content_snippet=snippet,
        provenance=SimpleNamespace(source_location=loc or _loc()),
    )


def _candidate(symbol="serve", commit_id=R40, scope="file", kind="DEFINITION"):
    from types import SimpleNamespace

    from investigation_agent_platform.domain.hypothesis.models import ClaimKind

    target = SimpleNamespace(
        symbol=symbol, declaring_type=None, file_path="app.py", commit_id=commit_id, scope=scope
    )
    return SimpleNamespace(
        claim_kind=getattr(ClaimKind, kind, kind), claim_target=target, assessments=[]
    )


def _bundle(**over):
    from investigation_agent_platform.domain.proof.definition_shadow import assemble_bundle

    base = dict(
        candidate=_candidate(),
        bound=[_row()],
        refuting_ids=[],
        triage_route="",
        verifier_version="v2",
        investigation_id="inv-001",
        hypothesis_id="hyp-001",
        application_id="app-001",
        input_set="BOUND_FULL",
    )
    base.update(over)
    return assemble_bundle(**base)


def _table() -> DispatchTable:
    from investigation_agent_platform.domain.proof.dispatch import DispatchRow

    row = DispatchRow(
        profile_id="investigation-default",
        profile_version="v1",
        language="python",
        claim_kind="DEFINITION",
        fact_category="definition",
        resolver_bundle="evidence-rebinding-normalizer",
        bundle_version="v1",
        contract_digest="sha256:test-contract-normalizer-v1",
        artifact_digest="sha256:test-artifact-normalizer-v1",
        rule_id="definition/v1",
        requires_target_declaration=True,
    ).sealed()
    return DispatchTable(rows=[row])


def _registry() -> ResolverRegistry:
    return ResolverRegistry(
        approved_bundles={
            "evidence-rebinding-normalizer": BundleApproval(
                bundle="evidence-rebinding-normalizer",
                versions={
                    "v1": {
                        "contract_digest": "sha256:test-contract-normalizer-v1",
                        "artifact_digest": "sha256:test-artifact-normalizer-v1",
                    }
                },
            )
        }
    )


def _obs(**over):
    from investigation_agent_platform.domain.proof.definition_shadow import (
        evaluate_definition_shadow,
    )

    base = evaluate_definition_shadow(_bundle(), _table(), _registry(), {"shadow_flag": True, "authority_flag": False})
    d = base.model_dump(mode="json")
    d.update(over)
    return d


IDENT = {
    "namespace": "ns",
    "workflow_id": "wf",
    "workflow_run_id": "run",
    "activity_id": "act",
    "attempt": 1,
}


# --- mode gate (F05/F06/F07/F08) --------------------------------------------------


def test_d3p_mode_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IAP_SHADOW_TYPED_DEFINITIONS", raising=False)
    monkeypatch.delenv("IAP_TYPED_DEFINITIONS", raising=False)
    assert resolve_definition_mode() == "off"


def test_d3p_a2_identity_is_present_and_joined() -> None:
    from investigation_agent_platform.domain.proof.definition_shadow import (
        ACTIVE_BEHAVIOR_VERSION,
        AMENDMENT_ID,
        PROTOCOL_ID,
    )

    obs = _obs()
    assert obs["active_behavior_version"] == ACTIVE_BEHAVIOR_VERSION
    assert obs["verifier_version"] == "v2"
    assert obs["protocol_id"] == PROTOCOL_ID
    assert obs["amendment_id"] == AMENDMENT_ID
    envelope = build_correlation_envelope(obs, 1, 0, {"status": "INCONCLUSIVE", "producing_path": "verify"}, IDENT)
    assert envelope["protocol_id"] == PROTOCOL_ID
    assert envelope["amendment_id"] == AMENDMENT_ID
    assert audit_envelope_join(envelope) == "ok"
    assert audit_envelope_join({**envelope, "amendment_id": "DEC-other"}) == "void:amendment-mismatch"


def test_d3p_mode_shadow_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAP_SHADOW_TYPED_DEFINITIONS", "1")
    monkeypatch.delenv("IAP_TYPED_DEFINITIONS", raising=False)
    assert resolve_definition_mode() == "shadow"


def test_d3p_f06_authority_hard_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    from investigation_agent_platform.domain.proof.definition_shadow import ShadowError

    monkeypatch.setenv("IAP_TYPED_DEFINITIONS", "1")
    with pytest.raises(ShadowError):
        resolve_definition_mode()


def test_d3p_f07_malformed_logs_and_disables(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    import logging

    monkeypatch.setenv("IAP_SHADOW_TYPED_DEFINITIONS", "true")
    with caplog.at_level(logging.WARNING):
        assert resolve_definition_mode() == "off"
    assert any("IAP_SHADOW_TYPED_DEFINITIONS" in r.message for r in caplog.records)


def test_d3p_f08_authority_forced_still_no_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with authority forced in-process, no consumer exists: the
    mode gate refuses first (F06); and at the evaluator level, an
    authority-flagged run still abstains with the stub false."""
    from investigation_agent_platform.domain.proof.definition_dispatch import (
        evaluate_declaration_requirement,
    )
    from investigation_agent_platform.domain.proof.definition_shadow import (
        ShadowError,
        evaluate_definition_shadow,
    )

    monkeypatch.setenv("IAP_SHADOW_TYPED_DEFINITIONS", "1")
    monkeypatch.setenv("IAP_TYPED_DEFINITIONS", "1")
    with pytest.raises(ShadowError):
        resolve_definition_mode()
    obs = evaluate_definition_shadow(
        _bundle(), _table(), _registry(), {"shadow_flag": True, "authority_flag": True}
    )
    assert obs.outcome.value == "ABSTAINED"
    assert obs.declaration_requirement_satisfied is False
    assert evaluate_declaration_requirement.__name__ == "evaluate_declaration_requirement"


# --- run_shadow_turn flag-off (N21/F01) --------------------------------------------


def test_d3p_f01_flag_off_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IAP_SHADOW_TYPED_DEFINITIONS", raising=False)
    assert run_shadow_turn(
        candidate=_candidate(), bound=[], refuting_ids=[], triage_route="",
        verifier_version="v2", investigation_id="i", hypothesis_id="h",
        application_id="a", input_set="BOUND_FULL", table=_table(), registry=_registry(),
    ) is None


def test_d3p_run_shadow_turn_failure_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAP_SHADOW_TYPED_DEFINITIONS", "1")
    out = run_shadow_turn(
        candidate=None, bound=None, refuting_ids=[], triage_route="",  # type: ignore[arg-type]
        verifier_version="v2", investigation_id="i", hypothesis_id="h",
        application_id="a", input_set="BOUND_FULL", table=_table(), registry=_registry(),
    )
    assert out is not None and out.get("capture_error") is True


def test_d3p_n21_attach_additive_key_only_when_observations() -> None:
    base = {"verified": True}
    assert attach_shadow_observations(base, []) == base
    assert attach_shadow_observations(base, [None]) == base  # type: ignore[list-item]
    out = attach_shadow_observations(base, [{"outcome": "ABSTAINED"}])
    assert out["shadow_definitions"] == [{"outcome": "ABSTAINED"}]
    assert out["verified"] is True


# --- envelope + pairing (N22/N25) ----------------------------------------------------


def test_d3p_n25_envelope_identity_stable_across_replay() -> None:
    obs = _obs()
    native = {"verified": False, "status": "INCONCLUSIVE", "producing_path": "general"}
    e1 = build_correlation_envelope(obs, 3, 7, native, IDENT)
    e2 = build_correlation_envelope(obs, 3, 7, native, IDENT)
    assert e1["envelope_digest"] == e2["envelope_digest"]
    assert e1["envelope_digest"].startswith("sha256:")
    tampered = dict(e1)
    tampered["iteration"] = 4
    assert tampered["iteration"] != e1["iteration"]
    import hashlib

    from investigation_agent_platform.domain.proof.canonical import canonical_dict_bytes

    raw = canonical_dict_bytes(
        {
            "domain": "iap:definition-correlation-envelope",
            "observation": obs,
            "iteration": 4,
            "receipt_index": 7,
            "native_outcome": native,
            "activity_identity": IDENT,
        }
    )
    assert ("sha256:" + hashlib.sha256(raw).hexdigest()) != e1["envelope_digest"]


def test_d3p_n22_pairing_dedup_keep_first() -> None:
    obs = _obs()
    native = {"verified": False, "status": "INCONCLUSIVE", "producing_path": "general"}
    envs, dups = pair_receipt([obs, dict(obs)], 3, 0, native, IDENT)
    assert len(envs) == 1 and dups == 1
    assert envs[0]["iteration"] == 3 and envs[0]["receipt_index"] == 0
    assert envs[0]["activity_identity"]["attempt"] == 1


def test_d3p_n22_superseded_attempt_distinguished() -> None:
    obs = _obs()
    native = {"verified": False, "status": "INCONCLUSIVE", "producing_path": "general"}
    ident2 = dict(IDENT, attempt=2)
    envs, dups = pair_receipt([obs], 3, 0, native, ident2)
    assert dups == 0
    envs2, _ = pair_receipt([obs], 3, 1, native, IDENT)
    assert envs[0]["activity_identity"]["attempt"] == 2
    assert envs2[0]["activity_identity"]["attempt"] == 1
    assert envs[0]["envelope_digest"] != envs2[0]["envelope_digest"]


def test_d3p_sdk_identity_mirror_conformance() -> None:
    """C2: our identity tuple mirrors temporalio.activity.Info field names."""
    import dataclasses

    from temporalio.activity import Info

    names = {f.name for f in dataclasses.fields(Info)}
    assert set(_SDK_IDENTITY_FIELDS) <= names, _SDK_IDENTITY_FIELDS
    assert "attempt" in names and "activity_id" in names


# --- join audit (N24) ------------------------------------------------------------------


def test_d3p_n24_audit_ok_and_voids() -> None:

    obs = _obs()
    native = {"verified": False, "status": "INCONCLUSIVE", "producing_path": "general"}
    env = build_correlation_envelope(obs, 1, 0, native, IDENT)
    assert audit_envelope_join(env) == "ok"
    no_path = build_correlation_envelope(obs, 1, 0, {"verified": False}, IDENT)
    assert audit_envelope_join(no_path) == "void:unattributable-path"
    assert audit_envelope_join({}) == "void:missing-native-outcome"
    assert audit_envelope_join({"native_outcome": {"x": 1}}) == "void:missing-observation"
    sup = build_correlation_envelope(obs, 1, 0, native, {**IDENT, "superseded_attempt": True})
    assert audit_envelope_join(sup) == "void:superseded-attempt"


# --- projection + overflow (N26) ----------------------------------------------------------


def test_d3p_n26_prefix_retention_with_running_counts() -> None:

    obs = _obs()
    native = {"verified": False, "status": "INCONCLUSIVE", "producing_path": "general"}
    envs = [build_correlation_envelope(dict(obs), i, i, native, IDENT) for i in range(300)]
    projected, health = project_trace_for_telemetry(envs)
    assert len(projected) == 256
    assert projected[0]["iteration"] == 0
    assert projected[-1]["iteration"] == 255
    assert health["dropped_count"] == 44
    assert health["trace_truncated"] is True
    assert health["malformed_dropped"] == 0
    assert set(projected[0].keys()) <= {
        "observation_id", "bundle_digest", "outcome", "reason_code", "iteration",
        "input_set", "producing_path", "native_status", "flag_config",
    }


def test_d3p_projection_no_snippets_or_traces() -> None:

    obs = _obs()
    obs["rule_trace"] = ["step"] * 1000
    native = {"verified": False, "status": "INCONCLUSIVE", "producing_path": "general"}
    env = build_correlation_envelope(obs, 1, 0, native, IDENT)
    proj = project_envelope_for_telemetry(env)
    assert "rule_trace" not in proj
    assert all(isinstance(v, (str, int, dict)) for v in proj.values())


def test_d3p_n15_failure_preserves_native_inputs_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """N15: even when the shadow path fails, native inputs are identical."""
    import copy

    monkeypatch.setenv("IAP_SHADOW_TYPED_DEFINITIONS", "1")
    cand = _candidate()
    bound = [_row(), _row(eid="ev-002")]
    before_c, before_b = copy.deepcopy(cand), copy.deepcopy(bound)
    out = run_shadow_turn(
        candidate=cand, bound=bound, refuting_ids=["r1"], triage_route="",
        verifier_version="v2", investigation_id="i", hypothesis_id="h",
        application_id="a", input_set="BOUND_FULL", table="not-a-table", registry=_registry(),  # type: ignore[arg-type]
    )
    assert out is not None and out.get("capture_error") is True
    assert cand == before_c and bound == before_b


def test_d3p_n17_omission_flag_paths() -> None:
    """N17/C5: in-band omission flag semantics (Path A)."""
    from investigation_agent_platform.domain.proof.definition_shadow import (
        telemetry_omission_flagged,
    )

    assert telemetry_omission_flagged({"k": 1}, None) is True
    assert telemetry_omission_flagged({"k": 1}, {"telemetry": True}) is False
    assert telemetry_omission_flagged({}, None) is False
    assert telemetry_omission_flagged(None, None) is False


def test_d3p_n17_derive_carries_definition_keys() -> None:
    """N17: _derive allowlists definition summary keys and caps the trace."""
    from investigation_agent_platform.application.worker.activities import _derive_loop_telemetry

    env = build_correlation_envelope(
        _obs(), 1, 0,
        {"verified": False, "status": "INCONCLUSIVE", "producing_path": "general"},
        IDENT,
    )
    passed = {
        "shadow_definition_summary": {
            "turns": 1, "admissible": 0, "abstained": 1, "unavailable": 0,
            "capture_errors": 0, "mirror_notes": 3, "dropped_count": 0, "duplicate_count": 0,
            "telemetry_omissions": 0, "rogue": 999,
            "by_input_set": {"BOUND_FULL": 1, "EVIL": "x", 42: 1},
            "by_reason": {"unbound-repository": 1, "rogue": "y"},
        },
        "definition_shadow_trace": [env, "not-a-dict", {**env, "iteration": True}],
        "loop_telemetry": {"x": 1},
    }
    out = _derive_loop_telemetry(passed, {"hypotheses_formulated": 0, "hypotheses_bound": 0, "hypotheses_verified": 0}, 0)
    assert out is not None
    assert out["shadow_definition_summary"]["abstained"] == 1
    assert out["shadow_definition_summary"]["mirror_notes"] == 3
    assert "rogue" not in out["shadow_definition_summary"]
    assert out["shadow_definition_summary"]["by_input_set"] == {"BOUND_FULL": 1}
    assert out["shadow_definition_summary"]["by_reason"] == {"unbound-repository": 1}
    assert len(out["definition_shadow_trace"]) == 1
    assert out["definition_shadow_trace"][0]["iteration"] == 1
    assert "rule_trace" not in out["definition_shadow_trace"][0]
    assert out["shadow_definition_summary"]["dropped_count"] == 2


def test_d3p_n21_empty_trace_stamps_no_shadow_keys() -> None:
    """N21 additive-key rule at the telemetry layer: an empty envelope
    trace stamps neither the trace nor the summary — a zeroed shadow
    block would fake a presence the control arm must prove absent."""
    from investigation_agent_platform.application.worker.activities import _derive_loop_telemetry

    out = _derive_loop_telemetry(
        {
            "shadow_definition_summary": {
                "turns": 0, "admissible": 0, "abstained": 0, "unavailable": 0,
                "capture_errors": 0, "mirror_notes": 0, "dropped_count": 0,
                "duplicate_count": 0, "telemetry_omissions": 0,
                "by_input_set": {}, "by_reason": {},
            },
            "definition_shadow_trace": [],
        },
        {"hypotheses_formulated": 0, "hypotheses_bound": 0, "hypotheses_verified": 0},
        0,
    )
    assert out is not None
    assert "shadow_definition_summary" not in out
    assert "definition_shadow_trace" not in out


def test_d3p_conclude_gate_input_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """T3: the conclude-time path records CONCLUDE_GATE observations."""
    monkeypatch.setenv("IAP_SHADOW_TYPED_DEFINITIONS", "1")
    obs = run_shadow_turn(
        candidate=_candidate(), bound=[_row()], refuting_ids=[], triage_route="",
        verifier_version="v2", investigation_id="i", hypothesis_id="h",
        application_id="a", input_set="CONCLUDE_GATE", table=_table(), registry=_registry(),
    )
    assert obs is not None
    assert obs.get("input_set") == "CONCLUDE_GATE"


def test_d3p_oversized_input_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAP_SHADOW_TYPED_DEFINITIONS", "1")
    out = run_shadow_turn(
        candidate=_candidate(), bound=[_row(snippet="x" * (1024 * 1024 + 1))],
        refuting_ids=[], triage_route="", verifier_version="v2", investigation_id="i",
        hypothesis_id="h", application_id="a", input_set="BOUND_FULL",
        table=_table(), registry=_registry(),
    )
    assert out is not None and out.get("capture_error") is True


def test_d3p_n22_distinct_observations_never_merge() -> None:
    """R-IMPL-01 negative: same execution, semantically distinct observations
    (different reasons, different input sets, different stages) must never
    dedup — only exact duplicates collapse."""
    native = {"verified": False, "status": "INCONCLUSIVE", "producing_path": "general"}
    base = _obs()
    other_kind = dict(base, outcome="UNAVAILABLE", reason_code="no-candidate-rows")
    other_set = dict(base, input_set="CITED_LOCATION")
    envs, dups = pair_receipt([base, other_kind, other_set], 2, 0, native, IDENT)
    assert dups == 0
    assert len(envs) == 3
    # exact duplicate still collapses (keep-first) and counts
    envs2, dups2 = pair_receipt([base, dict(base)], 2, 0, native, IDENT)
    assert dups2 == 1 and len(envs2) == 1


def test_d3p_n24_candidate_mismatch_voids() -> None:
    """R-IMPL-02 negative: pairing a T3 observation with a different
    candidate's native outcome is a void join, never a match."""
    from investigation_agent_platform.domain.proof.definition_shadow import audit_envelope_join

    obs = _obs()
    native = {
        "verified": True,
        "status": "VERIFIED",
        "producing_path": "conclude-gate",
        "hypothesis_id": "hyp-OTHER",
    }
    env = build_correlation_envelope(obs, 5, 0, native, IDENT)
    assert audit_envelope_join(env) == "void:candidate-mismatch"
    native_ok = dict(native, hypothesis_id=obs["hypothesis_id"])
    assert native_ok["hypothesis_id"] == "hyp-001"
    env_ok = build_correlation_envelope(obs, 5, 0, native_ok, IDENT)
    assert audit_envelope_join(env_ok) == "ok"


# --- reload re-verification (§3.2) ---------------------------------------------------


def test_d3p_reload_reverifies_before_further_construction() -> None:
    """§3.2 reload row: reload drops the cache and re-derives a verified
    table (rows re-sealed, digests stable); a plain load reuses the
    cache. Malformed fixtures refuse at load — no half-loaded cache."""
    from investigation_agent_platform.application.worker import activities as act

    first = act.reload_definition_dispatch_table()
    assert first[0].policy_digest().startswith("sha256:")
    for row in first[0].rows:
        row.verify()
    second = act._load_definition_dispatch_table()
    assert second[0] is first[0]  # cached, no re-derivation
    third = act.reload_definition_dispatch_table()
    assert third[0] is not first[0]  # re-derived
    assert third[0].policy_digest() == first[0].policy_digest()
    for row in third[0].rows:
        row.verify()


def test_d3p_reload_refuses_malformed_fixture(tmp_path) -> None:
    """A tampered/unreadable mapping refuses at load (fail closed)."""
    from pydantic import ValidationError

    from investigation_agent_platform.domain.proof.definition_dispatch import (
        load_reviewed_definition_table,
    )

    bad = tmp_path / "mapping.json"
    bad.write_text('{"definition_rows": [{"profile_id": 1}]}')
    with pytest.raises(ValidationError):
        load_reviewed_definition_table(bad)
    missing = tmp_path / "absent.json"
    with pytest.raises(OSError):
        load_reviewed_definition_table(missing)


# --- float normalization (live Stage C finding 2026-10-24) ------------------------------
#
# The workflow pairs each turn's native outcome (whose confidence is a
# float, e.g. 0.8999999999999999) into the tamper-evident envelope, but
# the canonical bytes forbid floats — so every live pairing raised
# INVALID_FLOAT and the silent `except: pass` dropped all observations.
# Envelopes must normalize finite floats and store exactly what they digest.


def test_d3p_pairing_float_confidence_produces_envelopes() -> None:
    import hashlib

    from investigation_agent_platform.domain.proof.canonical import canonical_dict_bytes

    obs = _obs()
    native = {
        "verified": True,
        "status": "VERIFIED",
        "confidence": 0.8999999999999999,
        "hypothesis_id": "h-1",
        "producing_path": "general",
    }
    native_before = dict(native)
    envs, dups = pair_receipt([obs], 2, 0, native, IDENT)
    assert len(envs) == 1 and dups == 0
    env = envs[0]
    assert env["native_outcome"]["confidence"] == "0.900000"
    assert native == native_before  # inputs never mutated
    raw = canonical_dict_bytes(
        {
            "domain": "iap:definition-correlation-envelope",
            "observation": env["observation"],
            "iteration": env["iteration"],
            "receipt_index": env["receipt_index"],
            "native_outcome": env["native_outcome"],
            "activity_identity": env["activity_identity"],
            "protocol_id": env["protocol_id"],
            "amendment_id": env["amendment_id"],
        }
    )
    assert env["envelope_digest"] == "sha256:" + hashlib.sha256(raw).hexdigest()


def test_d3p_pairing_nan_confidence_raises() -> None:
    from investigation_agent_platform.domain.proof.canonical import (
        CanonicalizationError,
    )

    obs = _obs()
    native = {
        "verified": False,
        "status": "ERROR",
        "confidence": float("nan"),
        "hypothesis_id": "h-1",
        "producing_path": "general",
    }
    with pytest.raises(CanonicalizationError, match="INVALID_FLOAT"):
        pair_receipt([obs], 1, 0, native, IDENT)


def test_d3p_envelope_normalizes_observation_floats() -> None:
    import hashlib

    from investigation_agent_platform.domain.proof.canonical import canonical_dict_bytes

    obs = _obs(score=0.123456789)
    native = {"verified": False, "status": "INCONCLUSIVE", "producing_path": "general"}
    env = build_correlation_envelope(obs, 3, 7, native, IDENT)
    assert env["observation"]["score"] == "0.123457"
    assert obs["score"] == 0.123456789  # input never mutated
    raw = canonical_dict_bytes(
        {
            "domain": "iap:definition-correlation-envelope",
            "observation": env["observation"],
            "iteration": 3,
            "receipt_index": 7,
            "native_outcome": env["native_outcome"],
            "activity_identity": env["activity_identity"],
            "protocol_id": env["protocol_id"],
            "amendment_id": env["amendment_id"],
        }
    )
    assert env["envelope_digest"] == "sha256:" + hashlib.sha256(raw).hexdigest()


def test_d3p_summarize_mirror_notes_are_coverage_not_capture_errors() -> None:
    """T2a/T2b gate notes carry no outcome vocabulary; they are recorded
    coverage, never capture errors (live Stage C finding 2026-10-24)."""
    from investigation_agent_platform.domain.proof.definition_shadow import (
        summarize_shadow_definitions,
    )

    summary = summarize_shadow_definitions(
        [
            {"tap": "T2a", "input_set": "CITED_LOCATION", "kind": "DEFINITION",
             "ok": True, "reason": ""},
            {"tap": "T2b", "input_set": "BOUND_FULL", "kind": "DEFINITION",
             "ok": False, "reason": "definition-backstop-no-range-read"},
            {"tap": "T3", "input_set": "CONCLUDE_GATE", "kind": "DEFINITION",
             "ok": True, "reason": ""},
            {"bogus": True},
        ]
    )
    assert summary["turns"] == 4
    assert summary["mirror_notes"] == 3
    assert summary["capture_errors"] == 1  # only the bogus record
    assert summary["by_reason"]["mirror:T2a"] == 1
    assert summary["by_reason"]["mirror:T2b"] == 1
    assert summary["by_reason"]["mirror:T3"] == 1
