# src/investigation_agent_platform/application/analysis/java/expressions.py
"""Conservative Java constant-expression evaluation (Phase 2).

Evaluates only side-effect-free expressions over constants under JLS
semantics: Java 32/64-bit wrap-around, truncating division, shift masking,
string conversion on ``+``, and short-circuit logic. Anything else —
method calls, ``new``, lambdas, identifiers, field/array access, switch —
returns PARTIAL with a stable limitation code. Never executes code.
"""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.application.analysis.java import _parse as _p
from investigation_agent_platform.application.analysis.java import literals as _lit

_MASK32 = 0xFFFFFFFF
_MASK64 = 0xFFFFFFFFFFFFFFFF
_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def _signed(value: int, bits: int) -> int:
    value &= (1 << bits) - 1
    if value >= (1 << (bits - 1)):
        value -= 1 << bits
    return value


def _to_int(value: tuple[str, Any]) -> int | None:
    kind, payload = value
    if kind in {"int", "long", "char", "byte", "short"}:
        return int(payload)
    return None


def _stringify(value: tuple[str, Any]) -> str | None:
    kind, payload = value
    if kind == "string":
        return str(payload)
    if kind == "boolean":
        return "true" if payload else "false"
    if kind == "char":
        return chr(payload)
    if kind in {"int", "long", "byte", "short"}:
        return str(payload)
    if kind in {"float", "double"}:
        return repr(float(payload))
    if kind == "null":
        return "null"
    return None


def _truthy(value: tuple[str, Any]) -> bool | None:
    if value[0] == "boolean":
        return bool(value[1])
    return None


def evaluate(
    node: Any, source: bytes, level: int | None
) -> tuple[tuple[str, Any] | None, str | None]:
    """Evaluate an expression node. Returns (typed_value, limitation_or_None)."""
    node_type = node.type
    if node_type == "parenthesized_expression":
        for child in node.children:
            if child.is_named:
                return evaluate(child, source, level)
        return (None, "unsupported_initializer_expression")
    if node_type in {
        "decimal_integer_literal",
        "hex_integer_literal",
        "octal_integer_literal",
        "binary_integer_literal",
    }:
        return _integer_literal(node, source)
    if node_type in {"decimal_floating_point_literal"}:
        value = _lit.parse_floating_literal(_p.node_text(node, source))
        if value is None:
            return (None, "unsupported_initializer_expression")
        return (("double", value), None)
    if node_type in {"hex_floating_point_literal"}:
        return (None, "unsupported_initializer_expression")
    if node_type == "character_literal":
        value, limitation = _lit.decode_character_literal(_p.node_text(node, source), level)
        if limitation is not None:
            return (None, limitation)
        return _char_value(value)
    if node_type == "string_literal":
        text = _p.node_text(node, source)
        if text.strip().startswith('"""'):
            value, limitation = _lit.decode_text_block(text, level)
        else:
            value, limitation = _lit.decode_string_literal(text, level)
        if limitation is not None:
            return (None, limitation)
        return (("string", value), None)
    if node_type == "true":
        return (("boolean", True), None)
    if node_type == "false":
        return (("boolean", False), None)
    if node_type == "null_literal":
        return (("null", None), None)
    if node_type == "unary_expression":
        return _unary(node, source, level)
    if node_type == "binary_expression":
        return _binary(node, source, level)
    if node_type == "ternary_expression":
        return _ternary(node, source, level)
    if node_type == "cast_expression":
        return _cast(node, source, level)
    return (None, "unsupported_initializer_expression")


def _char_value(formatted: str) -> tuple[tuple[str, Any] | None, str | None]:
    try:
        # base=0 would misread the zero-padded hex as octal, so base=16 stays.
        return (("char", int(formatted[2:], 16)), None)  # noqa: FURB166
    except ValueError:
        return (None, "invalid_java_literal")


def _integer_literal(node: Any, source: bytes) -> tuple[tuple[str, Any] | None, str | None]:
    text = _p.node_text(node, source).strip()
    if node.type == "octal_integer_literal" and text[:2].lower() == "0o":
        return (None, "invalid_java_literal")
    parsed = _lit.parse_integer_literal(text)
    if parsed is None:
        return (None, "invalid_java_literal")
    is_long, magnitude = parsed
    limit = _lit.INT64_MAX if is_long else _lit.INT32_MAX
    if magnitude > limit:
        return (None, "invalid_java_literal")
    return (("long" if is_long else "int", magnitude), None)


