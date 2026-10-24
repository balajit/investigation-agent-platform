"""D1 dispatch table + authorization predicate (D1-C/D/E + mapping artifacts).

Traceability: every test name carries its matrix ID (§7). The mapping
fixture (tests/fixtures/proof_kernel/dispatch_mapping.json) is the
reviewed standalone artifact: authoritative sources, closed
analysis-model vocabularies, and the CATEGORY_PROOF_RULES mapping.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from investigation_agent_platform.domain.proof.dispatch import (
    AnalysisModelVocabulary,
    AuthoritativeSource,
    AuthorizationDecision,
    BundleApproval,
    CategoryMapping,
    CertificateBinding,
    DispatchError,
    DispatchRow,
    DispatchTable,
    PremiseBinding,
    ResolverRegistry,
    authorize_edge_use,
    content_digest,
    verify_resolver_binding,
)
from investigation_agent_platform.domain.proof.edge_observation import (
    BoundSymbol,
    CallSite,
    EdgeError,
    EdgeObservation,
    EdgeVariant,
    ExpressionRange,
    LimitationCode,
    Lineage,
    LineageSource,
    PartialObservationPayload,
    RepositoryId,
    ResolvedEdgePayload,
    ResolverId,
    make_observation,
)

REV40 = "a" * 40


def _resolver(**over: object) -> ResolverId:
    base: dict[str, object] = {
        "bundle": "py-resolver",
        "bundle_version": "1.0",
        "contract_digest": "sha256:contract",
        "artifact_digest": "sha256:artifact",
        "parser_version": "3.0",
        "language": "python",
        "language_version": "3.13",
        "analysis_model": "ast-scope",
    }
    base.update(over)
    return ResolverId(**base)  # type: ignore[arg-type]


def _resolved(**over: object) -> EdgeObservation:
    repo = RepositoryId(name="org/repo", revision=REV40)
    src = LineageSource(
        evidence_id="e1",
        evidence_digest="sha256:e",
        artifact_digest="sha256:s",
        scope="code",
        revision=REV40,
        repository=repo,
    )
    lin = Lineage(sources=[src], analyzer_run_id="r1", execution_identity="x1")
    payload = ResolvedEdgePayload(
        caller=BoundSymbol(symbol="f", declaring_type="C", file_path="a.py", revision=REV40),
        callee=BoundSymbol(symbol="g", declaring_type="C", file_path="b.py", revision=REV40),
        call_site=CallSite(
            file_path="a.py",
            revision=REV40,
            start_line=12,
            end_line=13,
            expression_range=ExpressionRange(start_line=12, start_col=4, end_line=12, end_col=9),
            window_text="x = f()",
            syntax_kind="call",
        ),
        target_declaration=None,
        repository=repo,
        resolver=_resolver(**over.get("resolver", {})),
        lineage=lin,
    )
    return make_observation(EdgeVariant.RESOLVED_EDGE, payload)


def _row(**over: object) -> DispatchRow:
    base: dict[str, object] = {
        "profile_id": "prof-1",
        "profile_version": "3",
        "language": "python",
        "claim_kind": "CALL_EDGE",
        "fact_category": "call-edge",
        "resolver_bundle": "py-resolver",
        "bundle_version": "1.0",
        "contract_digest": "sha256:contract",
        "artifact_digest": "sha256:artifact",
        "rule_id": "call_edge/v1",
        "requires_target_declaration": False,
    }
    base.update(over)
    row = DispatchRow(**base)  # type: ignore[arg-type]
    return row.sealed()


def _table(*rows: DispatchRow) -> DispatchTable:
    return DispatchTable(rows=list(rows))


def _registry() -> ResolverRegistry:
    return ResolverRegistry(
        approved_bundles={
            "py-resolver": BundleApproval(
                bundle="py-resolver",
                versions={
                    "1.0": {
                        "contract_digest": "sha256:contract",
                        "artifact_digest": "sha256:artifact",
                    }
                },
            )
        }
    )


def _binding(
    row: DispatchRow, record: EdgeObservation, table: DispatchTable
) -> tuple[CertificateBinding, PremiseBinding]:
    premise = PremiseBinding(
        observation_id=record.observation_id,
        content_digest=content_digest(record),
        fact_category=row.fact_category,
    )
    binding = CertificateBinding(
        profile_id=row.profile_id,
        profile_version=row.profile_version,
        dispatch_row_digest=row.row_digest,
        policy_digest=table.policy_digest(),
        claim_kind=row.claim_kind,
        premises=[premise],
    )
    return binding, premise


def _authorized() -> AuthorizationDecision:
    row = _row()
    table = _table(row)
    record = _resolved()
    binding, premise = _binding(row, record, table)
    return authorize_edge_use(binding, record, table, _registry(), premise)


def test_d1_e01_lookup_exact_match_only() -> None:
    table = _table(_row())
    assert table.lookup("prof-1", "3", "python", "CALL_EDGE", "call-edge") is not None
    assert table.lookup("prof-1", "4", "python", "CALL_EDGE", "call-edge") is None
    assert table.lookup("prof-1", "3", "java", "CALL_EDGE", "call-edge") is None
    assert table.lookup("prof-1", "3", "python", "DEFINITION", "call-edge") is None
    assert table.lookup("prof-1", "3", "python", "CALL_EDGE", "other") is None


def test_d1_e02_duplicate_keys_rejected() -> None:
    with pytest.raises((DispatchError, ValidationError)):
        _table(_row(), _row())


def test_d1_e02b_table_version_pinned() -> None:
    with pytest.raises((DispatchError, ValidationError)):
        DispatchTable(table_version="dispatch/v9", rows=[_row()])
    assert _table(_row()).table_version == "dispatch/v1"


def test_d1_e03_unknown_rule_rejected() -> None:
    with pytest.raises(DispatchError):
        _row(rule_id="nope/v9").verify()
    with pytest.raises((DispatchError, ValidationError)):
        DispatchTable(
            rows=[
                DispatchRow(
                    profile_id="p",
                    profile_version="1",
                    language="python",
                    claim_kind="CALL_EDGE",
                    fact_category="call-edge",
                    resolver_bundle="b",
                    bundle_version="1",
                    contract_digest="x",
                    artifact_digest="y",
                    rule_id="nope/v9",
                    requires_target_declaration=False,
                    row_digest="sha256:z",
                )
            ]
        )


def test_d1_e04_row_digest_reverified_at_read() -> None:
    row = _row()
    tampered = row.model_copy(update={"bundle_version": "9.9"})
    with pytest.raises((DispatchError, ValidationError)):
        _table(tampered)
    with pytest.raises(DispatchError):
        tampered.verify()


def test_d1_e05_policy_digest_binds_row_set() -> None:
    t1 = _table(_row())
    t2 = _table(_row(), _row(profile_version="4", bundle_version="2.0"))
    assert t1.policy_digest() != t2.policy_digest()


def test_d1_e06_authorize_happy_path() -> None:
    d = _authorized()
    assert d.authorized and d.check == "all"


def test_d1_e07_wrong_profile_rejected() -> None:
    row, table, record, binding, premise = _parts()
    binding = binding.model_copy(update={"profile_id": "other"})
    d = authorize_edge_use(binding, record, table, _registry(), premise)
    assert not d.authorized and d.check == "dispatch"


def test_d1_e08_stale_profile_version_rejected() -> None:
    row, table, record, binding, premise = _parts()
    binding = binding.model_copy(update={"profile_version": "2"})
    d = authorize_edge_use(binding, record, table, _registry(), premise)
    assert not d.authorized


def test_d1_e09_category_substitution_rejected() -> None:
    row, table, record, binding, premise = _parts()
    premise = premise.model_copy(update={"fact_category": "definition"})
    d = authorize_edge_use(binding, record, table, _registry(), premise)
    assert not d.authorized


def test_d1_e10_claim_kind_substitution_rejected() -> None:
    row, table, record, binding, premise = _parts()
    binding = binding.model_copy(update={"claim_kind": "DEFINITION"})
    d = authorize_edge_use(binding, record, table, _registry(), premise)
    assert not d.authorized


def test_d1_e11_premise_id_substitution_rejected() -> None:
    row, table, record, binding, premise = _parts()
    premise = premise.model_copy(update={"observation_id": "sha256:" + "f" * 64})
    d = authorize_edge_use(binding, record, table, _registry(), premise)
    assert not d.authorized and d.check == "premise"


def test_d1_e12_premise_digest_substitution_rejected() -> None:
    row, table, record, binding, premise = _parts()
    premise = premise.model_copy(update={"content_digest": "sha256:" + "f" * 64})
    d = authorize_edge_use(binding, record, table, _registry(), premise)
    assert not d.authorized and d.check == "premise"


def test_d1_e13_resolver_bundle_substitution_rejected() -> None:
    row = _row()
    table = _table(row)
    other = _resolved(resolver={"bundle": "evil-resolver"})
    binding, premise = _binding(row, other, table)
    d = authorize_edge_use(binding, other, table, _registry(), premise)
    assert not d.authorized and d.check == "resolver"


def test_d1_e14_cross_policy_replay_rejected() -> None:
    row = _row()
    table = _table(row)
    record = _resolved()
    binding, premise = _binding(row, record, table)
    other_table = _table(_row(), _row(profile_version="9"))
    d = authorize_edge_use(binding, record, other_table, _registry(), premise)
    assert not d.authorized and d.check == "dispatch"


def test_d1_e15_unapproved_bundle_rejected() -> None:
    row, table, record, binding, premise = _parts()
    d = authorize_edge_use(binding, record, table, ResolverRegistry(), premise)
    assert not d.authorized and d.check == "resolver"


def test_d1_e16_target_declaration_requirement() -> None:
    row = _row(requires_target_declaration=True)
    table = _table(row)
    record = _resolved()
    binding, premise = _binding(row, record, table)
    d = authorize_edge_use(binding, record, table, _registry(), premise)
    assert not d.authorized and "target_declaration" in d.reason


def _parts() -> tuple[
    DispatchRow, DispatchTable, EdgeObservation, CertificateBinding, PremiseBinding
]:
    row = _row()
    table = _table(row)
    record = _resolved()
    binding, premise = _binding(row, record, table)
    return row, table, record, binding, premise


def test_d1_c01_artifact_substitution_refused() -> None:
    from investigation_agent_platform.domain.proof.dispatch import ActualResolver

    row = _row()
    good = ActualResolver(
        bundle="py-resolver",
        bundle_version="1.0",
        contract_digest="sha256:contract",
        artifact_digest="sha256:artifact",
    )
    verify_resolver_binding(row, good)
    for bad in (
        {"artifact_digest": "sha256:other-exe"},
        {"contract_digest": "sha256:other-contract"},
        {"bundle_version": "9.9"},
        {"bundle": "other-bundle"},
    ):
        kw = {
            "bundle": "py-resolver",
            "bundle_version": "1.0",
            "contract_digest": "sha256:contract",
            "artifact_digest": "sha256:artifact",
        }
        kw.update(bad)
        with pytest.raises(DispatchError):
            verify_resolver_binding(row, ActualResolver(**kw))  # type: ignore[arg-type]


def test_d1_f05_partial_never_satisfies_premise() -> None:
    row, table, _, binding, _ = _parts()
    repo = RepositoryId(name="org/repo", revision=REV40)
    partial = make_observation(
        EdgeVariant.PARTIAL_OBSERVATION,
        PartialObservationPayload(
            caller=None,
            callee=None,
            call_site=None,
            target_declaration=None,
            limitation_codes=[LimitationCode.AMBIGUOUS_TARGET],
            missing=["caller", "callee"],
            repository=repo,
            resolver=_resolver(),
            lineage=_lineage_for(repo),
        ),
    )
    premise = PremiseBinding(
        observation_id=partial.observation_id,
        content_digest=content_digest(partial),
        fact_category=row.fact_category,
    )
    d = authorize_edge_use(binding, partial, table, _registry(), premise)
    assert not d.authorized


def _lineage_for(repo: RepositoryId):
    from investigation_agent_platform.domain.proof.edge_observation import Lineage, LineageSource

    return Lineage(
        sources=[
            LineageSource(
                evidence_id="e1",
                evidence_digest="sha256:e",
                artifact_digest="sha256:s",
                scope="code",
                revision=repo.revision,
                repository=repo,
            )
        ],
        analyzer_run_id="r1",
        execution_identity="x1",
    )


def test_d1_mapping_artifact_validates() -> None:
    raw = json.loads(
        (
            Path(__file__).parent.parent / "fixtures" / "proof_kernel" / "dispatch_mapping.json"
        ).read_text()
    )
    vocabs = [AnalysisModelVocabulary(**v) for v in raw["analysis_models"]]
    by_bundle = {v.resolver_bundle: v for v in vocabs}
    by_bundle["py-resolver"].check("ast-scope")
    with pytest.raises(EdgeError):
        by_bundle["py-resolver"].check("llm-vibes")
    sources = [AuthoritativeSource(**s) for s in raw["authoritative_sources"]]
    assert all(s.encoding and s.preprocessing_boundary for s in sources)
    mappings = [CategoryMapping(**m) for m in raw["category_mappings"]]
    assert mappings and all(m.disposition for m in mappings)
    with pytest.raises((EdgeError, ValidationError)):
        AnalysisModelVocabulary(resolver_bundle="x", models=[])
