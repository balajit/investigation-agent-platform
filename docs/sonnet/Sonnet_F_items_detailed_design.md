# Design: Group F — forcing-cascade observability, replay posture, rule-table closure, carrier guard and pivot measurement (F9, F10, F8/F2/F4 follow-ups)

Loop-version placeholder for this group: `LV_F` (the index maps placeholders to real numbers at implementation time; the shipped version today is `lv7`). Code below spells the constant `LOOP_VERSION_LV_F`; never hardcode a number for a new version.

Verified against git HEAD `7860fb4` (clean). All probes were offline, read-only, and ran in the repo venv; nothing in the repo was edited except this file. The determinism lint, the positional-access guard, the inert-carrier property and the replay harness with scenarios S1 and S2 were extracted from this document and executed from a scratch directory outside the repo: the lint reports exactly the four known hazards, the guard flags the current tree (the red-before state), S1 and S2 replay cleanly with input parity, and the property reads 0 differences at lv6/lv7 and more than 0 at lv5. The review's line numbers are stale (13 remediation tranches landed), so every claim below is located by symbol and re-verified.

## Context

The forcing cascade (`apply_post_reason_cascade` in `application/worker/workflows/forcing.py`) is a pure, ordered set of eleven gathering rules driven from `RunInvestigationWorkflow._run_inner`. Three tranches already landed on it: F2 (lv6, work-order carriers are not turns), F4 (lv7, bounded non-destructive pivot) and F8 (typed `Force` flags, the ordered `RULES` table and the stand-down contract, no pin). Those tranches left five groups of follow-ups, which are this document:

- **F9 (observability).** The lists that describe what the cascade did are hand-written in six places and have drifted from the `Force` enum. Worse, the conclude-time telemetry stamp silently drops most counters, which makes two shipped, documented measurements (the lv7 pivot counter and the lv4 D6 firing rate) unmeasurable.
- **F10 (replay posture).** There is no Temporal replay test anywhere in the repo, no stated versioning posture, a latent uuid4 fallback, wall-clock defaults in passed-through domain modules, and (new, found while designing) a real replay hazard in how an unmarked loop pin is resolved.
- **F8 follow-ups.** The cascade is table-guarded but not one loop over `RULES`; a new rule still needs a `RULES` entry and a `_down` call.
- **F2 follow-ups.** Nothing structurally prevents a future positional reader of `action_history` from reading a carrier as a turn.
- **F4 follow-ups.** The pivot cap of two is unmeasured, a scoped `SEARCH_CODE` never pivots from lv7, and the pending-REQUERY-versus-pivot precedence was decided by the owner (recorded here as Decided, not redesigned).

Repo conventions this design obeys (restated because they shape every decision): loop-semantics changes pin on the entity through `loop_version_at_least` in `domain/investigation/loop_version.py`, never through environment switches and never through `workflow.patched`; IAP is not live, so no legacy branches or compatibility shims; tests are written first and shown red before the fix; one tranche per owner message; no commits, live runs, restarts, tenant cleans or migrations without owner approval; scratch and operator directories are never referenced from repo artifacts; no analyzed repository is named (synthetic names such as `SampleOrderFilter` and `OrderServiceHandler` only).

### Verdict summary

| Item | Verdict | Severity | Pin | Test cases |
|---|---|---|---|---|
| F9 observability | Open (follow-up of IMPLEMENTED F8); worse than the review stated | should-fix (raised from consider: it blocks measuring F4 and H5) | `LV_F` for one shadow-only mapping; none for the rest | 18 |
| F10 replay posture | Open; one new hazard found (unmarked pin floats to current) | should-fix (raised from consider) | none (posture, latch, lint, tests) | 21 |
| F8 follow-ups | Open (follow-up of IMPLEMENTED F8); D14 now, D15-D17 recommended behind a golden master | consider | none (behavior-preserving) | 15 |
| F2 follow-ups | Open (follow-up of IMPLEMENTED F2); (d) accepted by design with a sunset rule | consider | none | 13 |
| F4 follow-ups | Open (measurement); (f) Decided; scoped-search question measured, not changed; live measurement Gated (T6 battery, owner-deferred) | consider | measurement none; any cap or scoped-search change is `LV_F` | 17 |

### Review claims found wrong, stale or incomplete

