"""Pure S7 continuation derivation and idempotency helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from investigation_agent_platform.api.errors import sanitize_extra
from investigation_agent_platform.domain.finding.models import (
    _FETCH_ACTION_TOKEN,
    _FETCH_PATH_TOKEN,
)
from investigation_agent_platform.domain.investigation.models import ActionType
from investigation_agent_platform.infrastructure.telemetry.story import TRACE_LEVEL

logger = logging.getLogger(__name__)

_CALL_SITES_SUFFIX = re.compile(r"\s+call\s+sites\s*$", re.IGNORECASE)
_CALL_SITE_PAREN = re.compile(r"\s*\(\s*call\s+site\s+of\s+([^)]*)\)\s*", re.IGNORECASE)
_FOR_SUFFIX = re.compile(r"\s+for\s+(.+?)\s*$", re.IGNORECASE)
_WITHIN_SUFFIX = re.compile(r"\s+within\s+(.+?)\s*$", re.IGNORECASE)
_LINES_FRAGMENT = re.compile(r"\s+lines\s+(\S+)\s*$", re.IGNORECASE)
_LINE_RANGE = re.compile(r"^(\d+)-(\d+)$")


def continuation_idempotency_key(
    tenant_id: str,
    investigation_id: UUID,
    handoff_version: str,
    action: str,
    parameters: Mapping[str, Any],
) -> str:
    """Return the canonical SHA-256 identity of an exact continuation."""
    material = json.dumps(
        {
            "tenant_id": tenant_id,
            "investigation_id": str(investigation_id),
            "handoff_version": handoff_version,
            "action": action,
            "parameters": parameters,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _qualifiers(remainder: str) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {}
    remainder = _CALL_SITES_SUFFIX.sub("", remainder, count=1).strip()

    parenthetical = _CALL_SITE_PAREN.search(remainder)
    if parenthetical:
        symbol = parenthetical.group(1).strip()
        if symbol and symbol.casefold() != "the callee":
            params["symbol"] = symbol
        remainder = (remainder[: parenthetical.start()] + remainder[parenthetical.end() :]).strip()

    for_suffix = _FOR_SUFFIX.search(remainder)
    if for_suffix:
        params["symbol"] = for_suffix.group(1).strip()
        remainder = remainder[: for_suffix.start()].strip()

    within_suffix = _WITHIN_SUFFIX.search(remainder)
    if within_suffix:
        params["scope"] = within_suffix.group(1).strip()
        remainder = remainder[: within_suffix.start()].strip()

    lines = _LINES_FRAGMENT.search(remainder)
    if lines:
        match = _LINE_RANGE.fullmatch(lines.group(1))
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            if start >= 1 and end >= start:
                params["start_line"] = start
                params["end_line"] = end
            else:
                params["_invalid_lines"] = True
        else:
            params["_invalid_lines"] = True
        remainder = remainder[: lines.start()].strip()
    return remainder, params


def _derive_continuation_params_inner(
    next_fetch: str | None,
) -> tuple[ActionType, dict[str, Any]] | None:
    """Parse one concrete next-fetch string into an executable action payload."""
    try:
        text = str(next_fetch or "").strip()
        action_match = _FETCH_ACTION_TOKEN.search(text)
        if action_match:
            action = ActionType(action_match.group(1))
            remainder = (text[: action_match.start()] + text[action_match.end() :]).strip()
        else:
            if not _FETCH_PATH_TOKEN.search(text):
                return None
            action = ActionType.GET_CODE
            remainder = text

        remainder, qualifiers = _qualifiers(remainder)
        if qualifiers.pop("_invalid_lines", False):
            return None
        path_match = _FETCH_PATH_TOKEN.search(remainder)
        file_path = path_match.group(0) if path_match else None

        if action in {ActionType.GET_CODE, ActionType.GET_CODE_HISTORY}:
            if not file_path:
                return None
            params: dict[str, Any] = {"file_path": file_path}
            if action == ActionType.GET_CODE:
                params.update({key: qualifiers[key] for key in ("start_line", "end_line") if key in qualifiers})
            return action, params

        if action == ActionType.SEARCH_CODE:
            if remainder.casefold().startswith("within "):
                return None
            query = remainder.strip()
            if not query:
                return None
            params = {"query": query}
            if qualifiers.get("scope"):
                params["scope"] = qualifiers["scope"]
            return action, params

        if action == ActionType.FIND_SYMBOL:
            value = remainder.strip()
            return (action, {"symbol_name": value}) if value else None
        if action == ActionType.FIND_CALL_GRAPH:
            value = remainder.strip()
            return (action, {"symbol": value}) if value else None
        if action == ActionType.SEARCH_LOGS:
            value = remainder.strip()[:200]
            return (action, {"keywords": [value]}) if value else None
        if action == ActionType.SEARCH_REF_DOCS:
            value = remainder.strip()
            return (action, {"query": value}) if value else None
        return None
    except (TypeError, ValueError, OverflowError):
        return None


def derive_continuation_params(
    next_fetch: str | None,
) -> tuple[ActionType, dict[str, Any]] | None:
    """Parse one concrete next-fetch string (traced wrapper)."""
    result = _derive_continuation_params_inner(next_fetch)
    if logger.isEnabledFor(TRACE_LEVEL):
        logger.log(
            TRACE_LEVEL,
            "continuation params derived",
            extra=sanitize_extra(
                {
                    "event": "continuation.derive_continuation_params",
                    "outcome": "derived" if result is not None else "unparseable",
                    "action": result[0].value if result is not None else None,
                }
            ),
        )
    return result
