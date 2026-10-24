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

import os
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
        )
    except Exception:
        return None
