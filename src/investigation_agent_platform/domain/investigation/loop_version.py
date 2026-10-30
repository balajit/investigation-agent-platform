"""Loop-semantics version pin (investigation loop behavior, not data shape).

Per the repo contract, evolving engine semantics are pinned on the entity at
creation (``metadata["loop_version"]``) and routed on the recorded value:
in-flight rows keep the semantics they started with, new rows follow the
current one. Never gated on environment variables.

Versions:

- ``lv2``: the G3 ledger-requery slot stands down on defer and absence turns,
  matching every other gathering rule; a carried order simply waits for the
  next turn.

``lv1`` (the slot ignored defer/absence turns) was retired after a DB check
found zero investigations stamped with it; unmarked rows resolve to current.
"""

from __future__ import annotations

from typing import Any

LOOP_VERSION_LV2 = "lv2"
LOOP_VERSION_CURRENT = LOOP_VERSION_LV2
KNOWN_LOOP_VERSIONS = frozenset({LOOP_VERSION_LV2})


class UnknownLoopVersion(ValueError):
    """A recorded loop version pin is not one this build understands."""


def resolve_loop_version(metadata: Any) -> str:
    """Resolve the loop version recorded on an investigation's metadata.

    Unmarked rows resolve to :data:`LOOP_VERSION_CURRENT`. An unrecognized
    pin raises :class:`UnknownLoopVersion` so dispatch fails loudly instead
    of silently running unknown semantics. Pure; no I/O, no environment.
    """
    if metadata is None:
        return LOOP_VERSION_CURRENT
    if hasattr(metadata, "model_dump"):
        mapping = dict(metadata.model_dump() or {})
    elif isinstance(metadata, dict):
        mapping = dict(metadata)
    else:
        return LOOP_VERSION_CURRENT
    pinned = str(mapping.get("loop_version") or "").strip()
    if not pinned:
        return LOOP_VERSION_CURRENT
    if pinned not in KNOWN_LOOP_VERSIONS:
        raise UnknownLoopVersion(f"unrecognized loop version pin: {pinned!r}")
    return pinned
