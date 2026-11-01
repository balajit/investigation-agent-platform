#!/usr/bin/env python3
"""Run structural, configuration, schema, and agent-discovery checks."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
AGENTS = {
    "professor": "primary",
    "prompt-gen": "subagent",
    "research-assistant": "subagent",
    "code-analyst": "subagent",
    "architecture-agent": "subagent",
    "adversarial-reviewer": "subagent",
    "validation-agent": "subagent",
}
REQUIRED_HEADINGS = {
    ".opencode/workflows/research.md": ["Runtime Capability Gate", "Acceptance Criteria"],
    ".opencode/workflows/research-and-design.md": ["Requirements-to-Design Traceability", "Acceptance Criteria"],
    ".opencode/workflows/code-review.md": ["Finding Contract and Severity", "Independent Adversarial Review", "Independent Validation"],
    ".opencode/prompts/specification-analysis.md": ["Requirements Traceability", "Gaps, Ambiguities, and Conflicts"],
}
ALLOWED_ACTIONS = {"allow", "ask", "deny"}
ALLOWED_FRONTMATTER_KEYS = {"description", "mode", "temperature", "permission"}
PROFESSOR_TASK_TARGETS = {
    "*": "deny",
    "prompt-gen": "allow",
    "research-assistant": "allow",
    "code-analyst": "allow",
    "architecture-agent": "allow",
    "adversarial-reviewer": "allow",
    "validation-agent": "allow",
}
ALLOWED_PERMISSION_KEYS = {
    "read", "list", "glob", "grep", "task", "edit", "bash", "external_directory",
    "todowrite", "websearch", "webfetch", "*", *PROFESSOR_TASK_TARGETS,
}
EXPECTED_PERMISSIONS = {
    "professor": {"mode": "primary", "task": "delegated"},
    "prompt-gen": {"mode": "subagent", "task": "deny", "edit": "deny", "bash": "deny", "external_directory": "deny"},
    "research-assistant": {"mode": "subagent", "task": "deny", "edit": "deny", "bash": "deny", "external_directory": "deny", "websearch": "allow", "webfetch": "allow"},
    "code-analyst": {"mode": "subagent", "task": "deny", "edit": "deny", "bash": "deny", "external_directory": "deny"},
    "architecture-agent": {"mode": "subagent", "task": "deny", "edit": "deny", "bash": "deny", "external_directory": "deny"},
    "adversarial-reviewer": {"mode": "subagent", "task": "deny", "edit": "deny", "bash": "deny", "external_directory": "deny"},
    "validation-agent": {"mode": "subagent", "task": "deny", "edit": "deny", "bash": "deny", "external_directory": "deny"},
}


def check(condition: bool, message: str, failures: list[str]) -> None:
    print(("PASS" if condition else "FAIL") + ": " + message)
    if not condition:
        failures.append(message)


def parse_frontmatter(content: str) -> dict[str, Any]:
    """Parse the restricted YAML mapping used by v1 OpenCode agent files.

    Agent frontmatter is intentionally limited to scalar values and nested
    mappings for permissions. Rejecting unsupported YAML constructs is safer
    than treating them as valid configuration that OpenCode may ignore.
    """
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("frontmatter must begin with --- at byte zero")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise ValueError("frontmatter closing delimiter is missing") from error

    result: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, result)]
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if "\t" in raw[:indent] or ":" not in raw:
            raise ValueError(f"invalid frontmatter mapping line: {raw!r}")
        key, value = raw.strip().split(":", 1)
        key = key.strip().strip('"\'')
        if not key:
            raise ValueError("frontmatter contains an empty key")
        while stack[-1][0] >= indent:
            stack.pop()
        if not stack:
            raise ValueError("invalid frontmatter indentation")
        parent = stack[-1][1]
        if key in parent:
            raise ValueError(f"duplicate frontmatter key: {key}")
        value = value.strip()
        if not value:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = value.strip('"\'')
    return result


def validate_permissions(name: str, frontmatter: dict[str, Any], failures: list[str]) -> None:
    permissions = frontmatter.get("permission")
    unknown_frontmatter = set(frontmatter) - ALLOWED_FRONTMATTER_KEYS
    check(not unknown_frontmatter, f"supported frontmatter keys: {name}", failures)
    check(isinstance(permissions, dict), f"permission mapping is valid: {name}", failures)
    if not isinstance(permissions, dict):
        return

    def walk(mapping: dict[str, Any]) -> None:
        for key, value in mapping.items():
            check(key in ALLOWED_PERMISSION_KEYS, f"supported permission key for {name}: {key}", failures)
            if isinstance(value, dict):
                walk(value)
            else:
                check(value in ALLOWED_ACTIONS, f"valid permission action for {name}: {key}", failures)

    walk(permissions)
    expected = EXPECTED_PERMISSIONS[name]
    if name == "professor":
        check(permissions.get("task") == PROFESSOR_TASK_TARGETS, f"exact Professor delegation allowlist: {name}", failures)
    for key, value in expected.items():
        if key == "mode" or value == "delegated":
            continue
        observed = permissions.get(key)
        if key == "task" and isinstance(observed, dict):
            check(all(action == "deny" for action in observed.values()), f"no recursive task delegation: {name}", failures)
        elif name != "professor":
            check(observed == value, f"least-privilege permission {key}={value}: {name}", failures)
    if name != "research-assistant":
        check("websearch" not in permissions and "webfetch" not in permissions, f"research tools scoped to research-assistant: {name}", failures)


def main() -> int:
    failures: list[str] = []
    required_files = [
        "AGENTS.md", "README.md", "opencode.json",
        *[f".opencode/agents/{name}.md" for name in AGENTS],
        ".opencode/workflows/research.md", ".opencode/workflows/research-and-design.md", ".opencode/workflows/code-review.md",
        ".opencode/prompts/specification-analysis.md", ".opencode/prompts/architecture-decision.md",
        ".opencode/prompts/repository-review.md", ".opencode/prompts/research-task.md",
        ".opencode/schemas/task-package.schema.json", ".opencode/schemas/agent-result.schema.json",
    ]
    for relative in required_files:
        path = ROOT / relative
        check(path.is_file() and path.stat().st_size > 0, f"required non-empty file: {relative}", failures)

    for directory in ("workspace/research", "workspace/specifications", "workspace/evidence", "workspace/decisions", "workspace/deliverables"):
        check((ROOT / directory).is_dir(), f"workspace directory: {directory}", failures)

    try:
        config = json.loads((ROOT / "opencode.json").read_text(encoding="utf-8"))
        check(True, "opencode.json parses as JSON", failures)
    except (OSError, json.JSONDecodeError) as error:
        config = {}
        check(False, f"opencode.json parses as JSON ({error})", failures)
    check(config.get("default_agent") == "professor", "default agent is professor", failures)
    check(config.get("subagent_depth") == 1, "subagent_depth remains 1", failures)

    parsed_agents: dict[str, str] = {}
    for name in AGENTS:
        path = ROOT / ".opencode" / "agents" / f"{name}.md"
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        try:
            frontmatter = parse_frontmatter(content)
            check(True, f"frontmatter parses as restricted YAML: {name}", failures)
            check(frontmatter.get("mode") in ("primary", "subagent"), f"one supported mode: {name}", failures)
            check(isinstance(frontmatter.get("description"), str) and bool(frontmatter["description"].strip()), f"one non-empty description: {name}", failures)
            check(frontmatter.get("mode") == EXPECTED_PERMISSIONS[name]["mode"], f"expected mode: {name}", failures)
            if frontmatter.get("mode") in ("primary", "subagent"):
                parsed_agents[name] = frontmatter["mode"]
            validate_permissions(name, frontmatter, failures)
        except ValueError as error:
            check(False, f"frontmatter parses as restricted YAML: {name} ({error})", failures)

    check(parsed_agents == AGENTS, "agent filenames and expected modes are consistent", failures)
    check(sum(mode == "primary" for mode in parsed_agents.values()) == 1, "exactly one project primary agent", failures)

    for relative, headings in REQUIRED_HEADINGS.items():
        path = ROOT / relative
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        for heading in headings:
            check(heading in content, f"required section in {relative}: {heading}", failures)

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    tree = readme.split("## 4. Prerequisites", 1)[0]
    for relative in required_files[3:]:
        if relative.startswith(".opencode/"):
            basename = Path(relative).name
            check(basename in tree, f"README inventories {relative}", failures)
    for match in re.findall(r"(?:\.opencode|workspace)/[A-Za-z0-9_./-]+", readme):
        candidate = match.rstrip(".,:;`)")
        check((ROOT / candidate).exists(), f"README path resolves: {candidate}", failures)

    schema_result = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_schemas.py")], cwd=ROOT, text=True, capture_output=True)
    print(schema_result.stdout, end="")
    if schema_result.stderr:
        print(schema_result.stderr, end="", file=sys.stderr)
    check(schema_result.returncode == 0, "task and result schema fixtures", failures)

    executable = shutil.which("opencode")
    if not executable:
        print("NOT_VERIFIED: opencode agent discovery (CLI unavailable)")
        failures.append("agent discovery was not verified")
    else:
        try:
            result = subprocess.run([executable, "agent", "list"], cwd=ROOT, text=True, capture_output=True, timeout=60)
            output = result.stdout + "\n" + result.stderr
            discovered = dict(re.findall(r"^(\S+) \((primary|subagent|all)\)$", output, re.MULTILINE))
            for name, mode in AGENTS.items():
                check(discovered.get(name) == mode, f"OpenCode discovers {name} as {mode} (observed: {discovered.get(name, 'missing')})", failures)
            project_modes = {name: discovered[name] for name in AGENTS if name in discovered}
            check(sum(mode == "primary" for mode in project_modes.values()) == 1, "discovery has exactly one expected primary agent", failures)
            check(discovered.get(config.get("default_agent")) == "primary", "discovered default agent is primary", failures)
            check(result.returncode == 0, "opencode agent list exits successfully", failures)
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f"NOT_VERIFIED: opencode agent discovery ({error})")
            failures.append("agent discovery could not complete")

    print(f"Project validation: {len(failures)} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
