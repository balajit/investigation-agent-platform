#!/usr/bin/env python3
"""G3 benefit battery: does the retrieval ledger earn its keep?

Read-only. Follows scripts/agent_metrics.py conventions: SQLAlchemy model
classes for table/column names, no hand-guessed schema, no writes, no schema
change. Reads persisted rows only — same-turn in-memory workflow summaries
are a verify-time convenience, never a battery input, so every counted
attempt is durable.

Tenant scope is mandatory: --tenant is required and every read runs
inside the application's own rls_session (SET LOCAL app.tenant_id), so the
database role's RLS policies — not the script's WHERE clauses — enforce
isolation. An unscoped run is refused (exit 2), never executed.

Persisted data contract (all normative, all diagnostics-only):
  action_execution_id — every RETRIEVAL_ATTEMPT marker carries the
    action_executions.action_id of its dispatch (same UUID recorded by
    action_service.execute). Rows predating the join key carry none and
    count as missing, never as covered.
  VERIFY_EVALUATION   — one timeline row per verify turn carrying the
    recomputed claim bindings, their ledgers (paired by the
    (claim_key, target_ref) identity — target_ref keeps target-distinct
    claims with the same statement+kind separate), the missing
    obligations (claim identity only), and the turn number. Rows are
    ordered by timestamp; the turn disambiguates ties. Every
    current-version turn persists a row, even with empty
    bindings/ledgers/missing — a missing row is a reported persistence
    failure, never an unevaluated turn.
  evidence.retrieved_at — proves when a row entered the bank, so recovery
    novelty does not depend on the 25-ID per-attempt persistence cap.

Metrics (all computed from persisted rows, no new instrumentation):
  coverage    — fraction of eligible retrieval actions (dispatched
                retrieval-type action executions) carrying a recorded
                attempt joined on action_execution_id; bar 100%. Runs with
                dispatched retrieval actions and zero recorded attempts
                stay in the denominator as unmatched actions — missing
                telemetry shows up as missing, never vanishes.
  attribution — fraction of evaporation events traceable to a concrete
                recorded signal, measured on the latest VERIFY_EVALUATION
                event per investigation. Events are unknown citations from
                persisted bindings plus missing-obligation claims from the
                same event (expanded to one unit per distinct target sharing
                the claim_key, so target-distinct claims never merge). A
                unit is attributed when its identity's persisted ledger
                links a SEARCH_FAILED or PARTIAL_ROWS attempt timestamped at
                or before the evaluation — query_match ledgers join by key,
                id_overlap ledgers additionally require banked-ID overlap
                with the binding's cited set — or its binding carries the
                bounded_search flag. Later attempts can never explain an
                earlier evaluation. Signal-free units are unexplained.
                Bar: >= 80% with >= 10 events.
  recovery    — independent end-to-end chains keyed by
                (investigation_id, claim_key, target_ref): a persisted
                pre-evaluation binding with empty claim_local and nonempty
                unknown, a strictly later same-key attempt, and a later
                persisted post-evaluation binding for the same identity
                whose claim_local changed and now contains >= 1 novel row
                durable in the bank. Novelty is proven by attempt-listed
                banked IDs or by the row's bank retrieved_at falling inside
                the (pre, post] window (cap-independent); both proofs require
                post_local containment. The changed binding is read from
                persisted evaluations — observed, not inferred. One chain
                per investigation; bar: >= 3 chains across >= 3
                investigations.
  truncation  — bounded_search flags on claims whose only basis is a
                truncated scoped-empty; reported not-applicable when no
                truncation occurred in the window (never a pass/fail input).
  evaluable   — investigations with >= 1 VERIFY_EVALUATION event. Sample
                floors apply to evaluable runs: branches need >= 5 of them.
                Pre-marker runs contribute to coverage (as misses) but not
                to attribution or recovery.

Catalog attestation (fail-closed, exit 2 as schema_not_verified): the
script resolves the table's schema from pg_tables (refusing zero or
multiple matches), then verifies against that schema that
timeline_events.event_type is varchar(64), RLS is enabled AND forced,
and a policy exists whose USING predicate references both tenant_id and
the app.tenant_id setting (the deployed tenant_isolation predicate —
name matching alone is not accepted). The deployed catalog, not
migration sources, is the authority.

Pass rule: Branch R (recovery) passes when the verdict guard is clean,
coverage is 100% of eligible actions, evaluable runs >= 5, and >= 3
independent chains. Branch D (diagnostic) passes when the guard is clean,
coverage is 100% of eligible actions, evaluable runs >= 5, evaporation
events >= 10, and attribution >= 80%. Below both branches' sample floors
the battery reports inconclusive and the gate stays PARTIAL — inconclusive
is never a pass. Either branch still requires explicit owner sign-off on
the sample before G3 passes.

Exit codes: 0 pass / 1 bar-missed (incl. inconclusive) / 2 unreadable
(missing DB env, failed catalog attestation, query failure — an unread
battery is never a pass).

Usage:
  set -a && source .env.local && source ~/bin/.iap.env.local && set +a
  uv run python scripts/retrieval_benefit.py --tenant ID [--days N]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from itertools import pairwise

_REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from investigation_agent_platform.application.evidence.retrieval_ledger import (
    RETRYABLE_ACTIONS,
    RetrievalOutcome,
    _normalize_scope,
    claim_key,
)

# Verdict-bearing text columns are scanned for these ledger markers. A hit
# means ledger data leaked into a persisted verdict — the no-repair
# contract's battery-side tripwire (the unit-gate differentials in
# test_g3_retrieval_benefit.py are the primary enforcement).
_LEDGER_MARKERS = ("forced_requery", "ledger_requery", "claim_ref", "RETRIEVAL_ATTEMPT")


class _BatteryUnreadable(Exception):
    """Raised when the battery cannot read what it must (exit 2)."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI flags: --tenant ID scopes the battery, --days N sets the window."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default=None, help="Tenant ID to scope the battery (required).")
    parser.add_argument("--days", type=int, default=30, help="Trailing window in days.")
    return parser.parse_args(argv)


