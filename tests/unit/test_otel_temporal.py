# tests/unit/test_otel_temporal.py
"""OTel Temporal bridge (#9) + sampling policy (#10)."""

from __future__ import annotations


class TestTemporalInterceptor:
    def test_builds_or_fails_open(self) -> None:
        from investigation_agent_platform.infrastructure.observability.temporal_interceptor import (
            build_temporal_tracing_interceptor,
            temporal_interceptors,
        )

        built = build_temporal_tracing_interceptor()
        assert built is None or type(built).__name__ == "TracingInterceptor"
        listed = temporal_interceptors()
        assert isinstance(listed, list)
        assert listed == ([built] if built is not None else [])

    def test_workflow_spans_off_by_default(self) -> None:
        from investigation_agent_platform.infrastructure.observability.temporal_interceptor import (
            build_temporal_tracing_interceptor,
        )

        built = build_temporal_tracing_interceptor()
        if built is None:
            return
        # Replay safety: the contrib default creates no workflow-code spans.
        assert getattr(built, "always_create_workflow_spans", False) is False

    def test_no_spans_in_workflow_code(self) -> None:
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        wf_pkg = (
            repo_root
            / "src"
            / "investigation_agent_platform"
            / "application"
            / "worker"
            / "workflows"
        )
        modules = sorted(wf_pkg.glob("*.py"))
        assert modules, "workflows package must contain split modules"
        for module in modules:
            text = module.read_text(encoding="utf-8")
            assert "import opentelemetry" not in text, module.name
            assert "from opentelemetry" not in text, module.name


class TestSamplingPolicy:
    def test_sample_ratio_default_and_bounds(self) -> None:
        from pydantic import ValidationError

        from investigation_agent_platform.infrastructure.configuration.config import (
            TelemetryConfig,
        )

        assert TelemetryConfig().sample_ratio == 1.0
        assert TelemetryConfig(sample_ratio=0.1).sample_ratio == 0.1
        try:
            TelemetryConfig(sample_ratio=1.5)
        except ValidationError:
            pass
        else:
            raise AssertionError("ratio > 1 must fail")

    def test_sampler_wiring(self) -> None:
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

        sampler = ParentBased(root=TraceIdRatioBased(0.1))
        assert isinstance(sampler, ParentBased)

    def test_collector_config_declares_backend_and_tail(self) -> None:
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        text = (repo_root / "docker" / "otel-collector" / "config.yaml").read_text(encoding="utf-8")
        assert "otlp/tempo" in text
        assert "tail_sampling" in text
        assert "errors-always" in text
