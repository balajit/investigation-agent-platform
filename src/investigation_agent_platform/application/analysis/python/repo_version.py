# src/investigation_agent_platform/application/analysis/python/repo_version.py
"""Trusted repository Python version discovery (pinned-revision metadata only).

Reads ``.python-version`` / ``pyproject.toml`` / ``setup.cfg`` from the
analyzed repository at the same immutable SHA the fact resolver uses. The
reader callable is supplied by the worker hook (gateway ``get_source`` at
the pinned revision, same authorization, never banked as evidence), so this
module performs no I/O, follows no references, and never trusts
model-supplied values. Anything unparseable yields ``None`` and the
resolvers fall back to the assumed runtime default.
"""

from __future__ import annotations

import configparser
import re
import tomllib
from collections.abc import Awaitable, Callable

from investigation_agent_platform.application.analysis.python import literals as _lit

#: Candidate files in preference order: exact pin first, then build metadata.
VERSION_FILENAMES = (".python-version", "pyproject.toml", "setup.cfg")

_SPEC_RE = re.compile(r"(>=|==|~=)\s*v?(\d+)\.(\d+)")
_NUMBERED_LINE_RE = re.compile(r"^\s*\d+\| ?(.*)$")


def _denumber(content: str) -> str:
    """Strip optional ``N| `` evidence numbering; whole-file reads are raw."""
    lines = []
    for raw in (content or "").splitlines():
        match = _NUMBERED_LINE_RE.match(raw)
        lines.append(match.group(1) if match else raw)
    return "\n".join(lines)


def specifier_lower_bound(spec: str) -> tuple[int, int] | None:
    """Lower bound of a ``requires-python``-style specifier (``>=3.10,<3.14`` → 3.10).

    Considers only ``>=``/``==``/``~=`` pins and takes the minimum; strict
    ``>``/exclusions alone are not a usable floor and yield ``None``.
    """
    bounds = [
        (int(major), int(minor))
        for _, major, minor in _SPEC_RE.findall(spec or "")
        if 2 <= int(major) <= 4 and 0 <= int(minor) <= 30
    ]
    return min(bounds) if bounds else None


def parse_version_file(filename: str, content: str) -> tuple[int, int] | None:
    """Parse one version file's text. Returns ``(major, minor)`` or ``None``."""
    text = _denumber(content)
    name = (filename or "").rsplit("/", 1)[-1]
    if name == ".python-version":
        for raw in text.splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                return _lit.normalize_language_version(line)
        return None
    if name == "pyproject.toml":
        try:
            data = tomllib.loads(text)
        except (tomllib.TOMLDecodeError, ValueError):
            return None
        project = data.get("project") if isinstance(data, dict) else None
        spec = project.get("requires-python") if isinstance(project, dict) else None
        if not isinstance(spec, str) or not spec.strip():
            return None
        return specifier_lower_bound(spec)
    if name == "setup.cfg":
        parser = configparser.ConfigParser()
        try:
            parser.read_string(text)
        except configparser.Error:
            return None
        try:
            spec = parser.get("options", "python_requires", fallback=None)
        except configparser.Error:
            return None
        if not spec or not spec.strip():
            return None
        return specifier_lower_bound(spec)
    return None


async def resolve_repo_python_version(
    read_file: Callable[[str], Awaitable[str | None]],
) -> tuple[int, int] | None:
    """First parseable version wins across :data:`VERSION_FILENAMES`.

    ``read_file`` maps a repo-root filename to its text (or ``None`` when
    missing/unusable). Never raises: every failure means "no trusted
    version".
    """
    for filename in VERSION_FILENAMES:
        try:
            content = await read_file(filename)
        except Exception:
            continue
        if not content or not content.strip():
            continue
        try:
            parsed = parse_version_file(filename, content)
        except Exception:
            continue
        if parsed is not None:
            return parsed
    return None