def _uri() -> str:
    uri = os.environ.get("IAP_DATABASE_URI", "")
    return uri.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


# ---------------------------------------------------------------------------
# Pure metric core (hermetic: plain record dicts, no DB — unit-tested).
# ---------------------------------------------------------------------------


def _attempt_key(attempt: object) -> tuple[str, str] | None:
    """Normalized (query, scope) key for one attempt dict, or None."""
    if not isinstance(attempt, dict):
        return None
    query = str(attempt.get("search_query") or "").strip()
    if not query:
        return None
    return (query, _normalize_scope(attempt.get("scope")))


def _ts(value: object) -> str:
    """Timestamp string for ordering (ISO-8601, zero-padded — lexicographic)."""
    try:
        return str(value or "")
    except Exception:
        return ""


def _eval_order_key(eval_record: dict) -> tuple[str, int]:
    try:
        turn = int(eval_record.get("turn") or 0)
    except (TypeError, ValueError):
        turn = 0
    return (_ts(eval_record.get("timestamp")), turn)


def _latest_eval(evals: list[dict]) -> dict | None:
    ordered = [e for e in (evals or []) if isinstance(e, dict)]
    if not ordered:
        return None
    return max(ordered, key=_eval_order_key)


def _claim_identity(binding_or_ledger: dict) -> tuple[str, str]:
    """Unique per-binding join identity: (claim_key, target_ref).

    claim_key() covers statement+kind only and collides across
    target-distinct claims; target_ref (persisted on both records, "" on
    pre-field rows) keeps them separate.
    """
    try:
        return (
            str(binding_or_ledger.get("claim_key") or ""),
            str(binding_or_ledger.get("target_ref") or ""),
        )
    except Exception:
        return ("", "")


def _eval_ledgers_for_identity(eval_record: dict, identity: tuple[str, str]) -> list[dict]:
    return [
        ledger
        for ledger in (eval_record.get("ledgers") or [])
        if isinstance(ledger, dict) and _claim_identity(ledger) == identity
    ]


def _cited_universe(binding: dict) -> set[str]:
    universe: set[str] = set()
    for field in ("claim_local", "bank_fallback", "unknown"):
        try:
            for raw in binding.get(field) or []:
                if str(raw or ""):
                    universe.add(str(raw))
        except Exception:
            continue
    return universe


def _claim_unit_signal(
    identity: tuple[str, str],
    eval_record: dict,
    attempts: list[dict],
    as_of_ts: str,
) -> bool:
    """True when the persisted unit shows a recorded signal at eval time.

    Only attempts timestamped at or before the evaluation count — a later
    failure can never explain an earlier evaluation. Linkage follows the
    ledger's own recorded basis: query_match ledgers join by normalized
    key (D2's basis for those links); id_overlap ledgers additionally
    require the attempt's banked IDs to intersect the binding's cited
    universe (D2's basis for those links). A bounded_search flag on the
    binding signals on its own. (Binding lists are capped at 50 IDs, so
    an overlap hidden behind omitted_* counts as no signal — fail-safe.)
    """
    claim_key_value, _target_ref = identity
    if not str(claim_key_value or ""):
        return False
    binding: dict | None = None
    for candidate in eval_record.get("bindings") or []:
        if isinstance(candidate, dict) and _claim_identity(candidate) == identity:
            binding = candidate
            break
    if isinstance(binding, dict) and bool(binding.get("bounded_search")):
        return True
    ledgers = _eval_ledgers_for_identity(eval_record, identity)
    if not ledgers:
        return False
    keys: set[tuple[str, str]] = set()
    query_matched = False
    for ledger in ledgers:
        query = str(ledger.get("search_query") or "").strip()
        if query:
            keys.add((query, _normalize_scope(ledger.get("scope"))))
        if str(ledger.get("link_source") or "") in {"query_match", "stored_query_match"}:
            query_matched = True
    if not keys:
        return False
    universe = _cited_universe(binding) if isinstance(binding, dict) else set()
    for rec in attempts or []:
        if not isinstance(rec, dict):
            continue
        if _ts(rec.get("timestamp")) > as_of_ts:
            continue
        attempt = rec.get("attempt")
        if not isinstance(attempt, dict):
            continue
        if _attempt_key(attempt) not in keys:
            continue
        if str(attempt.get("outcome") or "") not in (
            RetrievalOutcome.SEARCH_FAILED.value,
            RetrievalOutcome.PARTIAL_ROWS.value,
        ):
            continue
        if query_matched:
            return True
        banked = {str(i or "") for i in (rec.get("banked_ids") or []) if str(i or "")}
        if banked & universe:
            return True
    return False


