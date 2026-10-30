# src/investigation_agent_platform/domain/evidence/qualification.py
"""S4 shape-aware evidence qualification (DES-S4-qualification v1.0), version-pinned.

Qualification judges readiness, never answer quality: a row qualifies iff it
carries the pinned fields its layer requires. Thin banks (a single
uninformative root commit) pass readiness and fail downstream with a targeted
next fetch — readiness must not smuggle in verdicts.

Pure: operates on Evidence objects or API-shaped dicts via tolerant accessors.
Version-pinned via QUALIFIER_VERSION (never env-switched); unmarked rows are
stamped current at bank time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

QUALIFIER_VERSION = "s4q-v1"

CODE_LAYER_TYPES = frozenset({"SOURCE_CODE", "EXCEPTION_PATH"})
HISTORY_LAYER_TYPES = frozenset({"COMMIT_HISTORY"})
RUNTIME_LAYER_TYPES = frozenset({"RUNTIME_LOG", "RUNTIME_TRACE", "LOG", "TRACE", "METRIC"})
STATE_LAYER_TYPES = frozenset({"DATABASE_STATE"})
PROBE_LAYER_TYPES = frozenset({"API_PROBE"})
DIVERGENCE_LAYER_TYPES = frozenset({"DIVERGENCE"})

# Rows carrying these miss reasons are infrastructure failures: present in
# the corpus, never qualifying, never citable as support.
FAILURE_MISS_REASONS = frozenset(
    {"timeout", "auth_failed", "provider_unavailable", "internal_error"}
)


@dataclass(frozen=True)
class Qualification:
    """One row's readiness verdict (never an answer-quality verdict)."""

    qualified: bool
    layer: str
    reason: str
    version: str = QUALIFIER_VERSION


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _etype(item: Any) -> str:
    try:
        etype = (
            item.get("evidence_type")
            if isinstance(item, dict)
            else getattr(item, "evidence_type", "")
        )
    except Exception:
        return ""
    try:
        return str(getattr(etype, "value", etype) or "").upper()
    except Exception:
        return ""


def _attrs(item: Any) -> dict:
    try:
        attrs = (
            item.get("attributes") if isinstance(item, dict) else getattr(item, "attributes", None)
        )
    except Exception:
        return {}
    return dict(attrs) if isinstance(attrs, dict) else {}


def _qualifier_version(item: Any) -> str | None:
    value = _attrs(item).get("s4_qualifier_version")
    text = str(value or "").strip()
    return text or None


def _location(item: Any) -> tuple[str, Any, Any, str]:
    """(file_path, line_start, line_end, revision) — dicts and models alike."""
    try:
        if isinstance(item, dict):
            prov = item.get("provenance") or {}
            loc = prov.get("source_location", {}) if isinstance(prov, dict) else {}
            if isinstance(loc, dict):
                return (
                    str(loc.get("file_path") or ""),
                    loc.get("line_start"),
                    loc.get("line_end"),
                    str(loc.get("revision") or ""),
                )
            return "", None, None, ""
        prov = getattr(item, "provenance", None)
        loc = getattr(prov, "source_location", None)
        return (
            str(getattr(loc, "file_path", "") or ""),
            getattr(loc, "line_start", None),
            getattr(loc, "line_end", None),
            str(getattr(loc, "revision", "") or ""),
        )
    except Exception:
        return "", None, None, ""


def _as_utc(value: Any) -> datetime | None:
    """Tolerant timestamp read: datetimes pass through (naive assumed UTC),
    ISO strings parse, everything else is None."""
    try:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value
        if isinstance(value, str) and value.strip():
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
    except Exception:
        return None
    return None


def _observed_at(item: Any) -> datetime | None:
    """Return the provider observation time; retrieval is not event time."""
    try:
        if isinstance(item, dict):
            return _as_utc(item.get("observed_at"))
        return _as_utc(getattr(item, "observed_at", None))
    except Exception:
        return None


def _source_identity(item: Any) -> str:
    """Provenance identity for runtime/state rows (never empty when pinned)."""
    try:
        if isinstance(item, dict):
            prov = item.get("provenance") or {}
            if isinstance(prov, dict):
                for key in ("source_system", "provider_type", "actual_provider_id"):
                    value = str(prov.get(key) or "").strip()
                    if value:
                        return value
            return ""
        prov = getattr(item, "provenance", None)
        for key in ("source_system", "provider_type", "actual_provider_id"):
            value = str(getattr(prov, key, "") or "").strip()
            if value:
                return value
        return ""
    except Exception:
        return ""


