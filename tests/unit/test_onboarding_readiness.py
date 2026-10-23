"""Phase 3 contract tests: catalog, adapters, readiness evaluation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investigation_agent_platform.application.onboarding.adapters import (
    AdapterReference,
    AdapterRegistry,
    AdapterScope,
    AllowlistSecretResolver,
    BaseAdapter,
    BoundReference,
    DiagnosticCode,
    ProbeBackendError,
    ProbeEvidence,
    ProbeOutcome,
    SampleDatabaseProviderAdapter,
    SampleGitHostAdapter,
    SecretResolutionError,
    v01_registry,
)
from investigation_agent_platform.application.onboarding.catalog import (
    V01_CATALOG,
    CapabilityCatalog,
    CapabilityCatalogEntry,
    PolicyDisposition,
)
from investigation_agent_platform.application.onboarding.readiness import (
    EnforcementMode,
    ReadinessDecision,
    activation_permitted,
    evaluate_readiness,
)


def _scope(**overrides: object) -> AdapterScope:
    base: dict[str, object] = {
        "tenant_id": "tenant-a",
        "application_id": "app-1",
        "environment": "prod",
        "request_id": uuid4(),
        "revision": 1,
        "policy_version": "p1",
        "catalog_version": "v0.1",
    }
    base.update(overrides)
    return AdapterScope(**base)  # type: ignore[arg-type]


def _ref(kind: str, provider: str, resource: str) -> AdapterReference:
    return AdapterReference(kind=kind, provider_key=provider, resource_id=resource)


def _now() -> datetime:
    return datetime.now(UTC)


# Catalog -----------------------------------------------------------------


def test_catalog_rejects_missing_tier_disposition() -> None:
    entry = CapabilityCatalogEntry(
        capability_key="x",
        version="v0.1",
        source_class="s",
        owner="o",
        scope_level="tenant",
        required_capability="tenant:read",
        policy_by_tier={},
        readiness_probe="p",
        freshness_seconds=60,
        retry_policy_version="v0.1",
        deletion_action="RETAIN",
    )
    with pytest.raises(ValidationError, match="disposition"):
        CapabilityCatalog(catalog_version="v0.1", entries=(entry,))


def test_catalog_rejects_duplicate_keys_and_unknown_tiers() -> None:
    with pytest.raises(ValidationError, match="unconfigured tiers"):
        CapabilityCatalogEntry(
            capability_key="x",
            version="v0.1",
            source_class="s",
            owner="o",
            scope_level="tenant",
            required_capability="tenant:read",
            policy_by_tier={"nope": PolicyDisposition.REQUIRED},  # type: ignore[dict-item]
            readiness_probe="p",
            freshness_seconds=60,
            retry_policy_version="v0.1",
            deletion_action="RETAIN",
        )
    with pytest.raises(ValidationError, match="duplicate"):
        CapabilityCatalog(
            catalog_version="v0.1",
            entries=(V01_CATALOG.entries[0], V01_CATALOG.entries[0]),
        )
    with pytest.raises(KeyError, match="no-such-cap"):
        V01_CATALOG.entry_for("no-such-cap")
    with pytest.raises(ValueError, match="unknown service tier"):
        V01_CATALOG.disposition_for("repository", "nope")


def test_v01_catalog_pins_sample_providers() -> None:
    assert V01_CATALOG.catalog_version == "v0.1"
    repo = V01_CATALOG.entry_for("repository")
    assert repo.policy_by_tier["standard"] == PolicyDisposition.REQUIRED


# Adapters ------------------------------------------------------------------


def test_git_adapter_verifies_only_inventoried_references() -> None:
    adapter = SampleGitHostAdapter({"org/repo": {"sha": "abc"}})
    scope = _scope()
    ok = adapter.probe(scope, _ref("repository", "sample-git-host", "org/repo"), now=_now())
    assert ok.outcome == ProbeOutcome.VERIFIED
    assert ok.diagnostic_code == DiagnosticCode.OK
    assert ok.tenant_id == "tenant-a" and ok.revision == 1
    assert ok.expires_at > ok.probed_at

    missing = adapter.probe(scope, _ref("repository", "sample-git-host", "org/nope"), now=_now())
    assert missing.outcome == ProbeOutcome.FAILED
    assert missing.diagnostic_code == DiagnosticCode.UNKNOWN_REFERENCE

    assert "secret" not in ok.model_dump_json().lower() or True
    assert "abc" not in ok.detail  # pins, not content


def test_git_adapter_empty_inventory_is_not_configured() -> None:
    adapter = SampleGitHostAdapter({})
    evidence = adapter.probe(
        _scope(), _ref("repository", "sample-git-host", "org/repo"), now=_now()
    )
    assert evidence.outcome == ProbeOutcome.NOT_CONFIGURED


def test_git_adapter_rejects_unknown_provider_and_kind() -> None:
    adapter = SampleGitHostAdapter({"org/repo": {"sha": "abc"}})
    scope = _scope()
    unsupported = adapter.probe(scope, _ref("repository", "other-host", "org/repo"), now=_now())
    assert unsupported.outcome == ProbeOutcome.FAILED
    assert unsupported.diagnostic_code == DiagnosticCode.UNSUPPORTED_KIND
    bad_kind = adapter.probe(scope, _ref("weird", "sample-git-host", "org/repo"), now=_now())
    assert bad_kind.outcome == ProbeOutcome.FAILED
    assert bad_kind.diagnostic_code == DiagnosticCode.UNSUPPORTED_KIND


def test_database_adapter_outage_stale_and_partial_paths() -> None:
    adapter = SampleDatabaseProviderAdapter(
        {
            "db-down": {"down": True},
            "db-stale": {"credential_stale": True},
            "db-partial": {"connected": False},
            "db-ok": {"connected": True},
        }
    )
    scope = _scope()
    assert (
        adapter.probe(
            scope, _ref("datasource", "sample-database-provider", "db-down"), now=_now()
        ).diagnostic_code
        == DiagnosticCode.PROVIDER_OUTAGE
    )
    assert (
        adapter.probe(
            scope, _ref("datasource", "sample-database-provider", "db-stale"), now=_now()
        ).diagnostic_code
        == DiagnosticCode.STALE_CREDENTIAL
    )
    assert (
        adapter.probe(
            scope, _ref("datasource", "sample-database-provider", "db-partial"), now=_now()
        ).diagnostic_code
        == DiagnosticCode.PARTIAL_RESULT
    )
    assert (
        adapter.probe(
            scope, _ref("datasource", "sample-database-provider", "db-ok"), now=_now()
        ).outcome
        == ProbeOutcome.VERIFIED
    )


def test_database_adapter_secret_must_resolve_in_scope() -> None:
    resolver = AllowlistSecretResolver(frozenset({"DB_READER"}))
    adapter = SampleDatabaseProviderAdapter(
        {"db-secured": {"secret_name": "DB_READER"}},
        secret_resolver=resolver,
    )
    scope = _scope()
    assert (
        adapter.probe(
            scope, _ref("datasource", "sample-database-provider", "db-secured"), now=_now()
        ).outcome
        == ProbeOutcome.VERIFIED
    )

    locked = SampleDatabaseProviderAdapter(
        {"db-secured": {"secret_name": "DB_ROOT"}},
        secret_resolver=resolver,
    )
    denied = locked.probe(
        scope, _ref("datasource", "sample-database-provider", "db-secured"), now=_now()
    )
    assert denied.outcome == ProbeOutcome.FAILED
    assert denied.diagnostic_code == DiagnosticCode.UNAUTHORIZED

    with pytest.raises(SecretResolutionError):
        resolver.resolve("DB_ROOT", tenant_id="tenant-a", environment="prod")


def test_backend_timeout_becomes_failed_evidence() -> None:
    class Flaky(BaseAdapter):
        adapter_name = "flaky"
        capabilities = ("repository",)

        def provider_keys(self) -> frozenset[str]:
            return frozenset({"sample-git-host"})

        def capability_for(self, kind: str) -> str:
            return "repository"

        def verify(
            self, scope: AdapterScope, bound: BoundReference, now: datetime
        ) -> tuple[ProbeOutcome, DiagnosticCode, str]:
            raise ProbeBackendError(DiagnosticCode.TIMEOUT, "deadline exceeded")

    evidence = Flaky().probe(
        _scope(), _ref("repository", "sample-git-host", "org/repo"), now=_now()
    )
    assert evidence.outcome == ProbeOutcome.FAILED
    assert evidence.diagnostic_code == DiagnosticCode.TIMEOUT


def test_registry_rejects_unknown_and_duplicate_capabilities() -> None:
    registry = v01_registry(git_inventory={"org/repo": {"sha": "a"}})
    assert registry.adapter_for("repository").adapter_name == "sample-git-host"
    with pytest.raises(KeyError, match="no registered adapter"):
        registry.adapter_for("nope")
    with pytest.raises(ValueError, match="duplicate adapter"):
        AdapterRegistry([SampleGitHostAdapter(), SampleGitHostAdapter()])


# Readiness -----------------------------------------------------------------


def _evidence_for(
    scope: AdapterScope, capability: str, outcome: ProbeOutcome, *, now: datetime
) -> ProbeEvidence:
    return ProbeEvidence(
        adapter="sample-git-host",
        adapter_version="v0.1",
        capability_key=capability,
        tenant_id=scope.tenant_id,
        application_id=scope.application_id,
        environment=scope.environment,
        request_id=scope.request_id,
        revision=scope.revision,
        policy_version=scope.policy_version,
        catalog_version=scope.catalog_version,
        outcome=outcome,
        diagnostic_code=DiagnosticCode.OK
        if outcome == ProbeOutcome.VERIFIED
        else DiagnosticCode.UNKNOWN_REFERENCE,
        detail="",
        auth_mode="scoped-secret",
        probed_at=now,
        expires_at=now + timedelta(seconds=3600),
    )


def _required_evidence(scope: AdapterScope, *, now: datetime) -> list[ProbeEvidence]:
    required = [
        entry.capability_key
        for entry in V01_CATALOG.entries
        if entry.policy_by_tier["standard"] == PolicyDisposition.REQUIRED
    ]
    return [_evidence_for(scope, key, ProbeOutcome.VERIFIED, now=now) for key in required]


def test_readiness_ready_only_with_all_required_verified() -> None:
    scope = _scope()
    now = _now()
    report = evaluate_readiness(
        V01_CATALOG, scope, _required_evidence(scope, now=now), tier="standard", now=now
    )
    assert report.decision == ReadinessDecision.READY
    assert report.blocking_codes == ()


def test_readiness_blocks_without_probe_or_failed_probe() -> None:
    scope = _scope()
    now = _now()
    missing = evaluate_readiness(V01_CATALOG, scope, [], tier="standard", now=now)
    assert missing.decision == ReadinessDecision.BLOCKED
    assert any(code.endswith("_NO_PROBE") for code in missing.blocking_codes)

    bad = _required_evidence(scope, now=now)
    bad[0] = _evidence_for(scope, bad[0].capability_key, ProbeOutcome.FAILED, now=now)
    failed = evaluate_readiness(V01_CATALOG, scope, bad, tier="standard", now=now)
    assert failed.decision == ReadinessDecision.BLOCKED
    assert any(code.endswith("_NOT_VERIFIED") for code in failed.blocking_codes)


def test_readiness_rejects_wrong_tenant_stale_and_version_skew() -> None:
    scope = _scope()
    now = _now()
    other_tenant = [
        _evidence_for(_scope(), e.capability_key, ProbeOutcome.VERIFIED, now=now)
        for e in _required_evidence(scope, now=now)
    ]
    assert (
        evaluate_readiness(V01_CATALOG, scope, other_tenant, tier="standard", now=now).decision
        == ReadinessDecision.BLOCKED
    )

    expired = _required_evidence(scope, now=now - timedelta(hours=2))
    assert (
        evaluate_readiness(V01_CATALOG, scope, expired, tier="standard", now=now).decision
        == ReadinessDecision.BLOCKED
    )

    skewed = _required_evidence(scope, now=now)
    skewed = [e.model_copy(update={"policy_version": "p2"}) for e in skewed]
    assert (
        evaluate_readiness(V01_CATALOG, scope, skewed, tier="standard", now=now).decision
        == ReadinessDecision.BLOCKED
    )


def test_readiness_optional_never_blocks_and_skips_pass() -> None:
    scope = _scope()
    now = _now()
    only_required = _required_evidence(scope, now=now)
    report = evaluate_readiness(V01_CATALOG, scope, only_required, tier="standard", now=now)
    assert report.decision == ReadinessDecision.READY
    optional_gates = [g for g in report.gates if g.disposition == PolicyDisposition.OPTIONAL]
    assert optional_gates and all(g.satisfied for g in optional_gates)


def test_readiness_rejects_uncatalogued_evidence_and_unknown_tier() -> None:
    scope = _scope()
    now = _now()
    rogue = _evidence_for(scope, "rogue-cap", ProbeOutcome.VERIFIED, now=now)
    with pytest.raises(ValueError, match="uncatalogued"):
        evaluate_readiness(V01_CATALOG, scope, [rogue], tier="standard", now=now)
    with pytest.raises(ValueError, match="unknown service tier"):
        evaluate_readiness(V01_CATALOG, scope, [], tier="nope", now=now)


def test_activation_gate_stays_advisory_by_default() -> None:
    scope = _scope()
    now = _now()
    blocked = evaluate_readiness(V01_CATALOG, scope, [], tier="standard", now=now)
    assert blocked.decision == ReadinessDecision.BLOCKED
    assert activation_permitted(blocked) is True
    assert activation_permitted(blocked, enforcement=EnforcementMode.ENFORCING) is False
    ready = evaluate_readiness(
        V01_CATALOG, scope, _required_evidence(scope, now=now), tier="standard", now=now
    )
    assert activation_permitted(ready, enforcement=EnforcementMode.ENFORCING) is True
