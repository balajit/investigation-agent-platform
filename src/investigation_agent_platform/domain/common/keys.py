# src/investigation_agent_platform/domain/common/keys.py
"""Unambiguous key joining (R12E).

Colon-delimited composite keys are only injective while no part contains
the delimiter in a boundary-shifting position. Tenant ids may contain
``:``, so any *new* key builder must join parts in a way that cannot
misparse. Existing keys whose part shapes are fixed (UUIDs, hex
digests) stay as they are — this helper guards future builders.
"""

from __future__ import annotations


def join_key_parts(*parts: object) -> str:
    """Unambiguous join: each part is length-prefixed.

    ``("a:b", "c")`` and ``("a", "b:c")`` join differently, so ``:`` or
    any other character in a part cannot shift a boundary. Pure.
    """
    out: list[str] = []
    for part in parts:
        try:
            text = str(part if part is not None else "")
        except Exception:
            text = ""
        out.append(f"{len(text)}:{text}")
    return "".join(out)

