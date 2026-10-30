"""Expected-vs-actual divergence and assertion evaluation (D4).

Pure helpers: diff golden payloads against observed bodies, and evaluate
field assertions against parsed row dicts. FAILs are machine-checked
evidence (absence counts); unparseable inputs skip, never raise.
"""

from __future__ import annotations

from typing import Any

_MAX_DIVERGENCES = 25


class _Missing:
    pass


_MISSING = _Missing()


def _scalar(value: Any) -> Any:
    try:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)[:500]
    except Exception:
        return None


def diff_expected_actual(expected: Any, actual: Any, path: str = "") -> list[dict[str, Any]]:
    """Recursive diff of golden vs observed (pure, deterministic).

    Returns [{path, kind, expected, actual}] with kind in
    missing/unexpected/mismatch. Dict keys union; lists compared by index
    up to the longer length; scalars by !=. Bounded at 25 entries.
    """
    try:
        out: list[dict[str, Any]] = []

        def _walk(exp: Any, act: Any, at: str) -> None:
            if len(out) >= _MAX_DIVERGENCES:
                return
            label = at or "$"
            if isinstance(exp, dict) and isinstance(act, dict):
                for key in exp:
                    _walk(
                        exp[key],
                        act.get(key, None) if key in act else _MISSING,
                        f"{label}.{key}" if label != "$" else str(key),
                    )
                for key in act:
                    if key not in exp:
                        if len(out) < _MAX_DIVERGENCES:
                            out.append(
                                {
                                    "path": f"{label}.{key}" if label != "$" else str(key),
                                    "kind": "unexpected",
                                    "expected": None,
                                    "actual": _scalar(act[key]),
                                }
                            )
                return
            if isinstance(exp, list) and isinstance(act, list):
                for index in range(max(len(exp), len(act))):
                    _walk(
                        exp[index] if index < len(exp) else _MISSING,
                        act[index] if index < len(act) else _MISSING,
                        f"{label}[{index}]",
                    )
                return
            if exp is _MISSING or act is _MISSING:
                out.append(
                    {
                        "path": label,
                        "kind": "missing" if act is _MISSING else "unexpected",
                        "expected": None if exp is _MISSING else _scalar(exp),
                        "actual": None if act is _MISSING else _scalar(act),
                    }
                )
                return
            if (
                type(exp) is not type(act)
                and not (isinstance(exp, (int, float)) and isinstance(act, (int, float)))
                or exp != act
            ):
                out.append(
                    {
                        "path": label,
                        "kind": "mismatch",
                        "expected": _scalar(exp),
                        "actual": _scalar(act),
                    }
                )

        _walk(expected, actual, path or "")
        return out
    except Exception:
        return []


def evaluate_assertions(rows: list[dict[str, Any]], assertions: list[Any]) -> list[dict[str, Any]]:
    """Evaluate field assertions over parsed rows (pure, deterministic).

    Returns FAIL entries [{field, op, expected, observed, row}]. PASS rows
    produce nothing (no budget burn on success). Unparseable rows and
    unknown ops skip. Row index (not content) identifies the row.
    """
    try:
        fails: list[dict[str, Any]] = []
        for index, row in enumerate(list(rows or [])):
            if not isinstance(row, dict):
                continue
            for assertion in assertions or []:
                try:
                    if isinstance(assertion, dict):
                        field = str(assertion.get("field", "") or "")
                        op = str(assertion.get("op", "") or "").lower()
                        value = assertion.get("value", None)
                    else:
                        field = str(getattr(assertion, "field", "") or "")
                        op = str(getattr(assertion, "op", "") or "").lower()
                        value = getattr(assertion, "value", None)
                    if not field or op not in (
                        "present",
                        "absent",
                        "equals",
                        "one_of",
                        "gte",
                        "lte",
                    ):
                        continue
                    present = field in row and row[field] is not None
                    failed = False
                    if op == "present":
                        failed = not present
                    elif op == "absent":
                        failed = present
                    elif not present:
                        failed = True
                    elif op == "equals":
                        failed = row[field] != value
                    elif op == "one_of":
                        failed = row[field] not in (
                            list(value) if isinstance(value, list) else [value]
                        )
                    elif op in ("gte", "lte"):
                        try:
                            observed_num = float(row[field])  # type: ignore[arg-type]
                            bound = float(value)  # type: ignore[arg-type]
                            failed = observed_num < bound if op == "gte" else observed_num > bound
                        except (TypeError, ValueError):
                            failed = True
                    if failed:
                        fails.append(
                            {
                                "field": field,
                                "op": op,
                                "expected": _scalar(value),
                                "observed": _scalar(row.get(field, None)),
                                "row": index,
                            }
                        )
                except Exception:
                    continue
        return fails
    except Exception:
        return []


from datetime import UTC, datetime

from investigation_agent_platform.domain.evidence.models import (
    Evidence,
    EvidenceType,
)
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def shape_divergence_evidence(
    *,
    tenant_id: str,
    investigation_id: Any,
    source_ref: str,
    endpoint: str,
    divergence: dict,
) -> Any:
    """Build a DIVERGENCE evidence row from a machine-checked diff (pure-ish).

    D4: absence/mismatch verified in code banks as citable evidence.
    Fingerprint scopes per investigation (same doctrine as log rows).
    """

    now = datetime.now(UTC)
    path = str(divergence.get("path", "") or "")
    kind = str(divergence.get("kind", "") or "")
    exp_text = str(divergence.get("expected", ""))[:300]
    act_text = str(divergence.get("actual", ""))[:300]
    title = f"Divergence [{kind}] {path}".strip()[:200]
    summary = (
        f"Expected-vs-actual divergence at '{path}': {kind} "
        f"(expected {exp_text!r}, observed {act_text!r})."
    )[:2000]
    fingerprint = f"divergence:{tenant_id}:{investigation_id}:{endpoint}:{path}:{kind}"
    return Evidence(
        tenant_id=tenant_id,
        investigation_id=investigation_id,
        evidence_type=EvidenceType.DIVERGENCE,
        provider="divergence-engine",
        source=f"divergence://{endpoint}/{path}",
        title=title,
        summary=summary,
        content_snippet=summary,
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            provider_type="divergence-engine",
            requested_provider_id="divergence-engine",
            actual_provider_id="divergence-engine",
            source_system="expected-vs-actual",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="divergence-engine",
                operation="compare",
                normalized_query_hash=fingerprint[:64],
            ),
            source_location=SourceLocation(
                system="divergence-engine",
                identifier=source_ref[:256],
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=fingerprint[:512],
    )