def _unary(
    node: Any, source: bytes, level: int | None
) -> tuple[tuple[str, Any] | None, str | None]:
    operator: str | None = None
    operand: Any | None = None
    for child in node.children:
        if child.is_named:
            operand = child
        elif child.type in {"+", "-", "!", "~"}:
            operator = child.type
    if operand is None or operator is None:
        return (None, "unsupported_initializer_expression")
    if operator == "-" and operand.type.endswith("integer_literal"):
        # The MIN_VALUE spellings (-2147483648, -9223372036854775808L) apply
        # unary minus to the max+1 magnitude, which is legal only when negated.
        parsed = _lit.parse_integer_literal(_p.node_text(operand, source).strip())
        if parsed is not None:
            is_long, magnitude = parsed
            floor = _INT64_MIN if is_long else _INT32_MIN
            if magnitude == -floor:
                return (("long" if is_long else "int", floor), None)
    value, limitation = evaluate(operand, source, level)
    if limitation is not None or value is None:
        return (None, limitation or "unsupported_initializer_expression")
    kind, payload = value
    if operator == "!":
        flag = _truthy(value)
        return (("boolean", not flag), None) if flag is not None else (None, "invalid_java_literal")
    if operator == "~":
        number = _to_int(value)
        if number is None:
            return (None, "invalid_java_literal")
        width = 64 if kind == "long" else 32
        return ((kind if kind == "long" else "int", _signed(~number, width)), None)
    number = _to_int(value)
    if number is None:
        return (None, "invalid_java_literal")
    if kind in {"float", "double"}:
        return ((kind, -payload if operator == "-" else payload), None)
    if operator == "+":
        if kind == "long":
            return (("long", _signed(number, 64)), None)
        return (("int", _signed(number, 32)), None)
    width = 64 if kind == "long" else 32
    result = _signed(-number, width)
    return ((kind if kind == "long" else "int", result), None)


def _operands(
    node: Any, source: bytes, level: int | None
) -> tuple[tuple[str, Any], str, tuple[str, Any]] | tuple[None, None, str]:
    parts = [c for c in node.children if c.is_named]
    operator = next((c.type for c in node.children if not c.is_named), "")
    if len(parts) != 2 or not operator:
        return (None, None, "unsupported_initializer_expression")
    left, limitation = evaluate(parts[0], source, level)
    if limitation is not None or left is None:
        return (None, None, limitation or "unsupported_initializer_expression")
    right, limitation = evaluate(parts[1], source, level)
    if limitation is not None or right is None:
        return (None, None, limitation or "unsupported_initializer_expression")
    return (left, operator, right)


def _numeric_pair(left: tuple[str, Any], right: tuple[str, Any]) -> tuple[str, int, int] | None:
    a, b = _to_int(left), _to_int(right)
    if a is None or b is None:
        return None
    kind = "long" if "long" in {left[0], right[0]} else "int"
    return (kind, a, b)


