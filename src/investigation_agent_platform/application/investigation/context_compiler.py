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

from investigation_agent_platform.infrastructure.observability.tracing import traced

# Versioned prompt/context contract. Bumped whenever the compiled section
# shape changes; persisted per decision for replay/evaluation.
REASONER_PROMPT_VERSION = "v2-code-grounded-009"

MAX_CODE_EXCERPTS = 8
MAX_CODE_EXCERPT_CHARS = 1500
MAX_HYPOTHESES = 10
MAX_TIMELINE_EVENTS = 8
MAX_PENDING_QUESTIONS = 10
MAX_FINDINGS = 5

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
    return {str(_get(e, "evidence_id", "")) for e in ranked[: max(0, count)] if _get(e, "evidence_id", "")}


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

    def _retrieved(e: Any) -> str:
        return str(_get(e, "retrieved_at", "") or "")

    # Newest-first: bound rows lead, then most-recently-retrieved — so caps
    # shed the OLDEST rows first as new evidence arrives, never the reverse.
    # (Grouping representatives become newest-of-group.) Pool 500 bounds the
    # sort cost on unbounded banks; the [:100] keeps the newest of the pool.
    pool = list(evidence_items[:500])
    bound_items = sorted(
        [e for e in pool if _eid(e) in bound], key=_retrieved, reverse=True
    )
    rest = sorted(
        [e for e in pool if _eid(e) not in bound], key=_retrieved, reverse=True
    )
    items = (bound_items + rest)[:100]
    recent_ids = _most_recent_ids(items, tier1_recent_count())
    floor = tier1_relevance_floor()
    units = _group_log_items(items, log_group)
    excerpts: list[dict[str, Any]] = []
    code_count = 0
    for representative, member_ids in units:
        e = representative
        summary = str(_get(e, "summary", "") or "")
        snippet = str(_get(e, "content_snippet", "") or "")
        etype = str(_get(_get(e, "evidence_type", ""), "value", e) or "")
        evidence_id = str(_get(e, "evidence_id", ""))
        relevance = _group_relevance(e, member_ids, items)
        tier = 1
        if (
            evidence_id
            and evidence_id not in bound
            and not (set(member_ids) & bound)
            and relevance < floor
            and evidence_id not in recent_ids
            and not (set(member_ids) & recent_ids)
        ):
            tier = 2
        is_code = "CODE" in etype.upper() or "EXCEPTION" in etype.upper()
        content = ""
        content_truncated = False
        if tier == 1 and is_code and snippet and code_count < MAX_CODE_EXCERPTS:
            code_count += 1
            if len(snippet) > MAX_CODE_EXCERPT_CHARS:
                content = snippet[:MAX_CODE_EXCERPT_CHARS]
                content_truncated = True
            else:
                content = snippet
        elif tier == 2 and snippet:
            content = _TIER2_MARKER
        prov = _get(e, "provenance", None)
        excerpts.append(
            {
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
                "revision": str(
                    _get(_get(prov, "source_location", None), "revision", "") or ""
                ),
            }
        )
    return excerpts


_LOG_GROUP_TYPES = frozenset({"RUNTIME_LOG", "RUNTIME_TRACE", "LOG", "TRACE"})


def _group_relevance(
    representative: Any, member_ids: list[str], items: list[Any]
) -> float:
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


def _group_log_items(
    items: list[Any], enabled: bool
) -> list[tuple[Any, list[str]]]:
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
                    str(r)[:200]
                    for r in (_get(h, "required_verification", []) or [])
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
    """Conservative bound-row heuristic for the purge pass.

    Excerpt dicts do not carry bound flags, so treat content-bearing rows
    with revision provenance as pinned; everything else is sheddable. Errs
    toward keeping (returns True on any doubt) — over-shedding citations
    is worse than over-budget context, and the loop re-checks.
    """
    try:
        return bool(excerpt.get("revision"))
    except Exception:
        return True


def enforce_context_budget(
    ctx: dict[str, Any], bound: set[str] | None = None, budget: int | None = None
) -> dict[str, Any]:
    """Shed oldest-first until the compiled context fits the token budget.

    Sacrifice order (pinned sections never shed): unbound Tier-1 excerpts →
    Tier-2 (content replaced by the retrieval marker, oldest first) →
    timeline tail beyond newest 3 → pending-questions tail beyond 5 →
    findings tail beyond 3. Records purged counts + final estimate alongside.
    Deterministic: same input, same output.
    """
    ceiling = budget if budget is not None else context_token_budget()
    bound_ids = set(bound or [])
    purged = {"excerpts_demoted": 0, "timeline_dropped": 0, "pending_dropped": 0, "findings_dropped": 0}

    def _total() -> int:
        return sum(
            _section_tokens(ctx.get(key))
            for key in ("plan", "hypotheses", "timeline", "pending_questions", "findings", "evidence_excerpts")
        )

    excerpts = ctx.get("evidence_excerpts") or []
    # Oldest-first demotion. Bound rows (hypothesis citations) are NEVER
    # demoted — the ID check is exact; the revision heuristic below only
    # protects unlisted-but-likely-cited rows conservatively.
    demotable = [
        e for e in excerpts
        if isinstance(e, dict) and e.get("tier") == 1 and e.get("content")
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
    ctx["purged"] = purged
    ctx["estimated_tokens"] = _total()
    return ctx


@traced("reason", require_scope=False)
def compile_reasoning_context(state: Any) -> dict[str, Any]:
    """Assemble the P0-1 context sections from hydrated investigation state."""
    problem = str(
        getattr(getattr(getattr(state, "investigation", None), "request", None), "problem_description", "")
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
            "hypotheses": compile_hypotheses(hypotheses),
            "timeline": compile_timeline(_get(state, "timeline", []) or []),
            "pending_questions": [
                str(q)[:300] for q in (_get(state, "pending_questions", []) or [])
            ][:MAX_PENDING_QUESTIONS],
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
