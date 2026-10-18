# tests/unit/test_profile_samples.py
"""Shipped application-profile samples must load, validate, and resolve.

Guards the ``config/profiles/*.yaml`` documents against schema drift: every
file is parsed through the production loader, must validate, must carry a
unique ``(tenant_id, id)``, must declare at least one service, and must name
an observability mapping that the runtime can actually resolve (built-in
``generic-ecs`` or a shipped ``config/observability-mappings/*.yaml``
``source_id``).
"""

from pathlib import Path

from investigation_agent_platform.domain.observability.mapping import GENERIC_ECS_SOURCE_ID
from investigation_agent_platform.infrastructure.configuration.config import (
    load_application_profile_from_file,
)
from investigation_agent_platform.infrastructure.configuration.mapping_registry import (
    load_mapping_profiles,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILES_DIR = REPO_ROOT / "config" / "profiles"
MAPPINGS_DIR = REPO_ROOT / "config" / "observability-mappings"


def _profile_paths() -> list[Path]:
    return sorted(PROFILES_DIR.glob("*.yaml"))


def test_profile_samples_exist() -> None:
    assert _profile_paths(), f"No profile samples found in {PROFILES_DIR}"


def test_shipped_profiles_load_and_validate() -> None:
    for path in _profile_paths():
        profile = load_application_profile_from_file(path)
        # Required identity/content is structurally ensured by the
        # loader; assert the values that other components key off.
        assert profile.id, f"{path.name}: empty id"
        assert profile.tenant_id, f"{path.name}: empty tenant_id"
        assert profile.environment, f"{path.name}: empty environment"
        assert profile.services, f"{path.name}: no services"
        assert profile.investigation.enabled_evidence_types, (
            f"{path.name}: no enabledEvidenceTypes"
        )


def test_shipped_profiles_have_unique_identity() -> None:
    seen: set[tuple[str, str]] = set()
    for path in _profile_paths():
        profile = load_application_profile_from_file(path)
        key = (profile.tenant_id, profile.id)
        assert key not in seen, f"{path.name}: duplicate (tenant_id, id) {key}"
        seen.add(key)


def test_shipped_profiles_reference_resolvable_mapping() -> None:
    registry = load_mapping_profiles(MAPPINGS_DIR)
    for path in _profile_paths():
        profile = load_application_profile_from_file(path)
        source_id = profile.mapping_source_id
        assert source_id == GENERIC_ECS_SOURCE_ID or source_id in registry.source_ids, (
            f"{path.name}: mapping_source_id {source_id!r} does not resolve"
        )


def test_shipped_profile_reads_are_sanitized() -> None:
    for path in _profile_paths():
        profile = load_application_profile_from_file(path)
        sanitized = profile.sanitized()
        for ds in sanitized.get("datasources", []):
            for tpl in ds.get("query_templates", {}).values():
                assert "sql" not in tpl, f"{path.name}: template SQL leaked into read shape"
            ref = ds.get("connection_ref")
            if ref is not None:
                assert set(ref) == {"present", "scheme"}, f"{path.name}: raw connection_ref leaked"
