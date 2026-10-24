"""Stage C harness canonical path (proposal §5): frozen contract tests.

The shared strict canonicalizer used identically by signing and
verification. Stable expectations: sorted keys, compact separators, NFC
enforcement, duplicate-key / float / trailing-data rejection, LF-normalized
formula bytes, and domain-separated digests.
"""

from __future__ import annotations

import pytest

from investigation_agent_platform.harness.canonical import (
    canonical_bytes,
    domain_digest,
    formula_canonical_bytes,
    formula_digest,
    sha256_digest,
    strict_parse,
)


def test_canonical_bytes_sorted_compact() -> None:
    assert canonical_bytes({"b": 1, "a": [1, 2]}) == b'{"a":[1,2],"b":1}'


def test_canonical_bytes_no_trailing_newline() -> None:
    assert not canonical_bytes({"a": 1}).endswith(b"\n")


def test_strict_parse_round_trip() -> None:
    raw = b'{"a":[1,2],"b":1}'
    assert strict_parse(raw) == {"a": [1, 2], "b": 1}


def test_strict_parse_rejects_duplicate_key() -> None:
    with pytest.raises(ValueError, match="duplicate JSON key"):
        strict_parse(b'{"a":1,"a":2}')


def test_strict_parse_rejects_float() -> None:
    with pytest.raises(ValueError, match="float"):
        strict_parse(b'{"a":1.0}')


def test_strict_parse_rejects_non_nfc() -> None:
    with pytest.raises(ValueError, match="non-NFC"):
        strict_parse('{"a":"é"}'.encode())


def test_strict_parse_rejects_trailing_data() -> None:
    with pytest.raises(ValueError, match="trailing data"):
        strict_parse(b'{"a":1} {"b":2}')


def test_strict_parse_accepts_leading_whitespace() -> None:
    assert strict_parse(b'  \n\t{"a":1}') == {"a": 1}


def test_strict_parse_accepts_json_trailing_whitespace() -> None:
    assert strict_parse(b'{"a":1} \t\r\n') == {"a": 1}


def test_strict_parse_rejects_non_json_trailing_whitespace() -> None:
    with pytest.raises(ValueError, match="trailing data"):
        strict_parse('{"a":1}\u00a0'.encode("utf-8"))


def test_strict_parse_rejects_nan() -> None:
    with pytest.raises(ValueError, match="non-standard constant"):
        strict_parse(b'{"a":NaN}')


def test_strict_parse_rejects_infinity() -> None:
    with pytest.raises(ValueError, match="non-standard constant"):
        strict_parse(b'{"a":Infinity}')
    with pytest.raises(ValueError, match="non-standard constant"):
        strict_parse(b'{"a":-Infinity}')


def test_canonical_bytes_rejects_float() -> None:
    with pytest.raises(ValueError, match="float"):
        canonical_bytes({"a": 1.0})


def test_canonical_bytes_rejects_nan_and_infinity() -> None:
    with pytest.raises(ValueError, match="float"):
        canonical_bytes({"a": float("nan")})
    with pytest.raises(ValueError, match="float"):
        canonical_bytes({"a": float("inf")})


def test_canonical_bytes_rejects_non_nfc() -> None:
    with pytest.raises(ValueError, match="non-NFC"):
        canonical_bytes({"a": "é"})


def test_canonical_bytes_rejects_non_string_key() -> None:
    with pytest.raises(ValueError, match="non-string key"):
        canonical_bytes({1: "a"})


def test_canonical_bytes_rejects_non_native_value() -> None:
    with pytest.raises(ValueError, match="non-JSON-native"):
        canonical_bytes({"a": object()})


def test_strict_parse_rejects_non_utf8() -> None:
    with pytest.raises((ValueError, UnicodeDecodeError)):
        strict_parse(b'{"a":\xff}')


def test_sha256_digest_shape() -> None:
    digest = sha256_digest(b"abc")
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64


def test_formula_canonical_bytes_lf_and_trailing_newline() -> None:
    assert formula_canonical_bytes("a: 1\r\nb: 2") == b"a: 1\nb: 2\n"
    assert formula_canonical_bytes("a: 1\n") == b"a: 1\n"


def test_formula_canonical_bytes_standalone_cr() -> None:
    assert formula_canonical_bytes("a: 1\rb: 2") == b"a: 1\nb: 2\n"


def test_formula_canonical_bytes_single_trailing_newline() -> None:
    assert formula_canonical_bytes("a: 1\n\n\n") == b"a: 1\n"
    assert formula_canonical_bytes("a: 1") == b"a: 1\n"


def test_formula_digest_fixed_golden() -> None:
    assert (
        formula_digest("metric: M\nnumerator: x\n")
        == "sha256:e33ccb362a94813b816a6c78688fc6c16aca6adc8b2cf41807d3959c18ad9368"
    )


def test_domain_digest_separates_domains() -> None:
    assert domain_digest(b"d1", b"p") != domain_digest(b"d2", b"p")


def test_formula_digest_is_domain_separated() -> None:
    text = "metric: M\nnumerator: x\n"
    assert formula_digest(text) == domain_digest(
        b"iap:d3-stagec:metric-formula/v1", formula_canonical_bytes(text)
    )