def _failure_reason(item: Any) -> str:
    reason = str(_attrs(item).get("miss_reason") or "").strip().lower()
    return reason if reason in FAILURE_MISS_REASONS else ""


def _unqualified(layer: str, reason: str) -> Qualification:
    return Qualification(qualified=False, layer=layer, reason=reason)


def qualify_code_row(item: Any) -> Qualification:
    """A code row qualifies iff all four pinned fields are present.

    Start-line-only rows are not ready: the previous start-line-only
    admission let schema-legal but unusable rows into readiness.
    """
    failure = _failure_reason(item)
    if failure:
        return _unqualified("code", f"infrastructure failure row ({failure})")
    file_path, line_start, line_end, revision = _location(item)
    if not file_path:
        return _unqualified("code", "no file path")
    if not _is_int(line_start):
        return _unqualified("code", "no pinned line-start")
    if not _is_int(line_end):
        return _unqualified("code", "start-line-only row is not ready")
    if not revision:
        return _unqualified("code", "no pinned revision")
    return Qualification(qualified=True, layer="code", reason="4 pinned fields present")


def qualify_history_row(item: Any, scope_path: str | None = None) -> Qualification:
    """Positive history rows qualify on type + path + revision + scope
    relevance. Zero-match rows are proof candidates, not positive rows
    (route those to qualify_absence_proof)."""
    failure = _failure_reason(item)
    if failure:
        return _unqualified("history", f"infrastructure failure row ({failure})")
    attrs = _attrs(item)
    if bool(attrs.get("discovery_only")):
        return _unqualified("history", "discovery-only row never qualifies")
    count = attrs.get("match_count")
    if isinstance(count, int) and not isinstance(count, bool) and count == 0:
        return _unqualified("history", "zero-match row is a proof candidate, not a positive row")
    file_path, _, _, revision = _location(item)
    if not file_path:
        return _unqualified("history", "no file path")
    if not revision:
        return _unqualified("history", "no pinned revision")
    if scope_path:
        want = str(scope_path).replace("\\", "/").strip().strip("/")
        got = file_path.replace("\\", "/").strip()
        if want and got != want and not got.startswith(want + "/"):
            return _unqualified("history", f"row outside requested scope {scope_path}")
    return Qualification(
        qualified=True, layer="history", reason="commit row pinned to path+revision"
    )


def _scope_covers(proof_scope: str, scope: str) -> bool:
    # Aligned with the verifier's absence gate (exact scope equality,
    # case-insensitive): readiness must never pass what verification
    # rejects, so substring matching is refused here.
    proof = (proof_scope or "").strip().casefold()
    want = (scope or "").strip().casefold()
    return bool(proof and want) and proof == want


def qualify_absence_proof(item: Any, scope: str) -> Qualification:
    """Complete empty-history proofs qualify on a separate branch: a
    match_count==0 row whose search scope covers the claimed scope, pinned
    to a revision. Mirrors the verifier's history-absence gate
    (discovery-only rows never prove; revision required)."""
    failure = _failure_reason(item)
    if failure:
        return _unqualified("absence-proof", f"infrastructure failure row ({failure})")
    attrs = _attrs(item)
    if bool(attrs.get("discovery_only")):
        return _unqualified("absence-proof", "discovery-only row never proves absence")
    count = attrs.get("match_count")
    if not (isinstance(count, int) and not isinstance(count, bool) and count == 0):
        return _unqualified("absence-proof", "not a zero-match proof row")
    if not scope or not scope.strip():
        return _unqualified("absence-proof", "absence claim states no scope")
    if not _scope_covers(str(attrs.get("search_scope") or ""), scope):
        return _unqualified("absence-proof", f"no scoped empty-search proof covers '{scope}'")
    _, _, _, revision = _location(item)
    if not revision:
        return _unqualified("absence-proof", "proof row has no pinned revision")
    return Qualification(
        qualified=True, layer="absence-proof", reason="scoped zero-match proof pinned"
    )


def qualify_runtime_row(
    item: Any, window: tuple[datetime, datetime] | None = None
) -> Qualification:
    """Runtime rows qualify on window pinning plus source identity.

    Without a window there is nothing to pin against, so the row cannot
    qualify (recorded, never dropped).
    """
    failure = _failure_reason(item)
    if failure:
        return _unqualified("runtime", f"infrastructure failure row ({failure})")
    identity = _source_identity(item)
    if not identity:
        return _unqualified("runtime", "no source identity")
    if window is None:
        return _unqualified("runtime", "no window to pin against")
    try:
        start, end = window
    except Exception:
        return _unqualified("runtime", "malformed window")
    observed = _observed_at(item)
    if observed is None:
        return _unqualified("runtime", "no event timestamp to pin")
    if observed < start or observed > end:
        return _unqualified("runtime", "event outside investigation window")
    return Qualification(
        qualified=True, layer="runtime", reason="window-pinned with source identity"
    )


