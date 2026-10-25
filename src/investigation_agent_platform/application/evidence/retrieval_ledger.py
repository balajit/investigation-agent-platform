# src/investigation_agent_platform/application/evidence/retrieval_ledger.py
"""WP-A3 retrieval attempt ledger (Stage A, diagnostics-only).

Records every retrieval action as a :class:`RetrievalAttempt` carrying
the per-claim-capable ``search_query`` plus a distinct outcome:
``ROWS_RETURNED`` vs ``SCOPED_EMPTY`` (clean run, stated scope, zero rows)
vs ``UNSCOPED_EMPTY`` (clean run, no scope — never absence proof) vs
``SEARCH_FAILED`` (the search itself failed: timeout/auth/outage/misuse).

Novelty (rows never banked before in this investigation) and proof-lift
(a re-query turning a previously-empty query+scope into rows) are
measured per attempt. The per-(query, scope) attempt bound
(``IAP_WP_A3_MAX_ATTEMPTS``, default 3) is *recorded* (``over_budget``),
never enforced here: identical repeats already replay without
re-calling the provider (idempotency layer), and blocking or
auto-retrying retrieval is a behavior change reserved for a later
stage behind its own flag. Verdict-level lift (rows changing a
conclusion) is battery analysis, not runtime state.

Join to claims: attempts carry the query; the verify-time diagnostics
join cited evidence IDs to the attempts recorded on the sibling
``ActionOutcome`` (``evidence_ids`` + ``retrieval_attempt`` travel
together). All fields are JSON scalars so attempts stay replay-safe.
"""

from __future__ import annotations

import hashlib
import os
import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

#: Flag + default for the per-(query, scope) attempt bound.
WP_A3_MAX_ATTEMPTS_ENV = "IAP_WP_A3_MAX_ATTEMPTS"
WP_A3_DEFAULT_MAX_ATTEMPTS = 3

#: Cap on investigations held in the in-memory registry (oldest evicted).
_LEDGER_REGISTRY_CAP = 500


class RetrievalOutcome(str, Enum):
    """Distinct retrieval outcomes (frozen vocabulary).

    Values are frozen API: never rename or remove a code, only append
    new ones. The SCOPED_EMPTY / SEARCH_FAILED distinction is the point:
    a clean empty search over a stated scope is citable direction, a
    failed search is not evidence of anything. ``PARTIAL_ROWS`` closes
    the mixed-signal hole: rows returned alongside a timeout,
    truncation, rate limit, or failed source are never ordinary success.
    ``ROWS_RETURNED`` means rows with no observed partial/failure
    signal — not completeness, not sufficiency for proof.
    """

    ROWS_RETURNED = "ROWS_RETURNED"
    PARTIAL_ROWS = "PARTIAL_ROWS"
    SCOPED_EMPTY = "SCOPED_EMPTY"
    UNSCOPED_EMPTY = "UNSCOPED_EMPTY"
    SEARCH_FAILED = "SEARCH_FAILED"


class RetrievalAttempt(BaseModel):
    """One recorded retrieval attempt (diagnostics-only).

    ``attempt_index`` is the zero-based position among attempts for the
    same normalized (query, scope) key in this investigation;
    ``over_budget`` marks attempts past the bound; ``lifted`` marks a
    re-query that turned a previously-empty key into rows.
    """

    model_config = ConfigDict(frozen=True)

    search_query: str = Field(default="", max_length=1024)
    provider: str = Field(default="", max_length=128)
    scope: str | None = Field(default=None, max_length=512)
    outcome: RetrievalOutcome = RetrievalOutcome.UNSCOPED_EMPTY
    rows_returned: int = Field(default=0, ge=0)
    novel_rows: int = Field(default=0, ge=0)
    attempt_index: int = Field(default=0, ge=0)
    over_budget: bool = False
    lifted: bool = False
    miss_reason: str | None = Field(default=None, max_length=256)
    # Partial-result signals observed alongside returned rows
    # (deterministic order: failure markers, truncation, source states).
    # Empty for clean outcomes. Telemetry only — never proof of
    # completeness.
    signals: list[str] = Field(default_factory=list, max_length=10)
    # Bound in force when this attempt was recorded (remediation plan
    # ARCH-HIGH-013: the env-controlled bound rides with the attempt so
    # identical inputs under different settings are distinguishable).
    max_attempts: int = Field(default=WP_A3_DEFAULT_MAX_ATTEMPTS, ge=1)
    # G3 additive counts (diagnostics-only, default None = legacy/unknown).
    provider_total_count: int | None = Field(default=None, ge=0)
    banked_count: int | None = Field(default=None, ge=0)


