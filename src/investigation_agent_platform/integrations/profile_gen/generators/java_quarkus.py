"""Quarkus generator: JAX-RS resources + MicroProfile config (G2).

Full generator (not an adapter): @Path resources with JAX-RS verb
annotations, @ConfigProperty keys, @RegisterRestClient edges, and
application.properties with %profile handling. Same interface as
java_spring; synthetic-fixture matrices only.
"""

from __future__ import annotations

import re
from typing import Any

REPO_TYPE = "java-quarkus"

_PATH_CLASS_RE = re.compile(r'@Path\(\s*"([^"]*)"\s*\)')
_VERB_RES = (
    ("GET", re.compile(r"@GET\b")),
    ("POST", re.compile(r"@POST\b")),
    ("PUT", re.compile(r"@PUT\b")),
    ("DELETE", re.compile(r"@DELETE\b")),
    ("PATCH", re.compile(r"@PATCH\b")),
    ("HEAD", re.compile(r"@HEAD\b")),
    ("OPTIONS", re.compile(r"@OPTIONS\b")),
)
_METHOD_SIG_RE = re.compile(
    r"@(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b.{0,200}?"
    r"(?:@Path\(\s*\"([^\"]*)\"\s*\).{0,200}?)?"
    r"(?:public|protected|private)?\s*[\w<>\[\]]+\s+(\w+)\s*\(",
    re.DOTALL,
)
_CONFIG_PROPERTY_RE = re.compile(
    r"@ConfigProperty\(\s*(?:name\s*=\s*)?\"([^\"]*)\""
)
_REGISTER_CLIENT_RE = re.compile(
    r"@RegisterRestClient(?:\(\s*(?:configKey\s*=\s*)?\"([^\"]*)\"[^)]*\))?"
)
_CONFIG_INJECT_RE = re.compile(r"@Inject\s+Config\s+\w+")
_PROFILE_PREFIX_RE = re.compile(r"^%[^.]+\.(.*)$")


def controllers(text: str) -> dict[str, Any]:
    """JAX-RS resource inventory (pure)."""
    try:
        body = str(text or "")
        class_paths = _PATH_CLASS_RE.findall(body)
        base_path = (class_paths[0] if class_paths else "").rstrip("/")
        endpoints: list[dict[str, str]] = []
        for match in _METHOD_SIG_RE.finditer(body):
            try:
                segment = (match.group(1) or "").strip("/")
                # The verb opening this match wins; otherwise the nearest
                # verb annotation strictly before the signature.
                method = "GET"
                head = match.group(0)[:12]
                head_verb = next(
                    (verb for verb, _ in _VERB_RES if head.startswith("@" + verb)),
                    "",
                )
                if head_verb:
                    method = head_verb
                else:
                    best_pos = -1
                    for verb, pattern in _VERB_RES:
                        for verb_match in pattern.finditer(body, 0, match.start()):
                            if verb_match.start() > best_pos:
                                best_pos = verb_match.start()
                                method = verb
                full_path = (base_path + "/" + segment).rstrip("/") or "/"
                endpoints.append({"method": method, "path": full_path.lstrip("/")})
            except Exception:
                continue
        return {"base_path": base_path.lstrip("/"), "endpoints": endpoints}
    except Exception:
        return {"base_path": "", "endpoints": []}


def validations(text: str) -> list[dict[str, Any]]:
    """Bean-validation entries (shared semantics with Spring)."""
    try:
        from investigation_agent_platform.integrations.profile_gen.parsers import (
            parse_dto_validations,
        )

        return parse_dto_validations(text)
    except Exception:
        return []


def clients(text: str) -> dict[str, Any]:
    """MicroProfile RestClient edges (pure)."""
    try:
        out: dict[str, Any] = {"rest_clients": []}
        for match in _REGISTER_CLIENT_RE.finditer(str(text or "")):
            try:
                out["rest_clients"].append({"name": (match.group(1) or "").strip()})
            except Exception:
                continue
        return out
    except Exception:
        return {"rest_clients": []}


def config_keys(text: str, filename: str = "") -> list[str]:
    """@ConfigProperty keys + properties-file keys (pure)."""
    try:
        keys: list[str] = []
        for match in _CONFIG_PROPERTY_RE.finditer(str(text or "")):
            try:
                if match.group(1):
                    keys.append(match.group(1))
            except Exception:
                continue
        if filename.endswith(".properties"):
            from investigation_agent_platform.integrations.profile_gen.parsers import (
                parse_properties_file,
            )

            for key in parse_properties_file(text):
                stripped = _PROFILE_PREFIX_RE.sub(r"\1", key)
                if stripped not in keys:
                    keys.append(stripped)
        return sorted(set(keys))
    except Exception:
        return []
