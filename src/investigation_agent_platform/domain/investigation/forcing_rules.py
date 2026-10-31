"""Forcing-rule vocabulary shared by the workflow cascade, the history stamp and telemetry.

Pure and stdlib-only on purpose: workflow code imports it through sandbox pass-through and
activity code imports it for the telemetry stamp, so it must import neither package.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: Claim kinds the smallest-fetch rule counts separately (cap 2 each). A test pins this to
#: ``forcing.SMALLEST_FETCH_ORDER`` and to ``verification._SMALLEST_FETCH_ORDER``.
SMALLEST_FETCH_KINDS: tuple[str, ...] = ("DEFINITION", "HISTORY", "BEHAVIOR", "ABSENCE", "CAUSAL")


class Force(StrEnum):
    """The forcing flags stamped on the reason data. Values are the stored flag keys."""

    ABSENCE_DISCONFIRMATION = "forced_absence_disconfirmation"
    NOT_CHECKABLE = "forced_not_checkable"
    PIVOT_RECOLLECT = "forced_pivot_recollect"
    WALK_STACK = "forced_walk_stack"
    REQUERY = "forced_requery"
    FLOOR = "forced_floor"
    FORMULATE = "forced_formulate"
    REREAD = "forced_reread"
    CALLSITE = "forced_callsite"
    LOG_SEARCH = "forced_log_search"
    REPLAN = "forced_replan"
    SMALLEST_FETCH = "forced_smallest_fetch"

    @property
    def label(self) -> str:
        """Short name for the timeline summary and the shadow trace (``forced_`` stripped)."""
        return self.value.removeprefix("forced_")


class ForcePayload(StrEnum):
    """Data keys a rule stamps beside its flag: strings, never flags (F8 allowed these)."""

    SMALLEST_FETCH_KIND = "forced_smallest_fetch_kind"
    SMALLEST_FETCH_GAP_ID = "forced_smallest_fetch_gap_id"
    #: Which rule wrote ``forced_formulate``: "formulate" or "synthesis" (they share the force).
    FORMULATE_RULE = "forced_formulate_rule"


PRE_CASCADE_FORCES = frozenset({Force.ABSENCE_DISCONFIRMATION, Force.NOT_CHECKABLE})


@dataclass(frozen=True)
class Rule:
    """One cascade rule (unchanged from F8, plus the counters it owns)."""

    name: str
    force: Force
    stands_down_on: frozenset[Force]
    exempt: dict[Force, str] = field(default_factory=dict)
    #: Counter keys this rule increments; empty means a single counter named after the rule.
    counters: tuple[str, ...] = ()

    @property
    def counter_keys(self) -> tuple[str, ...]:
        return self.counters or (self.name,)


_DISJOINT_COMMITTAL = (
    "disjoint by construction: this rule triggers only on committal proposals "
    "(FORMULATE/VERIFY/CONCLUDE) while a pivot needs a fetch proposal "
    "(PIVOT_FILE_ACTIONS); pinned by test"
)

#: The cascade's rules in evaluation order.
RULES: tuple[Rule, ...] = (
    Rule("pivot", Force.PIVOT_RECOLLECT, frozenset()),
    Rule(
        "walk_stack",
        Force.WALK_STACK,
        frozenset(),
        {
            Force.PIVOT_RECOLLECT: (
                "overrides the pivot's dispatch and clears its flag (F5): explicit "
                "intake frames outrank every other gathering rule"
            )
        },
    ),
    Rule("requery", Force.REQUERY, frozenset({Force.WALK_STACK, Force.PIVOT_RECOLLECT})),
    Rule(
        "floor",
        Force.FLOOR,
        frozenset({Force.WALK_STACK, Force.REQUERY}),
        {Force.PIVOT_RECOLLECT: _DISJOINT_COMMITTAL},
    ),
    Rule(
        "formulate",
        Force.FORMULATE,
        frozenset({Force.WALK_STACK, Force.FLOOR, Force.REQUERY, Force.PIVOT_RECOLLECT}),
    ),
    Rule(
        "synthesis",
        Force.FORMULATE,
        frozenset(
            {
                Force.WALK_STACK,
                Force.FORMULATE,
                Force.FLOOR,
                Force.REQUERY,
                Force.PIVOT_RECOLLECT,
            }
        ),
    ),
    Rule(
        "reread",
        Force.REREAD,
        frozenset({Force.FORMULATE, Force.WALK_STACK, Force.FLOOR, Force.REQUERY}),
        {Force.PIVOT_RECOLLECT: _DISJOINT_COMMITTAL},
    ),
    Rule(
        "callsite",
        Force.CALLSITE,
        frozenset(
            {
                Force.FORMULATE,
                Force.WALK_STACK,
                Force.FLOOR,
                Force.REREAD,
                Force.REQUERY,
                Force.PIVOT_RECOLLECT,
            }
        ),
    ),
    Rule(
        "log_search",
        Force.LOG_SEARCH,
        frozenset(
            {
                Force.FORMULATE,
                Force.REREAD,
                Force.WALK_STACK,
                Force.FLOOR,
                Force.CALLSITE,
                Force.REQUERY,
                Force.PIVOT_RECOLLECT,
            }
        ),
    ),
    Rule(
        "replan",
        Force.REPLAN,
        frozenset(
            {
                Force.FORMULATE,
                Force.REREAD,
                Force.LOG_SEARCH,
                Force.WALK_STACK,
                Force.FLOOR,
                Force.CALLSITE,
                Force.REQUERY,
                Force.PIVOT_RECOLLECT,
            }
        ),
    ),
    Rule(
        "smallest_fetch",
        Force.SMALLEST_FETCH,
        frozenset(
            {
                Force.FORMULATE,
                Force.REREAD,
                Force.WALK_STACK,
                Force.FLOOR,
                Force.CALLSITE,
                Force.REQUERY,
                Force.LOG_SEARCH,
                Force.REPLAN,
                Force.NOT_CHECKABLE,
                Force.PIVOT_RECOLLECT,
            }
        ),
        counters=tuple(f"smallest_fetch_{kind.lower()}" for kind in SMALLEST_FETCH_KINDS),
    ),
)


def check_rule_contract(rules: tuple[Rule, ...] | list[Rule]) -> list[str]:
    """Violations of the stand-down contract (empty list = satisfied). Pure."""
    problems: list[str] = []
    seen_names: set[str] = set()
    earlier: list[Force] = []
    for rule in rules:
        if rule.name in seen_names:
            problems.append(f"duplicate rule name: {rule.name}")
        seen_names.add(rule.name)
        for force in sorted(rule.stands_down_on & set(rule.exempt), key=lambda f: f.value):
            problems.append(f"rule {rule.name} both stands down on and exempts {force.value}")
        for force in earlier:
            if force not in rule.stands_down_on and force not in rule.exempt:
                problems.append(
                    f"rule {rule.name} neither stands down on nor exempts earlier force "
                    f"{force.value}"
                )
        for force, reason in rule.exempt.items():
            if not str(reason or "").strip():
                problems.append(f"rule {rule.name} exempts {force.value} without a reason")
            if force not in earlier:
                problems.append(f"rule {rule.name} exempts {force.value}, which is not earlier")
        for force in sorted(rule.stands_down_on, key=lambda f: f.value):
            if force not in earlier and force not in PRE_CASCADE_FORCES:
                problems.append(
                    f"rule {rule.name} stands down on {force.value}, which no earlier rule writes"
                )
        if rule.force not in earlier:
            earlier.append(rule.force)
    return problems


#: Counters that no rule owns: the absence-disconfirmation pre-cascade step.
EXTRA_COUNTER_KEYS: tuple[str, ...] = ("absence_disconfirmation",)


def counter_keys() -> tuple[str, ...]:
    """Every loop counter key, derived from the rule table (stable order, no duplicates)."""
    keys: list[str] = []
    for rule in RULES:
        for key in rule.counter_keys:
            if key not in keys:
                keys.append(key)
    for key in EXTRA_COUNTER_KEYS:
        if key not in keys:
            keys.append(key)
    return tuple(keys)


COUNTER_KEYS: tuple[str, ...] = counter_keys()


def new_counters() -> dict[str, int]:
    return dict.fromkeys(COUNTER_KEYS, 0)


def telemetry_key(counter: str) -> str:
    return f"forced_{counter}_count"


def counter_telemetry(counters: Mapping[str, Any]) -> dict[str, int]:
    """Telemetry entries for every counter; a malformed value is zeroed, never dropped."""
    out: dict[str, int] = {}
    for key in COUNTER_KEYS:
        value = counters.get(key, 0)
        ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        out[telemetry_key(key)] = value if ok else 0
    return out


def merge_counts(counters: dict[str, int], returned: Any) -> None:
    """Adopt the cascade's returned counters per key; a malformed value keeps the old one."""
    if not isinstance(returned, Mapping):
        return
    for key in COUNTER_KEYS:
        if key not in returned:
            continue
        try:
            value = int(returned[key])
        except (TypeError, ValueError):
            continue
        if value >= 0:
            counters[key] = value


