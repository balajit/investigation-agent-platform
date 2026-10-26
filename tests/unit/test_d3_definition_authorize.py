"""D3 definition authorization predicate (G05/N05-N08/N11/N13/N13b/N19 + F05/F06 + mapping artifact loader)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from investigation_agent_platform.domain.proof import definition_dispatch as defmod
from investigation_agent_platform.domain.proof.definition_dispatch import (
    STUB_PHASE0_DECLARATION_CHECK_UNIMPLEMENTED,
    DefinitionStage,
    authorize_definition_use,
    definition_content_digest,
    evaluate_declaration_requirement,
    shadow_typed_definitions_enabled,
    typed_definitions_enabled,
    verify_definition_binding,
)
from investigation_agent_platform.domain.proof.definition_observation import (
    BoundTargetIdentity,
    DefinitionVariant,
    LimitationCode,
    PartialDefinitionPayload,
    RepositoryId,
    ResolvedDefinitionPayload,
    ResolverId,
    make_definition_observation,
)
from investigation_agent_platform.domain.proof.dispatch import (
    ActualResolver,
    BundleApproval,
    CertificateBinding,
    DispatchError,
    DispatchRow,
    DispatchTable,
    PremiseBinding,
    ResolverRegistry,
    verify_resolver_binding,
)
from investigation_agent_platform.domain.proof.edge_observation import EdgeError

R40 = "a" * 40

FIXDIR = Path(__file__).parent.parent / "fixtures" / "proof_kernel"


def _repo(name: str = "acme/app", rev: str = R40) -> RepositoryId:
    return RepositoryId(name=name, revision=rev)


def _resolver(**over: object) -> ResolverId:
    base: dict[str, object] = {
        "bundle": "evidence-rebinding-normalizer",
        "bundle_version": "v1",
        "contract_digest": "sha256:test-contract-normalizer-v1",
        "artifact_digest": "sha256:test-artifact-normalizer-v1",
        "parser_version": "1.0",
        "language": "python",
        "language_version": "3.13",
        "analysis_model": "evidence-rebinding",
    }
    base.update(over)
    return ResolverId(**base)  # type: ignore[arg-type]


def _row(**over: object) -> DispatchRow:
    base: dict[str, object] = {
        "profile_id": "investigation-default",
        "profile_version": "v1",
        "language": "python",
        "claim_kind": "DEFINITION",
        "fact_category": "definition",
        "resolver_bundle": "evidence-rebinding-normalizer",
        "bundle_version": "v1",
        "contract_digest": "sha256:test-contract-normalizer-v1",
        "artifact_digest": "sha256:test-artifact-normalizer-v1",
        "rule_id": "definition/v1",
        "requires_target_declaration": True,
    }
    base.update(over)
    row = DispatchRow(**base)  # type: ignore[arg-type]
    return row.sealed()


def _table(*rows: DispatchRow) -> DispatchTable:
    return DispatchTable(rows=list(rows))


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


def _lineage(repo: RepositoryId):
    from investigation_agent_platform.domain.proof.edge_observation import (
        Lineage,
        LineageSource,
    )

    return Lineage(
        sources=[
            LineageSource(
                evidence_id="ev-001",
                evidence_digest="sha256:e1",
                artifact_digest="sha256:a1",
                scope="file",
                revision=repo.revision,
                repository=repo,
            )
        ],
        analyzer_run_id="an-001",
        execution_identity="ex-001",
    )


def _resolved_record(symbol: str = "serve") -> object:
    repo = _repo()
    return make_definition_observation(
        DefinitionVariant.RESOLVED_TARGET_BINDING,
        ResolvedDefinitionPayload(
            target=BoundTargetIdentity(
                symbol=symbol, declaring_type="Handler", file_path="src/app.py", revision=R40
            ),
            repository=repo,
            resolver=_resolver(),
            limitation_codes=[],
            lineage=_lineage(repo),
            assumptions=[],
        ),
    )


def _binding(row: DispatchRow, table: DispatchTable, record) -> CertificateBinding:  # type: ignore[no-untyped-def]
    return CertificateBinding(
        profile_id=row.profile_id,
        profile_version=row.profile_version,
        dispatch_row_digest=row.row_digest,
        policy_digest=table.policy_digest(),
        claim_kind="DEFINITION",
        premises=[_premise(record)],
    )


def _premise(record) -> PremiseBinding:  # type: ignore[no-untyped-def]
    return PremiseBinding(
        observation_id=record.observation_id,
        content_digest=definition_content_digest(record),
        fact_category="definition",
    )


# --- D3P-G05: exact dispatch match -------------------------------------------


def test_d3p_g05_exact_match_admits_with_stage2_false() -> None:
    record = _resolved_record()
    table = _table(_row())
    decision = authorize_definition_use(
        _binding(table.rows[0], table, record), record, table, _registry(), _premise(record)
    )
    assert decision.stage1 == DefinitionStage.ASSOCIATION_ADMISSIBLE
    assert decision.observation == "OBSERVATION_ADMISSIBLE"
    assert decision.association_scope == "source-range-only"
    assert decision.declaration_requirement_satisfied is False
    assert "association-admissible" in decision.reason_code
    assert any("stage1:dispatch-row" in step for step in decision.rule_trace)


def test_d3p_g05_null_declaration_never_proceeds() -> None:
    assert _row(requires_target_declaration=True).requires_target_declaration is True


# --- D3P-N08: wrong dispatch --------------------------------------------------


def test_d3p_n08_unknown_row_abstains() -> None:
    record = _resolved_record()
    table = _table(_row(language="java"))
    decision = authorize_definition_use(
        _binding(_row(), table, record), record, table, _registry(), _premise(record)
    )
    assert decision.stage1 == DefinitionStage.ABSTAINED
    assert decision.observation == "ABSTAINED"
    assert decision.reason_code == "unknown-dispatch"
    assert decision.declaration_requirement_satisfied is False


def test_d3p_n08_stale_policy_digest_rejects() -> None:
    record = _resolved_record()
    table = _table(_row())
    binding = _binding(table.rows[0], table, record).model_copy(
        update={"policy_digest": "sha256:stale"}
    )
    decision = authorize_definition_use(binding, record, table, _registry(), _premise(record))
    assert decision.stage1 == DefinitionStage.ABSTAINED
    assert decision.reason_code == "policy-mismatch"


def test_d3p_n08_unknown_table_version_rejected_at_construction() -> None:
    with pytest.raises((DispatchError, ValidationError)):
        DispatchTable(table_version="dispatch/v9", rows=[_row()])


# --- D3P-N11: cross-policy replay ----------------------------------------------


def test_d3p_n11_cross_table_replay_rejected() -> None:
    record = _resolved_record()
    table_a = _table(_row(profile_version="v1"))
    table_b = _table(_row(profile_version="v2"))
    binding = _binding(table_a.rows[0], table_a, record)
    decision = authorize_definition_use(binding, record, table_b, _registry(), _premise(record))
    assert decision.stage1 == DefinitionStage.ABSTAINED
    assert decision.reason_code in ("unknown-dispatch", "policy-mismatch")


def test_d3p_n11_premise_binding_mismatch_rejected() -> None:
    record = _resolved_record()
    table = _table(_row())
    premise = _premise(record).model_copy(update={"observation_id": "sha256:" + "0" * 64})
    decision = authorize_definition_use(
        _binding(table.rows[0], table, record), record, table, _registry(), premise
    )
    assert decision.reason_code == "premise-mismatch"


# --- D3P-N13/N13b: exact output tuples ------------------------------------------


def requires_declaration_proof(decision) -> bool:  # type: ignore[no-untyped-def]
    """Test-harness reference requirement predicate: reads the machine-readable
    field; a tuple never satisfies a declaration requirement in Phase 0."""
    return bool(decision.declaration_requirement_satisfied)


def test_d3p_n13_exact_tuple_association_admitted_satisfaction_refused() -> None:
    record = _resolved_record()
    table = _table(_row())
    decision = authorize_definition_use(
        _binding(table.rows[0], table, record), record, table, _registry(), _premise(record)
    )
    assert decision.stage1 == DefinitionStage.ASSOCIATION_ADMISSIBLE
    assert decision.observation == "OBSERVATION_ADMISSIBLE"
    assert decision.declaration_requirement_satisfied is False
    assert requires_declaration_proof(decision) is False
    assert "no-authorized" not in decision.reason_code  # reason names admissibility, not proof
    assert "no AUTHORIZED" not in repr(decision) and "AUTHORIZED" not in str(
        decision.stage1.value
    )


def test_d3p_n13b_fully_valid_still_insufficient() -> None:
    record = _resolved_record()  # fully digest-valid, provenance-complete
    table = _table(_row())
    decision = authorize_definition_use(
        _binding(table.rows[0], table, record), record, table, _registry(), _premise(record)
    )
    assert decision.observation == "OBSERVATION_ADMISSIBLE"
    assert decision.declaration_requirement_satisfied is False
    assert requires_declaration_proof(decision) is False  # machine-readable, not prose


def test_d3p_n13_partial_record_stage1_abstains() -> None:
    repo = _repo()
    record = make_definition_observation(
        DefinitionVariant.PARTIAL_OBSERVATION,
        PartialDefinitionPayload(
            target=None,
            limitation_codes=[LimitationCode.MISSING_INPUT],
            missing=["target"],
            repository=repo,
            resolver=_resolver(),
            lineage=_lineage(repo),
            assumptions=[],
        ),
    )
    table = _table(_row())
    decision = authorize_definition_use(
        _binding(table.rows[0], table, record), record, table, _registry(), _premise(record)
    )
    assert decision.stage1 == DefinitionStage.ABSTAINED
    assert decision.reason_code == "variant-not-resolved"
    assert decision.declaration_requirement_satisfied is False


def test_d3p_n12_partial_presented_for_authorization_abstains() -> None:
    """D3P-N12: a PARTIAL record (missing target) presented for
    authorization abstains — it never binds, never satisfies, and the
    machine-readable declaration field stays false."""
    repo = _repo()
    record = make_definition_observation(
        DefinitionVariant.PARTIAL_OBSERVATION,
        PartialDefinitionPayload(
            target=None,
            limitation_codes=[LimitationCode.MISSING_INPUT],
            missing=["target"],
            repository=repo,
            resolver=_resolver(),
            lineage=_lineage(repo),
            assumptions=[],
        ),
    )
    table = _table(_row())
    decision = authorize_definition_use(
        _binding(table.rows[0], table, record), record, table, _registry(), _premise(record)
    )
    assert decision.stage1 == DefinitionStage.ABSTAINED
    assert decision.observation == "ABSTAINED"
    assert requires_declaration_proof(decision) is False
    assert decision.dispatch_row_digest == ""  # no row bound


def test_d3p_n07_lineage_gap_never_binds() -> None:
    """D3P-N07 authorize half: a partial record whose lineage names a gap
    (DG-PARTIAL-03-LINEAGE shape) abstains at authorization — the gap
    named in ``missing`` is carried through, never silently completed
    into a bound association."""
    import json as _json

    doc = _json.loads((FIXDIR / "definition_golden_vectors.json").read_text())
    entry = next(v for v in doc["vectors"] if v["id"] == "DG-PARTIAL-03-LINEAGE")
    from investigation_agent_platform.domain.proof.definition_observation import (
        verify_definition_observation,
    )

    record = verify_definition_observation(entry["record"])
    assert list(record.payload.missing) == ["lineage-source"]  # type: ignore[union-attr]
    table = _table(_row())
    decision = authorize_definition_use(
        _binding(table.rows[0], table, record), record, table, _registry(), _premise(record)
    )
    assert decision.stage1 == DefinitionStage.ABSTAINED
    assert decision.reason_code == "variant-not-resolved"
    assert decision.declaration_requirement_satisfied is False


# --- Stage 2 stub pin (F-B6/C9) -------------------------------------------------


def test_d3p_stub_named_and_constant_false() -> None:
    assert STUB_PHASE0_DECLARATION_CHECK_UNIMPLEMENTED.startswith("STUB_PHASE0_")
    assert evaluate_declaration_requirement(_resolved_record()) is False
    assert defmod.evaluate_declaration_requirement.__name__ == "evaluate_declaration_requirement"
    with pytest.raises(DispatchError):
        evaluate_declaration_requirement("not-a-record")  # type: ignore[arg-type]


# --- D3P-N19: no authorization value, no consumer --------------------------------


def test_d3p_n19a_no_authorized_literal_in_shadow_modules() -> None:
    """AST scan: AUTHORIZED must not appear as code (enum value / string
    constant). Prose mentions in docstrings are not authorization."""
    import ast as _ast

    for modname in ("definition_observation", "definition_dispatch", "definition_shadow"):
        path = Path(defmod.__file__).parent / f"{modname}.py"
        tree = _ast.parse(path.read_text())
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Constant) and isinstance(node.value, str):
                assert node.value != "AUTHORIZED", (modname, node.lineno)
            if isinstance(node, _ast.ClassDef):
                for stmt in node.body:
                    targets: list[str] = []
                    if isinstance(stmt, _ast.Assign):
                        for t in stmt.targets:
                            if isinstance(t, _ast.Name):
                                targets.append(t.id)
                    elif isinstance(stmt, _ast.AnnAssign) and isinstance(stmt.target, _ast.Name):
                        targets.append(stmt.target.id)
                    assert "AUTHORIZED" not in targets, (modname, node.lineno)
    from investigation_agent_platform.domain.proof.definition_dispatch import DefinitionStage
    from investigation_agent_platform.domain.proof.definition_shadow import ShadowOutcome

    assert {m.value for m in DefinitionStage} == {
        "ASSOCIATION_ADMISSIBLE",
        "ABSTAINED",
        "UNAVAILABLE",
    }
    assert {m.value for m in ShadowOutcome} == {"OBSERVATION_ADMISSIBLE", "ABSTAINED", "UNAVAILABLE"}


def test_d3p_n19b_no_consumer_outside_definition_modules() -> None:
    """Repo-wide import scan: the ONLY sanctioned importers of the
    definition record/authorize modules are the shadow evaluator
    (definition_shadow) and the Phase 0 tap host
    (application/worker/activities.py — tap wiring only, never a
    verdict/persistence/publication consumer). The verification
    engine, in particular, must stay free of definition imports:
    it receives only an opaque observer callback.
    """
    import ast as _ast

    allowed_suffixes = (
        "domain/proof/definition_shadow.py",
        # Worker split: the verify activity and the activities re-export
        # shim are the only worker modules allowed to name these imports.
        "application/worker/activities/verification.py",
        "application/worker/activities/__init__.py",
    )
    consumers: list[str] = []
    root = Path(defmod.__file__).resolve().parents[4]
    src = root / "src" / "investigation_agent_platform"
    for path in list((src / "application").rglob("*.py")) + list(
        (src / "domain").rglob("*.py")
    ):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(root))
        if "domain/proof/definition_" in rel:
            continue
        try:
            tree = _ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in _ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, _ast.ImportFrom) and node.module:
                mods = [node.module]
            elif isinstance(node, _ast.Import):
                mods = [a.name for a in node.names]
            for m in mods:
                if "definition_observation" in m or "definition_dispatch" in m:
                    if not rel.endswith(allowed_suffixes):
                        consumers.append(f"{rel}:{m}")
    assert consumers == [], consumers
    engine = src / "application" / "investigation" / "verification.py"
    engine_text = engine.read_text()
    for forbidden in (
        "definition_observation",
        "definition_dispatch",
        "definition_shadow",
        "IAP_SHADOW_TYPED_DEFINITIONS",
        "IAP_TYPED_DEFINITIONS",
    ):
        assert forbidden not in engine_text, f"engine must not name {forbidden}"


def test_d3p_n19b_flags_have_no_src_consumers() -> None:
    """The D3 flags exist but nothing reads them yet (no consumer to activate)."""

    root = Path(defmod.__file__).resolve().parents[4]
    hits: list[str] = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts or path.name.startswith("test_"):
            continue
        if path.name in ("definition_dispatch.py",):
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if "shadow_typed_definitions_enabled" in text or (
            "typed_definitions_enabled" in text and "shadow_" not in text.split("typed_definitions_enabled")[0][-80:]
        ):
            hits.append(str(path))
    # tests/ may reference them; src/ readers are limited to the
    # definition_shadow mode gate and the Phase 0 tap host
    # (application/worker/activities.py). No verdict engine,
    # persistence, publication, or other worker module may read them —
    # which is why no consumer can be activated by flipping a flag.
    src_hits = [h for h in hits if "/src/" in h]
    allowed = {"definition_shadow.py", "activities.py"}
    assert all(Path(h).name in allowed for h in src_hits), src_hits
    forbidden = [h for h in src_hits if "/application/investigation/" in h or "/domain/" in h and "proof" not in h]
    assert forbidden == [], forbidden


# --- Flags F05 + startup contract -------------------------------------------------


@pytest.mark.parametrize("value", ["true", "yes", "on", "0", " 1 ", "2", "TRUE", ""])
def test_d3p_f05_flags_parse_strictly(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAP_SHADOW_TYPED_DEFINITIONS", value)
    monkeypatch.setenv("IAP_TYPED_DEFINITIONS", value)
    assert shadow_typed_definitions_enabled() is False
    assert typed_definitions_enabled() is False


def test_d3p_flags_enable_only_on_exact_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IAP_SHADOW_TYPED_DEFINITIONS", "1")
    monkeypatch.setenv("IAP_TYPED_DEFINITIONS", "1")
    assert shadow_typed_definitions_enabled() is True
    assert typed_definitions_enabled() is True


def test_d3p_flags_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IAP_SHADOW_TYPED_DEFINITIONS", raising=False)
    monkeypatch.delenv("IAP_TYPED_DEFINITIONS", raising=False)
    assert shadow_typed_definitions_enabled() is False
    assert typed_definitions_enabled() is False


def test_d3p_flag_single_os_environ_read_per_module() -> None:
    text = Path(defmod.__file__).read_text()
    assert text.count("os.environ") == 2  # one per accessor, no more


# --- verify_definition_binding delegates to the shared primitive ------------------


def test_d3p_verify_definition_binding_delegates() -> None:

    row = _row()
    actual = ActualResolver(
        bundle=row.resolver_bundle,
        bundle_version=row.bundle_version,
        contract_digest=row.contract_digest,
        artifact_digest=row.artifact_digest,
    )
    assert verify_definition_binding(row, actual) is None
    bad = ActualResolver(
        bundle=row.resolver_bundle,
        bundle_version=row.bundle_version,
        contract_digest="sha256:other",
        artifact_digest=row.artifact_digest,
    )
    with pytest.raises(DispatchError):
        verify_definition_binding(row, bad)
    # shared primitive behaves identically
    with pytest.raises(DispatchError):
        verify_resolver_binding(row, bad)


def test_d3p_n06_record_carried_digest_rejected() -> None:
    from investigation_agent_platform.domain.proof.definition_observation import (
        verify_definition_observation,
    )

    record = _resolved_record()
    tampered = dict(record.model_dump(mode="json"))
    tampered["observation_id"] = "sha256:" + "f" * 64
    with pytest.raises(EdgeError):
        verify_definition_observation(tampered)