def attempt_coverage(
    cohort: dict[str, dict],
    attempts_by_inv: dict[str, list[dict]],
) -> dict:
    """Per-action coverage over the action_execution_id join key.

    cohort maps investigation id -> {"eligible_action_ids": [...]} for
    dispatched retrieval-type actions. An eligible action counts as
    covered only when a persisted attempt carries its action_execution_id.
    Legacy attempts without the key, and runs with no attempts at all,
    stay in the denominator as missing.
    """
    eligible_ids: set[str] = set()
    for _inv, info in (cohort or {}).items():
        for aid in (info or {}).get("eligible_action_ids") or []:
            if str(aid or ""):
                eligible_ids.add(str(aid))
    recorded_ids: set[str] = set()
    recorded_attempts = 0
    for inv in cohort or {}:
        for rec in (attempts_by_inv or {}).get(str(inv), []):
            if not isinstance(rec, dict):
                continue
            recorded_attempts += 1
            key = rec.get("action_execution_id")
            if str(key or ""):
                recorded_ids.add(str(key))
    matched = eligible_ids & recorded_ids
    runs_covered = 0
    for inv, info in (cohort or {}).items():
        ids = {str(a) for a in (((info or {}).get("eligible_action_ids")) or []) if str(a or "")}
        if ids and ids <= recorded_ids:
            runs_covered += 1
    return {
        "eligible_runs": len(cohort or {}),
        "runs_covered": runs_covered,
        "eligible_actions": len(eligible_ids),
        "matched_actions": len(matched),
        "eligible_rate": (len(matched) / len(eligible_ids)) if eligible_ids else 1.0,
        "recorded_attempts": recorded_attempts,
    }


def evaporation_attribution(
    evals_by_inv: dict[str, list[dict]],
    attempts_by_inv: dict[str, list[dict]],
) -> dict:
    """Attribute evaporation events from the latest persisted evaluation.

    Events are unknown citations from persisted bindings plus
    missing-obligation claims from the same VERIFY_EVALUATION row. Units
    join on the (claim_key, target_ref) identity; a hypothesis-level
    missing obligation expands into one unit per distinct target sharing
    its claim_key (one unit when no binding shares the key) so
    target-distinct claims never merge. A unit is attributed when its
    claim shows a recorded signal at or before the evaluation;
    signal-free units are unexplained.
    """
    events = 0
    attributed = 0
    missing = 0
    for inv, evals in (evals_by_inv or {}).items():
        latest = _latest_eval(list(evals or []))
        if latest is None:
            continue
        as_of = _ts(latest.get("timestamp"))
        attempts = [r for r in ((attempts_by_inv or {}).get(str(inv), [])) if isinstance(r, dict)]
        for binding in latest.get("bindings") or []:
            if not isinstance(binding, dict):
                continue
            unknowns = [str(u or "") for u in (binding.get("unknown") or []) if str(u or "")]
            if not unknowns:
                continue
            identity = _claim_identity(binding)
            signaled = bool(identity[0]) and _claim_unit_signal(identity, latest, attempts, as_of)
            events += len(unknowns)
            if signaled:
                attributed += len(unknowns)
        for item in latest.get("missing_obligations") or []:
            if not isinstance(item, dict):
                continue
            try:
                key = claim_key(str(item.get("claim") or ""), item.get("claim_kind"))
            except Exception:
                continue
            targets = {
                str(b.get("target_ref") or "")
                for b in (latest.get("bindings") or [])
                if isinstance(b, dict) and str(b.get("claim_key") or "") == key
            } or {""}
            for target in sorted(targets):
                events += 1
                missing += 1
                if _claim_unit_signal((key, target), latest, attempts, as_of):
                    attributed += 1
    return {
        "events": events,
        "attributed": attributed,
        "unexplained": events - attributed,
        "rate": (attributed / events) if events else 0.0,
        "missing_obligations": missing,
    }