def _binary(
    node: Any, source: bytes, level: int | None
) -> tuple[tuple[str, Any] | None, str | None]:
    left, operator, right = _operands(node, source, level)
    if left is None or right is None or operator is None:
        return (None, operator or "unsupported_initializer_expression")
    assert left is not None and right is not None and operator is not None
    if operator == "+":
        if left[0] == "string" or right[0] == "string":
            head = _stringify(left)
            tail = _stringify(right)
            if head is None or tail is None:
                return (None, "unsupported_initializer_expression")
            return (("string", head + tail), None)
        pair = _numeric_pair(left, right)
        if pair is None:
            if left[0] in {"float", "double"} or right[0] in {"float", "double"}:
                return (("double", float(left[1]) + float(right[1])), None)
            return (None, "invalid_java_literal")
        kind, a, b = pair
        return ((kind, _signed(a + b, 64 if kind == "long" else 32)), None)
    if operator in {"&", "|", "^"} and left[0] == right[0] == "boolean":
        a, b = bool(left[1]), bool(right[1])
        if operator == "&":
            return (("boolean", a and b), None)
        if operator == "|":
            return (("boolean", a or b), None)
        return (("boolean", a != b), None)
    if operator in {"-", "*", "/", "%", "<<", ">>", ">>>", "&", "|", "^"}:
        pair = _numeric_pair(left, right)
        if pair is None:
            if operator in {"-", "*", "/"} and (
                left[0] in {"float", "double"} or right[0] in {"float", "double"}
            ):
                return _float_arith(operator, float(left[1]), float(right[1]))
            return (None, "invalid_java_literal")
        kind, a, b = pair
        width = 64 if kind == "long" else 32
        if operator == "-":
            return ((kind, _signed(a - b, width)), None)
        if operator == "*":
            return ((kind, _signed(a * b, width)), None)
        if operator in {"/", "%"}:
            if b == 0:
                return (None, "constant_evaluation_error")
            quotient = abs(a) // abs(b)
            if (a < 0) != (b < 0):
                quotient = -quotient
            if operator == "/":
                return ((kind, _signed(quotient, width)), None)
            return ((kind, _signed(a - quotient * b, width)), None)
        if operator == "<<":
            return ((kind, _signed(a << (b & (63 if kind == "long" else 31)), width)), None)
        if operator == ">>":
            return ((kind, _signed(a >> (b & (63 if kind == "long" else 31)), width)), None)
        if operator == ">>>":
            mask = _MASK64 if kind == "long" else _MASK32
            shifted = (a & mask) >> (b & (63 if kind == "long" else 31))
            return ((kind, shifted), None)
        if operator == "&":
            return ((kind, _signed(a & b, width)), None)
        if operator == "|":
            return ((kind, _signed(a | b, width)), None)
        return ((kind, _signed(a ^ b, width)), None)
    if operator in {"==", "!=", "<", ">", "<=", ">="}:
        return _comparison(operator, left, right)
    if operator == "&&":
        first = _truthy(left)
        if first is None:
            return (None, "invalid_java_literal")
        if not first:
            return (("boolean", False), None)
        second = _truthy(right)
        if second is None:
            return (None, "invalid_java_literal")
        return (("boolean", second), None)
    if operator == "||":
        first = _truthy(left)
        if first is None:
            return (None, "invalid_java_literal")
        if first:
            return (("boolean", True), None)
        second = _truthy(right)
        if second is None:
            return (None, "invalid_java_literal")
        return (("boolean", second), None)
    return (None, "unsupported_initializer_expression")


def _float_arith(operator: str, a: float, b: float) -> tuple[tuple[str, Any] | None, str | None]:
    if operator == "-":
        return (("double", a - b), None)
    if operator == "*":
        return (("double", a * b), None)
    # IEEE 754: float division never errors; zero divisors yield infinities/NaN.
    if b == 0.0:
        if a == 0.0:
            return (("double", float("nan")), None)
        return (("double", float("inf") if a > 0 else float("-inf")), None)
    return (("double", a / b), None)


def _comparison(
    operator: str, left: tuple[str, Any], right: tuple[str, Any]
) -> tuple[tuple[str, Any] | None, str | None]:
    if operator in {"==", "!="} and left[0] == right[0] == "boolean":
        result = left[1] == right[1]
        return (("boolean", result if operator == "==" else not result), None)
    if operator in {"==", "!="} and left[0] == right[0] == "null":
        return (("boolean", operator == "=="), None)
    if left[0] in {"float", "double"} or right[0] in {"float", "double"}:
        try:
            a, b = float(left[1]), float(right[1])
        except (TypeError, ValueError):
            return (None, "unsupported_initializer_expression")
        return _float_comparison(operator, a, b)
    pair = _numeric_pair(left, right)
    if pair is None:
        return (None, "unsupported_initializer_expression")
    _, a, b = pair
    if operator == "==":
        return (("boolean", a == b), None)
    if operator == "!=":
        return (("boolean", a != b), None)
    if operator == "<":
        return (("boolean", a < b), None)
    if operator == ">":
        return (("boolean", a > b), None)
    if operator == "<=":
        return (("boolean", a <= b), None)
    return (("boolean", a >= b), None)


def _float_comparison(
    operator: str, a: float, b: float
) -> tuple[tuple[str, Any] | None, str | None]:
    if operator == "==":
        return (("boolean", a == b), None)
    if operator == "!=":
        return (("boolean", a != b), None)
    if operator == "<":
        return (("boolean", a < b), None)
    if operator == ">":
        return (("boolean", a > b), None)
    if operator == "<=":
        return (("boolean", a <= b), None)
    if operator == ">=":
        return (("boolean", a >= b), None)
    return (None, "unsupported_initializer_expression")