#: Numeric workflow scalars stamped into metadata["loop_telemetry"] (not forcing counters).
LOOP_TELEMETRY_SCALARS: tuple[str, ...] = (
    "llm_tokens_total",
    "llm_cost_total",
    "formulated_count",
    "evidence_banked_total",
    "overconfident_turns",
    "iterations",
    "d6_blocked_no_evidence_count",
    "fetches_dispatched",
    "defer_repairs_used",
    "defer_repairs_limit",
)

#: Workflow keys stamped by their own validated block in _derive_loop_telemetry.
TELEMETRY_HANDLED_ELSEWHERE: frozenset[str] = frozenset(
    {
        "triage_route",
        "claim_binding_kept",
        "claim_binding_evaporated",
        "claim_binding_unmapped",
        "claim_binding_repaired",
        "claim_binding_repair_shadow",
        "claim_binding_version",
        "shadow_trace",
        "shadow_summary",
        "definition_shadow_trace",
        "shadow_definition_summary",
    }
)

#: Workflow keys deliberately not stamped.
TELEMETRY_DECLARED_NOT_STAMPED: Mapping[str, str] = {"plan": "rides action history, not telemetry"}


def telemetry_allowlist() -> frozenset[str]:
    return frozenset(LOOP_TELEMETRY_SCALARS) | {telemetry_key(key) for key in COUNTER_KEYS}


