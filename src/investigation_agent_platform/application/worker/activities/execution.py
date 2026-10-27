# src/investigation_agent_platform/application/worker/activities/execution.py
"""Temporal activity thin adapters — each delegates to the application/domain layer via AppContext."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from temporalio import activity

try:
    from temporalio.exceptions import ApplicationFailure  # type: ignore[attr-defined]
except ImportError:
    from temporalio.exceptions import (
        ApplicationError as ApplicationFailure,  # type: ignore[assignment, no-redef]
    )

logger = logging.getLogger(__name__)

from investigation_agent_platform.infrastructure.observability.tracing import traced

from .common import (
    _ACTION_REQUIRED_EVIDENCE_TYPE_LOCAL,
    COMP_ACTVTY,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_SKIPPED,
    OUTCOME_STARTED,
    _abandon_reservation,
    _activity_extra,
    _activity_outcome,
    _application_failure_from_exc,
    _coerce_root_ids,
    _coerced_data,
    _complete_reservation,
    _emit_telemetry,
    _persist_log_exemplars,
    _reserve_or_replay,
    _split_log_bypass,
    _story_debug,
    apply_effective_limits,
    classify_provider_failure,
    elapsed_ms,
    overlay_evidence_budget,
    story_log,
    tick,
)
from .inputs import ExecuteActionInput, GenericActivityResult, _get_ctx


def _symbol_evidence_packets(
    *,
    tenant_id: Any,
    investigation_id: Any,
    symbols: list[Any],
    symbol_name: str,
    now: Any | None = None,
) -> list[Any]:
    """Convert gateway symbol metadata into bankable discovery evidence.

    Phase 0.2: the gateway deliberately returns symbol results in
    ``provider_metadata`` with ``items=[]``. Callers that only read
    ``items`` therefore bank nothing (FIND_SYMBOL/TRACE_LOGIC were silent
    no-ops). Each symbol becomes a SOURCE_CODE row pinned to the location
    the provider actually observed, flagged ``discovery_only`` because the
    local provider cannot pin to a caller-supplied revision.
    """
    import hashlib as _hashlib

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

    observed_at = now or datetime.now(UTC)
    packets: list[Any] = []
    for raw in (symbols or [])[:50]:
        if not isinstance(raw, dict):
            continue
        location = raw.get("location") or {}
        if not isinstance(location, dict):
            continue
        file_path = str(location.get("file_path", "") or "").strip()
        try:
            line_number = int(location.get("line_number") or 0)
        except (TypeError, ValueError):
            line_number = 0
        name = str(raw.get("name", "") or symbol_name or "?")[:256]
        kind = str(raw.get("kind", "") or "symbol")[:64]
        snippet = str(location.get("snippet") or raw.get("signature") or "")[:4000]
        revision = str(location.get("revision", "") or "") or None
        revision_supported = bool(location.get("revision_supported", False))
        fingerprint = _hashlib.sha256(
            f"symbol:{tenant_id}:{investigation_id}:{name}:{file_path}:{line_number}".encode()
        ).hexdigest()[:64]
        packets.append(
            Evidence(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                evidence_type=EvidenceType.SOURCE_CODE,
                provider="code-intelligence",
                source=f"code://{file_path or '?'}#L{line_number or 0}",
                title=f"Symbol: {name} ({kind})",
                summary=(
                    f"Definition candidate '{name}' ({kind}) at "
                    f"{file_path or 'unknown file'}"
                    f"{':' + str(line_number) if line_number else ''}."
                ),
                content_snippet=snippet,
                attributes={
                    "symbol_name": name,
                    "symbol_kind": kind,
                    "discovery_only": True,
                    "revision_supported": revision_supported,
                },
                observed_at=observed_at,
                retrieved_at=observed_at,
                provenance=EvidenceProvenance(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    provider_type="code-intelligence",
                    requested_provider_id="treesitter-local",
                    actual_provider_id="treesitter-local",
                    source_system="symbol-index",
                    retrieval_timestamp=observed_at,
                    query_fingerprint=QueryFingerprint(
                        provider_type="code-intelligence",
                        operation="find_symbol",
                        normalized_query_hash=_hashlib.sha256(
                            str(symbol_name).encode()
                        ).hexdigest(),
                    ),
                    source_location=SourceLocation(
                        system="Code",
                        identifier=file_path or name,
                        file_path=file_path or None,
                        line_start=line_number or None,
                        line_end=line_number or None,
                        revision=revision,
                    ),
                ),
                freshness=EvidenceFreshness(observed_at=observed_at, retrieved_at=observed_at),
                fingerprint=fingerprint,
            )
        )
    return packets


def _with_fetch_reason(item: Any, fetch_reason: str) -> Any:
    """Stamp a banked row's attributes with a smallest-fetch reason (pure).

    Phase 4 (claim binding): the workflow-only smallest-fetch context
    reaches the bank boundary here — never through provider parameters.
    Empty reason returns the item unchanged. Dict and BaseModel
    (frozen) shapes are both tolerated; anything else passes through.
    Never raises.
    """
    try:
        reason = str(fetch_reason or "")
        if not reason or not reason.startswith("smallest_fetch:"):
            return item
        if isinstance(item, dict):
            attrs = dict(item.get("attributes", {}) or {})
            attrs["fetch_reason"] = reason
            item["attributes"] = attrs
            return item
        try:
            attrs = dict(getattr(item, "attributes", None) or {})
        except Exception:
            return item
        attrs["fetch_reason"] = reason
        try:
            return item.model_copy(update={"attributes": attrs})
        except Exception:
            return item
    except Exception:
        return item


def _banked_positive(items: list) -> bool:
    """True when banking produced citable positive evidence.

    D1 (claim-proof gaps): the Git history provider returns a zero-match
    proof row on empty walks. That row must not suppress the
    discovery-only absence row — only positive evidence does. A row
    counts as positive unless it is an explicit zero-match marker.
    """
    for item in items or []:
        try:
            attrs = getattr(item, "attributes", None) or {}
            count = attrs.get("match_count") if isinstance(attrs, dict) else None
        except Exception:
            count = None
        if isinstance(count, int) and not isinstance(count, bool) and count == 0:
            continue
        return True
    return False


def _absence_proof_row(
    *,
    tenant_id: Any,
    investigation_id: Any,
    evidence_type: Any,
    provider: str,
    operation: str,
    scope: str,
    now: Any | None = None,
) -> Any:
    """Bankable scoped empty-search proof (Phase 2 item 3).

    An empty result alone never proves absence — but an empty result over
    a stated, complete scope does, when paired with the code path that
    would have produced the fact. This row carries the scope and the zero
    count the verifier's absence gate requires. Fingerprint is
    deterministic per investigation+scope, so repeated identical searches
    re-save idempotently (the repo dedupes on tenant+fingerprint) instead
    of flooding evidence. discovery_only: a no-match is direction for
    further reads, never a definition.
    """
    import hashlib as _hashlib

    from investigation_agent_platform.domain.evidence.models import (
        Evidence,
    )
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    observed_at = now or datetime.now(UTC)
    fingerprint = _hashlib.sha256(
        f"absence:{tenant_id}:{investigation_id}:{scope}".encode()
    ).hexdigest()[:64]
    return Evidence(
        tenant_id=tenant_id,
        investigation_id=investigation_id,
        evidence_type=evidence_type,
        provider=provider,
        source=f"absence://{scope[:256]}",
        title=f"No matches in scope: {scope[:160]}",
        summary=f"Scoped search over {scope} returned zero matches.",
        content_snippet=f"No matches in scope: {scope}",
        attributes={
            "search_scope": scope,
            "match_count": 0,
            "discovery_only": True,
        },
        observed_at=observed_at,
        retrieved_at=observed_at,
        provenance=EvidenceProvenance(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            provider_type="search",
            requested_provider_id=provider,
            actual_provider_id=provider,
            source_system="absence-proof",
            retrieval_timestamp=observed_at,
            query_fingerprint=QueryFingerprint(
                provider_type="search",
                operation=operation,
                normalized_query_hash=_hashlib.sha256(scope.encode()).hexdigest(),
            ),
            source_location=SourceLocation(
                system="search",
                identifier=f"absence:{scope[:256]}",
            ),
        ),
        freshness=EvidenceFreshness(observed_at=observed_at, retrieved_at=observed_at),
        fingerprint=fingerprint,
    )


def _code_absence_scope(action_label: str, req: Any) -> str:
    """Human-readable search scope for a code-intelligence request (pure).

    Empty when the request carries no searchable subject — an absence
    proof over an empty query would be meaningless.
    """
    try:
        repo = str(getattr(req, "repository", "") or "").strip()
        rev = str(getattr(req, "revision", "") or "").strip() or "HEAD"
        if action_label == "SEARCH_CODE":
            query = str(getattr(req, "query", "") or "").strip()
            prefix = str(getattr(req, "path_prefix", "") or "").strip() or "/"
            if not query:
                return ""
            return f"{repo}@{rev}:{prefix}:query={query}"[:512]
        if action_label == "FIND_SYMBOL":
            symbol = str(getattr(req, "symbol_name", "") or "").strip()
            return f"{repo}@{rev}:symbol={symbol}"[:512] if symbol else ""
        if action_label == "FIND_CALL_GRAPH":
            symbol = str(getattr(req, "symbol", "") or "").strip()
            direction = str(getattr(req, "direction", "") or "BOTH")
            return f"{repo}@{rev}:callgraph={symbol}:{direction}"[:512] if symbol else ""
        if action_label == "GET_CODE_HISTORY":
            path = str(getattr(req, "file_path", "") or "").strip()
            return f"{repo}:history={path}"[:512] if path else ""

    except Exception:
        pass
    return ""


def _frame_file_matches(frame_file: Any, hit_file: Any) -> bool:
    """True when a search hit plausibly belongs to a stack frame's file.

    Phase 0.3: previously any hit with a file+revision was accepted, so a
    frame could be written onto a different file. A frame with no file hint
    accepts any hit; a frame with a path requires a suffix match, a frame
    with only a basename requires an equal basename.
    """
    frame = str(frame_file or "").replace("\\", "/").strip().strip("/")
    hit = str(hit_file or "").replace("\\", "/").strip().strip("/")
    if not frame:
        return bool(hit)
    if not hit:
        return False

    if frame == hit:
        return True
    if "/" in frame:
        return hit.endswith("/" + frame) or hit == frame
    return hit.rsplit("/", 1)[-1] == frame

def _stack_fingerprint(
    tenant_id: Any, investigation_id: Any, index: int, file_path: Any, line_no: int
) -> str:
    """Bounded fingerprint for stack-walk rows (pure).

    D2: Evidence.fingerprint caps at 128 chars; hash the identity
    instead of embedding long repo-relative paths.
    """

    try:
        import hashlib as _hashlib

        raw = f"stack:{tenant_id}:{investigation_id}:{index}:{file_path}:{line_no}"
        return _hashlib.sha256(raw.encode()).hexdigest()[:64]
    except Exception:
        return f"stack-{index}"

async def _fuse_journey_findings(
    *,
    tenant_id: str,
    investigation_id: Any,
    finding_repo: Any,
    evidence_repo: Any,
) -> None:
    """Merge step OBSERVATION findings into one incident finding (G-4).

    Fires only when JOURNEY spine evidence exists and two or more
    OBSERVATION findings were persisted: unions evidence IDs, concatenates
    statements (capped), and persists a single incident-level OBSERVATION.
    No-op otherwise. Fail-open by contract (caller catches).
    """
    from investigation_agent_platform.domain.evidence.models import EvidenceType
    from investigation_agent_platform.domain.finding.models import Finding, FindingType

    observations: list[Any] = []
    try:
        found, _ = await finding_repo.list_findings(tenant_id, investigation_id, limit=50)
        observations = [
            f
            for f in (found or [])
            if str(getattr(getattr(f, "finding_type", ""), "value", getattr(f, "finding_type", "")))
            == "OBSERVATION"
        ]
    except Exception:
        return
    if len(observations) < 2:
        return
    try:
        has_journey = False
        if evidence_repo is not None:
            rows = await evidence_repo.find_by_investigation_id(tenant_id, investigation_id)
            for row in rows or []:
                try:
                    etype = str(
                        getattr(
                            getattr(row, "evidence_type", ""),
                            "value",
                            getattr(row, "evidence_type", ""),
                        )
                    )
                except Exception:
                    continue
                if etype == str(EvidenceType.JOURNEY.value):
                    has_journey = True
                    break
        if not has_journey:
            return
    except Exception:
        return
    try:
        evidence_ids: list[Any] = []
        seen: set[str] = set()
        statements: list[str] = []
        related: list[Any] = []
        for finding in observations:
            for eid in list(getattr(finding, "evidence_ids", []) or [])[:100]:
                key = str(eid)
                if key not in seen:
                    seen.add(key)
                    evidence_ids.append(eid)
            statement = str(getattr(finding, "statement", "") or "").strip()
            if statement:
                statements.append(statement[:400])
            for hid in list(getattr(finding, "related_hypothesis_ids", []) or [])[:50]:
                if hid not in related:
                    related.append(hid)
        if not statements:
            return
        fused = Finding(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            finding_type=FindingType.OBSERVATION,
            title=f"Incident fusion across {len(observations)} journey observations",
            statement=" | ".join(statements)[:2000],
            evidence_ids=evidence_ids[:100],
            related_hypothesis_ids=related[:50],
            confidence=min(
                0.95,
                max([float(getattr(f, "confidence", 0.0) or 0.0) for f in observations] or [0.0]),
            ),
        )
        await finding_repo.save_finding_record(tenant_id, fused)
    except Exception:
        return

def _shape_search_keywords(raw: Any) -> list[str]:
    """Split sentence-like keyword blobs into distinctive tokens (pure).

    IAP audit: compound complaints arrive as one long keyword string and
    match nothing. Elements longer than ~60 chars split on non-word runs;
    tokens len>=4 kept (stopwords dropped), capped at 20, order-stable,
    deduped. Short keyword lists pass through untouched.
    """
    try:
        import re as _re

        stopwords = frozenset(
            {
                "what",
                "where",
                "when",
                "which",
                "does",
                "doing",
                "have",
                "has",
                "had",
                "with",
                "from",
                "that",
                "this",
                "these",
                "those",
                "there",
                "their",
                "about",
                "show",
                "implementation",
                "slices",
                "lack",
            }
        )
        flat: list[str] = []
        items = raw if isinstance(raw, list) else [raw]
        for element in items:
            try:
                text = str(element or "").strip()
            except Exception:
                continue
            if not text:
                continue
            if len(text) <= 60:
                if text not in flat:
                    flat.append(text)
                continue
            for token in _re.split(r"[^a-z0-9]+", text.lower()):
                if len(token) >= 4 and token not in stopwords and token not in flat:
                    flat.append(token)
                if len(flat) >= 20:
                    break
            if len(flat) >= 20:
                break
        return flat[:20]
    except Exception:
        try:
            return [str(v) for v in (raw or []) if str(v or "").strip()][:20]
        except Exception:
            return []

def _caller_chain_targets(edges: list[dict], limit: int = 5) -> list[dict]:
    """Caller-role graph edges worth an automatic call-site read.

    Callers first, one target per distinct file (a second edge in the
    same file shares the ±40-line window), bounded so a hub symbol
    cannot flood the turn. Module-level for unit testability. L2
    Helper-drop fix: the chaining step previously read only the first
    edge, so the second caller never got a citable range row.
    """
    targets: list[dict] = []
    seen_files: set[str] = set()
    ordered = sorted(
        (e for e in edges if isinstance(e, dict)),
        key=lambda e: 0 if e.get("role") == "caller" else 1,
    )
    for edge in ordered:
        if edge.get("role") != "caller":
            continue
        file_path = str(edge.get("file") or "").strip()
        line = edge.get("line")
        if not file_path or not isinstance(line, int) or file_path in seen_files:
            continue
        seen_files.add(file_path)
        targets.append(edge)
        if len(targets) >= max(1, limit):
            break
    return targets

async def fallback_call_graph_to_search(
    *,
    gateway: Any,
    tenant_id: str,
    investigation_id: Any,
    application_id: Any,
    parameters: dict,
    evidence_repo: Any,
    bank: Any,
    record_outcome: Any,
    outcome_detail: dict,
    inv_id_str: Any = "",
    union: bool = False,
) -> int:
    """B-5: empty graph (no call_graph.json / stale checkout) degrades
    to a SEARCH_CODE keyword query for the symbol instead of EMPTY.

    The Step-1 L2 run ANSWERED via caller-keyword rows while the
    Step-2 rerun took the graph path to 0 edges and FAILED — same
    question, different retrieval luck. This makes the keyword
    fallback deterministic: graph miss always searches. Banked rows
    carry miss_reason so the reasoner knows they are keyword rows,
    not true graph edges. Module-level for unit testability.

    Union mode (union=True): the graph banked a row but one side is
    empty (0 callers or 0 callees) — graph incompleteness, not proof
    of absence. Keyword rows supplement (union) instead of replacing,
    marked call_graph_partial_search_union.
    """
    from investigation_agent_platform.domain.evidence.requests import (
        CodeSearchRequest,
    )

    symbol = str((parameters or {}).get("symbol", "") or "").strip()
    if not symbol:
        return 0
    marker = "call_graph_partial_search_union" if union else "call_graph_empty_search_fallback"
    try:
        qres = await gateway.search_code(
            tenant_id,
            investigation_id,
            application_id or "example-app",
            CodeSearchRequest(
                repository=application_id or "example-app",
                revision=str((parameters or {}).get("revision", "HEAD")),
                query=symbol,
                language=(parameters or {}).get("language"),
                limit=20,
            ),
        )
    except Exception as exc:
        story_log(
            logger,
            logging.WARNING,
            "FIND_CALL_GRAPH keyword fallback failed; keeping empty",
            comp=COMP_ACTVTY,
            func="fallback_call_graph_to_search",
            stage="execute",
            step="fallback",
            outcome=OUTCOME_SKIPPED,
            tenant_id=tenant_id,
            investigation_id=inv_id_str,
            event="call graph fallback failed",
            error=str(exc),
        )
        return 0
    banked = bank(qres.items)
    if evidence_repo is not None and banked:
        await evidence_repo.save_batch(tenant_id, banked, investigation_id)
    if banked:
        outcome_detail["miss_reason"] = marker
    return record_outcome(banked)


def _stored_claim_query(raw_claim: object) -> tuple[str | None, str | None]:
    """Extract the stored retrieval hint from a FORMULATE claim mapping.

    Pure function; never raises. Returns (search_query, search_scope),
    each bounded or None. Operates on the already-sanitized claim mapping.
    """
    try:
        mapping = raw_claim if isinstance(raw_claim, dict) else {}
        query = mapping.get("search_query")
        scope = mapping.get("search_scope")
    except Exception:
        return (None, None)
    query_text = (
        str(query).strip()[:1024] if isinstance(query, str) and str(query).strip() else None
    )
    scope_text = str(scope).strip()[:512] if isinstance(scope, str) and str(scope).strip() else None
    return (query_text, scope_text)


def _scope_path_prefix(scope: object) -> str | None:
    """Path-prefix form of a stored scope, or None (pure).

    Stored scopes are "path prefix or repository@revision". Only the
    plain-path form maps to the SEARCH_CODE ``path_prefix`` parameter;
    a ``repository@revision`` scope carries no path information, so the
    handler keeps its default ("/") rather than searching a fabricated
    prefix. Never raises.
    """
    try:
        text = str(scope or "").strip()
    except Exception:
        return None
    if not text or "@" in text:
        return None
    return text


@activity.defn
@traced("worker", outcome_from=_activity_outcome, extra_from=_activity_extra)
async def execute_action_activity(
    params: ExecuteActionInput,
) -> GenericActivityResult:
    t0 = tick()
    data = _coerced_data(ExecuteActionInput, params, activity="execute_action_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    inv_id_probe = str(data.get("investigation_id", ""))
    story_log(
        logger,
        logging.INFO,
        "execute_action_activity started",
        comp=COMP_ACTVTY,
        func="execute_action_activity",
        stage="execute",
        step="start",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        investigation_id=inv_id_probe,
        event="execute_action_activity started",
        inputs={"action": str(data.get("action", "QUERY_STATE"))},
        action=str(data.get("action", "QUERY_STATE")),
    )
    # Debug story: full post-forcing dispatch input. Diff against
    # story.reason_out next_action/action_parameters to see forcing.
    _story_debug(
        tenant_id=tenant_id_probe,
        investigation_id=inv_id_probe,
        event="story.execute_in",
        action=str(data.get("action", "QUERY_STATE")),
        parameters=dict(data.get("parameters", {}) or {}),
        hypotheses=list(data.get("hypotheses", []) or []),
        claims=list(data.get("claims", []) or []),
        caller_edge_candidates=list(data.get("caller_edge_candidates", []) or []),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        inv_id_str = str(data.get("investigation_id", ""))
        action_type = str(data.get("action", "QUERY_STATE"))
        parameters: dict[str, Any] = dict(data.get("parameters", {}))
        # Phase 4 (claim binding): workflow-only smallest-fetch identity
        # ({"kind", "gap_id", "claim_ref"}) carried beside params, never
        # inside them. Stamped onto banked rows at the bank boundary;
        # never forwarded to providers, never in ledger keys.
        _fetch_reason = ""
        try:
            _sfetch_ctx = data.get("smallest_fetch_context")
            if isinstance(_sfetch_ctx, dict):
                _sf_kind = str(_sfetch_ctx.get("kind", "") or "")
                _sf_gap = str(_sfetch_ctx.get("gap_id", "") or "")
                if _sf_kind and _sf_gap:
                    _fetch_reason = f"smallest_fetch:{_sf_kind}:{_sf_gap}"
        except Exception:
            _fetch_reason = ""
        # Reason-proposed hypothesis strings for FORMULATE_HYPOTHESIS.
        proposed_hypotheses: list[str] = [
            str(h) for h in (data.get("hypotheses", []) or []) if str(h).strip()
        ]
        proposed_caller_candidates = [
            item
            for item in (data.get("caller_edge_candidates", []) or [])[:20]
            if isinstance(item, dict)
        ]
        # Phase 2.1: typed claims keyed by statement, kind, and target identity.
        # A claim whose statement is absent from hypotheses_evaluated is
        # still persisted. Same statement with different kinds stays
        # Caller edges and history commits for one statement also remain
        # distinct so each keeps its own citation binding.
        proposed_claims: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for raw_claim in (data.get("claims", []) or [])[:20]:
            if not isinstance(raw_claim, dict):
                continue
            claim_text = str(raw_claim.get("statement", "") or "").strip()[:2048]
            if not claim_text:
                continue
            claim_kind_key = str(raw_claim.get("claim_kind") or "CAUSAL").strip().upper()
            claim_target = raw_claim.get("target")
            claim_target = claim_target if isinstance(claim_target, dict) else {}
            raw_edges = raw_claim.get("caller_edges")
            claim_variants = (
                [
                    {
                        **raw_claim,
                        "target": edge.get("target"),
                        "evidence_ids": edge.get("evidence_ids", []),
                        # Phase 2 (claim binding): per-edge retrieval hint
                        # wins; edge-omitted (or None) falls back to the
                        # top-level claim pair.
                        "search_query": edge.get("search_query") or raw_claim.get("search_query"),
                        "search_scope": edge.get("search_scope") or raw_claim.get("search_scope"),
                    }
                    for edge in raw_edges
                    if isinstance(raw_edges, list)
                    and isinstance(edge, dict)
                    and isinstance(edge.get("target"), dict)
                ]
                if claim_kind_key == "CALL_EDGE" and isinstance(raw_edges, list) and raw_edges
                else [raw_claim]
            )
            for claim_variant in claim_variants:
                variant_target = claim_variant.get("target")
                variant_target = variant_target if isinstance(variant_target, dict) else {}
                proposed_claims[
                    (
                        claim_text,
                        claim_kind_key,
                        str(variant_target.get("counterpart") or "").strip().casefold(),
                        str(variant_target.get("commit_id") or "").strip().casefold(),
                        str(variant_target.get("file_path") or "")
                        .replace("\\", "/")
                        .strip()
                        .casefold(),
                    )
                ] = claim_variant
                if claim_text not in proposed_hypotheses:
                    proposed_hypotheses.append(claim_text)
        for candidate in proposed_caller_candidates:
            caller = str(candidate.get("caller", "") or "").strip()
            callee = str(candidate.get("callee", "") or "").strip()
            if not caller or not callee:
                continue
            candidate_statement = f"{caller} calls {callee}"
            if candidate_statement not in proposed_hypotheses:
                proposed_hypotheses.append(candidate_statement)
            proposed_claims.setdefault(
                (
                    candidate_statement,
                    "CALL_EDGE",
                    caller.casefold(),
                    str(candidate.get("revision", "") or "").casefold(),
                    str(candidate.get("file_path", "") or "").replace("\\", "/").casefold(),
                ),
                {
                    "statement": candidate_statement,
                    "claim_kind": "CALL_EDGE",
                    "evidence_ids": [candidate.get("evidence_id")],
                    "target": {
                        "symbol": callee,
                        "counterpart": caller,
                        "file_path": candidate.get("file_path"),
                        "line_start": candidate.get("line_start"),
                        "line_end": candidate.get("line_end"),
                        "commit_id": candidate.get("revision") or None,
                    },
                },
            )
        if not tenant_id or not inv_id_str:
            return GenericActivityResult(
                success=False, error="tenant_id and investigation_id required"
            )
        investigation_id = UUID(inv_id_str)
        ctx = _get_ctx()

        # Mandatory authorization gate (F-004): no action reaches the evidence
        # gateway or execution surface without an explicit allow decision.
        from investigation_agent_platform.domain.investigation.models import (
            ActionType,
            InvestigationAction,
        )

        try:
            resolved_action_type = ActionType(action_type)
        except ValueError as exc:
            raise ApplicationFailure(
                f"Unknown action type: {action_type}",
                type="InvalidActionTypeError",
                non_retryable=True,
            ) from exc

        inv_repo: Any = getattr(ctx, "investigation_repo", None)
        investigation_obj: Any = None
        if inv_repo is not None:
            investigation_obj = await inv_repo.get_by_id(tenant_id, investigation_id)
        application_id = str(
            data.get("application_id")
            or (getattr(investigation_obj, "application_id", None) if investigation_obj else None)
            or ""
        )
        from investigation_agent_platform.application.investigation.verification import (
            VERIFIER_VERSION_CURRENT,
            VerificationEngine,
        )
        verifier_version = VERIFIER_VERSION_CURRENT

        inv_action = InvestigationAction.create(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            action_type=resolved_action_type,
            parameters=parameters,
        )

        # TO_DO-011: idempotency-wired — action_id is deterministic (UUIDv5
        # over the execution hash, F-057), so a Temporal retry of the same
        # logical action reserves the same key instead of double-recording.
        _idem_active_key: str | None = None
        _idem_operation = "activity:execute_action"
        _idem_key = f"execute:{inv_action.action_id}"
        _idem_request = {
            "action_id": str(inv_action.action_id),
            "tenant_id": tenant_id,
            "investigation_id": inv_id_str,
            "action_type": resolved_action_type.value,
            "parameters": parameters,
        }
        _replayed, _idem_active_key = await _reserve_or_replay(
            ctx=ctx,
            tenant_id=tenant_id,
            key=_idem_key,
            request_payload=_idem_request,
            operation=_idem_operation,
            activity="execute_action_activity",
            investigation_id=inv_id_str,
        )
        if _replayed is not None:
            _replayed_data = dict(_replayed.get("data") or {})
            # Phase 3.2: execute rows stored before action_outcome are
            # legacy, not absent — tag explicitly instead of inventing an
            # outcome. The workflow records an empty outcome (identical to
            # live pre-fix behavior) while operators can distinguish replay
            # age. Budget rows are current-shape (keyed on budget_exceeded).
            if _replayed_data.get("action_outcome") is None and not _replayed_data.get(
                "budget_exceeded"
            ):
                _replayed_data["idempotency_replay_legacy"] = True
                # Phase 4.2: a legacy row's outcome shape is unknown, so a
                # match cannot be proven — count it as a mismatch.
                _emit_telemetry(ctx, tenant_id, investigation_id, "record_replay_mismatch")
            return GenericActivityResult(
                success=bool(_replayed.get("success", True)),
                data=_replayed_data,
                error=_replayed.get("error"),
            )

        action_service = ctx.execute_action_service()
        principal_id = str(data.get("principal_id", "worker"))

        # F-054: durable budget ledger — usage is derived from durable tables
        # (action_executions count + evidence count), reserved/checked before
        # execution rather than advisory config. Deny before authorize so a
        # budget-exhausted investigation cannot consume further provider calls.
        from investigation_agent_platform.domain.common.exceptions import (
            InvestigationLimitExceededException,
        )
        from investigation_agent_platform.domain.investigation.models import InvestigationLimits

        limits = InvestigationLimits()
        # Per-app evidence budget (§7.6): profile evidence_budget overlays the
        # process-global defaults; unknown/invalid keys are ignored, never
        # trusted. Also resolves the profile once for the correlate clamp.
        _budget_profile: Any = None
        profile_repo_for_limits: Any = getattr(ctx, "profile_repo", None)
        if profile_repo_for_limits is not None and application_id:
            try:
                _budget_profile = await profile_repo_for_limits.get_by_application_id(
                    tenant_id, application_id
                )
            except Exception as exc:
                story_log(
                    logger,
                    logging.ERROR,
                    "profile budget lookup failed; using global limits",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="load_budget",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="profile budget lookup failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
                _budget_profile = None
        if _budget_profile is not None:
            limits = overlay_evidence_budget(limits, _budget_profile)
        limits = apply_effective_limits(limits, investigation_obj)
        app_correlation_depth = (
            _budget_profile.investigation.correlation_depth
            if _budget_profile is not None
            else limits.max_correlation_depth
        )
        try:
            action_repo_for_budget: Any = getattr(ctx, "action_repo", None)
            evidence_repo_for_budget: Any = getattr(ctx, "evidence_repo", None)
            tool_calls = 0
            evidence_count = 0
            if action_repo_for_budget is not None and hasattr(
                action_repo_for_budget, "count_by_investigation"
            ):
                tool_calls = await action_repo_for_budget.count_by_investigation(
                    tenant_id, investigation_id
                )
            if evidence_repo_for_budget is not None:
                items = await evidence_repo_for_budget.find_by_investigation_id(
                    tenant_id, investigation_id
                )
                evidence_count = len(items)
            if tool_calls >= limits.max_tool_calls:
                raise InvestigationLimitExceededException(
                    f"Maximum tool call limit reached: {limits.max_tool_calls}"
                )
            if evidence_count >= limits.max_evidence_items:
                raise InvestigationLimitExceededException(
                    f"Maximum evidence limit reached: {limits.max_evidence_items}"
                )
        except InvestigationLimitExceededException as exc:
            # Budget exhaustion is a ROUTING signal, not an execution failure:
            # the workflow breaks to CONCLUDING with the gathered corpus
            # instead of dying mid-flight (previously this raised out of the
            # activity, failed the whole execution, and stranded the aggregate
            # in a non-terminal state with no audit record).
            story_log(
                logger,
                logging.WARNING,
                "execute_action_activity budget exhausted; routing to conclude",
                comp=COMP_ACTVTY,
                func="execute_action_activity",
                stage="execute",
                step="check_budget",
                outcome=OUTCOME_SKIPPED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="execute_action_activity budget exhausted",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            if _idem_active_key is not None:
                await _complete_reservation(
                    ctx=ctx,
                    tenant_id=tenant_id,
                    key=_idem_active_key,
                    operation=_idem_operation,
                    response={
                        "success": True,
                        "data": {
                            "status": "BUDGET_EXCEEDED",
                            "budget_exceeded": True,
                            "items_count": 0,
                        },
                    },
                    activity="execute_action_activity",
                    investigation_id=inv_id_str,
                )
            return GenericActivityResult(
                success=True,
                data={
                    "status": "BUDGET_EXCEEDED",
                    "budget_exceeded": True,
                    "items_count": 0,
                },
            )
        except Exception as exc:
            story_log(
                logger,
                logging.ERROR,
                "budget ledger check failed; failing closed",
                comp=COMP_ACTVTY,
                func="execute_action_activity",
                stage="execute",
                step="check_budget",
                outcome=OUTCOME_FAILED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="budget ledger check failed",
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )
            raise _application_failure_from_exc(exc) from exc

        # Evidence room: persist at most the remaining budget per action.
        # Provider pages rank best-first, so truncation keeps the most
        # relevant hits while leaving room for later iterations — without
        # this, one broad query (limit 100) banks 40+ rows against a
        # 25-item budget and the very next iteration trips BUDGET_EXCEEDED.
        # Log rows bypass the room: persistence is uncapped for ALL log
        # types/providers (raw + exemplar twin); budgets still bound
        # tool calls, iterations, LLM tokens, and prompt projection.
        evidence_room = max(0, limits.max_evidence_items - evidence_count)

        def _evidence_type_name(item: Any) -> str:
            etype = getattr(item, "evidence_type", "")
            return str(getattr(etype, "value", etype) or "").upper()

        def _bank(items: list) -> list:
            logs, rest = _split_log_bypass(items)
            kept_rest = rest[:evidence_room]
            kept = logs + kept_rest
            # Preserve provider best-first order across the partition.
            order = {id(item): index for index, item in enumerate(items)}
            kept.sort(key=lambda item: order.get(id(item), len(items)))
            if len(kept) < len(items):
                story_log(
                    logger,
                    logging.INFO,
                    "Evidence persist truncated to remaining budget",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="bank",
                    outcome=OUTCOME_COMPLETED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="evidence persist truncated",
                    outputs={"kept": len(kept), "dropped": len(items) - len(kept)},
                    kept=len(kept),
                    dropped=len(items) - len(kept),
                )
            # WP-A3: budget truncation is a partial-result signal, not
            # just a log line — the ledger must not read truncated
            # banking as ordinary success. Fail-open: never break banking.
            try:
                outcome_detail["truncated_rows"] = len(items) - len(kept)
            except Exception:
                pass
            # Phase 4 (claim binding): stamp banked rows with the
            # smallest-fetch reason at the single shared bank helper, so
            # every _bank(...) path (and only forced turns, via the
            # workflow-passed context) carries it. Empty reason returns
            # items unchanged.
            if _fetch_reason:
                try:
                    kept = [_with_fetch_reason(item, _fetch_reason) for item in kept]
                except Exception:
                    pass
            return kept

        try:
            await action_service.authorize(
                tenant_id,
                investigation_id,
                inv_action,
                principal_id,
                application_id=application_id or None,
            )
        except Exception as exc:
            # P0-2 BLOCKED: the action stays denied (fail-closed), but the
            # denial degrades to an EMPTY/BLOCKED outcome instead of killing
            # the investigation — the miss_reason steers the reasoner to
            # other capabilities rather than ending the run.
            from investigation_agent_platform.domain.investigation.models import (
                ActionOutcome as _BlockedOutcome,
            )
            from investigation_agent_platform.domain.investigation.models import (
                ActionOutcomeStatus as _BlockedStatus,
            )

            story_log(
                logger,
                logging.WARNING,
                "execute_action_activity denied by authorization gate; degrading",
                comp=COMP_ACTVTY,
                func="execute_action_activity",
                stage="force",
                step="authorize",
                outcome=OUTCOME_SKIPPED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="execute_action_activity denied by authorization gate",
                error=str(exc),
                error_type=type(exc).__name__,
                action_type=action_type,
            )
            try:
                resolved = ActionType(action_type)
            except ValueError:
                resolved = ActionType.QUERY_STATE
            blocked = _BlockedOutcome(
                action_id=inv_action.action_id,
                action_type=resolved,
                status=_BlockedStatus.BLOCKED,
                miss_reason="authorization_denied",
                error_code=type(exc).__name__,
            )
            return GenericActivityResult(
                success=True,
                data={
                    "status": "BLOCKED",
                    "items_count": 0,
                    "action_outcome": blocked.model_dump(mode="json"),
                },
            )

        # Dispatch via EvidenceGateway using an exhaustive enum-to-handler
        # registry (F-023) — unknown/unsupported actions are a hard failure,
        # never a silent no-op "EXECUTED".
        gateway: Any = getattr(ctx, "evidence_gateway", None)
        evidence_repo: Any = getattr(ctx, "evidence_repo", None)
        hypothesis_repo: Any = getattr(ctx, "hypothesis_repo", None)
        profile_repo: Any = getattr(ctx, "profile_repo", None)
        environment = "production"
        if profile_repo is not None and application_id:
            profile = await profile_repo.get_by_application_id(tenant_id, application_id)
            if profile is not None:
                environment = profile.environment

        result_status = "EXECUTED"
        items_count = 0
        # P0-2: typed outcome side-channel. Handlers keep their int return
        # (items banked); this dict carries status/miss reason/evidence ids
        # into the ActionOutcome built at dispatch below.
        outcome_detail: dict[str, Any] = {
            "status": "SUCCEEDED",
            "miss_reason": None,
            "evidence_ids": [],
        }

        def _record_outcome(banked: list[Any]) -> int:
            outcome_detail["evidence_ids"] = [
                str(getattr(e, "evidence_id", "")) for e in banked[:25]
            ]
            if not banked and outcome_detail["status"] == "SUCCEEDED":
                outcome_detail["status"] = "EMPTY"
            return len(banked)

        async def _handle_query_state() -> int:
            from investigation_agent_platform.domain.common.exceptions import (
                CredentialProviderError,
                ExecutionError,
                ProviderNotConfiguredError,
                ProviderTimeoutException,
                ProviderUnavailableException,
                RateLimitException,
            )
            from investigation_agent_platform.domain.evidence.requests import (
                ApplicationStateRequest,
            )

            # Default page 10 (not the request-model default 50): same
            # reasoning as SEARCH_LOGS — one action must not bank the whole
            # 25-item budget. Callers may request up to 200; persistence
            # truncates to the remaining budget regardless.
            req = ApplicationStateRequest(
                environment=environment,
                template_id=str(parameters.get("template_id") or "default"),
                parameters=parameters,
                limit=min(int(parameters.get("limit", 10)), 200),
            )
            try:
                qres = await gateway.get_application_state(
                    tenant_id, investigation_id, application_id or "example-app", req
                )
            except (
                ProviderTimeoutException,
                TimeoutError,
                CredentialProviderError,
                ProviderUnavailableException,
                RateLimitException,
                ConnectionError,
                ProviderNotConfiguredError,
                ExecutionError,
            ) as exc:
                # Phase 3.1: typed failure outcomes (same contract as code
                # navigation) — timeouts, auth blocks, and outages surface
                # distinctly instead of all reading "provider absent".
                miss_reason, outcome_status, _blocking = classify_provider_failure(
                    exc, "state_provider_not_configured"
                )
                outcome_detail["status"] = outcome_status
                outcome_detail["miss_reason"] = miss_reason
                if isinstance(exc, RateLimitException):
                    outcome_detail["error_code"] = "RATE_LIMITED"
                story_log(
                    logger,
                    logging.WARNING,
                    "QUERY_STATE provider failure; degrading",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="query_state",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="query_state provider failure",
                    error=str(exc),
                    miss_reason=miss_reason,
                    retry_after=getattr(exc, "retry_after", None),
                )
                return 0
            # P3-2: per-source fan-out breakdown rides into the outcome.
            outcome_detail["sources"] = list(
                (getattr(qres, "provider_metadata", None) or {}).get("per_source", []) or []
            )
            banked = _bank(qres.items)
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            if not banked and evidence_repo is not None:
                # Phase 2 item 3: an empty state query banks a scoped
                # no-match proof (same contract as code navigation above).
                try:
                    from investigation_agent_platform.domain.evidence.models import (
                        EvidenceType as _EvidenceType,
                    )

                    _params = getattr(req, "parameters", None) or {}
                    _scope_items = ",".join(
                        f"{k}={v}"
                        for k, v in sorted((str(k), str(v)) for k, v in dict(_params).items())
                    )
                    _state_scope = (
                        f"{getattr(req, 'environment', '')}:"
                        f"{getattr(req, 'template_id', '')}:{_scope_items}"
                    )[:512]
                    await evidence_repo.save(
                        tenant_id,
                        _with_fetch_reason(
                            _absence_proof_row(
                                tenant_id=tenant_id,
                                investigation_id=investigation_id,
                                evidence_type=_EvidenceType.DATABASE_STATE,
                                provider="state-provider",
                                operation="query_state",
                                scope=_state_scope,
                            ),
                            _fetch_reason,
                        ),
                        investigation_id,
                    )
                except Exception:
                    pass
            return _record_outcome(banked)

        async def _handle_search_logs() -> int:
            from investigation_agent_platform.domain.evidence.requests import (
                RuntimeEvidenceRequest,
            )

            # Part 9: clamp caller-controlled dimensions to the domain bounds
            # (mirrors the provider-side clamp that applied before: min()
            # semantics preserved; per-item lengths still reject as invalid).
            # Default page 10 (not 100): the investigation budget is 25 items
            # over ~10 iterations, so one broad action must not bank the whole
            # corpus — best-first ranking makes the top-10 the most relevant.
            # Callers may still request up to 200; persistence truncates to
            # the remaining budget regardless (see _bank above).
            req = RuntimeEvidenceRequest(
                environment=environment,
                keywords=_shape_search_keywords(parameters.get("keywords", [])) or [],
                services=list(parameters.get("services", []))[:10] or [],
                limit=min(int(parameters.get("limit", 10)), 200),
            )
            qres = None
            try:
                qres = await gateway.search_runtime_evidence(
                    tenant_id, investigation_id, application_id or "example-app", req
                )
            except Exception as exc:
                from investigation_agent_platform.domain.common.exceptions import (
                    ExecutionError as _ExecError,
                )
                from investigation_agent_platform.domain.common.exceptions import (
                    ProviderNotConfiguredError as _ProviderMissing,
                )

                if not isinstance(exc, (_ProviderMissing, _ExecError)):
                    raise
                story_log(
                    logger,
                    logging.WARNING,
                    "SEARCH_LOGS provider absent; degrades to empty",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="search_logs",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="search_logs provider absent",
                    error=str(exc),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "runtime_provider_not_configured"
                return 0
            banked = _bank(qres.items)
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            # Log-group persistence: group banked log rows and persist one
            # exemplar row per group (same member IDs verbatim, separate
            # table). Fail-open: exemplar failures never break raw banking.
            try:
                await _persist_log_exemplars(ctx, tenant_id, investigation_id, banked)
            except Exception as exc:
                story_log(
                    logger,
                    logging.WARNING,
                    "Log exemplar persist skipped; raw banking stands",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="search_logs",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="log exemplars skipped",
                    error=str(exc),
                )
            return _record_outcome(banked)

        async def _handle_get_code() -> int:
            from investigation_agent_platform.domain.common.exceptions import (
                CodePathNotFoundError,
                CredentialProviderError,
                ExecutionError,
                ProviderNotConfiguredError,
                ProviderTimeoutException,
                ProviderUnavailableException,
                RateLimitException,
            )
            from investigation_agent_platform.domain.evidence.requests import SourceRequest

            def _opt_line(value: Any) -> int | None:
                try:
                    number = int(str(value))
                except (TypeError, ValueError):
                    return None
                return number if number >= 1 else None

            req = SourceRequest(
                repository=application_id or "example-app",
                file_path=str(parameters.get("file_path", "")),
                # P1-2: revision/range are honored end to end ("HEAD" =
                # adapter resolves the profile default branch, previous
                # behavior — never silently substitutes another ref).
                revision=str(parameters.get("revision") or "").strip() or "HEAD",
                start_line=_opt_line(parameters.get("start_line")),
                end_line=_opt_line(parameters.get("end_line")),
            )
            if not req.file_path.strip():
                # Missing file path: agent omission, not a provider outage.
                # Degrade to empty (trace shows the attempt) instead of
                # failing the investigation — the escalation history steers
                # the reasoner away from repeating it. Unsafe paths still
                # fail closed in the validator/adapter below.
                story_log(
                    logger,
                    logging.WARNING,
                    "GET_CODE without file_path degrades to empty",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="get_code",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="get_code missing file_path",
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "missing_file_path"
                return 0
            try:
                evidence = await gateway.get_source(
                    tenant_id, investigation_id, application_id or "example-app", req
                )
            except (
                ProviderTimeoutException,
                TimeoutError,
                CredentialProviderError,
                ProviderUnavailableException,
                RateLimitException,
                ConnectionError,
                ProviderNotConfiguredError,
                ExecutionError,
            ) as exc:
                # Phase 3.1: typed failure outcomes — a timed-out or
                # unauthorized git read is infrastructure, not absence.
                # CodePathNotFoundError below still owns genuine path misses.
                miss_reason, outcome_status, _blocking = classify_provider_failure(
                    exc, "code_provider_not_configured"
                )
                outcome_detail["status"] = outcome_status
                outcome_detail["miss_reason"] = miss_reason
                if isinstance(exc, RateLimitException):
                    outcome_detail["error_code"] = "RATE_LIMITED"
                story_log(
                    logger,
                    logging.WARNING,
                    "GET_CODE provider failure; degrading",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="get_code",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="get_code provider failure",
                    error=str(exc),
                    miss_reason=miss_reason,
                    retry_after=getattr(exc, "retry_after", None),
                )
                return 0
            except CodePathNotFoundError as exc:
                # Path miss (agent-guessed path not in the tree): exploration
                # miss, not infrastructure failure. Degrade the same way;
                # genuine repo outages still fail loud below.
                story_log(
                    logger,
                    logging.WARNING,
                    "Code path not found; GET_CODE degrades to empty",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="get_code",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="get_code path miss",
                    error=str(exc),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "code_path_not_found"
                return 0
            # Phase 0.4: label the read mode. A path-only read returns the
            # bounded file head, which is discovery — it cannot support a
            # definition claim ("X is implemented in Y"). Only an explicit
            # line range is a decisive-window read.
            read_mode = (
                "range" if (req.start_line is not None or req.end_line is not None) else "path_only"
            )
            merged_attributes = dict(getattr(evidence, "attributes", None) or {})
            merged_attributes["read_mode"] = read_mode
            merged_attributes["discovery_only"] = True
            # Phase 4 (claim binding): same fetch_reason stamp as the
            # shared _bank helper (GET_CODE banks via singleton save).
            if _fetch_reason:
                merged_attributes["fetch_reason"] = _fetch_reason
            fact_field = str(parameters.get("field_name", "") or "").strip()
            fact_method = str(parameters.get("method_name", "") or "").strip()
            _evidence_path = str(
                getattr(
                    getattr(getattr(evidence, "provenance", None), "source_location", None),
                    "file_path",
                    "",
                )
                or ""
            )
            _is_python = _evidence_path.lower().endswith(".py")

            _version_repo = application_id or "example-app"

            async def _read_version_file(_name: str) -> str | None:
                # Trusted repo-declared version: read version files at the
                # analyzed SHA via the gateway (same authorization, never
                # banked). The dispatch module skips unpinned revisions.
                try:
                    _pinned = str(
                        getattr(
                            getattr(getattr(evidence, "provenance", None), "source_location", None),
                            "revision",
                            "",
                        )
                        or ""
                    )
                    _vreq = SourceRequest(
                        repository=_version_repo,
                        file_path=_name,
                        revision=_pinned,
                    )
                    _vev = await gateway.get_source(
                        tenant_id, investigation_id, _version_repo, _vreq
                    )
                except Exception:
                    return None
                _vattrs = getattr(_vev, "attributes", None) or {}
                if _vattrs.get("truncated"):
                    return None
                return str(getattr(_vev, "content_snippet", "") or "") or None

            if fact_field and _is_python:
                # Python value resolution (scope/class/framework): V2 only,
                # never the Java field-shaped legacy key.
                try:
                    from investigation_agent_platform.application.analysis.python.dispatch import (
                        resolve_python_field_fact as _dispatch_python_field,
                    )

                    _py_resolution, _config_source = await _dispatch_python_field(
                        evidence,
                        field_name=fact_field,
                        declaring_type=(
                            str(parameters.get("declaring_type") or "").strip() or None
                        ),
                        class_scope=(
                            str(parameters.get("class_scope") or "").strip() or None
                        ),
                        function_scope=(
                            str(parameters.get("function_scope") or "").strip() or None
                        ),
                        read_version_file=_read_version_file,
                    )
                    if _py_resolution is not None:
                        merged_attributes["code_fact_resolution_v2"] = (
                            _py_resolution.model_dump(mode="json")
                        )
                        if _config_source is not None:
                            # Names the configuration source for the
                            # verifier's CONFIGURATION_SOURCE family
                            # (declaration file, never a value).
                            merged_attributes["configuration_source"] = _config_source
                        outcome_detail["code_fact_resolution_v2"] = (
                            _py_resolution.model_dump(mode="json")
                        )
                except Exception:
                    pass
            elif fact_field:
                from investigation_agent_platform.application.analysis.java_field_facts import (
                    resolve_java_field_fact,
                )

                resolution = resolve_java_field_fact(
                    evidence,
                    fact_field,
                    declaring_type=(str(parameters.get("declaring_type") or "").strip() or None),
                )
                merged_attributes["code_fact_resolution"] = resolution.model_dump(mode="json")
                outcome_detail["code_fact_resolution"] = resolution.model_dump(mode="json")
                try:
                    from investigation_agent_platform.domain.code.models import (
                        emit_dual_resolution_attributes,
                    )

                    dual = emit_dual_resolution_attributes(resolution)
                    merged_attributes["code_fact_resolution_v2"] = dual[
                        "code_fact_resolution_v2"
                    ]
                    outcome_detail["code_fact_resolution_v2"] = dual[
                        "code_fact_resolution_v2"
                    ]
                except Exception:
                    pass
            if fact_method and _is_python:
                # Python method questions: with a class scope this answers
                # "which implementation runs" via the static MRO; without a
                # scope it inventories the function's possible paths.
                try:
                    from investigation_agent_platform.application.analysis.python.dispatch import (
                        resolve_python_method_fact as _dispatch_python_method,
                    )

                    _method_resolution = await _dispatch_python_method(
                        evidence,
                        method_name=fact_method,
                        declaring_type=(
                            str(parameters.get("declaring_type") or "").strip() or None
                        ),
                        class_scope=(
                            str(parameters.get("class_scope") or "").strip() or None
                        ),
                        guard_condition=(
                            str(parameters.get("guard_condition") or "").strip() or None
                        ),
                        read_version_file=_read_version_file,
                    )
                    merged_attributes["code_fact_resolution_v2"] = (
                        _method_resolution.model_dump(mode="json")
                    )
                    outcome_detail["code_fact_resolution_v2"] = (
                        _method_resolution.model_dump(mode="json")
                    )
                except Exception:
                    pass
            elif fact_method:
                # Method-return-on-guard resolution (L5 class): the subject
                # is a method's guarded `return` literal, which the field
                # path can never resolve. Emits V2 SOURCE_VALUE_FACT only
                # (legacy CodeFactResolution is field-shaped by construction).
                try:
                    from investigation_agent_platform.application.analysis.java.method_return_facts import (
                        resolve_java_method_return_fact,
                    )

                    _method_resolution = resolve_java_method_return_fact(
                        evidence,
                        fact_method,
                        guard_condition=(
                            str(parameters.get("guard_condition") or "").strip() or None
                        ),
                        declaring_type=(
                            str(parameters.get("declaring_type") or "").strip() or None
                        ),
                    )
                    merged_attributes["code_fact_resolution_v2"] = (
                        _method_resolution.model_dump(mode="json")
                    )
                    outcome_detail["code_fact_resolution_v2"] = (
                        _method_resolution.model_dump(mode="json")
                    )
                except Exception:
                    pass
            evidence = evidence.model_copy(update={"attributes": merged_attributes})
            if evidence_repo is not None:
                await evidence_repo.save(tenant_id, evidence, investigation_id)
            outcome_detail["evidence_ids"] = [str(evidence.evidence_id)]
            # Phase 4.2: revision pin check. An unpinned read (HEAD/empty)
            # asserts nothing, so it vacuously matches; a pinned read must
            # carry the requested SHA in provenance.
            try:
                _requested = str(getattr(req, "revision", "") or "")
                _actual_location = getattr(
                    getattr(evidence, "provenance", None), "source_location", None
                )
                _actual = str(getattr(_actual_location, "revision", "") or "")
                _pinned = _requested not in ("", "HEAD", "UNPINNED")
                _emit_telemetry(
                    ctx,
                    tenant_id,
                    investigation_id,
                    "record_revision_check",
                    "GET_CODE",
                    (not _pinned) or (_actual == _requested),
                )
            except Exception:
                pass
            return 1

        async def _handle_walk_session_graph() -> int:
            """G-3: session-anchored entity-graph walk.

            Takes {session_id, depth}: anchor template first, then one hop
            template per relationship edge, binding parent-key values from
            parsed row payloads (best-effort JSON; unparseable rows stop
            expansion but stay banked). Depth ≤3, values ≤20 per step,
            budget-capped via _bank. All templates are profile-declared
            (operator or auto-derived) — no dynamic SQL.
            """
            import json as _json

            from investigation_agent_platform.domain.evidence.requests import (
                ApplicationStateRequest,
            )

            session_id = str(parameters.get("session_id", "") or "").strip()[:256]
            try:
                depth = max(1, min(int(parameters.get("depth", 2)), 3))
            except (TypeError, ValueError):
                depth = 2
            if not session_id:
                story_log(
                    logger,
                    logging.WARNING,
                    "WALK_SESSION_GRAPH without session_id degrades to empty",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="walk_session_graph",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="walk session graph missing session",
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "missing_session_id"
                return 0

            def _parse_rows(item: Any) -> list[dict[str, Any]]:
                try:
                    attributes = getattr(item, "attributes", None) or {}
                    raw = attributes.get("raw_payload", None)
                    if raw is None:
                        raw = getattr(item, "content_snippet", "") or ""
                    if isinstance(raw, dict):
                        return [raw]
                    if isinstance(raw, str):
                        parsed = _json.loads(raw)
                        if isinstance(parsed, dict):
                            return [parsed]
                        if isinstance(parsed, list):
                            return [r for r in parsed if isinstance(r, dict)]
                    return []
                except Exception:
                    return []

            async def _run_template(template_id: str, binds: dict[str, Any]) -> list[Any]:
                try:
                    qres = await gateway.get_application_state(
                        tenant_id,
                        investigation_id,
                        application_id or "example-app",
                        ApplicationStateRequest(
                            environment=environment,
                            template_id=template_id,
                            parameters=dict(binds),
                            limit=50,
                        ),
                    )
                    return list(qres.items or [])
                except Exception as exc:
                    from investigation_agent_platform.domain.common.exceptions import (
                        ExecutionError as _ExecError,
                    )
                    from investigation_agent_platform.domain.common.exceptions import (
                        ProviderNotConfiguredError as _ProviderMissing,
                    )

                    if not isinstance(exc, (_ProviderMissing, _ExecError)):
                        raise
                    story_log(
                        logger,
                        logging.WARNING,
                        "WALK_SESSION_GRAPH leg degraded",
                        comp=COMP_ACTVTY,
                        func="execute_action_activity",
                        stage="execute",
                        step="walk_session_graph",
                        outcome=OUTCOME_SKIPPED,
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="walk session graph leg degraded",
                        error=str(exc),
                    )
                    return []

            collected: list[Any] = []
            anchor_rows = await _run_template("auto_session_anchor", {"session_id": session_id})
            if not anchor_rows:
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "session_anchor_empty"
                return _record_outcome([])
            collected.extend(anchor_rows)
            frontier: list[tuple[str, Any]] = []
            try:
                if profile_repo is not None and application_id:
                    profile = await profile_repo.get_by_application_id(tenant_id, application_id)
                    index = profile.query_template_index() if profile else {}
                else:
                    index = {}
            except Exception:
                index = {}
            hop = 0
            # Seed frontier from anchor rows: every scalar value is a
            # candidate parent key (bounded below).
            try:
                for item in anchor_rows:
                    for row in _parse_rows(item):
                        for value in row.values():
                            if isinstance(value, (str, int)) and str(value).strip():
                                frontier.append((str(value).strip()[:256], hop))
                                if len(frontier) >= 20:
                                    break
                        if len(frontier) >= 20:
                            break
            except Exception:
                pass
            template_names = []
            try:
                keys = list(index.keys() if hasattr(index, "keys") else [])
            except Exception:
                keys = []
            for key in keys:
                try:
                    short = str(key).split(".")[-1]
                    if short.startswith("auto_session_hop_") and key not in template_names:
                        template_names.append(str(key))
                except Exception:
                    continue
            while hop < depth and frontier and template_names:
                hop += 1
                next_frontier: list[tuple[str, Any]] = []
                for parent_value, _ in frontier[:20]:
                    for template_name in template_names[:5]:
                        rows = await _run_template(str(template_name), {"parent_key": parent_value})
                        collected.extend(rows)
                        try:
                            for item in rows:
                                for row in _parse_rows(item):
                                    for value in row.values():
                                        if isinstance(value, (str, int)) and str(value).strip():
                                            next_frontier.append((str(value).strip()[:256], hop))
                                            if len(next_frontier) >= 20:
                                                break
                                    if len(next_frontier) >= 20:
                                        break
                        except Exception:
                            continue
                        if len(next_frontier) >= 20:
                            break
                    if len(next_frontier) >= 20:
                        break
                frontier = next_frontier
            banked = _bank(collected)
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            return _record_outcome(banked)

        async def _handle_search_ref_docs() -> int:
            """T1-1: reference-docs search banking DOCUMENTATION rows.
            Takes {query}: refdoc service top hits become citable evidence
            (document_path + source_revision provenance) so doc answers
            verify through the doc-validated path instead of capping at
            PARTIAL. Fail-open throughout.
            """
            from investigation_agent_platform.domain.common.exceptions import (
                ExecutionError,
            )
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

            query = str(parameters.get("query", "") or "").strip()[:1000]
            if not query:
                story_log(
                    logger,
                    logging.WARNING,
                    "SEARCH_REF_DOCS without query degrades to empty",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="search_ref_docs",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="search ref docs missing query",
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "missing_query"
                return 0
            try:
                from investigation_agent_platform.application.reference.reference_service import (
                    ReferenceDocumentService,
                )

                chunk_repo = getattr(ctx, "reference_repo", None)
                if chunk_repo is None:
                    raise ExecutionError("reference repository unavailable")
                service = ReferenceDocumentService(
                    chunk_repo=chunk_repo,
                    profile_repo=getattr(ctx, "profile_repo", None),
                    embedder=None,
                )
                hits, _cursor = await service.search(
                    tenant_id,
                    query,
                    application_id=str(application_id or "") or None,
                    limit=10,
                )
            except Exception as exc:
                from investigation_agent_platform.domain.common.exceptions import (
                    ExecutionError as _ExecError,
                )
                from investigation_agent_platform.domain.common.exceptions import (
                    ProviderNotConfiguredError as _ProviderMissing,
                )

                if isinstance(exc, (_ProviderMissing, _ExecError)):
                    # Provider-absent (including no reference repository):
                    # degrade to empty, never fail the run.
                    story_log(
                        logger,
                        logging.WARNING,
                        "SEARCH_REF_DOCS provider absent; degrades to empty",
                        comp=COMP_ACTVTY,
                        func="execute_action_activity",
                        stage="execute",
                        step="search_ref_docs",
                        outcome=OUTCOME_SKIPPED,
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="search ref docs provider absent",
                        error=str(exc),
                    )
                    outcome_detail["status"] = "DEGRADED"
                    outcome_detail["miss_reason"] = "refdoc_provider_not_configured"
                    return 0
                raise
            now = datetime.now(UTC)
            banked_rows: list[Any] = []
            for hit in hits or []:
                try:
                    document_path = str(getattr(hit, "document_path", "") or "")
                    revision = str(getattr(hit, "source_revision", "") or "")
                    content = str(getattr(hit, "content", "") or "")
                    if not document_path:
                        continue
                    banked_rows.append(
                        Evidence(
                            tenant_id=tenant_id,
                            investigation_id=investigation_id,
                            evidence_type=EvidenceType.DOCUMENTATION,
                            provider="reference-docs",
                            source=f"refdoc://{document_path}",
                            title=f"Reference: {document_path}",
                            summary=(
                                f"Reference document {document_path} "
                                f"(revision {revision or 'unknown'}): "
                                f"{content[:800]}"
                            ),
                            content_snippet=content[:4000],
                            observed_at=now,
                            retrieved_at=now,
                            provenance=EvidenceProvenance(
                                tenant_id=tenant_id,
                                investigation_id=investigation_id,
                                provider_type="reference-docs",
                                requested_provider_id="reference-docs",
                                actual_provider_id="reference-docs",
                                source_system="reference-docs",
                                retrieval_timestamp=now,
                                query_fingerprint=QueryFingerprint(
                                    provider_type="reference-docs",
                                    operation="search_reference_docs",
                                    normalized_query_hash=query[:64],
                                ),
                                source_location=SourceLocation(
                                    system="reference-docs",
                                    identifier=document_path[:256],
                                    file_path=document_path,
                                    revision=revision or "unknown",
                                ),
                            ),
                            freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
                            fingerprint=f"refdoc-{tenant_id}-{investigation_id}-{document_path}"[
                                :128
                            ],
                        )
                    )
                except Exception:
                    continue
            banked = _bank(banked_rows)
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            if not banked:
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "refdoc_no_hits"
            return _record_outcome(banked)

        async def _handle_trace_journey() -> int:
            """G-1: session → tracking-ID spine from log rows.

            Takes {session_id} or {tracking_id}: searches logs for the
            identifier across services, extracts per-row tracking/service/
            timestamp via the profile's declared identity keys, and banks
            one JOURNEY spine row (ordered steps). Fail-open throughout.
            """
            from investigation_agent_platform.application.evidence.journey import (
                build_journey_spine,
            )
            from investigation_agent_platform.domain.evidence.models import (
                Evidence,
                EvidenceType,
            )
            from investigation_agent_platform.domain.evidence.requests import (
                RuntimeEvidenceRequest,
            )
            from investigation_agent_platform.domain.provenance.models import (
                EvidenceFreshness,
                EvidenceProvenance,
                QueryFingerprint,
                SourceLocation,
            )

            session_id = str(parameters.get("session_id", "") or "").strip()[:256]
            tracking_id = str(parameters.get("tracking_id", "") or "").strip()[:256]
            journey_id = session_id or tracking_id
            if not journey_id:
                story_log(
                    logger,
                    logging.WARNING,
                    "TRACE_JOURNEY without session_id/tracking_id degrades to empty",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="trace_journey",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="trace_journey missing identifier",
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "missing_journey_id"
                return 0

            def _nested_get(row: Any, dotted: str) -> Any:
                try:
                    current: Any = row
                    for part in str(dotted or "").split("."):
                        if isinstance(current, dict):
                            current = current.get(part, None)
                        else:
                            return None
                        if current is None:
                            return None
                    return current
                except Exception:
                    return None

            service_keys: list[tuple[str, str, str]] = []
            try:
                if profile_repo is not None and application_id:
                    profile = await profile_repo.get_by_application_id(tenant_id, application_id)
                    for svc in getattr(profile, "services", []) or []:
                        telemetry = getattr(svc, "telemetry", None)
                        service_keys.append(
                            (
                                str(getattr(svc, "name", "") or ""),
                                str(getattr(telemetry, "session_key", "") or ""),
                                str(getattr(telemetry, "tracking_key", "") or ""),
                            )
                        )
            except Exception:
                service_keys = []
            try:
                qres = await gateway.search_runtime_evidence(
                    tenant_id,
                    investigation_id,
                    application_id or "example-app",
                    RuntimeEvidenceRequest(
                        environment=environment,
                        keywords=[journey_id],
                        services=[],
                        limit=100,
                    ),
                )
            except Exception as exc:
                from investigation_agent_platform.domain.common.exceptions import (
                    ExecutionError as _ExecError,
                )
                from investigation_agent_platform.domain.common.exceptions import (
                    ProviderNotConfiguredError as _ProviderMissing,
                )

                if not isinstance(exc, (_ProviderMissing, _ExecError)):
                    raise
                story_log(
                    logger,
                    logging.WARNING,
                    "TRACE_JOURNEY provider absent; degrades to empty",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="trace_journey",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="trace_journey provider absent",
                    error=str(exc),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "runtime_provider_not_configured"
                return 0
            flat_rows: list[dict[str, Any]] = []
            for item in qres.items or []:
                try:
                    attributes = getattr(item, "attributes", None) or {}
                    raw = attributes.get("raw_payload", None)
                    if isinstance(raw, dict):
                        record = raw
                    else:
                        import json as _json

                        try:
                            record = _json.loads(
                                str(
                                    attributes.get("raw_payload", None)
                                    or getattr(item, "content_snippet", "")
                                    or ""
                                )
                            )
                            if not isinstance(record, dict):
                                record = {}
                        except Exception:
                            record = {}
                    tracking_value = ""
                    service_value = ""
                    for svc_name, session_key, tracking_key in service_keys:
                        for candidate in (tracking_key, session_key):
                            if candidate:
                                found = _nested_get(record, candidate)
                                if found:
                                    if candidate == tracking_key and not tracking_value:
                                        tracking_value = str(found)
                    if not service_value:
                        for service_candidate in (
                            "service.name",
                            "service_name",
                            "service",
                            "labels.service",
                            "serviceName",
                        ):
                            found = _nested_get(record, service_candidate)
                            if found:
                                service_value = str(found)
                                break
                    if not tracking_value:
                        for fallback in (
                            "trackingId",
                            "tracking_id",
                            "transactionId",
                            "transaction_id",
                            "trace.id",
                            "traceId",
                        ):
                            found = _nested_get(record, fallback)
                            if found:
                                tracking_value = str(found)
                                break
                    timestamp_value = str(
                        _nested_get(record, "@timestamp")
                        or _nested_get(record, "timestamp")
                        or getattr(item, "observed_at", "")
                        or ""
                    )
                    level_value = str(
                        _nested_get(record, "log.level")
                        or _nested_get(record, "level")
                        or _nested_get(record, "status")
                        or ""
                    )
                    flat_rows.append(
                        {
                            "tracking_id": tracking_value,
                            "service": service_value,
                            "timestamp": timestamp_value,
                            "status": level_value,
                        }
                    )
                except Exception:
                    continue
            spine = build_journey_spine(flat_rows, session_id=session_id or tracking_id)
            steps = spine.get("steps", []) or []
            if not steps:
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "journey_no_steps"
                return _record_outcome([])
            now = datetime.now(UTC)
            lines = [
                f"step {s.get('tracking_id') or '(unattributed)'}: "
                f"{', '.join(s.get('services', [])[:5])} "
                f"[{s.get('row_count', 0)} rows"
                f"{', ' + str(s.get('error_count', 0)) + ' errors' if s.get('error_count') else ''}]"
                for s in steps[:20]
            ]
            spine_row = Evidence(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                evidence_type=EvidenceType.JOURNEY,
                provider="journey-engine",
                source=f"journey://{journey_id}",
                title=f"Journey spine for {journey_id} ({len(steps)} steps)",
                summary=(
                    f"Session journey {journey_id}: {len(steps)} tracking step(s). "
                    + "; ".join(lines)[:1500]
                ),
                content_snippet="; ".join(lines)[:4000],
                observed_at=now,
                retrieved_at=now,
                provenance=EvidenceProvenance(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    provider_type="journey-engine",
                    requested_provider_id="journey-engine",
                    actual_provider_id="journey-engine",
                    source_system="log-journey",
                    retrieval_timestamp=now,
                    query_fingerprint=QueryFingerprint(
                        provider_type="journey-engine",
                        operation="trace_journey",
                        normalized_query_hash=journey_id[:64],
                    ),
                    source_location=SourceLocation(
                        system="journey-engine",
                        identifier=journey_id[:256],
                    ),
                ),
                freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
                fingerprint=f"journey-{tenant_id}-{investigation_id}-{journey_id}"[:128],
            )
            banked = _bank([spine_row])
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            return _record_outcome(banked)

        async def _handle_correlate() -> int:
            # G7: LLM-supplied IDs are untrusted exploration input, not a
            # contract — unparseable entries degrade (EMPTY + miss_reason),
            # never WORKFLOW_ERROR. A malformed parameter must steer the
            # reasoner, not kill the run.
            root_ids, dropped = _coerce_root_ids(parameters)
            try:
                max_depth = min(int(parameters.get("max_depth", 2)), app_correlation_depth)
            except (TypeError, ValueError):
                max_depth = min(2, app_correlation_depth)
            if not root_ids:
                story_log(
                    logger,
                    logging.WARNING,
                    "CORRELATE with no parseable root IDs degrades to empty",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="correlate",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="correlate roots unparseable",
                    dropped=dropped,
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "unparseable_root_ids"
                return 0
            qres = await gateway.correlate(
                tenant_id, investigation_id, application_id or "example-app", root_ids, max_depth
            )
            banked = _bank(qres.items)
            # P1-2: the gateway returns the graph in provider_metadata with
            # items=[] — bank one CALL_GRAPH summary so the traversal result
            # reaches reasoning instead of vanishing (items_count=0 before).
            graph = (qres.provider_metadata or {}).get("graph") or {}
            nodes = graph.get("nodes", []) or []
            edges = graph.get("edges", []) or []
            if nodes or edges:
                from investigation_agent_platform.domain.evidence.models import (
                    Evidence,
                    EvidenceType,
                )

                edge_lines = [
                    f"{e.get('source_id')} -[{e.get('relationship_type')}]-> {e.get('target_id')}"
                    for e in edges[:40]
                    if isinstance(e, dict)
                ]
                import hashlib as _hashlib

                from investigation_agent_platform.domain.provenance.models import (
                    EvidenceFreshness,
                    EvidenceProvenance,
                    QueryFingerprint,
                    SourceLocation,
                )

                now = datetime.now(UTC)
                fp = _hashlib.sha256(
                    f"correlate:{tenant_id}:{sorted(str(r) for r in root_ids)}:{max_depth}".encode()
                ).hexdigest()
                banked = list(banked) + [
                    Evidence(
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                        evidence_type=EvidenceType.CALL_GRAPH,
                        provider="correlation-engine",
                        source=f"correlation://{investigation_id}",
                        title=f"Correlation: {len(nodes)} nodes, {len(edges)} edges",
                        summary=(
                            f"Correlation from {len(root_ids)} root(s) at depth "
                            f"{max_depth}: {len(nodes)} nodes, {len(edges)} edges."
                        ),
                        content_snippet="\n".join(edge_lines)[:4000],
                        observed_at=now,
                        retrieved_at=now,
                        provenance=EvidenceProvenance(
                            tenant_id=tenant_id,
                            investigation_id=investigation_id,
                            provider_type="correlation",
                            requested_provider_id="correlation-engine",
                            actual_provider_id="correlation-engine",
                            source_system="evidence-relationships",
                            retrieval_timestamp=now,
                            query_fingerprint=QueryFingerprint(
                                provider_type="correlation",
                                operation="correlate",
                                normalized_query_hash=fp[:32],
                            ),
                            source_location=SourceLocation(
                                system="evidence-relationships",
                                identifier=str(investigation_id),
                            ),
                        ),
                        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
                        fingerprint=f"correlate-{fp[:32]}",
                    )
                ]
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            return _record_outcome(banked)

        async def _handle_code_navigation(
            action_label: str,
            miss_label: str,
            build_request: Any,
            gateway_method: str,
            summarize: Any = None,
        ) -> int:
            """Shared dispatch for SEARCH_CODE / FIND_SYMBOL /
            FIND_CALL_GRAPH / GET_CODE_HISTORY (P1-1).

            All four resolve through existing gateway methods with full
            sanitization/dedup; provider-absent degrades to EMPTY with a
            miss reason instead of failing the investigation. `summarize`
            optionally converts provider_metadata into bankable evidence
            (used where the gateway returns metadata-only results).
            """
            from investigation_agent_platform.domain.common.exceptions import (
                CredentialProviderError,
                ExecutionError,
                ProviderNotConfiguredError,
                ProviderTimeoutException,
                ProviderUnavailableException,
                RateLimitException,
                UnsupportedCapabilityError,
            )

            try:
                req = build_request()
            except Exception as exc:
                story_log(
                    logger,
                    logging.WARNING,
                    "%s invalid parameters degrades to empty",
                    action_label,
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step=str(action_label).lower(),
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event=f"{action_label} invalid parameters",
                    error=str(exc),
                    action_label=action_label,
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "invalid_parameters"
                return 0
            try:
                method = getattr(gateway, gateway_method)
                qres = await method(
                    tenant_id, investigation_id, application_id or "example-app", req
                )
            except UnsupportedCapabilityError as exc:
                # The provider cannot honor a required revision. This is
                # unproven, not absent — surface it distinctly so the loop
                # never reads "capability gap" as "no such symbol".
                story_log(
                    logger,
                    logging.WARNING,
                    "%s capability unsupported; degrading",
                    action_label,
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step=str(action_label).lower(),
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event=f"{action_label} unsupported capability",
                    error=str(exc),
                    action_label=action_label,
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "unsupported_capability"
                outcome_detail["capability"] = getattr(exc, "capability", None)
                return 0
            except (
                ProviderTimeoutException,
                TimeoutError,
                CredentialProviderError,
                ProviderUnavailableException,
                RateLimitException,
                ConnectionError,
                ProviderNotConfiguredError,
                ExecutionError,
            ) as exc:
                # Phase 3.1: typed failure outcomes. Timeouts (after gateway
                # retries exhaust), auth blocks, and unavailability surface
                # distinctly — a timeout is transient, an expired credential
                # blocks the run, and none of them is "provider absent".
                miss_reason, outcome_status, _blocking = classify_provider_failure(exc, miss_label)
                outcome_detail["status"] = outcome_status
                outcome_detail["miss_reason"] = miss_reason
                if isinstance(exc, RateLimitException):
                    outcome_detail["error_code"] = "RATE_LIMITED"
                story_log(
                    logger,
                    logging.WARNING,
                    "%s degrades to empty",
                    action_label,
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step=str(action_label).lower(),
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event=f"{action_label} provider failure",
                    error=str(exc),
                    miss_reason=miss_reason,
                    retry_after=getattr(exc, "retry_after", None),
                    action_label=action_label,
                )
                return 0
            banked = _bank(qres.items)
            # D4 fuzzy orientation is otherwise dropped at banking (the
            # fallback returns metadata-only results): copy the latest hint
            # into the outcome side-channel so the next reasoning turn can
            # use it as non-citable steering.
            try:
                _meta = getattr(qres, "provider_metadata", None) or {}
                _fuzzy = _meta.get("fuzzy_orientation") if isinstance(_meta, dict) else None
                if isinstance(_fuzzy, dict) and _fuzzy:
                    outcome_detail["fuzzy_orientation"] = _fuzzy
            except Exception:
                pass
            if summarize is not None:
                try:
                    banked = list(banked) + list(summarize(qres) or [])
                except Exception as exc:
                    story_log(
                        logger,
                        logging.WARNING,
                        "%s summary failed; keeping items",
                        action_label,
                        comp=COMP_ACTVTY,
                        func="execute_action_activity",
                        stage="execute",
                        step=str(action_label).lower(),
                        outcome=OUTCOME_SKIPPED,
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event=f"{action_label} summarize failed",
                        error=str(exc),
                        action_label=action_label,
                    )
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            if not _banked_positive(banked) and evidence_repo is not None:
                # Phase 2 item 3: a well-formed query with zero matches banks
                # a scoped no-match proof for future absence claims. D1
                # (claim-proof gaps): the history provider now returns a
                # zero-match proof row on empty walks — that row must not
                # suppress this discovery row, only positive evidence does.
                # Saved directly (never counted in the outcome), so replan-
                # on-empties still fires and the row stays citable via repo
                # scan. Fingerprint dedupes repeats idempotently.
                try:
                    from investigation_agent_platform.domain.evidence.models import (
                        EvidenceType as _EvidenceType,
                    )

                    _absence_scope = _code_absence_scope(action_label, req)
                    if _absence_scope:
                        await evidence_repo.save(
                            tenant_id,
                            _with_fetch_reason(
                                _absence_proof_row(
                                    tenant_id=tenant_id,
                                    investigation_id=investigation_id,
                                    # History misses bank as discovery-only
                                    # SOURCE_CODE: a COMMIT_HISTORY-typed absence
                                    # row carries no file/revision and can never
                                    # satisfy history_proof, inflating evidence
                                    # counts while reading as "unstamped".
                                    evidence_type=_EvidenceType.SOURCE_CODE,
                                    provider="code-intelligence",
                                    operation=action_label.lower(),
                                    scope=_absence_scope,
                                ),
                                _fetch_reason,
                            ),
                            investigation_id,
                        )
                except Exception:
                    pass
            return _record_outcome(banked)

        async def _handle_search_code() -> int:
            from investigation_agent_platform.domain.evidence.requests import (
                CodeSearchRequest,
            )

            def _build() -> CodeSearchRequest:
                return CodeSearchRequest(
                    repository=application_id or "example-app",
                    revision=str(parameters.get("revision", "HEAD")),
                    query=str(parameters.get("query", "")),
                    language=parameters.get("language"),
                    # Stored scopes ride the ledger budget key as "scope";
                    # map the path-prefix form onto the handler parameter
                    # the provider actually reads. An explicit path_prefix
                    # (prior action's own prefix) always wins.
                    path_prefix=parameters.get("path_prefix")
                    or _scope_path_prefix(parameters.get("scope")),
                    limit=min(int(parameters.get("limit", 20)), 100),
                    # Fail-closed opt-in: when the caller requires the
                    # provider to honor a pinned revision, an incapable
                    # provider raises UNSUPPORTED_CAPABILITY instead of
                    # silently returning HEAD rows. Default stays discovery.
                    require_revision=bool(parameters.get("require_revision", False)),
                )

            return await _handle_code_navigation(
                "SEARCH_CODE", "code_provider_not_configured", _build, "search_code"
            )

        async def _handle_find_symbol() -> int:
            from investigation_agent_platform.domain.evidence.requests import (
                SymbolRequest,
            )

            def _build() -> SymbolRequest:
                return SymbolRequest(
                    repository=application_id or "example-app",
                    revision=str(parameters.get("revision", "HEAD")),
                    symbol_name=str(parameters.get("symbol_name", "")),
                    require_revision=bool(parameters.get("require_revision", False)),
                )

            def _summarize(qres: Any) -> list[Any]:
                # Phase 0.2: gateway returns symbols in metadata with
                # items=[]; bank typed discovery packets so the symbol
                # reaches reasoning instead of vanishing.
                meta = getattr(qres, "provider_metadata", None) or {}
                symbols = meta.get("symbols", []) or []
                outcome_detail["_symbol_count"] = len(symbols)
                if not symbols:
                    _emit_telemetry(
                        ctx, tenant_id, investigation_id, "record_symbol_empty", "FIND_SYMBOL"
                    )
                return _symbol_evidence_packets(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    symbols=symbols,
                    symbol_name=str(parameters.get("symbol_name", "")),
                )

            banked_count = await _handle_code_navigation(
                "FIND_SYMBOL",
                "code_provider_not_configured",
                _build,
                "find_symbol",
                summarize=_summarize,
            )
            if banked_count == 0:
                outcome_detail.pop("_symbol_count", None)
            return banked_count

        async def _handle_walk_stack() -> int:
            """D2: resolve stack frames to ordered EXCEPTION_PATH evidence.

            For each frame (max 10): search the function name, take the
            first hit carrying file+revision provenance, and bank one
            frame-ordered row (frame file/line preferred, hit fallback).
            Frames resolving nowhere are banked as explicit unresolved
            rows (no file/line/revision, never a synthetic location) and
            counted in the miss detail, not failed. Zero resolutions
            degrades with miss_reason.
            """
            from investigation_agent_platform.domain.evidence.models import (
                Evidence,
                EvidenceType,
            )
            from investigation_agent_platform.domain.evidence.requests import (
                CodeSearchRequest,
            )
            from investigation_agent_platform.domain.provenance.models import (
                EvidenceFreshness,
                EvidenceProvenance,
                QueryFingerprint,
                SourceLocation,
            )

            raw_frames = parameters.get("frames", [])
            frames = [f for f in (raw_frames or []) if isinstance(f, dict)][:10]
            error_class = str(parameters.get("error_class", "") or "")[:256]
            if not frames:
                story_log(
                    logger,
                    logging.WARNING,
                    "WALK_STACK without frames degrades to empty",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="walk_stack",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="walk_stack missing frames",
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "missing_frames"
                return 0
            now = datetime.now(UTC)
            resolved = 0
            unresolved = 0
            banked_rows: list[Any] = []

            def _unresolved_frame_row(
                frame_index: int,
                func_name: str,
                file_hint_value: str,
                reason: str,
            ) -> Any:
                """Explicit unresolved frame — never a synthetic location.

                The row carries no file_path/line/revision provenance, so
                `_cited_location_evidence` excludes it by construction. It
                exists so the reasoner sees *which* frame failed and why,
                instead of a silent count in miss_reason.
                """
                return Evidence(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    evidence_type=EvidenceType.EXCEPTION_PATH,
                    provider="code-intelligence",
                    source=f"stack://frame-{frame_index}/unresolved",
                    title=f"Frame {frame_index}: unresolved ({reason})",
                    summary=(
                        f"Stack frame {frame_index} "
                        f"({func_name or file_hint_value or 'unknown'}) "
                        f"could not be resolved: {reason}."
                    ),
                    content_snippet="",
                    attributes={
                        "unresolved": True,
                        "reason": reason,
                        "frame_index": frame_index,
                        "frame_file": file_hint_value,
                        "frame_function": func_name,
                    },
                    observed_at=now,
                    retrieved_at=now,
                    provenance=EvidenceProvenance(
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                        provider_type="code-intelligence",
                        requested_provider_id="treesitter-local",
                        actual_provider_id="treesitter-local",
                        source_system="stack-walk",
                        retrieval_timestamp=now,
                        query_fingerprint=QueryFingerprint(
                            provider_type="code-intelligence",
                            operation="walk_stack",
                            normalized_query_hash="unresolved",
                        ),
                        source_location=SourceLocation(
                            system="git",
                            identifier=f"frame-{frame_index}-unresolved",
                            file_path=None,
                            line_start=None,
                            line_end=None,
                            revision=None,
                        ),
                    ),
                    freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
                    fingerprint=_stack_fingerprint(
                        tenant_id,
                        investigation_id,
                        frame_index,
                        f"unresolved:{reason}:{file_hint_value}",
                        0,
                    ),
                )

            for index, frame in enumerate(frames):
                try:
                    func = str(frame.get("function", "") or "").strip()
                    file_hint = str(frame.get("file", "") or "").strip()
                    try:
                        frame_line = int(frame.get("line") or 0) or None
                    except (TypeError, ValueError):
                        frame_line = None
                    query = func or file_hint
                    if not query:
                        banked_rows.append(
                            _unresolved_frame_row(index, func, file_hint, "missing_query")
                        )
                        unresolved += 1
                        continue
                    qres = await gateway.search_code(
                        tenant_id,
                        investigation_id,
                        application_id or "example-app",
                        CodeSearchRequest(
                            repository=application_id or "example-app",
                            revision=str(parameters.get("revision", "HEAD")),
                            query=query,
                            limit=3,
                            # Deliberately discovery: this search only finds
                            # candidate files for the frame hint. The pin
                            # comes from the range-read confirm below, so a
                            # strict require_revision here would only turn
                            # resolvable frames into unresolved rows.
                        ),
                    )
                except Exception as exc:
                    story_log(
                        logger,
                        logging.WARNING,
                        "WALK_STACK frame search failed; continuing",
                        comp=COMP_ACTVTY,
                        func="execute_action_activity",
                        stage="execute",
                        step="walk_stack",
                        outcome=OUTCOME_SKIPPED,
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="walk_stack frame failed",
                        error=str(exc),
                    )
                    banked_rows.append(
                        _unresolved_frame_row(
                            index,
                            str(frame.get("function", "") or "").strip()
                            if isinstance(frame, dict)
                            else "",
                            str(frame.get("file", "") or "").strip()
                            if isinstance(frame, dict)
                            else "",
                            "frame_search_failed",
                        )
                    )
                    unresolved += 1
                    continue
                # Phase 0.3: a frame may only be banked when its file
                # actually matches the frame's file hint AND a range read
                # at the frame line succeeds (proving the line exists and
                # capturing the real revision). Never synthesize a location
                # from a mismatched hit.
                hit = None
                for candidate in qres.items or []:
                    try:
                        location = getattr(
                            getattr(candidate, "provenance", None),
                            "source_location",
                            None,
                        )
                        hit_file = str(getattr(location, "file_path", "") or "")
                        if hit_file and _frame_file_matches(file_hint, hit_file):
                            hit = candidate
                            break
                    except Exception:
                        continue
                if hit is None:
                    banked_rows.append(
                        _unresolved_frame_row(index, func, file_hint, "no_file_match")
                    )
                    unresolved += 1
                    continue
                try:
                    from investigation_agent_platform.domain.evidence.requests import (
                        SourceRequest,
                    )

                    hit_location = hit.provenance.source_location
                    file_path = str(getattr(hit_location, "file_path", "") or "")
                    if not file_path:
                        banked_rows.append(
                            _unresolved_frame_row(index, func, file_hint, "missing_file")
                        )
                        unresolved += 1
                        continue
                    try:
                        anchor_line = int(frame_line or getattr(hit_location, "line_start", 0) or 0)
                    except (TypeError, ValueError):
                        anchor_line = 0
                    if anchor_line < 1:
                        banked_rows.append(
                            _unresolved_frame_row(index, func, file_hint, "missing_line")
                        )
                        unresolved += 1
                        continue
                    frame_read = await gateway.get_source(
                        tenant_id,
                        investigation_id,
                        application_id or "example-app",
                        SourceRequest(
                            repository=application_id or "example-app",
                            file_path=file_path,
                            revision=str(parameters.get("revision", "HEAD")),
                            start_line=anchor_line,
                            end_line=anchor_line,
                        ),
                    )
                    read_location = getattr(
                        getattr(frame_read, "provenance", None),
                        "source_location",
                        None,
                    )
                    confirmed_line = getattr(read_location, "line_start", None)
                    if confirmed_line != anchor_line:
                        # The requested line was clamped (past EOF), so the
                        # frame does not resolve to that line. Bank it as
                        # unresolved instead of fabricating a location.
                        banked_rows.append(
                            _unresolved_frame_row(index, func, file_hint, "line_not_confirmed")
                        )
                        unresolved += 1
                        continue
                    banked_rows.append(frame_read)
                    banked_rows.append(
                        Evidence(
                            tenant_id=tenant_id,
                            investigation_id=investigation_id,
                            evidence_type=EvidenceType.EXCEPTION_PATH,
                            provider="code-intelligence",
                            source=f"stack://frame-{index}",
                            title=(
                                f"Frame {index}: {func or file_hint} ({file_path}:{anchor_line})"
                            ),
                            summary=(
                                f"Stack frame {index} ({error_class or 'error'}) "
                                f"resolves to {file_path}:{anchor_line} "
                                f"revision {getattr(read_location, 'revision', None)}."
                            ),
                            content_snippet=str(getattr(frame_read, "content_snippet", "") or "")[
                                :2000
                            ],
                            observed_at=now,
                            retrieved_at=now,
                            provenance=EvidenceProvenance(
                                tenant_id=tenant_id,
                                investigation_id=investigation_id,
                                provider_type="code-intelligence",
                                requested_provider_id="treesitter-local",
                                actual_provider_id="treesitter-local",
                                source_system="stack-walk",
                                retrieval_timestamp=now,
                                query_fingerprint=QueryFingerprint(
                                    provider_type="code-intelligence",
                                    operation="walk_stack",
                                    normalized_query_hash=str(
                                        getattr(
                                            getattr(hit, "provenance", None),
                                            "query_fingerprint",
                                            None,
                                        )
                                        and getattr(
                                            hit.provenance.query_fingerprint,
                                            "normalized_query_hash",
                                            "",
                                        )
                                        or ""
                                    ),
                                ),
                                source_location=SourceLocation(
                                    system="git",
                                    identifier=f"frame-{index}",
                                    file_path=file_path,
                                    line_start=anchor_line,
                                    line_end=anchor_line,
                                    revision=getattr(read_location, "revision", None),
                                ),
                            ),
                            freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
                            fingerprint=_stack_fingerprint(
                                tenant_id,
                                investigation_id,
                                index,
                                file_path,
                                anchor_line,
                            ),
                        )
                    )
                    resolved += 1
                except Exception as exc:
                    story_log(
                        logger,
                        logging.WARNING,
                        "WALK_STACK row build failed; continuing",
                        comp=COMP_ACTVTY,
                        func="execute_action_activity",
                        stage="execute",
                        step="walk_stack",
                        outcome=OUTCOME_SKIPPED,
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="walk_stack row failed",
                        error=str(exc),
                    )
                    banked_rows.append(
                        _unresolved_frame_row(index, func, file_hint, "row_build_failed")
                    )
                    unresolved += 1
                    continue
            banked = _bank(banked_rows)
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            if resolved == 0:
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "walk_stack_no_resolution"
            else:
                outcome_detail["miss_reason"] = (
                    f"walk_stack_unresolved_{unresolved}" if unresolved else None
                )
            return _record_outcome(banked)

        async def _handle_trace_logic() -> int:
            """D2: resolve a consuming symbol and extract decision predicates.

            Takes {symbol, attributes[]}: FIND_SYMBOL locates the definition,
            GET_CODE reads it, and branch lines (if/switch/case/ternary)
            mentioning any attribute name are extracted as the decision
            path. Banks the range-read rows (SOURCE_CODE, citable) with the
            predicate lines recorded in miss/detail context. Generic across
            Java/Python (text-level predicates, never language AST).
            """
            import re as _re

            from investigation_agent_platform.domain.evidence.requests import (
                SymbolRequest,
            )

            symbol = str(parameters.get("symbol", "") or "").strip()
            raw_attributes = parameters.get("attributes", []) or []
            attributes = [
                str(a).strip()
                for a in (raw_attributes if isinstance(raw_attributes, list) else [])
                if str(a or "").strip()
            ][:20]
            if not symbol:
                story_log(
                    logger,
                    logging.WARNING,
                    "TRACE_LOGIC without symbol degrades to empty",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="trace_logic",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="trace_logic missing symbol",
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "missing_symbol"
                return 0
            try:
                symbol_rows = await gateway.find_symbol(
                    tenant_id,
                    investigation_id,
                    application_id or "example-app",
                    SymbolRequest(
                        repository=application_id or "example-app",
                        revision=str(parameters.get("revision", "HEAD")),
                        symbol_name=symbol,
                        require_revision=bool(parameters.get("require_revision", False)),
                    ),
                )
            except Exception as exc:
                story_log(
                    logger,
                    logging.WARNING,
                    "TRACE_LOGIC symbol lookup failed; degrading",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="trace_logic",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="trace_logic symbol failed",
                    error=str(exc),
                )
                try:
                    miss_reason, outcome_status, _blocking = classify_provider_failure(
                        exc, "trace_logic_symbol_miss"
                    )
                except Exception:
                    miss_reason, outcome_status = "trace_logic_symbol_miss", "DEGRADED"
                outcome_detail["status"] = outcome_status
                outcome_detail["miss_reason"] = miss_reason
                if miss_reason == "unsupported_capability":
                    outcome_detail["capability"] = getattr(exc, "capability", None)
                return 0
            # Phase 0.2/0.4: symbols arrive in provider_metadata (not
            # items); convert to discovery packets, then read a window
            # around each definition so predicate lines are anchored to
            # absolute line numbers and the read is a real range read.
            symbol_meta = getattr(symbol_rows, "provider_metadata", None) or {}
            definitions = _symbol_evidence_packets(
                tenant_id=tenant_id,
                investigation_id=investigation_id,
                symbols=symbol_meta.get("symbols", []) or [],
                symbol_name=symbol,
            )
            outcome_detail["_symbol_count"] = len(definitions)
            if not definitions:
                _emit_telemetry(
                    ctx, tenant_id, investigation_id, "record_symbol_empty", "TRACE_LOGIC"
                )
            banked_rows: list[Any] = list(definitions)
            predicate_lines: list[str] = []
            for packet in definitions[:3]:
                try:
                    from investigation_agent_platform.domain.evidence.requests import (
                        SourceRequest,
                    )

                    location = getattr(packet.provenance, "source_location", None)
                    file_path = str(getattr(location, "file_path", "") or "")
                    if not file_path:
                        continue
                    try:
                        anchor_line = int(getattr(location, "line_start", 0) or 0)
                    except (TypeError, ValueError):
                        anchor_line = 0
                    window_start = max(1, anchor_line - 120) if anchor_line else None
                    window_end = anchor_line + 120 if anchor_line else None
                    code = await gateway.get_source(
                        tenant_id,
                        investigation_id,
                        application_id or "example-app",
                        SourceRequest(
                            repository=application_id or "example-app",
                            file_path=file_path,
                            revision=str(parameters.get("revision", "HEAD")),
                            start_line=window_start,
                            end_line=window_end,
                        ),
                    )
                    banked_rows.append(code)
                    if attributes:
                        lowered_attrs = [a.lower() for a in attributes]
                        base_line = window_start or 1
                        for offset, line in enumerate(
                            str(getattr(code, "content_snippet", "") or "").splitlines()
                        ):
                            lowered = line.lower()
                            if _re.search(
                                r"\b(if|else|switch|case|when|elif|match)\b", lowered
                            ) and any(attr in lowered for attr in lowered_attrs):
                                predicate_lines.append(
                                    f"{file_path}:{base_line + offset}: {line.strip()[:160]}"
                                )
                                if len(predicate_lines) >= 20:
                                    break
                except Exception:
                    continue
            banked = _bank(banked_rows)
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            if not banked:
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "trace_logic_no_resolution"
            else:
                outcome_detail["miss_reason"] = None
                outcome_detail["predicate_lines"] = predicate_lines[:20]
            return _record_outcome(banked)

        async def _handle_find_call_graph() -> int:
            from investigation_agent_platform.domain.evidence.requests import (
                CallGraphRequest,
            )

            def _build() -> CallGraphRequest:
                return CallGraphRequest(
                    repository=application_id or "example-app",
                    revision=str(parameters.get("revision", "HEAD")),
                    symbol=str(parameters.get("symbol", "")),
                    direction=str(parameters.get("direction", "BOTH")),
                    depth=min(int(parameters.get("depth", 2)), 5),
                    require_revision=bool(parameters.get("require_revision", False)),
                )

            def _summarize(qres: Any) -> list[Any]:
                # P1-2: gateway returns call nodes in metadata with items=[]
                # — bank one CALL_GRAPH summary so the result reaches
                # reasoning instead of vanishing like CORRELATE used to.
                meta = getattr(qres, "provider_metadata", None) or {}
                callers = meta.get("callers", []) or []
                callees = meta.get("callees", []) or []
                outcome_detail["_graph_callers"] = len(callers)
                outcome_detail["_graph_callees"] = len(callees)
                graph_edges: list[dict] = []
                for role, node in [("caller", c) for c in callers] + [
                    ("callee", c) for c in callees
                ]:
                    try:
                        if not isinstance(node, dict):
                            continue
                        loc = node.get("location") or {}
                        if not isinstance(loc, dict):
                            continue
                        try:
                            edge_line = int(loc.get("line_number") or 0) or None
                        except (TypeError, ValueError):
                            edge_line = None
                        graph_edges.append(
                            {
                                "role": role,
                                "caller": str(node.get("caller_symbol") or "?")[:256],
                                "callee": str(node.get("callee_symbol") or "?")[:256],
                                "file": str(loc.get("file_path") or "").strip(),
                                "line": edge_line,
                            }
                        )
                    except Exception:
                        continue
                outcome_detail["_graph_edges"] = graph_edges[:10]
                if not callers and not callees:
                    return []
                import hashlib as _hashlib

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

                now = datetime.now(UTC)
                symbol = str(parameters.get("symbol", ""))
                lines = [
                    f"{(c.get('caller_symbol') or '?')} -> {(c.get('callee_symbol') or '?')} "
                    f"({(c.get('location') or {}).get('file_path', '?')})"
                    for c in (callers + callees)[:40]
                    if isinstance(c, dict)
                ]
                fp = _hashlib.sha256(
                    f"callgraph:{tenant_id}:{symbol}:{len(callers)}:{len(callees)}".encode()
                ).hexdigest()
                summary_row = Evidence(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    evidence_type=EvidenceType.CALL_GRAPH,
                    provider="code-intelligence",
                    source=f"callgraph://{symbol}",
                    title=f"Call graph: {symbol} ({len(callers)} callers, {len(callees)} callees)",
                    summary=(
                        f"Call graph for '{symbol}': {len(callers)} caller(s), "
                        f"{len(callees)} callee(s)."
                    ),
                    content_snippet="\n".join(lines)[:4000],
                    attributes={
                        "discovery_only": True,
                        "revision_supported": False,
                    },
                    observed_at=now,
                    retrieved_at=now,
                    provenance=EvidenceProvenance(
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                        provider_type="code-intelligence",
                        requested_provider_id="treesitter-local",
                        actual_provider_id="treesitter-local",
                        source_system="call-graph",
                        retrieval_timestamp=now,
                        query_fingerprint=QueryFingerprint(
                            provider_type="code-intelligence",
                            operation="find_call_graph",
                            normalized_query_hash=fp[:32],
                        ),
                        source_location=SourceLocation(
                            system="call-graph",
                            identifier=symbol,
                        ),
                    ),
                    freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
                    fingerprint=f"callgraph-{fp[:32]}",
                )
                # Per-edge packets: the nodes already carry file/line
                # provenance — bank each edge with it instead of leaving
                # only the location-less summary. Edges stay discovery_only
                # (a graph edge is not a shown call site) but now give the
                # model and the call-site forcing rule concrete files.
                edge_rows: list[Any] = []
                for edge in (callers + callees)[:10]:
                    try:
                        if not isinstance(edge, dict):
                            continue
                        loc = edge.get("location") or {}
                        if not isinstance(loc, dict):
                            continue
                        file_path = str(loc.get("file_path", "") or "").strip()
                        if not file_path:
                            continue
                        try:
                            edge_line = int(loc.get("line_number") or 0) or None
                        except (TypeError, ValueError):
                            edge_line = None
                        caller = str(edge.get("caller_symbol", "") or "?")[:256]
                        callee = str(edge.get("callee_symbol", "") or "?")[:256]
                        edge_fp = _hashlib.sha256(
                            f"calledge:{tenant_id}:{investigation_id}:{caller}:"
                            f"{callee}:{file_path}:{edge_line or 0}".encode()
                        ).hexdigest()[:64]
                        edge_rows.append(
                            Evidence(
                                tenant_id=tenant_id,
                                investigation_id=investigation_id,
                                evidence_type=EvidenceType.CALL_GRAPH,
                                provider="code-intelligence",
                                source=f"callgraph://{caller}->{callee}",
                                title=f"Call edge: {caller} -> {callee}",
                                summary=(
                                    f"'{caller}' calls '{callee}' "
                                    f"({file_path}"
                                    f"{':' + str(edge_line) if edge_line else ''})."
                                ),
                                content_snippet=(
                                    f"{caller} -> {callee} "
                                    f"({file_path}"
                                    f"{':' + str(edge_line) if edge_line else ''})"
                                )[:2000],
                                attributes={
                                    "discovery_only": True,
                                    "revision_supported": bool(
                                        loc.get("revision_supported", False)
                                    ),
                                    "caller_symbol": caller,
                                    "callee_symbol": callee,
                                },
                                observed_at=now,
                                retrieved_at=now,
                                provenance=EvidenceProvenance(
                                    tenant_id=tenant_id,
                                    investigation_id=investigation_id,
                                    provider_type="code-intelligence",
                                    requested_provider_id="treesitter-local",
                                    actual_provider_id="treesitter-local",
                                    source_system="call-graph",
                                    retrieval_timestamp=now,
                                    query_fingerprint=QueryFingerprint(
                                        provider_type="code-intelligence",
                                        operation="find_call_graph",
                                        normalized_query_hash=edge_fp[:32],
                                    ),
                                    source_location=SourceLocation(
                                        system="call-graph",
                                        identifier=f"{caller}->{callee}",
                                        file_path=file_path,
                                        line_start=edge_line,
                                        line_end=edge_line,
                                        revision=str(loc.get("revision", "") or "") or None,
                                    ),
                                ),
                                freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
                                fingerprint=edge_fp,
                            )
                        )
                    except Exception:
                        continue
                return [summary_row] + edge_rows

            banked_count = await _handle_code_navigation(
                "FIND_CALL_GRAPH",
                "code_provider_not_configured",
                _build,
                "find_call_graph",
                summarize=_summarize,
            )
            if banked_count == 0:
                outcome_detail.pop("_graph_callers", None)
                outcome_detail.pop("_graph_callees", None)
                return await fallback_call_graph_to_search(
                    gateway=gateway,
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    application_id=application_id,
                    parameters=parameters,
                    evidence_repo=evidence_repo,
                    bank=_bank,
                    record_outcome=_record_outcome,
                    outcome_detail=outcome_detail,
                    inv_id_str=inv_id_str,
                )
            _callers_n = outcome_detail.pop("_graph_callers", 1)
            _callees_n = outcome_detail.pop("_graph_callees", 1)
            if _callers_n == 0 or _callees_n == 0:
                extra = await fallback_call_graph_to_search(
                    gateway=gateway,
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    application_id=application_id,
                    parameters=parameters,
                    evidence_repo=evidence_repo,
                    bank=_bank,
                    record_outcome=_record_outcome,
                    outcome_detail=outcome_detail,
                    inv_id_str=inv_id_str,
                    union=True,
                )
                return banked_count + extra
            # Call-site chaining (L2 twice): a graph edge names the caller
            # file but no row shows the call expression the verifier
            # demands. Read a window around each caller edge (one per
            # file, bounded) so every visible caller gets a real
            # call-site row (TRACE_LOGIC pattern). Previously only the
            # first edge was read and the second caller never became
            # citable. Failures keep the graph results regardless.
            try:
                _edges = [
                    e for e in (outcome_detail.pop("_graph_edges", []) or []) if isinstance(e, dict)
                ]
                _targets = _caller_chain_targets(_edges)
                _banked_here: list[str] = []
                if evidence_repo is not None:
                    from investigation_agent_platform.domain.evidence.models import (
                        EvidenceType as _EvidenceType,
                    )
                    from investigation_agent_platform.domain.evidence.requests import (
                        SourceRequest as _SourceRequest,
                    )

                    for _target in _targets:
                        _anchor = int(_target["line"])
                        _read = await gateway.get_source(
                            tenant_id,
                            investigation_id,
                            application_id or "example-app",
                            _SourceRequest(
                                repository=application_id or "example-app",
                                file_path=str(_target["file"]),
                                revision=str(parameters.get("revision", "HEAD")),
                                start_line=max(1, _anchor - 40),
                                end_line=_anchor + 40,
                            ),
                        )
                        _read = _read.model_copy(
                            update={
                                "evidence_type": _EvidenceType.SOURCE_CODE,
                                "attributes": {
                                    **(getattr(_read, "attributes", None) or {}),
                                    "read_mode": "range",
                                    "call_graph_role": "caller",
                                },
                            }
                        )
                        _read = _with_fetch_reason(_read, _fetch_reason)
                        await evidence_repo.save(tenant_id, _read, investigation_id)
                        _banked_here.append(str(_read.evidence_id))
                if _banked_here:
                    try:
                        outcome_detail["evidence_ids"] = (
                            list(outcome_detail.get("evidence_ids") or []) + _banked_here
                        )
                    except Exception:
                        pass
                    return banked_count + len(_banked_here)
            except Exception:
                pass
            return banked_count

        async def _handle_get_code_history() -> int:
            from investigation_agent_platform.domain.evidence.requests import (
                CodeHistoryRequest,
            )

            def _build(path: str | None = None) -> CodeHistoryRequest:
                return CodeHistoryRequest(
                    repository=application_id or "example-app",
                    file_path=str(path if path is not None else parameters.get("file_path", "")),
                    symbol=parameters.get("symbol"),
                    limit=min(int(parameters.get("limit", 10)), 50),
                )

            banked_count = await _handle_code_navigation(
                "GET_CODE_HISTORY",
                "code_provider_not_configured",
                _build,
                "get_code_history",
            )
            if banked_count > 0:
                return banked_count
            # Tranche B: bare filenames (SampleServiceImpl.java) resolve
            # nowhere — locate the file via SEARCH_CODE, then take history
            # per hit (max 3). Deterministic fallback, miss-marked.
            from investigation_agent_platform.domain.evidence.requests import (
                CodeSearchRequest,
            )

            requested = str(parameters.get("file_path", "") or "").strip()
            if not requested or "/" in requested.strip("/"):
                return banked_count
            try:
                qres = await gateway.search_code(
                    tenant_id,
                    investigation_id,
                    application_id or "example-app",
                    CodeSearchRequest(
                        repository=application_id or "example-app",
                        revision="HEAD",
                        query=requested,
                        limit=10,
                    ),
                )
            except Exception as exc:
                story_log(
                    logger,
                    logging.WARNING,
                    "GET_CODE_HISTORY filename locate failed; keeping empty",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="get_code_history",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="history locate failed",
                    error=str(exc),
                )
                return banked_count
            seen: set[str] = set()
            extra_banked = 0
            for hit in qres.items or []:
                try:
                    provenance = getattr(hit, "provenance", None)
                    location = getattr(provenance, "source_location", None)
                    found = str(getattr(location, "file_path", "") or "")
                except Exception:
                    continue
                if not found or found in seen:
                    continue
                seen.add(found)
                if len(seen) > 3:
                    break
                try:
                    hres = await gateway.get_code_history(
                        tenant_id,
                        investigation_id,
                        application_id or "example-app",
                        _build(found),
                    )
                except Exception:
                    continue
                banked = _bank(hres.items)
                if evidence_repo is not None and banked:
                    await evidence_repo.save_batch(tenant_id, banked, investigation_id)
                extra_banked += _record_outcome(banked)
            if extra_banked:
                outcome_detail["miss_reason"] = "history_filename_fallback"
                outcome_detail["status"] = "SUCCEEDED"
            return banked_count + extra_banked

        async def _handle_probe_api() -> int:
            from investigation_agent_platform.domain.common.exceptions import (
                ExecutionError,
                ProbeEndpointNotFoundError,
            )
            from investigation_agent_platform.ports.evidence.api_probe import (
                ApiProbeRequest,
            )

            try:
                req = ApiProbeRequest(
                    endpoint_id=str(parameters.get("endpoint_id", "")),
                    path_params={
                        str(k): str(v) for k, v in (parameters.get("path_params", {}) or {}).items()
                    },
                    query_params={
                        str(k): str(v)
                        for k, v in (parameters.get("query_params", {}) or {}).items()
                    },
                )
            except Exception as exc:
                story_log(
                    logger,
                    logging.WARNING,
                    "PROBE_API invalid parameters degrades to empty",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="probe_api",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="PROBE_API invalid parameters",
                    error=str(exc),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "invalid_parameters"
                return 0
            if not req.endpoint_id.strip():
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "missing_endpoint_id"
                return 0
            try:
                qres = await gateway.probe_api(
                    tenant_id, investigation_id, application_id or "example-app", req
                )
            except (ProbeEndpointNotFoundError, ExecutionError) as exc:
                story_log(
                    logger,
                    logging.WARNING,
                    "PROBE_API degrades to empty",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="probe_api",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="PROBE_API endpoint miss",
                    error=str(exc),
                )
                outcome_detail["status"] = "DEGRADED"
                outcome_detail["miss_reason"] = "probe_endpoint_not_found"
                return 0
            banked = _bank(qres.items)
            # D4: expected-vs-actual comparison. parameters.expected holds
            # the golden payload; divergences bank as DIVERGENCE rows
            # (absence is evidence). Best-effort JSON parse, never loud.
            try:
                expected = parameters.get("expected", None)
                if isinstance(expected, dict) and expected:
                    from investigation_agent_platform.application.evidence.divergence import (
                        diff_expected_actual,
                        shape_divergence_evidence,
                    )

                    for item in qres.items or []:
                        try:
                            import json as _json

                            body_text = str(getattr(item, "content_snippet", "") or "")
                            actual = _json.loads(body_text)
                        except Exception:
                            continue
                        for div in diff_expected_actual(expected, actual)[:10]:
                            banked.append(
                                shape_divergence_evidence(
                                    tenant_id=tenant_id,
                                    investigation_id=investigation_id,
                                    source_ref=str(getattr(item, "evidence_id", "") or ""),
                                    endpoint=str(parameters.get("endpoint_id", "") or ""),
                                    divergence=div,
                                )
                            )
                        break
            except Exception:
                pass
            if evidence_repo is not None and banked:
                await evidence_repo.save_batch(tenant_id, banked, investigation_id)
            return _record_outcome(banked)

        async def _persist_proposed_hypotheses() -> int:
            # Hypothesis persistence happens for EVERY action type, not just
            # FORMULATE_HYPOTHESIS: the model attaches hypotheses_evaluated
            # to whatever action it proposes (often VERIFY_HYPOTHESIS or an
            # evidence-gathering action like SEARCH_CODE), so gating
            # persistence on the action type leaves the repo empty forever —
            # verification can never verify, and the ConclusionGate can never
            # approve COMPLETED. Statements dedupe against existing rows, so
            # calling this after an in-handler persist is a safe no-op.
            if not proposed_hypotheses:
                return 0
            if hypothesis_repo is None:
                return 0
            from investigation_agent_platform.domain.hypothesis.models import (
                AssessmentStrength,
                AssessmentType,
                ClaimKind,
                ClaimTarget,
                Hypothesis,
                HypothesisEvidenceAssessment,
            )

            try:
                existing = await hypothesis_repo.find_by_investigation_id(
                    tenant_id, investigation_id
                )
            except Exception as exc:
                story_log(
                    logger,
                    logging.ERROR,
                    "hypothesis dedupe read failed",
                    comp=COMP_ACTVTY,
                    func="execute_action_activity",
                    stage="execute",
                    step="formulate",
                    outcome=OUTCOME_SKIPPED,
                    tenant_id=tenant_id,
                    investigation_id=inv_id_str,
                    event="hypothesis dedupe read failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
                existing = []
            known = {str(getattr(h, "statement", "")).strip() for h in (existing or [])}
            saved = 0
            # Hypothesis budget: the profile's maximumHypotheses (via limits
            # overlay) caps the TOTAL, not just this batch — previously the
            # validator's cap was a literal `pass` and nothing enforced it.
            allowed = max(0, limits.max_hypotheses - len(existing or []))
            # P0-3: model-cited supporting evidence. FORMULATE_HYPOTHESIS may
            # carry supporting_evidence_ids — validated here against this
            # investigation's evidence rows (unknown IDs are dropped, never
            # trusted) and persisted on the hypothesis so verification can
            # bind claims to evidence instead of the whole corpus.
            cited: list[UUID] = []
            try:
                raw_ids = parameters.get("supporting_evidence_ids", []) or []
                if isinstance(raw_ids, list) and evidence_repo is not None:
                    rows = await evidence_repo.find_by_investigation_id(tenant_id, investigation_id)
                    known_ids = {getattr(e, "evidence_id", None) for e in (rows or [])}
                    for raw in raw_ids[:20]:
                        try:
                            cid = UUID(str(raw))
                        except (ValueError, TypeError, AttributeError):
                            continue
                        if cid in known_ids:
                            cited.append(cid)
            except Exception:
                cited = []
            known_evidence_rows: dict[UUID, Any] = {}
            try:
                if evidence_repo is not None:
                    _known_rows = await evidence_repo.find_by_investigation_id(
                        tenant_id, investigation_id
                    )
                    known_evidence_rows = {
                        getattr(row, "evidence_id", None): row
                        for row in (_known_rows or [])
                        if getattr(row, "evidence_id", None) is not None
                    }
            except Exception:
                known_evidence_rows = {}
            known_evidence_ids: set[Any] = set()
            if proposed_claims and evidence_repo is not None:
                known_evidence_ids = set(known_evidence_rows)
            for text in proposed_hypotheses[:20]:
                if saved >= allowed:
                    break
                statement = str(text or "").strip()[:2048]
                if not statement or statement in known:
                    continue
                known.add(statement)
                # Phase 2.1: typed claims carry their OWN evidence binding;
                # only statements without one inherit the batch-wide list.
                # Same statement with several kinds persists each kind's
                # binding as its own assessment.
                hypothesis_id = uuid4()
                claim_cited = list(cited)
                assessments: list[Any] = []
                typed_list = [
                    (claim_key, raw)
                    for claim_key, raw in proposed_claims.items()
                    if claim_key[0] == statement
                ]
                if typed_list:
                    # A typed claim carries ONLY its own binding — the
                    # batch-wide list is dropped, never merged.
                    claim_cited = []
                claim_binding_ids: dict[tuple[str, str, str, str, str], list[UUID]] = {}
                caller_obligations: list[tuple[Any, dict[str, Any], list[UUID]]] = []
                for claim_key, typed in typed_list:
                    typed_cited: list[Any] = []
                    for raw in (typed.get("evidence_ids", []) or [])[:20]:
                        try:
                            cid = UUID(str(raw))
                        except (ValueError, TypeError, AttributeError):
                            continue
                        if cid in known_evidence_ids:
                            if cid not in claim_cited:
                                claim_cited.append(cid)
                            if cid not in typed_cited:
                                typed_cited.append(cid)
                    claim_binding_ids[claim_key] = typed_cited
                    try:
                        kind = ClaimKind(str(typed.get("claim_kind") or "CAUSAL"))
                        raw_target = typed.get("target")
                        target = (
                            ClaimTarget.model_validate(raw_target)
                            if isinstance(raw_target, dict)
                            else None
                        )
                        if kind == ClaimKind.DEFINITION:
                            raw_resolution = next(
                                (
                                    (
                                        getattr(known_evidence_rows.get(cid), "attributes", {})
                                        or {}
                                    ).get("code_fact_resolution")
                                    for cid in typed_cited
                                    if isinstance(
                                        (
                                            getattr(known_evidence_rows.get(cid), "attributes", {})
                                            or {}
                                        ),
                                        dict,
                                    )
                                    and (
                                        getattr(known_evidence_rows.get(cid), "attributes", {})
                                        or {}
                                    ).get("code_fact_resolution")
                                    is not None
                                ),
                                None,
                            )
                            if raw_resolution is not None:
                                try:
                                    from investigation_agent_platform.domain.code.models import (
                                        CodeFactResolution,
                                        CodeFactStatus,
                                    )

                                    resolution = CodeFactResolution.model_validate(raw_resolution)
                                    if resolution.status == CodeFactStatus.RESOLVED:
                                        typed_value = (
                                            typed.get("claimed_value")
                                            or (raw_target or {}).get("claimed_value")
                                            if isinstance(raw_target, dict)
                                            else typed.get("claimed_value")
                                        )
                                        typed_semantics = (
                                            typed.get("value_semantics")
                                            or (raw_target or {}).get("value_semantics")
                                            if isinstance(raw_target, dict)
                                            else typed.get("value_semantics")
                                        )
                                        if (
                                            typed_value is not None
                                            and str(typed_value) == str(resolution.resolved_value)
                                            and str(typed_semantics or "").upper()
                                            == resolution.value_semantics.value
                                        ):
                                            claim_cited.extend(
                                                cid for cid in typed_cited if cid not in claim_cited
                                            )
                                except Exception:
                                    pass
                    except Exception:
                        # Malformed typing never blocks persistence; the
                        # claim stays untyped and verifies by inference.
                        continue
                try:
                    for claim_key, typed in typed_list:
                        target_data = typed.get("target")
                        target_data = target_data if isinstance(target_data, dict) else {}
                        edge_target = (
                            ClaimTarget.model_validate(target_data) if target_data else None
                        )
                        kind = ClaimKind(str(typed.get("claim_kind") or "CAUSAL"))
                        edge_targets = [edge_target]
                        if kind == ClaimKind.CALL_EDGE and verifier_version == VERIFIER_VERSION_CURRENT:
                            edge_targets = VerificationEngine._aggregate_call_edge_targets(edge_target)
                            if not edge_targets:
                                edge_targets = [edge_target]
                        if kind == ClaimKind.CALL_EDGE and verifier_version == VERIFIER_VERSION_CURRENT:
                            for target in edge_targets:
                                caller_obligations.append(
                                    (target, typed, claim_binding_ids.get(claim_key, []))
                                )
                            continue
                        fact_category = typed.get("fact_category") or (
                            edge_target.fact_category.value
                            if edge_target is not None and edge_target.fact_category is not None
                            else None
                        )
                        # Phase 2 (claim binding): stored retrieval hint rides
                        # every assessment of this claim variant.
                        stored_query, stored_scope = _stored_claim_query(typed)
                        bound_ids = claim_binding_ids.get(claim_key, [])
                        if not bound_ids:
                            # No usable cited evidence (all malformed or
                            # unbanked) — persist the claim anyway so its
                            # query survives to the ledger and the directive.
                            # Mirrors CALL_EDGE_UNRESOLVED for all kinds.
                            assessments.append(
                                HypothesisEvidenceAssessment(
                                    hypothesis_id=hypothesis_id,
                                    evidence_id=None,
                                    assessment=AssessmentType.SUPPORTS,
                                    strength=AssessmentStrength.MODERATE,
                                    reason="claim unresolved: no usable evidence",
                                    claim_kind=kind,
                                    claim_target=edge_target,
                                    commit_id=claim_key[3] or None,
                                    search_query=stored_query,
                                    search_scope=stored_scope,
                                    reason_code="CLAIM_UNRESOLVED",
                                    schema_version="v2",
                                )
                            )
                        for evidence_id in bound_ids:
                            assessment = HypothesisEvidenceAssessment(
                                hypothesis_id=hypothesis_id,
                                evidence_id=evidence_id,
                                assessment=AssessmentType.SUPPORTS,
                                strength=AssessmentStrength.MODERATE,
                                reason=f"cited by {kind.value} claim",
                                claim_kind=kind,
                                claim_target=edge_target,
                                commit_id=claim_key[3] or None,
                                search_query=stored_query,
                                search_scope=stored_scope,
                                fact_category=fact_category,
                                value_semantics=(
                                    str(
                                        typed.get("value_semantics")
                                        or target_data.get("value_semantics", "")
                                    )[:64]
                                    or None
                                ),
                                claimed_value=(
                                    str(
                                        typed.get("claimed_value")
                                        or target_data.get("claimed_value", "")
                                    )[:512]
                                    or None
                                ),
                            )
                            assessments.append(assessment)
                    if verifier_version == VERIFIER_VERSION_CURRENT and caller_obligations:
                        required_verification: list[str] = []
                        for edge_target, typed, edge_ids in caller_obligations:
                            edge_key = VerificationEngine.caller_edge_key(edge_target)
                            required_verification.append(f"CALL_EDGE|{edge_key}")
                            matching_ids = list(edge_ids)
                            if edge_target.file_path:
                                matching_ids = [
                                    evidence_id
                                    for evidence_id in matching_ids
                                    if VerificationEngine._paths_match(
                                        str(edge_target.file_path),
                                        VerificationEngine._item_file(
                                            known_evidence_rows.get(evidence_id)
                                        ),
                                    )
                                ]
                            # Phase 2 (claim binding): caller obligations carry
                            # the stored pair of their source variant.
                            edge_query, edge_scope = _stored_claim_query(typed)
                            if not matching_ids:
                                assessments.append(
                                    HypothesisEvidenceAssessment(
                                        hypothesis_id=hypothesis_id,
                                        evidence_id=None,
                                        assessment=AssessmentType.SUPPORTS,
                                        strength=AssessmentStrength.MODERATE,
                                        reason="caller edge obligation unresolved",
                                        claim_kind=ClaimKind.CALL_EDGE,
                                        claim_target=edge_target,
                                        search_query=edge_query,
                                        search_scope=edge_scope,
                                        reason_code="CALL_EDGE_UNRESOLVED",
                                        schema_version="v2",
                                    )
                                )
                                continue
                            for evidence_id in matching_ids:
                                assessments.append(
                                    HypothesisEvidenceAssessment(
                                        hypothesis_id=hypothesis_id,
                                        evidence_id=evidence_id,
                                        assessment=AssessmentType.SUPPORTS,
                                        strength=AssessmentStrength.MODERATE,
                                        reason="cited by CALL_EDGE obligation",
                                        claim_kind=ClaimKind.CALL_EDGE,
                                        claim_target=edge_target,
                                        search_query=edge_query,
                                        search_scope=edge_scope,
                                        reason_code="CALL_EDGE_BOUND",
                                        schema_version="v2",
                                    )
                                )
                        required_verification = list(dict.fromkeys(required_verification))
                    else:
                        required_verification = []
                    await hypothesis_repo.save(
                        tenant_id,
                        Hypothesis(
                            id=hypothesis_id,
                            tenant_id=tenant_id,
                            investigation_id=investigation_id,
                            title=statement[:256],
                            description=statement[:4096],
                            statement=statement,
                            supporting_evidence_ids=claim_cited,
                            assessments=assessments,
                            required_verification=required_verification,
                        ),
                        investigation_id,
                        0,
                    )
                    saved += 1
                    # Debug story: hypothesis persistence. Shows what was
                    # formulated from which cited evidence and which
                    # assessments (BOUND vs UNRESOLVED) resulted.
                    _story_debug(
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="story.hypothesis_saved",
                        hypothesis_id=str(hypothesis_id),
                        statement=str(statement)[:500],
                        supporting_evidence_ids=[str(e) for e in (claim_cited or [])],
                        assessment_codes=[str(getattr(a, "reason_code", "?") or "?") for a in (assessments or [])],
                        assessment_kinds=[str(getattr(getattr(a, "claim_kind", None), "value", getattr(a, "claim_kind", "?")) or "?") for a in (assessments or [])],
                        required_verification=list(required_verification or []),
                    )
                except Exception as exc:
                    story_log(
                        logger,
                        logging.ERROR,
                        "hypothesis persistence failed",
                        comp=COMP_ACTVTY,
                        func="execute_action_activity",
                        stage="execute",
                        step="formulate",
                        outcome=OUTCOME_FAILED,
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="hypothesis persistence failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                        exc_info=True,
                    )
            # P0-3: VERIFY_HYPOTHESIS may carry validated causal steps for one
            # hypothesis. Steps whose evidence does not resolve to this
            # investigation are dropped; the rest attach to the hypothesis so
            # verification can require a real causal chain.
            return saved

        async def _handle_internal_reasoning_action() -> int:
            # VERIFY_HYPOTHESIS / CONCLUDE never touch an external evidence
            # provider — they operate purely on already persisted domain
            # state, so there is nothing to dispatch here.
            saved = await _persist_proposed_hypotheses()
            # P0-3: VERIFY_HYPOTHESIS may carry validated causal steps for one
            # hypothesis. Steps whose evidence does not resolve to this
            # investigation are dropped; the rest attach to the hypothesis so
            # verification can require a real causal chain.
            if resolved_action_type == ActionType.VERIFY_HYPOTHESIS and hypothesis_repo is not None:
                try:
                    from investigation_agent_platform.domain.hypothesis.models import (
                        HypothesisCausalStep,
                        HypothesisStatus,
                    )

                    target_raw = parameters.get("hypothesis_id")
                    raw_steps = parameters.get("causal_steps", []) or []
                    if target_raw and isinstance(raw_steps, list) and raw_steps:
                        target_id = UUID(str(target_raw))
                        rows = (
                            await evidence_repo.find_by_investigation_id(
                                tenant_id, investigation_id
                            )
                            if evidence_repo is not None
                            else []
                        )
                        known_ids = {getattr(e, "evidence_id", None) for e in (rows or [])}
                        steps: list[HypothesisCausalStep] = []
                        for raw in raw_steps[:10]:
                            if not isinstance(raw, dict):
                                continue
                            eids: list[UUID] = []
                            for r in (raw.get("evidence_ids", []) or [])[:20]:
                                try:
                                    cid = UUID(str(r))
                                except (ValueError, TypeError, AttributeError):
                                    continue
                                if cid in known_ids:
                                    eids.append(cid)
                            if not eids:
                                continue
                            steps.append(
                                HypothesisCausalStep(
                                    cause=str(raw.get("cause", ""))[:1024],
                                    mechanism=str(raw.get("mechanism", ""))[:2048],
                                    effect=str(raw.get("effect", ""))[:1024],
                                    evidence_ids=eids,
                                )
                            )
                        if steps:
                            target = await hypothesis_repo.get_by_id(tenant_id, target_id)
                            if target is not None and str(
                                getattr(target, "investigation_id", "")
                            ) == str(investigation_id):
                                updated = target.model_copy(update={"causal_steps": steps})
                                if updated.status == HypothesisStatus.PROPOSED:
                                    updated = updated.transition_status(
                                        HypothesisStatus.UNDER_INVESTIGATION
                                    )
                                await hypothesis_repo.save(
                                    tenant_id, updated, investigation_id, target.version
                                )
                except Exception as exc:
                    story_log(
                        logger,
                        logging.WARNING,
                        "causal step attachment failed; continuing",
                        comp=COMP_ACTVTY,
                        func="execute_action_activity",
                        stage="execute",
                        step="formulate",
                        outcome=OUTCOME_SKIPPED,
                        tenant_id=tenant_id,
                        investigation_id=inv_id_str,
                        event="causal step attachment failed",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
            return saved

        _HANDLERS: dict[Any, Any] = {
            ActionType.QUERY_STATE: _handle_query_state,
            ActionType.SEARCH_LOGS: _handle_search_logs,
            ActionType.GET_CODE: _handle_get_code,
            ActionType.SEARCH_CODE: _handle_search_code,
            ActionType.FIND_SYMBOL: _handle_find_symbol,
            ActionType.WALK_STACK: _handle_walk_stack,
            ActionType.TRACE_LOGIC: _handle_trace_logic,
            ActionType.FIND_CALL_GRAPH: _handle_find_call_graph,
            ActionType.GET_CODE_HISTORY: _handle_get_code_history,
            ActionType.PROBE_API: _handle_probe_api,
            ActionType.CORRELATE: _handle_correlate,
            ActionType.TRACE_JOURNEY: _handle_trace_journey,
            ActionType.WALK_SESSION_GRAPH: _handle_walk_session_graph,
            ActionType.SEARCH_REF_DOCS: _handle_search_ref_docs,
            ActionType.FORMULATE_HYPOTHESIS: _handle_internal_reasoning_action,
            ActionType.VERIFY_HYPOTHESIS: _handle_internal_reasoning_action,
            ActionType.CONCLUDE: _handle_internal_reasoning_action,
        }

        handler = _HANDLERS.get(resolved_action_type)
        if handler is None:
            # Exhaustive registry: this should be unreachable given the
            # ActionType(action_type) resolution above, but fail closed if a
            # new enum member is ever added without a handler.
            raise ApplicationFailure(
                f"No execution handler registered for action type: {resolved_action_type.value}",
                type="UnhandledActionTypeError",
                non_retryable=True,
            )

        if resolved_action_type in _ACTION_REQUIRED_EVIDENCE_TYPE_LOCAL and gateway is None:
            raise ApplicationFailure(
                "Evidence gateway is not configured; cannot execute evidence-producing action",
                type="EVIDENCE_PROVIDER_UNAVAILABLE",
                non_retryable=True,
            )

        try:
            import time as _time

            from investigation_agent_platform.domain.investigation.models import (
                ActionOutcome,
                ActionOutcomeStatus,
            )

            _dispatch_start = _time.monotonic()
            items_count = await handler()
            _dispatch_ms = int((_time.monotonic() - _dispatch_start) * 1000)
            # P0-2: typed outcome from the handler side-channel. DEGRADED
            # survives even with zero items; otherwise zero items => EMPTY.
            _outcome_status = outcome_detail.get("status") or (
                "SUCCEEDED" if items_count > 0 else "EMPTY"
            )
            # Lost-hypothesis fix: reason-attached hypotheses persist for
            # EVERY action type, not just FORMULATE_HYPOTHESIS. The internal
            # reasoning actions already persisted in-handler; the helper
            # dedupes against existing rows, so gating them out here only
            # avoids a redundant repo read.
            if resolved_action_type not in (
                ActionType.FORMULATE_HYPOTHESIS,
                ActionType.VERIFY_HYPOTHESIS,
                ActionType.CONCLUDE,
            ):
                await _persist_proposed_hypotheses()
            # WP-A3: retrieval attempt ledger (diagnostics-only,
            # fail-open). Records the query/attempt with a distinct
            # outcome plus novelty/lift against earlier attempts in
            # this investigation. Never blocks or alters the outcome.
            # G3: provider_total_count arrives via outcome_detail when a
            # handler surfaces the gateway total (None preserves legacy
            # behavior); banked_count is the handler's banked-row count,
            # not the 25-capped evidence_ids list. The attempt id tags
            # the workflow history entry; the RETRIEVAL_ATTEMPT timeline
            # marker below is skipped until Step 4 adds the enum member.
            _attempt_id: str | None = None
            _provider_total_count: int | None = None
            _resolved_action_name = str(
                getattr(resolved_action_type, "value", resolved_action_type)
            )
            try:
                from investigation_agent_platform.application.evidence.retrieval_ledger import (
                    attempt_id_for as _attempt_id_for,
                )
                from investigation_agent_platform.application.evidence.retrieval_ledger import (
                    extract_search_query as _extract_search_query,
                )
                from investigation_agent_platform.application.evidence.retrieval_ledger import (
                    record_retrieval_attempt as _record_retrieval_attempt,
                )

                try:
                    _provider_total_raw = outcome_detail.get("provider_total_count")
                    _provider_total_count = (
                        int(_provider_total_raw)
                        if _provider_total_raw is not None
                        else None
                    )
                except (TypeError, ValueError):
                    _provider_total_count = None
                _retrieval_attempt = _record_retrieval_attempt(
                    investigation_id=inv_id_str,
                    action_type=_resolved_action_name,
                    parameters=parameters if isinstance(parameters, dict) else {},
                    status=str(_outcome_status),
                    miss_reason=outcome_detail.get("miss_reason"),
                    row_ids=[
                        str(e) for e in (outcome_detail.get("evidence_ids") or [])
                    ],
                    rows_returned=int(items_count or 0),
                    truncated_rows=int(outcome_detail.get("truncated_rows") or 0),
                    source_statuses=[
                        str(s.get("status"))
                        for s in (outcome_detail.get("sources") or [])
                        if isinstance(s, dict) and s.get("status") is not None
                    ],
                    provider_total_count=_provider_total_count,
                    banked_count=int(items_count or 0),
                )
                if _retrieval_attempt is not None:
                    outcome_detail["retrieval_attempt"] = (
                        _retrieval_attempt.model_dump(mode="json")
                    )
                try:
                    _exec_iteration = data.get("iteration")
                    if _exec_iteration is not None:
                        _attempt_query, _attempt_scope, _attempt_provider = (
                            _extract_search_query(
                                _resolved_action_name,
                                parameters if isinstance(parameters, dict) else {},
                            )
                        )
                        _attempt_id = _attempt_id_for(
                            inv_id_str,
                            int(_exec_iteration),
                            _resolved_action_name,
                            _attempt_query,
                            _attempt_scope,
                        )
                except (TypeError, ValueError):
                    _attempt_id = None
                if _attempt_id is not None:
                    outcome_detail["attempt_id"] = _attempt_id
            except Exception:
                pass
            # G3: RETRIEVAL_ATTEMPT timeline marker (diagnostics-only,
            # fail-open — marker faults never touch the outcome above).
            try:
                from investigation_agent_platform.domain.timeline.models import (
                    TimelineEvent,
                    TimelineEventType,
                )

                _retrieval_event_type = getattr(
                    TimelineEventType, "RETRIEVAL_ATTEMPT", None
                )
                _timeline_repo = getattr(ctx, "timeline_repo", None)
                if (
                    _retrieval_event_type is not None
                    and _timeline_repo is not None
                ):
                    # No sequence-number read: TimelineEventORM persists
                    # no sequence column (ordering is by timestamp), so a
                    # full-history read here would buy nothing.
                    _marker_evidence_ids = [
                        str(e) for e in (outcome_detail.get("evidence_ids") or [])
                    ][:25]
                    await _timeline_repo.append(
                        tenant_id,
                        TimelineEvent(
                            tenant_id=tenant_id,
                            investigation_id=investigation_id,
                            timestamp=datetime.now(UTC),
                            event_type=_retrieval_event_type,
                            description=(
                                f"{_resolved_action_name} attempt "
                                f"{_attempt_id or 'unindexed'}: "
                                f"{_outcome_status} "
                                f"({int(items_count or 0)} banked)"
                            )[:2048],
                            attributes={
                                "action_type": _resolved_action_name,
                                "status": str(_outcome_status),
                                "attempt_id": _attempt_id,
                                # G3: per-action coverage join key — the
                                # action_executions.action_id for this
                                # dispatch (same UUID recorded by
                                # action_service.execute). Lets the battery
                                # pair each eligible retrieval action with
                                # its recorded attempt. None-safe: legacy
                                # rows simply carry no key and count as
                                # missing, never as covered.
                                "action_execution_id": str(inv_action.action_id),
                                "provider_total_count": _provider_total_count,
                                "banked_count": int(items_count or 0),
                                "truncated_rows": int(
                                    outcome_detail.get("truncated_rows") or 0
                                ),
                                "miss_reason": outcome_detail.get("miss_reason"),
                                "evidence_ids": _marker_evidence_ids,
                                "attempt": outcome_detail.get("retrieval_attempt"),
                            },
                        ),
                        investigation_id,
                    )
            except Exception:
                pass
            action_outcome = ActionOutcome(
                action_id=inv_action.action_id,
                action_type=resolved_action_type,
                status=ActionOutcomeStatus(_outcome_status),
                evidence_ids=list(outcome_detail.get("evidence_ids") or [])[:25],
                item_count=items_count,
                error_code=outcome_detail.get("error_code"),
                miss_reason=outcome_detail.get("miss_reason"),
                duration_ms=_dispatch_ms,
                retrieval_attempt=(
                    outcome_detail.get("retrieval_attempt")
                    if isinstance(outcome_detail.get("retrieval_attempt"), dict)
                    else None
                ),
                fuzzy_orientation=(
                    outcome_detail.get("fuzzy_orientation")
                    if isinstance(outcome_detail.get("fuzzy_orientation"), dict)
                    else None
                ),
                sources=[
                    {k: str(v) for k, v in s.items()}
                    for s in (outcome_detail.get("sources") or [])
                    if isinstance(s, dict)
                ][:10],
            )
            reconciled_caller_edges = 0
            if (
                resolved_action_type == ActionType.GET_CODE
                and parameters.get("caller_edge_key")
                and hypothesis_repo is not None
                and evidence_repo is not None
                and outcome_detail.get("evidence_ids")
            ):
                from investigation_agent_platform.domain.common.exceptions import (
                    ConcurrencyError,
                )
                from investigation_agent_platform.domain.hypothesis.models import (
                    ClaimKind,
                    ClaimTarget,
                )

                try:
                    start_line = int(parameters.get("start_line") or 0) or None
                    end_line = int(parameters.get("end_line") or 0) or None
                except (TypeError, ValueError):
                    start_line, end_line = None, None
                revision = str(parameters.get("revision") or "").strip()
                caller_target = ClaimTarget(
                    symbol=str(parameters.get("caller_edge_symbol") or "").strip() or None,
                    counterpart=str(
                        parameters.get("caller_edge_counterpart") or ""
                    ).strip()
                    or None,
                    file_path=str(parameters.get("file_path") or "").strip() or None,
                    line_start=start_line,
                    line_end=end_line,
                    scope=str(parameters.get("caller_edge_scope") or "").strip() or None,
                    commit_id=revision or None,
                )
                caller_edge_key = VerificationEngine.caller_edge_key(caller_target)

                for attempt in range(2):
                    if not revision or not start_line or not end_line:
                        break
                    try:
                        hypotheses = await hypothesis_repo.find_by_investigation_id(
                            tenant_id, investigation_id
                        )
                        evidence_rows = await evidence_repo.find_by_investigation_id(
                            tenant_id, investigation_id
                        )
                        candidate_ids = {
                            str(value) for value in (outcome_detail.get("evidence_ids") or [])
                        }
                        candidates = [
                            row
                            for row in (evidence_rows or [])
                            if str(getattr(row, "evidence_id", "") or "") in candidate_ids
                        ]
                        eligible = VerificationEngine.caller_edge_binding_rows(
                            candidates,
                            caller_target,
                            candidates,
                        )
                        eligible_ids = {getattr(row, "evidence_id", None) for row in eligible}
                        if not eligible_ids:
                            break
                        for hypothesis in hypotheses or []:
                            unresolved = [
                                assessment
                                for assessment in (getattr(hypothesis, "assessments", []) or [])
                                if assessment.claim_kind == ClaimKind.CALL_EDGE
                                and assessment.reason_code == "CALL_EDGE_UNRESOLVED"
                                and assessment.claim_target is not None
                                and VerificationEngine.caller_edge_key(
                                    assessment.claim_target
                                )
                                == caller_edge_key
                            ]
                            if not unresolved:
                                continue
                            updated_assessments = list(hypothesis.assessments)
                            supporting_ids = list(hypothesis.supporting_evidence_ids)
                            changed = False
                            for assessment in unresolved:
                                replacement = assessment.model_copy(
                                    update={
                                        "evidence_id": next(iter(eligible_ids)),
                                        "reason": "exact forced caller-edge range read",
                                        "reason_code": "CALL_EDGE_BOUND",
                                    }
                                )
                                index = updated_assessments.index(assessment)
                                updated_assessments[index] = replacement
                                if replacement.evidence_id not in supporting_ids:
                                    supporting_ids.append(replacement.evidence_id)
                                changed = True
                            if changed:
                                updated = hypothesis.model_copy(
                                    update={
                                        "assessments": updated_assessments,
                                        "supporting_evidence_ids": supporting_ids[:200],
                                    }
                                )
                                await hypothesis_repo.save(
                                    tenant_id,
                                    updated,
                                    investigation_id,
                                    hypothesis.version,
                                )
                                reconciled_caller_edges += len(unresolved)
                        break
                    except ConcurrencyError:
                        if attempt == 1:
                            raise
                        continue
                outcome_detail["caller_edges_reconciled"] = reconciled_caller_edges
            # Phase 4.2: proof-chain counter (additive, fail-open). The
            # miss_reason breakdown is what lets dashboards separate
            # provider failure from no-match.
            _emit_telemetry(
                ctx,
                tenant_id,
                investigation_id,
                "record_code_action",
                str(getattr(resolved_action_type, "value", resolved_action_type)),
                _outcome_status,
                str(outcome_detail.get("miss_reason") or "none"),
            )
            # Operational telemetry: action type + result counts only (values
            # ride as extra keys per TO_DO-007/019; lazy %-style message).
            story_log(
                logger,
                logging.WARNING,
                "execute_action_activity dispatched action=%s items=%s hypotheses=%s",
                resolved_action_type.value,
                items_count,
                len(proposed_hypotheses),
                comp=COMP_ACTVTY,
                func="execute_action_activity",
                stage="execute",
                step="dispatch",
                outcome=OUTCOME_COMPLETED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="execute_action_activity dispatched",
                outputs={
                    "action": resolved_action_type.value,
                    "items_count": items_count,
                },
                action=resolved_action_type.value,
                items_count=items_count,
                hypotheses_count=len(proposed_hypotheses),
            )
        except ApplicationFailure:
            raise
        except Exception as exc:
            story_log(
                logger,
                logging.ERROR,
                "execute_action_activity gateway dispatch failed",
                comp=COMP_ACTVTY,
                func="execute_action_activity",
                stage="execute",
                step="dispatch",
                outcome=OUTCOME_FAILED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="execute_action_activity gateway dispatch failed",
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
                action_type=action_type,
            )
            raise _application_failure_from_exc(exc) from exc

        # Audit action execution (always — the authorized action must always
        # be recorded, whether or not a gateway is wired).
        try:
            await action_service.execute(
                tenant_id, investigation_id, inv_action, principal_id=principal_id
            )
        except Exception as exc:
            story_log(
                logger,
                logging.ERROR,
                "execute_action_activity record_action failed",
                comp=COMP_ACTVTY,
                func="execute_action_activity",
                stage="execute",
                step="record_action",
                outcome=OUTCOME_FAILED,
                tenant_id=tenant_id,
                investigation_id=inv_id_str,
                event="execute_action_activity record_action failed",
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )
            raise _application_failure_from_exc(exc) from exc

        if _idem_active_key is not None:
            await _complete_reservation(
                ctx=ctx,
                tenant_id=tenant_id,
                key=_idem_active_key,
                operation=_idem_operation,
                response={
                    "success": True,
                    "data": {
                        "status": result_status,
                        "items_count": items_count,
                        "attempt_id": _attempt_id,
                        "code_fact_resolution": outcome_detail.get("code_fact_resolution"),
                        # Phase 3.2: the stored response must equal the live
                        # return below — a replayed action without the outcome
                        # would silently change the next-turn history.
                        "action_outcome": action_outcome.model_dump(mode="json"),
                    },
                },
                activity="execute_action_activity",
                investigation_id=inv_id_str,
            )
        story_log(
            logger,
            logging.INFO,
            "execute_action_activity completed",
            comp=COMP_ACTVTY,
            func="execute_action_activity",
            stage="execute",
            step="complete",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            investigation_id=inv_id_str,
            event="execute_action_activity completed",
            outputs={
                "outcome": "executed",
                "action": resolved_action_type.value,
                "items_count": items_count,
            },
            elapsed_ms=elapsed_ms(t0),
            action=resolved_action_type.value,
            items_count=items_count,
        )
        # Debug story: full evidence-out payload per action.
        _story_debug(
            tenant_id=tenant_id,
            investigation_id=inv_id_str,
            event="story.execute_out",
            action=resolved_action_type.value,
            result_status=result_status,
            items_count=items_count,
            action_outcome=action_outcome.model_dump(mode="json"),
            caller_edges_reconciled=outcome_detail.get("caller_edges_reconciled", 0),
        )
        return GenericActivityResult(
            success=True,
            data={
                "status": result_status,
                "items_count": items_count,
                "attempt_id": _attempt_id,
                "code_fact_resolution": outcome_detail.get("code_fact_resolution"),
                "caller_edges_reconciled": outcome_detail.get("caller_edges_reconciled", 0),
                "action_outcome": action_outcome.model_dump(mode="json"),
            },
        )
    except ApplicationFailure:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="execute_action_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        raise
    except Exception as exc:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="execute_action_activity",
            investigation_id=str(locals().get("inv_id_str", "") or ""),
        )
        story_log(
            logger,
            logging.ERROR,
            "execute_action_activity failed",
            comp=COMP_ACTVTY,
            func="execute_action_activity",
            stage="execute",
            step="execute",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            investigation_id=inv_id_probe,
            event="execute_action_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise _application_failure_from_exc(exc) from exc
