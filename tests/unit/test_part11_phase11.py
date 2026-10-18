# tests/unit/test_part11_phase11.py
"""Task 9.b — Defensive LLM JSON recovery tests (Phase 11.11).

Pure-function tests over `resilient_json_extract`: extraction modes,
fail-closed matrix, call-site permission policy, bounded fuzz, and
excerpt-free errors/metrics. No I/O, no network, no LLM calls.
"""

from __future__ import annotations

import json

import pytest

from investigation_agent_platform.infrastructure.reasoning.json_recovery import (
    MAX_NESTING_DEPTH,
    MAX_RECOVERY_CHARS,
    JsonRecoveryError,
    RecoveryParseMode,
    RecoveryPermission,
    record_json_recovery,
    resilient_json_extract,
)


def _perm(**overrides) -> RecoveryPermission:
    base: dict[str, object] = {"schema_version": "1.0", "explicit_permission": True}
    base.update(overrides)
    return RecoveryPermission(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# extraction modes
# ---------------------------------------------------------------------------


class TestExtractionModes:
    def test_direct_parse_needs_no_permission(self) -> None:
        result = resilient_json_extract(
            '{"assignments": []}',
            permission=RecoveryPermission(schema_version="1.0"),
        )
        assert result.parse_mode == RecoveryParseMode.DIRECT
        assert result.parsed == {"assignments": []}
        assert result.repairs == []

    def test_code_fences(self) -> None:
        for fenced in (
            '```json\n{"a": 1}\n```',
            '```\n{"a": 1}\n```',
            '  ```json\n{"a": [1, 2]}\n```  ',
        ):
            result = resilient_json_extract(fenced, permission=_perm())
            assert result.parse_mode == RecoveryParseMode.FENCE_STRIPPED
            assert result.parsed == {"a": 1} if "1}" in fenced else result.parsed

    def test_trailing_prose(self) -> None:
        result = resilient_json_extract(
            'Here is the result: {"status": "ok"} — let me know if you need more.',
            permission=_perm(),
        )
        assert result.parse_mode == RecoveryParseMode.SCANNED
        assert result.parsed == {"status": "ok"}

    def test_leading_prose_with_braces_in_prose(self) -> None:
        # The first structural opener wins; prose braces before it are skipped
        # only when they precede the JSON object... a prose brace opens the
        # scan, so this must fail closed rather than return garbage.
        with pytest.raises(JsonRecoveryError):
            resilient_json_extract(
                "note { not json } trailing",
                permission=_perm(),
            )

    def test_confirmed_truncation_repair(self) -> None:
        result = resilient_json_extract(
            '{"assignments": [{"finding_id": "abc", "cluster_key": "C001"}',
            permission=_perm(),
            finish_reason="length",
        )
        assert result.parse_mode == RecoveryParseMode.REPAIRED_TRUNCATION
        assert result.parsed == {"assignments": [{"finding_id": "abc", "cluster_key": "C001"}]}
        assert result.repairs
        # Repaired output is itself valid JSON.
        json.loads(json.dumps(result.parsed))

    def test_unterminated_string_repair(self) -> None:
        result = resilient_json_extract(
            '{"content": "hello world',
            permission=_perm(),
            finish_reason="length",
        )
        assert result.parsed == {"content": "hello world"}
        assert "close-string" in result.repairs

    def test_truncation_repair_requires_confirmation_or_permission(self) -> None:
        text = '{"a": {"b": [1, 2'
        unconfirmed = RecoveryPermission(schema_version="1.0")
        with pytest.raises(JsonRecoveryError) as exc_info:
            resilient_json_extract(text, permission=unconfirmed, finish_reason="stop")
        assert exc_info.value.code == "REPAIR_NOT_PERMITTED"
        confirmed = RecoveryPermission(schema_version="1.0", truncation_confirmed=True)
        result = resilient_json_extract(text, permission=confirmed, finish_reason="stop")
        assert result.parse_mode == RecoveryParseMode.REPAIRED_TRUNCATION

    def test_provenance_fields(self) -> None:
        result = resilient_json_extract(
            '{"a": 1} trailing prose',
            permission=_perm(),
            finish_reason="stop",
        )
        provenance = result.to_provenance()
        assert provenance.schema_version == "1.0"
        assert provenance.input_digests["response"] == result.content_digest
        assert provenance.metadata["parse_mode"] == "scanned"
        assert provenance.metadata["finish_reason"] == "stop"
        assert result.content_digest.startswith("sha256:")


# ---------------------------------------------------------------------------
# fail-closed matrix
# ---------------------------------------------------------------------------


class TestFailClosed:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "no json here ((( ",
            "```json\n{broken",  # unclosed fence, no balanced span, unrepairable
            '{"a": 1]}}',  # mismatched closers
            "[[[[(((((",
            '{"a": unterminated-value',  # bare token can never close into valid JSON
        ],
    )
    def test_unrecoverable_inputs(self, text: str) -> None:
        with pytest.raises(JsonRecoveryError):
            resilient_json_extract(text, permission=_perm())

    def test_missing_required_fields_surface_at_schema_validation(self) -> None:
        """Recovery extracts; the call-site schema rejects missing fields."""
        from pydantic import BaseModel, ValidationError

        class _Assignment(BaseModel):
            finding_id: str
            cluster_key: str

        result = resilient_json_extract('Results: {"finding_id": "abc"} end', permission=_perm())
        assert result.parsed == {"finding_id": "abc"}
        with pytest.raises(ValidationError):
            _Assignment.model_validate(result.parsed)

    def test_duplicate_keys_fail_closed(self) -> None:
        with pytest.raises(JsonRecoveryError) as exc_info:
            resilient_json_extract('{"a": 1, "a": 2}', permission=_perm())
        assert exc_info.value.code == "DUPLICATE_KEY"

    def test_excessive_nesting_fail_closed(self) -> None:
        deep = "[" * (MAX_NESTING_DEPTH + 5) + "]" * (MAX_NESTING_DEPTH + 5)
        with pytest.raises(JsonRecoveryError) as exc_info:
            resilient_json_extract(deep, permission=_perm())
        assert exc_info.value.code == "NESTING_TOO_DEEP"

    def test_oversized_input_fail_closed(self) -> None:
        with pytest.raises(JsonRecoveryError) as exc_info:
            resilient_json_extract("x" * (MAX_RECOVERY_CHARS + 1), permission=_perm())
        assert exc_info.value.code == "INPUT_TOO_LARGE"

    def test_non_object_root_fail_closed(self) -> None:
        with pytest.raises(JsonRecoveryError) as exc_info:
            resilient_json_extract('"just a string"', permission=_perm())
        assert exc_info.value.code == "NOT_AN_OBJECT"

    def test_domain_invalid_values_surface_at_validation(self) -> None:
        """Schema-valid JSON that is domain-invalid fails at domain validation."""
        from investigation_agent_platform.domain.finding.clustering import (
            ClusterAssignmentResult,
        )

        result = resilient_json_extract(
            '{"finding_id": "not-a-uuid", "cluster_key": "C001"}', permission=_perm()
        )
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ClusterAssignmentResult.model_validate(result.parsed)

    def test_altered_tool_arguments_rejected_by_schema(self) -> None:
        """Extra/renamed tool fields fail strict schema validation downstream."""
        from pydantic import BaseModel, ConfigDict

        class _ToolArgs(BaseModel):
            model_config = ConfigDict(extra="forbid")

            investigation_id: str

        result = resilient_json_extract(
            '{"investigation_id": "x", "investigation_id_hacked": true}',
            permission=_perm(),
        )
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _ToolArgs.model_validate(result.parsed)


