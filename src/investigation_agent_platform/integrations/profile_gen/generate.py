"""Profile generation orchestration (P4).

Clone → list → parse → merge config → assemble. Thin I/O shell over
the pure github/parsers/precedence/assembler modules. Clone is shallow
(depth 1) into a temp dir; file walk capped; failures raise with
context (callers decide degrade vs loud).
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MAX_FILES = 5000
_SKIP_DIRS = frozenset(
    {".git", "target", "build", "node_modules", "dist", "out", ".gradle", ".idea"}
)
_JAVA_EXTS = frozenset({".java"})
_SPRING_BASENAMES = frozenset(
    {
        "application.yml",
        "application.yaml",
        "application.properties",
        "bootstrap.yml",
        "bootstrap.yaml",
        "bootstrap.properties",
    }
)


def clone_repo(clone_url: str, branch: str = "") -> Path:
    """Shallow-clone into a fresh temp dir; returns the checkout path.

    Private repos authenticate with the GitHub token (IAP_GITHUB_TOKEN env
    first, `gh` CLI keychain fallback — same precedence as API discovery).
    The token travels inside pygit2's credential callback only: it is never
    embedded in the URL, logged, or persisted.
    """
    try:
        import pygit2
    except Exception as exc:
        raise RuntimeError("pygit2 unavailable for profile generation.") from exc
    try:
        import os as _os

        from investigation_agent_platform.integrations.profile_gen.github import (
            _gh_cli_token,
        )

        token = _os.environ.get("IAP_GITHUB_TOKEN", "").strip() or _gh_cli_token()
        target = Path(tempfile.mkdtemp(prefix="iap-profgen-"))
        kwargs: dict[str, Any] = {"depth": 1}
        if branch:
            kwargs["checkout_branch"] = branch
        if token:
            kwargs["callbacks"] = pygit2.RemoteCallbacks(
                credentials=pygit2.UserPass("git", token)
            )
        try:
            pygit2.clone_repository(clone_url, str(target), **kwargs)
        except Exception as branch_exc:
            # Requested branch missing upstream (wrong default guess):
            # retry on the remote's default HEAD instead of failing.
            if branch and "not found" in str(branch_exc).lower():
                kwargs.pop("checkout_branch", None)
                pygit2.clone_repository(clone_url, str(target), **kwargs)
            else:
                raise
        return target
    except Exception as exc:
        text = str(exc)
        if "authentication required" in text.lower() or "auth" in text.lower():
            raise RuntimeError(
                "Clone failed: private repo needs GitHub auth — set "
                "IAP_GITHUB_TOKEN or run `gh auth login`."
            ) from exc
        raise RuntimeError(f"Clone failed: {text[:300]}") from exc


def list_files(root: Path) -> list[str]:
    """Bounded repo-relative file listing (pure-ish I/O)."""
    try:
        out: list[str] = []
        for path in sorted(root.rglob("*")):
            try:
                if len(out) >= _MAX_FILES:
                    break
                if not path.is_file():
                    continue
                rel = path.relative_to(root)
                if any(part in _SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
                    continue
                out.append(rel.as_posix())
            except Exception:
                continue
        return out
    except Exception:
        return []


def parse_checkout(
    root: Path, files: list[str], generator: Any | None = None
) -> dict[str, Any]:
    """Parse controllers/DTOs/clients/configs from a checkout (I/O + pure).

    Returns {controllers, validations, feign_clients, rest_calls,
    value_keys, spring_files} with per-module provenance. Bounded and
    fail-open per file. The generator module selects per-type parsers
    (java_spring default preserves legacy behavior); source extensions
    follow the generator family (java: .java, python: .py).
    """
    from investigation_agent_platform.integrations.profile_gen.parsers import (
        parse_dto_validations,
        parse_feign_client,
        parse_java_controller,
        parse_properties_file,
        parse_rest_calls,
        parse_spring_yaml,
        parse_value_keys,
    )

    try:
        family = str(getattr(generator, "REPO_TYPE", "") or "")
    except Exception:
        family = ""
    if family.startswith("python"):
        source_exts = frozenset({".py"})
    else:
        source_exts = _JAVA_EXTS

    def _call(name: str, *args: Any) -> Any:
        try:
            if generator is not None and hasattr(generator, name):
                return getattr(generator, name)(*args)
        except Exception:
            pass
        return None

    controllers: list[dict[str, Any]] = []
    validations: list[dict[str, Any]] = []
    feign_clients: list[dict[str, Any]] = []
    rest_calls: list[dict[str, Any]] = []
    value_keys: list[str] = []
    spring_files: dict[str, Any] = {}
    try:
        for rel in files:
            try:
                path = root / rel
                if path.suffix not in source_exts and path.name not in _SPRING_BASENAMES:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                if len(text) > 1_000_000:
                    continue
                if path.suffix in source_exts:
                    generator_used = generator is not None
                    if generator_used:
                        controller = _call("controllers", text) or {"endpoints": []}
                    else:
                        controller = parse_java_controller(text)
                    if controller.get("endpoints"):
                        controllers.append({**controller, "file": rel})
                    if generator_used:
                        validation_entries = _call("validations", text) or []
                    else:
                        validation_entries = parse_dto_validations(text)
                    for entry in validation_entries:
                        validations.append({**entry, "file": rel} if isinstance(entry, dict) else entry)
                    client_info = _call("clients", text) or {}
                    for feign_entry in client_info.get("feign", []) or []:
                        if isinstance(feign_entry, dict):
                            feign_clients.append({**feign_entry, "file": rel})
                    for feign_entry in client_info.get("rest_clients", []) or []:
                        if isinstance(feign_entry, dict) and feign_entry.get("name"):
                            feign_clients.append(
                                {"name": str(feign_entry["name"]), "url": "", "file": rel}
                            )
                    for call in client_info.get("rest", []) or []:
                        if isinstance(call, dict):
                            rest_calls.append({**call, "file": rel})
                    for call in client_info.get("http", []) or []:
                        if isinstance(call, dict):
                            rest_calls.append({**call, "file": rel})
                    feign = parse_feign_client(text)
                    if feign and not generator_used:
                        feign_clients.append({**feign, "file": rel})
                    if not generator_used:
                        for call in parse_rest_calls(text):
                            rest_calls.append({**call, "file": rel})
                    if generator_used:
                        config_candidates = _call("config_keys", text, path.name) or []
                    else:
                        config_candidates = parse_value_keys(text)
                    for key in config_candidates:
                        if key not in value_keys:
                            value_keys.append(key)
                else:
                    if path.suffix in (".yml", ".yaml"):
                        parsed = parse_spring_yaml(text)
                    else:
                        parsed = parse_properties_file(text)
                    for key, value in parsed.items():
                        spring_files.setdefault(key, value)
            except Exception:
                continue
    except Exception:
        pass
    return {
        "controllers": controllers,
        "validations": validations,
        "feign_clients": feign_clients,
        "rest_calls": rest_calls,
        "value_keys": value_keys,
        "spring_files": spring_files,
    }


_EXT_LANGUAGE = {
    ".py": "python", ".java": "java", ".js": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".go": "go", ".rs": "rust", ".rb": "ruby",
    ".c": "c", ".cpp": "cpp", ".cs": "csharp",
}


def dominant_language(files: list[str]) -> str | None:
    """Most common source extension mapped to language (pure)."""
    try:
        from collections import Counter

        counts: Counter[str] = Counter()
        for raw in files or []:
            try:
                suffix = "." + str(raw or "").rsplit(".", 1)[-1].lower() if "." in str(raw or "") else ""
                language = _EXT_LANGUAGE.get(suffix, "")
                if language:
                    counts[language] += 1
            except Exception:
                continue
        if not counts:
            return None
        return counts.most_common(1)[0][0]
    except Exception:
        return None


def dto_attributes_for_endpoints(
    validations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flatten DTO validations to expectation attributes (pure)."""
    try:
        merged: dict[str, dict[str, Any]] = {}
        for entry in validations or []:
            try:
                if not isinstance(entry, dict):
                    continue
                for attr in entry if isinstance(entry, list) else [entry]:
                    if not isinstance(attr, dict) or not attr.get("field"):
                        # entry shape is {field, constraints} already
                        pass
                    name = str(attr.get("field", "") or "")
                    if not name:
                        continue
                    constraints = list(attr.get("constraints", []) or [])
                    current = merged.get(name, {"name": name})
                    if "NotNull" in constraints or "Required" in constraints:
                        current["required"] = True
                    merged[name] = current
            except Exception:
                continue
        return list(merged.values())
    except Exception:
        return []
