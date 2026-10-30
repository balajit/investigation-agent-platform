"""Repo parsers for profile generation (P2).

Generic framework-level extraction (never service-specific): Spring MVC
controllers, bean-validation annotations, Feign clients, RestTemplate call
sites, and Spring properties/YAML files. Regex-based heuristics over file
text — bounded, documented, fail-open per file. Unit matrices use
synthetic fixtures only.
"""

from __future__ import annotations

import re
from typing import Any

_MAPPING_RE = re.compile(
    r"@(?:RequestMapping|GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping)"
    r'\s*(?:\(\s*(?:"([^"]*)")?[^)]*\))?'
)
_METHOD_FROM_ANNOTATION = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}
_METHOD_ATTR_RE = re.compile(r"method\s*=\s*RequestMethod\.(\w+)")
_PATH_ATTR_RE = re.compile(r'(?:value|path)\s*=\s*"([^"]*)"')
_CLASS_REQUEST_MAPPING_RE = re.compile(r"@RequestMapping\(\s*\"([^\"]*)\"")
_FIELD_RE = re.compile(r"(?:private|protected|public)\s+([\w<>\[\].,\s]+?)\s+(\w+)\s*[;=]")
_ANNOTATION_RE = re.compile(r"@(\w+)(?:\(([^)]*)\))?")
_CONSTRAINT_ANNOTATIONS = {
    "NotNull",
    "NotBlank",
    "NotEmpty",
    "Size",
    "Min",
    "Max",
    "Pattern",
    "Valid",
    "Positive",
    "PositiveOrZero",
    "Required",
}
_FEIGN_RE = re.compile(
    r"@FeignClient\(\s*(?:name|value)\s*=\s*\"([^\"]*)\""
    r"(?:[^)]*url\s*=\s*\"([^\"]*)\")?"
)
_REST_CALL_RE = re.compile(
    r"\.(getForObject|getForEntity|postForObject|postForEntity|exchange|put|delete)"
    r"\s*\(\s*\"([^\"]*)\""
)
_VALUE_RE = re.compile(r"@Value\(\s*\"\$\{([^}:]+)(?::([^}]*))?\}\"")
_PROP_LINE_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*[:=]\s*(.*?)\s*$")


def parse_java_controller(text: str) -> dict[str, Any]:
    """Extract base path + endpoint inventory from a controller file (pure).

    Returns {base_path, endpoints: [{method, path}]}. Method defaults
    from the annotation kind, overridden by method= attributes. Paths
    join class-level base with method-level segments.
    """
    try:
        body = str(text or "")
        base_match = _CLASS_REQUEST_MAPPING_RE.search(body)
        base_path = (base_match.group(1) if base_match else "").rstrip("/")
        endpoints: list[dict[str, str]] = []
        for match in _MAPPING_RE.finditer(body):
            try:
                full = match.group(0)
                kind = re.search(r"@(\w+Mapping)", full)
                kind_name = kind.group(1) if kind else ""
                # Class-level @RequestMapping declares the base path (no
                # method attribute, followed by a class declaration) —
                # skip it here; base_path above already captured it.
                if kind_name == "RequestMapping" and "method" not in full.lower():
                    following = body[match.end() : match.end() + 120]
                    if re.search(r"\b(class|interface)\b", following):
                        continue
                method = _METHOD_FROM_ANNOTATION.get(kind_name, "")
                method_attr = _METHOD_ATTR_RE.search(full)
                if method_attr:
                    method = method_attr.group(1).upper()
                if not method:
                    if kind_name == "RequestMapping":
                        continue
                    method = "GET"
                path_match = _PATH_ATTR_RE.search(full)
                segment = path_match.group(1) if path_match else match.group(1) or ""
                full_path = (base_path + "/" + str(segment).lstrip("/")).rstrip("/") or "/"
                endpoints.append({"method": method, "path": full_path})
            except Exception:
                continue
        return {"base_path": base_path, "endpoints": endpoints}
    except Exception:
        return {"base_path": "", "endpoints": []}


def parse_dto_validations(text: str) -> list[dict[str, Any]]:
    """Extract validated fields with constraints (pure).

    Returns [{field, constraints: [annotation names]}] for fields
    carrying bean-validation (or Required-style) annotations.
    """
    try:
        body = str(text or "")
        lines = body.splitlines()
        out: list[dict[str, Any]] = []
        pending: list[str] = []
        for line in lines:
            try:
                stripped = line.strip()
                for annotation in _ANNOTATION_RE.finditer(stripped):
                    name = annotation.group(1)
                    if name in _CONSTRAINT_ANNOTATIONS:
                        pending.append(name)
                field_match = _FIELD_RE.match(stripped)
                if field_match and pending:
                    out.append({"field": field_match.group(2), "constraints": list(pending)})
                    pending = []
                elif field_match or (
                    stripped and not stripped.startswith("@") and not stripped.startswith("//")
                ):
                    pending = []
            except Exception:
                continue
        return out
    except Exception:
        return []


def parse_feign_client(text: str) -> dict[str, Any] | None:
    """Extract a Feign client declaration (pure). None when absent."""
    try:
        match = _FEIGN_RE.search(str(text or ""))
        if not match:
            return None
        return {"name": match.group(1), "url": match.group(2) or ""}
    except Exception:
        return None


def parse_rest_calls(text: str) -> list[dict[str, str]]:
    """Extract RestTemplate-style call sites with URL templates (pure)."""
    try:
        out = []
        for match in _REST_CALL_RE.finditer(str(text or "")):
            out.append({"method": match.group(1), "url": match.group(2)[:512]})
        return out
    except Exception:
        return []


def parse_value_keys(text: str) -> list[str]:
    """Extract @Value property keys (pure)."""
    try:
        return sorted(
            {
                _VALUE_RE.search(m.group(0)) and m.group(1)
                for m in _VALUE_RE.finditer(str(text or ""))
                if m.group(1)
            }
        )
    except Exception:
        return []


def parse_properties_file(text: str) -> dict[str, str]:
    """Parse .properties content to a flat dict (pure).

    Skips comments/blank lines; last occurrence wins (Spring semantics).
    """
    try:
        out: dict[str, str] = {}
        for line in str(text or "").splitlines():
            try:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or stripped.startswith("!"):
                    continue
                match = _PROP_LINE_RE.match(stripped)
                if match:
                    out[match.group(1)] = match.group(2)
            except Exception:
                continue
        return out
    except Exception:
        return {}


def parse_spring_yaml(text: str) -> dict[str, Any]:
    """Parse application YAML to a flat dotted dict (pure).

    Uses pyyaml when available; flattens nested maps with dot separators.
    Lists indexed numerically (server.ports[0]). Unparseable → {}.
    """
    try:
        import yaml as _yaml  # type: ignore[import-not-found]

        doc = _yaml.safe_load(str(text or ""))
        flat: dict[str, Any] = {}

        def _flatten(node: Any, prefix: str) -> None:
            try:
                if isinstance(node, dict):
                    for key, value in node.items():
                        _flatten(value, f"{prefix}.{key}" if prefix else str(key))
                elif isinstance(node, list):
                    for index, value in enumerate(node):
                        _flatten(value, f"{prefix}[{index}]")
                elif node is not None:
                    flat[prefix] = node
            except Exception:
                pass

        _flatten(doc, "")
        return flat
    except Exception:
        return {}
