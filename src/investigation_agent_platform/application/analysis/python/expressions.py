# src/investigation_agent_platform/application/analysis/python/expressions.py
"""Conservative Python constant-expression evaluation.

Evaluates only side-effect-free expressions over literals: numbers, strings,
booleans, None, unary/binary/boolean/comparison operators, conditional
expressions over supported branches, and literal list/tuple/set/dict
construction from supported elements. Anything else — calls, subscripts,
attribute access, interpolation, comprehensions, lambdas, await, named
expressions, unknown names — returns PARTIAL with a stable limitation code.
Never executes code, never imports modules.
"""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.application.analysis.python import _parse as _p
from investigation_agent_platform.application.analysis.python import literals as _lit

# Tagged value: (kind, payload). Kinds: int, float, bool, none, str,
# list, tuple, set, dict.
_Value = tuple[str, Any]

_UNSUPPORTED = "unsupported_expression"


def _int(node: Any, source: bytes) -> tuple[_Value | None, str | None]:
    value = _lit.parse_int_literal(_p.node_text(node, source))
    if value is None:
        return (None, _UNSUPPORTED)
    return (("int", value), None)


def _float(node: Any, source: bytes) -> tuple[_Value | None, str | None]:
    value = _lit.parse_float_literal(_p.node_text(node, source))
    if value is None:
        return (None, _UNSUPPORTED)
    return (("float", value), None)


def _string(node: Any, source: bytes) -> tuple[_Value | None, str | None]:
    # Interpolations / format strings execute code: abstain.
    for child in _p.walk(node):
        if child.type == "interpolation":
            return (None, "unsupported_fstring_expression")
    value, limitation = _lit.parse_string_literal(_p.node_text(node, source))
    if limitation is not None or value is None:
        return (None, limitation or _UNSUPPORTED)
    return (("str", value), None)


def _unary(node: Any, source: bytes) -> tuple[_Value | None, str | None]:
    operand = next((c for c in node.children if c.is_named), None)
    if operand is None:
        return (None, _UNSUPPORTED)
    value, limitation = evaluate(operand, source)
    if limitation is not None or value is None:
        return (None, limitation or _UNSUPPORTED)
    op = next((c for c in node.children if not c.is_named), None)
    text = _p.node_text(op, source).strip() if op is not None else ""
    kind, payload = value
    try:
        if text == "-":
            if kind == "int":
                return (("int", -payload), None)
            if kind == "float":
                return (("float", -payload), None)
            return (None, _UNSUPPORTED)
        if text == "+":
            if kind in {"int", "float"}:
                return (value, None)
            return (None, _UNSUPPORTED)
        if text == "~":
            if kind == "int":
                return (("int", ~payload), None)
            return (None, _UNSUPPORTED)
    except (OverflowError, ValueError):
        return (None, _UNSUPPORTED)
    return (None, _UNSUPPORTED)


def _not(node: Any, source: bytes) -> tuple[_Value | None, str | None]:
    operand = next((c for c in node.children if c.is_named), None)
    if operand is None:
        return (None, _UNSUPPORTED)
    value, limitation = evaluate(operand, source)
    if limitation is not None or value is None:
        return (None, limitation or _UNSUPPORTED)
    if value[0] == "bool":
        return (("bool", not value[1]), None)
    return (None, _UNSUPPORTED)


def _truthy(value: _Value) -> bool | None:
    kind, payload = value
    if kind == "bool":
        return bool(payload)
    if kind == "none":
        return False
    if kind == "int" and payload == 0:
        return False
    if kind == "int":
        return True
    return None


