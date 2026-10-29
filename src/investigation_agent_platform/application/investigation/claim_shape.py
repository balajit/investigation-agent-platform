# src/investigation_agent_platform/application/investigation/claim_shape.py
"""Value-claim shape detection (single home).

Classifies a claim statement as a source-value question — a method's
guarded return literal or a field's initializer/default value — or
None. Used ONLY to demand machine-readable structure at formulation
time (fact_category, claimed_value, target) with one feedback retry;
never to fill claim content. The model commits the category, value,
and target, or the claim stays untyped and the honest path applies.
No per-case logic lives here: two shapes, one table.
"""

from __future__ import annotations

import re
from typing import Literal

ValueShape = Literal["method-return", "field-value"]

_VALUE_LITERAL_RE = r"(['\"][^'\"]+['\"]|[-+]?\d+(?:\.\d+)?(?:[fFdDlL])?|true|false|null)"

_METHOD_RETURN_RE = re.compile(r"(?i)\breturns?\s+" + _VALUE_LITERAL_RE + r"(?=\s|[.,;:!?)\]]|$)")

_FIELD_VALUE_RE = re.compile(
    r"(?i)(?:default\s+value|initial\s+value|field\s+value|"
    r"value\s+of\s+(?:the\s+)?(?:field\s+)?[A-Za-z_][\w.]*\s+is|"
    r"initializes?\s+to|initialized\s+to|defaults?\s+to|assigned\s+to)"
    r".{0,80}?" + _VALUE_LITERAL_RE
)

# Shape → the typing the structure demand names. The model still
# commits every field; this table only makes the demand actionable.
_SHAPE_TYPING: dict[str, dict[str, str]] = {
    "method-return": {
        "fact_category": "SOURCE_VALUE_FACT",
        "value_semantics": "GUARDED_RETURN_LITERAL",
    },
    "field-value": {
        "fact_category": "SOURCE_VALUE_FACT",
        "value_semantics": "EXPLICIT_INITIALIZER",
    },
}


def value_shape_of(statement: str) -> ValueShape | None:
    """Classify a claim statement, or None when it is not value-shaped."""
    try:
        text = str(statement or "")
        if not text.strip():
            return None
        if _METHOD_RETURN_RE.search(text) is not None:
            return "method-return"
        if _FIELD_VALUE_RE.search(text) is not None:
            return "field-value"
        return None
    except Exception:
        return None


def suggested_typing(shape: ValueShape) -> dict[str, str]:
    """Typing hint the structure demand names for one shape (pure)."""
    return dict(_SHAPE_TYPING[shape])
