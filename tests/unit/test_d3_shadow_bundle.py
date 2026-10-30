"""D3 shadow bundle assembly, evaluator, trust verification (G06/G07/N04/N09/N10/N14/N20/N23 + import closure)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from investigation_agent_platform.domain.proof import definition_shadow as mod
from investigation_agent_platform.domain.proof.definition_shadow import (
    CurrencyProperty,
    ShadowError,
    ShadowOutcome,
    assemble_bundle,
    check_config_surface,
    compute_bundle_digest,
    compute_currency,
    derive_manifest_digest,
    evaluate_definition_shadow,
    summarize_shadow_definitions,
    verify_shadow_startup,
)
from investigation_agent_platform.domain.proof.dispatch import (
    BundleApproval,
    DispatchTable,
    ResolverRegistry,
)

R40 = "a" * 40
R64 = "b" * 64


def _loc(fp="src/app.py", ls=10, le=20, rev=R40):
    return SimpleNamespace(file_path=fp, line_start=ls, line_end=le, revision=rev)


def _row(eid="ev-001", snippet="def serve():\n    pass\n", loc=None):
    return SimpleNamespace(
        evidence_id=eid,
        content_snippet=snippet,
        provenance=SimpleNamespace(source_location=loc or _loc()),
    )


def _candidate(
    kind="DEFINITION", symbol="serve", commit=R40, scope="file", decl="Handler", fp="src/app.py"
):
    from investigation_agent_platform.domain.hypothesis.models import ClaimKind

    target = SimpleNamespace(
        symbol=symbol, declaring_type=decl, file_path=fp, commit_id=commit, scope=scope
    )
    hyp = SimpleNamespace(
        claim_kind=getattr(ClaimKind, kind, kind), claim_target=target, assessments=[]
    )
    return hyp


def _bundle(**over):
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


def _table():
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


def _registry():
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


FLAGS = {"shadow_flag": True, "authority_flag": False}


# --- D3P-G06: resident bundle evaluates to a recorded observation ----------------


def test_d3p_g06_fully_resident_bundle_records_observation() -> None:
    bundle = _bundle()
    assert bundle.bundle_digest.startswith("sha256:")
    assert bundle.bundle_digest == compute_bundle_digest(bundle)
    assert not bundle.unavailable
    obs = evaluate_definition_shadow(bundle, _table(), _registry(), FLAGS)
    assert obs.bundle_digest == bundle.bundle_digest
    assert obs.outcome in (ShadowOutcome.ABSTAINED, ShadowOutcome.UNAVAILABLE)
    assert obs.association_scope == "source-range-only"
    assert obs.declaration_requirement_satisfied is False
    assert obs.flag_config_snapshot == FLAGS
    assert obs.observation_id.startswith("sha256:")
    assert obs.reason_code != ""
    assert obs.rule_trace and obs.rule_trace[0] == "evaluate:start"


def test_d3p_g06_abstains_on_unbound_repository_live_shape() -> None:
    """Live-shaped bundle (no repo anywhere): ABSTAINED, precise reason, full trace."""
    bundle = _bundle()
    assert bundle.currency.repository == CurrencyProperty.UNKNOWN
    obs = evaluate_definition_shadow(bundle, _table(), _registry(), FLAGS)
    assert obs.outcome == ShadowOutcome.ABSTAINED
    assert obs.reason_code == "unbound-repository"
    assert any("repository-unbound" in s for s in obs.rule_trace)


# --- D3P-N04: bytes identity ------------------------------------------------------


def test_d3p_n04_bytes_change_changes_digest() -> None:
    b1 = _bundle(bound=[_row(eid="ev-001", snippet="def serve():\n    pass\n")])
    b2 = _bundle(bound=[_row(eid="ev-001", snippet="def serve():\n    return 1\n")])
    assert b1.resident_window_bytes_ref != b2.resident_window_bytes_ref
    assert b1.bundle_digest != b2.bundle_digest


# --- D3P-N05/N23: currency ----------------------------------------------------------


def test_d3p_n05_revision_mismatch() -> None:
    cur = compute_currency([R40, R64], R40, "file")
    assert cur.revision == CurrencyProperty.MISMATCH
    bundle = _bundle(bound=[_row(loc=_loc(rev=R40)), _row(eid="ev-002", loc=_loc(rev=R64))])
    obs = evaluate_definition_shadow(bundle, _table(), _registry(), FLAGS)
    assert obs.outcome == ShadowOutcome.ABSTAINED
    assert obs.reason_code == "stale-or-unknown-context"


def test_d3p_n23_unbound_source_defaults_unknown() -> None:
    cur = compute_currency([], None, None)
    assert cur.repository == CurrencyProperty.UNKNOWN
    assert cur.revision == CurrencyProperty.UNKNOWN
    assert cur.scope == CurrencyProperty.UNKNOWN
    cur2 = compute_currency([R40], None, "file")
    assert cur2.revision == CurrencyProperty.UNKNOWN
    assert cur2.scope == CurrencyProperty.VALIDATED
    cur3 = compute_currency([R40], R40, "file")
    assert cur3.revision == CurrencyProperty.VALIDATED


def test_d3p_unknown_scope_abstains() -> None:
    bundle = _bundle(candidate=_candidate(scope=None))
    obs = evaluate_definition_shadow(bundle, _table(), _registry(), FLAGS)
    assert obs.outcome == ShadowOutcome.ABSTAINED
    assert obs.reason_code == "stale-or-unknown-context"


# --- UNAVAILABLE paths ---------------------------------------------------------------


def test_d3p_unavailable_not_a_definition_claim() -> None:
    bundle = _bundle(candidate=_candidate(kind="CALL_EDGE"))
    assert "definition-claim" in bundle.unavailable
    obs = evaluate_definition_shadow(bundle, _table(), _registry(), FLAGS)
    assert obs.outcome == ShadowOutcome.UNAVAILABLE
    assert obs.reason_code == "not-a-definition-claim"


def test_d3p_unavailable_no_candidate_rows() -> None:
    bundle = _bundle(bound=[])
    assert "bound-rows" in bundle.unavailable
    obs = evaluate_definition_shadow(bundle, _table(), _registry(), FLAGS)
    assert obs.outcome == ShadowOutcome.UNAVAILABLE
    assert obs.reason_code == "no-candidate-rows"


def test_d3p_unavailable_oversized_input() -> None:
    big = "x" * (1024 * 1024 + 1)
    with pytest.raises(ShadowError):
        _bundle(bound=[_row(snippet=big)])


# --- D3P-G07/N09/N10/N20: trust verification -------------------------------------------


def _manifest_inputs(**over):
    base = {
        "package_files": {"definition_shadow.py": b"code-bytes"},
        "dependencies": {"temporalio": "1.33.0"},
        "config_surface": {"MODE": "test"},
    }
    base.update(over)
    return base


def test_d3p_g07_trust_happy_path_permits() -> None:
    assert verify_shadow_startup(_table(), _registry(), _manifest_inputs(), {"MODE"}, {"MODE"})


def test_d3p_n09a_d_contract_only_change_refuses() -> None:
    from investigation_agent_platform.domain.proof.definition_dispatch import (
        verify_definition_binding,
    )
    from investigation_agent_platform.domain.proof.dispatch import ActualResolver, DispatchError

    row = _table().rows[0]
    bad = ActualResolver(
        bundle=row.resolver_bundle,
        bundle_version=row.bundle_version,
        contract_digest="sha256:other",
        artifact_digest=row.artifact_digest,
    )
    with pytest.raises(DispatchError):
        verify_definition_binding(row, bad)


def test_d3p_n09b_d_executable_substitution_refuses() -> None:
    from investigation_agent_platform.domain.proof.definition_dispatch import (
        verify_definition_binding,
    )
    from investigation_agent_platform.domain.proof.dispatch import ActualResolver, DispatchError

    row = _table().rows[0]
    bad = ActualResolver(
        bundle=row.resolver_bundle,
        bundle_version=row.bundle_version,
        contract_digest=row.contract_digest,
        artifact_digest="sha256:other",
    )
    with pytest.raises(DispatchError):
        verify_definition_binding(row, bad)


def test_d3p_n09c_d_dependency_drift_refuses() -> None:
    good = derive_manifest_digest(**_manifest_inputs())
    drifted = derive_manifest_digest(**_manifest_inputs(dependencies={"temporalio": "9.9.9"}))
    assert good != drifted


def test_d3p_n09d_config_drift_refuses() -> None:
    good = derive_manifest_digest(**_manifest_inputs())
    drifted = derive_manifest_digest(**_manifest_inputs(config_surface={"MODE": "prod"}))
    assert good != drifted


def test_d3p_n10_unallowlisted_read_is_violation() -> None:
    with pytest.raises(ShadowError):
        check_config_surface({"MODE", "SECRET_TOKEN"}, {"MODE"})
    check_config_surface({"MODE"}, {"MODE", "OTHER"})


def test_d3p_n20_unmeasurable_refuses() -> None:
    with pytest.raises(ShadowError):
        derive_manifest_digest(None, {}, {})
    with pytest.raises(ShadowError):
        verify_shadow_startup(_table(), _registry(), None, set(), set())


# --- summarizer + tamper evidence --------------------------------------------------------


def test_d3p_summarize_counts_only() -> None:
    bundle = _bundle()
    obs = evaluate_definition_shadow(bundle, _table(), _registry(), FLAGS)
    summary = summarize_shadow_definitions([obs.model_dump(mode="json"), {"bogus": True}, "x"])
    assert summary["abstained"] == 1
    assert summary["capture_errors"] == 2
    assert summary["admissible"] == 0


def test_d3p_summarize_breaks_down_input_set_and_reason() -> None:
    """§1.6 frozen inventory: the summary carries per-input-set counts
    and a reason-code breakdown (additive dicts, §7 Observability)."""
    summary = summarize_shadow_definitions(
        [
            {
                "outcome": "ABSTAINED",
                "input_set": "BOUND_FULL",
                "reason_code": "unbound-repository",
            },
            {
                "outcome": "ABSTAINED",
                "input_set": "CITED_LOCATION",
                "reason_code": "unbound-repository",
            },
            {
                "outcome": "UNAVAILABLE",
                "input_set": "BOUND_FULL",
                "reason_code": "no-candidate-rows",
            },
            {"capture_error": True, "reason": "tap-fault", "input_set": "CONCLUDE_GATE"},
        ]
    )
    assert summary["by_input_set"] == {"BOUND_FULL": 2, "CITED_LOCATION": 1, "CONCLUDE_GATE": 1}
    assert summary["by_reason"] == {
        "unbound-repository": 2,
        "no-candidate-rows": 1,
        "tap-fault": 1,
    }
    assert summary["turns"] == 4
    assert summary["abstained"] == 2 and summary["unavailable"] == 1
    assert summary["capture_errors"] == 1


def test_d3p_bundle_digest_tamper_evident() -> None:
    bundle = _bundle()
    tampered = bundle.model_copy(update={"triage_route": "runtime"})
    assert tampered.bundle_digest != compute_bundle_digest(tampered)
    obs = evaluate_definition_shadow(tampered, _table(), _registry(), FLAGS)
    assert obs.outcome == ShadowOutcome.UNAVAILABLE
    assert obs.reason_code == "unavailable-input"


def test_d3p_shadow_module_import_closure_static() -> None:
    """D3P static gate: shadow module imports nothing effectful."""
    import ast
    from pathlib import Path as _P

    tree = ast.parse((_P(mod.__file__)).read_text())
    top = {
        (n.names[0].name if isinstance(n, ast.Import) else n.module or "").split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
    }
    assert top <= {
        "__future__",
        "hashlib",
        "enum",
        "typing",
        "pydantic",
        "os",
        "logging",
        "dataclasses",
        "investigation_agent_platform",
    }, top
    text = _P(mod.__file__).read_text()
    assert "import subprocess" not in text
    assert "importlib" not in text
    # os.environ reads are confined to the flag gate (resolve_definition_mode),
    # mirroring the D1 single-purpose discipline: nowhere else may touch the env.
    lines = text.splitlines()
    in_gate = False
    for i, line in enumerate(lines):
        if line.startswith("def resolve_definition_mode"):
            in_gate = True
        elif in_gate and (line.startswith("def ") or line.startswith("class ")):
            in_gate = False
        if "os.environ" in line:
            assert in_gate, f"os.environ outside flag gate at line {i + 1}: {line.strip()}"
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            assert not n.module.startswith("investigation_agent_platform.application"), n.module
            assert not n.module.startswith("investigation_agent_platform.infrastructure"), n.module
            assert "temporalio" not in (n.module or ""), n.module
        elif isinstance(n, ast.Import):
            for a in n.names:
                assert not a.name.startswith("investigation_agent_platform.application"), a.name