def qualify_state_row(item: Any) -> Qualification:
    """State rows qualify on source identity (point-in-time reads carry
    their own observed/retrieved timestamps)."""
    failure = _failure_reason(item)
    if failure:
        return _unqualified("state", f"infrastructure failure row ({failure})")
    if not _source_identity(item):
        return _unqualified("state", "no source identity")
    return Qualification(qualified=True, layer="state", reason="state identity present")


def qualify_probe_row(item: Any) -> Qualification:
    """Probe rows qualify when the probe ran without infrastructure
    failure (the result itself is judged downstream, never here)."""
    failure = _failure_reason(item)
    if failure:
        return _unqualified("probe", f"infrastructure failure row ({failure})")
    return Qualification(qualified=True, layer="probe", reason="probe executed")


def qualify_divergence_row(item: Any) -> Qualification:
    """Machine-checked expected-vs-actual rows qualify like probes: the
    check ran, the verdict belongs downstream."""
    failure = _failure_reason(item)
    if failure:
        return _unqualified("divergence", f"infrastructure failure row ({failure})")
    return Qualification(qualified=True, layer="divergence", reason="machine-checked diff present")


def qualify_item(
    item: Any,
    scope_path: str | None = None,
    window: tuple[datetime, datetime] | None = None,
) -> Qualification:
    """Dispatch one banked row to its layer predicate (never raises)."""
    try:
        stored_version = _qualifier_version(item)
        if stored_version is not None and stored_version != QUALIFIER_VERSION:
            return _unqualified("unknown", f"unsupported qualifier version {stored_version!r}")
        etype = _etype(item)
        attrs = _attrs(item)
        count = attrs.get("match_count")
        if isinstance(count, int) and not isinstance(count, bool) and count == 0:
            # Zero-match rows are proof candidates, never positive rows —
            # regardless of layer. The proof covers its own stated scope
            # when the caller has no question scope yet (bank time); scope-
            # to-question matching still happens at verify time.
            return qualify_absence_proof(item, scope_path or str(attrs.get("search_scope") or ""))
        if etype in CODE_LAYER_TYPES:
            return qualify_code_row(item)
        if etype in HISTORY_LAYER_TYPES:
            return qualify_history_row(item, scope_path)
        if etype in RUNTIME_LAYER_TYPES:
            return qualify_runtime_row(item, window)
        if etype in STATE_LAYER_TYPES:
            return qualify_state_row(item)
        if etype in PROBE_LAYER_TYPES:
            return qualify_probe_row(item)
        if etype in DIVERGENCE_LAYER_TYPES:
            return qualify_divergence_row(item)
        if not etype:
            return _unqualified("unknown", "row has no evidence type")
        return _unqualified("unknown", f"layer {etype} not readiness-counted in v1")
    except Exception as exc:
        return _unqualified("unknown", f"qualification failed closed: {type(exc).__name__}")


def qualify_contrast_half(item: Any) -> tuple[Any, bool, str]:
    """One contrast half: present, timestamped, and sourced.

    Lighter than full layer qualification on purpose — the old half sits
    at/before G, outside the investigation window, so window pinning can
    never apply to it. The joint window-ordering (pair level) does the
    real work; P3 binding judges whether the pair proves anything.
    Returns (timestamp-or-None, ok, reason).
    """
    try:
        failure = _failure_reason(item)
        if failure:
            return None, False, f"infrastructure failure row ({failure})"
        ts = _observed_at(item)
        if ts is None:
            return None, False, "contrast half has no event timestamp"
        identity = _source_identity(item)
        file_path, _, _, _ = _location(item)
        if not identity and not file_path:
            return None, False, "contrast half has no source identity"
        return ts, True, "half present, timestamped, and sourced"
    except Exception as exc:
        return None, False, f"qualification failed closed: {type(exc).__name__}"


