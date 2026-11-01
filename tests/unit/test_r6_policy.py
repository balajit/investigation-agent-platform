"""R6: one capability policy module (pure; no I/O, no clock)."""

from __future__ import annotations

from investigation_agent_platform.domain.investigation.models import ActionType
from investigation_agent_platform.domain.profile.capability_policy import (
    ACTION_POLICY,
    INTERNAL_ACTIONS,
    DenyReason,
    PlatformOperation,
    assert_table_complete,
    capability_available,
    capability_available_by_name,
    effective_capabilities,
    newest_active_revision,
    unavailable_actions,
)
from investigation_agent_platform.domain.profile.models import (
    ApplicationProfile,
    InvestigationConfig,
    ProfileStatus,
    Service,
    ServiceCode,
)


def _profile(
    evidence_types,
    capabilities,
    *,
    status=ProfileStatus.ACTIVE,
    version=2,
    repos=("org/a",),
) -> ApplicationProfile:
    return ApplicationProfile(
        id="app1",
        tenant_id="t1",
        name="n",
        description="d",
        environment="production",
        version=version,
        status=status,
        investigation=InvestigationConfig(enabledEvidenceTypes=list(evidence_types)),
        allowed_capabilities=list(capabilities),
        services=[
            Service(
                name="a",
                code=ServiceCode(
                    org="org",
                    repos=[r.split("/", 1)[1] if "/" in r else r for r in repos],
                    source_roots=["src"],
                ),
            )
        ],
    )


def test_r6_t01_table_is_complete():
    assert assert_table_complete() == []
    assert all(a in ACTION_POLICY for a in ActionType if a not in INTERNAL_ACTIONS)


def test_r6_t01_negative_control_missing_row_is_reported(monkeypatch):
    import investigation_agent_platform.domain.profile.capability_policy as policy

    monkeypatch.delitem(policy.ACTION_POLICY, ActionType.SEARCH_LOGS)
    assert assert_table_complete() == ["SEARCH_LOGS"]


def test_r6_t03_every_row_both_gates():
    for action, rule in ACTION_POLICY.items():
        full = _profile([rule.evidence_type], [rule.capability])
        assert capability_available(full, action).allowed, action
        no_cap = _profile([rule.evidence_type], [])
        decision = capability_available(no_cap, action)
        assert not decision.allowed
        assert decision.reason == DenyReason.CAPABILITY_NOT_DECLARED, action
        no_evidence = _profile([], [rule.capability])
        decision = capability_available(no_evidence, action)
        assert not decision.allowed
        assert decision.reason == DenyReason.EVIDENCE_TYPE_NOT_ENABLED, action


def test_r6_t09_model_cannot_propose_platform_operations():
    for name in ("QUERY_CODE_TOPOLOGY", "RESOLVE_DOMAIN_ATTRIBUTION", "query_code_topology"):
        decision = capability_available_by_name(_profile(["SOURCE_CODE"], []), name)
        assert not decision.allowed
        assert decision.reason == DenyReason.UNKNOWN_ACTION
    try:
        ActionType("QUERY_CODE_TOPOLOGY")
        raise AssertionError("platform op must not be an ActionType")
    except ValueError:
        pass
    assert PlatformOperation.CODE_TOPOLOGY.value == "QUERY_CODE_TOPOLOGY"


def test_r6_t13_malformed_inputs():
    profile = _profile(["LOG"], ["SEARCH_LOGS"])
    assert capability_available(profile, "").reason == DenyReason.UNKNOWN_ACTION
    assert capability_available(profile, None).reason == DenyReason.UNKNOWN_ACTION  # type: ignore[arg-type]
    assert capability_available(profile, "search_logs").reason == DenyReason.UNKNOWN_ACTION
    none_caps = ApplicationProfile.model_construct(
        allowed_capabilities=None, investigation={"enabledEvidenceTypes": ["LOG"]}
    )
    assert not capability_available(none_caps, ActionType.SEARCH_LOGS).allowed
    bad_types = ApplicationProfile.model_construct(
        allowed_capabilities=["SEARCH_LOGS"], investigation={"enabledEvidenceTypes": "LOG"}
    )
    assert not capability_available(bad_types, ActionType.SEARCH_LOGS).allowed
    assert capability_available(None, ActionType.SEARCH_LOGS).reason == (
        DenyReason.NO_ACTIVE_PROFILE
    )
    assert unavailable_actions(None) == tuple(sorted(a.value for a in ACTION_POLICY))
    assert effective_capabilities(None) == ()


def test_r6_newest_active_revision_skips_draft():
    from types import SimpleNamespace

    revisions = [
        SimpleNamespace(status=ProfileStatus.DRAFT),
        SimpleNamespace(status=ProfileStatus.ACTIVE),
    ]
    assert newest_active_revision(revisions).status == ProfileStatus.ACTIVE
    assert newest_active_revision([]) is None
