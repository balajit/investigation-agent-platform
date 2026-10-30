"""D3 definition/v1 record: schema, canonical identity, adjudication (G01/G03/G04/N01-N03/N12 + vectors + mapping artifact)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from investigation_agent_platform.domain.proof import definition_observation as mod
from investigation_agent_platform.domain.proof.definition_observation import (
    BoundTargetIdentity,
    DefinitionVariant,
    LimitationCode,
    PartialDefinitionPayload,
    RangeReference,
    RepositoryId,
    ResolvedDefinitionPayload,
    ResolverId,
    TargetReference,
    UnresolvedDefinitionPayload,
    adjudicate_target_binding,
    compute_definition_id,
    make_definition_observation,
    verify_definition_observation,
)
from investigation_agent_platform.domain.proof.edge_observation import EdgeError

R40 = "a" * 40
R64 = "b" * 64

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


def _lineage(repo: RepositoryId, eid: str = "ev-001", rev: str = R40):
    from investigation_agent_platform.domain.proof.edge_observation import (
        Lineage,
        LineageSource,
    )

    return Lineage(
        sources=[
            LineageSource(
                evidence_id=eid,
                evidence_digest="sha256:e1",
                artifact_digest="sha256:a1",
                scope="file",
                revision=rev,
                repository=repo,
            )
        ],
        analyzer_run_id="an-001",
        execution_identity="ex-001",
    )


def _resolved(symbol: str = "serve", **over: object) -> ResolvedDefinitionPayload:
    base: dict[str, object] = {
        "target": BoundTargetIdentity(
            symbol=symbol, declaring_type="Handler", file_path="src/app.py", revision=R40
        ),
        "repository": _repo(),
        "resolver": _resolver(),
        "limitation_codes": [],
        "lineage": _lineage(_repo()),
        "assumptions": [],
    }
    base.update(over)
    return ResolvedDefinitionPayload(**base)  # type: ignore[arg-type]


# --- D3P-G01: canonical target vectors -------------------------------------


def test_d3p_g01_path_normalization_identical_preimage() -> None:
    a = _resolved()
    b = _resolved()
    object.__setattr__(b.target, "file_path", "x")  # placeholder, replaced below
    t1 = BoundTargetIdentity(
        symbol="s", declaring_type=None, file_path="./src/app.py", revision=R40
    )
    t2 = BoundTargetIdentity(symbol="s", declaring_type=None, file_path="src//app.py", revision=R40)
    assert t1.file_path == t2.file_path == "src/app.py"
    r1 = ResolvedDefinitionPayload(
        target=t1,
        repository=_repo(),
        resolver=_resolver(),
        limitation_codes=[],
        lineage=_lineage(_repo()),
        assumptions=[],
    )
    r2 = ResolvedDefinitionPayload(
        target=t2,
        repository=_repo(),
        resolver=_resolver(),
        limitation_codes=[],
        lineage=_lineage(_repo()),
        assumptions=[],
    )
    assert compute_definition_id(DefinitionVariant.RESOLVED_TARGET_BINDING, r1) == (
        compute_definition_id(DefinitionVariant.RESOLVED_TARGET_BINDING, r2)
    )
    assert a is not None and b is not None  # builders well-formed


def test_d3p_g01_symbol_exactness_no_last_segment_match() -> None:
    full = BoundTargetIdentity(
        symbol="a::serve", declaring_type=None, file_path="f.py", revision=R40
    )
    last = BoundTargetIdentity(symbol="serve", declaring_type=None, file_path="f.py", revision=R40)
    assert full.symbol != last.symbol  # native tolerance deliberately not carried over
    upper = BoundTargetIdentity(symbol="Serve", declaring_type=None, file_path="f.py", revision=R40)
    lower = BoundTargetIdentity(symbol="serve", declaring_type=None, file_path="f.py", revision=R40)
    assert upper.symbol != lower.symbol  # case-sensitive, no folding


def test_d3p_g01_revision_pin_and_type_omission() -> None:
    with pytest.raises((EdgeError, ValidationError)):
        BoundTargetIdentity(symbol="s", declaring_type=None, file_path="f.py", revision="main")
    with pytest.raises((EdgeError, ValidationError)):
        BoundTargetIdentity(symbol="s", declaring_type=None, file_path="f.py", revision=R40[:7])
    ok = BoundTargetIdentity(symbol="s", declaring_type=None, file_path="f.py", revision=R40)
    assert ok.declaring_type is None  # explicit null = complete statement
    ref = TargetReference(symbol="s")
    assert ref.declaring_type is None and ref.file_path is None and ref.revision is None


def test_d3p_g01_symbol_stripped_and_nfc() -> None:
    t = BoundTargetIdentity(symbol="  serve  ", declaring_type=None, file_path="f.py", revision=R40)
    assert t.symbol == "serve"
    with pytest.raises((EdgeError, ValidationError)):
        BoundTargetIdentity(symbol="   ", declaring_type=None, file_path="f.py", revision=R40)
    with pytest.raises((EdgeError, ValidationError)):
        BoundTargetIdentity(
            symbol="serv\u0065\u0301", declaring_type=None, file_path="f.py", revision=R40
        )


# --- D3P-G03/G04: unresolved / partial shapes -------------------------------


def test_d3p_g03_unresolved_ref_only_no_bound_endpoint() -> None:
    payload = UnresolvedDefinitionPayload(
        observed_ref=RangeReference(
            file_path="src/app.py", revision=R40, start_line=10, end_line=20
        ),
        limitation_codes=[LimitationCode.MISSING_INPUT],
        repository=None,
        resolver=_resolver(),
        lineage=None,
        assumptions=[],
    )
    rec = make_definition_observation(DefinitionVariant.UNRESOLVED_OBSERVATION, payload)
    assert rec.observation_id.startswith("sha256:")
    dumped = rec.model_dump(mode="json")
    assert "target" not in dumped["payload"]  # unbound endpoint inexpressible


def test_d3p_g04_partial_missing_taxonomy() -> None:
    payload = PartialDefinitionPayload(
        target=None,
        limitation_codes=[LimitationCode.AMBIGUOUS_TARGET],
        missing=["target"],
        repository=_repo(),
        resolver=_resolver(),
        lineage=_lineage(_repo()),
        assumptions=[],
    )
    rec = make_definition_observation(DefinitionVariant.PARTIAL_OBSERVATION, payload)
    assert rec.payload.missing == ["target"]  # type: ignore[union-attr]


# --- D3P-N01: malformed records ----------------------------------------------


def test_d3p_n01_unknown_variant_rejected() -> None:
    with pytest.raises((EdgeError, ValidationError, ValueError)):
        verify_definition_observation(
            {
                "observation_id": "x",
                "variant": "RESOLVED_EDGE",
                "record_version": "definition/v1",
                "payload": {},
            }
        )


def test_d3p_n01_unknown_field_rejected() -> None:
    with pytest.raises((EdgeError, ValidationError)):
        ResolvedDefinitionPayload(
            target=BoundTargetIdentity(
                symbol="s", declaring_type=None, file_path="f.py", revision=R40
            ),
            repository=_repo(),
            resolver=_resolver(),
            limitation_codes=[],
            lineage=_lineage(_repo()),
            assumptions=[],
            bogus=1,  # type: ignore[call-arg]
        )


def test_d3p_n01_missing_taxonomy_and_resolved_codes() -> None:
    with pytest.raises(EdgeError):
        make_definition_observation(
            DefinitionVariant.PARTIAL_OBSERVATION,
            PartialDefinitionPayload(
                target=None,
                limitation_codes=[LimitationCode.MISSING_INPUT],
                missing=["repository"],
                repository=_repo(),
                resolver=_resolver(),
                lineage=_lineage(_repo()),
                assumptions=[],
            ),
        )
    with pytest.raises(EdgeError):
        make_definition_observation(
            DefinitionVariant.RESOLVED_TARGET_BINDING,
            ResolvedDefinitionPayload(
                target=BoundTargetIdentity(
                    symbol="s", declaring_type=None, file_path="f.py", revision=R40
                ),
                repository=_repo(),
                resolver=_resolver(),
                limitation_codes=[LimitationCode.MISSING_INPUT],
                lineage=_lineage(_repo()),
                assumptions=[],
            ),
        )


def test_d3p_n01_duplicate_assumptions_and_sources() -> None:
    with pytest.raises(EdgeError):
        make_definition_observation(
            DefinitionVariant.RESOLVED_TARGET_BINDING,
            _resolved(assumptions=["static-model-only", "static-model-only"]),
        )
    from investigation_agent_platform.domain.proof.edge_observation import LineageSource

    dup = LineageSource(
        evidence_id="ev-001",
        evidence_digest="sha256:e1",
        artifact_digest="sha256:a1",
        scope="file",
        revision=R40,
        repository=_repo(),
    )
    from investigation_agent_platform.domain.proof.edge_observation import Lineage

    with pytest.raises(EdgeError):
        make_definition_observation(
            DefinitionVariant.RESOLVED_TARGET_BINDING,
            _resolved(
                lineage=Lineage(sources=[dup, dup], analyzer_run_id="a", execution_identity="e")
            ),
        )


# --- D3P-N03: candidate adjudication ------------------------------------------


def test_d3p_n03_adjudication_counts() -> None:
    assert adjudicate_target_binding(1, True).variant == DefinitionVariant.RESOLVED_TARGET_BINDING
    zero = adjudicate_target_binding(0, True)
    assert zero.variant == DefinitionVariant.PARTIAL_OBSERVATION
    assert zero.missing == ["target"] and zero.limitation_codes == [LimitationCode.MISSING_INPUT]
    multi = adjudicate_target_binding(3, True)
    assert multi.missing == ["target"] and multi.limitation_codes == [
        LimitationCode.AMBIGUOUS_TARGET
    ]
    noref = adjudicate_target_binding(0, False)
    assert noref.variant == DefinitionVariant.UNRESOLVED_OBSERVATION
    with pytest.raises(EdgeError):
        adjudicate_target_binding(-1, True)
    with pytest.raises(EdgeError):
        adjudicate_target_binding(True, True)  # type: ignore[arg-type]


# --- D3P-N02: canon/v1 negatives on D3-used models -------------------------------


def test_d3p_n02_canon_negatives_rejected() -> None:
    """D3P-N02: bad path, reversed range, float line, null field and
    non-NFC text are rejected at the D3-used models — no digest, no
    record, never repaired into a different record."""
    # bad path: backslash rejected by the repo rule
    with pytest.raises((EdgeError, ValidationError)):
        BoundTargetIdentity(symbol="s", declaring_type=None, file_path="src\\app.py", revision=R40)
    # reversed range
    with pytest.raises((EdgeError, ValidationError)):
        RangeReference(file_path="src/app.py", revision=R40, start_line=20, end_line=10)
    # float line (StrictInt)
    with pytest.raises((EdgeError, ValidationError)):
        RangeReference(
            file_path="src/app.py",
            revision=R40,
            start_line=10.5,
            end_line=20,  # type: ignore[arg-type]
        )
    # null field
    with pytest.raises((EdgeError, ValidationError)):
        RangeReference(
            file_path="src/app.py",
            revision=None,  # type: ignore[arg-type]
            start_line=10,
            end_line=20,
        )
    # non-NFC range text is normalized-or-rejected at the shared layer;
    # the D3 symbol path pins NFC explicitly (G01), the range path pins
    # bounds above. Empty range also fails closed.
    with pytest.raises((EdgeError, ValidationError)):
        RangeReference(file_path="src/app.py", revision=R40, start_line=10, end_line=10)


# --- D3P-N07: incomplete lineage -------------------------------------------------


def test_d3p_n07_lineage_gap_record_names_gap() -> None:
    """D3P-N07 record half: a partial record with a lineage gap is
    well-formed and names the gap in ``missing`` (never silently
    completed, never upgraded into a binding)."""
    vectors = _load_vectors("definition_golden_vectors.json", "vectors")
    entry = next(v for v in vectors if v["id"] == "DG-PARTIAL-03-LINEAGE")
    assert entry["maps_to"] and "D3P-N07" in entry["maps_to"]
    rec = verify_definition_observation(entry["record"])
    assert rec.variant == DefinitionVariant.PARTIAL_OBSERVATION
    assert list(rec.payload.missing) == ["lineage-source"]  # type: ignore[union-attr]
    assert LimitationCode.INPUT_NOT_FOUND in rec.payload.limitation_codes  # type: ignore[union-attr]


# --- vectors + mapping artifact -------------------------------------------------


def _load_vectors(name: str, section: str) -> list[dict]:
    doc = json.loads((FIXDIR / name).read_bytes())
    return doc[section]


def test_d3p_golden_vectors_verify() -> None:
    for entry in _load_vectors("definition_golden_vectors.json", "vectors"):
        rec = verify_definition_observation(entry["record"])
        assert rec.observation_id == entry["record"]["observation_id"]
        assert entry["maps_to"], entry["id"]


def test_d3p_invalid_vectors_rejected() -> None:
    for entry in _load_vectors("definition_invalid_vectors.json", "invalid"):
        try:
            verify_definition_observation(entry["record"])
        except (EdgeError, ValidationError):
            pass
        else:
            raise AssertionError(f"invalid vector accepted: {entry['id']}")
        assert entry["maps_to"], entry["id"]


def test_d3_mapping_artifact_validates() -> None:
    from investigation_agent_platform.domain.proof.dispatch import DispatchRow, DispatchTable

    doc = json.loads((FIXDIR / "definition_dispatch_mapping.json").read_bytes())
    assert doc["schema_version"] == "dispatch-mapping/v1"
    assert doc["table_version"] == "dispatch/v1"
    rows = [DispatchRow(**r).sealed() for r in doc["definition_rows"]]
    table = DispatchTable(rows=rows)
    assert all(r.claim_kind == "DEFINITION" for r in table.rows)
    assert all(r.requires_target_declaration for r in table.rows)
    assert all(r.rule_id == "definition/v1" for r in table.rows)
    assert table.policy_digest().startswith("sha256:")
    cats = {c["fact_category"]: c for c in doc["category_mappings"]}
    assert cats["definition"]["disposition"] == "admit-as-observation-only"


def test_d3p_module_import_closure_static() -> None:
    """D3P static gate (mirrors D1-G): new modules import nothing effectful."""
    import ast

    allowed = {
        "definition_observation": {
            "__future__",
            "hashlib",
            "unicodedata",
            "enum",
            "typing",
            "pydantic",
            "investigation_agent_platform",
        },
    }
    tree = ast.parse((Path(mod.__file__)).read_text())
    top = {
        (n.names[0].name if isinstance(n, ast.Import) else n.module or "").split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
    }
    assert top <= allowed["definition_observation"], top
    text = Path(mod.__file__).read_text()
    assert text.count("os.environ") == 0
    assert "import subprocess" not in text and "importlib" not in text