def _binary(node: Any, source: bytes) -> tuple[_Value | None, str | None]:
    named = [c for c in node.children if c.is_named]
    if len(named) != 2:
        return (None, _UNSUPPORTED)
    op = next((c for c in node.children if not c.is_named), None)
    left, llim = evaluate(named[0], source)
    right, rlim = evaluate(named[1], source)
    if llim is not None or left is None:
        return (None, llim or _UNSUPPORTED)
    if rlim is not None or right is None:
        return (None, rlim or _UNSUPPORTED)
    text = _p.node_text(op, source).strip() if op is not None else ""
    lk, lp = left
    rk, rp = right
    try:
        if text in {"+", "-", "*", "//", "%", "**", "<<", ">>", "&", "|", "^"}:
            if lk == "int" and rk == "int":
                if text == "+":
                    return (("int", lp + rp), None)
                if text == "-":
                    return (("int", lp - rp), None)
                if text == "*":
                    return (("int", lp * rp), None)
                if text == "//":
                    if rp == 0:
                        return (None, "unsupported_zero_division")
                    return (("int", lp // rp), None)
                if text == "%":
                    if rp == 0:
                        return (None, "unsupported_zero_division")
                    return (("int", lp % rp), None)
                if text == "**":
                    if rp < 0 or rp > 64 or abs(lp) > 10**6:
                        return (None, "unsupported_exponent_range")
                    return (("int", lp**rp), None)
                if text == "<<":
                    if rp < 0 or rp > 1024:
                        return (None, "unsupported_shift_range")
                    return (("int", lp << rp), None)
                if text == ">>":
                    if rp < 0 or rp > 1024:
                        return (None, "unsupported_shift_range")
                    return (("int", lp >> rp), None)
                if text == "&":
                    return (("int", lp & rp), None)
                if text == "|":
                    return (("int", lp | rp), None)
                if text == "^":
                    return (("int", lp ^ rp), None)
            if text == "+" and lk == "str" and rk == "str":
                if len(lp) + len(rp) > 4096:
                    return (None, "unsupported_string_size")
                return (("str", lp + rp), None)
            if text == "*" and (
                (lk == "str" and rk == "int") or (lk == "int" and rk == "str")
            ):
                s, n = (lp, rp) if lk == "str" else (rp, lp)
                if n < 0 or n > 256 or len(s) * max(n, 0) > 4096:
                    return (None, "unsupported_string_size")
                return (("str", s * n), None)
            if lk in {"int", "float"} and rk in {"int", "float"} and text in {"+", "-", "*", "/"}:
                a, b = (float(lp), float(rp))
                if text == "+":
                    return (("float", a + b), None)
                if text == "-":
                    return (("float", a - b), None)
                if text == "*":
                    return (("float", a * b), None)
                if b == 0:
                    return (None, "unsupported_zero_division")
                return (("float", a / b), None)
            return (None, _UNSUPPORTED)
    except (OverflowError, ValueError, MemoryError):
        return (None, _UNSUPPORTED)
    return (None, _UNSUPPORTED)


def _boolean(node: Any, source: bytes) -> tuple[_Value | None, str | None]:
    named = [c for c in node.children if c.is_named]
    op = next((c for c in node.children if not c.is_named), None)
    text = _p.node_text(op, source).strip().lower() if op is not None else ""
    if text not in {"and", "or"} or not named:
        return (None, _UNSUPPORTED)
    values: list[_Value] = []
    for child in named:
        value, limitation = evaluate(child, source)
        if limitation is not None or value is None:
            return (None, limitation or _UNSUPPORTED)
        if value[0] != "bool":
            return (None, "unsupported_nonboolean_logic_operand")
        values.append(value)
    result = values[0][1]
    for value in values[1:]:
        result = (result and value[1]) if text == "and" else (result or value[1])
    return (("bool", bool(result)), None)


def _comparison(node: Any, source: bytes) -> tuple[_Value | None, str | None]:
    named = [c for c in node.children if c.is_named]
    ops = [c for c in node.children if not c.is_named]
    if len(named) != 2 or len(ops) != 1:
        return (None, "unsupported_chained_comparison")
    left, llim = evaluate(named[0], source)
    right, rlim = evaluate(named[1], source)
    if llim is not None or left is None:
        return (None, llim or _UNSUPPORTED)
    if rlim is not None or right is None:
        return (None, rlim or _UNSUPPORTED)
    text = _p.node_text(ops[0], source).strip()
    lk, lp = left
    rk, rp = right
    if lk != rk or lk not in {"int", "float", "str", "bool"}:
        if not (lk == "int" and rk == "float") and not (lk == "float" and rk == "int"):
            return (None, _UNSUPPORTED)
        lp, rp = float(lp), float(rp)
    try:
        if text == "==":
            return (("bool", lp == rp), None)
        if text == "!=":
            return (("bool", lp != rp), None)
        if text == "<":
            return (("bool", lp < rp), None)
        if text == "<=":
            return (("bool", lp <= rp), None)
        if text == ">":
            return (("bool", lp > rp), None)
        if text == ">=":
            return (("bool", lp >= rp), None)
    except TypeError:
        return (None, _UNSUPPORTED)
    return (None, _UNSUPPORTED)


def _conditional(node: Any, source: bytes) -> tuple[_Value | None, str | None]:
    # conditional_expression: <body> if <condition> else <alternative>
    named = [c for c in node.children if c.is_named]
    if len(named) != 3:
        return (None, _UNSUPPORTED)
    cond, clim = evaluate(named[1], source)
    if clim is not None or cond is None:
        return (None, clim or _UNSUPPORTED)
    truth = _truthy(cond)
    if truth is None:
        return (None, "unsupported_condition_value")
    branch = named[0] if truth else named[2]
    return evaluate(branch, source)


def _collection(node: Any, source: bytes) -> tuple[_Value | None, str | None]:
    kind = node.type
    if kind == "list":
        items: list[Any] = []
        for child in node.children:
            if not child.is_named:
                continue
            if child.type == "list_comprehension":
                return (None, "unsupported_comprehension")
            value, limitation = evaluate(child, source)
            if limitation is not None or value is None:
                return (None, limitation or _UNSUPPORTED)
            items.append(_materialize(value))
        return (("list", items), None)
    if kind == "tuple":
        named = [c for c in node.children if c.is_named]
        if any(c.type in {"tuple_comprehension", "generator_expression"} for c in named):
            return (None, "unsupported_comprehension")
        items = []
        for child in named:
            value, limitation = evaluate(child, source)
            if limitation is not None or value is None:
                return (None, limitation or _UNSUPPORTED)
            items.append(_materialize(value))
        return (("tuple", tuple(items)), None)
    if kind == "set":
        named = [c for c in node.children if c.is_named]
        if any(c.type in {"set_comprehension", "generator_expression"} for c in named):
            return (None, "unsupported_comprehension")
        items = []
        for child in named:
            value, limitation = evaluate(child, source)
            if limitation is not None or value is None:
                return (None, limitation or _UNSUPPORTED)
            items.append(_materialize(value))
        try:
            return (("set", sorted(items, key=repr)), None)
        except TypeError:
            return (None, _UNSUPPORTED)
    if kind == "dictionary":
        pairs: dict[Any, Any] = {}
        for child in node.children:
            if child.type == "pair":
                named = [c for c in child.children if c.is_named]
                if len(named) != 2:
                    return (None, _UNSUPPORTED)
                key, klim = evaluate(named[0], source)
                val, vlim = evaluate(named[1], source)
                if klim is not None or key is None:
                    return (None, klim or _UNSUPPORTED)
                if vlim is not None or val is None:
                    return (None, vlim or _UNSUPPORTED)
                if key[0] not in {"str", "int", "bool"}:
                    return (None, "unsupported_dict_key")
                pairs[_materialize(key)] = _materialize(val)
            elif child.is_named:
                if child.type == "dictionary_comprehension":
                    return (None, "unsupported_comprehension")
                if child.type == "pair":
                    continue
                return (None, _UNSUPPORTED)
        return (("dict", pairs), None)
    return (None, _UNSUPPORTED)


def _materialize(value: _Value) -> Any:
    kind, payload = value
    if kind == "none":
        return None
    return payload


def evaluate(node: Any, source: bytes) -> tuple[_Value | None, str | None]:
    """Evaluate an expression node. Returns (tagged_value, limitation_or_None)."""
    node_type = node.type
    if node_type == "integer":
        return _int(node, source)
    if node_type == "float":
        return _float(node, source)
    if node_type == "true":
        return (("bool", True), None)
    if node_type == "false":
        return (("bool", False), None)
    if node_type == "none":
        return (("none", None), None)
    if node_type == "string":
        return _string(node, source)
    if node_type == "unary_operator":
        return _unary(node, source)
    if node_type == "not_operator":
        return _not(node, source)
    if node_type == "binary_operator":
        return _binary(node, source)
    if node_type == "boolean_operator":
        return _boolean(node, source)
    if node_type == "comparison_operator":
        return _comparison(node, source)
    if node_type == "conditional_expression":
        return _conditional(node, source)
    if node_type in {"list", "tuple", "set", "dictionary"}:
        return _collection(node, source)
    if node_type == "parenthesized_expression":
        for child in node.children:
            if child.is_named:
                return evaluate(child, source)
        return (None, _UNSUPPORTED)
    if node_type == "identifier":
        text = _p.node_text(node, source).strip()
        if text == "True":
            return (("bool", True), None)
        if text == "False":
            return (("bool", False), None)
        if text == "None":
            return (("none", None), None)
        return (None, "unsupported_name_reference")
    if node_type == "call":
        return (None, "unsupported_call_expression")
    if node_type in {"attribute", "subscript"}:
        return (None, "unsupported_lookup_expression")
    if node_type in {
        "lambda",
        "list_comprehension",
        "set_comprehension",
        "dictionary_comprehension",
        "generator_expression",
        "await",
        "named_expression",
    }:
        return (None, "unsupported_dynamic_expression")
    return (None, _UNSUPPORTED)


def format_value(value: _Value) -> str:
    """Render a tagged value the way the verifier compares resolved_value."""
    kind, payload = value
    if kind == "none":
        return "None"
    if kind == "bool":
        return "True" if payload else "False"
    if kind == "str":
        return str(payload)
    if kind == "float":
        return repr(float(payload))
    if kind == "list":
        return repr(list(payload))
    if kind == "tuple":
        return repr(tuple(payload))
    if kind == "set":
        return repr(list(payload))
    if kind == "dict":
        return repr(dict(payload))
    return str(payload)
