# src/investigation_agent_platform/application/analysis/python/literals.py
"""Python lexical literal decoding (no execution, no eval).

Every function is total over strings: it returns a decoded value, reports
the spelling as unsupported/invalid, never raises, and never executes code.
Version gating is minimal: ``match``/``X | Y``-style syntax is a parse-level
concern handled by the caller via the repo-declared grammar version.
"""

from __future__ import annotations

import re
import sys

_INT_RE = re.compile(
    r"^[+-]?(?:0[bB][01](?:_?[01])*|0[oO][0-7](?:_?[0-7])*|0[xX][0-9a-fA-F](?:_?[0-9a-fA-F])*|(?:0(?:_?0)*|[1-9](?:_?[0-9])*))$"
)
_FLOAT_RE = re.compile(
    r"^[+-]?(?:(?:[0-9](?:_?[0-9])*\.[0-9](?:_?[0-9])*|[0-9](?:_?[0-9])*\.[0-9]*|\.[0-9](?:_?[0-9])*)(?:[eE][+-]?[0-9](?:_?[0-9])*)?|[0-9](?:_?[0-9])*[eE][+-]?[0-9](?:_?[0-9])*)|^[+-]?(?:inf|Infinity|nan|NaN)$"
)
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_SIMPLE_ESCAPES = {
    "\\": "\\",
    "'": "'",
    '"': '"',
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "0": "\0",
}


def normalize_language_version(version: object) -> tuple[int, int] | None:
    """Normalize a caller-attested (repo/build metadata) Python version.

    Accepts ``"3.10"``, ``(3, 11)``, ``3.12``. ``None`` means unknown.
    Never trusts model-supplied values — callers must pass only trusted
    metadata or ``None``.
    """
    if version is None:
        return None
    if isinstance(version, bool):
        return None
    if isinstance(version, (tuple, list)) and len(version) == 2:
        try:
            major, minor = int(version[0]), int(version[1])
        except (ValueError, TypeError):
            return None
        return (major, minor) if 2 <= major <= 4 and 0 <= minor <= 30 else None
    text = str(version).strip().lstrip("v")
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", text)
    if match:
        return (int(match.group(1)), int(match.group(2)))
    if re.fullmatch(r"\d{1,2}", text):
        return (3, int(text))
    return None


def resolve_language_version(version: object) -> tuple[str, bool]:
    """Return (``"major.minor"`` label, assumed_default).

    Trusted repo/build metadata wins when parseable; otherwise the default
    is the running interpreter's version (the grammar the Tree-sitter
    parser actually implements) and ``assumed_default`` is True so
    resolvers can record the caveat on resolved results.
    """
    level = normalize_language_version(version)
    if level is not None:
        return (f"{level[0]}.{level[1]}", False)
    runtime = sys.version_info
    return (f"{runtime.major}.{runtime.minor}", True)


def parse_int_literal(text: str) -> int | None:
    """Parse a Python integer literal (underscores, 0o/0x/0b required forms)."""
    raw = text.strip().replace("_", "")
    if not _INT_RE.match(text.strip()):
        return None
    try:
        return int(raw, 0)
    except ValueError:
        return None


def parse_float_literal(text: str) -> float | None:
    """Parse a Python float literal. Returns None for non-float spellings."""
    raw = text.strip()
    if not _FLOAT_RE.match(raw):
        return None
    try:
        return float(raw.replace("_", ""))
    except ValueError:
        return None


def _decode_escapes(content: str) -> tuple[str, bool]:
    out: list[str] = []
    i = 0
    while i < len(content):
        ch = content[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = content[i + 1 : i + 2]
        if nxt in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[nxt])
            i += 2
        elif nxt == "x" and re.fullmatch(r"[0-9a-fA-F]{2}", content[i + 2 : i + 4] or ""):
            out.append(chr(int(content[i + 2 : i + 4], 16)))
            i += 4
        elif nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", content[i + 2 : i + 6] or ""):
            out.append(chr(int(content[i + 2 : i + 6], 16)))
            i += 6
        elif nxt in {"\n", ""}:
            return ("", False)
        else:
            return ("", False)
    return ("".join(out), True)


def parse_string_literal(text: str) -> tuple[str | None, str | None]:
    """Decode a simple (non-f, non-bytes) Python string literal.

    Returns (value, limitation). Triple-quoted forms decode without
    incidental-whitespace stripping (declaration text is preserved verbatim).
    f-strings, bytes, and unknown prefixes are unsupported, never executed.
    """
    raw = text.strip()
    match = re.fullmatch(r"([A-Za-z]*)('''|\"\"\"|'|\")([\s\S]*)('''|\"\"\"|'|\")", raw, re.DOTALL)
    if not match:
        return (None, "unsupported_string_literal")
    prefix, opener, content, closer = (
        match.group(1).lower(),
        match.group(2),
        match.group(3),
        match.group(4),
    )
    if opener != closer:
        return (None, "invalid_string_literal")
    if prefix not in {"", "r", "u", "ur", "ru"}:
        # f/F, b/B, and combinations can execute or change type: abstain.
        return (None, "unsupported_string_prefix")
    if prefix.startswith("r"):
        return (content, None)
    value, ok = _decode_escapes(content)
    if not ok:
        return (None, "unsupported_string_escape")
    return (value, None)


def is_valid_name(text: str) -> bool:
    return bool(_NAME_RE.match(text or ""))
