# src/investigation_agent_platform/application/onboarding_agent/agent/watchdog.py
"""Watchdog diagnosis invocation contract (agent design §2, contract 2).

An authorized, tenant-scoped control-store state snapshot in, ranked
diagnosis out. Reads only the state the invoking workflow explicitly
supplies — never queries control state — and never remediates. Pure
function: identical snapshots diagnose identically; the clock is
caller-supplied via the snapshot, and causes are emitted in a fixed rule
order with sequential ranks.

Confidence discipline: exceeding a stuck bound is VERIFIED (timestamps and
the bound are evidence in hand); *why* it is stuck is SPECULATIVE until a
distinguishing test says otherwise, and every non-VERIFIED cause names its
tests. Missing data yields an explicit insufficient-evidence cause, never a
guess.
"""

from __future__ import annotations

import itertools
from typing import Any

from investigation_agent_platform.application.onboarding_agent.contracts.coverage import (
    Confidence,
)
from investigation_agent_platform.application.onboarding_agent.contracts.watchdog import (
    Diagnosis,
    RankedCause,
    WatchdogSnapshot,
)

REPEATED_FAILURE_THRESHOLD = 3


def _elapsed_seconds(snapshot: WatchdogSnapshot, stage_index: int) -> float:
    entered = snapshot.stages[stage_index].entered_at
    return (snapshot.now - entered).total_seconds()