- **Stale line numbers** throughout (`_reason_summary` is `forcing.py:84`, the review said `:89-95`; the shadow list is in `run_investigation.py` near `_shadow_turn["forced"]`, the review said `:912`).
- **"`inputs.py:307-312` silently drops several counters" understates it.** `_derive_loop_telemetry` drops ten workflow keys: `forced_pivot_count`, all five `forced_smallest_fetch_<kind>_count`, `forced_absence_disconfirmation_count`, `d6_blocked_no_evidence_count`, `fetches_dispatched`, `defer_repairs_used`, `defer_repairs_limit` (plus `plan`). `forced_floor_count` and `forced_callsite_count` are not even sent by the workflow. Probe: feeding every workflow key through `_derive_loop_telemetry` returned only `forced_formulate_count`, `forced_reread_count`, `forced_log_search_count`, `forced_walk_stack_count`, `forced_replan_count`, `forced_synthesis_count`, `overconfident_turns`, `iterations` and the non-forced keys.
- **Shipped architecture text is false.** Section 70 says the pivot count lives in `metadata["loop_telemetry"]` as `forced_pivot_count`, and section 67 says `d6_blocked_no_evidence_count` and `fetches_dispatched` are stamped "so the gate's firing rate is measurable". Neither is persisted. The only test (`test_h5_d6_evidence_cap.py`) asserts the substring appears in the workflow source, not that the stamp survives.
- **The review did not notice the debug flag list misses `forced_requery`** and that `_reason_summary` renders five of twelve `Force` members (probe: missing `absence_disconfirmation`, `callsite`, `floor`, `not_checkable`, `pivot_recollect`, `requery`, `smallest_fetch`). The shadow list misses `pivot_recollect` and `requery`.
- **`forcing_used["synthesis"] = forced_formulate_count` is a stale mirror, not a deliberate shared cap.** The shadow docstring says formulate and synthesis share a combined cap; they do not (the cascade keeps `formulate` and `synthesis` counters separately, each capped at 2). Probe: after a synthesis fire the cascade returns `counts == {'synthesis': 1}` while `forced_formulate_count` stays 0, so the shadow reports zero uses of both rules.
- **"The `uuid4` fallback is unreachable and the sandbox would reject it" is right**, and `WorkOrder`'s `uuid4` default is never executed on a production path (every construction site pins the id; AST probe). The wall-clock default is not latent: `FrontierCandidate.seeded_at` executes `datetime.now` inside a sandboxed replay on every turn where a pivot fires (probe: one hit, no sandbox error, because the module is passed through).
- **`retrieval_ledger.py:277` reading `os.environ` is not reachable from workflow code** (a reachability lint over the workflow's imports finds no env read; the two env readers, `_max_attempts_from_env` and `resolve_definition_mode`, are activity-side).
- **New, not in the review (F10):** the workflow resolves an unmarked pin with `input_data.loop_version or LOOP_VERSION_CURRENT` in three places. The bulk-intake child path constructs `RunInvestigationInput` without a pin. A history started with `None` therefore replays under whatever `LOOP_VERSION_CURRENT` is at replay time, so the next version bump changes the semantics (and possibly the command sequence) of in-flight runs.
- **New (F10):** the SDK `Replayer` does not compare activity inputs. Probe: a recorded `execute_action_activity` input was tampered and replay still passed. A replay test that only calls `replay_workflow` cannot detect a cascade change that dispatches a different file with the same activity sequence; an input-parity interceptor is mandatory.

## References

- `docs/REVIEW-Sonnet-v0.1.md` (remediation table, rows 11-13) and `docs/REVIEW-Sonnet-v0.1-part2.md` sections 6.3 (F8-F10), 6.4 (guard completeness), 6.5 (replay-safety stress test), 7 (work orders).
- `docs/IAP_Arch_Eng_Recommendations.log` — entries dated 2026-10-30/31 for F2, F4, F8 (follow-ups a-e), F9-F10 and the requery-carrier replay-validation entry.
- `docs/IAP-architecture-v1.0.md` sections 66-71 — the lv2-lv7 remediation record; section 69 (F2), 70 (F4) and 71 (F8) are the baselines this document extends.
- `docs/design/README.md` and `docs/design/DES-reason-workorders-v1.0.md` — house conventions (no legacy branches, version-pinned semantics, no customer identifiers).
- `src/investigation_agent_platform/domain/investigation/loop_version.py` — the pin contract (`resolve_loop_version`, `loop_version_at_least`, `KNOWN_LOOP_VERSIONS`).
- Temporal Python SDK 1.33.0 (installed) verified against the SDK source and Context7 (`/temporalio/sdk-python`): `temporalio.worker.Replayer(workflows=..., interceptors=...)`, `replay_workflow(history, raise_on_replay_failure=True)`, `temporalio.client.WorkflowHistory(workflow_id, events)` with `from_json`/`to_json`, `temporalio.worker.Interceptor.workflow_interceptor_class` with `WorkflowOutboundInterceptor.start_activity`, `workflow.patched(id)` and `workflow.deprecate_patch(id)`, `VersioningBehavior.PINNED/AUTO_UPGRADE` and `WorkerDeploymentConfig` for worker versioning, and `WorkflowEnvironment.start_time_skipping/start_local` (both download a server binary, so they are not offline).
- `tests/unit/test_f8_forcing_rules.py` (83), `test_f2_carrier_separation.py` (30), `test_f4_pivot_bounds.py` (34), `test_cascade_seam.py`, `test_loop_exit_contract.py` — the existing pins. Baseline run for this design: 170 passed in 0.5 s across these five files.

## Goals / Non-Goals

**Goals:**

- Derive every observability list (timeline summary, debug flag line, history stamp, shadow `forced` list, telemetry allowlist, cascade counters) from one definition (`Force`, `RULES`, one counter map) so adding a rule cannot leave a list behind.
- Make the conclude-time telemetry stamp lossless for every counter the workflow keeps, with a contract test that fails when the workflow sends a key the stamp would drop.
- Fix the shadow `forcing_used["synthesis"]` mapping behind a loop-version pin.
- State the repo's versioning posture explicitly (loop pin plus replay corpus, `workflow.patched` not used before go-live) and enforce it with tests.
- Build a real, offline Temporal replay strategy: scripted activities, history synthesis, the SDK `Replayer` in the real sandbox, an input-parity interceptor, committed synthetic transcripts, and scenarios covering carrier append and in-place consumed stamps.
- Close the unmarked-pin replay hazard, delete the `uuid4` fallback, and remove wall-clock and random defaults from workflow-reachable domain models, with a static determinism lint over everything the workflow can reach.
- Decide F8's trigger/apply protocol (recommended: do it, behind a golden-master differential harness) and add a completeness contract that closes "a new rule needs a table entry and a `_down` call" immediately.
- Add a structural guard against positional readers of `action_history` (typed turn view, AST guard, metamorphic property).
- Instrument the pivot (cap saturation, scoped-search skips, productivity) and write the decision rule for changing the cap.

**Non-Goals:**

- Re-deciding the pending-REQUERY-versus-pivot precedence (Decided 2026-10-31, recorded below; only a regression pin is added).
- W1/W2/W3/W4 and W5/W6 (advisory-layer deletion, requery TTL, outcome-blind coverage, dead surface). F10-D9 and F10-D10 note where W1 deletion would dissolve them; the W decisions are not made here.
- Changing any forcing rule's trigger semantics, caps or precedence. Every F9, F10, F8 and F2 decision is behavior-preserving at the dispatch level; the only semantic change is the `LV_F`-pinned shadow mapping.
- Live runs, tenant cleans, migrations, deploys, restarts. Items that need them are marked "requires owner approval".
- Committing real Temporal histories. Real histories carry evidence payloads from analyzed repositories; committed fixtures are synthetic scripted transcripts only.
- Worker Versioning (`PINNED` deployments) or `workflow.patched` adoption before go-live (decided in D7, revisited at the go-live gate).

### Recommended tranche order (one tranche per owner message)

1. **F10-A** (static lint, `uuid4` fallback deletion, deterministic defaults, pin latch) and **F10-B** (replay harness and committed transcripts). Everything else in this group refactors workflow code; the replay corpus must exist first so those refactors are proven command-neutral.
2. **F9** (leaf module, counter map, lossless telemetry, typed lists, `LV_F` shadow mapping).
3. **F8** (D14 completeness contract in the same tranche as F9; D15-D17 protocol loop as its own tranche behind the golden master).
4. **F2** (typed view, positional-access guard, metamorphic property).
5. **F4** (measurement counters; the cap decision happens only after data exists).

## Decisions

| # | Item | One-line decision | Pin |
|---|---|---|---|
| D1 | F9 | Move `Force`, `Rule`, `RULES`, `check_rule_contract` to a pure leaf module `domain/investigation/forcing_rules.py` and derive labels, counters and telemetry keys there | none |
| D2 | F9 | One counter map (`COUNTER_KEYS`) replaces 17 workflow locals and two hand-copied dict blocks | none |
| D3 | F9 | Lossless telemetry allowlist derived from the counter map plus a documented scalar list, with a workflow-literal contract test and a dropped-key log | none |
| D4 | F9 | Every observability list derives from `Force` (`label`, `forced_stamp`, `shadow_forced`) and the formulate rule writes its own name | none |
| D5 | F9 | Shadow `forcing_used` maps each rule to its own counter | `LV_F` |
| D6 | F9 | `HistoryKey` and `ForcePayload` enums for keys that cross modules | none |
| D7 | F10 | Versioning posture: loop pin plus replay corpus; `workflow.patched` not used before go-live | none |
| D8 | F10 | Latch the loop pin in history through the create-activity result; no `CURRENT` fallback inside workflow code | none |
| D9 | F10 | Delete the `_order_id` `uuid4` fallback | none |
| D10 | F10 | Deterministic content-addressed ids and no wall-clock default in `WorkOrder` and `FrontierCandidate` | none |
| D11 | F10 | Static determinism lint over everything the workflow can reach | none |
| D12 | F10 | Offline replay harness: scripted activities, history synthesis, sandboxed `Replayer`, input-parity interceptor | none |
| D13 | F10 | Scenario catalog and corpus policy (transcripts, regeneration, pin variants) | none |
| D14 | F8 | Cascade completeness contract (AST): every table rule has exactly one guarded block that writes its force and counter | none |
| D15 | F8 | Trigger/apply protocol: `CascadeContext`, `Fire`, `TRIGGERS`, one loop over `RULES` (recommended: do it) | none |
| D16 | F8 | Golden-master differential harness, recorded before D15 | none |
| D17 | F8 | `Rule.cancels` generalizes the F5 walk-stack-over-pivot cancel and its counter refund | none |
| D18 | F2 | `Turns` typed view and a single pin site, `turn_view`, replacing the `separate_carriers` flag | none |
| D19 | F2 | The workflow stamps its just-appended turn through a reference, not `action_history[-1]` | none |
| D20 | F2 | AST positional-access guard with a reasoned allowlist | none |
| D21 | F2 | Metamorphic inert-carrier property test; lv5 sunset rule | none |
| D22 | F4 | `PivotDecision` with a reason; measurement counters `pivot_capped`, `pivot_scoped_skipped`, `pivot_productive` | none |
| D23 | F4 | Pure cap-verdict evaluator and the quantitative decision rule | none |
| D24 | F4 | Conditional `pivot_cap_for(loop_version)` and scoped-search follow-up (not enabled until the rule fires) | `LV_F` (conditional) |
| D25 | F4 | Decided: a pending REQUERY order does not outrank a genuine pivot; add regression pins | none |

## Items

### Item F9: Forced-flag observability, lossless telemetry and one counter map

#### Status

**Open — follow-up of IMPLEMENTED F8** (F8 landed the `Force` enum and `RULES` table that make this derivable). Severity: **should-fix** (raised from the review's consider tier, because the telemetry loss makes the lv7 pivot counter and the lv4 D6 firing rate unmeasurable and makes shipped architecture text false). Dependencies: F8 (landed); F10-B (the replay corpus) should land first because D2 rewrites workflow counter plumbing and must be proven command-neutral. Gating: none. The only semantic change is D5, pinned `LV_F`.

#### Verified problem

Probed offline against the real code (`forcing.py`, `run_investigation.py`, `inputs.py`):

| List | Where | Today (probe result) | Gap |
|---|---|---|---|
| Timeline summary flags | `forcing.py::_reason_summary` | renders `formulate, reread, log_search, walk_stack, replan` | missing `absence_disconfirmation`, `callsite`, `floor`, `not_checkable`, `pivot_recollect`, `requery`, `smallest_fetch` (7 of 12) |
| Debug flag line | `run_investigation.py`, the `_forced_flags = sorted(...)` tuple | 11 of 12 | missing `forced_requery` |
| History stamp | `run_investigation.py`, the `action_history[-1].update({...})` literal | all 12 `Force` values plus two payload keys | correct today, but hand-listed |
| Shadow `forced` list | `run_investigation.py`, `_shadow_turn["forced"]` | 10 short names plus a `synthesis` heuristic | missing `pivot_recollect`, `requery`; `synthesis` inferred by recomputing `_should_force_formulate` |
| Cascade counters | workflow locals plus two hand-copied dicts around `apply_post_reason_cascade` | 14 keys in, 14 keys out | duplicated in three places; `requery` and `absence` live elsewhere |
| Telemetry stamp | `inputs.py::_derive_loop_telemetry` literal tuple | 12 allowlisted numeric keys | drops 10 workflow keys (see Context); `floor` and `callsite` counters are never sent |
| Shadow `forcing_used` | `run_investigation.py`, the `build_shadow_snapshot(forcing_used={...})` call | `"synthesis": forced_formulate_count` | wrong counter; probe shows the shadow undercounts synthesis fires |

Evidence for the telemetry loss: `_derive_loop_telemetry({k: 1 for k in <every workflow loop_telemetry key>}, {}, 0)` returns `forced_replan_count`, `iterations`, `overconfident_turns` and the hypothesis/binding keys only for the keys it knows; `forced_pivot_count`, `forced_smallest_fetch_*_count`, `forced_absence_disconfirmation_count`, `d6_blocked_no_evidence_count`, `fetches_dispatched` and `defer_repairs_*` are absent from the result. `telemetry_omission_flagged` only fires when the whole derivation returns `None`, so a per-key drop is invisible.

#### Design

##### D1: A pure leaf module owns the forcing vocabulary

**Decision:** Move `Force`, `Rule`, `RULES`, `PRE_CASCADE_FORCES` and `check_rule_contract` out of `workflows/forcing.py` into `domain/investigation/forcing_rules.py`, and derive labels, counter keys and telemetry keys there. The move is required, not cosmetic: `_derive_loop_telemetry` lives in `activities/inputs.py`, and importing `workflows.forcing` from the activities package is circular (`workflows/__init__` imports `run_investigation`, which imports `activities`, which is mid-initialization). The leaf is stdlib-only, so the workflow imports it through `imports_passed_through()` exactly as it already imports `loop_version`, and enum identity is shared across sandbox instances.

```python
# src/investigation_agent_platform/domain/investigation/forcing_rules.py
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
    """Data keys a rule stamps beside its flag: strings, never flags (F8 allowed these two)."""

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


# RULES and check_rule_contract move here verbatim from forcing.py (eleven rules, the three
# documented exemptions, the contract checker). The only table edit is the smallest-fetch rule:
#
#     Rule("smallest_fetch", Force.SMALLEST_FETCH, frozenset({...unchanged...}),
#          counters=tuple(f"smallest_fetch_{kind.lower()}" for kind in SMALLEST_FETCH_KINDS)),
```

**Alternative considered:** import `workflows.forcing` lazily inside `_derive_loop_telemetry`. It works at call time, but it makes activity code depend on the workflow package and keeps the vocabulary in a 3,000-line module. Rejected: layering inversion and no shared home for the keys. **Alternative considered:** have the workflow send the allowlist to the conclude activity in the payload. Rejected: the activity would trust a caller-supplied key list, and the contract test could not see it.

**Trade-offs:** a one-time import churn in tests (`from ...forcing import Force, RULES, ...` becomes `from ...forcing_rules import ...`). No alias is left behind in `forcing.py` (no compatibility shims); tests are updated in the same change.

---

##### D2: One counter map

**Decision:** The 17 hand-written workflow locals (`forced_walk_stack_count`, ..., `forced_smallest_fetch_causal_count`, `forced_absence_disconfirmation_count`) and the two hand-copied dict blocks around the cascade call become one `counters: dict[str, int]`, whose keys come from `COUNTER_KEYS`. Telemetry names are regular: `forced_<key>_count`, which reproduces every existing name byte-for-byte (`forced_pivot_count`, `forced_smallest_fetch_definition_count`, `forced_absence_disconfirmation_count`, ...) and adds the three that were never recorded (`forced_floor_count`, `forced_callsite_count`, `forced_requery_count`). The cascade does not count `requery` (it has no cap by design); the workflow increments it where it already stamps the consumption.

```python
# src/investigation_agent_platform/domain/investigation/forcing_rules.py  (continued)

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
```

```python
# src/investigation_agent_platform/application/worker/workflows/run_investigation.py  (after)
from investigation_agent_platform.domain.investigation.forcing_rules import (  # imports_passed_through block
    counter_telemetry, forced_stamp, fired_labels, merge_counts, new_counters, payload_stamp,
    shadow_forced, shadow_forcing_used,
)

counters = new_counters()                      # replaces forced_*_count locals

# pre-cascade absence step
if _absence_target is not None:
    counters["absence_disconfirmation"] += 1

_cascade_state = apply_post_reason_cascade(
    PostReasonCascadeState(..., counts=dict(counters), ...)   # the 14-line dict literal is gone
)
merge_counts(counters, _cascade_state.counts)                 # the 17-line unpack is gone

# requery consumption stamp (existing site)
if _reason_data.get(Force.REQUERY.value) and isinstance(_consumed_order_ref, dict):
    _consumed_order_ref["consumed"] = True
    counters["requery"] += 1
    ...

# conclude payload
loop_telemetry={
    **counter_telemetry(counters),
    "llm_tokens_total": llm_tokens_total,
    ...  # scalars exactly as today
}
```

**Alternative considered:** a `LoopCounters` dataclass with typed attributes. Rejected: the cascade already takes and returns a `dict[str, int]` (`PostReasonCascadeState.counts`), the telemetry is a flat dict, and a dataclass would need the same key list for conversion. **Trade-offs:** the source-text pin `test_f4_pivot_bounds.py::test_the_workflow_maps_the_pivot_counter_back_from_the_cascade` (three substring assertions on the old plumbing) is rewritten to assert behavior (F9-T17) instead of text; the substring pins in `test_h5_d6_evidence_cap.py` and the requery-stamp wiring pin in `test_f8_forcing_rules.py` are re-pointed at the new names. `merge_counts` is per-key fail-open where the old unpack was all-or-nothing on the first malformed value; for well-formed values the result is identical.

---

##### D3: A lossless telemetry stamp with a contract test

**Decision:** `_derive_loop_telemetry` builds its numeric allowlist from `LOOP_TELEMETRY_SCALARS` plus `{telemetry_key(k) for k in COUNTER_KEYS}`. The scalars that were dropped silently (`d6_blocked_no_evidence_count`, `fetches_dispatched`, `defer_repairs_used`, `defer_repairs_limit`) join the list. `plan` (a list, not a number) is declared not stamped rather than silently dropped. A contract test parses the workflow's `loop_telemetry={...}` literal and fails if any literal key is neither allowlisted nor listed as handled by a dedicated block, so a future key cannot be dropped silently again. An unknown key at stamp time logs once, naming keys only.

```python
# src/investigation_agent_platform/domain/investigation/forcing_rules.py  (continued)

#: Numeric workflow scalars stamped into metadata["loop_telemetry"] (not forcing counters).
LOOP_TELEMETRY_SCALARS: tuple[str, ...] = (
    "llm_tokens_total",
    "llm_cost_total",
    "formulated_count",
    "evidence_banked_total",
    "overconfident_turns",
    "iterations",
    "d6_blocked_no_evidence_count",  # lv4 D6 cap firing rate (was dropped)
    "fetches_dispatched",  # lv4 (was dropped)
    "defer_repairs_used",  # was dropped
    "defer_repairs_limit",  # was dropped
)

#: Workflow keys stamped by their own validated block in _derive_loop_telemetry (shape-checked
#: there, so they are not in the numeric allowlist).
TELEMETRY_HANDLED_ELSEWHERE: frozenset[str] = frozenset(
    {
        "triage_route",
        "claim_binding_kept", "claim_binding_evaporated", "claim_binding_unmapped",
        "claim_binding_repaired", "claim_binding_repair_shadow", "claim_binding_version",
        "shadow_trace", "shadow_summary",
        "definition_shadow_trace", "shadow_definition_summary",
    }
)

#: Workflow keys deliberately not stamped. ``plan`` rides the reason history; the E1 comment in
#: the workflow says it also rides telemetry, which it never did (owner decision O-F9-2).
TELEMETRY_DECLARED_NOT_STAMPED: Mapping[str, str] = {"plan": "rides action history, not telemetry"}


def telemetry_allowlist() -> frozenset[str]:
    return frozenset(LOOP_TELEMETRY_SCALARS) | {telemetry_key(key) for key in COUNTER_KEYS}
```

```python
# src/investigation_agent_platform/application/worker/activities/inputs.py  (edit to _derive_loop_telemetry)
from investigation_agent_platform.domain.investigation.forcing_rules import (
    TELEMETRY_DECLARED_NOT_STAMPED,
    TELEMETRY_HANDLED_ELSEWHERE,
    telemetry_allowlist,
)

_TELEMETRY_NUMERIC_KEYS = tuple(sorted(telemetry_allowlist()))
...
        telemetry: dict[str, Any] = {}
        for key in _TELEMETRY_NUMERIC_KEYS:          # replaces the 12-name literal tuple
            try:
                value = passed.get(key)
            except Exception:
                continue
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float)):
                telemetry[key] = value
        # Visible, bounded drop accounting: names only, never values.
        try:
            unknown = sorted(
                str(k)
                for k in passed
                if k not in telemetry_allowlist()
                and k not in TELEMETRY_HANDLED_ELSEWHERE
                and k not in TELEMETRY_DECLARED_NOT_STAMPED
            )
            if unknown:
                logger.warning(
                    "loop telemetry dropped unrecognized keys: %s",
                    ",".join(unknown[:20]),
                    extra={"event": "loop telemetry key dropped", "dropped_key_count": len(unknown)},
                )
        except Exception:
            pass
```

```python
# tests/unit/test_f9_observability.py (the contract test, F9-T13)
import ast
import pathlib

from investigation_agent_platform.domain.investigation.forcing_rules import (
    TELEMETRY_DECLARED_NOT_STAMPED,
    TELEMETRY_HANDLED_ELSEWHERE,
    telemetry_allowlist,
)

WORKFLOW = pathlib.Path("src/investigation_agent_platform/application/worker/workflows/run_investigation.py")


def _loop_telemetry_literal_keys() -> set[str]:
    tree = ast.parse(WORKFLOW.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "loop_telemetry" and isinstance(node.value, ast.Dict):
            return {k.value for k in node.value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    raise AssertionError("loop_telemetry literal not found")


def test_every_literal_workflow_telemetry_key_is_stamped_or_declared() -> None:
    literal = _loop_telemetry_literal_keys()
    known = telemetry_allowlist() | TELEMETRY_HANDLED_ELSEWHERE | set(TELEMETRY_DECLARED_NOT_STAMPED)
    assert literal <= known, f"workflow sends keys the stamp would drop: {sorted(literal - known)}"
```

**Alternative considered:** stamp every key in `passed` unconditionally. Rejected: the stamp is a shape-checked, size-capped metadata write; unknown payload content must not reach the row. **Alternative considered:** make `telemetry_omission_flagged` per-key. Rejected: it is the C5 Path A whole-derivation flag with its own tests; the new log plus the contract test cover the per-key case without changing it.

**Trade-offs:** `plan` stays unstamped (declared); stamping a bounded `plan` is owner decision O-F9-2. Older investigation rows lack the new keys; absence means "not recorded", never zero (D3 does not backfill, and F9-T18 pins that the derivation does not zero-fill).

---

##### D4: Every observability list derives from `Force`

**Decision:** Four helpers in the leaf replace four hand lists. `_reason_summary`, the debug story line, the history stamp and the shadow list all iterate `Force`. The formulate and synthesis rules share one force (F8 decision), so the cascade writes the rule's own name into `ForcePayload.FORMULATE_RULE`, and the shadow list reads it instead of recomputing `_should_force_formulate` against the post-cascade data.

```python
# src/investigation_agent_platform/domain/investigation/forcing_rules.py  (continued)

def fired_labels(reason_data: Mapping[str, Any]) -> list[str]:
    """Labels of every force set on the data, in ``Force`` declaration order (pure, never raises)."""
    try:
        return [force.label for force in Force if reason_data.get(force.value)]
    except Exception:
        return []


def forced_stamp(reason_data: Mapping[str, Any]) -> dict[str, bool]:
    """The history-entry flag block: one bool per ``Force`` member (stored keys unchanged)."""
    return {force.value: bool(reason_data.get(force.value, False)) for force in Force}


def payload_stamp(reason_data: Mapping[str, Any]) -> dict[str, str]:
    """The history-entry payload block: one string per ``ForcePayload`` member."""
    return {p.value: str(reason_data.get(p.value, "") or "") for p in ForcePayload}


def shadow_forced(reason_data: Mapping[str, Any]) -> list[str]:
    """The shadow trace ``forced`` list: every fired label, plus ``synthesis`` when it wrote FORMULATE.

    Synthesis shares the FORMULATE force, so a synthesis fire lists both ``formulate`` and
    ``synthesis`` (the stored shape before this change).
    """
    labels = set(fired_labels(reason_data))
    if (
        reason_data.get(Force.FORMULATE.value)
        and reason_data.get(ForcePayload.FORMULATE_RULE.value) == "synthesis"
    ):
        labels.add("synthesis")
    return sorted(labels)
```

```python
# src/investigation_agent_platform/application/worker/workflows/forcing.py  (edits)
def _reason_summary(reason_data: dict[str, Any]) -> str:
    ...
    forced_bits = fired_labels(reason_data) if isinstance(reason_data, dict) else []   # was a 5-key loop
    forced = f" forced=[{','.join(forced_bits)}]" if forced_bits else ""
    ...

# in apply_post_reason_cascade: the two FORMULATE-writing blocks each add one line
_reason_data[ForcePayload.FORMULATE_RULE.value] = "formulate"   # in the formulate block
_reason_data[ForcePayload.FORMULATE_RULE.value] = "synthesis"   # in the synthesis block
```

```python
# src/investigation_agent_platform/application/worker/workflows/run_investigation.py  (edits)
workflow.logger.debug(
    "story.forcing_outcome iteration=%s proposal=%s final=%s forced=%s defer=%s",
    iteration, ..., ",".join(fired_labels(_reason_data)) or "none", bool(_is_defer),
)                                                    # the 11-name sorted tuple is gone
_shadow_turn["forced"] = shadow_forced(_reason_data)  # the 10-pair tuple and the synthesis heuristic are gone
action_history[-1].update({..., **forced_stamp(_reason_data), **payload_stamp(_reason_data), ...})
```

**Alternative considered:** give synthesis its own `Force` member. Rejected: it would add a stored history key (`forced_synthesis`) and change every stand-down set that currently treats formulate and synthesis as one override (F8 decision, 83 tests). **Alternative considered:** keep the owed-recompute heuristic. Rejected: it recomputes a trigger against post-cascade data to infer which rule fired; the payload key states it.

**Trade-offs:** timeline summaries render forced flags in enum declaration order instead of the old fixed five-name order; `scripts/agent_metrics.py::parse_reason_trace` splits the bracket on commas and accepts any token, so it needs no change (F9-T02 pins the format). The shadow `forced` list gains `pivot_recollect` and `requery` members; the frozen G6 protocol already classifies loop rules outside the shadow's vocabulary as "unmodeled-loop-forcing", so new members are classified, not errors (the frozen protocol document is not edited; see Documentation Plan).

---

##### D5: The shadow maps each rule to its own counter (pinned `LV_F`)

**Decision:** `forcing_used["synthesis"]` is `counters["synthesis"]` from `LV_F`. Older pins keep the mirror so G6 run-sets recorded under older pins stay comparable. This is the only semantic change in F9, and it affects the compute-only shadow decision, never loop dispatch.

```python
# src/investigation_agent_platform/domain/investigation/forcing_rules.py  (continued)
def shadow_forcing_used(counters: Mapping[str, Any], *, synthesis_separate: bool) -> dict[str, int]:
    """Per-rule used counts for the shadow snapshot (pure). Unknown or malformed values read as 0."""

    def used(key: str) -> int:
        value = counters.get(key, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    return {
        "walk_stack": used("walk_stack"),
        "floor": used("floor"),
        "formulate": used("formulate"),
        "synthesis": used("synthesis") if synthesis_separate else used("formulate"),
        "reread": used("reread"),
        "callsite": used("callsite"),
        "smallest_fetch": sum(used(f"smallest_fetch_{kind.lower()}") for kind in SMALLEST_FETCH_KINDS),
    }
```

```python
# src/investigation_agent_platform/application/worker/workflows/forcing.py  (new, beside _carriers_separated)
def _synthesis_counted_separately(loop_version: Any) -> bool:
    """Whether the shadow reads synthesis fires from their own counter (LV_F+), fail-safe False."""
    try:
        return bool(loop_version_at_least({"loop_version": loop_version}, LOOP_VERSION_LV_F))
    except Exception:
        return False
```

```python
# src/investigation_agent_platform/application/worker/workflows/run_investigation.py  (edit)
forcing_used=shadow_forcing_used(counters, synthesis_separate=_synthesis_counted_separately(loop_version)),
```

**Alternative considered:** fix the mapping with no pin because the shadow is compute-only. Rejected: shadow records are stored in telemetry and G6 compares run-sets that must be definition-stable; a mid-stream mapping change would silently shift the divergence rate. The pin costs one boolean and keeps older rows reproducible. Owner decision O-F9-1 may overrule (no pin, ledger note instead). **Trade-offs:** the shadow stays "wrong" for runs pinned lv7 or older by design.

---

##### D6: `HistoryKey` for keys that cross modules

**Decision:** Four keys are written by the workflow and read by other modules as raw strings: `requery_consumed` (`retrieval_ledger.directive_consumed`), `forced_smallest_fetch_gap_id` (`forcing._smallest_fetch_already_covered`, already a `ForcePayload` member after D1), `kind` (carrier discrimination) and `consumed`. They get one definition; the F8 raw-string test extends to the whole `workflows/` package plus `retrieval_ledger.py`.

```python
# src/investigation_agent_platform/domain/investigation/forcing_rules.py  (continued)
class HistoryKey(StrEnum):
    """Action-history keys read outside the module that writes them."""

    KIND = "kind"  # carriers only; a real turn never has it (pinned by test)
    CONSUMED = "consumed"
    REQUERY_CONSUMED = "requery_consumed"
```

**Alternative considered:** a `TypedDict` for the whole turn entry. Rejected: the entry carries 30-odd keys, mixes workflow-written and activity-derived fields, and the mypy ratchet baseline is 506; a partial typed view buys less than an enum for the four shared keys. **Trade-offs:** `retrieval_ledger.py` (an `application` module that the workflow imports through pass-through) gains a `domain` import, which the layering already permits.

#### Version pinning / migration

- **D1-D4, D6: no pin needed.** They change no decision, no command sequence and no stored shape (stored keys are byte-identical; new telemetry keys and two new shadow labels are additive, per the N21 additive-key rule). Evidence: F9-T04 pins the history key set, F9-T12 pins that every old telemetry name is preserved, and the F10 replay corpus proves the workflow command sequence is unchanged.
- **D5: pinned `LV_F`.** Routed with `loop_version_at_least` through `_synthesis_counted_separately`; an unrecognized or unreadable pin keeps the old mirror (fail-safe). `domain/investigation/loop_version.py` gains `LOOP_VERSION_LV_F`, the entry in `KNOWN_LOOP_VERSIONS` and `_LOOP_VERSION_ORDER`, and a docstring paragraph: "lvF: lv7 plus per-rule shadow forcing counts (review F9). The shadow snapshot reads synthesis fires from the synthesis counter instead of mirroring the formulate counter; compute-only, it cannot change a dispatch." `LOOP_VERSION_CURRENT` moves to `LOOP_VERSION_LV_F`.
- **Migration:** none (no schema change; `metadata` is a JSON column). Existing rows keep their keys; new keys appear only on runs concluded after the change.

#### Test cases

All written first and shown red on the pre-change tree (F9-T01, T03, T05, T06, T07, T10, T11, T12, T13, T16, T17 fail or error on import today; T04, T08, T09, T14, T15, T18 are regression pins that pass before and after). Test file: `tests/unit/test_f9_observability.py`; the workflow-harness case reuses the F10 harness.

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| F9-T01 | Summary renders every force | unit | one reason dict per `Force` member with only that flag set | `_reason_summary(d)` | the summary contains `forced=[<label>]` for all 12 members | Red today for 7 members. Negative control: drop `Force.CALLSITE` from the iteration source (or restore the 5-key tuple) and this fails for the removed member |
| F9-T02 | Summary order and format stable | unit | reason dict with `forced_replan`, `forced_formulate` and `forced_pivot_recollect` set | `_reason_summary` then `scripts/agent_metrics.parse_reason_trace` on the text | labels in enum order; `parse_reason_trace(...)["forced"]` returns the same list | Pins the consumer contract so a reorder cannot break the metrics script |
| F9-T03 | No raw force strings in the workflow | unit (AST) | parse `run_investigation.py` and `forcing.py` | collect string literals starting with `forced_` outside `ForcePayload`/enum definitions | none (only enum members and `ForcePayload` are used) | Extends F8's "no raw guard" test across both modules. Negative control: add `"forced_floor"` as a literal in a copy and assert the check flags it |
| F9-T04 | History stamp shape pinned | unit | the set of 12 stored flag keys copied into the test from the pre-change source | `forced_stamp({})` and `forced_stamp` with each flag set | exactly those 12 keys, all bool; `payload_stamp` has the three payload keys as strings | Regression pin on the stored shape (passes before and after) |
| F9-T05 | Shadow list covers pivot and requery | unit | reason dicts with `forced_pivot_recollect`, `forced_requery`, and with `forced_formulate` plus payload `synthesis` | `shadow_forced(d)` | `["pivot_recollect"]`, `["requery"]`, and `["formulate", "synthesis"]` | The synthesis case equals the pre-change stored shape; old ten labels unchanged (loop over them) |
| F9-T06 | Cascade writes the formulate rule name | unit | cascade state that fires formulate; another that fires synthesis (8 rows, 2 actions, nothing formulated) | `apply_post_reason_cascade` | `forced_formulate_rule == "formulate"` and `"synthesis"` respectively; flag `forced_formulate` True in both | Probe-grounded: synthesis case yields `counts == {'synthesis': 1}` |
| F9-T07 | Shadow reads synthesis from its own counter (LV_F) | unit | `counters = {"formulate": 0, "synthesis": 2}` | `shadow_forcing_used(counters, synthesis_separate=True)` | `synthesis == 2`, `formulate == 0` | Negative control: revert to `used("formulate")` and the assertion fails |
| F9-T08 | Older pin keeps the mirror | unit | same counters; pins `lv7`, `lv6`, `lv5` | `_synthesis_counted_separately(pin)` then `shadow_forcing_used` | `False`; `synthesis == formulate == 0` | Version-pin routing: an older pin keeps the old behavior |
| F9-T09 | Malformed pin fails safe | unit | pins `None`, `""`, `"lv999"`, `42` | `_synthesis_counted_separately` | `False` for every value, never raises | Fail-safe (old mirror) rather than fail-closed: shadow is compute-only |
| F9-T10 | Counter keys derive from the table | unit | the real `RULES` | `counter_keys()` | exactly the 16 keys: `pivot, walk_stack, requery, floor, formulate, synthesis, reread, callsite, log_search, replan`, five `smallest_fetch_<kind>`, `absence_disconfirmation`; every `telemetry_key` for a pre-existing counter equals its old literal name | Negative control: append a `Rule("x", ...)` to a copy of `RULES` and assert its key appears (proves derivation, not a literal) |
| F9-T11 | Cascade counts stay inside the map | unit | 200 deterministic cascade states (the F8-D16 corpus helpers, or a small fixed set before D16 lands) | run the cascade; collect `out.counts` keys | `set(out.counts) <= set(COUNTER_KEYS)`; `SMALLEST_FETCH_KINDS == forcing.SMALLEST_FETCH_ORDER == verification._SMALLEST_FETCH_ORDER` | Order-agreement pin, same pattern as the existing F5 order test |
| F9-T12 | Telemetry is lossless for every counter | unit | `passed = {every workflow key: 3}` | `_derive_loop_telemetry(passed, {}, 0)` | output contains every `forced_<key>_count` (incl. `pivot`, five smallest-fetch kinds, `absence_disconfirmation`, `floor`, `callsite`, `requery`), `d6_blocked_no_evidence_count`, `fetches_dispatched`, `defer_repairs_used`, `defer_repairs_limit`; all ten previously-present names unchanged | Red today (probe: ten keys missing). Negative control: remove one counter from `COUNTER_KEYS` and the corresponding assertion fails |
| F9-T13 | Workflow keys cannot be dropped silently | unit (AST) | parse the `loop_telemetry={...}` literal in the workflow | compare literal keys with allowlist, handled-elsewhere and declared-not-stamped | literal keys are a subset | Code shown in D3. Negative control: add `"new_counter": 1` to a copy of the literal; the test names it |
| F9-T14 | Malformed telemetry values | unit | `passed` with `True`, `"7"`, `-1`, `None`, `float('nan')` for counter keys | `_derive_loop_telemetry`; `counter_telemetry` | `_derive_loop_telemetry` skips bool/None/str; `counter_telemetry` zeroes bool/negative/str; nothing raises and other keys survive | Fail-open per key; whole-derivation `None` is still reserved for non-dict input |
| F9-T15 | `merge_counts` is per-key fail-open | unit | counters all 1; returned `{"pivot": "x", "floor": 5, "replan": -2, "bogus": 9}` | `merge_counts` | `pivot` and `replan` keep 1, `floor` becomes 5, `bogus` ignored; a non-mapping `returned` changes nothing | Differs from the old all-or-nothing unpack only for malformed input |
| F9-T16 | Dropped key is logged by name only | unit | `passed` with `{"mystery": 1, "iterations": 2}` and a `caplog` at WARNING | `_derive_loop_telemetry` | one warning naming `mystery`, no values, `iterations` still stamped | The log is bounded to 20 names |
| F9-T17 | Counters survive end to end | workflow-harness | F10 harness scenario S1 (five distinct-file proposals, lv7) | record the workflow; read the `conclude_investigation_activity` input | `loop_telemetry["forced_pivot_count"] == 2`, `forced_floor_count`, `forced_callsite_count`, `forced_requery_count`, `forced_absence_disconfirmation_count` present as ints | Replaces the source-text pin in `test_f4_pivot_bounds.py`. Negative control: stop passing `**counter_telemetry(counters)` and `forced_pivot_count` goes missing |
| F9-T18 | Older rows are not zero-filled | unit | `passed` without any counter keys (a pre-change run) | `_derive_loop_telemetry` | no `forced_*_count` keys in the output (absence means not recorded) | Protects the "absent is not zero" contract the F4 evaluator relies on |

#### Rollout, risks, open owner decisions

Land after F10-B so the replay corpus proves D2 is command-neutral. One tranche; update the tests that pin workflow source text (the F4 counter pin above, the H5 substring pins, the F8 requery-stamp pin) or import `Force`, `Rule` and `RULES` from `forcing`; run the full unit suite, ruff, and the mypy ratchet. Architecture section 72 corrects the sections 67 and 70 telemetry claims (they become true). Risks: a missed raw `"forced_..."` literal (caught by F9-T03); G6 comparability (D5 pin). Requires owner approval: none. **Open owner decisions:** O-F9-1 pin versus no pin for D5; O-F9-2 stamp a bounded `plan` into telemetry or keep it declared not stamped.

### Item F10: Replay posture — versioning stance, replay corpus, determinism lint and the unmarked-pin latch

#### Status

**Open.** Severity: **should-fix** (raised from consider: the unmarked-pin hazard is a real replay break on the next version bump, and there is no replay test of any kind). Dependencies: none inbound; F9 and the F8 loop (D15) depend on this item's replay corpus. Coordination: group W's decision on deleting the advisory layer (W1) would dissolve D9 and the `FrontierCandidate`/`WorkOrder` parts of D10 (the lint in D11 stays). Gating: none. Deploy requires draining or terminating in-flight dev workflows (**requires owner approval**), see Rollout.

Also resolves the ledger entry "Workflow requery carrier mutation requires history/replay validation" (scenario S2 and test F10-T14) and F2 follow-up (b) (F10-T17).

#### Verified problem

| Claim | Verified how | Verdict |
|---|---|---|
| No Temporal replay test exists | `rg 'Replayer|WorkflowEnvironment|WorkflowHistory|patched|get_version'` over `src`, `tests`, `scripts` returns no code hit; `test_loop_exit_contract.py` says "No Temporal test harness exists in this repo" | Confirmed |
| No versioning posture | no `workflow.patched` anywhere; the only mechanism is the entity loop pin | Confirmed; D7 states it |
| `uuid4` fallback in the order-id helper | `forcing.py::_order_id` ends `except Exception: return uuid.uuid4()` after `uuid.uuid5(...)` (cannot raise for a `str`); the sandbox restricts `uuid.uuid4` (SDK `_restrictions.py`: `"uuid": SandboxMatcher(use={"uuid1","uuid4","uuid7"}, only_runtime=True)`) | Confirmed, unreachable today |
| Passed-through modules run real clock and uuid defaults | AST probe: `FrontierCandidate.candidate_id` default `uuid4`, `FrontierCandidate.seeded_at` default `datetime.now(UTC)`, `WorkOrder.work_order_id` default `uuid4`. Runtime probe (sandboxed replay of a scenario where a pivot fires, with the `datetime` binding in `frontier` instrumented): `FrontierCandidate.seeded_at` executed `datetime.now` once, no sandbox error | `seeded_at` Confirmed live; the two `uuid4` defaults are latent (every construction site in `forcing.py` and `work_orders.py` pins the id) |
| `retrieval_ledger.py:277` reads `os.environ` | reachability lint from the workflow modules: no env read reachable; `_max_attempts_from_env` and `definition_shadow.resolve_definition_mode` are activity-side | Not reachable from workflow code (review caveat holds only for adjacent code) |
| In-place carrier mutation is replay-safe | SDK source: activity arguments are converted in `_ActivityHandle._apply_schedule_command` when the activity task first runs; every call site `await`s `execute_activity` immediately, so no workflow code runs between schedule and conversion. Probe: scripted replay with carriers appended and stamped in place round-trips with identical inputs | Holds today; relies on the await-immediately idiom (D11 guards against the non-awaited form) |
| **New:** unmarked pin floats to current | `run_investigation.py` uses `input_data.loop_version or LOOP_VERSION_CURRENT` in three places; `bulk_intake.py::_run_one_child` builds `RunInvestigationInput` with no `loop_version`; the API path passes `resolve_loop_version(entity.metadata)` | Confirmed hazard: a `None` input replays under the replay-time `LOOP_VERSION_CURRENT` |
| **New:** `Replayer` does not check activity inputs | probe: tampered the recorded input of the second `execute_action_activity`; `replay_workflow` still passed | Confirmed; an input-parity interceptor is mandatory |

Offline evidence that a real replay strategy is feasible: a prototype synthesized a 341-event history for a five-turn run with scripted activities, replayed it in the real `SandboxedWorkflowRunner` in 0.3-0.5 s, and a negative control (one activity removed from the history) raised `NondeterminismError`. Scenario probes dispatched exactly the sequences the cascade tests pin (lv7: `a.py`, `b.py@HEAD`, `c.py@HEAD`, `d.py`, `e.py`; lv6 keeps pivoting on `d.py` and `e.py`).

#### Design

##### D7: The repo's versioning posture

**Decision:** Loop-semantics changes are versioned by the entity pin (`loop_version_at_least`), and every workflow-code change is validated by a replay corpus. `workflow.patched` is not used before go-live. The pin is sufficient for replay because it travels in the workflow start input (recorded in the history's first event) and, after D8, is latched in history even for unmarked inputs, so an old history replays under the branch it ran. A change that adds, removes or reorders commands unrelated to loop semantics (a new activity, a timer) is allowed before go-live only with a drain of in-flight dev workflows; after go-live it needs `workflow.patched` or Worker Versioning, decided at the go-live gate.

| Change kind | Mechanism | Before go-live | After go-live |
|---|---|---|---|
| Loop semantics (decisions, or the commands they gate) | `loop_version_at_least` on the entity pin | required | required |
| Command-neutral refactor (F8, F9, D15) | replay corpus must stay green with **unmodified** fixtures | required | required |
| Command-sequence change unrelated to semantics | drain in-flight dev workflows (owner approval) | allowed | `workflow.patched`, or Worker Versioning (`VersioningBehavior.PINNED` with `WorkerDeploymentConfig`), owner decision |

```python
# tests/unit/test_workflow_versioning_posture.py
import ast
import pathlib

WORKFLOWS = pathlib.Path("src/investigation_agent_platform/application/worker/workflows")
BANNED = {"patched", "deprecate_patch", "get_version"}


def test_no_workflow_patch_markers_before_go_live() -> None:
    hits: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in BANNED:
                hits.append(f"{path.name}:{node.lineno} {node.attr}")
    assert not hits, f"posture is loop pin + replay corpus, not patched(): {hits}"
```

**Alternative considered:** adopt `workflow.patched` for every cascade change. Rejected: the repo contract is pin-on-entity; a patch marker records "this run saw the change" in history while the entity pin records "this run is defined to have these semantics" in the data model where the activities (which are not replayed) also read it (`_policy_for_loop_version`, the verify and conclude activities). Two mechanisms for one concept would diverge. **Alternative considered:** Worker Versioning now. Rejected: no live deployment, and `use_worker_versioning` requires a `build_id` and a deployment story the repo does not have; revisit at go-live. **Trade-offs:** the posture pin test is deliberately tripwire-shaped; adding `patched` later is a conscious edit of that test and of this posture.

---

##### D8: Latch the loop pin in history

**Decision:** The create activity returns the entity's resolved pin; the workflow latches `input.loop_version or create_res.loop_version` once and never consults `LOOP_VERSION_CURRENT` itself. Activity results are recorded in history, so replay reads the recorded value instead of the code constant. An unknown pin on the entity fails non-retryably in the activity (matching the API dispatch, which already fails an unknown pin); a missing pin after both sources fails the run closed.

```python
# src/investigation_agent_platform/application/worker/activities/inputs.py
@dataclass
class CreateInvestigationOutput:
    investigation_id: str
    status: str
    disposition_action: str | None = None
    primary_target_id: str | None = None
    #: The entity's resolved loop pin (F10). Recorded in history so replay does not
    #: re-resolve an unmarked row against the code's current version.
    loop_version: str = ""
```

```python
# src/investigation_agent_platform/application/worker/activities/reasoning.py  (both return paths)
from temporalio.exceptions import ApplicationError  # already imported as ApplicationFailure here

from investigation_agent_platform.domain.investigation.loop_version import (
    UnknownLoopVersion,
    resolve_loop_version,
)


def _pinned_loop_version(inv: Any) -> str:
    try:
        return resolve_loop_version(getattr(inv, "metadata", None))
    except UnknownLoopVersion as exc:
        raise ApplicationFailure(str(exc), type="UnknownLoopVersion", non_retryable=True) from exc

# loaded-existing path:   CreateInvestigationOutput(..., loop_version=_pinned_loop_version(existing))
# created-new path:       CreateInvestigationOutput(..., loop_version=_pinned_loop_version(inv))
```

```python
# src/investigation_agent_platform/application/worker/workflows/run_investigation.py
        if create_res.status == "CANCELLED":
            ...  # unchanged early return

        # F10: latch the pin from history. The input pin (API dispatch) wins; an unmarked input
        # (bulk-intake child, legacy caller) takes the pin the create activity recorded, so a
        # later LOOP_VERSION_CURRENT bump cannot change what a replayed run decides.
        loop_version = str(input_data.loop_version or getattr(create_res, "loop_version", "") or "")
        if not loop_version:
            raise ApplicationError(
                "loop version pin unavailable", type="LoopVersionMissing", non_retryable=True
            )
        ...
        _split_carriers = _carriers_separated(loop_version)        # was input_data.loop_version or CURRENT
        ... loop_version=loop_version,                              # the two other call sites
```

**Alternative considered:** make the bulk-intake parent pass the pin. Rejected: the parent only has an investigation id from an activity-provided record list and would need an extra lookup; the latch covers every caller at one site. **Alternative considered:** `workflow.patched("loop-pin-latch")`. Rejected by D7. **Trade-offs:** a history recorded before this change with an unmarked input has no `loop_version` in its create result and fails closed on replay (dev-only; drain, see Rollout). The `PostReasonCascadeState.loop_version = LOOP_VERSION_CURRENT` dataclass default stays for pure unit tests; the workflow always passes the latched value.

---

##### D9: Delete the `uuid4` fallback

```python
# src/investigation_agent_platform/application/worker/workflows/forcing.py
def _order_id(kind: str, action: str, params: dict[str, Any], seed_source: str) -> Any:
    """Deterministic work-order identity (pure, never random). Never raises for str inputs."""
    try:
        canonical = "|".join(
            [
                str(WORK_ORDER_VERSION_CURRENT or ""),
                str(kind or ""),
                str(action or ""),
                json.dumps(dict(params or {}), sort_keys=True, default=str),
                str(seed_source or ""),
            ]
        )
    except Exception:
        canonical = f"{WORK_ORDER_VERSION_CURRENT}|{kind}|{action}|{seed_source}"
    return uuid.uuid5(uuid.NAMESPACE_URL, canonical)   # uuid5 over a str cannot fail; no random fallback
```

**Alternative considered:** leave it ("unreachable"). Rejected: in the sandbox the handler would raise `RestrictedWorkflowAccessError` from inside an `except` and the caller's blanket `except` would swallow it, hiding the fault. If W1 deletes `_order_id`, this decision is moot. **Trade-offs:** none.

---

##### D10: Deterministic defaults in workflow-reachable domain models

**Decision:** `WorkOrder` and `FrontierCandidate` stop calling `uuid4`/`datetime.now` for defaults. Ids default to a content-addressed `uuid5` (a new pure helper); `seeded_at` becomes `datetime | None = None` because no production code reads it (review W6; AST probe finds no reader outside `frontier.py`). Content addressing also fixes the advisory ranking tie-break (`candidate_id.hex`) from random to stable.

```python
# src/investigation_agent_platform/domain/common/ids.py   (new)
from __future__ import annotations

import json
import uuid
from typing import Any


def _canon(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return getattr(value, "value", value)


def content_uuid(kind: str, *parts: Any) -> uuid.UUID:
    """Deterministic uuid5 over canonical JSON of ``parts`` (pure; no clock, no randomness)."""
    canonical = json.dumps([_canon(p) for p in parts], sort_keys=True, default=str)
    return uuid.uuid5(uuid.NAMESPACE_URL, f"iap-content-id|{kind}|{canonical}")
```

```python
# src/investigation_agent_platform/domain/collection/work_orders.py
_NIL_ID = UUID(int=0)

class WorkOrder(BaseModel):
    model_config = ConfigDict(frozen=True)

    work_order_id: UUID = Field(default=_NIL_ID)  # nil means "derive from content"
    ...  # remaining fields unchanged

    @model_validator(mode="before")
    @classmethod
    def _content_addressed_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("work_order_id") in (None, _NIL_ID):
            data = {
                **data,
                "work_order_id": content_uuid(
                    "work-order",
                    data.get("version", WORK_ORDER_VERSION_CURRENT),
                    data.get("kind"),
                    data.get("action_type"),
                    data.get("action_parameters") or {},
                    data.get("seed_source", ""),
                    data.get("gap_id"),
                ),
            }
        return data
```

```python
# src/investigation_agent_platform/domain/collection/frontier.py
class FrontierCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: UUID = Field(default=_NIL_ID)
    ...
    #: Informational only; no production reader (review W6). The domain never reads the clock.
    seeded_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _content_addressed_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("candidate_id") in (None, _NIL_ID):
            data = {
                **data,
                "candidate_id": content_uuid(
                    "frontier-candidate",
                    data.get("version", FRONTIER_VERSION_CURRENT),
                    data.get("service"), data.get("file_path"), data.get("commit"),
                    data.get("span"), data.get("config"), data.get("dimension"),
                    data.get("seed_fetch"), data.get("addresses"), data.get("seed_source", ""),
                ),
            }
        return data
```

`seed_from_dormant_templates` (dead per W6, not workflow-reachable, so lint-clean) takes `now: datetime | None = None` instead of calling `datetime.now(UTC)`, so it stays deterministic if it is ever wired in.

**Alternative considered:** inject `workflow.now()` and `workflow.uuid4()` from the workflow call sites. Rejected: the domain package must not import `temporalio`, and the call sites in `work_orders_to_seeds` are inside the domain. **Alternative considered:** `ConfigDict(arbitrary...)` no-default required ids. Rejected: every test and call site would have to mint ids. **Trade-offs:** two orders with identical content now share an id (intended: `_order_id` already documents content-addressed dedupe); callers that need distinct identical orders pass `work_order_id` explicitly.

---

##### D11: Static determinism lint over what the workflow can reach

**Decision:** A unit test parses the two workflow modules, follows names imported from the rest of the platform transitively, and fails on `uuid.uuid4`/`uuid1`, `random.*`, `time.time`/`monotonic`, `os.environ`/`getenv`, `datetime.now/utcnow/today`, and bare `uuid4`/`uuid1`/`getenv` loads inside any reachable function or class (class bodies include pydantic `Field(default_factory=...)`). Reachability, not module scope, is the rule: `retrieval_ledger._max_attempts_from_env` and `definition_shadow.resolve_definition_mode` read the environment but are activity-side, so a module-level scan would be a false positive. An `ACCEPTED` map records any deliberate exception with a reason (empty at landing). The prototype was run against HEAD: it reports exactly four hits (`forcing._order_id` `uuid.uuid4`; `FrontierCandidate` `uuid4` and `datetime.now`; `WorkOrder` `uuid4`) and none for the env readers.

```python
# tests/unit/test_workflow_determinism_lint.py
from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path("src")
ROOTS = [
    "investigation_agent_platform.application.worker.workflows.run_investigation",
    "investigation_agent_platform.application.worker.workflows.forcing",
]
#: (module, function-or-class, construct) -> reason. Empty means no exceptions.
ACCEPTED: dict[tuple[str, str, str], str] = {}
FORBIDDEN_ATTRS = {
    ("uuid", "uuid4"), ("uuid", "uuid1"), ("random", "random"), ("random", "randint"),
    ("random", "choice"), ("time", "time"), ("time", "monotonic"), ("os", "environ"),
    ("os", "getenv"), ("datetime", "now"), ("datetime", "utcnow"), ("datetime", "today"),
    ("secrets", "token_hex"),
}
FORBIDDEN_NAMES = {"uuid4", "uuid1", "getenv"}


def _tree(module: str) -> ast.Module | None:
    path = SRC / (module.replace(".", "/") + ".py")
    return ast.parse(path.read_text(encoding="utf-8")) if path.exists() else None


def _defs(tree: ast.Module) -> dict[str, ast.AST]:
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def _platform_imports(tree: ast.Module) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.level == 0 and (n.module or "").startswith("investigation_agent_platform"):
            out |= {(n.module, a.name) for a in n.names}  # type: ignore[arg-type]
    return out


def _violations(node: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and (n.value.id, n.attr) in FORBIDDEN_ATTRS:
            found.append((n.lineno, f"{n.value.id}.{n.attr}"))
        if isinstance(n, ast.Name) and n.id in FORBIDDEN_NAMES and isinstance(n.ctx, ast.Load):
            found.append((n.lineno, n.id))
    return found


def _reachable(module: str, roots: list[str], seen: set[tuple[str, str]]) -> list[tuple[str, str, int, str]]:
    tree = _tree(module)
    if tree is None:
        return []
    table, out, stack = _defs(tree), [], list(roots)
    while stack:
        name = stack.pop()
        if (module, name) in seen or name not in table:
            continue
        seen.add((module, name))
        node = table[name]
        out += [(module, name, line, what) for line, what in _violations(node)]
        used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        stack += [u for u in used if u in table]
        for other_module, imported in _platform_imports(tree):
            if imported in used and imported not in table:
                out += _reachable(other_module, [imported], seen)
    return out


def _lint() -> list[tuple[str, str, int, str]]:
    seen: set[tuple[str, str]] = set()
    hits: list[tuple[str, str, int, str]] = []
    for module in ROOTS:
        tree = _tree(module)
        assert tree is not None
        hits += _reachable(module, list(_defs(tree)), seen)
    return sorted({h for h in hits if (h[0], h[1], h[3]) not in ACCEPTED})


def test_workflow_reachable_code_is_deterministic() -> None:
    assert _lint() == [], "workflow-reachable code reads clock, randomness or environment"
```

**Alternative considered:** rely on the SDK sandbox to raise at runtime. Rejected: the sandbox does not restrict passed-through modules (probe: `FrontierCandidate.seeded_at` read the real clock inside a sandboxed replay without error), and a swallowed `except Exception` can hide the error even in sandboxed modules. **Trade-offs:** name-based reachability over-approximates (a same-named local shadows nothing); false positives are resolved with a documented `ACCEPTED` entry. It also does not see dynamic dispatch; the replay corpus (D12) is the runtime complement.

---

##### D12: The offline replay harness

**Decision:** Test the real `RunInvestigationWorkflow` three ways with no server and no network: (1) run its body in-process against scripted activities and record `(activity, input, result)`; (2) synthesize the Temporal event history a worker would have written for that script; (3) replay that history in the real `SandboxedWorkflowRunner` through `Replayer`, with an interceptor that captures every activity input the replayed workflow re-derives, compared field by field to the recorded inputs. Step 3 is what the SDK alone cannot do (it checks commands, not inputs). `WorkflowEnvironment.start_time_skipping`/`start_local` are not adopted for this tier because both download a server binary; an optional integration smoke with a stub-activity worker needs owner approval for that download and is outside the counted tests.

```python
# tests/replay/iap_replay_harness.py
"""Offline Temporal replay harness for RunInvestigationWorkflow (no server, no network)."""
from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from unittest import mock

from google.protobuf.timestamp_pb2 import Timestamp
from temporalio import workflow
from temporalio.api.common.v1 import ActivityType, Payloads, WorkflowType
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import (
    ActivityTaskCompletedEventAttributes, ActivityTaskScheduledEventAttributes,
    ActivityTaskStartedEventAttributes, HistoryEvent, WorkflowExecutionCompletedEventAttributes,
    WorkflowExecutionStartedEventAttributes, WorkflowTaskCompletedEventAttributes,
    WorkflowTaskScheduledEventAttributes, WorkflowTaskStartedEventAttributes,
)
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.client import WorkflowHistory
from temporalio.converter import DataConverter
from temporalio.worker import Interceptor, Replayer, WorkflowInboundInterceptor, WorkflowOutboundInterceptor

from investigation_agent_platform.application.worker.activities.inputs import (
    CreateInvestigationOutput, GenericActivityResult,
)
from investigation_agent_platform.application.worker.workflows import run_investigation as ri

Handler = Callable[[Any, int], Any]  # (activity input, 0-based call index of that activity) -> result


@dataclass
class Call:
    name: str
    arg: Any
    result: Any


def ok(**data: Any) -> GenericActivityResult:
    return GenericActivityResult(success=True, data=data)


class Script:
    """Scripted activity layer: per-activity handlers; empty success by default."""

    def __init__(self, handlers: dict[str, Handler], loop_version: str) -> None:
        self.handlers, self.loop_version = handlers, loop_version
        self.calls: list[Call] = []
        self._seen: dict[str, int] = {}

    async def execute_activity(self, fn: Any, arg: Any = None, **_kw: Any) -> Any:
        name = fn.__name__
        index = self._seen.get(name, 0)
        self._seen[name] = index + 1
        if name in self.handlers:
            result = self.handlers[name](arg, index)
        elif name == "create_investigation_activity":
            result = CreateInvestigationOutput(
                investigation_id=arg.investigation_id, status="CREATED", loop_version=self.loop_version
            )
        else:
            result = ok()
        self.calls.append(Call(name, arg, result))
        return result


async def record(script: Script, wf_input: ri.RunInvestigationInput) -> ri.RunInvestigationResult:
    """Run the real workflow body in-process against the scripted activities."""
    wf = ri.RunInvestigationWorkflow()
    with (
        mock.patch.object(workflow, "execute_activity", script.execute_activity),
        mock.patch.object(workflow, "wait_condition", lambda _f, **_k: asyncio.sleep(0)),
    ):
        return await wf._run_inner(wf_input)


async def build_history(wf_input: Any, calls: list[Call], result: Any, workflow_id: str) -> WorkflowHistory:
    """Synthesize the event history a worker would have written for these calls."""
    encode = DataConverter.default.encode
    clock = [1_700_000_000]
    events: list[HistoryEvent] = []

    def add(**kw: Any) -> int:
        clock[0] += 1
        events.append(HistoryEvent(event_id=len(events) + 1, event_time=Timestamp(seconds=clock[0]), **kw))
        return len(events)

    queue = TaskQueue(name="replay-q")
    add(
        event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
        workflow_execution_started_event_attributes=WorkflowExecutionStartedEventAttributes(
            workflow_type=WorkflowType(name="RunInvestigationWorkflow"), task_queue=queue,
            input=Payloads(payloads=await encode([wf_input])), attempt=1,
            original_execution_run_id="run-1", first_execution_run_id="run-1",
        ),
    )

    async def workflow_task() -> int:
        sched = add(
            event_type=EventType.EVENT_TYPE_WORKFLOW_TASK_SCHEDULED,
            workflow_task_scheduled_event_attributes=WorkflowTaskScheduledEventAttributes(task_queue=queue),
        )
        started = add(
            event_type=EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED,
            workflow_task_started_event_attributes=WorkflowTaskStartedEventAttributes(scheduled_event_id=sched),
        )
        return add(
            event_type=EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED,
            workflow_task_completed_event_attributes=WorkflowTaskCompletedEventAttributes(
                scheduled_event_id=sched, started_event_id=started
            ),
        )

    for seq, call in enumerate(calls, start=1):
        done_task = await workflow_task()
        scheduled = add(
            event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED,
            activity_task_scheduled_event_attributes=ActivityTaskScheduledEventAttributes(
                activity_id=str(seq), activity_type=ActivityType(name=call.name), task_queue=queue,
                input=Payloads(payloads=await encode([call.arg])), workflow_task_completed_event_id=done_task,
            ),
        )
        started = add(
            event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED,
            activity_task_started_event_attributes=ActivityTaskStartedEventAttributes(
                scheduled_event_id=scheduled, attempt=1
            ),
        )
        add(
            event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED,
            activity_task_completed_event_attributes=ActivityTaskCompletedEventAttributes(
                result=Payloads(payloads=await encode([call.result])),
                scheduled_event_id=scheduled, started_event_id=started,
            ),
        )
    final_task = await workflow_task()
    add(
        event_type=EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
        workflow_execution_completed_event_attributes=WorkflowExecutionCompletedEventAttributes(
            result=Payloads(payloads=await encode([result])), workflow_task_completed_event_id=final_task
        ),
    )
    return WorkflowHistory(workflow_id, events)


def normalize(value: Any) -> Any:
    raw = dataclasses.asdict(value) if dataclasses.is_dataclass(value) else value
    return json.loads(json.dumps(raw, default=str, sort_keys=True))


async def recorded_inputs(history: WorkflowHistory) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for event in history.events:
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            attrs = event.activity_task_scheduled_event_attributes
            decoded = await DataConverter.default.decode(list(attrs.input.payloads))
            out.append((attrs.activity_type.name, normalize(decoded[0])))
    return out


class _Parity:
    """Collects the activity inputs the REPLAYED workflow schedules (the SDK does not compare them)."""

    def __init__(self) -> None:
        self.inputs: list[tuple[str, Any]] = []

    def interceptor(self) -> Interceptor:
        sink = self.inputs

        class _Out(WorkflowOutboundInterceptor):
            def start_activity(self, input: Any) -> Any:
                sink.append((input.activity, input.args[0] if input.args else None))
                return super().start_activity(input)

        class _In(WorkflowInboundInterceptor):
            def init(self, outbound: WorkflowOutboundInterceptor) -> None:
                super().init(_Out(outbound))

        class _Capture(Interceptor):
            def workflow_interceptor_class(self, input: Any) -> type[WorkflowInboundInterceptor]:
                return _In

        return _Capture()


async def replay(history: WorkflowHistory) -> list[tuple[str, Any]]:
    """Replay in the real sandbox (raises NondeterminismError on a command mismatch);
    return the activity inputs the workflow re-derived, normalized for comparison."""
    parity = _Parity()
    await Replayer(
        workflows=[ri.RunInvestigationWorkflow], interceptors=[parity.interceptor()]
    ).replay_workflow(history)
    return [(name, normalize(arg)) for name, arg in parity.inputs]


async def assert_replays_identically(history: WorkflowHistory) -> None:
    assert await replay(history) == await recorded_inputs(history), "replayed inputs diverge from recorded"
```

**Alternative considered:** capture real histories from the dev Temporal service and commit them. Rejected: histories embed evidence payloads from analyzed repositories, which the non-reference rule forbids in committed content; committed fixtures are scripted and synthetic. A real captured history may still be replayed locally and uncommitted (**requires owner approval** to read the dev cluster). **Alternative considered:** `WorkflowEnvironment` with a real worker. Rejected for this tier (binary download, slower, still needs scripted activities). **Trade-offs:** the synthesized history is only as faithful as the builder (one workflow task per activity; no timers, signals or retries). Signals (`pause`, `cancel`), updates and retried activities are not covered; the scenarios cover the cascade, carriers and pin routing, which is where this group's risk lives.

---

##### D13: Scenario catalog and corpus policy

**Decision:** Committed fixtures are *transcripts* (the workflow input, the scripted calls, the result and the expected dispatch list, plain JSON recorded at a base SHA), not serialized histories; the test synthesizes the history from the transcript. Two checks per scenario: **self-replay** (record fresh, build, replay: catches nondeterminism and in-place-mutation bugs in the current code) and **golden replay** (load the committed transcript recorded at an earlier SHA, replay with the current code, require input parity and the expected dispatch list: catches refactor drift and version-pin regressions). Fixtures are regenerated only with `python tests/replay/record_transcripts.py` and only when a loop version intentionally changes behavior; a pin's fixtures are never regenerated, and a new version adds new fixtures beside the old ones (the older-pin regression pins).

| Scenario | Script | Proves |
|---|---|---|
| S1 `pivot_cap` | five distinct-file `GET_CODE` proposals, `max_iterations=5` | lv7 dispatches `a.py`, `b.py@HEAD`, `c.py@HEAD`, `d.py`, `e.py`; lv6 pivots on every turn |
| S2 `requery_carrier` | verify turn 0 returns `ledger_requery`; the model keeps proposing `svc/a.py` | carrier append, G3 dispatch of the `SEARCH_CODE` order on turn 1, in-place consumed stamp, no refire on turn 2 |
| S3 `gap_feed` | verify turn 0 returns a `DEFINITION` claim gap | feed carrier append, smallest-fetch dispatch of `svc/Order.java` lines 10-40 at `HEAD`, coverage stamp, no refire |
| S4 `unmarked_pin` | `loop_version=None` input, create result `lv6` | the latch (D8): lv6 semantics even after `LOOP_VERSION_CURRENT` is patched to a later value in the test |
| S5 `carrier_in_prompt` | S3 under pins `lv5` and `lv7` | lv5 reasoner payload carries the `work_order_feed` entry; lv7 payload is turns only (F2 follow-up b) |

```python
# tests/replay/test_workflow_replay.py
from __future__ import annotations

import json
import pathlib

import pytest
from iap_replay_harness import (
    Script, assert_replays_identically, build_history, ok, record,
)

from investigation_agent_platform.application.worker.workflows import run_investigation as ri

FIXTURES = pathlib.Path("tests/fixtures/replay")


def _reason(files):  # one GET_CODE proposal per turn
    def handler(_arg, i):
        f = files[min(i, len(files) - 1)]
        return ok(next_action="GET_CODE", action_parameters={"file_path": f},
                  conclusion_readiness=0.1, hypotheses_evaluated=[], observations=[])
    return handler


def _exec(_arg, i):
    return ok(action_outcome={"status": "SUCCEEDED", "item_count": 1, "evidence_ids": [f"e{i}"]})


def _verify_requery(_arg, i):
    if i == 0:
        return ok(verified=False, ledger_requery={
            "query": "SampleOrderFilter", "action_type": "SEARCH_CODE",
            "action_parameters": {"query": "SampleOrderFilter"},
            "claim_ref": "claim-1", "scope": "", "max_attempts": 3})
    return ok(verified=False)


def _verify_gap(_arg, i):
    if i == 0:
        return ok(verified=False, claim_gaps=[{
            "gap_id": "DEFINITION|svc/Order.java", "kind": "DEFINITION", "claim_ref": "claim-2",
            "target": {"file_path": "svc/Order.java", "line_start": 10, "line_end": 40}, "scope": "HEAD"}])
    return ok(verified=False)


async def _run(handlers, pin, iterations, input_pin="same"):
    wf_input = ri.RunInvestigationInput(
        application_id="app", tenant_id="tenant-a", investigation_id="inv-1",
        description="orders fail", max_iterations=iterations,
        loop_version=pin if input_pin == "same" else input_pin)
    script = Script(handlers, pin)
    result = await record(script, wf_input)
    history = await build_history(wf_input, script.calls, result, "wf-investigation-inv-1")
    return script, history


def _dispatches(script):
    return [(c.arg.action, c.arg.parameters) for c in script.calls if c.name == "execute_action_activity"]


@pytest.mark.asyncio
async def test_s1_pivot_cap_lv7_self_replay_and_dispatch() -> None:
    files = ["a.py", "b.py", "c.py", "d.py", "e.py"]
    script, history = await _run({"reason_activity": _reason(files), "execute_action_activity": _exec}, "lv7", 5)
    await assert_replays_identically(history)
    assert _dispatches(script) == [
        ("GET_CODE", {"file_path": "a.py"}),
        ("GET_CODE", {"file_path": "b.py", "revision": "HEAD"}),
        ("GET_CODE", {"file_path": "c.py", "revision": "HEAD"}),
        ("GET_CODE", {"file_path": "d.py"}),
        ("GET_CODE", {"file_path": "e.py"}),
    ]


@pytest.mark.asyncio
async def test_s2_requery_carrier_dispatches_once_and_replays() -> None:
    script, history = await _run({"reason_activity": _reason(["svc/a.py"]),
                                  "execute_action_activity": _exec,
                                  "verify_root_cause_activity": _verify_requery}, "lv7", 4)
    await assert_replays_identically(history)
    assert _dispatches(script)[:3] == [
        ("GET_CODE", {"file_path": "svc/a.py"}),
        ("SEARCH_CODE", {"query": "SampleOrderFilter"}),
        ("GET_CODE", {"file_path": "svc/a.py"}),
    ]
```

The remaining scenarios follow the same shape (S3 asserts the `svc/Order.java` fetch with `start_line=10`, `end_line=40`, `revision="HEAD"` on turn 1 and no refire; S4 records with `loop_version=None` and `Script(..., "lv6")` while `monkeypatch.setattr(ri, "LOOP_VERSION_CURRENT", "lv6", raising=False)` (the attribute disappears once D8 removes the import) and then replays after setting it to `"lv7"`; S5 reads `reason_activity` call inputs and compares `[e.get("kind", "turn") for e in call.arg.recent_actions]`). Probe results for S3 and S5: lv7 reasoner `recent_actions` per turn is `[], [turn], [turn, turn], ...`; lv5 is `[], [turn, work_order_feed], [turn, work_order_feed, turn], ...`.

#### Version pinning / migration

**No loop-version pin is needed for F10.** D7 is a posture, D8 changes where an already-defined value is read from (no decision changes for any run whose input carries a pin, which is every API-dispatched run; for an unmarked input the latch freezes the value the old code recomputed from the constant), D9-D10 delete randomness and clock reads that influence no decision, and D11-D13 are tests. The pin machinery itself is exercised by the corpus: S1 (lv6 versus lv7) and S5 (lv5 versus lv7) are the workflow-level older-pin regression pins. Migration: `CreateInvestigationOutput` gains one defaulted field (additive for the payload converter); no database change.

#### Test cases

Written first; F10-T01, T04, T05, T06, T08, T10, T21 are red on the pre-change tree (T01 reports exactly the four known hits). Files: `tests/unit/test_workflow_determinism_lint.py`, `tests/unit/test_workflow_versioning_posture.py`, `tests/unit/test_f10_deterministic_defaults.py`, `tests/replay/test_workflow_replay.py`.

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| F10-T01 | Lint finds today's four hazards | unit | `ACCEPTED = {}`, unchanged tree | `_lint()` | `forcing._order_id uuid.uuid4`, `FrontierCandidate uuid4`, `FrontierCandidate datetime.now`, `WorkOrder uuid4` | Red-before evidence; it is the "shown to fail" step for D9-D10 |
| F10-T02 | Lint is green after the fix | unit | tree after D9, D10 | `test_workflow_reachable_code_is_deterministic` | empty | The standing guard |
| F10-T03 | Lint is reachability-based | unit | temp copy of a reachable module with `uuid.uuid4()` added to a reachable function, and `os.environ` left in unreachable `retrieval_ledger._max_attempts_from_env` | lint over the copy | flags the reachable `uuid4`, does not flag the env reader | Negative control: the injected call is the line to add; removing the reachability walk makes the env reader a false positive |
| F10-T04 | `_order_id` never calls `uuid4` | unit | `monkeypatch.setattr(uuid, "uuid4", boom)`; params containing a non-JSON value | `_order_id("FETCH_SEED", "GET_CODE", {"x": object()}, "s")` | returns a `uuid5`, deterministic across two calls, `boom` never called | Covers the `json.dumps` failure branch that used to fall through to `uuid4` |
| F10-T05 | `WorkOrder` id is content-addressed | unit | same fields twice; one field changed; explicit `work_order_id` | construct `WorkOrder` | equal ids for equal content, different for different, explicit id wins, nil id is replaced; `uuid4` patched to raise is never called | Edge: `action_parameters` key order must not matter (canonical JSON) |
| F10-T06 | `FrontierCandidate` reads no clock | unit | `datetime` instrumented in `frontier`; pivot-style construction via `work_orders_to_seeds` | construct candidates | `seeded_at is None`, id content-addressed and stable, the `now` hook never called | Red today (`seeded_at` executes `datetime.now`) |
| F10-T07 | Versioning posture pin | unit (AST) | all `workflows/*.py` | scan for `patched`, `deprecate_patch`, `get_version` | none found | Negative control: add `workflow.patched("x")` to a temp copy and the scan reports it; revisit at the go-live gate |
| F10-T08 | Latch adopts the recorded pin | workflow-harness | input `loop_version=None`; create result `lv6` (S4) | record, replay | dispatch sequence equals the lv6 expectation (pivot on `d.py` with `revision`) | Red today: the workflow uses `LOOP_VERSION_CURRENT` and dispatches the lv7 sequence |
| F10-T09 | Input pin wins over the create result | workflow-harness | input `lv6`, create result `lv7` | record | lv6 sequence | The input is what history already carries; a mismatch is not an error because the activity result is advisory when the input pin exists |
| F10-T10 | Missing pin fails closed | workflow-harness | input `None`, create result `loop_version=""` | `record` | the run reports `FAILED` with `error` containing `LoopVersionMissing`, no execute activity is scheduled | Fail-closed: running unknown semantics is worse than failing |
| F10-T11 | Unknown entity pin is non-retryable | unit | entity metadata `{"loop_version": "lv999"}`; and unmarked metadata | `_pinned_loop_version` | raises `ApplicationFailure(type="UnknownLoopVersion", non_retryable=True)`; unmarked resolves to the current version | Malformed pin input |
| F10-T12 | S1 self-replay and dispatch (lv7) | workflow-harness | S1 | record, build, replay, parity | no `NondeterminismError`; replayed inputs equal recorded; dispatch list as in D13 | Happy path |
| F10-T13 | Golden replay of committed transcripts | workflow-harness | `tests/fixtures/replay/*.json` recorded at the base SHA | build history from the transcript, replay with current code | parity holds and `expected_dispatch` matches for S1-S3 under their recorded pins | The refactor-drift detector for F9 and F8-D15. Negative control: change one cascade guard (for example lower `PIVOT_RECOLLECT_CAP` to 1) and S1's golden replay fails on parity while plain `replay_workflow` would pass |
| F10-T14 | Carrier append and in-place consumed stamp replay | workflow-harness | S2 | record, replay, parity | `SEARCH_CODE {query}` dispatched exactly once on turn 1, not on turn 2; replay clean | Resolves the ledger entry on requery-carrier mutation |
| F10-T15 | Gap feed and coverage stamp replay | workflow-harness | S3 | record, replay | smallest-fetch dispatch once, no refire, replay clean | Covers the second carrier kind |
| F10-T16 | Older pin keeps older dispatches | workflow-harness | S1 under `lv6` and `lv7` | record both | lv6 pivots on `d.py` and `e.py` (`revision` present), lv7 does not | Version-pin routing at workflow level |
| F10-T17 | Carrier visibility in the prompt by pin | workflow-harness | S5 | read `reason_activity` inputs | lv5 includes `work_order_feed` in `recent_actions`, lv7 does not | Closes F2 follow-up (b) and documents (d) as intended |
| F10-T18 | Harness negative control: removed activity | workflow-harness | S1 history with one activity event pair removed | `replay_workflow` | raises `NondeterminismError` | Proves the harness can fail (probe-verified) |
| F10-T19 | Harness negative control: tampered input | workflow-harness | S1 history with an `execute_action_activity` input rewritten to another file | `replay_workflow` alone, then `assert_replays_identically` | plain replay passes (SDK does not compare inputs); the parity helper fails | Documents why the interceptor exists |
| F10-T20 | Recorded payload is immune to later in-place stamps | workflow-harness | S2 under `lv5` (carriers are in `recent_actions`) | read the recorded reasoner input for turn 2, then the live carrier after the run | recorded carrier `consumed` is false while the live carrier ended true; replay parity holds | Validates the await-immediately encoding analysis in Verified problem |
| F10-T21 | Unmarked pin does not float with the code constant | workflow-harness | S4: input pin `None`, create result `lv6`, recorded while the module constant reads `lv6`, then the constant is set to `lv7` | replay | parity holds | Red today (the workflow re-reads the constant, so the replayed dispatches follow lv7 and diverge from the recorded lv6 ones). Negative control: restore `or LOOP_VERSION_CURRENT` and it fails again |

#### Rollout, risks, open owner decisions

Two tranches: F10-A (D8-D11 and the lint, posture and unit tests; includes the create-activity field) then F10-B (harness, scenarios, transcripts recorded at the commit that precedes F9/F8). Record the transcripts **before** any F9 or F8 refactor lands. Deploy impact: in-flight dev workflows started under the old code that lack the create-result pin cannot replay under D8; drain or terminate them first (**requires owner approval**). Risks: synthesized histories are simpler than real ones (mitigated by keeping scenarios cascade-focused and by an optional local replay of a real, uncommitted history with owner approval); name-based reachability over-approximation (documented `ACCEPTED`). Architecture section 72 records the posture. **Open owner decisions:** O-F10-1 confirm the posture (pin plus corpus, no `patched` before go-live); O-F10-2 whether to add the optional `WorkflowEnvironment` integration smoke (binary download); O-F10-3 order relative to group W (if W1 deletes the advisory layer first, skip D9 and the model parts of D10).

### Item F8 follow-ups: table-driven cascade and rule-completeness closure

#### Status

**Open — follow-up of IMPLEMENTED F8.** Severity: consider. Dependencies: D14 rides with the F9 tranche (it needs `Rule.counter_keys`); D15-D17 depend on F10-B (the replay corpus) and on D16 (the golden master), and should follow F9 so the counters are already one map. Gating: none. Follow-ups (c) and (d) of the ledger entry are **accepted by design** and recorded under Verified problem; (a), (b) and (e) are designed here (b is F9).

#### Verified problem

- **(a) Not one loop.** `apply_post_reason_cascade` calls `_down("<rule>")` eleven times in table order (pinned by `test_the_cascade_consults_the_table_once_per_rule_in_table_order`), but each rule keeps its own trigger signature and its own apply block: ten different helper signatures, hand-written `_counts[...]` increments, an `else:` branch in the smallest-fetch counter chain that increments `smallest_fetch_causal` for any kind that is not one of the other four, and a hard-coded F5 cancel for walk-stack over pivot (flag cleared, pivot count refunded).
- **(e) Closure is one-directional.** A rule in the table with no cascade site fails the order test, and a `_down` with no table entry fails it too. But a table rule whose block never writes its `Force`, or increments a counter key that is not in the table (a typo such as `smallest_fetch_causl`), passes every current test, and the cascade is not generated from the table.
- **(c) Accepted:** D6 is one fact (the state or the flag). Identical in production, where both are set together.
- **(d) Accepted:** the formulate trigger is no longer evaluated on a turn where the rule yields; triggers are pure, decisions are identical, and a raising trigger is no longer reached there. D15 makes this uniform for every rule (the trigger is never called when the rule yields); F8-T15 pins it.

The eleven rules fall into three shapes, which is what makes a protocol feasible: eight are uniform ("evaluate a helper; on a hit override the action and parameters, set the flag, bump a counter": walk-stack, floor, formulate, synthesis, re-read, call-site, log-search, replan); three are special (pivot: version-dependent counter and a cancel interaction; requery: effects without dispatch when the order is budget-exhausted, plus a consumed-reference hand-off, no counter; smallest-fetch: per-kind pick, extra payload keys, workflow-only bank context).

#### Design

##### Evaluation: do it or document why not

| Option | What | Risk | Closes (a)/(e) | Verdict |
|---|---|---|---|---|
| A | D14 completeness contract only | none (test only) | closes (e) in both directions; leaves (a) | do now regardless |
| B | A plus D15-D17: one loop over `RULES` with a `Fire` protocol | moderate: the cascade is the loop's most critical pure function | closes (a) and (e) structurally, removes the counter chain and the F5 special case | **recommended**, gated on D16 and F10-B |
| C | leave as is | none | neither | acceptable fallback if the owner prefers stability |

Recommendation: B, in two steps (record D16 first; then D15 and D17 as one tranche). The code size is roughly neutral (adapters replace inline blocks), so the case is not brevity: it is that after F9 the last hand-maintained per-rule facts are the flag write, the counter increment and the `_down` call, and they are exactly what a loop derives. The behavior risk is bounded because the golden master (about 600 recorded cascade input/output pairs spanning all pins) and the replay corpus both must stay green with unmodified fixtures. If either differs, stop: the refactor is wrong, not the fixture.

##### D14: Completeness contract (do now)

**Decision:** An AST test asserts, for every `Rule`, that the cascade writes `Force.<rule.force.name>.value` and increments each of the rule's counter keys through `_counts["<key>"]` (the `requery` counter is incremented by the workflow, so it is excluded), and that every `_counts["..."]` literal in the cascade is a `COUNTER_KEYS` member. Together with the existing order test this closes (e) in both directions without touching the cascade.

```python
# tests/unit/test_f8_cascade_completeness.py
import ast
import inspect

from investigation_agent_platform.application.worker.workflows import forcing
from investigation_agent_platform.domain.investigation.forcing_rules import COUNTER_KEYS, RULES, Rule

WORKFLOW_COUNTED = {"requery"}  # incremented by the workflow at the consume stamp, not by the cascade


def completeness_problems(source: str, rules: tuple[Rule, ...] = RULES, keys=COUNTER_KEYS) -> list[str]:
    tree = ast.parse(source)
    writes: set[str] = set()
    counted: set[str] = set()
    for node in ast.walk(tree):
        # _reason_data[Force.X.value] = ...
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            seg = ast.unparse(node.slice)
            if seg.startswith("Force.") and seg.endswith(".value"):
                writes.add(seg.removeprefix("Force.").removesuffix(".value"))
            if ast.unparse(node.value) == "_counts" and isinstance(node.slice, ast.Constant):
                counted.add(str(node.slice.value))
    problems = []
    for rule in rules:
        if rule.force.name not in writes:
            problems.append(f"rule {rule.name} never writes Force.{rule.force.name}")
        for key in rule.counter_keys:
            if key not in WORKFLOW_COUNTED and key not in counted:
                problems.append(f"rule {rule.name} never increments counter {key}")
    problems += [f"cascade counts unknown key {k}" for k in sorted(counted - set(keys))]
    return problems


def test_the_cascade_writes_every_rules_force_and_counters() -> None:
    assert completeness_problems(inspect.getsource(forcing.apply_post_reason_cascade)) == []
```

**Alternative considered:** per-block analysis (match each `_down("<rule>")` guard to its body). Rejected: the guards take four different syntactic shapes (`if not _down(...)`, a conditional expression for walk-stack, a `and` chain for formulate), so a block matcher would be brittle; the presence check is robust and D15 replaces it. **Trade-offs:** presence, not placement, is checked (a rule could write another rule's force; the order and equivalence tests catch that).

---

##### D15: Trigger/apply protocol and one loop over `RULES`

**Decision:** Each rule gets a trigger adapter `trigger(ctx) -> Fire | None` registered in a `TRIGGERS` map keyed by rule name; the cascade becomes one loop. `Fire` carries everything a rule can do: an override (action and parameters), the counter to bump, extra payload keys, an entry to stamp consumed at commit, and a consumed-reference for the workflow. The special cases map onto `Fire` without special code in the loop: the requery order that is budget-exhausted is `Fire(next_action=None, consume=entry)` (effects only); smallest-fetch returns its kind counter and payload in `Fire`; the pivot counter is `None` below lv7.

```python
# src/investigation_agent_platform/application/worker/workflows/forcing.py
@dataclass(frozen=True)
class CascadeContext:
    state: PostReasonCascadeState
    reason: dict[str, Any]  # current dispatch data; earlier rules may have rewritten it
    counts: Mapping[str, int]
    history: list[Any]
    separate: bool  # lv6 carrier separation
    pivot_v2: bool  # lv7 hardened pivot

    def count(self, key: str) -> int:
        try:
            return int(self.counts.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0


@dataclass(frozen=True)
class Fire:
    """What one rule did: an override, effects only (``next_action is None``), or both."""

    next_action: str | None
    action_parameters: dict[str, Any] | None = None
    counter: str | None = None  # key in COUNTER_KEYS to bump
    extra: Mapping[str, Any] = field(default_factory=dict)  # payload keys stamped on the reason data
    consume: dict[str, Any] | None = None  # history entry stamped consumed at commit
    consumed_ref: dict[str, Any] | None = None  # carrier the workflow stamps after dispatch


Trigger = Callable[[CascadeContext], Fire | None]


def _entry_at(history: list[Any], index: Any) -> dict[str, Any] | None:
    try:
        entry = history[index]
    except (TypeError, IndexError, KeyError):
        return None
    return entry if isinstance(entry, dict) else None


def _t_pivot(ctx: CascadeContext) -> Fire | None:
    targets = _recent_collection_surfaces(ctx.history, separate_carriers=ctx.separate)
    turns_left = int(ctx.state.max_iterations or 0) - int(ctx.state.iteration or 0)
    out = _should_force_pivot_recollect(
        ctx.state.prev_focus, ctx.reason, turns_left, targets,
        preserve_proposal=ctx.pivot_v2, forced_count=ctx.count("pivot"),
    )
    if out is None:
        return None
    return Fire(out["next_action"], out["action_parameters"], counter="pivot" if ctx.pivot_v2 else None)


def _t_walk_stack(ctx: CascadeContext) -> Fire | None:
    params = _should_force_walk_stack(ctx.state.knowledge_seed, ctx.reason, ctx.history, ctx.count("walk_stack"))
    return Fire("WALK_STACK", params, counter="walk_stack") if params else None


def _t_requery(ctx: CascadeContext) -> Fire | None:
    out = _should_force_order_requery(ctx.history, recorded_attempt_summaries(ctx.history or []) or [])
    if out is None:
        return None
    entry = _entry_at(ctx.history, out.get("order_index"))
    if out.get("mark_consumed"):  # budget-exhausted: retire the carrier, dispatch nothing
        return Fire(None, consume=entry) if entry is not None else None
    return Fire(out["action_type"], out["action_parameters"], consumed_ref=entry)


def _t_floor(ctx: CascadeContext) -> Fire | None:
    s = ctx.state
    out = _explore_floor_target(
        ctx.reason, int(s.evidence_banked_total or 0), s.triage_route, s.description,
        s.knowledge_seed, ctx.count("floor"), history=ctx.history,
    )
    return Fire(out["next_action"], out["action_parameters"], counter="floor") if out else None


def _t_formulate(ctx: CascadeContext) -> Fire | None:
    s = ctx.state
    if not _should_force_formulate(
        int(s.iteration or 0), int(s.formulated_count or 0), ctx.reason.get("hypotheses_evaluated"),
        int(s.evidence_banked_total or 0), ctx.count("formulate"),
    ):
        return None
    return Fire("FORMULATE_HYPOTHESIS", {}, counter="formulate",
                extra={ForcePayload.FORMULATE_RULE.value: "formulate"})


def _t_synthesis(ctx: CascadeContext) -> Fire | None:
    s = ctx.state
    if not _should_force_synthesis(
        ctx.history, int(s.evidence_banked_total or 0), int(s.formulated_count or 0),
        ctx.count("synthesis"), ctx.reason,
    ):
        return None
    return Fire("FORMULATE_HYPOTHESIS", {}, counter="synthesis",
                extra={ForcePayload.FORMULATE_RULE.value: "synthesis"})


def _t_reread(ctx: CascadeContext) -> Fire | None:
    path = _reread_target(ctx.reason, ctx.history, ctx.count("reread"))
    return Fire("GET_CODE", {"file_path": path}, counter="reread") if path else None


def _t_callsite(ctx: CascadeContext) -> Fire | None:
    out = _callsite_target(
        ctx.reason, ctx.history, ctx.count("callsite"),
        tenant_id=ctx.state.tenant_id, investigation_id=str(ctx.state.investigation_id),
    )
    return Fire(out["next_action"], out["action_parameters"], counter="callsite") if out else None


def _t_log_search(ctx: CascadeContext) -> Fire | None:
    params = _should_force_log_search(str(ctx.state.description or ""), ctx.reason, ctx.history, ctx.count("log_search"))
    return Fire("SEARCH_LOGS", params, counter="log_search") if params else None


def _t_replan(ctx: CascadeContext) -> Fire | None:
    out = _should_force_replan(
        str(ctx.state.description or ""), ctx.reason, ctx.history, ctx.count("replan"),
        separate_carriers=ctx.separate,
    )
    return Fire(out["next_action"], out["action_parameters"], counter="replan") if out else None


def _t_smallest_fetch(ctx: CascadeContext) -> Fire | None:
    orders: list[dict[str, Any]] = []
    for evt in reversed(ctx.history or []):  # newest feed carrier only (a tombstone supersedes older feeds)
        if isinstance(evt, dict) and evt.get(HistoryKey.KIND.value) == "work_order_feed":
            raw = evt.get("orders")
            orders = [o for o in raw if isinstance(o, dict)] if isinstance(raw, list) else []
            break
    for kind in SMALLEST_FETCH_ORDER:
        picked = _pick_gap_order(orders, kind=kind, history=ctx.history,
                                 forced_count=ctx.count(f"smallest_fetch_{kind.lower()}"))
        if picked is None:
            continue
        gap_id = str(picked.gap_id or "")
        return Fire(
            picked.action_type, dict(picked.action_parameters or {}),
            counter=f"smallest_fetch_{kind.lower()}",
            extra={
                ForcePayload.SMALLEST_FETCH_KIND.value: kind,
                ForcePayload.SMALLEST_FETCH_GAP_ID.value: gap_id,
                "smallest_fetch_context": {"kind": kind, "gap_id": gap_id, "claim_ref": str(picked.claim_ref or "")},
            },
        )
    return None


TRIGGERS: dict[str, Trigger] = {
    "pivot": _t_pivot, "walk_stack": _t_walk_stack, "requery": _t_requery, "floor": _t_floor,
    "formulate": _t_formulate, "synthesis": _t_synthesis, "reread": _t_reread,
    "callsite": _t_callsite, "log_search": _t_log_search, "replan": _t_replan,
    "smallest_fetch": _t_smallest_fetch,
}
```

```python
# the cascade body (replaces the eleven inline blocks; the entry and commit code are unchanged)
        reason = dict(state.reason_data or {})
        reason_before = reason  # for the lv7 anchor logic below
        counts = dict(state.counts) if isinstance(state.counts, dict) else {}
        pending_consume: list[dict[str, Any]] = []
        consumed_ref: dict[str, Any] | None = None
        fired_counter: dict[Force, str] = {}  # force -> counter bumped by the rule that wrote it
        fired: dict[str, Fire] = {}
        for rule in RULES:
            if _stands_down(rule.name, reason, is_defer=_is_defer, absence_forced=_absence_forced, d6_fired=_d6_fired):
                continue
            ctx = CascadeContext(state, reason, counts, history, _separate, _pivot_v2)
            fire = TRIGGERS[rule.name](ctx)
            if fire is None:
                continue
            if fire.consume is not None:
                pending_consume.append(fire.consume)
            if fire.next_action is None:  # effects only: no dispatch, no flag, no counter
                continue
            reason = {**reason, "next_action": fire.next_action,
                      "action_parameters": dict(fire.action_parameters or {}),
                      rule.force.value: True, **fire.extra}
            fired[rule.name] = fire
            if fire.counter:
                counts[fire.counter] = ctx.count(fire.counter) + 1
                fired_counter[rule.force] = fire.counter
            for cancelled in rule.cancels:  # F5: overwritten dispatch means the earlier rule did not happen
                reason[cancelled.value] = False
                refund = fired_counter.pop(cancelled, None)
                if refund:
                    counts[refund] = max(0, counts.get(refund, 0) - 1)
            if fire.consumed_ref is not None:
                consumed_ref = fire.consumed_ref
```

**Alternative considered:** put the callable in the `Rule` itself so `RULES` is the only table. Rejected: `forcing_rules.py` is a pure leaf imported by activity code and must not import the trigger helpers; `TRIGGERS` lives beside them and a test pins `set(TRIGGERS) == {r.name for r in RULES}`. **Alternative considered:** a class hierarchy of rules. Rejected: eleven stateless pure functions need no object model. **Trade-offs:** `Fire.consume` and `Fire.consumed_ref` exist for one rule (requery); that is the cost of expressing "effects without dispatch" inside a uniform loop.

---

##### D16: Golden master, recorded before D15

**Decision:** Record the current cascade's behavior over a deterministic corpus before touching it, and compare after. The corpus is the cross product of small pools (about 14 proposals, 12 histories including inert, live-requery and live-feed carriers, the five pre-cascade combinations of defer/D6/absence, pins lv5/lv6/lv7, counter variants including the caps, intake frames present or absent, three evidence totals and four iterations), sampled to about 600 cases with a fixed seed. Each case records `reason_data`, the returned counts, the focus anchor, the consumed-reference index and which history entries were stamped consumed. A coverage test requires every `Force` to fire in at least five cases and each pre-cascade combination in at least twenty, so a thin corpus cannot give false comfort.

```python
# tests/unit/cascade_corpus.py  (shared by the recorder and the test)
import copy
import itertools
import random

from investigation_agent_platform.application.worker.workflows.forcing import (
    PostReasonCascadeState, apply_post_reason_cascade,
)

PROPOSALS = [...]   # 14 reason dicts: GET_CODE on a new/old file, with/without lines, SEARCH_CODE with and
HISTORIES = [...]   # without a query, FIND_SYMBOL, VERIFY with definition language, FORMULATE, CONCLUDE, ...
PRE = [(False, False, False), (True, False, False), (False, True, False), (False, False, True), (True, True, True)]
PINS = ["lv5", "lv6", "lv7"]


def corpus(n: int = 600, seed: int = 20261031):
    space = list(itertools.product(range(len(PROPOSALS)), range(len(HISTORIES)), PRE, PINS, range(3), range(4)))
    return random.Random(seed).sample(space, min(n, len(space)))


def run_case(case) -> dict:
    p, h, (defer, d6, absence), pin, banked_i, iter_i = case
    history = copy.deepcopy(HISTORIES[h])
    state = PostReasonCascadeState(
        reason_data=copy.deepcopy(PROPOSALS[p]), action_history=history,
        evidence_banked_total=(0, 2, 9)[banked_i], triage_route="BUG",
        description="orders fail with an error in the order filter", knowledge_seed={},
        max_iterations=10, iteration=(0, 1, 3, 8)[iter_i],
        prev_focus={"surface": "svc/old.py", "surface_kind": "file", "line_start": None, "line_end": None},
        is_defer=defer, d6_fired=d6, absence_forced=absence, counts={}, formulated_count=banked_i % 2,
        tenant_id="tenant-a", investigation_id="inv-1", loop_version=pin,
    )
    out = apply_post_reason_cascade(state)
    return {
        "reason": out.reason_data, "counts": dict(out.counts), "focus": out.prev_focus,
        "consumed_ref": history.index(out.consumed_ref) if out.consumed_ref in history else None,
        "stamped": [i for i, e in enumerate(history) if isinstance(e, dict) and e.get("consumed") is True],
    }
```

The recorder (`python tests/unit/record_cascade_golden.py`) writes `tests/fixtures/cascade_golden_v1.json` with a header naming the base SHA; the test compares `run_case` for every case. The recorder is run once, before D15, and never again for D15 (a diff means the refactor is wrong). **Alternative considered:** keep the old cascade as an oracle function in the test. Rejected: a 330-line frozen copy is exactly the legacy path the repo contract forbids; recorded data is not code. **Trade-offs:** a golden file is opaque on failure; the test prints the case tuple and the differing keys.

---

##### D17: `Rule.cancels` for the F5 walk-stack-over-pivot cancel

**Decision:** The one hard-coded interaction becomes data: `Rule("walk_stack", ..., exempt={PIVOT_RECOLLECT: ...}, cancels=frozenset({Force.PIVOT_RECOLLECT}))`. The loop (D15) clears the cancelled flag (unconditionally, matching today's `forced_pivot_recollect: False` on every walk-stack fire) and refunds the counter the cancelled rule bumped, if any. The contract gains one check: `cancels` must be a subset of `exempt` (a rule that overwrites an earlier force cannot also stand down on it).

```python
# src/investigation_agent_platform/domain/investigation/forcing_rules.py  (Rule gains one field)
    cancels: frozenset[Force] = frozenset()

# check_rule_contract gains:
        for force in sorted(rule.cancels - set(rule.exempt), key=lambda f: f.value):
            problems.append(f"rule {rule.name} cancels {force.value} but does not exempt it")
```

**Alternative considered:** leave F5 as a special case after the loop. Rejected: it is the only reason the loop would not be uniform. **Trade-offs:** none beyond one more table field and one contract line.

#### Version pinning / migration

**No pin needed.** D14 and D16 are tests; D15 and D17 are behavior-preserving, which is proven rather than asserted: the golden master (about 600 cases across lv5, lv6 and lv7) and the F10 replay corpus (unmodified transcripts, input parity) must both stay green, and the 83 existing F8 tests continue to pass. F8 follow-ups (c) and (d) are recorded as accepted. No stored shape changes (the history, shadow and telemetry keys are unchanged by this item).

#### Test cases

Written first: F8-T04 is recorded on the current tree (green), then F8-T05, T06, T07, T08, T09, T10, T13 go red on import and turn green with D15/D17; F8-T01 and T02 go green with D14.

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| F8-T01 | Cascade writes every rule's force and counters | unit (AST) | real cascade source and `RULES` | `completeness_problems(source)` | empty | Green on today's tree (D14) |
| F8-T02 | Completeness contract can fail | unit | synthetic cascade source with the `Force.CALLSITE` write deleted and a `_counts["smallest_fetch_causl"]` typo | `completeness_problems` | names `callsite` and the unknown key | Negative control: the deleted write is the line that makes it fail |
| F8-T03 | Corpus is not thin | unit | the recorded corpus | run and count | every `Force` fires in at least 5 cases; each pre-cascade combination in at least 20; all three pins present | Guards the golden master's value |
| F8-T04 | Golden master equality | unit | `cascade_golden_v1.json` recorded pre-D15 | `run_case` for every case | identical `reason`, `counts`, `focus`, `consumed_ref`, `stamped` | Passes before and after D15; the equivalence proof. Negative control: flip one guard in the cascade (for example drop `Force.REQUERY` from the floor rule's stand-down set) and several cases differ |
| F8-T05 | Triggers match the table | unit | `TRIGGERS` and `RULES` | compare | same names, no extras, evaluation order comes from `RULES` | Red before D15 |
| F8-T06 | A new rule needs no cascade edit | unit | monkeypatch `RULES`/`TRIGGERS` with a trailing `Rule("probe", ...)` and a trigger that fires | run the cascade | the probe rule fires, writes its force and its counter | This is the structural closure of (e); negative control: remove the loop and the probe never fires |
| F8-T07 | Rule without a trigger is rejected | unit | table with a rule missing from `TRIGGERS` | `check_triggers(RULES, TRIGGERS)` (new, beside `check_rule_contract`) | one problem per missing or extra name | Fail-closed at import in `forcing.py` |
| F8-T08 | `cancels` contract | unit | bad table (`cancels` without `exempt`); real `RULES` | `check_rule_contract` | bad table flagged; real table: walk-stack cancels pivot and exempts it | Extends the F8 bad-table tests |
| F8-T09 | Cancel refunds only what was counted | unit | lv7 state with frames and a file proposal (pivot fires then walk-stack wins); a state where pivot never fired; an lv6 state | cascade | first: `forced_pivot_recollect is False` and `counts["pivot"] == 0`; second: flag still `False`, counts unchanged; lv6: no counter involved | Edge: unconditional `False` write is pinned |
| F8-T10 | Requery effects-only fire | unit | carrier whose attempt budget is exhausted, then a floor-eligible proposal | cascade | the carrier is stamped consumed at commit, no requery flag, the floor rule still evaluates | Equivalence of the `mark_consumed` branch |
| F8-T11 | Trigger fault passes the proposal through uncommitted | unit | trigger for a later rule raises after an earlier rule fired and a carrier was queued for consumption | cascade | proposal unchanged, `counts` and stamps untouched, one WARNING logged | F7 retained in the loop |
| F8-T12 | Smallest-fetch counters are per kind | unit | feed carrier with `HISTORY` and `BEHAVIOR` gaps | cascade | only `smallest_fetch_history` increments; no path can increment `smallest_fetch_causal` for another kind | Removes the old `else:` default |
| F8-T13 | Cascade has no per-rule blocks | unit (AST) | `apply_post_reason_cascade` source | count `_down(` literals and `for ... in RULES` loops | zero and one | Structural pin replacing the F8 order test |
| F8-T14 | Replay corpus unchanged | workflow-harness | F10 S1-S3 transcripts, unmodified | golden replay | parity and expected dispatch hold | Command-neutrality proof for the workflow |
| F8-T15 | Yielding rules never evaluate their trigger | unit | spy triggers; states for defer, absence and D6 | cascade | zero trigger calls | Pins accepted follow-up (d) uniformly |

#### Rollout, risks, open owner decisions

Order: D14 with the F9 tranche; D16 recorded in its own change (tests and data only); D15 and D17 as one tranche. Risks: a subtle equivalence break (mitigated by D16 plus the replay corpus; stop on any diff); trigger adapters drifting from helper signatures (mypy and the 83 F8 tests). Requires owner approval: none. **Open owner decisions:** O-F8-1 proceed with B (recommended) or stay at A.

### Item F2 follow-ups: a structural guard against positional readers of `action_history`

#### Status

**Open — follow-up of IMPLEMENTED F2** (lv6). Severity: consider. Dependencies: coordinate with F8-D15 (the cascade context carries `turns` instead of a `separate` flag) and F10 (follow-up (b) is closed by the replay scenarios S1 and S5). Gating: none. Follow-up (d) (an lv5-or-older run still shows carriers in its reasoner prompt) is **accepted by design** and gets a sunset rule (D21). Follow-up (c) is F8-F10, designed above.

#### Verified problem

An AST probe of subscripts over history-like names (`action_history`, `history`, `entries`, `source`, `recent_actions`) found exactly the sites that exist today: `_should_force_replan` (`entries[-2:]`), `_recent_collection_surfaces` (`source[-N:]`), `_should_force_order_requery` (two index loops into the full list, which is correct because it stamps carriers in place), and in the workflow four `action_history[-1]` stamp sites (the turn just appended, correct) plus the two `_turn_view()[...]` reads. Every site is correct today. A metamorphic probe on the real cascade inserted inert carriers (an empty gap-feed tombstone and a consumed requery carrier) at random positions: 0 of 720 cases changed any output at lv7, and 90 of 720 changed at lv5. So the property "inert carriers never change a decision" holds from lv6 and is violated by lv5 exactly as designed (the F2 tests' negative control).

What is missing is a guard: nothing stops the next reader from writing `history[-3:]` and reading a carrier as a turn, and the per-call-site `separate_carriers` boolean threaded through two helpers is the thing a new reader will forget.

#### Design

##### D18: A typed `Turns` view and a single pin site

**Decision:** Introduce `Turns = NewType("Turns", list)` and `turn_view(history, loop_version) -> Turns`, the only place the lv6 pin is consulted for history reads. Positional readers take `Turns`, so mypy rejects a raw list at a call site, and the `separate_carriers` flag disappears. Carrier readers keep the full list (they index into it and stamp entries in place) and are named as such in D20. The older-pin branch is explicit: lv5 and older, unreadable and unknown pins get the raw dict entries (carriers read as turns, intentionally).

```python
# src/investigation_agent_platform/application/worker/workflows/forcing.py
from typing import NewType

#: A history view that is safe to index positionally ("the last N turns"). Build it only with
#: ``turn_view``; the carrier readers use the full list instead (they index into it and stamp in place).
Turns = NewType("Turns", list)


def turn_view(history: Any, loop_version: Any) -> Turns:
    """The turn entries a positional reader may index (pure, never raises).

    From lv6 carriers are removed; an older, unreadable or unknown pin keeps the raw dict entries,
    so carriers read as turns exactly as those runs always did (review F2, accepted by design).
    """
    try:
        if _carriers_separated(loop_version):
            return Turns(turn_entries(history))
        return Turns([e for e in (history or []) if isinstance(e, dict)])
    except Exception:
        return Turns([])


def _should_force_replan(description: str | None, reason_data: dict | None, turns: Turns, forced_count: int) -> dict | None:
    ...  # body unchanged except: entries = turns; the separate_carriers branch is deleted


def _recent_collection_surfaces(turns: Turns, limit: int = 10) -> list[str]:
    ...  # body unchanged except: entries = turns[-max(1, int(limit or 10)) :]
```

```python
# run_investigation.py: the local _turn_view and _split_carriers are deleted
recent_actions=turn_view(action_history, loop_version)[-10:]       # reasoner and verify inputs
actions_used=len(turn_view(action_history, loop_version))
_prev = (turn_view(action_history, loop_version) or [{}])[-1]
```

**Alternative considered:** a second list for carriers. Rejected in F2 (the carrier readers index into the full list and stamp in place; splitting changes every reader and the checkpoint shape). **Alternative considered:** a `list` subclass that raises on slicing. Rejected: runtime magic over a structure that is serialized into activity payloads. **Trade-offs:** about a dozen call sites in tests change from `separate_carriers=True` to `turn_view(history, "lv7")`, and the three workflow-wiring source pins in `test_f2_carrier_separation.py` (`_turn_view(` count, `recent_actions=_turn_view()[-10:]`, `action_history[-1]["requery_consumed"]`) are rewritten against `turn_view` and `_this_turn`; at lv5 the view now drops non-dict items where `_recent_collection_surfaces` used to count them in its window (the workflow only appends dicts, so no production difference; F8-T04's golden master contains none).

---

##### D19: Stamp the appended turn through a reference

**Decision:** The workflow keeps the dict it appends and updates that object, so no code reads `action_history[-1]` and the last positional index in the workflow disappears.

```python
# run_investigation.py
_this_turn = {"iteration": iteration, "action": str(_reason_data.get("next_action", "?")), ...}  # the existing literal
action_history.append(_this_turn)
...
_this_turn.update({"parameters": ..., "outcome_status": ..., **forced_stamp(_reason_data), ...})
if _reason_data.get(Force.REQUERY.value) and isinstance(_consumed_order_ref, dict):
    _consumed_order_ref["consumed"] = True
    _this_turn["requery_consumed"] = {...}
_decision_summary = _reason_summary({**(reason_res.data or {}), **_this_turn})
```

Equivalence: the carriers are appended after the verify activity, later in the iteration than every one of these sites, so at each site `action_history[-1]` was already this turn's dict. **Alternative considered:** a helper `last_turn(history)`. Rejected: a reference cannot index the wrong element. **Trade-offs:** none.

---

##### D20: An AST guard for positional access, with a reasoned allowlist

**Decision:** A test fails on any positional access to a history-like name (a non-string subscript, `reversed(x)`, `enumerate(x)`, `range(len(x))`) in the history-handling modules unless the enclosing function takes a `Turns` parameter (it was handed the safe view) or appears in `CARRIER_READERS` with a reason. A stale allowlist entry (the function no longer exists or no longer indexes) also fails, so the list cannot rot.

```python
# tests/unit/test_f2_positional_history_guard.py
from __future__ import annotations

import ast
import pathlib

BASE = pathlib.Path("src/investigation_agent_platform/application")
MODULES = [
    BASE / "worker/workflows/forcing.py", BASE / "worker/workflows/run_investigation.py",
    BASE / "evidence/retrieval_ledger.py", BASE / "investigation/shadow_capture.py",
]
HISTORY_NAMES = {"action_history", "history", "_history", "entries", "recent_actions", "source", "turns"}
CARRIER_READERS = {
    "_should_force_order_requery": "indexes into the full list to find and stamp the newest unconsumed REQUERY carrier",
    "_smallest_fetch_already_covered": "scans for a gap-id coverage stamp by key, newest first",
    "_t_smallest_fetch": "reads the newest feed carrier (a tombstone supersedes older feeds)",
}


def _is_history(node: ast.AST) -> bool:
    name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""
    return name in HISTORY_NAMES


def positional_problems(source: str, allow: dict[str, str] = CARRIER_READERS) -> list[str]:
    tree, problems = ast.parse(source), []
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        takes_turns = any("Turns" in ast.unparse(a.annotation) for a in fn.args.args if a.annotation)
        for node in ast.walk(fn):
            hit = None
            if isinstance(node, ast.Subscript) and _is_history(node.value):
                if not (isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
                    hit = ast.unparse(node)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"reversed", "enumerate"}:
                if node.args and any(_is_history(n) for n in ast.walk(node.args[0])):
                    hit = ast.unparse(node)
            if hit and not takes_turns and fn.name not in allow:
                problems.append(f"{fn.name}:{node.lineno} positional read {hit}")
    return problems


def test_no_positional_history_reads_outside_carrier_readers() -> None:
    for path in MODULES:
        assert positional_problems(path.read_text(encoding="utf-8")) == [], path.name
```

**Alternative considered:** a lint-only convention in the docs. Rejected: F2 exists because a convention was not enough. **Alternative considered:** mypy alone through `Turns`. Rejected as the only guard: the ratchet baseline (506) tolerates new errors being offset, and carrier readers legitimately take the raw list. **Trade-offs:** name-based; a reader that aliases the history to another name evades the AST guard, which is why D21's behavioral property exists as the second net.

---

##### D21: Metamorphic property and the lv5 sunset rule

**Decision:** A deterministic enumeration (seeded `random.Random`, no timing dependence) inserts inert carriers at every kind of position into a set of base histories and requires the cascade output to equal the carrier-free output, under lv6 and lv7; the same enumeration at lv5 must find a difference (the negative control that proves the property can fail). A live carrier must still dispatch wherever it sits. **Sunset:** a loop version is retired when a read-only grouped count of the pin on investigation metadata shows no row carrying it (the lv1 precedent in `loop_version.py`); retiring lv2-lv5 together makes `_carriers_separated` constant and deletes the lv5 branch of `turn_view` and this test's negative control in the same change. Running that count touches the dev database read-only (**requires owner approval**).

```python
# tests/unit/test_f2_inert_carrier_property.py
import copy
import random

from investigation_agent_platform.application.worker.workflows.forcing import (
    PostReasonCascadeState, apply_post_reason_cascade,
)

INERT = [
    {"kind": "work_order_feed", "order_kind": "GAP_FILL", "iteration": 2, "turn_issued": 2, "orders": []},
    {"kind": "work_order", "order_kind": "REQUERY", "iteration": 2, "turn_issued": 2, "consumed": True,
     "claim_ref": "c", "query": "q", "scope": "", "max_attempts": 3},
]
PROPOSALS = [...]   # SEARCH_CODE with a query, GET_CODE on a new file, VERIFY with definition language
HISTORIES = [...]   # two empty searches; a read, a log search and a search; two searches then a read


def _run(history, pin, proposal):
    state = PostReasonCascadeState(
        reason_data=dict(proposal), action_history=copy.deepcopy(history), evidence_banked_total=2,
        triage_route="BUG", description="where is the order filter implemented", knowledge_seed={},
        max_iterations=10, iteration=4,
        prev_focus={"surface": "svc/old.py", "surface_kind": "file", "line_start": None, "line_end": None},
        is_defer=False, d6_fired=False, absence_forced=False, counts={}, formulated_count=1,
        tenant_id="t", investigation_id="i", loop_version=pin,
    )
    out = apply_post_reason_cascade(state)
    return out.reason_data, dict(out.counts), out.prev_focus


def _carrier_sensitive(pin) -> int:
    rnd, diffs = random.Random(7), 0
    for base in HISTORIES:
        for proposal in PROPOSALS:
            reference = _run(base, pin, proposal)
            for _ in range(60):
                history = list(base)
                for carrier in rnd.sample(INERT, rnd.randint(1, 2)):
                    history.insert(rnd.randint(0, len(history)), copy.deepcopy(carrier))
                diffs += _run(history, pin, proposal) != reference
    return diffs


def test_inert_carriers_never_change_a_decision_from_lv6() -> None:
    assert _carrier_sensitive("lv7") == 0 and _carrier_sensitive("lv6") == 0


def test_the_property_can_fail_at_lv5() -> None:
    assert _carrier_sensitive("lv5") > 0   # probe: 90 of 720; lv5 keeps carriers-as-turns by design
```

**Alternative considered:** Hypothesis. Available (6.169) and fine for exploration, but derandomized enumeration gives the same coverage with no shrinking machinery and no flake risk in CI. **Trade-offs:** the property covers inert carriers only; live carriers change dispatch by design and are covered by F2-T11.

#### Version pinning / migration

**No pin needed.** D18-D20 are structure and tests with identical decisions at every pin (the lv5 identity branch is kept explicitly inside `turn_view`); D21 is a test plus a retirement procedure. The lv6 pin machinery is unchanged. Evidence: the F2 suite (30 tests), the F8-D16 golden master and the F10 scenarios S1 and S5 stay green unmodified except for the `separate_carriers` call sites, which become `turn_view(...)`.

#### Test cases

Written first: F2-T04, T05, T06 and T08 are red before D18-D20; F2-T09, T10 and T11 are regression pins that pass on the current tree (the property is true today) and exist to catch the next reader.

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| F2-T01 | `turn_view` from lv6 drops carriers | unit | history `[turn, feed, turn, requery, turn]`, pins lv6 and lv7 | `turn_view` | three turns in order; input list unmutated | Happy path |
| F2-T02 | `turn_view` below lv6 keeps carriers | unit | same history, pins lv5, lv3, lv2 | `turn_view` | all five dict entries | Version-pin routing: older pin keeps the old behavior |
| F2-T03 | Unknown or unreadable pin keeps the old view | unit | pins `None`, `""`, `"lv999"`, `42` | `turn_view` | raw dict entries, never raises | Fail-safe, matches `_carriers_separated` |
| F2-T04 | Readers require the typed view | unit | `inspect.signature` of `_should_force_replan` and `_recent_collection_surfaces` | inspect | no `separate_carriers` parameter; the history parameter is annotated `Turns` | Red before D18 |
| F2-T05 | Guard is green on the tree | unit (AST) | the four modules | `positional_problems` | empty | Red before D19 (`action_history[-1]` sites) |
| F2-T06 | Guard can fail | unit | synthetic source: `def f(history): return history[-2:]`; and the same with `turns: Turns` | `positional_problems` | first is flagged, second is not | Negative control: the flagged line is the one a future reader would add |
| F2-T07 | Allowlist cannot rot | unit | `CARRIER_READERS` | for each name, find the function and require a positional read in it | every entry still exists and still indexes | A stale entry fails the test |
| F2-T08 | Workflow stamps through a reference | unit (AST) | `run_investigation.py` | search for `action_history[-1]` | none; `_this_turn` is defined once and used for every stamp | Red before D19 |
| F2-T09 | Inert carriers never change a decision (lv6, lv7) | unit | enumerated histories and proposals | `_carrier_sensitive("lv7")` and `("lv6")` | 0 | 720 cases per pin |
| F2-T10 | The property can fail at lv5 | unit | same enumeration | `_carrier_sensitive("lv5")` | greater than 0 | Negative control; if it ever reads 0 the property test has gone vacuous |
| F2-T11 | Live carriers dispatch wherever they sit | unit | one live REQUERY carrier placed first, middle and last in a history | cascade | the order dispatches (`forced_requery`) in all three positions | Carrier readers must stay position-independent |
| F2-T12 | Malformed histories | unit | history containing `None`, an int, a string and a half-formed carrier | `turn_view` and both readers | non-dicts dropped, no exception, readers return their no-fire result | Malformed input |
| F2-T13 | lv5 retirement (conditional) | unit | only in the retirement tranche | `resolve_loop_version({"loop_version": "lv5"})` | raises `UnknownLoopVersion`; `_carriers_separated` is constant true; this file's lv5 control is deleted | Not run until the owner-approved count shows zero lv5-or-older rows |

#### Rollout, risks, open owner decisions

Land after F10-B and F9 so the typed view is proven command-neutral; with F8-D15 the cascade context carries `turns`. If F8-D15 has not landed, `apply_post_reason_cascade` (the inline smallest-fetch feed scan, `reversed(_history or [])`) is a carrier reader and is added to `CARRIER_READERS` until D15 moves the scan into `_t_smallest_fetch`; the guard is red on today's tree for that function, `_should_force_replan`, `_recent_collection_surfaces` and the four workflow stamp sites (probe). Risks: call-site churn in tests (mechanical); name-based guard evasion (D21 is the second net). **Open owner decisions:** O-F2-1 approve the read-only pin count that would let lv2-lv5 retire (the only way follow-up (d) ever goes away).

### Item F4 follow-ups: pivot measurement, the cap decision rule, and the recorded pivot precedence

#### Status

**Open — follow-up of IMPLEMENTED F4** (lv7), measurement tranche. Severity: consider. Dependencies: **F9 is a hard dependency** (today `forced_pivot_count` is not persisted, so nothing about the pivot can be measured); F10-B for the workflow-level counter test. Gating: the cap and scoped-search *changes* are gated on the data and the decision rule in D23; the live measurement (follow-up b, "fold into the T6 gate") is **Gated** on the owner-deferred Tranche-6 battery. Follow-up (c) (pivot absent from observability) folds into F9 (D4); follow-up (d) (no replay test) is F10 (scenario S1); follow-up (f) is **Decided** (D25, recorded, not redesigned).

#### Verified problem

- **(a) The cap is unmeasured and unmeasurable.** `PIVOT_RECOLLECT_CAP = 2` "follows the other rules' precedent". The workflow keeps `forced_pivot_count` and sends it in `loop_telemetry`, but `_derive_loop_telemetry` drops it (F9 evidence). Even after F9, a count of 2 cannot distinguish "the model pivoted twice and stopped" from "the model wanted to pivot five times and the cap withheld three"; a capped pivot passes silently through `_should_force_pivot_recollect` returning `None`.
- **Scoped `SEARCH_CODE` never pivots from lv7.** Probe on the real cascade (lv7, previous focus `svc/old.py`, proposal `SEARCH_CODE` with `file_path="svc/new.py"` and `query="SampleOrderFilter"`): the dispatch is unchanged, `forced_pivot_recollect` is not set, no counter moves, and the anchor moves to `svc/new.py`. So the model's own scoped search is the collection, and a later `GET_CODE` of `svc/new.py` is no shift. Consequence: a scoped search banks snippets, not a range read; a definition claim later needs the P0-2 re-read rule to get the lines. Whether that costs outcomes is unknown.
- **At the cap the waiting requery order fires.** Probe: with a REQUERY carrier pending and a different-file proposal, `counts == {}` gives `forced_pivot_recollect=True` and the order waits (counted pivot 1); with `counts == {'pivot': 2}` the same state dispatches the requery order (`forced_requery=True`). This is what bounds the cost of decision (f) to at most `PIVOT_RECOLLECT_CAP` turns per run.
- **(f) Decided 2026-10-31:** a pending REQUERY order does not outrank a genuine model pivot. Not redesigned here.

#### Design

##### D22: `PivotDecision` with a reason, and three measurement counters

**Decision:** `_pivot_decision` returns the override and why it did or did not fire; `_should_force_pivot_recollect` becomes a one-line wrapper (a stable pure API with many callers, not a compatibility shim). The cascade counts two suppressions and the workflow counts productivity: `pivot_capped` (a pivot that would have fired but the cap withheld it), `pivot_scoped_skipped` (a pivot-eligible shift the lv7 scoped-search rule left alone), `pivot_productive` (a fired pivot whose fetch banked at least one row). They join `EXTRA_COUNTER_KEYS`, so F9's single counter map stamps `forced_pivot_capped_count`, `forced_pivot_scoped_skipped_count` and `forced_pivot_productive_count` with no further plumbing. The cap check moves from the top of the rule to just before it returns an override, so "capped" means "would have fired", and no dispatch decision changes.

```python
# src/investigation_agent_platform/application/worker/workflows/forcing.py
class PivotReason(StrEnum):
    FIRED = "fired"
    LIVENESS = "liveness"  # fewer than two turns left
    NO_SHIFT = "no_shift"  # no focus, no previous focus, same surface, or only a window refinement
    DAMPED = "damped"  # the surface was collected recently
    SCOPED_SEARCH = "scoped_search"  # lv7: a SEARCH_CODE that already carries a query is left alone
    CAPPED = "capped"  # lv7: the per-run cap withheld a pivot that would have fired


@dataclass(frozen=True)
class PivotDecision:
    override: dict[str, Any] | None
    reason: PivotReason


def _pivot_decision(
    prev_focus, reason_data, turns_remaining, recent_targets, *, preserve_proposal=False, forced_count=0
) -> PivotDecision:
    """The pivot rule with its reason (pure, never raises). Dispatch decisions equal the old rule's."""
    try:
        if not pivot_can_pay_off(turns_remaining):
            return PivotDecision(None, PivotReason.LIVENESS)
        prev: FocusPoint | None = None
        if isinstance(prev_focus, dict):
            try:
                prev = FocusPoint.model_validate(prev_focus)
            except Exception:
                prev = None
        curr = focus_from_reason_data(reason_data if isinstance(reason_data, dict) else {})
        if curr is None or detect_shift(prev, curr) != FocusShift.PIVOTED:
            return PivotDecision(None, PivotReason.NO_SHIFT)
        recent = {str(r or "") for r in (recent_targets or []) if str(r or "")}
        if f"{curr.surface_kind}:{curr.surface}" in recent:
            return PivotDecision(None, PivotReason.DAMPED)
        if preserve_proposal:
            proposed = str((reason_data or {}).get("next_action", "") or "").upper()
            kept = _proposed_params(reason_data)
            if (
                curr.surface_kind == "file"
                and proposed == "SEARCH_CODE"
                and str(kept.get("query") or "").strip()
            ):
                return PivotDecision(None, PivotReason.SCOPED_SEARCH)
            if _cap_reached(forced_count):
                return PivotDecision(None, PivotReason.CAPPED)
            override = _hardened_override(curr, proposed, kept)  # the existing lv7 rewrite, unchanged
        else:
            override = _legacy_override(curr)  # the existing pre-lv7 rewrite, unchanged
        return PivotDecision(override, PivotReason.FIRED)
    except Exception:
        return PivotDecision(None, PivotReason.NO_SHIFT)


def _cap_reached(forced_count: Any) -> bool:
    try:
        return not isinstance(forced_count, bool) and int(forced_count) >= PIVOT_RECOLLECT_CAP
    except (TypeError, ValueError, OverflowError):
        return False  # a malformed counter leaves the cap inert, as before


def _should_force_pivot_recollect(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
    return _pivot_decision(*args, **kwargs).override
```

```python
# in the pivot trigger (inline today, _t_pivot after F8-D15)
decision = _pivot_decision(...)
if decision.reason is PivotReason.CAPPED:
    _counts["pivot_capped"] = _count("pivot_capped") + 1
elif decision.reason is PivotReason.SCOPED_SEARCH:
    _counts["pivot_scoped_skipped"] = _count("pivot_scoped_skipped") + 1
_pivot_override = decision.override

# run_investigation.py: after the history stamp
if _reason_data.get(Force.PIVOT_RECOLLECT.value) and len(_outcome.get("evidence_ids", []) or []) > 0:
    counters["pivot_productive"] += 1
```

```python
# src/investigation_agent_platform/domain/investigation/forcing_rules.py
EXTRA_COUNTER_KEYS: tuple[str, ...] = (
    "absence_disconfirmation", "pivot_capped", "pivot_scoped_skipped", "pivot_productive",
)
```

**Alternative considered:** log suppression events and count them offline. Rejected: counters are what the stamp carries and what the evaluator reads; a log needs a separate, unversioned scrape. **Alternative considered:** record each pivot as a structured history event. Rejected: the history entry already carries `forced_pivot_recollect` per turn, and the three counters answer the cap question without a new payload. **Trade-offs:** the F8-D16 golden master compares `counts`, which now gains up to two keys on lv7 cases; this is the single sanctioned golden update, reviewed as "only the two new counter keys changed". The counters are meaningful from lv7 only; an lv6 run never reports `pivot_capped` because it has no cap (documented, and F4-T07 pins it).

---

##### D23: The cap-verdict evaluator and the decision rule

**Decision:** A pure evaluator turns rows of recorded telemetry into a verdict, so the decision is a function with tests, not a judgment call. Population: runs pinned lv7 or later that recorded all counters (a row missing a key is excluded, never zero-filled, because pre-F9 rows say nothing). **Insufficient data** below 30 runs. **Raise the cap** only if at least 20 percent of runs were starved (`pivots == cap` and `pivot_capped >= 1`) and, comparing repeats of the same case, at least three cases passed more often when not starved than when starved. **Lower the cap to one** if pivots are mostly unproductive (productive pivots divided by pivots is below 0.5 over at least 20 pivots); this takes precedence, because an unproductive rule should not be widened. Otherwise **keep**. The thresholds are deliberately conservative for a battery of about fourteen live cases times three repeats (about 42 runs): a change needs both volume and replication across cases.

```python
# src/investigation_agent_platform/domain/investigation/pivot_cap_review.py   (new, pure)
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

MIN_RUNS = 30
STARVED_SHARE_TO_RAISE = 0.20
MIN_HARMED_CASES = 3
PRODUCTIVITY_FLOOR = 0.5
MIN_PIVOTS_FOR_PRODUCTIVITY = 20


@dataclass(frozen=True)
class RunRow:
    case_id: str
    pivots: int | None  # forced_pivot_count; None when the run predates the counter
    capped: int | None
    scoped_skipped: int | None
    productive: int | None
    passed: bool | None  # battery verdict for the run, when known


@dataclass(frozen=True)
class CapVerdict:
    verdict: Literal["keep", "raise", "lower", "insufficient"]
    reasons: tuple[str, ...]
    stats: dict[str, float]


def review(rows: list[RunRow], cap: int = 2) -> CapVerdict:
    pop = [r for r in rows if None not in (r.pivots, r.capped, r.productive)]
    n = len(pop)
    stats: dict[str, float] = {"runs": float(n)}
    if n < MIN_RUNS:
        return CapVerdict("insufficient", (f"{n} usable runs, need {MIN_RUNS}",), stats)
    pivots = sum(r.pivots or 0 for r in pop)
    productive = sum(r.productive or 0 for r in pop)
    stats["pivots"] = float(pivots)
    if pivots >= MIN_PIVOTS_FOR_PRODUCTIVITY:
        stats["productive_share"] = productive / pivots
        if stats["productive_share"] < PRODUCTIVITY_FLOOR:
            return CapVerdict("lower", (f"productive share {stats['productive_share']:.2f} below {PRODUCTIVITY_FLOOR}",), stats)
    starved = [r for r in pop if (r.pivots or 0) >= cap and (r.capped or 0) >= 1]
    stats["starved_share"] = len(starved) / n
    by_case: dict[str, dict[bool, list[bool]]] = defaultdict(lambda: {True: [], False: []})
    for r in pop:
        if r.passed is not None:
            by_case[r.case_id][r in starved].append(r.passed)
    harmed = sum(
        1 for sides in by_case.values()
        if sides[True] and sides[False] and (sum(sides[False]) / len(sides[False])) > (sum(sides[True]) / len(sides[True]))
    )
    stats["harmed_cases"] = float(harmed)
    if stats["starved_share"] >= STARVED_SHARE_TO_RAISE and harmed >= MIN_HARMED_CASES:
        return CapVerdict("raise", (f"starved {stats['starved_share']:.2f}, harmed cases {harmed}",), stats)
    return CapVerdict("keep", ("no starvation harm and productive pivots",), stats)
```

Extraction (**requires owner approval**, read-only): a grouped read over the investigation metadata JSON column for `loop_telemetry` keys `forced_pivot_count`, `forced_pivot_capped_count`, `forced_pivot_scoped_skipped_count`, `forced_pivot_productive_count` and `loop_version`, joined to the battery's per-run verdicts by investigation id. The evaluator never touches the database.

**Alternative considered:** raise the cap speculatively (the review's concern about long multi-file investigations). Rejected: the cap's cost is at most two forced turns per run and the pivot's productivity is also unknown; widening an unmeasured rule is the same mistake as narrowing it. **Trade-offs:** thresholds are judgment calls, written down so they can be argued with and changed in one place; the evaluator reports its statistics so a borderline result is visible.

---

##### D24: Conditional cap and scoped-search changes (not enabled until D23 says so)

**Decision:** The cap becomes a function of the pin so that any future change is version-pinned; today it returns 2 for every pin. If D23 returns `raise` or `lower`, the constant is set and the new value applies from the new loop version only; lv7 and older runs keep 2. For the scoped-search question the design is: leave the rule alone, measure `pivot_scoped_skipped`, and only if the same paired-case harm criterion as the cap holds for runs with `pivot_scoped_skipped >= 1`, add a bounded follow-up range read (one `GET_CODE` of the scoped file) from the new version. No code for the follow-up read is written now, because the data decides whether it exists.

```python
# src/investigation_agent_platform/application/worker/workflows/forcing.py
PIVOT_RECOLLECT_CAP = 2  # lv7
#: Set only after the D23 review returns raise or lower; the value applies from LV_F (or its successor).
PIVOT_RECOLLECT_CAP_NEXT: int | None = None


def pivot_cap_for(loop_version: Any) -> int:
    try:
        if PIVOT_RECOLLECT_CAP_NEXT is not None and loop_version_at_least(
            {"loop_version": loop_version}, LOOP_VERSION_LV_F
        ):
            return PIVOT_RECOLLECT_CAP_NEXT
    except Exception:
        pass
    return PIVOT_RECOLLECT_CAP
```

`_cap_reached` then takes the run's pin (`_pivot_decision(..., cap=pivot_cap_for(state.loop_version))`). **Alternative considered:** make the cap a profile setting. Rejected: it is loop semantics, so it belongs on the entity pin, not in per-tenant configuration. **Trade-offs:** an unused constant and function until the data arrives; both are two lines and fully tested (F4-T16).

---

##### D25: Decided — a pending REQUERY order does not outrank a genuine pivot

**Decision (owner, 2026-10-31; recorded, not redesigned):** the pivot keeps its documented precedence over later gathering rules, including a waiting requery order. The order is not lost: it waits unconsumed, and once the per-run cap is reached it fires (probe above), so the cost is bounded by the cap. Revisit only if live runs show requery orders expiring or starving behind pivots; D22's `pivot_capped` and the requery counter from F9 are the signals. This item adds two regression pins so the decision cannot be silently reversed (F4-T14).

#### Version pinning / migration

- **D22, D23, D25: no pin.** D22's counters and reason are observability and do not change any dispatch (the cap check moves later in the same pure function; F4-T02 compares the wrapper against the pre-change function over a generated set, and the existing 34 F4 tests stay green). They are meaningful from lv7 only, by construction. D23 is a pure evaluator with no runtime hook.
- **D24: `LV_F` (or its successor), conditional.** Any cap or scoped-search change routes through `loop_version_at_least`; lv7 keeps 2 and the current scoped-search behavior. If F9 ships `LV_F` first and this change ships later, it takes the next version, not `LV_F` (never hardcode a number).
- **Migration:** none. New telemetry keys are additive; absence means "not recorded" and the evaluator excludes such rows.

#### Test cases

Written first: F4-T01 and F4-T03 through F4-T09 are red before D22; F4-T10 through F4-T13 are red on import; F4-T02, F4-T14 and F4-T15 are regression pins that pass before and after. Files: `tests/unit/test_f4_pivot_measurement.py` and `tests/unit/test_pivot_cap_review.py`.

| ID | Name | Level | Setup | Action | Expected | Notes |
|---|---|---|---|---|---|---|
| F4-T01 | Every branch reports its reason | unit | liveness 1 turn left; no previous focus; same surface; recent target; scoped search; count at cap; plain pivot | `_pivot_decision` | `LIVENESS`, `NO_SHIFT`, `NO_SHIFT`, `DAMPED`, `SCOPED_SEARCH`, `CAPPED`, `FIRED` | Happy path plus each edge |
| F4-T02 | Dispatch decisions unchanged | unit | 300 deterministic (previous focus, proposal, history targets, count, pin flag) tuples whose outputs were recorded from the pre-change function | `_should_force_pivot_recollect` | identical override or `None` for every tuple | Regression pin; negative control: swap the cap check back above the damping check and the `CAPPED`/`DAMPED` classification tests fail while dispatch equality still holds, which is why T01 exists |
| F4-T03 | Capped pivots are counted | unit | lv7 cascade, `counts={"pivot": 2}`, a different-file `GET_CODE` proposal | cascade | `counts["pivot_capped"] == 1`; dispatch is the model's own proposal; `counts["pivot"]` stays 2 | Probe-grounded (`GET_CODE`, no pivot flag, pivot count 2) |
| F4-T04 | Non-pivots are not counted as capped | unit | at cap but: no shift; damped surface; one turn left | cascade | `pivot_capped` absent or 0 | Edge: only would-have-fired counts |
| F4-T05 | Scoped-search skips are counted | unit | lv7 proposal `SEARCH_CODE` with `file_path` and `query` on a new file | cascade | `pivot_scoped_skipped == 1`, no pivot flag, anchor moves to the new file | Probe-grounded; the anchor movement is also pinned by T15 |
| F4-T06 | Stand-down turns count nothing | unit | defer, absence and D6 states with an eligible pivot proposal | cascade | no pivot counters change | The rule yields before evaluation |
| F4-T07 | lv6 reports no suppression counters | unit | lv6 state at "cap" and with a scoped search | cascade | `pivot_capped` and `pivot_scoped_skipped` absent; pivot still fires | Version-pin routing: older pin keeps the old behavior |
| F4-T08 | Productive pivots are counted | workflow-harness | F10 S1 with turn 1's execute result banking no rows, and a variant that banks | record | `forced_pivot_productive_count` 0 for the first, 1 for the second | Negative control: stop incrementing at the stamp site and the second variant reads 0 |
| F4-T09 | The counters reach the stamp | workflow-harness | F10 S1 (five files, lv7) | conclude input | `forced_pivot_count == 2`, `forced_pivot_capped_count == 1`, `forced_pivot_scoped_skipped_count == 0`, all present | Depends on F9; the last turn has one turn left so it is `LIVENESS`, not capped |
| F4-T10 | Evaluator raises on starvation harm | unit | 40 rows: 12 starved, three cases where unstarved repeats pass more often, productive share 0.8 | `review` | `raise`, with the statistics in `stats` | Happy path |
| F4-T11 | Evaluator lowers on unproductive pivots | unit | 40 rows, 24 pivots, 8 productive | `review` | `lower` even when starvation also holds | Precedence: lower beats raise |
| F4-T12 | Too little or incomplete data | unit | 29 rows; and 40 rows where 15 lack counter keys | `review` | `insufficient`; the second counts 25 usable runs, so also `insufficient` | Rows missing keys are excluded, never zero-filled |
| F4-T13 | Threshold boundaries | unit | starved share 0.19 versus 0.20; harmed cases 2 versus 3; productive share 0.50 versus 0.49 | `review` | `keep`, `raise` / `keep`, `raise` / `keep`, `lower` | Boundary values pinned; a malformed row (negative or non-int values) is excluded without raising |
| F4-T14 | Decided precedence pinned | unit | carried REQUERY order pending, different-file proposal; then the same with `counts={"pivot": 2}` | cascade | first: pivot fires, `forced_requery` absent, carrier unconsumed; second: requery fires and the carrier is consumed | Regression pin of decision (f) and its bounded cost; negative control: let a pending order outrank the pivot and the first case fails |
| F4-T15 | Scoped search moves the anchor (lv7) | unit | scoped `SEARCH_CODE` on `svc/new.py`, then a `GET_CODE` of `svc/new.py` next turn | two cascades | no pivot on the second turn; at lv6 the first turn pivots (rewrites to a read) | Pins today's behavior so a future change is deliberate |
| F4-T16 | Cap lookup is pin-routed | unit | `pivot_cap_for` with the next-cap constant unset, then monkeypatched to 3 | call with lv7, `LV_F`, `None`, `"lv999"` | unset: 2 everywhere; set: lv7 and unreadable pins 2, `LV_F` 3 | Conditional until D23 fires; fail-safe on a bad pin |
| F4-T17 | Live measurement on the battery | battery | the T6 gate's repeats on a fresh tenant per run, counters present | extract rows, run `review` | a recorded verdict (`keep` expected) with the statistics attached to the T6 report | **Gated: owner-deferred Tranche-6 gate; live, requires owner approval** (fresh tenants or cleanup); follow-up (b) |

#### Rollout, risks, open owner decisions

Tranche after F9 (needs the lossless stamp) and after F10-B (T08 and T09). Update the F8-D16 golden's `counts` expectation in the same change, reviewed as the two new keys only. Risks: counter semantics misread (documented: lv7+ only, absent means unrecorded); thresholds contested (single constants, reported statistics). Requires owner approval: the read-only extraction, and the T17 live run. **Open owner decisions:** O-F4-1 adopt the D23 thresholds or change them before any data is gathered (they must be fixed before the data is seen); O-F4-2 whether the scoped-search question gets its own battery case.

## Data Storage

No schema, table or migration changes. The only persisted additions are JSON keys in the existing investigation `metadata["loop_telemetry"]` object: `forced_floor_count`, `forced_callsite_count`, `forced_requery_count`, `forced_pivot_capped_count`, `forced_pivot_scoped_skipped_count`, `forced_pivot_productive_count` (new), and the previously dropped `forced_pivot_count`, five `forced_smallest_fetch_<kind>_count`, `forced_absence_disconfirmation_count`, `d6_blocked_no_evidence_count`, `fetches_dispatched`, `defer_repairs_used`, `defer_repairs_limit` (now stamped). Absent keys mean "not recorded by the run's code version"; they are never zero. `CreateInvestigationOutput.loop_version` is a Temporal payload field (JSON, defaulted), not a table column.

## Migrations

None. Ordering constraints between tranches (not database migrations): record the replay transcripts and the cascade golden master before the refactors they protect; update the golden's `counts` once for F4's two keys. In-flight dev workflows must be drained before the F10 latch deploys (**requires owner approval**).

## Testing Philosophy

### Replay and determinism

The strategy is layered because the SDK checks less than it appears to: the `Replayer` raises on a changed command sequence but never compares activity inputs (probe-verified), so every replay test uses the input-parity interceptor, and a negative control tampers an input to prove plain replay would have passed. Self-replay of a freshly recorded run catches nondeterminism and mutation-after-schedule in the current code; golden replay of committed, synthetic transcripts recorded at an earlier commit catches refactor drift and older-pin regressions. The static determinism lint is the complement for passed-through modules, which the sandbox does not police.

### Observability derivation

Every list is tested against the `Force` enum and the counter map, so the tests fail when a member is added without a list entry and when a workflow key would be dropped. Stored shapes are pinned by copying the pre-change key sets into the tests. The telemetry contract test parses the workflow's literal, so it cannot be satisfied by a code comment.

### Table-driven cascade

Equivalence is proven by data: the golden master (about 600 cases across all three pins) and the replay corpus must stay green without regenerating fixtures. A regenerated fixture during a behavior-preserving refactor is a defect. The completeness contract and the structural AST pins make "a new rule needs a table entry and a call" a failing test instead of a convention.

### Carrier separation guard

The AST guard catches the obvious reader; the metamorphic property catches the aliased one. The lv5 negative control keeps the property honest by proving it can fail.

### Pivot measurement

The decision rule is a pure function with boundary tests, so the thresholds are reviewable and changeable in one place before any data is seen. Counter tests are written against the real cascade, with probe outputs as the expected values.

## Documentation Plan

### `docs/IAP-architecture-v1.0.md`

**Audience:** Developers and the owner

Add section 72 covering this group, and correct the shipped claims (see the subsection below). Cover the versioning posture (D7) as a standing rule: loop pin plus replay corpus, no `workflow.patched` before go-live, and the go-live gate.

### `docs/IAP_Arch_Eng_Recommendations.log`

**Audience:** Owner and future sessions

The session rules require new observations to be ledgered before anything else, but this design was written under a read-only brief that forbids editing any file except this one. The implementing session must append, in the standard single-line format: (1) telemetry drop (ten workflow keys never stamped, sections 67 and 70 false); (2) unmarked loop pin floats to the current version on replay (bulk-intake child path); (3) `Replayer` does not compare activity inputs; (4) `FrontierCandidate.seeded_at` executes a wall-clock read inside sandboxed replay; (5) `forcing_used["synthesis"]` is a stale mirror; (6) the F2/F4/F8/F9/F10 follow-up entries move to `PLANNED` with this document as the plan, and the requery-carrier replay entry closes when F10-T14 lands.

### `docs/design/` (optional)

**Audience:** Developers

If the owner wants the replay harness as a standing component design, record it as `DES-workflow-replay-v1.0.md` (Satisfies: the loop-reengineering requirement, Implements: the reason and collect plane decisions). Not required for this group.

### `docs/IAP-proof-kernel-g6-measure-protocol-v1.0.md` (no edit; note only)

**Audience:** Operators running G6 run-sets

The frozen protocol is not edited. Record in the architecture section that the shadow `forced` list gains `pivot_recollect` and `requery` (classified as unmodeled loop forcing) and that runs pinned `LV_F` or later read `forcing_used["synthesis"]` from its own counter, so run-sets are compared within a pin.

### `docs/IAP-architecture-v1.0.md` changes

- Sections 67 and 70: replace the telemetry claims with the post-F9 truth (the keys are stamped), and note that rows recorded before F9 lack them.
- Section 69: replace the `separate_carriers` description with the `Turns` view and `turn_view`, add the guard and the lv5 sunset rule; remove "no Temporal replay" and point at the replay corpus.
- Section 70: replace "unmeasured" with the D22 counters and the D23 decision rule; keep the 2026-10-31 owner decision text and add the regression pins.
- Section 71: record the leaf module move, the completeness contract, and (if adopted) the trigger/apply protocol, `Fire`, `TRIGGERS` and `Rule.cancels`; replace "not done, deliberately" with the decision.
- New section 72: versioning posture, the pin latch, the determinism lint, the replay harness and corpus policy, the lossless telemetry contract, and the `LV_F` entry (shadow `forcing_used` mapping). No `docs/dev/architecture.md` exists; `docs/IAP-architecture-v1.0.md` is the architecture document.
- `domain/investigation/loop_version.py` docstring: add the `LV_F` paragraph quoted in F9.

## Risks / Trade-offs

### Synthesized histories are simpler than real ones

**Risk:** The builder writes one workflow task per activity and no timers, signals, updates or retries, so replay confidence covers the cascade, carriers and pin routing but not signal handling or retried activities.

**Mitigation:** Scenarios are scoped to where this group's risk lives; a real, uncommitted history can be replayed locally with owner approval; transcripts are synthetic so the analyzed-repository rule is never at risk.

### Behavior-preserving refactors are only as safe as their oracle

**Risk:** D2, D15 and D18 touch the loop's hottest code; a subtle difference could ship if the corpus is thin.

**Mitigation:** The corpus has coverage assertions (every force, every pre-cascade combination, all pins); fixtures are never regenerated for a refactor; the replay corpus is an independent second oracle; one tranche per owner message keeps each diff small.

### Changing where the pin is read from

**Risk:** D8 makes the create-activity result part of the replay contract, so histories recorded before it cannot be replayed if they have an unmarked input.

**Mitigation:** IAP is not live; drain in-flight dev workflows before the deploy (owner approval); API-dispatched runs, which carry the pin in the input, are unaffected.

### Thresholds chosen before data

**Risk:** The D23 numbers are judgment calls and could be tuned after seeing results.

**Mitigation:** They are fixed by owner decision O-F4-1 before extraction; the evaluator returns its statistics so any later change is visible and argued.

### Name-based static guards

**Risk:** The determinism lint and the positional-access guard match names; aliasing or dynamic dispatch can evade them.

**Mitigation:** Each has a behavioral second net (the replay corpus; the inert-carrier property) and documented `ACCEPTED`/`CARRIER_READERS` maps with a staleness test.

### Scope creep into group W

**Risk:** D9 and the model parts of D10 overlap W1 (advisory-layer deletion).

**Mitigation:** The items are independent and the lint (D11) stays either way; O-F10-3 asks the owner for the order so no work is done twice.

### Standing rules restated

One tranche per owner message; tests first and shown red before each fix; no commits without the owner; no live runs, restarts, tenant cleans, migrations or database reads without owner approval (this design proposes three: the in-flight workflow drain, the loop-version count, and the pivot extraction plus T6 battery); no scratch-directory or operator-directory references; no analyzed repository named (synthetic names only).
