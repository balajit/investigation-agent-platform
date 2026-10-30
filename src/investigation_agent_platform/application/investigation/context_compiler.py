# src/investigation_agent_platform/application/investigation/context_compiler.py
"""Reasoning context compiler (P0-1).

Builds the model-visible context as a named, budgeted projection over durable
investigation state — replacing ad hoc first-N assembly hidden inside
``ReasoningCoordinator.reason``. Every section is independently capped so the
compiled context stays within model input budgets; full artifacts remain in
Postgres and are referenced by evidence ID.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

from investigation_agent_platform.domain.hypothesis.templates import (
    TEMPLATES_VERSION_CURRENT,
    classify_banked_sites,
    instantiate_templates,
)
from investigation_agent_platform.domain.investigation.models import anchors_from_metadata
from investigation_agent_platform.infrastructure.observability.tracing import traced

# Versioned prompt/context contract. Bumped whenever the compiled section
# shape changes; persisted per decision for replay/evaluation.
# v3-claim-grounded-019: adds the S2 dormant_templates section and its
# deterministic budget trimming contract.
REASONER_PROMPT_VERSION = "v3-claim-grounded-019"
CODE_PACKET_VERSION = "code-packet-v1"

MAX_CODE_EXCERPTS = 8
# A code packet carries the requested range, not a 1,500-char head. The
# provider already caps content at 4,000 chars; keeping the same cap here
# means a range read is not silently cut before the asserted line. Truncated
# packets disclose the cut and carry next_window so the model can continue.
MAX_CODE_EXCERPT_CHARS = 4000
MAX_EVIDENCE_POOL = 500
MAX_VISIBLE_EXCERPTS = 100
MAX_HYPOTHESES = 10
MAX_TIMELINE_EVENTS = 8
MAX_PENDING_QUESTIONS = 10
MAX_FINDINGS = 5
# S2 dormant templates (chunk 2): hard cap on rendered templates; the
# budget enforcer may trim further from the end with a dropped count.
MAX_DORMANT_TEMPLATES = 8


# Context-window shrink: global token ceiling for the compiled context so
# older content purges as newer arrives (counts/chars caps alone are blind
# to the model window). Priority order is fixed — pinned sections (problem,
# plan, active hypotheses + bound Tier-1 rows) are NEVER purged; everything
# else sheds oldest-first with a compaction note recording what left.
# Estimator is chars/4 (model-agnostic, dependency-free); conservative by
# design. Default 12000 preserves current behavior; tighten per model via
# IAP_COMPILER_MAX_TOKENS.
def context_token_budget() -> int:
    """Global compiled-context token ceiling (default 12000)."""
    return _env_int("IAP_COMPILER_MAX_TOKENS", 12000)


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (chars/4, rounded up)."""
    try:
        length = len(text or "")
    except Exception:
        return 0
    return (length + 3) // 4 if length else 0


# R4 relevance-tiered excerpts: Tier 1 carries full snippets, Tier 2 carries
# summary + provenance + a by-ID retrieval marker. Tiers subdivide the
# existing caps, never exceed them. Bound-evidence IDs (supporting/refuting
# any active hypothesis) are always Tier 1 — the verifier's citations must
# never be summarized away.
_TIER2_MARKER = "[full snippet available by evidence_id]"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def tier1_recent_count() -> int:
    """R4: most-recently-retrieved items kept at full snippets (default 5)."""
    return _env_int("IAP_COMPILER_FULL_EXCERPT_RECENT", 5)


def tier1_relevance_floor() -> float:
    """R4: relevance at/above which excerpts keep full snippets (default 0.8)."""
    return _env_float("IAP_COMPILER_FULL_EXCERPT_RELEVANCE", 0.8)


# P2-1: deterministic issue-surface classifier. Guidance only — the plan
# tells the model which surfaces this complaint shape implies; it is never
# used as proof at the gate (see D5: reading unrelated code must not verify).
_SURFACE_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("api", ("endpoint", "api", "response", "status code", "returns", "http", "route")),
    ("code", ("function", "class", "exception", "traceback", "error", "bug", "returns")),
    ("state", ("row", "count", "table", "query", "database", "missing data")),
    ("log", ("slow", "latency", "timeout", "spike", "rate", "log")),
)


@traced("reason", require_scope=False)
def classify_surfaces(problem_description: str) -> dict[str, list[str]]:
    """Keyword-signal surface classification with open questions seeded."""
    text = (problem_description or "").lower()
    required = sorted(
        {surface for surface, signals in _SURFACE_SIGNALS if any(s in text for s in signals)}
    )
    open_questions = [
        f"Which evidence surface will discriminate this complaint? Suggested: {', '.join(required) or 'log'}."
    ]
    # O5 (TO_DO_LLM_List): "not returning / missing / empty" complaints need
    # an existence-check first — verify X exists in state at all before
    # theorizing about read-path filtering.
    if any(
        signal in text
        for signal in (
            "not returning",
            "not returned",
            "returns nothing",
            "returns no ",
            "no results",
            "empty response",
            "empty list",
            "missing",
            "not showing",
            "does not show",
            "doesn't show",
            "zero rows",
            "0 rows",
        )
    ):
        if "state" not in required:
            required.append("state")
            required.sort()
        open_questions.insert(
            0,
            "Does the expected item exist in state at all? Existence-check via QUERY_STATE "
            "before theorizing about read-path filtering.",
        )
    return {
        "required_surfaces": required,
        "open_questions": open_questions,
        "completed_checks": [],
    }


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default) if hasattr(obj, name) else default