def diagnose(snapshot: WatchdogSnapshot) -> Diagnosis:
    """Diagnose one watchdog snapshot, most specific cause first."""
    coords: dict[str, Any] = {
        "request_id": snapshot.request_id,
        "revision": snapshot.revision,
        "generation": snapshot.generation,
    }
    if not snapshot.stages:
        return Diagnosis(
            **coords,
            causes=[
                RankedCause(
                    rank=1,
                    cause="insufficient evidence: snapshot carries no stage history",
                    confidence=Confidence.VERIFIED,
                    evidence=["stages is empty"],
                    distinguishing_tests=[
                        (
                            "resupply the snapshot with the full stage history, "
                            "stuck bounds, and failure counters"
                        )
                    ],
                )
            ],
        )

    try:
        skewed = [
            observation.stage
            for observation in snapshot.stages
            if observation.entered_at > snapshot.now
        ]
    except TypeError:
        skewed = ["<uncomparable timestamps>"]
    if skewed:
        return Diagnosis(
            **coords,
            causes=[
                RankedCause(
                    rank=1,
                    cause="snapshot clock skew: stage timestamps are newer than "
                    "the snapshot clock, so staleness cannot be judged",
                    confidence=Confidence.VERIFIED,
                    evidence=[f"stages newer than now: {', '.join(skewed)}"],
                    distinguishing_tests=[
                        (
                            "resupply the snapshot with consistent, timezone-aware "
                            "timestamps from a single clock"
                        )
                    ],
                )
            ],
        )

    if all(observation.stage != snapshot.current_stage for observation in snapshot.stages):
        return Diagnosis(
            **coords,
            causes=[
                RankedCause(
                    rank=1,
                    cause=f"snapshot inconsistent: current stage "
                    f"{snapshot.current_stage!r} appears in no stage history",
                    confidence=Confidence.VERIFIED,
                    evidence=[
                        "history stages: "
                        + ", ".join(observation.stage for observation in snapshot.stages)
                    ],
                    distinguishing_tests=[
                        (
                            "resupply the snapshot with the full stage history "
                            "including the current stage"
                        )
                    ],
                )
            ],
        )

    causes: list[RankedCause] = []
    ranks = itertools.count(1)
    for index, observation in enumerate(snapshot.stages):
        bound = snapshot.stuck_bounds_seconds.get(observation.stage)
        if bound is None:
            continue
        elapsed = _elapsed_seconds(snapshot, index)
        if elapsed <= bound:
            continue
        token = observation.lease_fencing_token
        causes.append(
            RankedCause(
                rank=next(ranks),
                cause=f"stage {observation.stage!r} exceeded its stuck bound: "
                f"{elapsed:.0f}s elapsed against a {bound}s bound",
                confidence=Confidence.VERIFIED,
                evidence=[
                    f"entered_at {observation.entered_at.isoformat()}",
                    f"snapshot now {snapshot.now.isoformat()}",
                    f"bound {bound}s for stage {observation.stage!r}",
                    f"lease fencing token {token}",
                ],
                distinguishing_tests=[
                    (
                        "inspect liveness of the worker holding the lease for "
                        f"stage {observation.stage!r}"
                    ),
                    (
                        "compare the fencing token across consecutive snapshots "
                        "to separate a stalled holder from a lost lease"
                    ),
                ],
            )
        )
        causes.append(
            RankedCause(
                rank=next(ranks),
                cause=f"lease holder for stage {observation.stage!r} stalled "
                "or lost without release",
                confidence=Confidence.SPECULATIVE,
                evidence=[
                    f"no stage transition for {elapsed:.0f}s",
                    f"lease fencing token {token}",
                ],
                distinguishing_tests=[
                    ("check worker process liveness and recent heartbeats for the lease holder"),
                    (f"look for a STALE_FENCE audit event superseding token {token}"),
                ],
            )
        )
        causes.append(
            RankedCause(
                rank=next(ranks),
                cause=f"stuck bound for stage {observation.stage!r} too tight "
                "for its normal duration",
                confidence=Confidence.SPECULATIVE,
                evidence=[
                    f"elapsed {elapsed:.0f}s against a {bound}s bound",
                    "no failure signal in the supplied snapshot"
                    if snapshot.consecutive_failures == 0
                    else f"{snapshot.consecutive_failures} consecutive failures",
                ],
                distinguishing_tests=[
                    (
                        f"compare against historical durations for stage "
                        f"{observation.stage!r} before changing the bound"
                    ),
                    (
                        "confirm the lease holder is progressing (token "
                        "advancing) rather than stalled"
                    ),
                ],
            )
        )

    if snapshot.consecutive_failures >= REPEATED_FAILURE_THRESHOLD:
        causes.append(
            RankedCause(
                rank=next(ranks),
                cause=f"{snapshot.consecutive_failures} consecutive failures: "
                "poison input or deterministic fault, not transient noise",
                confidence=Confidence.PROJECTED,
                evidence=[
                    (
                        f"consecutive_failures "
                        f"{snapshot.consecutive_failures} >= threshold "
                        f"{REPEATED_FAILURE_THRESHOLD}"
                    )
                ]
                + ([f"workflow notes: {'; '.join(snapshot.notes)}"] if snapshot.notes else []),
                distinguishing_tests=[
                    "inspect the failure notes for a repeating error signature",
                    (
                        "replay the failing step against a minimal input to "
                        "separate input faults from environment faults"
                    ),
                ],
            )
        )

    unbounded = sorted(
        {
            observation.stage
            for observation in snapshot.stages
            if observation.stage not in snapshot.stuck_bounds_seconds
        }
    )
    if unbounded:
        causes.append(
            RankedCause(
                rank=next(ranks),
                cause="no stuck bound defined for stages "
                + ", ".join(repr(stage) for stage in unbounded)
                + "; stalledness cannot be judged there",
                confidence=Confidence.VERIFIED,
                evidence=[
                    "bounded stages: "
                    + (
                        ", ".join(sorted(snapshot.stuck_bounds_seconds))
                        or "no bounds defined at all"
                    ),
                ],
                distinguishing_tests=[
                    ("define stuck bounds for the unbounded stages, then re-run diagnosis"),
                ],
            )
        )

    if not causes:
        causes.append(
            RankedCause(
                rank=next(ranks),
                cause="progress within bounds: no anomaly in the supplied snapshot",
                confidence=Confidence.VERIFIED,
                evidence=[
                    f"current stage {snapshot.current_stage!r} within its bound",
                    (
                        f"consecutive failures {snapshot.consecutive_failures} "
                        f"below threshold {REPEATED_FAILURE_THRESHOLD}"
                    ),
                ],
                distinguishing_tests=[
                    ("re-run diagnosis on the next snapshot to confirm continued progress")
                ],
            )
        )

    return Diagnosis(**coords, causes=causes)