#: miss_reason markers (lowercase substring) meaning the search itself
#: failed — never a clean empty. Kept explicit and snapshot-tested.
_FAILURE_MISS_MARKERS = frozenset(
    {
        "timeout",
        "auth_failed",
        "authorization_denied",
        "provider_unavailable",
        "unavailable",
        "rate_limited",
        "not_configured",
        "unsupported_capability",
        "connection",
        "missing_",
        "blocked",
        "denied",
    }
)

#: ActionOutcome statuses meaning the search never ran clean.
_FAILURE_STATUSES = frozenset({"FAILED", "BLOCKED"})


def partial_signals(
    miss_reason: str | None,
    truncated_rows: int = 0,
    source_statuses: list[str] | None = None,
) -> list[str]:
    """Deterministic partial-result signals for a row-returning attempt.

    Sorted and deduplicated so identical signal sets always produce the
    identical list. Empty means no partial/failure signal was observed.
    """
    signals: list[str] = []
    lowered_miss = str(miss_reason or "").strip().lower()
    if lowered_miss:
        for marker in sorted(_FAILURE_MISS_MARKERS):
            if marker in lowered_miss and f"failure:{marker}" not in signals:
                signals.append(f"failure:{marker}")
    try:
        truncated = int(truncated_rows or 0)
    except (TypeError, ValueError):
        truncated = 0
    if truncated > 0:
        signals.append(f"truncated:{truncated}")
    for status in source_statuses or []:
        normalized = str(status or "").strip().upper()
        if normalized and normalized != "SUCCEEDED":
            marker = f"source:{normalized}"
            if marker not in signals:
                signals.append(marker)
    return sorted(signals)


def classify_retrieval_outcome(
    status: str | None,
    miss_reason: str | None,
    rows_returned: int,
    scope: str | None,
    truncated_rows: int = 0,
    source_statuses: list[str] | None = None,
) -> RetrievalOutcome:
    """Map (action status, miss reason, row count, scope, signals) to outcome.

    Pure and total. Rows with any partial signal classify PARTIAL_ROWS —
    never ordinary success. Rows beat failure statuses only when no
    partial signal exists. Otherwise failures first, then scope presence
    decides the empty flavor.
    """
    try:
        rows = int(rows_returned or 0)
    except (TypeError, ValueError):
        rows = 0
    if rows > 0:
        if partial_signals(miss_reason, truncated_rows, source_statuses):
            return RetrievalOutcome.PARTIAL_ROWS
        return RetrievalOutcome.ROWS_RETURNED
    normalized_status = str(status or "").strip().upper()
    if normalized_status in _FAILURE_STATUSES:
        return RetrievalOutcome.SEARCH_FAILED
    lowered_miss = str(miss_reason or "").strip().lower()
    if lowered_miss and any(m in lowered_miss for m in _FAILURE_MISS_MARKERS):
        return RetrievalOutcome.SEARCH_FAILED
    if str(scope or "").strip():
        return RetrievalOutcome.SCOPED_EMPTY
    return RetrievalOutcome.UNSCOPED_EMPTY


def _normalize_key(query: str, scope: str | None) -> tuple[str, str]:
    return (str(query or "").strip()[:1024], str(scope or "").strip()[:512])