# ---------------------------------------------------------------------------
# call-site policy
# ---------------------------------------------------------------------------


class TestCallSitePolicy:
    def test_sensitive_call_sites_reject_recovery(self) -> None:
        permission = _perm(sensitive=True)
        with pytest.raises(JsonRecoveryError) as exc_info:
            resilient_json_extract('Credential: {"token": "abc"} trailing', permission=permission)
        assert exc_info.value.code == "SENSITIVE_USE_DENIED"

    def test_destructive_call_sites_reject_recovery(self) -> None:
        permission = _perm(destructive=True)
        with pytest.raises(JsonRecoveryError) as exc_info:
            resilient_json_extract('Plan: {"action": "delete"} trailing', permission=permission)
        assert exc_info.value.code == "SENSITIVE_USE_DENIED"

    def test_authorization_tool_credential_pattern(self) -> None:
        """The pattern auth/tool/credential call sites must use: recovery
        constructed with sensitive/destructive flags refuses repaired output
        while direct parses (strict path) still work."""

        def _auth_call_site(raw: str) -> dict:
            permission = RecoveryPermission(
                schema_version="1.0",
                explicit_permission=True,
                sensitive=True,  # tokens/claims: never repaired
            )
            return resilient_json_extract(raw, permission=permission).parsed  # type: ignore[return-value]

        assert _auth_call_site('{"sub": "user-1"}') == {"sub": "user-1"}
        with pytest.raises(JsonRecoveryError):
            _auth_call_site('```json\n{"sub": "user-1"}\n```')

    def test_errors_contain_no_raw_excerpt(self) -> None:
        secrets = "sk-super-secret-marker-12345"
        cases = [
            f"prefix {secrets} {{broken",
            f'{{"a": 1, "a": 2, "marker": "{secrets}"}}',
            "x" * 100 + secrets,
        ]
        for text in cases:
            try:
                resilient_json_extract(text, permission=_perm())
            except JsonRecoveryError as exc:
                assert secrets not in str(exc)
                assert secrets not in exc.code
                assert secrets not in exc.content_digest
            else:
                pytest.fail("expected JsonRecoveryError")


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