def _most_recent_ids(items: list[Any], count: int) -> set[str]:
    """IDs of the `count` most recently retrieved items (by `retrieved_at`).

    Items without a parseable timestamp sort last — recency unknown means no
    Tier-1 boost; relevance/bound rules still apply. Non-positive count
    disables the recency rule.
    """
    if count <= 0:
        return set()

    def _retrieved_key(e: Any) -> str:
        return str(_get(e, "retrieved_at", "") or "")

    ranked = sorted(items, key=_retrieved_key, reverse=True)
    return {
        str(_get(e, "evidence_id", ""))
        for e in ranked[: max(0, count)]
        if _get(e, "evidence_id", "")
    }


def _bound_evidence_ids(hypotheses: list[Any]) -> set[str]:
    """Supporting + refuting IDs across hypotheses — always Tier 1."""
    bound: set[str] = set()
    for h in hypotheses or []:
        for key in ("supporting_evidence_ids", "refuting_evidence_ids"):
            for i in _get(h, key, []) or []:
                text = str(i or "")
                if text:
                    bound.add(text)
    return bound


def _is_code_type(etype: str) -> bool:
    return "CODE" in etype.upper() or "EXCEPTION" in etype.upper()


def _read_mode(e: Any, snippet: str) -> str:
    attrs = _get(e, "attributes", {}) or {}
    if isinstance(attrs, dict):
        declared = str(attrs.get("read_mode", "") or "")
        if declared in ("range", "path_only"):
            return declared
        if bool(attrs.get("discovery_only")):
            return "path_only"
    # Legacy rows: numbered lines are the provider's range-read shape.
    first = snippet.splitlines()[0] if snippet else ""
    parts = first.split("|", 1)
    if len(parts) == 2 and parts[0].strip().isdigit():
        return "range"
    return "path_only"


def _last_visible_line(content: str) -> int | None:
    """Last numbered line in provider range content (``480| text``)."""
    for raw in reversed(content.splitlines()):
        head = raw.split("|", 1)[0].strip()
        if head.isdigit():
            return int(head)
    return None


def _next_window(
    content: str, truncated: bool, start_line: Any, end_line: Any
) -> dict[str, int] | None:
    """Continuation hint when a packet was cut, or None when complete."""
    if not truncated or not content:
        return None
    last = _last_visible_line(content)
    if last is None:
        try:
            last = int(start_line or 1) + content.count("\n")
        except (TypeError, ValueError):
            return None
    nxt_start = last + 1
    try:
        limit = int(end_line) if end_line else nxt_start + 199
    except (TypeError, ValueError):
        limit = nxt_start + 199
    if nxt_start > limit:
        return None
    return {"start_line": nxt_start, "end_line": min(limit, nxt_start + 199)}


def _symbol_spans(e: Any, start_line: Any) -> list[dict[str, Any]]:
    attrs = _get(e, "attributes", {}) or {}
    if not isinstance(attrs, dict):
        return []
    name = str(attrs.get("symbol_name", "") or "").strip()
    if not name:
        return []
    try:
        line = int(start_line) if start_line else None
    except (TypeError, ValueError):
        line = None
    return [
        {
            "name": name[:256],
            "kind": str(attrs.get("symbol_kind", "") or "symbol")[:64],
            "line": line,
        }
    ]