class RetrievalLedger:
    """In-memory per-investigation attempt accumulator (diagnostics-only).

    Tracks seen row IDs (novelty), per-key attempt counts (bound), and
    per-key empty history (lift). No I/O, no blocking, fail-open by
    contract — callers must additionally guard with try/except.
    """

    def __init__(self, max_attempts: int = WP_A3_DEFAULT_MAX_ATTEMPTS) -> None:
        try:
            bound = int(max_attempts)
        except (TypeError, ValueError):
            bound = WP_A3_DEFAULT_MAX_ATTEMPTS
        self.max_attempts = max(1, bound)
        self._seen_rows: set[str] = set()
        self._key_counts: dict[tuple[str, str], int] = {}
        self._key_empty: dict[tuple[str, str], bool] = {}

    def record(
        self,
        search_query: str,
        provider: str,
        scope: str | None,
        outcome: RetrievalOutcome,
        row_ids: list[str] | None,
        rows_returned: int,
        miss_reason: str | None = None,
        signals: list[str] | None = None,
        provider_total_count: int | None = None,
        banked_count: int | None = None,
    ) -> RetrievalAttempt:
        """Record one attempt, computing novelty/lift/bound flags."""
        key = _normalize_key(search_query, scope)
        index = self._key_counts.get(key, 0)
        self._key_counts[key] = index + 1
        novel = 0
        for raw in row_ids or []:
            rid = str(raw or "").strip()
            if rid and rid not in self._seen_rows:
                self._seen_rows.add(rid)
                novel += 1
        try:
            rows = int(rows_returned or 0)
        except (TypeError, ValueError):
            rows = 0
        was_empty = self._key_empty.get(key, False)
        if rows > 0:
            self._key_empty[key] = False
        elif outcome in (RetrievalOutcome.SCOPED_EMPTY, RetrievalOutcome.UNSCOPED_EMPTY):
            self._key_empty[key] = True
        return RetrievalAttempt(
            search_query=str(search_query or "")[:1024],
            provider=str(provider or "")[:128],
            scope=(str(scope)[:512] if scope else None),
            outcome=outcome,
            rows_returned=max(0, rows),
            novel_rows=novel,
            attempt_index=index,
            over_budget=index >= self.max_attempts,
            lifted=bool(was_empty and rows > 0),
            miss_reason=(str(miss_reason)[:256] if miss_reason else None),
            signals=[str(s)[:128] for s in (signals or [])][:10],
            max_attempts=self.max_attempts,
            provider_total_count=provider_total_count,
            banked_count=banked_count,
        )


_LEDGERS: dict[str, RetrievalLedger] = {}


def _ledger_for(investigation_id: str, max_attempts: int) -> RetrievalLedger:
    ledger = _LEDGERS.get(investigation_id)
    if ledger is None:
        if len(_LEDGERS) >= _LEDGER_REGISTRY_CAP:
            try:
                _LEDGERS.pop(next(iter(_LEDGERS)))
            except (StopIteration, KeyError):
                pass
        ledger = RetrievalLedger(max_attempts=max_attempts)
        _LEDGERS[investigation_id] = ledger
    return ledger


def _max_attempts_from_env() -> int:
    try:
        return max(1, int(os.environ.get(WP_A3_MAX_ATTEMPTS_ENV, WP_A3_DEFAULT_MAX_ATTEMPTS)))
    except (TypeError, ValueError):
        return WP_A3_DEFAULT_MAX_ATTEMPTS


#: Raw-parameter keys consulted (in order) for the human query string.
_QUERY_KEYS = (
    "query",
    "symbol",
    "symbol_name",
    "method_name",
    "tracking_id",
    "endpoint_id",
    "template_id",
    "session_id",
    "file_path",
    "path_prefix",
)


def extract_search_query(
    action_type: str, parameters: dict[str, object]
) -> tuple[str, str | None, str]:
    """Derive (search_query, scope, provider) from action parameters.

    Scope mirrors the ``_code_absence_scope`` conventions from raw
    parameters for code actions (repo@rev:subject); explicit
    ``parameters["scope"]`` wins when present; otherwise None (an
    unscoped search — honestly UNSCOPED_EMPTY when it finds nothing).
    """
    params = parameters if isinstance(parameters, dict) else {}
    query = ""
    for key in _QUERY_KEYS:
        try:
            candidate = str(params.get(key) or "").strip()
        except Exception:
            continue
        if candidate:
            query = candidate[:1024]
            break
    provider = ""
    try:
        provider = str(params.get("provider") or "").strip()[:128]
    except Exception:
        provider = ""
    scope: str | None = None
    try:
        explicit = str(params.get("scope") or "").strip()
        if explicit:
            scope = explicit[:512]
    except Exception:
        scope = None
    if scope is None:
        try:
            repo = str(params.get("repository") or "").strip()
            rev = str(params.get("revision") or "").strip() or "HEAD"
            action = str(action_type or "").strip().upper()
            if action == "SEARCH_CODE" and query:
                prefix = str(params.get("path_prefix") or "").strip() or "/"
                scope = f"{repo}@{rev}:{prefix}:query={query}"[:512]
            elif action == "FIND_SYMBOL":
                symbol = str(params.get("symbol_name") or "").strip()
                scope = f"{repo}@{rev}:symbol={symbol}"[:512] if symbol else None
            elif action == "FIND_CALL_GRAPH":
                symbol = str(params.get("symbol") or "").strip()
                direction = str(params.get("direction") or "BOTH").strip() or "BOTH"
                scope = f"{repo}@{rev}:callgraph={symbol}:{direction}"[:512] if symbol else None
            elif action == "GET_CODE_HISTORY":
                path = str(params.get("file_path") or "").strip()
                scope = f"{repo}:history={path}"[:512] if path else None
        except Exception:
            scope = None
    return query, scope, provider


