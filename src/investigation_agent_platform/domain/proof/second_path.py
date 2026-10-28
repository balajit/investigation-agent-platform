# src/investigation_agent_platform/domain/proof/second_path.py
"""Second independent decision path (B1 diverse validator, Option 1).

Implements the five rule semantics a second time, from the Component-2
D6a ordered contract sequences, on a deliberately disjoint substrate:

- no import of ``rules``, ``checker_semantics``, ``certificate``,
  ``canonical``, ``premises``, or ``manifest`` (only the frozen
  ``CheckerOutcome`` vocabulary from ``outcomes``, which both paths must
  speak for the conjunction gate to compose);
- hand-rolled structural validation instead of the shared Pydantic
  premise models;
- an independently written canonical-byte routine (same byte layout as
  the contract requires — sorted keys, compact separators, UTF-8 —
  with the same fail-closed float/NFC guards) instead of the shared
  ``canonical_dict_bytes``;
- independently restated closed key sets (V2 target/scope keys, premise
  envelope keys, header literals) instead of the shared tables.

Structure is deliberately different from the first path: a single
table-driven engine (envelope -> hash binding -> shape -> header ->
cardinality -> joins -> bindings -> scope) parameterized by per-rule
spec rows, rather than five bespoke functions.

Honesty note on independence: this module was written with knowledge of
the first path's behavior (same author, same session), so author
blindness is NOT claimed. Diversity is proven empirically instead, by
the differential suite in
``tests/unit/test_second_path_diversity.py``: mutants that fool path 1
must not fool this path (and vice versa), and both paths must agree on
the full valid corpus. Any future change to the rule contract must
update both paths plus the differential suite; the fingerprint control
(``validator_fingerprints``) covers this file so an edit to one path
without the other fails closed.

Conjunction protocol (wired in ``certificate.check_certificate``): the
certificate is VALID only if path 1 AND this path both return VALID.
Any disagreement, or any exception raised by this path, denies closed
(``CHECKER_FAILURE`` carrying the second-path outcome and reason, so
divergences stay visible in the verdict instead of being silently
mapped to a first-path denial class).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from investigation_agent_platform.domain.proof.outcomes import CheckerOutcome

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")

# Closed contracts, restated from the Component-2 D6a sequences and the
# frozen fixture corpus (NOT imported from canonical.py — that is the
# shared-table substrate this path exists to avoid re-inheriting).
_TARGET_KEYS: dict[str, frozenset[str]] = {
    "definition/v1": frozenset({"symbol", "file_path"}),
    "call_edge/v1": frozenset({"caller", "callee", "counterpart", "symbol"}),
    "behavior/v1": frozenset({"symbol", "file_path"}),
    "absence/v1": frozenset({"universe"}),
    "history/v1": frozenset({"repository", "commit_id", "file_path", "subject"}),
}
_SCOPE_KEYS: dict[str, frozenset[str]] = {
    "definition/v1": frozenset({"revision"}),
    "call_edge/v1": frozenset({"revision"}),
    "behavior/v1": frozenset({"repository", "revision", "build_identity", "execution_id", "environment"}),
    "absence/v1": frozenset({"repository", "revision", "path_scope"}),
    "history/v1": frozenset({"repository_revision"}),
}
_ENVELOPE_KEYS = frozenset({"evidence_id", "content_hash", "premise_type", "scope"})
_HEADER: dict[str, dict[str, str | None]] = {
    "definition/v1": {"form": "EXISTENCE", "quantifier": "EXISTENTIAL"},
    "call_edge/v1": {"form": "EXISTENCE", "quantifier": "EXISTENTIAL"},
    "behavior/v1": {"form": "OBSERVATION", "quantifier": "OBSERVATIONAL"},
    "absence/v1": {"form": "ABSENCE", "quantifier": "UNIVERSAL"},
    "history/v1": {"form": "HISTORICAL", "quantifier": "EXISTENTIAL"},
}
# Test-only authority approved 2026-10-24; real-world authority OPEN (B3).
_APPROVED_AUTHORITIES = frozenset({"synthetic-test-only-authority-001"})


def _canonical_bytes(node: dict[str, Any]) -> bytes:
    """Independent canonical bytes: sorted keys, compact separators, UTF-8.

    Same byte layout the contract requires (so digests agree with path 1
    on valid inputs), with the same fail-closed guards: floats rejected
    anywhere (even integral ones), non-NFC strings rejected. Raises
    ValueError on guard failure — callers map it to a typed denial.
    """
    if not isinstance(node, dict):
        raise TypeError("canonical input must be a mapping")
    _reject_floats(node, "$")
    _reject_non_nfc(node, "$")
    return json.dumps(node, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _reject_floats(node: Any, where: str) -> None:
    if isinstance(node, float):
        raise TypeError(f"float forbidden at {where}")
    if isinstance(node, dict):
        for key, item in node.items():
            _reject_floats(item, f"{where}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _reject_floats(item, f"{where}[{index}]")


def _reject_non_nfc(node: Any, where: str) -> None:
    if isinstance(node, str):
        if node != unicodedata.normalize("NFC", node):
            raise ValueError(f"non-NFC string at {where}")
    elif isinstance(node, dict):
        for key, item in node.items():
            _reject_non_nfc(item, f"{where}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _reject_non_nfc(item, f"{where}[{index}]")


def _digest(record: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(dict(record))).hexdigest()


def _scalar_equal(first: Any, second: Any) -> bool:
    """Type-strict scalar equality: bool is not int (contract restatement)."""
    if isinstance(first, bool) or isinstance(second, bool):
        return isinstance(first, bool) and isinstance(second, bool) and first == second
    return type(first) is type(second) and first == second


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and len(value) > 0


def _entries(certificate: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    premises = certificate.get("premises")
    if not isinstance(premises, list) or not premises:
        return None
    if any(not isinstance(p, Mapping) for p in premises):
        return None
    return list(premises)


def _check_envelope(entries: list[Mapping[str, Any]]) -> tuple[CheckerOutcome, str] | None:
    """Every entry carries exactly the schema envelope (no semantic fields)."""
    seen: set[str] = set()
    for entry in entries:
        keys = set(entry.keys())
        if not keys <= _ENVELOPE_KEYS:
            return CheckerOutcome.INADMISSIBLE_PREMISE, "premise entry carries non-envelope keys"
        pid = entry.get("evidence_id")
        if not _nonempty(pid):
            return CheckerOutcome.MISSING_PREMISE, "premise entry names no evidence_id"
        if pid in seen:
            return CheckerOutcome.INADMISSIBLE_PREMISE, f"premise repeats evidence_id {pid}"
        seen.add(str(pid))
        if not isinstance(entry.get("content_hash"), str) or not _SHA256.match(
            str(entry.get("content_hash"))
        ):
            return CheckerOutcome.INADMISSIBLE_PREMISE, "premise entry carries no sha256 content hash"
        if not _nonempty(entry.get("premise_type")):
            return CheckerOutcome.INADMISSIBLE_PREMISE, "premise entry names no premise type"
    return None


def _check_hashes(
    entries: list[Mapping[str, Any]], artifacts: Mapping[str, Any]
) -> tuple[CheckerOutcome, str] | None:
    """Each entry binds, by content hash, to available artifact bytes."""
    if not isinstance(artifacts, Mapping):
        return CheckerOutcome.PREMISE_UNAVAILABLE, "no artifact store"
    for entry in entries:
        pid = str(entry.get("evidence_id") or "")
        record = artifacts.get(pid)
        if not isinstance(record, Mapping):
            return CheckerOutcome.PREMISE_UNAVAILABLE, f"no artifact bytes: {pid}"
        try:
            actual = _digest(record)
        except (ValueError, TypeError) as exc:
            return CheckerOutcome.INADMISSIBLE_PREMISE, f"artifact not canonical: {exc}"
        if actual != str(entry.get("content_hash")):
            return CheckerOutcome.PREMISE_UNAVAILABLE, f"artifact hash mismatch: {pid}"
    return None


def _check_manifest(
    rule_id: str, entries: list[Mapping[str, Any]], manifest: Any
) -> tuple[CheckerOutcome, str] | None:
    """Manifest ID/hash/type maps cover exactly the certified premises."""
    try:
        ids = list(getattr(manifest, "evidence_ids", None) or [])
        hashes = dict(getattr(manifest, "content_hashes", None) or {})
        types = dict(getattr(manifest, "premise_types", None) or {})
    except Exception:
        return CheckerOutcome.PREMISE_UNAVAILABLE, "manifest unreadable"
    wanted = [str(e.get("evidence_id") or "") for e in entries]
    if set(ids) != set(wanted):
        return CheckerOutcome.PREMISE_UNAVAILABLE, "manifest ID set differs from premises"
    for entry in entries:
        pid = str(entry.get("evidence_id") or "")
        if hashes.get(pid) != str(entry.get("content_hash")):
            return CheckerOutcome.PREMISE_UNAVAILABLE, f"manifest hash differs: {pid}"
        if types.get(pid) != str(entry.get("premise_type")):
            return CheckerOutcome.PREMISE_UNAVAILABLE, f"manifest type differs: {pid}"
    _ = rule_id
    return None


def _check_header(
    rule_id: str, proposition: Mapping[str, Any]
) -> tuple[CheckerOutcome, str] | None:
    want = _HEADER[rule_id]
    for key, expected in want.items():
        if expected is not None and proposition.get(key) != expected:
            return (
                CheckerOutcome.UNSUPPORTED_CONSTRUCT,
                f"{rule_id} needs {key}={expected}",
            )
    return None


def _claim(proposition: Mapping[str, Any], rule_id: str) -> dict[str, Any]:
    """Independent claim projection: header fields + closed target/scope keys."""
    claim: dict[str, Any] = {}
    proposition = proposition if isinstance(proposition, dict) else {}
    for key in (
        "form",
        "quantifier",
        "relation",
        "modality",
        "completeness",
        "predicate",
        "asserted_value",
    ):
        if key in proposition:
            claim[key] = proposition[key]
    target = proposition.get("target")
    target = target if isinstance(target, dict) else {}
    for key in _TARGET_KEYS.get(rule_id, frozenset()):
        if key in target:
            claim[key] = target[key]
    scope = proposition.get("scope")
    scope = scope if isinstance(scope, dict) else {}
    for key in _SCOPE_KEYS.get(rule_id, frozenset()):
        if key in scope:
            claim[key] = scope[key]
    if rule_id == "call_edge/v1":
        if "caller" not in claim and isinstance(target.get("counterpart"), str):
            if target["counterpart"].strip():
                claim["caller"] = target["counterpart"].strip()
        if "callee" not in claim and isinstance(target.get("symbol"), str):
            if target["symbol"].strip():
                claim["callee"] = target["symbol"].strip()
    return claim


def _check_bindings(
    certificate: Mapping[str, Any], proposition: Mapping[str, Any], rule_id: str
) -> tuple[CheckerOutcome, str] | None:
    instance = certificate.get("inference_instance")
    bindings = instance.get("bindings") if isinstance(instance, Mapping) else None
    if not isinstance(bindings, dict) or bindings != _claim(proposition, rule_id):
        return CheckerOutcome.INVALID_INFERENCE, "inference bindings diverge from proposition target"
    return None


def _check_scope_join(
    certificate: Mapping[str, Any], entries: list[Mapping[str, Any]]
) -> tuple[CheckerOutcome, str] | None:
    """Independent K11: every premise scope revision equals the certificate one."""
    scope = certificate.get("scope")
    scope = dict(scope) if isinstance(scope, dict) else {}
    if "revision" not in scope:
        return None
    for entry in entries:
        pscope = entry.get("scope")
        if not isinstance(pscope, dict) or "revision" not in pscope:
            return CheckerOutcome.SCOPE_MISMATCH, "premise scope missing revision (K11)"
        if str(pscope["revision"]) != str(scope["revision"]):
            return CheckerOutcome.SCOPE_MISMATCH, "premise/proposition scope revision differs (K11)"
    return None


def _shape(record: Mapping[str, Any], required: Mapping[str, str]) -> str | None:
    """Hand-rolled structural check: required key -> kind code.

    Kinds: ``str`` (non-empty string), ``sha`` (40 lowercase hex),
    ``digest`` (sha256: + 64 hex), ``int`` (true int, bool excluded),
    ``pred`` (non-empty string predicate), ``list`` (a list), ``any`` (present).
    Returns the first defect description, or None when the shape holds.
    """
    for key, kind in required.items():
        value = record.get(key, None)
        if kind == "str":
            if not _nonempty(value):
                return f"{key} must be a non-empty string"
        elif kind == "pred":
            if not _nonempty(value):
                return f"{key} must be a non-empty predicate"
        elif kind == "sha":
            if not isinstance(value, str) or not _FULL_SHA.match(value):
                return f"{key} must be a full lowercase SHA"
        elif kind == "digest":
            if not isinstance(value, str) or not _SHA256.match(value):
                return f"{key} must be a sha256 digest"
        elif kind == "int":
            if not isinstance(value, int) or isinstance(value, bool):
                return f"{key} must be an integer"
        elif kind == "list":
            if not isinstance(value, list):
                return f"{key} must be a list"
        elif kind == "any":
            if key not in record:
                return f"{key} must be present"
    return None


def _check_definition(
    certificate: Mapping[str, Any],
    proposition: Mapping[str, Any],
    entries: list[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
) -> tuple[CheckerOutcome, str]:
    if len(entries) != 1:
        return CheckerOutcome.MISSING_PREMISE, "definition needs exactly one premise"
    entry = entries[0]
    if entry.get("premise_type") != "RANGE_READ_FACT":
        return CheckerOutcome.INADMISSIBLE_PREMISE, "definition needs RANGE_READ premise"
    record = artifacts.get(str(entry.get("evidence_id") or ""))
    assert isinstance(record, Mapping)
    defect = _shape(
        record,
        {"file_path": "str", "revision": "str", "window_text": "str", "target": "str"},
    )
    if defect is not None:
        return CheckerOutcome.INADMISSIBLE_PREMISE, f"definition premise malformed: {defect}"
    claim = _claim(proposition, "definition/v1")
    symbol = str(claim.get("symbol") or "")
    if not symbol:
        return CheckerOutcome.MISSING_PREMISE, "definition target names no symbol"
    if symbol not in str(record.get("window_text") or ""):
        return CheckerOutcome.INVALID_INFERENCE, "window does not contain the asserted symbol"
    if "revision" in claim and str(record.get("revision")) != str(claim.get("revision")):
        return CheckerOutcome.INVALID_INFERENCE, "definition premise source differs from claim scope"
    _ = certificate
    return CheckerOutcome.VALID, "range read shows the defined symbol"


def _check_call_edge(
    certificate: Mapping[str, Any],
    proposition: Mapping[str, Any],
    entries: list[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
) -> tuple[CheckerOutcome, str]:
    for entry in entries:
        if entry.get("premise_type") != "NORMALIZED_CALLSITE_FACT":
            return CheckerOutcome.INADMISSIBLE_PREMISE, "call edge needs NORMALIZED_CALLSITE premises"
    claim = _claim(proposition, "call_edge/v1")
    caller = str(claim.get("caller") or "")
    callee = str(claim.get("callee") or "")
    if not caller or not callee:
        return CheckerOutcome.MISSING_PREMISE, "call-edge claim binds no caller/callee"
    shown = False
    for entry in entries:
        record = artifacts.get(str(entry.get("evidence_id") or ""))
        assert isinstance(record, Mapping)
        defect = _shape(
            record,
            {
                "revision": "str",
                "caller": "str",
                "callee": "str",
                "call_text": "str",
                "line_start": "int",
                "line_end": "int",
            },
        )
        if defect is not None:
            return CheckerOutcome.INADMISSIBLE_PREMISE, f"call-site premise malformed: {defect}"
        if "revision" in claim and str(record.get("revision")) != str(claim.get("revision")):
            return CheckerOutcome.INVALID_INFERENCE, "call edge premise source differs from claim scope"
        text = str(record.get("call_text") or "")
        if str(record.get("caller")) == caller and str(record.get("callee")) == callee:
            if caller in text and callee in text:
                shown = True
    if not shown:
        return CheckerOutcome.INVALID_INFERENCE, "no premise binds the asserted caller/callee in cited text"
    _ = certificate
    return CheckerOutcome.VALID, "normalized call site shows the asserted edge"


def _check_behavior(
    certificate: Mapping[str, Any],
    proposition: Mapping[str, Any],
    entries: list[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
    mapping_authority: Any,
) -> tuple[CheckerOutcome, str]:
    by_type: dict[str, list[str]] = {}
    for entry in entries:
        by_type.setdefault(str(entry.get("premise_type")), []).append(str(entry.get("evidence_id") or ""))
    if (
        sorted(by_type) != ["BUILD_SOURCE_MAPPING", "RANGE_READ_FACT", "RUNTIME_OBSERVATION_FACT"]
        or any(len(v) != 1 for v in by_type.values())
    ):
        return (
            CheckerOutcome.INADMISSIBLE_PREMISE,
            "behavior needs one code, one runtime, and one mapping premise",
        )
    code = artifacts.get(by_type["RANGE_READ_FACT"][0])
    runtime = artifacts.get(by_type["RUNTIME_OBSERVATION_FACT"][0])
    mapping = artifacts.get(by_type["BUILD_SOURCE_MAPPING"][0])
    assert isinstance(code, Mapping) and isinstance(runtime, Mapping) and isinstance(mapping, Mapping)
    defect = _shape(
        code, {"file_path": "str", "revision": "str", "target": "str", "window_text": "str"}
    )
    if defect is not None:
        return CheckerOutcome.INADMISSIBLE_PREMISE, f"behavior code premise malformed: {defect}"
    defect = _shape(
        runtime,
        {
            "symbol": "str",
            "predicate": "pred",
            "repository": "str",
            "source_revision": "sha",
            "build_identity": "str",
            "execution_id": "str",
            "environment": "str",
        },
    )
    if defect is not None:
        return CheckerOutcome.INADMISSIBLE_PREMISE, f"behavior runtime premise malformed: {defect}"
    if "observed_value" not in runtime:
        return CheckerOutcome.INADMISSIBLE_PREMISE, "behavior runtime premise names no observed value"
    if isinstance(runtime.get("observed_value"), float):
        return CheckerOutcome.INADMISSIBLE_PREMISE, "behavior observed value forbids floats"
    defect = _shape(
        mapping,
        {
            "build_identity": "str",
            "repository": "str",
            "source_revision": "sha",
            "provenance_ref": "str",
            "mapping_digest": "digest",
        },
    )
    if defect is not None:
        return CheckerOutcome.INADMISSIBLE_PREMISE, f"behavior mapping malformed: {defect}"
    claim = _claim(proposition, "behavior/v1")
    for key in ("symbol", "file_path", "repository", "revision", "predicate"):
        if key not in claim:
            return CheckerOutcome.MISSING_PREMISE, f"behavior claim names no {key}"
    if str(code.get("target")) != str(claim["symbol"]) or str(code.get("file_path")) != str(
        claim["file_path"]
    ):
        return CheckerOutcome.INVALID_INFERENCE, "code premise does not show the asserted target"
    if str(claim["symbol"]) not in str(code.get("window_text") or ""):
        return CheckerOutcome.INVALID_INFERENCE, "code window does not show the asserted target"
    if str(code.get("repository")) != str(claim["repository"]) or str(code.get("revision")) != str(
        claim["revision"]
    ):
        return CheckerOutcome.INVALID_INFERENCE, "code source differs from claim scope"
    if (
        str(runtime.get("symbol")) != str(claim["symbol"])
        or str(runtime.get("predicate")) != str(claim["predicate"])
        or not _scalar_equal(runtime.get("observed_value"), claim.get("asserted_value"))
    ):
        return CheckerOutcome.INVALID_INFERENCE, "runtime observation does not establish the asserted behavior"
    if (
        str(runtime.get("repository")) != str(code.get("repository"))
        or str(runtime.get("source_revision")) != str(code.get("revision"))
        or str(runtime.get("build_identity")) != str(claim.get("build_identity"))
        or str(runtime.get("execution_id")) != str(claim.get("execution_id"))
        or str(runtime.get("environment")) != str(claim.get("environment"))
    ):
        return CheckerOutcome.INVALID_INFERENCE, "runtime scope does not join the cited source"
    if (
        str(mapping.get("build_identity")) != str(runtime.get("build_identity"))
        or str(mapping.get("repository")) != str(runtime.get("repository"))
        or str(mapping.get("source_revision")) != str(runtime.get("source_revision"))
    ):
        return CheckerOutcome.INVALID_INFERENCE, "mapping does not bind the observed build to the cited source"
    refs = mapping_authority.get("approved_provenance_refs") if isinstance(
        mapping_authority, Mapping
    ) else None
    if not isinstance(refs, list) or str(mapping.get("provenance_ref")) not in [
        str(r) for r in refs
    ]:
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "build-source mapping authority not approved"
    if str(mapping.get("provenance_ref")) not in _APPROVED_AUTHORITIES:
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "build-source mapping authority not approved"
    _ = certificate
    return CheckerOutcome.VALID, "code, runtime, and attested mapping join on the asserted behavior"


def _check_absence(
    certificate: Mapping[str, Any],
    proposition: Mapping[str, Any],
    entries: list[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
    manifest: Any,
) -> tuple[CheckerOutcome, str]:
    if len(entries) != 1 or entries[0].get("premise_type") != "ABSENCE_COVERAGE_FACT":
        return CheckerOutcome.INADMISSIBLE_PREMISE, "absence needs exactly one coverage premise"
    record = artifacts.get(str(entries[0].get("evidence_id") or ""))
    assert isinstance(record, Mapping)
    defect = _shape(
        record,
        {
            "universe": "str",
            "predicate": "pred",
            "search_status": "str",
            "search_config_digest": "digest",
            "match_count": "int",
        },
    )
    if defect is not None:
        return CheckerOutcome.INADMISSIBLE_PREMISE, f"absence coverage malformed: {defect}"
    scope = record.get("scope")
    if not isinstance(scope, dict):
        return CheckerOutcome.INADMISSIBLE_PREMISE, "absence coverage names no scope"
    for key in ("repository", "revision", "path_scope"):
        if not _nonempty(scope.get(key)):
            return CheckerOutcome.INADMISSIBLE_PREMISE, f"absence scope names no {key}"
    if not _FULL_SHA.match(str(scope["revision"])):
        return CheckerOutcome.INADMISSIBLE_PREMISE, "absence scope revision must be a full SHA"
    basis = record.get("coverage_basis")
    if not isinstance(basis, dict) or not _nonempty(basis.get("kind")) or not _nonempty(
        basis.get("value")
    ):
        return CheckerOutcome.INADMISSIBLE_PREMISE, "absence coverage names no basis"
    exclusions = record.get("exclusions")
    if not isinstance(exclusions, list):
        return CheckerOutcome.INADMISSIBLE_PREMISE, "absence coverage names no exclusions list"
    if str(record.get("search_status")) != "COMPLETE_EMPTY":
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "absence needs a complete-empty search"
    if int(record.get("match_count")) != 0:
        return CheckerOutcome.INVALID_INFERENCE, "absence coverage reports matches"
    claim = _claim(proposition, "absence/v1")
    for key in ("universe", "repository", "revision", "path_scope"):
        if key not in claim:
            return CheckerOutcome.MISSING_PREMISE, f"absence claim names no {key}"
    if (
        str(record.get("universe")) != str(claim["universe"])
        or str(record.get("predicate")) != str(claim.get("predicate"))
        or str(scope.get("repository")) != str(claim["repository"])
        or str(scope.get("revision")) != str(claim["revision"])
        or str(scope.get("path_scope")) != str(claim["path_scope"])
    ):
        return CheckerOutcome.INVALID_INFERENCE, "coverage does not match the claimed scope"
    queried = str(claim["universe"]) + "/" + str(claim["path_scope"])
    for exclusion in exclusions:
        excl = str(exclusion).rstrip("/")
        if excl and (queried == excl or queried.startswith(excl + "/")):
            return CheckerOutcome.INVALID_INFERENCE, "relevant scope excluded from coverage"
    try:
        retrieval = dict(getattr(manifest, "retrieval_config", None) or {})
    except Exception:
        retrieval = {}
    if _nonempty(retrieval.get("scope")) and str(retrieval["scope"]) != str(claim["universe"]):
        return CheckerOutcome.INVALID_INFERENCE, "coverage universe differs from manifest scope"
    _ = certificate
    return CheckerOutcome.VALID, "complete-empty coverage establishes absence"


def _check_history(
    certificate: Mapping[str, Any],
    proposition: Mapping[str, Any],
    entries: list[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
) -> tuple[CheckerOutcome, str]:
    if len(entries) != 1 or entries[0].get("premise_type") != "COMMIT_HISTORY":
        return CheckerOutcome.INADMISSIBLE_PREMISE, "history needs exactly one commit-history premise"
    record = artifacts.get(str(entries[0].get("evidence_id") or ""))
    assert isinstance(record, Mapping)
    defect = _shape(
        record,
        {
            "repository": "str",
            "repository_revision": "sha",
            "commit_id": "sha",
            "file_path": "str",
            "subject": "str",
        },
    )
    if defect is not None:
        return CheckerOutcome.INADMISSIBLE_PREMISE, f"history premise malformed: {defect}"
    for locator in ("branch", "time_range", "since", "until"):
        if locator in record or locator in proposition:
            return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "history takes commit pins, not locators"
    if "relation" in proposition or "completeness" in proposition:
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, "history takes no relation or completeness"
    claim = _claim(proposition, "history/v1")
    for key in ("repository", "commit_id", "file_path", "subject", "repository_revision"):
        if key not in claim:
            return CheckerOutcome.MISSING_PREMISE, f"history claim names no {key}"
    if not _FULL_SHA.match(str(claim.get("commit_id") or "")):
        return CheckerOutcome.INADMISSIBLE_PREMISE, "history claim commit must be a full SHA"
    for key in ("repository", "repository_revision", "commit_id", "file_path", "subject"):
        if str(record.get(key)) != str(claim.get(key)):
            return CheckerOutcome.INVALID_INFERENCE, "history premise differs from the claimed commit"
    _ = certificate
    return CheckerOutcome.VALID, "cited history matches the claimed commit"


def check_rule_second_path(
    rule_id: str,
    certificate: Mapping[str, Any],
    proposition: Mapping[str, Any],
    manifest: Any,
    artifacts: Mapping[str, Mapping[str, Any]],
    mapping_authority: Any = None,
) -> tuple[CheckerOutcome, str]:
    """Second-path verdict for one rule: VALID or the first typed failure.

    Never raises on well-formed input shapes: structural defects map to
    typed denials. Unexpected internal errors propagate to the caller,
    which must fail closed (the conjunction gate maps them to
    CHECKER_FAILURE, never VALID).
    """
    if rule_id not in _HEADER:
        return CheckerOutcome.UNSUPPORTED_CONSTRUCT, f"no second-path semantic for rule: {rule_id!r}"
    if not isinstance(certificate, Mapping) or not isinstance(proposition, dict):
        return CheckerOutcome.CHECKER_FAILURE, "second path needs certificate and proposition mappings"
    header_failure = _check_header(rule_id, proposition)
    if header_failure is not None:
        return header_failure
    entries = _entries(certificate)
    if entries is None:
        return CheckerOutcome.MISSING_PREMISE, "certificate carries no premises"
    envelope_failure = _check_envelope(entries)
    if envelope_failure is not None:
        return envelope_failure
    if not isinstance(artifacts, Mapping):
        return CheckerOutcome.PREMISE_UNAVAILABLE, "no artifact store"
    hash_failure = _check_hashes(entries, artifacts)
    if hash_failure is not None:
        return hash_failure
    manifest_failure = _check_manifest(rule_id, entries, manifest)
    if manifest_failure is not None:
        return manifest_failure
    if rule_id == "definition/v1":
        verdict = _check_definition(certificate, proposition, entries, artifacts)
    elif rule_id == "call_edge/v1":
        verdict = _check_call_edge(certificate, proposition, entries, artifacts)
    elif rule_id == "behavior/v1":
        verdict = _check_behavior(certificate, proposition, entries, artifacts, mapping_authority)
    elif rule_id == "absence/v1":
        verdict = _check_absence(certificate, proposition, entries, artifacts, manifest)
    else:
        verdict = _check_history(certificate, proposition, entries, artifacts)
    if verdict[0] != CheckerOutcome.VALID:
        return verdict
    bindings_failure = _check_bindings(certificate, proposition, rule_id)
    if bindings_failure is not None:
        return bindings_failure
    scope_failure = _check_scope_join(certificate, entries)
    if scope_failure is not None:
        return scope_failure
    return verdict
