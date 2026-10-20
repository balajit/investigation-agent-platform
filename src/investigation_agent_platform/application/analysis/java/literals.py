# src/investigation_agent_platform/application/analysis/java/literals.py
"""Java lexical literal decoding under JLS semantics (Phase 2).

Never parses Java with Python's ``ast`` and never executes snippets. Every
function is total over strings: it returns a decoded value, reports that a
version-gated form needs a known source level, or reports the spelling as
invalid Java. Callers map those outcomes to RESOLVED / PARTIAL.
"""

from __future__ import annotations

import re

INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

_DEC_RE = re.compile(r"^((?:0|[1-9](?:[0-9]|_[0-9])*))([lL])?$")
_HEX_RE = re.compile(r"^0[xX]([0-9a-fA-F](?:[0-9a-fA-F]|_[0-9a-fA-F])*)([lL])?$")
_OCT_RE = re.compile(r"^0(_?[0-7])+$")
_BIN_RE = re.compile(r"^0[bB]([01](?:[01]|_[01])*)([lL])?$")
_FLOAT_DEC_RE = re.compile(
    r"^(?:"
    r"[0-9](?:[0-9]|_[0-9])*\.(?:[0-9](?:[0-9]|_[0-9])*)?"
    r"|\.[0-9](?:[0-9]|_[0-9])*"
    r"|[0-9](?:[0-9]|_[0-9])*"
    r")"
    r"(?:[eE][+-]?[0-9](?:[0-9]|_[0-9])*)?"
    r"([fFdD])?$"
)
_UNICODE_ESCAPE_RE = re.compile(r"\\u+([0-9a-fA-F]{4})")
_SIMPLE_ESCAPES = {
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "f": "\f",
    "r": "\r",
    '"': '"',
    "'": "'",
    "\\": "\\",
}


def normalize_source_level(version: object) -> int | None:
    """Normalize a caller-attested major Java release (8, 11, 17, 21, ...).

    ``None`` means unknown. Only trusted repository/build metadata may supply
    this; model- or action-supplied levels are untrusted hints and must not
    reach this function. Returns ``None`` when unparseable.
    """
    if version is None:
        return None
    if isinstance(version, bool):
        return None
    if isinstance(version, int):
        return version if 1 <= version <= 99 else None
    text = str(version).strip()
    if re.fullmatch(r"1\.(\d+)", text):
        return int(text.split(".")[1])
    if re.fullmatch(r"\d{1,2}", text):
        return int(text)
    return None


def parse_integer_literal(text: str) -> tuple[bool, int] | None:
    """Parse a Java integer literal to (is_long, magnitude).

    Returns ``None`` for any spelling Java rejects: ``0o``/``0O`` prefixes,
    misplaced or doubled underscores, ``08``/``09``, bad suffixes, and
    magnitudes beyond one past the type maximum (the unary-minus operand
    allowance; bare literals at exactly max+1 are still illegal and rejected
    by the caller). Never raises.
    """
    raw = text.strip()
    match = _DEC_RE.fullmatch(raw)
    if match:
        return _magnitude(match.group(1), match.group(2), 10)
    match = _HEX_RE.fullmatch(raw)
    if match:
        return _magnitude(match.group(1), match.group(2), 16)
    if _OCT_RE.fullmatch(raw):
        return _magnitude(raw.replace("_", ""), None, 8)
    match = _BIN_RE.fullmatch(raw)
    if match:
        return _magnitude(match.group(1), match.group(2), 2)
    return None


def _magnitude(digits: str, suffix: str | None, base: int) -> tuple[bool, int] | None:
    is_long = suffix is not None and suffix in {"l", "L"}
    if suffix is not None and not is_long:
        return None
    try:
        magnitude = int(digits.replace("_", ""), base)
    except ValueError:
        return None
    limit = (2**63 if is_long else 2**31) + 1
    if magnitude >= limit:
        return None
    return (is_long, magnitude)


def integer_literal_value(
    text: str, *, negated: bool = False, field_type: str = "int"
) -> int | None:
    """Resolve an integer literal (with optional unary-minus context).

    ``-2147483648`` is legal (unary minus over the max+1 magnitude); the bare
    ``2147483648`` literal is not. Returns the signed value or ``None``.
    """
    parsed = parse_integer_literal(text)
    if parsed is None:
        return None
    is_long, magnitude = parsed
    limit = 2**63 - 1 if is_long else 2**31 - 1
    if negated:
        value = -magnitude
        if value < (INT64_MIN if is_long else INT32_MIN):
            return None
        return value
    if magnitude > limit:
        return None
    return magnitude


