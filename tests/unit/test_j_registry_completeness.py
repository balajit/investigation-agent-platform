"""D1 registry completeness: every statically emitted limitation code is registered.

Fails the build when an analyzer emits a code the registry does not know,
so an unregistered code can never silently read as UNSUPPORTED. Also pins
the D7 sweep: no analyzer builds a dynamic `<lang>_parser_unavailable:<Exc>`
string anymore (the registry keeps the legacy entry for old rows only).
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_ANALYSIS = _ROOT / "src" / "investigation_agent_platform" / "application" / "analysis"

_CODE = r"[a-z][a-z0-9_]{2,}"
_PATTERNS = [
    re.compile(rf'limitation_codes=\("({_CODE})"'),
    re.compile(rf'(?:unresolved|_unresolved|_base_unresolved|_partial)\((?:[^()"\'\n]*,\s*){{0,4}}"({_CODE})"'),
    re.compile(rf'\bcode="({_CODE})"'),
    re.compile(rf'_marker\(\s*[^,]+,\s*"({_CODE})"'),
    re.compile(rf'return \[\],\s*"({_CODE})"'),
    re.compile(rf'WindowAnalysis\(\s*WindowStatus\.\w+,\s*limitations=\("({_CODE})"'),
    re.compile(rf'^\s*"({_CODE})",?\s*$'),
]

_SKIP_FILES = {"limitations.py", "failure.py"}


def _emitted_codes() -> set[str]:
    codes: set[str] = set()
    for path in sorted(_ANALYSIS.rglob("*.py")):
        if path.name in _SKIP_FILES:
            continue
        text = path.read_text()
        for pattern in _PATTERNS:
            codes.update(pattern.findall(text))
    # failure_code outputs (dynamic by exception type, fixed code set).
    codes.update({"grammar_unavailable", "analysis_failed"})
    return codes


def test_every_emitted_code_is_registered():
    from investigation_agent_platform.application.analysis import limitations as lim

    emitted = _emitted_codes()
    # The scan is syntactic; pin the codes this tranche owns so a silent
    # pattern miss cannot make the gate vacuous.
    for owned in (
        "enum_constant_not_modeled",
        "switch_return_not_modeled",
        "literal_too_long",
        "grammar_unavailable",
        "analysis_failed",
    ):
        assert owned in emitted, f"scan missed owned code {owned}"
    registry = set(lim._REGISTRY)
    unregistered = sorted(code for code in emitted if code not in registry)
    assert not unregistered, f"unregistered limitation codes: {unregistered}"


def test_no_dynamic_parser_unavailable_strings():
    offenders = []
    for path in sorted(_ANALYSIS.rglob("*.py")):
        if path.name in _SKIP_FILES:
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if "parser_unavailable:" in line:
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert not offenders, f"dynamic parser-unavailable strings remain: {offenders}"
