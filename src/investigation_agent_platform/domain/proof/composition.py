# src/investigation_agent_platform/domain/proof/composition.py
"""WP-C1 composition + invalidation graph (frozen, pure, deterministic).

Conjunction needs every mandatory premise (route); alternatives need one
complete admissible derivation — a failed alternative never taints a
survivor and never counts as a refutation (K5). Invalidation closure runs
over the actual derivation graph only: transitive, cycle-free, filtered
by each rule's declared ``Deps(R_i)``. Root-premise change invalidates
all transitive dependents (K6); unrelated routes survive; undeclared
changes never invalidate (over-invalidation guard); operational classes
(capabilities, budgets, provider availability, logging config) never
invalidate — they affect progress only, never truth.

Dependency is never inferred from shared files, claims, or evaluations:
callers pass explicit ``depends_on`` edges plus premise membership.
Two derivations citing the same file with no edge between them are
independent. Duplicate premise IDs inside one derivation are rejected
at construction (K14); duplicated evidence across derivations is shared
support, never double-counted corroboration.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.proof.certificate import CheckerOutcome


class CompositionErrorCode(str, Enum):
    """Stable composition/invalidation failure codes (frozen; append-only)."""

    CYCLE = "CYCLE"
    DUPLICATE_PREMISE = "DUPLICATE_PREMISE"
    UNKNOWN_RULE = "UNKNOWN_RULE"
    BAD_GRAPH = "BAD_GRAPH"


class CompositionError(ValueError):
    """Fail-closed composition failure carrying a stable code."""

    def __init__(self, code: CompositionErrorCode | str, detail: str = "") -> None:
        self.code = str(getattr(code, "value", code))
        super().__init__(f"{self.code}: {detail}" if detail else self.code)


# Declared proof-relevant input classes per rule (Deps(R_i), frozen).
# Operational classes are deliberately absent: they can never invalidate.
RULE_DEPS: dict[str, frozenset[str]] = {
    "definition/v1": frozenset(
        {"evidence.content", "evidence.availability", "scope.revision", "rule.bundle"}
    ),
    "call_edge/v1": frozenset(
        {"evidence.content", "evidence.availability", "scope.revision", "rule.bundle"}
    ),
}

# Change classes that affect progress only, never truth (contracts §5.1).
OPERATIONAL_CLASSES: frozenset[str] = frozenset(
    {"capability", "budget", "provider.availability", "logging.config"}
)


class DerivationNode(BaseModel):
    """One node in the derivation graph: explicit edges only."""

    model_config = ConfigDict(frozen=True)

    derivation_id: str = Field(..., max_length=256)
    rule_id: str = Field(..., max_length=128)
    premise_ids: list[str] = Field(default_factory=list, max_length=500)
    depends_on: list[str] = Field(default_factory=list, max_length=100)


def _node(node: DerivationNode | dict[str, Any]) -> DerivationNode:
    if isinstance(node, DerivationNode):
        return node
    try:
        return DerivationNode.model_validate(dict(node))
    except Exception as exc:
        raise CompositionError(CompositionErrorCode.BAD_GRAPH, f"bad node: {exc}") from exc


def make_node(
    derivation_id: str,
    rule_id: str,
    premise_ids: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> DerivationNode:
    """Build a node; duplicate premise IDs inside one node reject (K14)."""
    premises = [str(p) for p in (premise_ids or [])]
    if len(set(premises)) != len(premises):
        raise CompositionError(
            CompositionErrorCode.DUPLICATE_PREMISE,
            f"{derivation_id}: duplicate premise IDs never double-count (K14)",
        )
    if rule_id not in RULE_DEPS:
        raise CompositionError(CompositionErrorCode.UNKNOWN_RULE, f"unregistered rule: {rule_id!r}")
    return DerivationNode(
        derivation_id=str(derivation_id),
        rule_id=str(rule_id),
        premise_ids=premises,
        depends_on=[str(d) for d in (depends_on or [])],
    )


def detect_cycle(edges: Mapping[str, list[str]]) -> list[str]:
    """Return a cycle path or [] (pure DFS, deterministic key order)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {k: WHITE for k in edges}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for dep in edges.get(node, []) or []:
            if dep not in color:
                color[dep] = WHITE
            if color.get(dep) == GRAY:
                return stack[stack.index(dep):] + [dep]
            if color.get(dep) == WHITE:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for key in sorted(edges):
        if color.get(key) == WHITE:
            found = visit(key)
            if found:
                return found
    return []