def lift_recovery_chains(
    attempts_by_inv: dict[str, list[dict]],
    evals_by_inv: dict[str, list[dict]],
    banks_by_inv: dict[str, dict[str, str]],
) -> dict:
    """Detect recovery chains from persisted evaluations, one per run max.

    Chain key is (investigation_id, claim_key, target_ref): a
    pre-evaluation binding with empty claim_local and nonempty unknown, a
    strictly later same-key attempt, and a later post-evaluation binding
    for the same identity whose claim_local changed and now contains >= 1
    novel row that is durable in the evidence bank. Novelty is proven two
    independent ways (union): the row was listed as banked by a same-key
    window attempt and by nothing earlier (attempt-list proof), or the
    row's bank retrieved_at falls inside the (pre, post] window while a
    same-key window attempt returned rows AND no other-key in-window
    attempt banked or returned rows (timestamp proof — independent of
    the 25-ID per-attempt persistence cap, with the sole-banking-key rule
    eliminating the cross-attempt confound). The changed binding is read
    from persisted VERIFY_EVALUATION rows — observed across evaluations,
    not inferred from final state. (Both proofs require post_local
    containment.)
    """
    chains: list[dict] = []
    for inv, evals in (evals_by_inv or {}).items():
        ordered = sorted([e for e in (evals or []) if isinstance(e, dict)], key=_eval_order_key)
        attempts = [r for r in ((attempts_by_inv or {}).get(str(inv), [])) if isinstance(r, dict)]
        bank_times: dict[str, str] = dict(banks_by_inv.get(str(inv), {}) or {})
        bank = set(bank_times)
        claim_evals: dict[tuple[str, str], list[dict]] = {}
        for ev in ordered:
            seen: set[tuple[str, str]] = set()
            for binding in ev.get("bindings") or []:
                if not isinstance(binding, dict):
                    continue
                identity = _claim_identity(binding)
                if identity[0] and identity not in seen:
                    seen.add(identity)
                    claim_evals.setdefault(identity, []).append(ev)
        for identity, evs in claim_evals.items():
            if len(evs) < 2:
                continue
            hit: dict | None = None
            for pre, post in pairwise(evs):
                pre_b = next(
                    (
                        b
                        for b in (pre.get("bindings") or [])
                        if isinstance(b, dict) and _claim_identity(b) == identity
                    ),
                    {},
                )
                post_b = next(
                    (
                        b
                        for b in (post.get("bindings") or [])
                        if isinstance(b, dict) and _claim_identity(b) == identity
                    ),
                    {},
                )
                pre_local = {str(i or "") for i in (pre_b.get("claim_local") or []) if str(i or "")}
                pre_unknown = [str(u or "") for u in (pre_b.get("unknown") or []) if str(u or "")]
                post_local = {
                    str(i or "") for i in (post_b.get("claim_local") or []) if str(i or "")
                }
                if pre_local or not pre_unknown or not post_local:
                    continue
                if post_local == pre_local:
                    continue
                post_keys: set[tuple[str, str]] = set()
                for ledger in _eval_ledgers_for_identity(post, identity):
                    ledger_query = str(ledger.get("search_query") or "").strip()
                    if ledger_query:
                        post_keys.add((ledger_query, _normalize_scope(ledger.get("scope"))))
                if not post_keys:
                    continue
                t0 = _eval_order_key(pre)
                t1 = _eval_order_key(post)
                earlier: set[str] = set()
                window: list[str] = []
                window_returned_rows = False
                window_other_banking = False
                for rec in attempts:
                    key = _attempt_key(rec.get("attempt"))
                    rts = _ts(rec.get("timestamp"))
                    banked = [str(i or "") for i in (rec.get("banked_ids") or []) if str(i or "")]
                    outcome = rec.get("attempt") or {}
                    banking = bool(banked) or str(outcome.get("outcome") or "") in (
                        RetrievalOutcome.ROWS_RETURNED.value,
                        RetrievalOutcome.PARTIAL_ROWS.value,
                    )
                    if key not in post_keys:
                        # A non-same-key attempt that banked/returned rows
                        # in-window could have introduced the timed row, so
                        # the timestamp proof is withheld (listed proof is
                        # row-linked and unaffected).
                        if banking and t0[0] < rts <= t1[0]:
                            window_other_banking = True
                        continue
                    if rts > t0[0] and rts <= t1[0]:
                        window.extend(banked)
                        if banking:
                            window_returned_rows = True
                    elif rts <= t0[0]:
                        earlier.update(banked)
                novel_listed = {b for b in window if b not in earlier}
                durable_listed = {b for b in novel_listed if b in bank}
                novel_timed = {
                    row
                    for row in post_local
                    if row in bank and t0[0] < _ts(bank_times.get(row)) <= t1[0]
                }
                # Either proof contributes only rows the post binding
                # actually cites; incidental durable rows never veto. The
                # timestamp proof additionally requires the same-key
                # attempt to be the sole row-banking attempt in-window —
                # otherwise a different-key attempt could have banked the
                # timed row while the same-key attempt returned an
                # unrelated one. Residual: bank writes outside dispatched
                # retrieval attempts (e.g. seeded exemplars) are assumed
                # absent in-window; such a writer would defeat the
                # temporal tie the same way.
                durable = {b for b in durable_listed if b in post_local}
                if window_returned_rows and not window_other_banking:
                    durable |= novel_timed
                if durable:
                    first_key = min(post_keys)
                    hit = {
                        "query": first_key[0],
                        "scope": first_key[1],
                        "novel_rows": sorted(durable),
                    }
                    break
            if hit is not None:
                chains.append(
                    {
                        "investigation_id": str(inv),
                        "claim_key": identity[0],
                        "target_ref": identity[1],
                        "query": hit["query"],
                        "scope": hit["scope"],
                        "novel_rows": hit["novel_rows"],
                    }
                )
                break
    distinct_invs = {c["investigation_id"] for c in chains}
    return {
        "chains": len(chains),
        "investigations": len(distinct_invs),
        "keys": chains,
    }


def truncation_visibility(
    evals_by_inv: dict[str, list[dict]],
    attempts_by_inv: dict[str, list[dict]],
) -> dict:
    """Count bounded_search flags on latest persisted bindings.

    N/A when no truncation occurred in the window (never a pass/fail
    input). Flags are counted once per claim on the latest evaluation —
    not once per turn — so repeated evaluations never inflate the count.
    """
    flags = 0
    for evals in (evals_by_inv or {}).values():
        latest = _latest_eval(list(evals or []))
        if latest is None:
            continue
        for binding in latest.get("bindings") or []:
            try:
                if isinstance(binding, dict) and bool(binding.get("bounded_search")):
                    flags += 1
            except Exception:
                continue
    truncation_seen = False
    for pairs in (attempts_by_inv or {}).values():
        for rec in pairs or []:
            attempt = rec.get("attempt") if isinstance(rec, dict) else None
            if not isinstance(attempt, dict):
                continue
            try:
                truncated = int(attempt.get("truncated_rows") or 0)
            except (TypeError, ValueError):
                truncated = 0
            provider_total = attempt.get("provider_total_count")
            banked_count = attempt.get("banked_count")
            try:
                provider_visible = (
                    provider_total is not None
                    and banked_count is not None
                    and int(provider_total) > int(banked_count)
                )
            except (TypeError, ValueError):
                provider_visible = False
            signals = attempt.get("signals") or []
            signaled = (
                any(str(s).startswith("truncated:") for s in signals)
                if isinstance(signals, list)
                else False
            )
            if truncated > 0 or provider_visible or signaled:
                truncation_seen = True
                break
        if truncation_seen:
            break
    return {
        "flags": flags,
        "truncation_seen": truncation_seen,
        "applicable": truncation_seen,
    }