def _ternary(
    node: Any, source: bytes, level: int | None
) -> tuple[tuple[str, Any] | None, str | None]:
    parts = [c for c in node.children if c.is_named]
    if len(parts) != 3:
        return (None, "unsupported_initializer_expression")
    condition, limitation = evaluate(parts[0], source, level)
    if limitation is not None or condition is None:
        return (None, limitation or "unsupported_initializer_expression")
    flag = _truthy(condition)
    if flag is None:
        return (None, "invalid_java_literal")
    return evaluate(parts[1] if flag else parts[2], source, level)


def _cast(node: Any, source: bytes, level: int | None) -> tuple[tuple[str, Any] | None, str | None]:
    type_node = node.child_by_field_name("type")
    operand = node.child_by_field_name("value")
    target = _p.node_text(type_node, source).strip() if type_node is not None else None
    if target is None or operand is None:
        return (None, "unsupported_initializer_expression")
    value, limitation = evaluate(operand, source, level)
    if limitation is not None or value is None:
        return (None, limitation or "unsupported_initializer_expression")
    kind, payload = value
    if target in {"byte", "short", "char", "int"}:
        number = _to_int(value)
        if number is None:
            if target in {"byte", "short", "char", "int"} and kind in {"float", "double"}:
                number = int(payload)
            else:
                return (None, "invalid_java_literal")
        width = {"byte": 8, "short": 16, "char": 16, "int": 32}[target]
        narrowed = _signed(number, width)
        if target == "char":
            if not 0 <= narrowed <= 0xFFFF:
                return (None, "constant_evaluation_error")
            return (("char", narrowed), None)
        return (("int", narrowed), None)
    if target == "long":
        number = _to_int(value)
        if number is None:
            if kind in {"float", "double"}:
                number = int(payload)
            else:
                return (None, "invalid_java_literal")
        return (("long", _signed(number, 64)), None)
    if target in {"float", "double"}:
        number = _to_int(value)
        if number is None:
            if kind in {"float", "double"}:
                return (("double", float(payload)), None)
            return (None, "invalid_java_literal")
        return (("double", float(number)), None)
    return (None, "unsupported_initializer_expression")


def format_for_field_type(value: tuple[str, Any], field_type: str) -> tuple[str | None, str | None]:
    """Format an evaluated value for a declared field type, JLS-checked."""
    base = field_type.replace("[]", "").split("<", 1)[0].strip()
    kind, payload = value
    if base == "boolean":
        flag = _truthy(value)
        return (
            (("true" if flag else "false"), None)
            if flag is not None
            else (None, "invalid_java_literal")
        )
    if base in {"byte", "short", "int", "long"}:
        number = _to_int(value)
        if number is None:
            return (None, "invalid_java_literal")
        if base != "long" and kind == "long":
            # Narrowing a long to a smaller type needs an explicit cast.
            return (None, "invalid_java_literal")
        limits = {
            "byte": (-128, 127),
            "short": (-32768, 32767),
            "int": (_INT32_MIN, _INT32_MAX),
            "long": (_INT64_MIN, _INT64_MAX),
        }
        low, high = limits[base]
        if not low <= number <= high:
            return (None, "constant_evaluation_error")
        return (str(number), None)
    if base in {"float", "double"}:
        number = _to_int(value)
        if number is not None:
            return (repr(float(number)), None)
        if kind in {"float", "double"}:
            return (repr(float(payload)), None)
        return (None, "invalid_java_literal")
    if base == "char":
        if kind == "char":
            return (f"U+{payload:04X}", None)
        number = _to_int(value)
        if number is not None and 0 <= number <= 0xFFFF:
            return (f"U+{number:04X}", None)
        return (None, "invalid_java_literal")
    if base in {"String", "java.lang.String"}:
        text = _stringify(value)
        return (text, None) if text is not None else (None, "invalid_java_literal")
    if base == "var":
        # Inferred declaration anchor: the value is known, the static type is
        # the anchor only. Any non-null evaluated value formats via conversion.
        if kind == "null":
            return ("null", None)
        text = _stringify(value)
        return (text, None) if text is not None else (None, "invalid_java_literal")
    # Reference-type fields only accept null conservatively (legacy behavior);
    # boxing of a computed value is not claimed without a cast in source.
    if kind == "null":
        return ("null", None)
    return (None, "invalid_java_literal")