def _code_packet(
    e: Any,
    *,
    evidence_id: str,
    snippet: str,
    content: str,
    content_truncated: bool,
    bound: bool,
    start_line: Any,
    end_line: Any,
) -> dict[str, Any]:
    """Range/target/continuation fields for a code excerpt (Phase 1.1).

    The packet is deliberately additive: existing excerpt keys stay in place
    so older readers keep working, while the prompt can now show the model
    exactly which window it received and how to continue.
    """
    prov = _get(e, "provenance", None)
    location = _get(prov, "source_location", None)
    file_path = str(_get(location, "file_path", "") or "")
    revision = str(_get(location, "revision", "") or "")
    attrs = _get(e, "attributes", {}) or {}
    mode = _read_mode(e, snippet)
    discovery = bool(
        (isinstance(attrs, dict) and attrs.get("discovery_only")) or mode == "path_only"
    )
    next_window = _next_window(content, content_truncated, start_line, end_line)
    if next_window is None and isinstance(attrs, dict) and attrs.get("truncated"):
        # Provider cut the requested window to fit its snippet budget:
        # continue where the snippet stopped, bounded by what was asked.
        try:
            provider_start = int(attrs.get("next_start_line") or 0)
        except (TypeError, ValueError):
            provider_start = 0
        if provider_start > 0:
            try:
                provider_limit = int(attrs.get("requested_end_line") or 0) or None
            except (TypeError, ValueError):
                provider_limit = None
            window_end = provider_limit or (provider_start + 199)
            if provider_start <= window_end:
                next_window = {
                    "start_line": provider_start,
                    "end_line": min(window_end, provider_start + 199),
                }
    if bound:
        why = "hypothesis_citation"
    elif mode == "range":
        why = "range_read"
    elif discovery:
        why = "discovery"
    else:
        why = "recent_code"
    return {
        "packet_version": CODE_PACKET_VERSION,
        "citable_evidence_id": evidence_id,
        "target": {
            "file_path": file_path,
            "revision": revision,
            "symbol": str((attrs.get("symbol_name") if isinstance(attrs, dict) else "") or "")
            or None,
            "frame": None,
        },
        "window": {
            "start_line": start_line,
            "end_line": end_line,
            "why": why,
        },
        "read_mode": mode,
        "discovery_only": discovery,
        "symbol_spans": _symbol_spans(e, start_line),
        "code_fact_resolution": (
            attrs.get("code_fact_resolution")
            if isinstance(attrs, dict) and isinstance(attrs.get("code_fact_resolution"), dict)
            else None
        ),
        "code_fact_resolutions": (
            attrs.get("code_fact_resolutions")
            if isinstance(attrs, dict) and isinstance(attrs.get("code_fact_resolutions"), list)
            else []
        ),
        "next_window": next_window,
        "claim_priority": 0 if bound else (1 if mode == "range" else 2),
    }


def _sort_key(e: Any) -> tuple[str, str, str]:
    """Deterministic ordering even when the repository returns rows unordered."""
    return (
        str(_get(e, "retrieved_at", "") or ""),
        str(_get(e, "observed_at", "") or ""),
        str(_get(e, "evidence_id", "") or ""),
    )


def _code_sort_key(e: Any) -> tuple[int, str, str, str]:
    """Range reads before path-only heads, then newest-first, then ID."""
    mode = _read_mode(e, str(_get(e, "content_snippet", "") or ""))
    retrieved, observed, evidence_id = _sort_key(e)
    return (1 if mode == "range" else 0, retrieved, observed, evidence_id)