def assert_acyclic(nodes: Mapping[str, DerivationNode | dict[str, Any]]) -> None:
    """Reject cyclic derivation graphs (K2); self-edges count.

    Closed-graph check (F-C1): every ``depends_on`` target must be a node
    in the graph. An edge to nothing is always a caller bug, and silently
    tolerating it would under-invalidate through the missing edge.
    """
    frozen = {nid: _node(n) for nid, n in (nodes or {}).items()}
    for nid, node in frozen.items():
        for dep in node.depends_on:
            if str(dep) not in frozen:
                raise CompositionError(
                    CompositionErrorCode.BAD_GRAPH,
                    f"{nid} depends on unknown derivation: {dep}",
                )
    edges = {nid: list(node.depends_on) for nid, node in frozen.items()}
    cycle = detect_cycle(edges)
    if cycle:
        raise CompositionError(CompositionErrorCode.CYCLE, " -> ".join(cycle))


def compose_conjunction(results: list[CheckerOutcome | str]) -> tuple[CheckerOutcome, str]:
    """Every mandatory route must be VALID; first failure wins (never collapsed)."""
    rows = list(results or [])
    if not rows:
        return CheckerOutcome.MISSING_PREMISE, "conjunction needs at least one route"
    for raw in rows:
        outcome = raw if isinstance(raw, CheckerOutcome) else CheckerOutcome(str(raw))
        if outcome != CheckerOutcome.VALID:
            return outcome, f"conjunction fails on {outcome.value}"
    return CheckerOutcome.VALID, "all mandatory routes valid"


def compose_alternatives(results: list[CheckerOutcome | str]) -> tuple[CheckerOutcome, str]:
    """One VALID survivor carries the proposition (K5); failures never taint it."""
    rows = list(results or [])
    if not rows:
        return CheckerOutcome.MISSING_PREMISE, "alternatives need at least one route"
    first_failure: CheckerOutcome | None = None
    for raw in rows:
        outcome = raw if isinstance(raw, CheckerOutcome) else CheckerOutcome(str(raw))
        if outcome == CheckerOutcome.VALID:
            return CheckerOutcome.VALID, "surviving route establishes the proposition (K5)"
        if first_failure is None:
            first_failure = outcome
    assert first_failure is not None
    return first_failure, f"no surviving route: {first_failure.value}"


def invalidate_derivations(
    changed_derivation_ids: set[str] | list[str],
    nodes: Mapping[str, DerivationNode | dict[str, Any]],
) -> set[str]:
    """Transitive closure over explicit edges: changed + all dependents (K6).

    Cycle-free by visited set; unrelated nodes are preserved.
    """
    frozen = {nid: _node(n) for nid, n in (nodes or {}).items()}
    reverse: dict[str, set[str]] = {nid: set() for nid in frozen}
    for nid, node in frozen.items():
        for dep in node.depends_on:
            reverse.setdefault(str(dep), set()).add(nid)
    invalidated: set[str] = set()
    stack = [str(c) for c in (changed_derivation_ids or [])]
    while stack:
        current = stack.pop()
        if current in invalidated:
            continue
        invalidated.add(current)
        for dependent in reverse.get(current, ()):
            if dependent not in invalidated:
                stack.append(dependent)
    return {d for d in invalidated if d in frozen}


def invalidate_by_evidence(
    changed_evidence_ids: set[str] | list[str],
    changed_classes: set[str] | list[str],
    nodes: Mapping[str, DerivationNode | dict[str, Any]],
) -> set[str]:
    """Premise-change closure filtered by declared Deps(R_i).

    A node invalidates only when a changed evidence ID is among its
    premises AND a changed class is in its rule's declared deps.
    Undeclared classes (including every operational class) invalidate
    nothing — the over-invalidation guard.
    """
    changed_ev = {str(e) for e in (changed_evidence_ids or [])}
    classes = {str(c) for c in (changed_classes or [])}
    if not changed_ev or not classes:
        return set()
    if classes <= set(OPERATIONAL_CLASSES):
        return set()
    frozen = {nid: _node(n) for nid, n in (nodes or {}).items()}
    direct: set[str] = set()
    for nid, node in frozen.items():
        deps = RULE_DEPS.get(node.rule_id, frozenset())
        if not (classes & set(deps)):
            continue
        if any(pid in changed_ev for pid in node.premise_ids):
            direct.add(nid)
    if not direct:
        return set()
    return invalidate_derivations(direct, frozen)