def shadow_repair_review(evals_by_inv: dict[str, list[dict]]) -> dict:
    """Phase 3 gate input: every shadow/applied repair candidate.

    Lists each persisted repair with its review identity (claim, target,
    candidate, donor, basis, outcome, version, gate reason) keyed for
    per-candidate dispositions. Fails closed: any marker with
    repaired_truncated or listed-count != counted-total blocks the gate
    (complete=False); recovery is deterministic recompute from persisted
    inputs (see design D4).
    """
    candidates: list[dict] = []
    truncated_markers: list[dict] = []
    mismatch_markers: list[dict] = []
    for inv_id, evals in (evals_by_inv or {}).items():
        for rec in evals or []:
            if not isinstance(rec, dict):
                continue
            listed = [r for r in (rec.get("repaired") or []) if isinstance(r, dict)]
            try:
                applied_n = int(rec.get("repaired_count") or 0)
            except (TypeError, ValueError):
                applied_n = 0
            try:
                shadow_n = int(rec.get("repair_shadow_count") or 0)
            except (TypeError, ValueError):
                shadow_n = 0
            marker_ref = {
                "investigation_id": str(inv_id),
                "turn": rec.get("turn"),
                "hypothesis_id": str(rec.get("hypothesis_id") or ""),
            }
            if bool(rec.get("repaired_truncated", False)):
                truncated_markers.append(dict(marker_ref))
            if len(listed) != applied_n + shadow_n:
                mismatch_markers.append(
                    {**marker_ref, "listed": len(listed), "counted": applied_n + shadow_n}
                )
            for rep in listed:
                try:
                    target = rep.get("target")
                    target_summary = (
                        {
                            k: target.get(k)
                            for k in (
                                "symbol",
                                "file_path",
                                "commit_id",
                                "commit_subject",
                            )
                            if isinstance(target, dict) and target.get(k)
                        }
                        if isinstance(target, dict)
                        else {}
                    )
                except Exception:
                    target_summary = {}
                candidates.append(
                    {
                        **marker_ref,
                        "claim_key": str(rep.get("claim_key") or ""),
                        "claim_ref": str(rep.get("claim_ref") or ""),
                        "target_ref": str(rep.get("target_ref") or ""),
                        "target": target_summary,
                        "cited_id": str(rep.get("cited_id") or ""),
                        "donor_id": str(rep.get("donor_id") or ""),
                        "basis": str(rep.get("basis") or ""),
                        "outcome": str(rep.get("outcome") or ""),
                        "binding_version": str(rep.get("binding_version") or ""),
                        "gate_reason": str(rep.get("gate_reason") or ""),
                        "disposition_key": "|".join(
                            [
                                str(marker_ref["hypothesis_id"]),
                                str(marker_ref["turn"]),
                                str(rep.get("claim_ref") or ""),
                                str(rep.get("cited_id") or ""),
                                str(rep.get("donor_id") or ""),
                                str(rep.get("binding_version") or ""),
                            ]
                        ),
                    }
                )
    complete = not truncated_markers and not mismatch_markers
    return {
        "candidates": candidates,
        "n_candidates": len(candidates),
        "truncated_markers": truncated_markers,
        "count_mismatch_markers": mismatch_markers,
        "complete": complete,
    }


def verdict_guard_from_texts(verdict_texts: list[str]) -> dict:
    """Battery-side no-repair tripwire over persisted verdict text.

    Scans persisted conclusion/hypothesis text for ledger markers. Clean
    means no ledger data leaked into a persisted verdict; the unit-gate
    verdict differentials are the primary enforcement.
    """
    hits: list[dict] = []
    for idx, text in enumerate(verdict_texts or []):
        body = str(text or "")
        for marker in _LEDGER_MARKERS:
            if marker in body:
                hits.append({"row": idx, "marker": marker})
                break
    return {"clean": not hits, "rows_scanned": len(verdict_texts or []), "hits": hits}


