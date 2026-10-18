"""Stack-frame parsers for exception intake (D2).

Pure helpers: parse Python tracebacks and Java stack traces (including
`Caused by:` chains) into generic frame dicts {file, line, function}.
Unknown lines degrade to None (never raise). Language-generic downstream:
everything past this module operates on frames, never on trace dialects.
"""

from __future__ import annotations

import re
from typing import Any

_PYTHON_FRAME_RE = re.compile(
    r'^\s*File "([^"]+)", line (\d+), in (\S+)\s*$'
)
_JAVA_FRAME_RE = re.compile(
    r"^\s*at\s+([\w.$]+)\(([^():]*)(?::(\d+))?\)\s*$"
)
_JAVA_CAUSED_BY_RE = re.compile(r"^\s*Caused by:\s*(\S+)(?::\s*(.*))?\s*$")
_EXCEPTION_HEAD_RES = (
    re.compile(r"^Traceback \(most recent call last\):\s*$"),
    re.compile(r"^(\S+(?:Error|Exception|Throwable|Failure))\s*:\s*(.*)\s*$"),
)


def parse_python_traceback(text: str) -> list[dict[str, Any]]:
    """Parse a CPython traceback into frames, innermost last (pure)."""
    try:
        frames: list[dict[str, Any]] = []
        for line in str(text or "").splitlines():
            match = _PYTHON_FRAME_RE.match(line)
            if not match:
                continue
            filename, lineno, func = match.groups()
            try:
                lineno_int = int(lineno)
            except (TypeError, ValueError):
                continue
            frames.append(
                {"file": filename.strip(), "line": lineno_int, "function": func.strip()}
            )
        return [f for f in frames if f.get("file")]
    except Exception:
        return []


def parse_java_stacktrace(text: str) -> list[dict[str, Any]]:
    """Parse a JVM stack trace into frames, unwrapping Caused by chains.

    Frames before the first `Caused by:` belong to the outer exception;
    each chain restarts the frame list from its own `at` lines, innermost
    (deepest cause) appended last. Native frames (Native Method/Unknown
    Source) are kept with line None.
    """
    try:
        chains: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for line in str(text or "").splitlines():
            if _JAVA_CAUSED_BY_RE.match(line):
                if current:
                    chains.append(current)
                current = []
                continue
            match = _JAVA_FRAME_RE.match(line)
            if not match:
                continue
            func, source, lineno = match.groups()
            try:
                lineno_int = int(lineno) if lineno is not None else None
            except (TypeError, ValueError):
                lineno_int = None
            if source in ("Native Method", "Unknown Source"):
                entry: dict[str, Any] = {"function": func.strip()}
            else:
                entry = {"function": func.strip(), "file": source.strip()}
                if lineno_int is not None:
                    entry["line"] = lineno_int
            current.append(entry)
        if current:
            chains.append(current)
        if not chains:
            return []
        # Deepest cause first (root fault), then outer wrappers.
        ordered: list[dict[str, Any]] = []
        for chain in reversed(chains):
            ordered.extend(chain)
        return ordered
    except Exception:
        return []


def parse_stack_frames(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Detect dialect and parse (pure). Returns (language, frames).

    Language is "python", "java", or "" when undetected (frames empty).
    """
    try:
        body = str(text or "")
        if "Traceback (most recent call last):" in body:
            return "python", parse_python_traceback(body)
        if re.search(r"^\s*at\s+[\w.$]+\(", body, re.MULTILINE):
            frames = parse_java_stacktrace(body)
            if frames:
                return "java", frames
        if re.search(r"^\s*Caused by:", body, re.MULTILINE):
            return "java", parse_java_stacktrace(body)
        return "", []
    except Exception:
        return "", []