class HistoryKey(StrEnum):
    """History-entry keys the cascade stamps (flags + payloads), one definition."""

    ABSENCE_DISCONFIRMATION = "forced_absence_disconfirmation"
    NOT_CHECKABLE = "forced_not_checkable"
    PIVOT_RECOLLECT = "forced_pivot_recollect"
    WALK_STACK = "forced_walk_stack"
    REQUERY = "forced_requery"
    FLOOR = "forced_floor"
    FORMULATE = "forced_formulate"
    REREAD = "forced_reread"
    CALLSITE = "forced_callsite"
    LOG_SEARCH = "forced_log_search"
    REPLAN = "forced_replan"
    SMALLEST_FETCH = "forced_smallest_fetch"
    SMALLEST_FETCH_KIND = "forced_smallest_fetch_kind"
    SMALLEST_FETCH_GAP_ID = "forced_smallest_fetch_gap_id"
    FORMULATE_RULE = "forced_formulate_rule"


def fired_labels(reason_data: Mapping[str, Any]) -> list[str]:
    """Labels of every force set on the data, in ``Force`` declaration order (pure)."""
    try:
        return [force.label for force in Force if reason_data.get(force.value)]
    except Exception:
        return []


def forced_stamp(reason_data: Mapping[str, Any]) -> dict[str, bool]:
    """The history-entry flag block: one bool per ``Force`` member."""
    return {force.value: bool(reason_data.get(force.value, False)) for force in Force}


def payload_stamp(reason_data: Mapping[str, Any]) -> dict[str, str]:
    """The history-entry payload block: one string per ``ForcePayload`` member."""
    return {p.value: str(reason_data.get(p.value, "") or "") for p in ForcePayload}


def shadow_forced(reason_data: Mapping[str, Any]) -> list[str]:
    """The shadow trace ``forced`` list: every fired label, plus ``synthesis`` when it wrote FORMULATE."""
    labels = set(fired_labels(reason_data))
    if (
        reason_data.get(Force.FORMULATE.value)
        and reason_data.get(ForcePayload.FORMULATE_RULE.value) == "synthesis"
    ):
        labels.add("synthesis")
    return sorted(labels)


def shadow_forcing_used(counters: Mapping[str, Any], *, synthesis_separate: bool) -> dict[str, int]:
    """Per-rule used counts for the shadow snapshot (pure)."""

    def used(key: str) -> int:
        value = counters.get(key, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    out = {
        "walk_stack": used("walk_stack"),
        "requery": used("requery"),
        "floor": used("floor"),
        "formulate": used("formulate"),
        "reread": used("reread"),
        "callsite": used("callsite"),
        "log_search": used("log_search"),
        "replan": used("replan"),
        "pivot": used("pivot"),
    }
    if synthesis_separate:
        out["formulate"] = used("formulate")
        out["synthesis"] = used("synthesis")
    else:
        out["synthesis"] = used("formulate")
    return out
