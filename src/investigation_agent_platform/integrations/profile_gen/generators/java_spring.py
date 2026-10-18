"""Spring generator: re-exports the established parsers (G1).

The Spring MVC / bean-validation / Feign / properties extraction lives
in profile_gen.parsers (unit-covered); this module binds it behind the
generator interface so the assembler never special-cases frameworks.
"""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.integrations.profile_gen.parsers import (
    parse_dto_validations,
    parse_feign_client,
    parse_java_controller,
    parse_properties_file,
    parse_rest_calls,
    parse_spring_yaml,
    parse_value_keys,
)

REPO_TYPE = "java-spring"


def controllers(text: str) -> dict[str, Any]:
    """Controller inventory (base path + endpoints)."""
    try:
        return parse_java_controller(text)
    except Exception:
        return {"base_path": "", "endpoints": []}


def validations(text: str) -> list[dict[str, Any]]:
    """DTO validation entries."""
    try:
        return parse_dto_validations(text)
    except Exception:
        return []


def clients(text: str) -> dict[str, Any]:
    """Inter-service client edges (Feign + RestTemplate)."""
    try:
        out: dict[str, Any] = {"feign": [], "rest": []}
        feign = parse_feign_client(text)
        if feign:
            out["feign"].append(feign)
        out["rest"].extend(parse_rest_calls(text))
        return out
    except Exception:
        return {"feign": [], "rest": []}


def config_keys(text: str, filename: str = "") -> list[str]:
    """Injected/config property keys."""
    try:
        keys = list(parse_value_keys(text))
        if filename.endswith((".yml", ".yaml")):
            flat = parse_spring_yaml(text)
            keys.extend(str(k) for k in flat)
        elif filename.endswith(".properties"):
            flat = parse_properties_file(text)
            keys.extend(str(k) for k in flat)
        return sorted(set(keys))
    except Exception:
        return []