def compute_report(
    cohort: dict[str, dict],
    attempts_by_inv: dict[str, list[dict]],
    evals_by_inv: dict[str, list[dict]],
    banks_by_inv: dict[str, dict[str, str]],
    verdict_texts: list[str],
) -> dict:
    """Compute the four G3 metrics plus the pass bar over loaded records.

    Bindings, ledgers, and missing obligations are read from persisted
    VERIFY_EVALUATION rows — never recomputed. Sample floors apply to
    evaluable runs (investigations with >= 1 evaluation event); the
    cohort still sets the coverage denominator.
    """
    coverage = attempt_coverage(cohort or {}, attempts_by_inv or {})
    attribution = evaporation_attribution(evals_by_inv or {}, attempts_by_inv or {})
    recovery = lift_recovery_chains(attempts_by_inv or {}, evals_by_inv or {}, banks_by_inv or {})
    truncation = truncation_visibility(evals_by_inv or {}, attempts_by_inv or {})
    guard = verdict_guard_from_texts(verdict_texts or [])
    # Phase 3: shadow/applied repair review list + gate completeness.
    # Additive — the G3 pass bar is unchanged.
    repair_review = shadow_repair_review(evals_by_inv or {})
    n_runs = len(cohort or {})
    evaluable_runs = sum(1 for inv in (cohort or {}) if (evals_by_inv or {}).get(str(inv)))
    n_bindings = 0
    for inv in cohort or {}:
        latest = _latest_eval(list((evals_by_inv or {}).get(str(inv), [])))
        if latest is not None:
            n_bindings += len([b for b in (latest.get("bindings") or []) if isinstance(b, dict)])
    n_attempt_records = sum(len(v) for v in (attempts_by_inv or {}).values())
    branch_r = (
        bool(guard["clean"])
        and coverage["eligible_rate"] == 1.0
        and evaluable_runs >= 5
        and recovery["chains"] >= 3
        and recovery["investigations"] >= 3
    )
    branch_d = (
        bool(guard["clean"])
        and coverage["eligible_rate"] == 1.0
        and evaluable_runs >= 5
        and attribution["events"] >= 10
        and attribution["rate"] >= 0.80
    )
    inconclusive = evaluable_runs < 5 or (
        (not branch_r and not branch_d) and attribution["events"] < 10
    )
    return {
        "n_runs": n_runs,
        "evaluable_runs": evaluable_runs,
        "n_attempts": n_attempt_records,
        "n_claims": n_bindings,
        "n_evaporation_events": attribution["events"],
        "n_missing_obligations": attribution["missing_obligations"],
        "coverage": coverage,
        "attribution": attribution,
        "recovery": recovery,
        "truncation": truncation,
        "verdict_guard": guard,
        "shadow_repair_review": repair_review,
        "branch_recovery": branch_r,
        "branch_diagnostic": branch_d,
        "inconclusive": inconclusive,
        "pass": bool((branch_r or branch_d) and not inconclusive),
    }


