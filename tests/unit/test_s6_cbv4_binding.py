"""WP-05 (cbv4): Delta diff proof, Contrast final-gate binding, data-gated Mechanism-shift."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from investigation_agent_platform.application.investigation.verification import (
    CLAIM_BINDING_CBV4,
    CLAIM_BINDING_CURRENT,
    ContrastBindingInput,
    DeltaBindingInput,
    MechanismShiftInput,
    VerificationEngine,
    resolve_claim_binding_version,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.hypothesis.models import (
    AssessmentStrength,
    AssessmentType,
    ClaimKind,
    ClaimTarget,
    Hypothesis,
    HypothesisEvidenceAssessment,
    HypothesisStatus,
)
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def _ev(etype=EvidenceType.LOG, file_path="", revision="", at=None, summary="s", evidence_id=None, attributes=None):
    iid = uuid4()
    now = at or datetime.now(UTC)
    return Evidence(
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_id=evidence_id or uuid4(),
        evidence_type=etype,
        provider="prov-a" if etype != EvidenceType.DIVERGENCE else "code-diff",
        source="urn:src",
        title="t",
        summary=summary,
        fingerprint=f"fp-{uuid4()}",
        observed_at=now,
        retrieved_at=now,
        attributes=dict(attributes or {}),
        provenance=EvidenceProvenance(
            tenant_id="tenant-a",
            investigation_id=iid,
            provider_type="test",
            requested_provider_id="test",
            actual_provider_id="test",
            source_system="test",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="test", operation="test", normalized_query_hash="abc"
            ),
            source_location=SourceLocation(
                system="test",
                identifier="x",
                file_path=file_path,
                revision=revision,
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
    )


def _commit_row(path, revision, **kw):
    return _ev(EvidenceType.COMMIT_HISTORY, file_path=path, revision=revision, **kw)


def _witness(path="src/A.java", before="def456", after="abc123", **kw):
    return _ev(
        EvidenceType.DIVERGENCE, file_path=path, revision=after,
        attributes={"delta_witness": True, "target_path": path,
                    "source_ref": before, "target_ref": after},
        **kw,
    )


def _target(path="src/A.java", after="abc123", before="def456", subject="fixed the thing"):
    return ClaimTarget(file_path=path, commit_id=after, base_commit_id=before,
                       commit_subject=subject)


class TestDeltaProof:
    def test_missing_path_refused(self):
        ok, reason = VerificationEngine.delta_proof([], ClaimTarget(commit_id="a", base_commit_id="b"))
        assert ok is False and "file_path" in reason

    def test_missing_after_refused(self):
        ok, reason = VerificationEngine.delta_proof([], ClaimTarget(file_path="src/A.java", base_commit_id="b"))
        assert ok is False and "commit_id" in reason

    def test_missing_before_refused(self):
        ok, reason = VerificationEngine.delta_proof([], ClaimTarget(file_path="src/A.java", commit_id="a"))
        assert ok is False and "base_commit_id" in reason

    def test_same_before_after_refused(self):
        ok, _ = VerificationEngine.delta_proof([], _target(after="abc", before="abc"))
        assert ok is False

    def test_no_rows_refused(self):
        ok, _ = VerificationEngine.delta_proof([], _target())
        assert ok is False

    def test_commit_history_rows_are_not_witnesses(self):
        rows = [_commit_row("src/A.java", "abc123"), _commit_row("src/A.java", "def456")]
        ok, reason = VerificationEngine.delta_proof(rows, _target())
        assert ok is False and "witness" in reason

    def test_wrong_path_witness_refused(self):
        rows = [_witness(path="src/B.java")]
        ok, _ = VerificationEngine.delta_proof(rows, _target())
        assert ok is False

    def test_mismatched_anchors_refused(self):
        rows = [_witness(before="xxx111", after="yyy222")]
        ok, reason = VerificationEngine.delta_proof(rows, _target())
        assert ok is False and "abc123" in reason or "def456" in reason

    def test_witness_without_flags_refused(self):
        rows = [_ev(EvidenceType.DIVERGENCE, file_path="src/A.java", revision="abc123",
                    attributes={"target_path": "src/A.java"})]
        ok, _ = VerificationEngine.delta_proof(rows, _target())
        assert ok is False

    def test_subject_text_never_suffices(self):
        # A prose-matching commit-history row is a pointer, not a witness.
        rows = [_commit_row("src/A.java", "abc123", summary="fixed the thing")]
        ok, _ = VerificationEngine.delta_proof(rows, _target())
        assert ok is False

    def test_matching_witness_proves(self):
        ok, reason = VerificationEngine.delta_proof([_witness()], _target())
        assert (ok, reason) == (True, "")

    def test_malformed_witness_skipped_valid_witness_proves(self):
        bad = _ev(EvidenceType.DIVERGENCE, file_path="src/A.java", revision="zzz")
        ok, _ = VerificationEngine.delta_proof([bad, _witness()], _target())
        assert ok is True


class TestContrastBindingProof:
    def _halves(self):
        base = datetime.now(UTC)
        old = _ev(EvidenceType.LOG, file_path="svc/old.log", at=base - timedelta(days=2))
        new = _ev(EvidenceType.LOG, file_path="svc/new.log", at=base)
        return old, new, base

    def _code_row(self, path="src/A.java", revision="abc123", evidence_id=None):
        return _ev(EvidenceType.SOURCE_CODE, file_path=path, revision=revision, evidence_id=evidence_id)

    def test_missing_assumption_refused(self):
        old, new, _ = self._halves()
        ok, reason = VerificationEngine.contrast_binding_proof(old, new, [], [], "")
        assert ok is False and "assumption" in reason

    def test_misordered_pair_refused(self):
        earlier, later, _ = self._halves()
        row = self._code_row()
        ok, _ = VerificationEngine.contrast_binding_proof(
            later, earlier, [row], [str(row.evidence_id)], "assumption X", target_path="src/A.java"
        )
        assert ok is False

    def test_old_after_g_refused(self):
        old, new, base = self._halves()
        row = self._code_row()
        ok, _ = VerificationEngine.contrast_binding_proof(
            old, new, [row], [str(row.evidence_id)], "assumption X",
            g=base - timedelta(days=10), target_path="src/A.java",
        )
        assert ok is False

    def test_new_before_t_refused(self):
        old, new, base = self._halves()
        row = self._code_row()
        ok, _ = VerificationEngine.contrast_binding_proof(
            old, new, [row], [str(row.evidence_id)], "assumption X",
            t=base + timedelta(days=10), target_path="src/A.java",
        )
        assert ok is False

    def test_no_code_row_refused(self):
        old, new, _ = self._halves()
        ok, _ = VerificationEngine.contrast_binding_proof(old, new, [], [], "assumption X")
        assert ok is False

    def test_uncited_code_row_refused(self):
        old, new, _ = self._halves()
        row = self._code_row()
        ok, _ = VerificationEngine.contrast_binding_proof(old, new, [row], ["other-id"], "assumption X")
        assert ok is False

    def test_unpinned_code_row_refused(self):
        old, new, _ = self._halves()
        row = self._code_row(revision="")
        ok, _ = VerificationEngine.contrast_binding_proof(
            old, new, [row], [str(row.evidence_id)], "assumption X"
        )
        assert ok is False

    def test_unrelated_path_code_row_refused(self):
        old, new, _ = self._halves()
        row = self._code_row(path="src/Other.java")
        ok, _ = VerificationEngine.contrast_binding_proof(
            old, new, [row], [str(row.evidence_id)], "assumption X", target_path="src/A.java"
        )
        assert ok is False

    def test_non_code_row_refused(self):
        old, new, _ = self._halves()
        row = _ev(EvidenceType.LOG, file_path="src/A.java", revision="abc123")
        ok, _ = VerificationEngine.contrast_binding_proof(
            old, new, [row], [str(row.evidence_id)], "assumption X", target_path="src/A.java"
        )
        assert ok is False

    def test_bound_pair_proves(self):
        old, new, base = self._halves()
        row = self._code_row()
        ok, reason = VerificationEngine.contrast_binding_proof(
            old, new, [row], [str(row.evidence_id)], "assumption X",
            g=base - timedelta(days=1), t=base - timedelta(hours=1),
            target_path="src/A.java",
        )
        assert (ok, reason) == (True, "")


class TestMechanismShiftGate:
    def _binding(self, **kw):
        args = dict(
            metric_name="checkout_error_rate",
            parent_symbols=["checkout", "auth"],
            stable_parent_symbols=["checkout", "auth"],
            boundary_start=datetime.now(UTC) - timedelta(hours=2),
            boundary_end=datetime.now(UTC) - timedelta(hours=1),
            telemetry_available=True,
            investigation_id=uuid4(),
        )
        args.update(kw)
        return MechanismShiftInput(**args)

    def _rows(self, base, before_n=3, after_n=6):
        rows = []
        for i in range(before_n):
            rows.append(_ev(EvidenceType.RUNTIME_LOG, at=base - timedelta(hours=3, minutes=i)))
        for i in range(after_n):
            rows.append(_ev(EvidenceType.RUNTIME_LOG, at=base - timedelta(minutes=30 - i)))
        return rows

    def test_missing_input_abstains(self):
        out = VerificationEngine.mechanism_shift_proof(None)
        assert (out.proved, out.code) == (False, "MECHSHIFT_NO_INPUT")

    def test_missing_metric_abstains(self):
        out = VerificationEngine.mechanism_shift_proof(self._binding(metric_name=""))
        assert out.code == "MECHSHIFT_NO_METRIC" and out.proved is False

    def test_missing_parents_abstain(self):
        out = VerificationEngine.mechanism_shift_proof(self._binding(parent_symbols=[]))
        assert out.code == "MECHSHIFT_NO_PARENTS" and out.proved is False

    def test_unstable_parents_abstain(self):
        out = VerificationEngine.mechanism_shift_proof(self._binding(stable_parent_symbols=["checkout"]))
        assert out.code.startswith("MECHSHIFT_PARENTS_UNSTABLE")

    def test_missing_boundary_abstains(self):
        out = VerificationEngine.mechanism_shift_proof(self._binding(boundary_start=None))
        assert out.code == "MECHSHIFT_NO_BOUNDARY"

    def test_inverted_boundary_abstains(self):
        now = datetime.now(UTC)
        out = VerificationEngine.mechanism_shift_proof(
            self._binding(boundary_start=now, boundary_end=now - timedelta(hours=1))
        )
        assert out.code == "MECHSHIFT_NO_BOUNDARY"

    def test_missing_telemetry_abstains(self):
        out = VerificationEngine.mechanism_shift_proof(self._binding(telemetry_available=False))
        assert out.code == "MECHSHIFT_NO_TELEMETRY"

    def test_missing_samples_abstain(self):
        out = VerificationEngine.mechanism_shift_proof(self._binding(), [])
        assert (out.proved, out.before_count, out.after_count, out.rate_ratio) == (False, 0, 0, None)
        assert out.code == "MECHSHIFT_INSUFFICIENT_SAMPLES"

    def test_non_telemetry_rows_ignored(self):
        base = datetime.now(UTC)
        rows = [self._code_row_like(path="src/A.java", revision="a", at=base - timedelta(hours=3))]
        out = VerificationEngine.mechanism_shift_proof(self._binding(), rows)
        assert out.code == "MECHSHIFT_INSUFFICIENT_SAMPLES"

    def _code_row_like(self, path, revision, at):
        return _ev(EvidenceType.SOURCE_CODE, file_path=path, revision=revision, at=at)

    def test_shift_below_threshold_abstains_with_stats(self):
        base = datetime.now(UTC)
        out = VerificationEngine.mechanism_shift_proof(self._binding(), self._rows(base, 6, 6))
        assert out.proved is False and out.code == "MECHSHIFT_SHIFT_BELOW_THRESHOLD"
        assert (out.before_count, out.after_count) == (6, 6)
        assert out.rate_ratio is not None and out.rate_ratio < 2.0

    def test_shift_proves_at_ratio(self):
        base = datetime.now(UTC)
        out = VerificationEngine.mechanism_shift_proof(self._binding(), self._rows(base, 2, 8))
        assert out.proved is True and out.code == "MECHSHIFT_SHIFT_PROVED"
        assert (out.before_count, out.after_count) == (2, 8)

    def test_numbers_tune_without_logic_change(self):
        from investigation_agent_platform.application.investigation.verification import (
            MechanismShiftThresholds,
        )

        base = datetime.now(UTC)
        rows = self._rows(base, 6, 6)
        default_out = VerificationEngine.mechanism_shift_proof(self._binding(), rows)
        relaxed = MechanismShiftThresholds(
            min_samples_per_side=2, min_rate_ratio=1.0, required_parent_hold_fraction=1.0
        )
        tuned_out = VerificationEngine.mechanism_shift_proof(self._binding(), rows, relaxed)
        assert default_out.proved is False and tuned_out.proved is True
        assert (tuned_out.before_count, tuned_out.after_count) == (6, 6)

    def test_parent_fraction_tunes(self):
        from investigation_agent_platform.application.investigation.verification import (
            MechanismShiftThresholds,
        )

        base = datetime.now(UTC)
        rows = self._rows(base, 2, 8)
        binding = self._binding(stable_parent_symbols=["checkout"])
        strict = VerificationEngine.mechanism_shift_proof(binding, rows)
        relaxed_t = MechanismShiftThresholds(
            min_samples_per_side=2, min_rate_ratio=2.0, required_parent_hold_fraction=0.5
        )
        tuned = VerificationEngine.mechanism_shift_proof(binding, rows, relaxed_t)
        assert strict.code.startswith("MECHSHIFT_PARENTS_UNSTABLE") and tuned.proved is True


class TestCbv4GroupGate:
    """Final-gate wiring: model fields drive delta/contrast/mechanism checks."""

    def _asm(self, hyp, kind, target, evidence_id=None):
        return HypothesisEvidenceAssessment(
            hypothesis_id=hyp.id,
            evidence_id=evidence_id,
            assessment=AssessmentType.SUPPORTS,
            strength=AssessmentStrength.STRONG,
            reason="claim",
            claim_kind=kind,
            claim_target=target,
        )

    def _hyp(self, statement, kind, target):
        iid = uuid4()
        hyp = Hypothesis(
            tenant_id="tenant-a",
            investigation_id=iid,
            title=statement[:256],
            description=statement,
            statement=statement,
            status=HypothesisStatus.PROPOSED,
        )
        return hyp.model_copy(update={"assessments": [self._asm(hyp, kind, target)]})

    def _history_pair(self, base, path="src/A.java", after="abc123", before="def456"):
        after_row = _ev(
            EvidenceType.COMMIT_HISTORY, file_path=path, revision=after,
            summary="fixed the thing", at=base,
        )
        before_row = _ev(
            EvidenceType.COMMIT_HISTORY, file_path=path, revision=before,
            summary="earlier change", at=base - timedelta(days=1),
        )
        witness = _witness(path=path, before=before, after=after)
        return after_row, before_row, witness

    def _history_target(self, after_row, before_row, witness, path="src/A.java",
                        after="abc123", before="def456"):
        return ClaimTarget(
            file_path=path, commit_id=after, base_commit_id=before,
            commit_subject="fixed the thing",
            delta_evidence_ids=[witness.evidence_id],
        )

    def test_history_delta_gate_proves(self):
        from investigation_agent_platform.application.investigation.verification import (
            RootCauseVerificationPolicy,
        )

        base = datetime.now(UTC)
        after_row, before_row, witness = self._history_pair(base)
        target = self._history_target(after_row, before_row, witness)
        hyp = self._hyp("fixed the thing in src/A.java", ClaimKind.HISTORY, target)
        engine = VerificationEngine(claim_binding_version="cbv4")
        assert engine._group_verifies(
            hyp, "HISTORY", [after_row, before_row, witness], [], [],
            RootCauseVerificationPolicy(),
        ) is True

    def test_history_delta_gate_fails_without_witness(self):
        from investigation_agent_platform.application.investigation.verification import (
            RootCauseVerificationPolicy,
        )

        base = datetime.now(UTC)
        after_row, before_row, witness = self._history_pair(base)
        target = self._history_target(after_row, before_row, witness)
        hyp = self._hyp("fixed the thing in src/A.java", ClaimKind.HISTORY, target)
        engine = VerificationEngine(claim_binding_version="cbv4")
        assert engine._group_verifies(
            hyp, "HISTORY", [after_row, before_row], [], [],
            RootCauseVerificationPolicy(),
        ) is False

    def test_contrast_gate_bound_to_history_group(self):
        from investigation_agent_platform.application.investigation.verification import (
            RootCauseVerificationPolicy,
        )

        base = datetime.now(UTC)
        after_row, before_row, witness = self._history_pair(base)
        old = _ev(EvidenceType.LOG, file_path="svc/old.log", at=base - timedelta(days=2))
        new = _ev(EvidenceType.LOG, file_path="svc/new.log", at=base)
        code = _ev(EvidenceType.SOURCE_CODE, file_path="src/A.java", revision="abc123")
        target = self._history_target(after_row, before_row, witness)
        target = target.model_copy(update=dict(
            contrast_old_evidence_id=old.evidence_id,
            contrast_new_evidence_id=new.evidence_id,
            contrast_code_evidence_ids=[code.evidence_id],
        ))
        hyp = self._hyp("fixed the thing in src/A.java", ClaimKind.HISTORY, target)
        engine = VerificationEngine(claim_binding_version="cbv4")
        rows = [after_row, before_row, witness, old, new, code]
        assert engine._group_verifies(
            hyp, "HISTORY", rows, [], [], RootCauseVerificationPolicy()
        ) is True
        # Same claim, but the code row never banked: gate fails closed.
        assert engine._group_verifies(
            hyp, "HISTORY", [after_row, before_row, witness, old, new], [], [],
            RootCauseVerificationPolicy(),
        ) is False

    def test_mechanism_gate_abstains_without_telemetry_rows(self):
        from investigation_agent_platform.application.investigation.verification import (
            RootCauseVerificationPolicy,
        )

        base = datetime.now(UTC)
        target = ClaimTarget(
            file_path="src/A.java",
            mechanism_metric="error_rate",
            mechanism_parent_symbols=["a", "b"],
            mechanism_stable_parent_symbols=["a", "b"],
            mechanism_boundary_start=base - timedelta(hours=2),
            mechanism_boundary_end=base - timedelta(hours=1),
        )
        hyp = self._hyp("mechanism claim", ClaimKind.CAUSAL, target)
        engine = VerificationEngine(claim_binding_version="cbv4")
        assert engine._group_verifies(
            hyp, "CAUSAL", [], [], [], RootCauseVerificationPolicy()
        ) is False

    def test_mechanism_gate_tuned_through_engine(self):
        from investigation_agent_platform.application.investigation.verification import (
            MechanismShiftThresholds,
            RootCauseVerificationPolicy,
        )

        base = datetime.now(UTC)
        after_hist, before_hist, witness = self._history_pair(base)
        rows = [after_hist, before_hist, witness]
        for i in range(6):  # before side: 6 rows over ~1h ending at the boundary start
            rows.append(_ev(EvidenceType.RUNTIME_LOG, at=base - timedelta(hours=3, minutes=-i * 10)))
        for i in range(6):  # after side: 6 rows over ~35min after the boundary end
            rows.append(_ev(EvidenceType.RUNTIME_LOG, at=base - timedelta(minutes=30 - i)))
        target = ClaimTarget(
            file_path="src/A.java", commit_id="abc123", base_commit_id="def456",
            commit_subject="fixed the thing",
            delta_evidence_ids=[witness.evidence_id],
            mechanism_metric="error_rate",
            mechanism_parent_symbols=["a", "b"],
            mechanism_stable_parent_symbols=["a", "b"],
            mechanism_boundary_start=base - timedelta(hours=2),
            mechanism_boundary_end=base - timedelta(hours=1),
        )
        hyp = self._hyp("fixed the thing in src/A.java", ClaimKind.HISTORY, target)
        strict = VerificationEngine(claim_binding_version="cbv4")
        assert strict._group_verifies(
            hyp, "HISTORY", rows, [], [], RootCauseVerificationPolicy()
        ) is False
        relaxed = VerificationEngine(
            claim_binding_version="cbv4",
            mechanism_thresholds=MechanismShiftThresholds(
                min_samples_per_side=2, min_rate_ratio=1.0, required_parent_hold_fraction=1.0
            ),
        )
        assert relaxed._group_verifies(
            hyp, "HISTORY", rows, [], [], RootCauseVerificationPolicy()
        ) is True
    def test_current_stays_cbv1(self):
        assert CLAIM_BINDING_CURRENT == "cbv1"

    def test_cbv4_resolves_pinned(self):
        version, source = resolve_claim_binding_version({"claim_binding_version": "cbv4"})
        assert (version, source) == (CLAIM_BINDING_CBV4, "pinned")

    def test_unknown_pin_still_fails_closed(self):
        from investigation_agent_platform.application.investigation.verification import (
            UnknownVerifierVersion,
        )

        with pytest.raises(UnknownVerifierVersion):
            resolve_claim_binding_version({"claim_binding_version": "cbv9"})

    def test_cbv4_history_without_base_does_not_verify(self):
        iid = uuid4()
        hyp = Hypothesis(
            tenant_id="tenant-a",
            investigation_id=iid,
            title="change happened",
            description="change happened",
            statement="change happened",
            status=HypothesisStatus.PROPOSED,
            claim_kind=ClaimKind.HISTORY,
            claim_target=ClaimTarget(file_path="src/A.java", commit_id="abc123"),
        )
        rows = [
            _ev(EvidenceType.COMMIT_HISTORY, file_path="src/A.java", revision="abc123"),
        ]
        engine = VerificationEngine(claim_binding_version="cbv4")
        assert engine.validate_history_claim(hyp, rows, []) is None

    def test_input_models_exist(self):
        iid = uuid4()
        DeltaBindingInput(
            target_file="src/A.java", before_revision="a", after_revision="b", investigation_id=iid
        )
        ContrastBindingInput(broken_assumption="assumption X", investigation_id=iid)
        MechanismShiftInput(
            metric_name="m", parent_symbols=["p"], investigation_id=iid,
        )