@traced("reason", require_scope=False)
def compile_evidence_excerpts(
    evidence_items: list[Any],
    bound_evidence_ids: set[str] | None = None,
    log_group: bool = True,
) -> list[dict[str, Any]]:
    """Bounded excerpts with provenance + R4 relevance tiers + log grouping.

    Tier 1 (full snippet): bound to an active hypothesis (supporting or
    refuting — never summarized away), relevance at/above the floor, or
    among the most recently retrieved. Tier 2: summary + provenance +
    relevance with a by-ID retrieval marker instead of the snippet body.
    Code bodies included (truncated with disclosure) — previously only
    summaries reached the model, so GET_CODE results were invisible to the
    next reasoning turn. Tiers subdivide the existing caps, never exceed them.

    Log grouping (`log_group`, default ON): LOG/RUNTIME_LOG/TRACE rows with
    identical masked shape collapse to one exemplar excerpt carrying
    `log_group={count, member_ids}` — member IDs verbatim so citations stay
    valid. Per-run `log_optimization: off` (or env kill-switch) restores
    per-row excerpts for the expensive diamond hunt.
    """
    bound = bound_evidence_ids or set()

    def _eid(e: Any) -> str:
        return str(_get(e, "evidence_id", "") or "")

    # Newest-first: bound rows lead, then code (range reads before heads),
    # then the rest — so caps shed the OLDEST rows first as new evidence
    # arrives. Code is promoted ahead of logs because a claim's decisive
    # range must not be crowded out by newer log rows (Phase 1.1). The 500
    # pool bounds the sort cost; the [:100] keeps the highest-priority 100.
    # Phase 4 (claim binding): fetch_reason rows (smallest-fetch banked,
    # uncited) sort into their own partition between bound and code, and
    # beyond-cut fetch rows are carried into the pool (bounded ≤10 by the
    # forcing caps). Hydration is unordered (no ORDER BY), so without the
    # carry any fetch row past the cut may shed regardless of age.
    def _has_fetch_reason(row: Any) -> bool:
        try:
            attrs = _get(row, "attributes", {}) or {}
            return isinstance(attrs, dict) and str(attrs.get("fetch_reason") or "").startswith(
                "smallest_fetch:"
            )
        except Exception:
            return False

    pool = list(evidence_items[:MAX_EVIDENCE_POOL])
    try:
        _seen_pool = {_eid(r) for r in pool}
        for r in evidence_items[MAX_EVIDENCE_POOL:]:
            try:
                if _has_fetch_reason(r):
                    _rid = _eid(r)
                    if _rid and _rid not in _seen_pool:
                        pool.append(r)
                        _seen_pool.add(_rid)
            except Exception:
                continue
    except Exception:
        pass
    bound_items = sorted([e for e in pool if _eid(e) in bound], key=_sort_key, reverse=True)
    fetch_items = sorted(
        [e for e in pool if _eid(e) not in bound and _has_fetch_reason(e)],
        key=_sort_key,
        reverse=True,
    )
    fetch_ids = {_eid(e) for e in fetch_items}
    code_items = sorted(
        [
            e
            for e in pool
            if _eid(e) not in bound
            and _eid(e) not in fetch_ids
            and _is_code_type(str(_get(_get(e, "evidence_type", ""), "value", e) or ""))
        ],
        key=_code_sort_key,
        reverse=True,
    )
    code_ids = {_eid(e) for e in code_items}
    rest = sorted(
        [
            e
            for e in pool
            if _eid(e) not in bound and _eid(e) not in fetch_ids and _eid(e) not in code_ids
        ],
        key=_sort_key,
        reverse=True,
    )
    items = (bound_items + fetch_items + code_items + rest)[:MAX_VISIBLE_EXCERPTS]
    _by_id = {_eid(_it): _it for _it in items}
    # Phase 4 code-body queue: bound bodies first, then fetch bodies, then
    # unbound code — a fetch body displaces unbound code, never a bound
    # body. Bounded by MAX_CODE_EXCERPTS like before.
    _body_ids: set[str] = set()
    try:
        _queue: list[str] = []
        for _e in bound_items + fetch_items + code_items:
            try:
                if _is_code_type(str(_get(_get(_e, "evidence_type", ""), "value", _e) or "")):
                    _iid = _eid(_e)
                    if _iid and _iid not in _queue:
                        _queue.append(_iid)
            except Exception:
                continue
        _body_ids = set(_queue[:MAX_CODE_EXCERPTS])
    except Exception:
        _body_ids = set()
    recent_ids = _most_recent_ids(items, tier1_recent_count())
    floor = tier1_relevance_floor()
    units = _group_log_items(items, log_group)
    excerpts: list[dict[str, Any]] = []
    for representative, member_ids in units:
        e = representative
        summary = str(_get(e, "summary", "") or "")
        snippet = str(_get(e, "content_snippet", "") or "")
        etype = str(_get(_get(e, "evidence_type", ""), "value", e) or "")
        evidence_id = str(_get(e, "evidence_id", ""))
        relevance = _group_relevance(e, member_ids, items)
        # Phase 4 (claim binding): fetch_reason rows stay Tier 1 until
        # cited (bound pin takes over) — checked on the representative
        # and on grouped members, so a grouped fetch row is not demoted.
        _fetch_pinned = _has_fetch_reason(e)
        if not _fetch_pinned:
            try:
                for _mid in member_ids or []:
                    _mrow = _by_id.get(str(_mid))
                    if _mrow is not None and _has_fetch_reason(_mrow):
                        _fetch_pinned = True
                        break
            except Exception:
                pass
        tier = 1
        if (
            evidence_id
            and evidence_id not in bound
            and not (set(member_ids) & bound)
            and relevance < floor
            and evidence_id not in recent_ids
            and not (set(member_ids) & recent_ids)
            and not _fetch_pinned
        ):
            tier = 2
        is_code = _is_code_type(etype)
        content = ""
        content_truncated = False
        if tier == 1 and is_code and snippet and evidence_id in _body_ids:
            if len(snippet) > MAX_CODE_EXCERPT_CHARS:
                content = snippet[:MAX_CODE_EXCERPT_CHARS]
                content_truncated = True
            else:
                content = snippet
            # The provider may have cut the requested window to fit its own
            # snippet budget (honest provenance + truncated attributes).
            # Surface that cut here so the packet never looks complete when
            # the requested range continues past the snippet.
            attrs = _get(e, "attributes", {}) or {}
            if isinstance(attrs, dict) and attrs.get("truncated"):
                content_truncated = True
        elif tier == 2 and snippet:
            content = _TIER2_MARKER
        prov = _get(e, "provenance", None)
        location = _get(prov, "source_location", None)
        start_line = _get(location, "line_start", None)
        end_line = _get(location, "line_end", None)
        excerpt: dict[str, Any] = {
            "evidence_id": evidence_id,
            "evidence_type": etype,
            "source": str(_get(e, "source", "") or ""),
            "summary": summary[:1000],
            "summary_truncated": len(summary) > 1000,
            "content": content,
            "content_truncated": content_truncated,
            "tier": tier,
            "log_group": (
                {"count": len(member_ids), "member_ids": member_ids}
                if len(member_ids) > 1
                else None
            ),
            "relevance": _get(e, "relevance", 1.0),
            "confidence": _get(e, "confidence", 1.0),
            "is_redacted": bool(_get(e, "is_redacted", False)),
            "observed_at": str(_get(e, "observed_at", "") or ""),
            "retrieved_at": str(_get(e, "retrieved_at", "") or ""),
            "provider": str(_get(e, "provider", "") or ""),
            "revision": str(_get(location, "revision", "") or ""),
            "story": story_block(e),
        }
        if is_code and content:
            excerpt.update(
                _code_packet(
                    e,
                    evidence_id=evidence_id,
                    snippet=snippet,
                    content=content,
                    content_truncated=content_truncated,
                    bound=bool(evidence_id in bound or (set(member_ids) & bound)),
                    start_line=start_line,
                    end_line=end_line,
                )
            )
        excerpts.append(excerpt)
    return excerpts