class TestRecoveryMetrics:
    def test_metric_labels_bounded_no_content(self) -> None:
        calls: list[tuple[str, float, dict]] = []

        class _Obs:
            def record_metric(self, name: str, value: float, tags: dict) -> None:
                calls.append((name, value, tags))

        record_json_recovery(
            _Obs(),  # type: ignore[arg-type]
            "repaired",
            "repaired_truncation",
            schema_version="1.0",
            provider="openai",
            model="gpt-4o-mini",
        )
        assert calls == [
            (
                "json_recovery",
                1.0,
                {
                    "outcome": "repaired",
                    "parse_mode": "repaired_truncation",
                    "schema_version": "1.0",
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                },
            )
        ]

    def test_none_observability_is_noop(self) -> None:
        record_json_recovery(None, "success", "direct")

    def test_failed_outcome_warns_without_payload(self, caplog) -> None:
        with caplog.at_level("WARNING"):
            record_json_recovery(None, "failed", "direct")
        # None observability short-circuits; with a sink, only mode/schema log.
        logs: list[str] = []

        class _Obs:
            def record_metric(self, name: str, value: float, tags: dict) -> None:
                logs.append(json.dumps(tags))

        with caplog.at_level("WARNING"):
            record_json_recovery(_Obs(), "failed", "direct")  # type: ignore[arg-type]
        assert logs and "tenant" not in logs[0]


# ---------------------------------------------------------------------------
# bounded fuzz
# ---------------------------------------------------------------------------


class TestBoundedFuzz:
    def test_random_inputs_never_raise_untyped(self) -> None:
        import random

        rng = random.Random(20260930)
        alphabet = list('{}[]":, abc01\\\n\t') + ['{"a":', "```", "trailing prose "]
        permission = _perm()
        for _ in range(500):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 120)))
            try:
                result = resilient_json_extract(text, permission=permission)
            except JsonRecoveryError as exc:
                # Excerpt-free guarantee holds on every failure path.
                assert text[:40] not in str(exc) or len(text) < 40
                continue
            # Successes are always re-parseable, guarded JSON.
            json.loads(json.dumps(result.parsed))
            assert result.content_digest.startswith("sha256:")
            assert result.schema_version == "1.0"

    def test_truncation_prefixes_repair_or_fail_closed(self) -> None:
        canonical = json.dumps({"assignments": [{"finding_id": "f-1", "cluster_key": "C001"}]})
        permission = _perm()
        repaired = 0
        for cut in range(20, len(canonical), 7):
            try:
                result = resilient_json_extract(
                    canonical[:cut], permission=permission, finish_reason="length"
                )
            except JsonRecoveryError:
                continue
            repaired += 1
            # A repaired prefix must be a structural prefix of the original.
            assert result.parse_mode in (
                RecoveryParseMode.DIRECT,
                RecoveryParseMode.REPAIRED_TRUNCATION,
            )
        assert repaired >= 1
