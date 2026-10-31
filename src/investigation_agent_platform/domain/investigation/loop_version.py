"""Loop-semantics version pin (investigation loop behavior, not data shape).

Per the repo contract, evolving engine semantics are pinned on the entity at
creation (``metadata["loop_version"]``) and routed on the recorded value:
in-flight rows keep the semantics they started with, new rows follow the
current one. Never gated on environment variables.

Versions:

- ``lv2``: the G3 ledger-requery slot stands down on defer and absence turns,
  matching every other gathering rule; a carried order simply waits for the
  next turn.

- ``lv3``: lv2 plus two reasoning-quality rules (review H5, H8). A
  NOT_CHECKABLE claim that states a causal mechanism, or that cites evidence,
  is re-typed as checkable instead of being trusted (H5 floor); and a claim
  group that cites rows but fails its own gate becomes a smallest-fetch gap, so
  it has a re-fetch route (H8). Both only add retrieval; neither can publish a
  verdict.

``lv1`` (the slot ignored defer/absence turns) was retired after a DB check
found zero investigations stamped with it; unmarked rows resolve to current.
"""

from __future__ import annotations

from typing import Any

LOOP_VERSION_LV2 = "lv2"
LOOP_VERSION_LV3 = "lv3"
LOOP_VERSION_CURRENT = LOOP_VERSION_LV3
KNOWN_LOOP_VERSIONS = frozenset({LOOP_VERSION_LV2, LOOP_VERSION_LV3})
# Release order: a later version includes every earlier version's semantics.
_LOOP_VERSION_ORDER = {LOOP_VERSION_LV2: 2, LOOP_VERSION_LV3: 3}


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


def loop_version_at_least(metadata: Any, minimum: str) -> bool:
    """Whether the entity's recorded loop version includes ``minimum``'s semantics.

    Routes version-pinned behavior: ``loop_version_at_least(meta, "lv3")`` is
    True for an lv3 (or unmarked, which resolves to current) row and False for
    a row pinned to lv2, so in-flight runs keep the semantics they started
    with. An unknown pin raises like :func:`resolve_loop_version`.
    """
    return _LOOP_VERSION_ORDER[resolve_loop_version(metadata)] >= _LOOP_VERSION_ORDER[minimum]
