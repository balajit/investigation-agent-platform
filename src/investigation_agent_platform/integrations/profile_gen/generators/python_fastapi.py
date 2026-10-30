"""FastAPI generator: routers + Pydantic + HTTPX (G3).

Full generator: route decorators with prefixes, Pydantic Field
constraints, httpx/requests call sites, and settings/env keys.
Same interface as the Java generators.
"""

from __future__ import annotations

import re
from typing import Any

REPO_TYPE = "python-fastapi"

_ROUTE_RES = (
    ("GET", re.compile(r"@(?:\w+\.)?get\(\s*\"([^\"]*)\"")),
    ("POST", re.compile(r"@(?:\w+\.)?post\(\s*\"([^\"]*)\"")),
    ("PUT", re.compile(r"@(?:\w+\.)?put\(\s*\"([^\"]*)\"")),
    ("DELETE", re.compile(r"@(?:\w+\.)?delete\(\s*\"([^\"]*)\"")),
    ("PATCH", re.compile(r"@(?:\w+\.)?patch\(\s*\"([^\"]*)\"")),
)
_ROUTER_PREFIX_RE = re.compile(r"APIRouter\(\s*prefix\s*=\s*\"([^\"]*)\"")
_FIELD_RE = re.compile(r"^\s*(\w+)\s*:\s*[^=\n]+?(?:=\s*Field\(([^)]*)\))?\s*$")
_FIELD_KW_RE = re.compile(r"(gt|ge|lt|le|min_length|max_length|pattern)\s*=\s*([^,\)]+)")
_PYDANTIC_CONSTRAINTS = {"gt", "ge", "lt", "le", "min_length", "max_length", "pattern"}
_HTTPX_RE = re.compile(r"(?:httpx|requests)\.(get|post|put|delete|patch)\(\s*[\"']([^\"']+)[\"']")
_ENV_RE = re.compile(
    r"os\.getenv\(\s*[\"']([^\"']+)[\"']|os\.environ(?:\[|\.get\()\s*[\"']([^\"']+)[\"']"
)
_SETTINGS_RE = re.compile(r"(\w+)\s*:\s*[^=\n]*=\s*Field\([^)]*env\s*=\s*[\"']([^\"']+)[\"']")


def controllers(text: str) -> dict[str, Any]:
    """FastAPI route inventory with router prefixes (pure)."""
    try:
        body = str(text or "")
        prefix_match = _ROUTER_PREFIX_RE.search(body)
        base_path = (prefix_match.group(1) if prefix_match else "").rstrip("/")
        endpoints: list[dict[str, str]] = []
        for method, pattern in _ROUTE_RES:
            for match in pattern.finditer(body):
                try:
                    segment = (match.group(1) or "").strip("/")
                    full_path = (base_path + "/" + segment).strip("/") or "/"
                    endpoints.append({"method": method, "path": full_path})
                except Exception:
                    continue
        return {"base_path": base_path.strip("/"), "endpoints": endpoints}
    except Exception:
        return {"base_path": "", "endpoints": []}


def validations(text: str) -> list[dict[str, Any]]:
    """Pydantic field constraints (pure)."""
    try:
        out: list[dict[str, Any]] = []
        for line in str(text or "").splitlines():
            try:
                match = _FIELD_RE.match(line)
                if not match:
                    continue
                field_name, field_args = match.group(1), match.group(2) or ""
                constraints: list[str] = []
                for kw_match in _FIELD_KW_RE.finditer(field_args):
                    if kw_match.group(1) in _PYDANTIC_CONSTRAINTS:
                        constraints.append(kw_match.group(1))
                if constraints:
                    out.append({"field": field_name, "constraints": constraints})
            except Exception:
                continue
        return out
    except Exception:
        return []


def clients(text: str) -> dict[str, Any]:
    """httpx/requests call sites (pure)."""
    try:
        out: list[dict[str, str]] = []
        for match in _HTTPX_RE.finditer(str(text or "")):
            out.append({"method": match.group(1), "url": match.group(2)[:512]})
        return {"http": out}
    except Exception:
        return {"http": []}


def config_keys(text: str, filename: str = "") -> list[str]:
    """Env/settings keys (pure)."""
    try:
        keys: list[str] = []
        for match in _ENV_RE.finditer(str(text or "")):
            try:
                name = match.group(1) or match.group(2)
                if name and name not in keys:
                    keys.append(name)
            except Exception:
                continue
        for match in _SETTINGS_RE.finditer(str(text or "")):
            try:
                if match.group(2) and match.group(2) not in keys:
                    keys.append(match.group(2))
            except Exception:
                continue
        return sorted(keys)
    except Exception:
        return []