def record_retrieval_attempt(
    investigation_id: str,
    action_type: str,
    parameters: dict[str, object],
    status: str | None,
    miss_reason: str | None,
    row_ids: list[str] | None,
    rows_returned: int = 0,
    truncated_rows: int = 0,
    source_statuses: list[str] | None = None,
    provider_total_count: int | None = None,
    banked_count: int | None = None,
) -> RetrievalAttempt | None:
    """Record one retrieval attempt for an investigation (fail-open).

    Builds the query/scope via :func:`extract_search_query`, classifies
    the outcome (rows plus any partial signal become PARTIAL_ROWS, never
    ordinary success), and accumulates novelty/lift/bound state in the
    investigation ledger. Returns None on any internal error — callers
    treat a missing attempt as "no diagnostics", never as failure.
    """
    try:
        query, scope, provider = extract_search_query(action_type, parameters)
        outcome = classify_retrieval_outcome(
            status,
            miss_reason,
            rows_returned,
            scope,
            truncated_rows=truncated_rows,
            source_statuses=source_statuses,
        )
        signals = (
            partial_signals(miss_reason, truncated_rows, source_statuses)
            if outcome == RetrievalOutcome.PARTIAL_ROWS
            else []
        )
        ledger = _ledger_for(str(investigation_id or ""), _max_attempts_from_env())
        return ledger.record(
            search_query=query,
            provider=provider,
            scope=scope,
            outcome=outcome,
            row_ids=row_ids,
            rows_returned=rows_returned,
            miss_reason=miss_reason,
            signals=signals,
            provider_total_count=provider_total_count,
            banked_count=banked_count,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# G3 per-claim retrieval binding + bounded re-retrieval directives.
# Diagnostics-only: no verdict, tier, status, or confidence change may be
# caused by anything below. Normative contract:
# docs/IAP-proof-kernel-g3-design-v1.0.md.
# ---------------------------------------------------------------------------

#: Caps for per-claim structures (diagnostics stay bounded).
MAX_CLAIM_ATTEMPTS = 10
MAX_PARTITION_IDS = 50

#: Action types the ledger directive may re-issue: exactly the set
#: extract_search_query() understands. Anything else is rejected.
RETRYABLE_ACTIONS = frozenset(
    {"SEARCH_CODE", "FIND_SYMBOL", "FIND_CALL_GRAPH", "GET_CODE_HISTORY"}
)


def _normalize_scope(value: object) -> str:
    """Scope normalization shared by marker, stamp, and budget count."""
    return str(value or "").strip()


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def claim_key(statement: str, claim_kind: str | None) -> str:
    """Deterministic claim key: normalized statement plus claim kind."""
    collapsed = re.sub(r"\s+", " ", str(statement or "").strip())
    kind = str(claim_kind or "").strip() or "UNTYPED"
    return f"{collapsed[:512]}|{kind[:32]}"


def claim_ref(claim_key_value: str) -> str:
    """Bounded claim reference (16 hex chars of sha256 over the claim key)."""
    return hashlib.sha256(str(claim_key_value or "").encode("utf-8")).hexdigest()[:16]


class ClaimRetrievalLedger(BaseModel):
    """Retrieval history for one claim: attempts behind its citations."""

    model_config = ConfigDict(frozen=True)

    claim_key: str = Field(description="claim_key() for the owning claim.")
    search_query: str = Field(default="", max_length=1024)
    scope: str | None = Field(
        default=None,
        max_length=512,
        description="Normalized retrieval scope paired with search_query.",
    )
    checkable: bool = Field(
        default=True,
        description="False for unfalsifiable claims, which never dispatch retrieval.",
    )
    attempt_indexes: list[int] = Field(
        default_factory=list,
        description="Positions into the investigation attempt list, oldest first.",
    )
    link_source: str = Field(
        default="id_overlap",
        description="How attempts were linked: id_overlap or query_match.",
    )
    target_ref: str = Field(
        default="",
        max_length=32,
        description="Target-distinctness fingerprint from _distinct_typed_claims "
        "(16 hex chars, or NO_TARGET / empty for untargeted claims). "
        "Pairs with claim_key to form the unique per-binding join identity: "
        "claim_key() alone covers statement+kind only and collides across "
        "target-distinct claims.",
    )


class ClaimRetrievalBinding(BaseModel):
    """Partition of one claim's cited IDs against what was retrieved."""

    model_config = ConfigDict(frozen=True)

    claim_key: str = Field(description="Full human-readable claim identity.")
    claim_ref: str = Field(
        description="16-hex bounded reference; the only claim form that "
        "enters length-capped fields such as EvaporatedId.cited_by."
    )
    claim_local: list[str] = Field(
        default_factory=list,
        description="Cited IDs retrieved for this claim (attempt-linked).",
    )
    bank_fallback: list[str] = Field(
        default_factory=list,
        description="Cited IDs found only in the investigation bank.",
    )
    unknown: list[str] = Field(
        default_factory=list,
        description="Cited IDs in neither set: evaporated, never silent.",
    )
    omitted_local: int = Field(default=0, ge=0)
    omitted_bank: int = Field(default=0, ge=0)
    omitted_unknown: int = Field(default=0, ge=0)
    unmapped: bool = Field(
        default=False,
        description="True when the claim has no linked attempts (legacy rows).",
    )
    bounded_search: bool = Field(
        default=False,
        description="True when the claim's only basis is a scoped empty that "
        "followed a truncated search of the same scope (diagnostic flag, "
        "never proof).",
    )
    target_ref: str = Field(
        default="",
        max_length=32,
        description="Target-distinctness fingerprint pairing with claim_key "
        "to form the unique per-binding join identity (see ClaimRetrievalLedger).",
    )


def partition_citations(
    cited_ids: list[str],
    local_ids: set[str],
    bank_ids: set[str],
) -> tuple[list[str], list[str], list[str]]:
    """Split cited IDs into claim-local, bank-fallback, and unknown sets."""
    cited = [str(i or "") for i in (cited_ids or []) if str(i or "")]
    local = {str(i or "") for i in (local_ids or set()) if str(i or "")}
    bank = {str(i or "") for i in (bank_ids or set()) if str(i or "")}
    claim_local = sorted(set(cited) & local)
    bank_fallback = sorted((set(cited) - local) & bank)
    unknown = sorted(set(cited) - local - bank)
    return claim_local, bank_fallback, unknown


def _normalize_target_mapping(target: object) -> dict[str, object]:
    """Best-effort mapping view of a claim target (never raises).

    Production assessments carry the Pydantic ``ClaimTarget`` model, not a
    plain dict — ``model_dump()`` is preferred so model instances and
    dict-shaped targets normalize to the same mapping. ``None``-valued
    entries are dropped so ``ClaimTarget(symbol="f")`` fingerprints
    identically to ``{"symbol": "f"}``. Anything unrecognizable yields {}.
    """
    try:
        dump = getattr(target, "model_dump", None)
        if callable(dump):
            dumped = dump()
            if isinstance(dumped, dict):
                return {k: v for k, v in dumped.items() if v is not None}
        if isinstance(target, dict):
            return {k: v for k, v in target.items() if v is not None}
    except Exception:
        pass
    return {}


def derive_claim_query(
    statement: str,
    claim_kind: str | None,
    claim_target: dict[str, object] | None,
) -> tuple[str, str | None]:
    """Best-effort (query, scope) for a claim, mirroring extract_search_query."""
    target = _normalize_target_mapping(claim_target)
    query = ""
    for key in ("symbol", "file_path", "counterpart", "commit_id"):
        try:
            candidate = str(target.get(key) or "").strip()
        except Exception:
            continue
        if candidate:
            query = candidate[:1024]
            break
    if not query:
        try:
            query = str(statement or "").strip()[:64]
        except Exception:
            query = ""
    scope: str | None = None
    try:
        explicit = str(target.get("scope") or "").strip()
        if explicit:
            scope = explicit[:512]
    except Exception:
        scope = None
    return query, scope


def attempt_id_for(
    investigation_id: str,
    iteration: int,
    action_type: str,
    query: str,
    scope: object,
) -> str:
    """Deterministic stable ID for one attempt (dedup across write paths)."""
    raw = "|".join(
        [
            str(investigation_id or ""),
            str(int(iteration or 0)),
            str(action_type or "").strip().upper(),
            str(query or "").strip(),
            _normalize_scope(scope),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_claim_ledger(
    statement: str,
    claim_kind: str | None,
    claim_target: dict[str, object] | None,
    cited_ids: list[str],
    bank_ids: set[str],
    attempts: list[tuple[list[str], dict[str, object] | None]],
    target_ref: str | None = None,
) -> tuple[ClaimRetrievalLedger, ClaimRetrievalBinding, list[str]]:
    """Join one claim to the investigation's recorded attempts (pure).

    ``target_ref`` is the _distinct_typed_claims target key for this
    group (fingerprint, NO_TARGET, or None for callers without grouping).
    It rides both records so target-distinct claims sharing a claim_key
    stay separately joinable downstream.
    """
    key = claim_key(statement, claim_kind)
    ref = claim_ref(key)
    try:
        target_value = str(target_ref or "")
    except Exception:
        target_value = ""
    query, scope = derive_claim_query(statement, claim_kind, claim_target)
    cited = [str(i or "") for i in (cited_ids or []) if str(i or "")]
    linked: list[int] = []
    local_ids: set[str] = set()
    link_source = "id_overlap"
    for index, (banked, attempt) in enumerate(attempts or []):
        banked_set = {str(i or "") for i in (banked or []) if str(i or "")}
        overlap = set(cited) & banked_set
        if overlap:
            linked.append(index)
            local_ids |= overlap
            continue
        if isinstance(attempt, dict) and query:
            if str(attempt.get("search_query") or "") == query:
                linked.append(index)
                link_source = "query_match"
    linked = linked[:MAX_CLAIM_ATTEMPTS]
    bank_set = {str(i or "") for i in (bank_ids or set()) if str(i or "")}
    if not linked:
        bank_fallback = sorted(set(cited) & bank_set)
        unknown = sorted(set(cited) - bank_set)
        return (
            ClaimRetrievalLedger(
                claim_key=key,
                search_query=query,
                scope=scope,
                checkable=bool(query),
                attempt_indexes=[],
                link_source="id_overlap",
                target_ref=target_value,
            ),
            ClaimRetrievalBinding(
                claim_key=key,
                claim_ref=ref,
                claim_local=[],
                bank_fallback=bank_fallback[:MAX_PARTITION_IDS],
                unknown=unknown[:MAX_PARTITION_IDS],
                omitted_bank=max(0, len(bank_fallback) - MAX_PARTITION_IDS),
                omitted_unknown=max(0, len(unknown) - MAX_PARTITION_IDS),
                unmapped=True,
                target_ref=target_value,
            ),
            unknown,
        )
    claim_local, bank_fallback, unknown = partition_citations(cited, local_ids, bank_ids)
    return (
        ClaimRetrievalLedger(
            claim_key=key,
            search_query=query,
            scope=scope,
            checkable=bool(query),
            attempt_indexes=linked,
            link_source=link_source,
            target_ref=target_value,
        ),
        ClaimRetrievalBinding(
            claim_key=key,
            claim_ref=ref,
            claim_local=claim_local[:MAX_PARTITION_IDS],
            bank_fallback=bank_fallback[:MAX_PARTITION_IDS],
            unknown=unknown[:MAX_PARTITION_IDS],
            omitted_local=max(0, len(claim_local) - MAX_PARTITION_IDS),
            omitted_bank=max(0, len(bank_fallback) - MAX_PARTITION_IDS),
            omitted_unknown=max(0, len(unknown) - MAX_PARTITION_IDS),
            target_ref=target_value,
        ),
        unknown,
    )


def _distinct_typed_claims(hypothesis: object) -> list[dict[str, object]]:
    """Group one hypothesis's citations by distinct typed claim.

    Each group carries ``target_key`` (the target fingerprint, or
    NO_TARGET) alongside kind/statement/cited_ids so downstream join
    records stay target-distinct even when claim_key() collides.
    """
    groups: dict[tuple[str, str], dict[str, object]] = {}
    assessments: list[object] = []
    try:
        raw = getattr(hypothesis, "assessments", None) or []
        assessments = list(raw) if isinstance(raw, list) else []
    except Exception:
        assessments = []
    try:
        statement = str(getattr(hypothesis, "statement", "") or "")
    except Exception:
        statement = ""
    for item in assessments:
        try:
            kind_value = str(getattr(item, "claim_kind", None) or "") or "UNTYPED"
        except Exception:
            kind_value = "UNTYPED"
        try:
            target = getattr(item, "claim_target", None)
            target_dict = _normalize_target_mapping(target)
        except Exception:
            target_dict = {}
        try:
            fingerprint = hashlib.sha256(
                str(sorted((str(k), str(v)) for k, v in target_dict.items())).encode("utf-8")
            ).hexdigest()[:16]
        except Exception:
            fingerprint = "NOTARGET"
        target_key = fingerprint if target_dict else "NO_TARGET"
        group_key = (kind_value, target_key)
        entry = groups.get(group_key)
        if entry is None:
            entry = {
                "statement": statement,
                "kind_value": kind_value,
                "target_dict": target_dict,
                "target_key": target_key,
                "cited_ids": [],
            }
            groups[group_key] = entry
        try:
            cited = list(getattr(item, "cited_ids", None) or [])
        except Exception:
            cited = []
        existing = entry.get("cited_ids")
        if isinstance(existing, list):
            for cid in cited:
                text = str(cid or "")
                if text and text not in existing:
                    existing.append(text)
    return list(groups.values())


def re_retrieval_directive(
    binding: ClaimRetrievalBinding,
    ledger: ClaimRetrievalLedger,
    latest_outcome: str | None,
    recorded_attempts: list[tuple[list[str], dict[str, object] | None]],
    action_type: str,
    parameters: dict[str, object] | None,
    max_attempts: int = WP_A3_DEFAULT_MAX_ATTEMPTS,
) -> dict[str, object] | None:
    """Bounded re-query directive for one unsatisfied claim, or None.

    Eligibility and budget use the durable persisted count for the
    normalized (query, scope) key. ``recorded_attempts`` is required —
    the timeline-backed list (see :func:`recorded_retrieval_attempts`);
    there is no caller-index fallback. Process-local indexes are
    telemetry only and never enter this function.
    """
    try:
        budget = max(1, int(max_attempts))
    except (TypeError, ValueError):
        budget = WP_A3_DEFAULT_MAX_ATTEMPTS
    durable = count_persisted_attempts(
        recorded_attempts, ledger.search_query, ledger.scope
    )
    if durable >= budget:
        return None
    if not ledger.search_query:
        return None
    if str(action_type or "").upper() not in RETRYABLE_ACTIONS:
        return None
    if not isinstance(parameters, dict):
        return None
    if not all(isinstance(k, str) and _is_scalar(v) for k, v in parameters.items()):
        return None
    failed_retry = str(latest_outcome or "") == RetrievalOutcome.SEARCH_FAILED.value
    unknown_retry = bool(binding.unknown) and durable == 0
    if not (failed_retry or unknown_retry):
        return None
    return {
        "action": "RETRIEVE",
        "action_type": str(action_type).upper(),
        "action_parameters": {str(k): v for k, v in parameters.items()},
        "query": ledger.search_query,
        "scope": _normalize_scope(ledger.scope),
        "claim_ref": binding.claim_ref,
        "attempt_index": durable,
        "max_attempts": budget,
    }


def recorded_retrieval_attempts(events: list) -> list[tuple[list[str], dict]]:
    """(banked evidence_ids, attempt dict) for RETRIEVAL_ATTEMPT events only.

    The marker carries the stable attempt id at top-level
    ``attributes["attempt_id"]`` (the nested ``attempt`` dump predates it
    and has no id field of its own). It is stamped into the returned
    dict when absent so the durable counter can de-duplicate retries
    and same-turn/persisted pairs; a nested id already present is never
    overwritten.
    """
    out: list[tuple[list[str], dict]] = []
    for event in events or []:
        event_type = getattr(event, "event_type", None)
        if str(getattr(event_type, "value", event_type)) != "RETRIEVAL_ATTEMPT":
            continue
        attributes = getattr(event, "attributes", None) or {}
        attempt = attributes.get("attempt") if isinstance(attributes, dict) else None
        banked = attributes.get("evidence_ids") if isinstance(attributes, dict) else []
        attempt_dict = dict(attempt) if isinstance(attempt, dict) else {}
        if isinstance(attributes, dict):
            top_id = attributes.get("attempt_id")
            if top_id and not attempt_dict.get("attempt_id"):
                attempt_dict["attempt_id"] = str(top_id)
        out.append(
            ([str(i) for i in (banked or []) if str(i)], attempt_dict)
        )
    return out


def recorded_attempt_summaries(entries: list) -> list[tuple[list[str], dict]] | None:
    """(banked evidence_ids, attempt dict) from workflow history summaries.

    Same-turn actions not yet flushed to the timeline table carry their
    attempt under ``entry["retrieval_attempt"]`` with banked ids under
    ``entry["evidence_ids"]``. Entries without an attempt dict
    contribute nothing. The entry's top-level ``attempt_id`` is carried
    into the attempt dict when absent so the durable counter
    de-duplicates same-turn/persisted pairs.

    Returns None when no entry yields a pair — unavailable history,
    distinct from confirmed-empty — so directive selection fails
    closed instead of authorizing a retry on zero knowledge.
    """
    out: list[tuple[list[str], dict]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        attempt = entry.get("retrieval_attempt")
        if not isinstance(attempt, dict):
            continue
        attempt_dict = dict(attempt)
        top_id = entry.get("attempt_id")
        if top_id and not attempt_dict.get("attempt_id"):
            attempt_dict["attempt_id"] = str(top_id)
        banked = entry.get("evidence_ids") or []
        out.append(
            ([str(i) for i in (banked or []) if str(i)], attempt_dict)
        )
    return out or None


def merge_recorded_attempts(
    persisted: list[tuple[list[str], dict]] | None,
    summaries: list[tuple[list[str], dict]] | None,
) -> list[tuple[list[str], dict]]:
    """Persisted timeline pairs plus genuinely new summary pairs.

    A summary pair whose ``attempt_id`` matches a persisted pair is the
    same attempt before/after flushing — kept once (persisted copy
    wins; both carry the same payload). Pairs without an id cannot be
    matched and always join. Persisted order is preserved first so the
    durable attempt index keeps its meaning; summaries append after.
    """
    seen_ids: set[str] = set()
    for _banked, attempt in persisted or []:
        if isinstance(attempt, dict):
            attempt_id_value = attempt.get("attempt_id")
            if attempt_id_value:
                seen_ids.add(str(attempt_id_value))
    out = list(persisted or [])
    for banked, attempt in summaries or []:
        if isinstance(attempt, dict):
            attempt_id_value = attempt.get("attempt_id")
            if attempt_id_value:
                key = str(attempt_id_value)
                if key in seen_ids:
                    continue
                seen_ids.add(key)
        out.append((banked, attempt))
    return out


def count_persisted_attempts(
    recorded_attempts: list[tuple[list[str], dict[str, object] | None]],
    query: str,
    scope: object,
) -> int:
    """Count durable attempts for one normalized query/scope key.

    Attempts carrying the same stable ``attempt_id`` (retries, or a
    same-turn attempt echoed next to its persisted marker) count once.
    Attempts without an id keep legacy behavior and each count.
    """
    normalized_query = str(query or "").strip()
    normalized_scope = _normalize_scope(scope)
    seen_ids: set[str] = set()
    count = 0
    for _banked, attempt in recorded_attempts or []:
        if not isinstance(attempt, dict):
            continue
        if str(attempt.get("search_query") or "").strip() != normalized_query:
            continue
        if _normalize_scope(attempt.get("scope")) != normalized_scope:
            continue
        attempt_id_value = attempt.get("attempt_id")
        if attempt_id_value:
            key = str(attempt_id_value)
            if key in seen_ids:
                continue
            seen_ids.add(key)
        count += 1
    return count


def directive_consumed(
    marker: dict[str, object],
    history_entries: list[dict[str, object]],
    recorded_attempts: list[tuple[list[str], dict[str, object] | None]],
) -> bool:
    """True only when the directive's requery already happened or budgeted out."""
    claim_ref_value = str(marker.get("claim_ref") or "")
    query = str(marker.get("query") or "").strip()
    scope = _normalize_scope(marker.get("scope"))
    issued_turn = marker.get("turn_issued", -1)
    try:
        issued_turn = int(issued_turn)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        issued_turn = -1
    budget = marker.get("max_attempts", WP_A3_DEFAULT_MAX_ATTEMPTS)
    try:
        budget = max(1, int(budget))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        budget = WP_A3_DEFAULT_MAX_ATTEMPTS
    if count_persisted_attempts(recorded_attempts or [], query, scope) >= budget:
        return True
    for entry in history_entries or []:
        if not isinstance(entry, dict):
            continue
        try:
            entry_turn = int(entry.get("iteration", entry.get("turn", -1)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if entry_turn <= issued_turn:
            continue
        stamp = entry.get("forced_requery")
        if not isinstance(stamp, dict):
            continue
        if (
            str(stamp.get("claim_ref") or "") == claim_ref_value
            and str(stamp.get("query") or "").strip() == query
            and _normalize_scope(stamp.get("scope")) == scope
        ):
            return True
    return False
