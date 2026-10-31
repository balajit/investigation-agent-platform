# src/investigation_agent_platform/application/analysis/grammar.py
"""Grammar loading, caching and identity for the Java and Python analyzers (J6, single home)."""

from __future__ import annotations

import threading
import time
from functools import cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any

from investigation_agent_platform.application.analysis.failure import GrammarUnavailable

SUPPORTED = frozenset({"java", "python"})
_RETRY_AFTER_S = 60.0

_languages: dict[str, Any] = {}
_abi: dict[str, int] = {}
_failed_at: dict[str, float] = {}
_local = threading.local()


@cache
def _package_version(name: str) -> str:
    try:
        return _pkg_version(name)
    except PackageNotFoundError:
        return "unknown"


def language(name: str) -> Any:
    """The process-wide ``tree_sitter.Language``; loads once, fails typed and fast."""
    if name not in SUPPORTED:
        raise GrammarUnavailable(f"unsupported_language:{name}")
    cached = _languages.get(name)
    if cached is not None:
        return cached
    failed = _failed_at.get(name)
    if failed is not None and time.monotonic() - failed < _RETRY_AFTER_S:
        # A cold or air-gapped worker must not retry a download on every row.
        raise GrammarUnavailable("negative_cache")
    try:
        from tree_sitter_language_pack import get_language

        loaded = get_language(name)
    except Exception as exc:  # pack Error subclasses, ImportError, OSError
        _failed_at[name] = time.monotonic()
        raise GrammarUnavailable(type(exc).__name__) from exc
    _languages[name] = loaded
    _failed_at.pop(name, None)
    return loaded


def parser(name: str) -> Any:
    """One ``Parser`` per thread: parsers are not documented as thread-safe."""
    cache = getattr(_local, "parsers", None)
    if cache is None:
        cache = _local.parsers = {}
    cached = cache.get(name)
    if cached is None:
        from tree_sitter import Parser

        cached = cache[name] = Parser(language(name))
    return cached


def query(name: str, pattern: str) -> Any:
    """One compiled ``Query`` per thread and pattern."""
    cache = getattr(_local, "queries", None)
    if cache is None:
        cache = _local.queries = {}
    key = (name, pattern)
    cached = cache.get(key)
    if cached is None:
        from tree_sitter import Query

        cached = cache[key] = Query(language(name), pattern)
    return cached


def identity(name: str) -> str:
    """``tree-sitter-java;pack=1.20.0;ts=0.26.0;abi=14`` (field cap is 64).

    Never raises: an unloadable grammar reports ``abi=unavailable`` so failure
    records can still be built.
    """
    abi = _abi.get(name)
    if abi is None:
        try:
            abi = _abi[name] = int(language(name).abi_version)
        except GrammarUnavailable:
            abi = None
    return (
        f"tree-sitter-{name};pack={_package_version('tree-sitter-language-pack')};"
        f"ts={_package_version('tree-sitter')};abi={abi if abi is not None else 'unavailable'}"
    )