def format_report(report: dict) -> str:
    """Human-readable battery report with sample sizes on every line."""
    cov = report.get("coverage", {})
    attr = report.get("attribution", {})
    rec = report.get("recovery", {})
    trunc = report.get("truncation", {})
    guard = report.get("verdict_guard", {})
    repair_review = report.get("shadow_repair_review", {})
    lines = [
        "## G3 retrieval-benefit battery",
        (
            f"runs: {report.get('n_runs')} evaluable={report.get('evaluable_runs')} "
            f"(attempts: {report.get('n_attempts')}, "
            f"claims: {report.get('n_claims')}, evaporation events: "
            f"{report.get('n_evaporation_events')} "
            f"incl. missing_obligations={report.get('n_missing_obligations')})"
        ),
        (
            f"coverage: eligible_runs={cov.get('eligible_runs')} "
            f"runs_covered={cov.get('runs_covered')} "
            f"eligible_rate={cov.get('eligible_rate', 0.0):.2f} "
            f"(eligible_actions={cov.get('eligible_actions')}, "
            f"matched_actions={cov.get('matched_actions')}, "
            f"recorded_attempts={cov.get('recorded_attempts')})"
        ),
        (
            f"attribution: events={attr.get('events')} attributed={attr.get('attributed')} "
            f"unexplained={attr.get('unexplained')} rate={attr.get('rate', 0.0):.2f}"
        ),
        f"recovery: chains={rec.get('chains')} investigations={rec.get('investigations')}",
        (
            f"truncation: flags={trunc.get('flags')} "
            f"truncation_seen={trunc.get('truncation_seen')} "
            f"applicable={trunc.get('applicable')}"
        ),
        (
            f"verdict_guard: clean={guard.get('clean')} "
            f"rows_scanned={guard.get('rows_scanned')} hits={len(guard.get('hits', []))}"
        ),
        (
            f"repair_review: candidates={repair_review.get('n_candidates')} "
            f"complete={repair_review.get('complete')} "
            f"truncated_markers={len(repair_review.get('truncated_markers', []))} "
            f"mismatched_markers={len(repair_review.get('count_mismatch_markers', []))}"
        ),
        (
            f"branch_recovery={report.get('branch_recovery')} "
            f"branch_diagnostic={report.get('branch_diagnostic')} "
            f"inconclusive={report.get('inconclusive')}"
        ),
        f"PASS={report.get('pass')}",
    ]
    if report.get("evaluable_runs", 0) < report.get("n_runs", 0):
        lines.append(
            "caveat: pre-marker runs contribute to coverage as misses but not to "
            "attribution or recovery (no VERIFY_EVALUATION rows)."
        )
    if report.get("inconclusive"):
        lines.append(
            "caveat: below sample floors — gate stays PARTIAL, inconclusive is never a pass."
        )
    if not trunc.get("applicable"):
        lines.append(
            "caveat: no truncation in window — truncation is "
            "not-applicable (neither pass nor fail input)."
        )
    # D5: explicit owner sign-off is required on every sample — passing
    # numbers included — so the caveat is unconditional, not fail-only.
    lines.append(
        "caveat: numbers alone never close G3 — either branch "
        "needs explicit owner sign-off on the sample."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DB loading (read-only; model classes for table/column names).
# ---------------------------------------------------------------------------


async def _attest_schema(session, table_name: str) -> None:
    """Fail closed unless the catalog shows the RETRIEVAL_ATTEMPT path.

    Resolves the table's schema from pg_tables (zero or multiple matches
    fail closed — the attestation must describe the relation actually
    queried), then verifies against that schema: event_type is
    varchar(64), RLS is enabled AND forced, and a policy exists whose
    USING predicate references both tenant_id and the app.tenant_id
    setting (the deployed tenant_isolation predicate; a matching name
    alone is not accepted). Raises _BatteryUnreadable (exit 2) when the
    attestation is absent — the deployed catalog, not migration sources,
    is the authority.
    """
    from sqlalchemy import text

    # NOTE: pg_tables exposes only rowsecurity, not the FORCE flag —
    # relforcerowsecurity lives in pg_class. Query the real catalog.
    rls_rows = (
        (
            await session.execute(
                text(
                    "SELECT n.nspname AS schemaname, "
                    "c.relrowsecurity AS rowsecurity, "
                    "c.relforcerowsecurity AS forcerowsecurity "
                    "FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace "
                    "WHERE c.relname = :t AND c.relkind = 'r'"
                ),
                {"t": table_name},
            )
        )
        .mappings()
        .all()
    )
    schemas = {str(dict(r).get("schemaname") or "") for r in rls_rows}
    schemas.discard("")
    if len(schemas) != 1:
        raise _BatteryUnreadable(
            "schema_not_verified: "
            f"{table_name} resolves to {len(schemas)} schemas "
            f"({sorted(schemas) or 'none'}) — refusing an ambiguous attestation"
        )
    schema = next(iter(schemas))
    qualified = f"{schema}.{table_name}"
    problems: list[str] = []
    col_rows = (
        (
            await session.execute(
                text(
                    "SELECT data_type, character_maximum_length FROM "
                    "information_schema.columns WHERE table_schema = :s "
                    "AND table_name = :t AND column_name = 'event_type'"
                ),
                {"s": schema, "t": table_name},
            )
        )
        .mappings()
        .all()
    )
    if not col_rows:
        problems.append(f"{qualified}.event_type column absent from catalog")
    else:
        row = dict(col_rows[0])
        if row.get("data_type") != "character varying" or row.get("character_maximum_length") != 64:
            problems.append(
                f"{qualified}.event_type is "
                f"{row.get('data_type')}({row.get('character_maximum_length')}), "
                "expected character varying(64)"
            )
    rls = dict(rls_rows[0])
    if not rls.get("rowsecurity") or not rls.get("forcerowsecurity"):
        problems.append(
            f"{qualified} RLS not enabled+forced "
            f"(rowsecurity={rls.get('rowsecurity')}, "
            f"forcerowsecurity={rls.get('forcerowsecurity')})"
        )
    pol_rows = (
        (
            await session.execute(
                text(
                    "SELECT policyname, cmd, qual FROM pg_policies "
                    "WHERE schemaname = :s AND tablename = :t"
                ),
                {"s": schema, "t": table_name},
            )
        )
        .mappings()
        .all()
    )
    tenant_predicates = [
        str(dict(r).get("policyname") or "")
        for r in pol_rows
        if "tenant_id" in str(dict(r).get("qual") or "")
        and "app.tenant_id" in str(dict(r).get("qual") or "")
    ]
    if not tenant_predicates:
        policy_names = [str(dict(r).get("policyname") or "") for r in pol_rows]
        problems.append(
            f"{qualified} has no policy with a tenant-isolation USING predicate "
            f"(policies={policy_names or 'none'})"
        )
    if problems:
        raise _BatteryUnreadable("schema_not_verified: " + "; ".join(problems))


async def _load_records(tenant_id: str, days: int) -> tuple[dict, dict, dict, dict, list[str]]:
    """Load cohort, attempts, evaluations, bank, and verdict texts (read-only).

    Tenant scope is mandatory: every read runs inside the application's
    own rls_session (SET LOCAL app.tenant_id) and carries an explicit
    tenant predicate, so isolation holds even if a session-level setting
    were dropped. An empty tenant is refused before any query runs.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.orm import sessionmaker

    from investigation_agent_platform.infrastructure.persistence import models
    from investigation_agent_platform.infrastructure.persistence.models import (
        ActionExecutionORM,
        EvidenceORM,
        InvestigationConclusionORM,
        TimelineEventORM,
    )
    from investigation_agent_platform.infrastructure.persistence.rls import rls_session

    if not str(tenant_id or "").strip():
        raise _BatteryUnreadable("missing --tenant — unscoped runs are refused")
    uri = _uri()
    if not uri:
        raise _BatteryUnreadable(
            "missing IAP_DATABASE_URI — set it via .env.local / "
            "~/bin/.iap.env.local before running the battery"
        )
    eng = create_async_engine(uri)
    try:
        from sqlalchemy.ext.asyncio import AsyncSession

        maker = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
        since = datetime.now(UTC) - timedelta(days=max(0, int(days or 0)))
        async with rls_session(maker, str(tenant_id)) as session:
            await _attest_schema(session, TimelineEventORM.__tablename__)

            action_stmt = select(ActionExecutionORM).where(
                ActionExecutionORM.tenant_id == tenant_id,
                ActionExecutionORM.created_at >= since,
                ActionExecutionORM.action_type.in_(sorted(RETRYABLE_ACTIONS)),
            )
            cohort: dict[str, dict] = {}
            for row in (await session.execute(action_stmt)).scalars().all():
                inv = str(row.investigation_id)
                entry = cohort.setdefault(inv, {"eligible_action_ids": []})
                try:
                    entry["eligible_action_ids"].append(str(row.action_id))
                except Exception:
                    continue
            inv_ids = list(cohort)

            attempts_by_inv: dict[str, list[dict]] = {}
            evals_by_inv: dict[str, list[dict]] = {}
            if inv_ids:
                tl_stmt = (
                    select(TimelineEventORM)
                    .where(TimelineEventORM.tenant_id == tenant_id)
                    .where(TimelineEventORM.timestamp >= since)
                    .where(TimelineEventORM.investigation_id.in_(inv_ids))
                    .order_by(TimelineEventORM.timestamp.asc())
                )
                for event in (await session.execute(tl_stmt)).scalars().all():
                    event_type = getattr(event, "event_type", None)
                    type_name = str(getattr(event_type, "value", event_type))
                    attrs = getattr(event, "attributes", None) or {}
                    if not isinstance(attrs, dict):
                        attrs = {}
                    try:
                        stamp = event.timestamp.isoformat()
                    except Exception:
                        stamp = ""
                    if type_name == "RETRIEVAL_ATTEMPT":
                        attempt = attrs.get("attempt")
                        banked = attrs.get("evidence_ids") or list(
                            getattr(event, "evidence_ids", None) or []
                        )
                        attempts_by_inv.setdefault(str(event.investigation_id), []).append(
                            {
                                "banked_ids": [
                                    str(i or "") for i in (banked or []) if str(i or "")
                                ],
                                "attempt": dict(attempt) if isinstance(attempt, dict) else None,
                                "action_execution_id": str(attrs.get("action_execution_id") or "")
                                or None,
                                "timestamp": stamp,
                            }
                        )
                    elif type_name == "VERIFY_EVALUATION":
                        try:
                            turn = attrs.get("turn")
                            turn_value = int(turn) if turn is not None else None
                        except (TypeError, ValueError):
                            turn_value = None
                        evals_by_inv.setdefault(str(event.investigation_id), []).append(
                            {
                                "timestamp": stamp,
                                "turn": turn_value,
                                "hypothesis_id": str(attrs.get("hypothesis_id") or ""),
                                "bindings": list(attrs.get("bindings") or []),
                                "ledgers": list(attrs.get("ledgers") or []),
                                "missing_obligations": list(attrs.get("missing_obligations") or []),
                                "evaporated_count": attrs.get("evaporated_count", 0),
                                # Phase 3 (claim binding): bounded repair
                                # dumps plus uncapped counts (fail-closed
                                # gate inputs — see shadow_repair_review).
                                "repaired": list(attrs.get("repaired") or []),
                                "repaired_truncated": bool(attrs.get("repaired_truncated", False)),
                                "repaired_count": attrs.get("repaired_count", 0),
                                "repair_shadow_count": attrs.get("repair_shadow_count", 0),
                            }
                        )

            banks_by_inv: dict[str, dict[str, str]] = {}
            if inv_ids:
                ev_stmt = select(EvidenceORM).where(
                    EvidenceORM.tenant_id == tenant_id,
                    EvidenceORM.investigation_id.in_(inv_ids),
                )
                for ev in (await session.execute(ev_stmt)).scalars().all():
                    # evidence_key == in-app evidence_id (see
                    # evidence_repository: evidence_key=str(evidence.evidence_id)).
                    # retrieved_at proves when the row entered the bank, so
                    # recovery novelty does not depend on the 25-ID
                    # per-attempt persistence cap.
                    try:
                        banked_ts = ev.retrieved_at.isoformat()
                    except Exception:
                        banked_ts = ""
                    banks_by_inv.setdefault(str(ev.investigation_id), {})[str(ev.evidence_key)] = (
                        banked_ts
                    )

            verdict_texts: list[str] = []
            if inv_ids:
                concl_stmt = select(InvestigationConclusionORM).where(
                    InvestigationConclusionORM.tenant_id == tenant_id,
                    InvestigationConclusionORM.investigation_id.in_(inv_ids),
                )
                for concl in (await session.execute(concl_stmt)).scalars().all():
                    for field in (
                        "root_cause",
                        "best_reason",
                        "next_check",
                        "limitations",
                        "recommended_next_steps",
                    ):
                        value = getattr(concl, field, None)
                        if isinstance(value, str) and value:
                            verdict_texts.append(value)
                        elif isinstance(value, list):
                            verdict_texts.extend(str(v) for v in value if v)
            _ = models
            return cohort, attempts_by_inv, evals_by_inv, banks_by_inv, verdict_texts
    finally:
        await eng.dispose()


def run_battery(tenant_id: str | None, days: int) -> dict:
    """Load persisted records and compute the report (DB errors raise)."""
    cohort, attempts, evals, banks, verdict_texts = asyncio.run(_load_records(tenant_id, days))
    return compute_report(cohort, attempts, evals, banks, verdict_texts)


def main(argv: list[str] | None = None) -> int:
    """CLI entry: print the report; 0 pass / 1 bar-missed / 2 unreadable."""
    args = parse_args(argv)
    if not str(args.tenant or "").strip():
        print("battery unreadable: missing --tenant — unscoped runs are refused", file=sys.stderr)
        return 2
    try:
        report = run_battery(tenant_id=args.tenant, days=args.days)
    except _BatteryUnreadable as exc:
        print(f"battery unreadable: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"battery unreadable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(format_report(report))
    return 0 if report.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