def parse_floating_literal(text: str) -> float | None:
    """Parse a Java decimal floating-point literal (hex floats unsupported)."""
    raw = text.strip()
    if raw[:2].lower() in {"0x"}:
        return None
    match = _FLOAT_DEC_RE.fullmatch(raw)
    if not match:
        return None
    body, suffix = raw, match.group(1)
    if suffix:
        body = body[:-1]
    if "." not in body and "e" not in body and "E" not in body and suffix is None:
        return None
    try:
        return float(body.replace("_", ""))
    except ValueError:
        return None


def _decode_escape(text: str, pos: int, level: int | None) -> tuple[str, int] | None | str:
    """Decode one escape at ``pos`` (just past the backslash).

    Returns ``(decoded, next_pos)``, the string ``"version_gated"`` when the
    form needs a newer source level than attested, or ``None`` when invalid.
    """
    if pos >= len(text):
        return None
    char = text[pos]
    if char in _SIMPLE_ESCAPES:
        return (_SIMPLE_ESCAPES[char], pos + 1)
    if char == "s":
        if level is None or level < 15:
            return "version_gated"
        return (" ", pos + 1)
    if char == "u":
        match = _UNICODE_ESCAPE_RE.match(text, pos - 1)
        if not match:
            return None
        return (chr(int(match.group(1), 16)), match.end())
    if char in "01234567":
        end = pos + 1
        if char in "0123" and text[pos + 1 : pos + 2] in "01234567":
            end = pos + 2
            if text[pos + 2 : pos + 3] in "01234567":
                end = pos + 3
        return (chr(int(text[pos:end], 8)), end)
    return None


def decode_string_content(content: str, level: int | None) -> tuple[str, str | None]:
    """Decode raw string-literal content. Returns (value, limitation_or_None)."""
    out: list[str] = []
    pos = 0
    while pos < len(content):
        char = content[pos]
        if char != "\\":
            out.append(char)
            pos += 1
            continue
        decoded = _decode_escape(content, pos + 1, level)
        if decoded is None:
            return ("", "invalid_java_literal")
        if decoded == "version_gated":
            return ("", "unsupported_language_version")
        piece, pos = decoded
        out.append(piece)
    return ("".join(out), None)


def decode_character_literal(text: str, level: int | None) -> tuple[str, str | None]:
    """Decode a char literal to ``U+XXXX``. Returns (value, limitation_or_None)."""
    raw = text.strip()
    if len(raw) < 2 or not raw.startswith("'") or not raw.endswith("'"):
        return ("", "invalid_java_literal")
    value, limitation = decode_string_content(raw[1:-1], level)
    if limitation is not None:
        return ("", limitation)
    if len(value) != 1:
        return ("", "invalid_java_literal")
    return (f"U+{ord(value):04X}", None)


def decode_string_literal(text: str, level: int | None) -> tuple[str, str | None]:
    """Decode a double-quoted string literal. Returns (value, limitation_or_None)."""
    raw = text.strip()
    if len(raw) < 2 or not raw.startswith('"') or not raw.endswith('"'):
        return ("", "invalid_java_literal")
    return decode_string_content(raw[1:-1], level)


def decode_text_block(text: str, level: int | None) -> tuple[str, str | None]:
    """Decode a text block per JLS incidental-whitespace stripping (needs 15+)."""
    raw = text.strip()
    if not raw.startswith('"""') or not raw.endswith('"""') or len(raw) < 6:
        return ("", "invalid_java_literal")
    if level is None or level < 15:
        return ("", "unsupported_language_version")
    content = raw[3:-3].replace("\r\n", "\n").replace("\r", "\n")
    lines = content.split("\n")
    # The opening delimiter is always followed by a line terminator, so the
    # first segment is blank by construction; it carries no content.
    if lines and not lines[0].strip():
        lines = lines[1:]
    non_blank = [line for line in lines if line.strip()]
    candidates = non_blank + ([lines[-1]] if lines else [])
    prefix = candidates[0][: len(candidates[0]) - len(candidates[0].lstrip())] if candidates else ""
    for line in candidates[1:]:
        indent = line[: len(line) - len(line.lstrip())]
        while not indent.startswith(prefix) and prefix:
            prefix = prefix[:-1]
        common = 0
        for a, b in zip(prefix, indent):
            if a != b or a not in {" ", "\t"}:
                break
            common += 1
        prefix = prefix[:common]
    stripped = [line[len(prefix) :] if line.startswith(prefix) else line.lstrip() for line in lines]
    return ("\n".join(stripped), None)