_LOG_GROUP_TYPES = frozenset({"RUNTIME_LOG", "RUNTIME_TRACE", "LOG", "TRACE"})

_STORY_ATTR_KEYS = (
    "story_comp",
    "story_func",
    "story_stage",
    "story_step",
    "story_outcome",
    "story_event",
    "story_elapsed_ms",
)


def story_block(e: Any) -> dict[str, Any] | None:
    """Breadcrumb block for log-type evidence carrying a story envelope.

    Returns the comp/stage/outcome trail (plus capped inputs/outputs) the
    operator reads in the log — the same logic the investigator follows:
    typed transitions with inputs/outputs at every boundary, earliest
    divergence first. None when the evidence carries no envelope.
    """
    attrs = _get(e, "attributes", {}) or {}
    if not isinstance(attrs, dict) or not str(attrs.get("story_comp", "") or ""):
        return None
    block: dict[str, Any] = {
        key[6:]: attrs[key] for key in _STORY_ATTR_KEYS if attrs.get(key) not in (None, "")
    }
    for key in ("story_inputs", "story_outputs"):
        value = attrs.get(key, "")
        if value not in (None, ""):
            text = value if isinstance(value, str) else str(value)
            block[key[6:]] = text[:500]
    return block


def _group_relevance(representative: Any, member_ids: list[str], items: list[Any]) -> float:
    """Max relevance across group members (a hot member keeps the group Tier 1)."""
    try:
        best = float(_get(representative, "relevance", 1.0) or 0.0)
    except (TypeError, ValueError):
        best = 1.0
    if len(member_ids) > 1:
        by_id = {str(_get(e, "evidence_id", "")): e for e in items}
        for member_id in member_ids:
            member = by_id.get(member_id)
            if member is None:
                continue
            try:
                score = float(_get(member, "relevance", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            best = max(best, score)
    return best


def _group_log_items(items: list[Any], enabled: bool) -> list[tuple[Any, list[str]]]:
    """Partition into (representative, member-ID) units, first-appearance order.

    Log-type rows with identical masked shape share one unit (representative
    = first chronologically in list order); everything else passes through
    as a singleton unit. Disabled → every item is its own unit.
    """
    if not enabled:
        return [(e, [str(_get(e, "evidence_id", ""))]) for e in items]
    from investigation_agent_platform.application.compression.logparse import (
        mask_volatiles,
    )

    units: list[tuple[Any, list[str]]] = []
    group_index: dict[str, int] = {}
    for e in items:
        etype = str(_get(_get(e, "evidence_type", ""), "value", e) or "").upper()
        evidence_id = str(_get(e, "evidence_id", ""))
        if etype not in _LOG_GROUP_TYPES or not evidence_id:
            units.append((e, [evidence_id]))
            continue
        key = "\x1f".join(
            [
                str(_get(e, "provider", "") or ""),
                str(_get(e, "source", "") or ""),
                mask_volatiles(
                    str(_get(e, "summary", "") or "")
                    + "\n"
                    + str(_get(e, "content_snippet", "") or "")
                ),
            ]
        )
        if key in group_index:
            position = group_index[key]
            representative, member_ids = units[position]
            if evidence_id not in member_ids:
                units[position] = (representative, [*member_ids, evidence_id])
        else:
            group_index[key] = len(units)
            units.append((e, [evidence_id]))
    return units


def log_grouping_enabled(state: Any) -> bool:
    """Per-run override wins; env kill-switch second; default ON.

    An investigator reruns with ``parameters.log_optimization = "off"`` to
    pay full overhead hunting diamond clues. ``IAP_COMPILER_LOG_GROUP=0`` /
    ``false`` disables globally.
    """
    try:
        request = getattr(getattr(state, "investigation", None), "request", None)
        parameters = getattr(request, "parameters", None) or {}
        if isinstance(parameters, dict):
            flag = parameters.get("log_optimization", None)
            if isinstance(flag, str) and flag.strip().lower() in ("off", "false", "0", "no"):
                return False
            if isinstance(flag, bool) and not flag:
                return False
    except Exception:
        pass
    try:
        return os.environ.get("IAP_COMPILER_LOG_GROUP", "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )
    except Exception:
        return True


# Dead-end statuses render slim: the claim + verdict survive, the working
# apparatus (citation lists, verification checklists) purges. VERIFIED stays
# full — it is the likely conclusion and its statement must not truncate.
_TERMINAL_HYPOTHESIS_STATUSES = frozenset({"REJECTED", "CONTRADICTED"})


@traced("reason", require_scope=False)
def compile_hypotheses(hypotheses: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for h in (hypotheses or [])[:MAX_HYPOTHESES]:
        status = str(_get(_get(h, "status", ""), "value", _get(h, "status", "")))
        if status in _TERMINAL_HYPOTHESIS_STATUSES:
            out.append(
                {
                    "id": str(_get(h, "id", "")),
                    "statement": str(_get(h, "statement", "") or "")[:200],
                    "status": status,
                    "terminal": True,
                }
            )
            continue
        out.append(
            {
                "id": str(_get(h, "id", "")),
                "statement": str(_get(h, "statement", "") or "")[:1000],
                "status": str(_get(_get(h, "status", ""), "value", _get(h, "status", ""))),
                "confidence_score": _get(h, "confidence_score", 0.5),
                "support_score": _get(h, "support_score", 0.5),
                "supporting_evidence_ids": [
                    str(i) for i in (_get(h, "supporting_evidence_ids", []) or [])
                ][:20],
                "refuting_evidence_ids": [
                    str(i) for i in (_get(h, "refuting_evidence_ids", []) or [])
                ][:20],
                "required_verification": [
                    str(r)[:200] for r in (_get(h, "required_verification", []) or [])
                ][:10],
            }
        )
    return out


@traced("reason", require_scope=False)
def compile_timeline(timeline: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in (timeline or [])[-MAX_TIMELINE_EVENTS:]:
        out.append(
            {
                "event": str(_get(_get(t, "event_type", ""), "value", _get(t, "event_type", ""))),
                "description": str(_get(t, "description", "") or "")[:300],
                "at": str(_get(t, "timestamp", "")),
            }
        )
    return out


def _section_tokens(value: Any) -> int:
    """Estimate tokens for a compiled section (JSON-serialized)."""
    import json as _json

    try:
        return estimate_tokens(_json.dumps(value, default=str))
    except Exception:
        return 0


def _is_bound_excerpt(excerpt: dict[str, Any]) -> bool:
    """True only for excerpts the compiler itself marked as claim-bound.

    Older excerpts (no ``claim_priority``) are not protected: revision
    presence alone used to pin every code row, which let a single large
    packet blow the token ceiling while the budget could not shed it.
    """
    try:
        return excerpt.get("claim_priority") == 0
    except Exception:
        return False


def _shrink_code_packet(excerpt: dict[str, Any]) -> bool:
    """Cut one code packet within itself and disclose the continuation.

    Returns True when the packet was shortened. The header/target/window
    stay; ``next_window`` tells the model exactly how to read further, so a
    budget cut never silently replaces asserted lines with a summary.
    """
    content = str(excerpt.get("content") or "")
    if excerpt.get("packet_version") != CODE_PACKET_VERSION or len(content) <= 1500:
        return False
    cut = content[:1500]
    # Prefer a line boundary when one exists in the first 1,500 chars.
    newline = cut.rfind("\n")
    if newline > 500:
        cut = cut[:newline]
    window = excerpt.get("window") or {}
    excerpt["content"] = cut
    excerpt["content_truncated"] = True
    excerpt["next_window"] = _next_window(
        cut, True, window.get("start_line"), window.get("end_line")
    )
    return True


def enforce_context_budget(
    ctx: dict[str, Any], bound: set[str] | None = None, budget: int | None = None
) -> dict[str, Any]:
    """Shed oldest-first until the compiled context fits the token budget.

    Sacrifice order (pinned sections never shed): unbound code packets shrink
    within themselves (disclosing ``next_window``) → unbound Tier-1 excerpts
    demote → Tier-2 (content replaced by the retrieval marker, oldest first) →
    timeline tail beyond newest 3 → pending-questions tail beyond 5 →
    findings tail beyond 3. Records purged counts + final estimate alongside.
    Deterministic: same input, same output.
    """
    ceiling = budget if budget is not None else context_token_budget()
    bound_ids = set(bound or [])
    purged = {
        "excerpts_demoted": 0,
        "code_packets_shrunk": 0,
        "timeline_dropped": 0,
        "pending_dropped": 0,
        "findings_dropped": 0,
    }

    def _total() -> int:
        return sum(
            _section_tokens(ctx.get(key))
            for key in (
                "plan",
                "case_state",
                "hypotheses",
                "timeline",
                "pending_questions",
                "findings",
                "evidence_excerpts",
                "dormant_templates",
            )
        )

    excerpts = ctx.get("evidence_excerpts") or []
    # 1) Shrink unbound code packets before demoting any packet entirely.
    shrinkable = [
        e
        for e in excerpts
        if isinstance(e, dict)
        and e.get("tier") == 1
        and e.get("content")
        and e.get("packet_version") == CODE_PACKET_VERSION
        and str(e.get("evidence_id", "") or "") not in bound_ids
        and not _is_bound_excerpt(e)
    ]
    while _total() > ceiling:
        candidates = [e for e in shrinkable if len(str(e.get("content") or "")) > 1500]
        if not candidates:
            break
        victim = max(candidates, key=lambda e: len(str(e.get("content") or "")))
        if not _shrink_code_packet(victim):
            break
        purged["code_packets_shrunk"] += 1
    # 2) Oldest-first demotion. Bound rows (hypothesis citations) are NEVER
    # demoted — the ID check is exact.
    demotable = [
        e
        for e in excerpts
        if isinstance(e, dict)
        and e.get("tier") == 1
        and e.get("content")
        and str(e.get("evidence_id", "") or "") not in bound_ids
        and not _is_bound_excerpt(e)
    ]
    demotable.sort(key=lambda e: str(e.get("retrieved_at", "") or ""))
    while _total() > ceiling and demotable:
        victim = demotable.pop(0)
        victim["content"] = _TIER2_MARKER
        victim["tier"] = 2
        victim["content_truncated"] = True
        purged["excerpts_demoted"] += 1
    timeline = ctx.get("timeline") or []
    while _total() > ceiling and len(timeline) > 3:
        timeline.pop(0)
        purged["timeline_dropped"] += 1
    ctx["timeline"] = timeline
    for key, cap, counter in (
        ("pending_questions", 5, "pending_dropped"),
        ("findings", 3, "findings_dropped"),
    ):
        section = ctx.get(key) or []
        while _total() > ceiling and len(section) > cap:
            section.pop(0)
            purged[counter] += 1
        ctx[key] = section
    templates_block = ctx.get("dormant_templates") or {}
    templates = templates_block.get("templates") or []
    dropped_templates = 0
    while _total() > ceiling and templates:
        templates.pop()
        dropped_templates += 1
    if dropped_templates:
        templates_block = {**templates_block, "templates": templates}
        ctx["dormant_templates"] = templates_block
    purged["templates_dropped"] = dropped_templates
    ctx["purged"] = purged
    ctx["estimated_tokens"] = _total()
    return ctx


def external_classifier_for_roots(roots: Any) -> Any:
    """Build an ``is_external`` classifier over tenant source roots.

    Returns a classifier that marks every file external when no roots are
    configured, because the dispatch validator rejects all code paths in that
    case. Otherwise a file is external when it sits outside every root.
    """
    normed: list[str] = []
    for root in roots or []:
        text = str(root or "").strip().strip("/")
        if text:
            normed.append(text)
    if not normed:
        return lambda _file_path: True

    def is_external(file_path: str) -> bool:
        normalized = f"/{str(file_path or '').lstrip('/')}"
        for root in normed:
            root_norm = f"/{root}"
            if normalized == root_norm or normalized.startswith(f"{root_norm}/"):
                return False
        return True

    return is_external


@traced("reason", require_scope=False)
def compile_dormant_templates(
    evidence: list[Any],
    *,
    anchors: Any | None = None,
    is_external: Any | None = None,
) -> dict[str, Any]:
    """Render S2 dormant templates as a model-facing section (chunk 2).

    Classifies pinned banked range reads into suspect paths, instantiates
    templates-v1, and renders each with its discriminating fetch (or
    cross-boundary handoff), cost-ordered assumptions, and budget pricing
    against the P1 spread budget. Capped at MAX_DORMANT_TEMPLATES;
    the budget enforcer may trim further.
    """
    spread_budget = 50
    onset_t: str | None = None
    last_good_g: str | None = None
    onset_given = False
    last_good_given = False
    try:
        spread_budget = int(getattr(anchors, "spread_budget", 50) or 0)
    except (TypeError, ValueError):
        spread_budget = 50
    try:
        raw_onset = getattr(anchors, "onset_t", None)
        onset_t = raw_onset.isoformat() if raw_onset is not None else None
        raw_g = getattr(anchors, "last_good_g", None)
        last_good_g = raw_g.isoformat() if raw_g is not None else None
        onset_given = bool(getattr(anchors, "onset_given", False))
        last_good_given = bool(getattr(anchors, "last_good_given", False))
    except Exception:
        pass
    rendered: list[dict[str, Any]] = []
    try:
        paths = classify_banked_sites(list(evidence or []), is_external=is_external)
    except Exception:
        paths = []
    for path in paths:
        try:
            templates = instantiate_templates(path)
        except Exception:
            continue
        sites_by_id = {s.node_id: s for s in path.sites}
        for template in templates:
            first_site = next(
                (sites_by_id[nid] for nid in template.site_node_ids if nid in sites_by_id),
                None,
            )
            fetch = template.discriminating_fetch
            handoff = template.handoff_candidate
            rendered.append(
                {
                    "kind": template.kind.value,
                    "template_version": template.version,
                    "suspect_path": {
                        "repository": path.repository,
                        "revision": path.revision,
                        "file": first_site.file_path if first_site is not None else "",
                    },
                    "discriminating_fetch": (
                        {"fetch_kind": fetch.fetch_kind.value, "target": fetch.target}
                        if fetch is not None
                        else None
                    ),
                    "assumptions": [
                        {
                            "expects": a.expects,
                            "check_order": a.check_order,
                            "cost_estimate": a.cost_estimate,
                        }
                        for a in template.assumptions
                    ],
                    "handoff": (
                        {
                            "upstream_identity": handoff.upstream_identity,
                            "localizing_evidence": handoff.localizing_evidence,
                        }
                        if handoff is not None
                        else None
                    ),
                    "total_cost": template.total_cost,
                    "priced_within_budget": template.priced_within(spread_budget),
                }
            )
            if len(rendered) >= MAX_DORMANT_TEMPLATES:
                break
        if len(rendered) >= MAX_DORMANT_TEMPLATES:
            break
    return {
        "templates_version": TEMPLATES_VERSION_CURRENT,
        "anchors": {
            "onset_t": onset_t or "unknown",
            "last_good_g": last_good_g or "unknown",
            "onset_given": onset_given,
            "last_good_given": last_good_given,
            "spread_budget": spread_budget,
        },
        "templates": rendered,
    }


@traced("reason", require_scope=False)
def compile_case_state(state: Any, evidence: list[Any]) -> dict[str, Any]:
    """Pinned case facts rendered from already-persisted state (SWE-agent ctx §5).

    No new storage, no agent memory: lifecycle, banked counts by evidence
    type, known-fact/entity counts, tool-history depth, and plan length come
    from fields the worker already hydrates. Small by construction (scalars
    only) and pinned — the budget enforcer never sheds it.
    """
    by_type: dict[str, int] = {}
    for item in evidence or []:
        etype = getattr(getattr(item, "evidence_type", ""), "value", None)
        key = str(etype or getattr(item, "evidence_type", "") or "UNKNOWN")
        by_type[key] = by_type.get(key, 0) + 1
    investigation = getattr(state, "investigation", None)
    return {
        "lifecycle_state": str(getattr(getattr(investigation, "status", ""), "value", "") or ""),
        "banked_total": len(evidence or []),
        "banked_by_type": by_type,
        "known_facts": len(getattr(state, "known_facts", []) or []),
        "known_entities": len(getattr(state, "known_entities", []) or []),
        "tool_history_depth": len(getattr(state, "tool_history", []) or []),
        "plan_steps": len(getattr(state, "investigation_plan", []) or []),
    }


def compile_reasoning_context(state: Any, is_external: Any | None = None) -> dict[str, Any]:
    """Assemble the P0-1 context sections from hydrated investigation state.

    ``is_external`` optionally marks banked code files outside tenant
    source roots as cross-boundary for S2 template instantiation; when
    omitted every site stays in-tenant (conservative default).
    """
    problem = str(
        getattr(
            getattr(getattr(state, "investigation", None), "request", None),
            "problem_description",
            "",
        )
        or ""
    )
    hypotheses = _get(state, "active_hypotheses", []) or []
    evidence = _get(state, "evidence", []) or []
    grouping = log_grouping_enabled(state)
    logger.info(
        "compile_reasoning_context started",
        extra={
            "evidence_items": len(evidence),
            "hypotheses": len(hypotheses),
            "log_grouping": grouping,
            "event": "compile_reasoning_context started",
        },
    )
    bound = _bound_evidence_ids(hypotheses)
    excerpts = compile_evidence_excerpts(evidence, bound, grouping)
    try:
        anchors = anchors_from_metadata(
            getattr(getattr(state, "investigation", None), "metadata", None)
        )
    except Exception:
        anchors = None
    dormant_templates = compile_dormant_templates(
        evidence, anchors=anchors, is_external=is_external
    )
    tier_counts: dict[int, int] = {}
    grouped = 0
    for excerpt in excerpts:
        tier_counts[excerpt.get("tier", 1)] = tier_counts.get(excerpt.get("tier", 1), 0) + 1
        if excerpt.get("log_group"):
            grouped += 1
    logger.info(
        "compile_reasoning_context completed",
        extra={
            "excerpts": len(excerpts),
            "tier1": tier_counts.get(1, 0),
            "tier2": tier_counts.get(2, 0),
            "grouped_excerpts": grouped,
            "log_grouping": grouping,
            "bound_ids": len(bound),
            "prompt_version": REASONER_PROMPT_VERSION,
            "event": "compile_reasoning_context completed",
        },
    )
    return enforce_context_budget(
        {
            "prompt_version": REASONER_PROMPT_VERSION,
            "plan": classify_surfaces(problem),
            "case_state": compile_case_state(state, evidence),
            "hypotheses": compile_hypotheses(hypotheses),
            "timeline": compile_timeline(_get(state, "timeline", []) or []),
            "pending_questions": [
                str(q)[:300] for q in (_get(state, "pending_questions", []) or [])
            ][:MAX_PENDING_QUESTIONS],
            "dormant_templates": dormant_templates,
            "findings": [
                {
                    "title": str(_get(f, "title", "") or "")[:200],
                    "statement": str(_get(f, "statement", "") or "")[:500],
                }
                for f in (_get(state, "findings", []) or [])
            ][:MAX_FINDINGS],
            "evidence_excerpts": excerpts,
        },
        bound=bound,
    )