def qualify_contrast_pair(
    old_item: Any, new_item: Any, g: Any = None, t: Any = None
) -> Qualification:
    """Contrast pairs qualify jointly: both halves present and
    window-ordered, old at/before G and new at/after T when the
    endpoints are given. The code row encoding the broken assumption is
    bound downstream (P3), never here.
    """
    try:
        old_ts, old_ok, old_reason = qualify_contrast_half(old_item)
        if not old_ok:
            return _unqualified("contrast", f"old half: {old_reason}")
        new_ts, new_ok, new_reason = qualify_contrast_half(new_item)
        if not new_ok:
            return _unqualified("contrast", f"new half: {new_reason}")
        if new_ts < old_ts:
            return _unqualified("contrast", "contrast halves misordered")
        if g is not None:
            g_ts = _as_utc(g)
            if g_ts is not None and old_ts > g_ts:
                return _unqualified("contrast", "old half is newer than G")
        if t is not None:
            t_ts = _as_utc(t)
            if t_ts is not None and new_ts < t_ts:
                return _unqualified("contrast", "new half is older than T")
        return Qualification(
            qualified=True, layer="contrast", reason="halves present and window-ordered"
        )
    except Exception as exc:
        return _unqualified("contrast", f"qualification failed closed: {type(exc).__name__}")


def _contrast_coverage(
    items: Any, window: tuple[datetime, datetime] | None, t_anchored: bool
) -> bool:
    """Readiness-level contrast: an old-side half (at/before window start)
    and a new-side half both present. New side is at/after window end when
    onset T is anchored, else anywhere in-window-or-newer (current-state
    rows are the new half when T is unknown).
    """
    try:
        if window is None:
            return False
        start, end = window
        old_times: list[datetime] = []
        new_times: list[datetime] = []
        new_bar = end if t_anchored else start
        for item in items or []:
            ts, ok, _ = qualify_contrast_half(item)
            if not ok or ts is None:
                continue
            if ts <= start:
                old_times.append(ts)
            if ts >= new_bar:
                new_times.append(ts)
        return any(old < new for old in old_times for new in new_times)
    except Exception:
        return False


_POSITIVE_LAYERS = frozenset({"code", "history", "runtime", "state", "probe", "divergence"})


def s4_ready(
    question_shape: str,
    qualifications: list[Qualification],
    scope: str | None = None,
    window: tuple[datetime, datetime] | None = None,
    items: Any = None,
    t_anchored: bool = False,
) -> tuple[bool, str, str]:
    """Shape-aware readiness: passes iff qualifying evidence is banked.

    Returns (ready, missing, satisfied_by). missing names the gap for
    downstream discriminating-check construction (P4); satisfied_by names
    the layer that carried readiness (or the contrast branch).
    Qualification never judges answer quality — thin banks pass here and
    fail downstream. Contrast coverage needs the raw items plus the
    window; without them the contrast branch is skipped (never assumed).
    """
    _ = scope
    quals = list(qualifications or [])
    by_layer: dict[str, int] = {}
    for qual in quals:
        if qual.qualified:
            by_layer[qual.layer] = by_layer.get(qual.layer, 0) + 1
    positive = any(by_layer.get(layer, 0) > 0 for layer in _POSITIVE_LAYERS)
    absence = by_layer.get("absence-proof", 0) > 0
    shape = (question_shape or "unknown").strip().lower()
    contrast = _contrast_coverage(items, window, t_anchored)

    def _absence_branch() -> tuple[bool, str, str] | None:
        if absence and not positive:
            return True, "", "absence-proof"
        return None

    if shape == "code":
        if by_layer.get("code", 0) > 0:
            return True, "", "code"
        if by_layer.get("history", 0) > 0:
            return True, "", "history"
        if contrast:
            return True, "", "contrast"
        # No absence fallback: the contract requires a qualified
        # code/history row. A proven absence is an S6 verdict on a
        # complete empty-history proof, never S4 readiness.
        return False, "qualified code row (4 pinned fields) or pinned history row", ""
    if shape == "state":
        if by_layer.get("state", 0) > 0:
            return True, "", "state"
        # No absence fallback: the contract requires qualified state
        # evidence for state questions.
        return False, "qualified state row with source identity", ""
    if shape == "log":
        if by_layer.get("runtime", 0) > 0:
            return True, "", "runtime"
        if contrast:
            return True, "", "contrast"
        branch = _absence_branch()
        if branch is not None:
            return branch
        return False, "window-pinned runtime row or scoped absence proof", ""
    # Unknown shape: any qualified layer carries readiness — never
    # auto-pass on unqualified rows.
    for layer in ("code", "history", "runtime", "state", "probe", "divergence"):
        if by_layer.get(layer, 0) > 0:
            return True, "", layer
    if contrast:
        return True, "", "contrast"
    branch = _absence_branch()
    if branch is not None:
        return branch
    return False, "any qualified evidence row", ""
