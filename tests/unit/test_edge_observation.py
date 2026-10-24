"""D1 edge observation: identity, validation, adjudication (D1-A/B/D/F).

Traceability: every test name carries its matrix ID (§7). The pinned
golden vector (D1-A01) was verified by hand against the canonical
preimage before pinning; any identity-altering change breaks it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from investigation_agent_platform.domain.proof.edge_observation import (
    ASSUMPTIONS_V1,
    Adjudication,
    BoundSymbol,
    CallerBinding,
    CallSite,
    EdgeError,
    EdgeObservation,
    EdgeVariant,
    ExpressionRange,
    LimitationCode,
    Lineage,
    LineageSource,
    PartialObservationPayload,
    RangeReference,
    RepositoryId,
    ResolvedEdgePayload,
    ResolverId,
    SymbolReference,
    SyntaxForm,
    TargetDeclaration,
    UnresolvedObservationPayload,
    adjudicate_resolution,
    limitation_category,
    limitation_downstream,
    limitation_retryable,
    make_observation,
    verify_observation,
)

REV40 = "a" * 40
REV64 = "b" * 64


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


def _repo(**over: object) -> RepositoryId:
    base: dict[str, object] = {"name": "org/repo", "revision": REV40}
    base.update(over)
    return RepositoryId(**base)  # type: ignore[arg-type]


def _lineage(repo: RepositoryId | None = None, **over: object) -> Lineage:
    repo = repo or _repo()
    src = LineageSource(
        evidence_id="e1",
        evidence_digest="sha256:e",
        artifact_digest="sha256:s",
        scope="code",
        revision=repo.revision,
        repository=repo,
    )
    return Lineage(sources=[src], analyzer_run_id="r1", execution_identity="x1", **over)  # type: ignore[arg-type]


def _sym(symbol: str = "f", path: str = "a.py") -> BoundSymbol:
    return BoundSymbol(symbol=symbol, declaring_type="C", file_path=path, revision=REV40)


def _site(**over: object) -> CallSite:
    base: dict[str, object] = {
        "file_path": "a.py",
        "revision": REV40,
        "start_line": 12,
        "end_line": 13,
        "expression_range": ExpressionRange(start_line=12, start_col=4, end_line=12, end_col=9),
        "window_text": "x = f()",
        "syntax_kind": "call",
    }
    base.update(over)
    return CallSite(**base)  # type: ignore[arg-type]


def _resolved(**over: object) -> ResolvedEdgePayload:
    base: dict[str, object] = {
        "caller": _sym("f"),
        "callee": _sym("g", "b.py"),
        "call_site": _site(),
        "target_declaration": None,
        "repository": _repo(),
        "resolver": _resolver(),
        "lineage": _lineage(),
    }
    base.update(over)
    return ResolvedEdgePayload(**base)  # type: ignore[arg-type]


def _obs(
    payload: ResolvedEdgePayload | PartialObservationPayload | UnresolvedObservationPayload,
    variant: EdgeVariant,
) -> EdgeObservation:
    return make_observation(variant, payload)


def test_d1_a01_golden_vector_pinned() -> None:
    o = _obs(_resolved(), EdgeVariant.RESOLVED_EDGE)
    assert (
        o.observation_id
        == "sha256:e295d813d213d341286e2d0356bb4878729d24b7174e8f518f048c7635adf258"
    )


def test_d1_a02_deterministic_same_inputs() -> None:
    assert (
        _obs(_resolved(), EdgeVariant.RESOLVED_EDGE).observation_id
        == _obs(_resolved(), EdgeVariant.RESOLVED_EDGE).observation_id
    )


def test_d1_a03_per_field_sensitivity() -> None:
    base = _obs(_resolved(), EdgeVariant.RESOLVED_EDGE).observation_id
    assert (
        _obs(_resolved(callee=_sym("h", "b.py")), EdgeVariant.RESOLVED_EDGE).observation_id != base
    )
    assert (
        _obs(
            _resolved(call_site=_site(start_line=13, end_line=14)), EdgeVariant.RESOLVED_EDGE
        ).observation_id
        != base
    )
    assert (
        _obs(
            _resolved(resolver=_resolver(bundle_version="1.1")), EdgeVariant.RESOLVED_EDGE
        ).observation_id
        != base
    )
    assert (
        _obs(
            _resolved(resolver=_resolver(artifact_digest="sha256:other")), EdgeVariant.RESOLVED_EDGE
        ).observation_id
        != base
    )
    coherent = _resolved(repository=_repo(revision=REV64))
    coherent_lineage = _lineage(_repo(revision=REV64))
    assert (
        _obs(
            _resolved(repository=_repo(revision=REV64), lineage=coherent_lineage),
            EdgeVariant.RESOLVED_EDGE,
        ).observation_id
        != base
    )
    assert coherent.repository.revision == REV64


def test_d1_a03b_incoherent_repo_change_fails_closed() -> None:
    with pytest.raises(EdgeError):
        _obs(_resolved(repository=_repo(revision=REV64)), EdgeVariant.RESOLVED_EDGE)


def test_d1_a04_set_reorder_invariance() -> None:
    a = _resolved(assumptions=["no-execution-claim", "static-model-only"])
    b = _resolved(assumptions=["static-model-only", "no-execution-claim"])
    assert (
        _obs(a, EdgeVariant.RESOLVED_EDGE).observation_id
        == _obs(b, EdgeVariant.RESOLVED_EDGE).observation_id
    )


def test_d1_a05_duplicate_assumptions_rejected() -> None:
    with pytest.raises(EdgeError):
        _obs(
            _resolved(assumptions=["static-model-only", "static-model-only"]),
            EdgeVariant.RESOLVED_EDGE,
        )


def test_d1_a06_unknown_assumption_rejected() -> None:
    assert "static-model-only" in ASSUMPTIONS_V1 and "no-execution-claim" in ASSUMPTIONS_V1
    with pytest.raises(EdgeError):
        _obs(_resolved(assumptions=["trust-me"]), EdgeVariant.RESOLVED_EDGE)


def test_d1_a07_unicode_normalization() -> None:
    decomposed = "café"
    assert decomposed != "café"
    with pytest.raises((EdgeError, ValidationError)):
        _site(window_text=decomposed)


def test_d1_a08_unknown_fields_rejected_every_level() -> None:
    with pytest.raises(ValidationError):
        BoundSymbol(symbol="f", declaring_type="C", file_path="a.py", revision=REV40, extra="x")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        CallSite(
            file_path="a.py",
            revision=REV40,
            start_line=1,
            end_line=2,
            expression_range={"start_line": 1},
            window_text="x",
            syntax_kind="call",
        )  # type: ignore[dict-item]


def test_d1_a08b_unvalidated_mutation_rejected_on_read() -> None:
    # model_copy(update=...) deliberately skips validation (pydantic
    # semantics): the guarantee is read-time, not constructor-time.
    # A smuggled value must fail verify_observation, never reach the kernel.
    o = _obs(_resolved(), EdgeVariant.RESOLVED_EDGE)
    smuggled = o.payload.model_copy(update={"limitation_codes": ["NOPE"]})
    data = {
        "observation_id": o.observation_id,
        "variant": "RESOLVED_EDGE",
        "record_version": "edge/v1",
        "payload": smuggled.model_dump(mode="json"),
    }
    with pytest.raises(EdgeError):
        verify_observation(data)


def test_d1_a09_single_multi_empty_boundary_ranges() -> None:
    _site(start_line=12, end_line=13)
    _site(start_line=12, end_line=20)
    for bad in (
        {"start_line": 12, "end_line": 12},
        {"start_line": 14, "end_line": 13},
        {"start_line": 0, "end_line": 5},
    ):
        with pytest.raises((EdgeError, ValidationError)):
            _site(**bad)
    with pytest.raises((EdgeError, ValidationError)):
        ExpressionRange(start_line=12, start_col=9, end_line=12, end_col=4)


def test_d1_a10_line_span_vs_expression_range_independent() -> None:
    s = _site(start_line=12, end_line=13)
    assert (s.expression_range.start_line, s.expression_range.start_col) == (12, 4)
    t = TargetDeclaration(file_path="b.py", revision=REV40, start_line=3, end_line=9)
    assert t.end_line - t.start_line == 6


def test_d1_a11_path_rule() -> None:
    assert _sym(path="./src//a.py").file_path == "src/a.py"
    assert _sym(path="src/a.py/").file_path == "src/a.py"
    with pytest.raises((EdgeError, ValidationError)):
        _sym(path="src\\a.py")


def test_d1_a12_revision_pinning() -> None:
    assert _sym().revision == REV40
    BoundSymbol(symbol="f", declaring_type="C", file_path="a.py", revision=REV64)
    for bad in ("HEAD", "UNPINNED", "main", "v1.2.0", REV40[:7], "xyz"):
        with pytest.raises((EdgeError, ValidationError)):
            BoundSymbol(symbol="f", declaring_type="C", file_path="a.py", revision=bad)


def test_d1_a13_resolved_with_codes_rejected() -> None:
    with pytest.raises(EdgeError):
        _obs(_resolved(limitation_codes=[LimitationCode.MISSING_INPUT]), EdgeVariant.RESOLVED_EDGE)


def test_d1_a14_verify_round_trip_and_tamper() -> None:
    o = _obs(_resolved(), EdgeVariant.RESOLVED_EDGE)
    data = {
        "observation_id": o.observation_id,
        "variant": "RESOLVED_EDGE",
        "record_version": "edge/v1",
        "payload": o.payload.model_dump(mode="json"),
    }
    assert verify_observation(data).observation_id == o.observation_id
    tampered = dict(data)
    tampered["observation_id"] = "sha256:" + "0" * 64
    with pytest.raises(EdgeError):
        verify_observation(tampered)
    wrong_variant = dict(data, variant="PARTIAL_OBSERVATION")
    with pytest.raises(EdgeError):
        verify_observation(wrong_variant)


def test_d1_d01_lineage_full_tuple_identity() -> None:
    repo = _repo()
    l1 = _lineage(repo)
    l2 = Lineage(
        sources=[
            LineageSource(
                evidence_id="e1",
                evidence_digest="sha256:DIFFERENT",
                artifact_digest="sha256:s",
                scope="code",
                revision=repo.revision,
                repository=repo,
            )
        ],
        analyzer_run_id="r1",
        execution_identity="x1",
    )
    assert (
        _obs(_resolved(lineage=l1), EdgeVariant.RESOLVED_EDGE).observation_id
        != _obs(_resolved(lineage=l2), EdgeVariant.RESOLVED_EDGE).observation_id
    )
    l3 = Lineage(
        sources=[
            LineageSource(
                evidence_id="e1",
                evidence_digest="sha256:e",
                artifact_digest="sha256:s",
                scope="other-scope",
                revision=repo.revision,
                repository=repo,
            )
        ],
        analyzer_run_id="r1",
        execution_identity="x1",
    )
    assert (
        _obs(_resolved(lineage=l1), EdgeVariant.RESOLVED_EDGE).observation_id
        == _obs(_resolved(lineage=l3), EdgeVariant.RESOLVED_EDGE).observation_id
    )


def test_d1_d02_duplicate_source_rejected() -> None:
    repo = _repo()
    src = LineageSource(
        evidence_id="e1",
        evidence_digest="sha256:e",
        artifact_digest="sha256:s",
        scope="code",
        revision=repo.revision,
        repository=repo,
    )
    dup = Lineage(sources=[src, src], analyzer_run_id="r1", execution_identity="x1")
    with pytest.raises(EdgeError):
        _obs(_resolved(lineage=dup), EdgeVariant.RESOLVED_EDGE)


def test_d1_d03_repo_mismatch_and_revision_mismatch() -> None:
    repo = _repo()
    other_repo = _repo(name="other/repo", revision=REV64)
    bad_entry = Lineage(
        sources=[
            LineageSource(
                evidence_id="e1",
                evidence_digest="sha256:e",
                artifact_digest="sha256:s",
                scope="code",
                revision=REV40,
                repository=other_repo,
            )
        ],
        analyzer_run_id="r1",
        execution_identity="x1",
    )
    with pytest.raises(EdgeError):
        _obs(_resolved(lineage=bad_entry, repository=repo), EdgeVariant.RESOLVED_EDGE)
    rev_mismatch = Lineage(
        sources=[
            LineageSource(
                evidence_id="e1",
                evidence_digest="sha256:e",
                artifact_digest="sha256:s",
                scope="code",
                revision=REV64,
                repository=repo,
            )
        ],
        analyzer_run_id="r1",
        execution_identity="x1",
    )
    with pytest.raises(EdgeError):
        _obs(_resolved(lineage=rev_mismatch), EdgeVariant.RESOLVED_EDGE)


def test_d1_d04_single_repo_must_equal_observation_repo() -> None:
    other = _repo(name="other/repo", revision=REV64)
    lin = _lineage(other)
    with pytest.raises(EdgeError):
        _obs(_resolved(lineage=lin, repository=_repo()), EdgeVariant.RESOLVED_EDGE)


def test_d1_b01_comment_string_import_decoys_not_resolved() -> None:
    for form in (
        SyntaxForm.COMMENT,
        SyntaxForm.STRING_LITERAL,
        SyntaxForm.IMPORT_STATEMENT,
        SyntaxForm.NAME_MENTION,
    ):
        adj = adjudicate_resolution(form, True, CallerBinding.PARSER_SCOPE, True, 1)
        assert adj.variant != EdgeVariant.RESOLVED_EDGE, form


def test_d1_b02_name_match_binding_insufficient() -> None:
    adj = adjudicate_resolution(SyntaxForm.CALL_EXPRESSION, True, CallerBinding.NAME_MATCH, True, 1)
    assert adj.variant == EdgeVariant.PARTIAL_OBSERVATION and adj.missing == ["caller"]


def test_d1_b03_unbound_caller_partial() -> None:
    adj = adjudicate_resolution(SyntaxForm.CALL_EXPRESSION, True, CallerBinding.UNBOUND, True, 1)
    assert adj.variant == EdgeVariant.PARTIAL_OBSERVATION and adj.missing == ["caller"]


def test_d1_b04_ambiguous_target() -> None:
    adj = adjudicate_resolution(
        SyntaxForm.CALL_EXPRESSION, True, CallerBinding.PARSER_SCOPE, False, 3
    )
    assert adj.variant == EdgeVariant.PARTIAL_OBSERVATION
    assert adj.limitation_codes == [LimitationCode.AMBIGUOUS_TARGET]
    assert adj.missing == ["callee"]


def test_d1_b05_missing_and_unresolved_target() -> None:
    adj = adjudicate_resolution(
        SyntaxForm.CALL_EXPRESSION, True, CallerBinding.PARSER_SCOPE, False, 0
    )
    assert adj.limitation_codes == [LimitationCode.MISSING_INPUT]
    adj = adjudicate_resolution(
        SyntaxForm.CALL_EXPRESSION, True, CallerBinding.PARSER_SCOPE, False, 1
    )
    assert adj.limitation_codes == [LimitationCode.NOT_YET_RESOLVED]


def test_d1_b06_missing_syntax_kind_unresolved() -> None:
    adj = adjudicate_resolution(
        SyntaxForm.CALL_EXPRESSION, False, CallerBinding.PARSER_SCOPE, True, 1
    )
    assert adj.variant == EdgeVariant.UNRESOLVED_OBSERVATION


def test_d1_b07_true_triple_resolves() -> None:
    adj: Adjudication = adjudicate_resolution(
        SyntaxForm.CALL_EXPRESSION, True, CallerBinding.PARSER_SCOPE, True, 1
    )
    assert adj.variant == EdgeVariant.RESOLVED_EDGE and adj.limitation_codes == []


def test_d1_f01_partial_needs_codes_and_missing() -> None:
    with pytest.raises(ValidationError):
        _obs(
            PartialObservationPayload(
                caller=_sym(),
                repository=_repo(),
                resolver=_resolver(),
                lineage=_lineage(),
                limitation_codes=[],
                missing=[],
            ),
            EdgeVariant.PARTIAL_OBSERVATION,
        )
    with pytest.raises(EdgeError):
        _obs(
            PartialObservationPayload(
                caller=_sym(),
                repository=_repo(),
                resolver=_resolver(),
                lineage=_lineage(),
                limitation_codes=[LimitationCode.MISSING_INPUT],
                missing=["not-a-part"],
            ),
            EdgeVariant.PARTIAL_OBSERVATION,
        )


def test_d1_f02_unresolved_needs_codes() -> None:
    ref = RangeReference(file_path="a.py", revision=REV40, start_line=1, end_line=5)
    with pytest.raises(ValidationError):
        _obs(
            UnresolvedObservationPayload(
                observed_ref=ref,
                limitation_codes=[],
                repository=_repo(),
                resolver=_resolver(),
                lineage=_lineage(),
            ),
            EdgeVariant.UNRESOLVED_OBSERVATION,
        )
    sym = SymbolReference(symbol="g", revision=REV40)
    assert sym.declaring_type is None and sym.file_path is None
    o = _obs(
        UnresolvedObservationPayload(
            observed_ref=sym,
            limitation_codes=[LimitationCode.AMBIGUOUS_TARGET],
            repository=_repo(),
            resolver=_resolver(),
            lineage=_lineage(),
        ),
        EdgeVariant.UNRESOLVED_OBSERVATION,
    )
    assert o.variant == EdgeVariant.UNRESOLVED_OBSERVATION


def test_d1_f03_limitation_taxonomy_closed() -> None:
    assert limitation_category("AMBIGUOUS_TARGET").value == "AMBIGUOUS_TARGET"
    assert limitation_retryable("TRANSIENT_FAILURE") is True
    assert limitation_retryable("AMBIGUOUS_TARGET") is False
    assert limitation_downstream("MISSING_INPUT") == "invalidates_use"
    assert limitation_downstream("TRANSIENT_FAILURE") == "invalidates_use"
    with pytest.raises(EdgeError):
        limitation_category("FUTURE_CODE")
    with pytest.raises(EdgeError):
        limitation_retryable("FUTURE_CODE")
    with pytest.raises(EdgeError):
        limitation_downstream("FUTURE_CODE")


def test_d1_b08_adjudication_rejects_malformed_counts() -> None:
    with pytest.raises(EdgeError):
        adjudicate_resolution(
            SyntaxForm.CALL_EXPRESSION, True, CallerBinding.PARSER_SCOPE, False, -1
        )
    with pytest.raises(EdgeError):
        adjudicate_resolution(
            SyntaxForm.CALL_EXPRESSION, True, CallerBinding.PARSER_SCOPE, False, True
        )  # type: ignore[arg-type]


def test_d1_a15_expression_range_bool_lines_rejected() -> None:
    with pytest.raises((EdgeError, ValidationError)):
        ExpressionRange(start_line=True, start_col=0, end_line=2, end_col=0)  # type: ignore[dict-item]


def test_d1_f04_null_target_means_exempt_only_at_authz() -> None:
    o = _obs(_resolved(target_declaration=None), EdgeVariant.RESOLVED_EDGE)
    assert o.payload.target_declaration is None  # type: ignore[union-attr]
    raw = o.payload.model_dump(mode="json")  # type: ignore[union-attr]
    assert "target_declaration" in raw and raw["target_declaration"] is None
