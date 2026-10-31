"""Cross-claim conflict checks between claim groups of one hypothesis (review H17b).

Pure; never raises on well-formed input, may raise on hostile input (callers fail closed).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from investigation_agent_platform.application.investigation.claim_identity import (
    canonical_path,
    canonical_target,
)

_POSITIVE = {"DEFINITION", "CALL_EDGE"}


@dataclass(frozen=True)
class ClaimConflict:
    code: str  # REVISION_MIXED (non-blocking) or CLAIM_CONFLICT (blocking)
    detail: str
    blocking: bool


def _targets(asms: list[Any]) -> list[dict[str, Any]]:
    out = []
    for asm in asms:
        target = getattr(asm, "claim_target", None)
        dump = getattr(target, "model_dump", None)
        if callable(dump):
            out.append(canonical_target(dict(dump())))
        elif isinstance(target, Mapping):
            out.append(canonical_target(dict(target)))
        else:
            out.append({})
    return out


def cross_claim_conflicts(groups: Mapping[tuple[str, str], list[Any]]) -> list[ClaimConflict]:
    """Conflicts BETWEEN claim groups of one hypothesis. May raise on hostile input; callers fail closed."""
    conflicts: list[ClaimConflict] = []
    by_kind: dict[tuple[str, str], list[dict[str, Any]]] = {key: _targets(asms) for key, asms in groups.items()}

    # R1: claims about code (not HISTORY, whose subject is a commit) pinned to different revisions.
    revisions = {
        t["commit_id"]
        for (kind, _), targets in by_kind.items()
        if kind != "HISTORY"
        for t in targets
        if t.get("commit_id")
    }
    if len(revisions) > 1:
        conflicts.append(ClaimConflict(
            "REVISION_MIXED",
            "claims are pinned to different revisions: " + ", ".join(sorted(revisions))[:200],
            blocking=False,
        ))

    # R2: ABSENCE of a symbol that another group defines or calls (within the absence scope).
    for (kind, _), targets in by_kind.items():
        if kind != "ABSENCE":
            continue
        for absent in targets:
            symbol = absent.get("symbol")
            scope = absent.get("scope") or ""
            if not symbol:
                continue
            for (other_kind, _), others in by_kind.items():
                if other_kind not in _POSITIVE:
                    continue
                for positive in others:
                    path = canonical_path(positive.get("file_path")) or ""
                    in_scope = (not scope) or path.startswith(scope)
                    if positive.get("symbol") == symbol and in_scope:
                        conflicts.append(ClaimConflict(
                            "CLAIM_CONFLICT",
                            f"one claim says {symbol} is absent in {scope or 'scope'}, another says it is present",
                            blocking=True,
                        ))

    # R3: two groups assert different values for the same (kind, symbol, file, value_semantics).
    seen: dict[tuple[str, str, str, str], str] = {}
    for (kind, _), targets in by_kind.items():
        for t in targets:
            if not t.get("claimed_value"):
                continue
            ident = (kind, t.get("symbol", ""), t.get("file_path", ""), t.get("value_semantics", ""))
            previous = seen.setdefault(ident, t["claimed_value"])
            if previous != t["claimed_value"]:
                conflicts.append(ClaimConflict(
                    "CLAIM_CONFLICT",
                    f"two claimed values for {ident[1] or 'the same target'}: "
                    f"{previous[:60]} and {t['claimed_value'][:60]}",
                    blocking=True,
                ))
    return conflicts
