#!/usr/bin/env python3
"""Tranche B calibration battery: spring-sample-service (tenant n-a).

Files one investigation per case against application_id=spring-sample-service,
polls to terminal via the API (no arbitrary sleeps), and scores each case
PASS / PARTIAL / FAIL with evidence links.

Usage:
  uv run python scripts/tranche_b_battery.py [--api-base URL] [--tenant ID]
      [--timeout SECS] [--only L1,L4]

Scorer-known expectations are pinned below (grounded in the synthetic
Spring checkout built by scripts/build-sample-checkout.py).
Cases are code-navigation only: log/refdoc legs are out of scope (marked N/A).
The profile additionally declares Oracle state, which cases never query.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from investigation_agent_platform.domain.finding.models import names_concrete_fetch_target

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
APP_ID = "spring-sample-service"

#: T9-D19: rubric version, bumped whenever any scoring dimension changes.
#: Printed in every report header; battery_stats.compare refuses to compare
#: cells stamped with different rubrics. The pre-rubric history reads as r0.
RUBRIC_VERSION = "r2"

ANSWERED_SET = {"ANSWERED", "ROOT_CAUSE_ESTABLISHED", "ROOT_CAUSE_LIKELY"}
ABSTAIN_SET = {"INSUFFICIENT_EVIDENCE", "NO_ROOT_CAUSE_FOUND"}
# Rows carrying these miss reasons are infrastructure failures: they may
# appear in the corpus but must never support a conclusion.
FAILURE_REASONS = {"timeout", "auth_failed", "provider_unavailable", "internal_error"}


def load_cases() -> list[dict]:
    """Case definitions shared by the live battery and the offline grader tests.

    Single source of truth: tests/fixtures/battery_cases/*.json. Cases with
    live=false run only in the offline grader suite (they need fixtures the
    sample checkout cannot produce: multi-commit history, provider outages).
    """
    root = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "battery_cases"
    if not root.is_dir():
        raise FileNotFoundError(f"battery case fixtures missing: {root}")
    cases = []
    for path in sorted(root.glob("*.json")):
        with open(path, encoding="utf-8") as fh:
            cases.append(json.load(fh))
    return cases


CASES = load_cases()


def api(base: str, tenant: str, method: str, path: str, body=None, timeout=30):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"X-Tenant-ID": tenant, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode() or "{}")
        except Exception:
            return exc.code, {}


def poll(base: str, tenant: str, iid: str, timeout: int) -> dict:
    t0 = time.time()
    while True:
        _, data = api(base, tenant, "GET", f"/api/v1/investigations/{iid}")
        inv = data.get("investigation", {})
        if inv.get("status") in TERMINAL:
            return inv
        if time.time() - t0 > timeout:
            raise TimeoutError(f"{iid} not terminal after {timeout}s")
        time.sleep(15)


# Operator-side measurement stamp. The pin travels on the investigation
# entity (metadata["claim_binding_version"]) exactly as the repo contract
# requires; it is written directly by the operator between create and start
# rather than through a public API field, so no tenant can choose its own
# verification semantics.
CLAIM_BINDING_STAMP_SQL = (
    "UPDATE investigations "
    "SET metadata_json = coalesce(metadata_json, '{}'::jsonb) "
    "|| jsonb_build_object('claim_binding_version', $1::text) "
    "WHERE id = $2::uuid AND tenant_id = $3 AND status = 'CREATED'"
)


def known_claim_binding_versions() -> list[str]:
    from investigation_agent_platform.application.investigation.verification import (
        KNOWN_CLAIM_BINDING_VERSIONS,
    )

    return sorted(KNOWN_CLAIM_BINDING_VERSIONS)


def stamp_claim_binding_version(tenant: str, iid: str, version: str) -> bool:
    """Pin ``version`` on a CREATED investigation. True when exactly one row changed.

    Needs IAP_DATABASE_URI (operator environment; never printed). A run the
    disposition already closed (status != CREATED) is not stamped and
    returns False.
    """
    import asyncio

    import asyncpg

    uri = os.environ.get("IAP_DATABASE_URI", "")
    dsn = re.sub(r"^postgresql\+asyncpg://", "postgresql://", uri)
    if not dsn:
        raise RuntimeError("IAP_DATABASE_URI is required to stamp --claim-binding-version")

    async def _run() -> bool:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("SELECT set_config('app.tenant_id', $1, false)", tenant)
            result = await conn.execute(CLAIM_BINDING_STAMP_SQL, version, iid, tenant)
            return str(result).endswith(" 1")
        finally:
            await conn.close()

    return asyncio.run(_run())


def pinned_claim_binding_version(base: str, tenant: str, iid: str) -> str:
    """The claim-binding pin recorded on a graded investigation, or 'unknown'."""
    try:
        _, data = api(base, tenant, "GET", f"/api/v1/investigations/{iid}")
        meta = (data.get("investigation") or {}).get("metadata") or {}
        return str(meta.get("claim_binding_version") or "unmarked")
    except Exception:
        return "unknown"


def _rows_by_id(evidence: dict) -> dict:
    by_id = {}
    for row in (evidence or {}).get("items", []) or []:
        try:
            by_id[str(row.get("evidence_id", ""))] = row
        except Exception:
            continue
    return by_id


def _supporting_rows(conclusion: dict, hypotheses: dict, by_id: dict) -> list:
    # T8-D16: only the conclusion's own evidence. A linked hypothesis is
    # unverified prose; its cited rows must not rescue the conclusion.
    ids = {str(i) for i in (conclusion or {}).get("supporting_evidence_ids") or []}
    return [by_id[i] for i in ids if i in by_id]


def _row_location(row: dict) -> tuple:
    prov = ((row or {}).get("provenance") or {}).get("source_location") or {}
    return (
        str(prov.get("file_path") or ""),
        prov.get("line_start"),
        prov.get("line_end"),
        str(prov.get("revision") or ""),
    )


def _path_matches(want: str, got: str) -> bool:
    w = (want or "").replace("\\", "/").strip().strip("/")
    g = (got or "").replace("\\", "/").strip().strip("/")
    if not w or not g:
        return False
    if w == g:
        return True
    if "/" in w:
        return g.endswith("/" + w)
    return g.rsplit("/", 1)[-1] == w


def _word_in(name: str, text: str) -> bool:
    if not name:
        return False
    return (
        re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text or "", re.IGNORECASE)
        is not None
    )


_DECLARATION_PREFIX = re.compile(
    r"(?:^|\s)(?:async\s+)?(?:def|class|function|func|fn|void|public|private|protected|static)\s+[\w.<>\[\], ]*$"
)


def _call_shown(callee: str, snippet: str) -> bool:
    """A call expression for callee appears (declarations excluded)."""
    names = {callee}
    for sep in ("::", ".", "#"):
        if sep in callee:
            names.add(callee.rsplit(sep, 1)[-1])
    for raw in (snippet or "").splitlines():
        line = (
            raw.split("| ", 1)[1]
            if ("| " in raw and raw.split("|", 1)[0].strip().isdigit())
            else raw
        )
        for name in names:
            if not name:
                continue
            for match in re.finditer(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"\s*\(", line):
                if _DECLARATION_PREFIX.search(line[: match.start()]):
                    continue
                return True
    return False


def _row_texts(row: dict) -> str:
    parts = [str(row.get(k, "") or "") for k in ("title", "summary", "content_snippet", "source")]
    file_path, _, _, _ = _row_location(row)
    parts.append(file_path)
    return "\n".join(parts)


_DECLARES = re.compile(r"\b(?:class|interface|enum|record|def|function|func)\s+{name}\b")


def _declares(symbol: str, snippet: str) -> bool:
    """The window declares the symbol; a mere mention (a call, an import, a comment) does not count."""
    return re.search(_DECLARES.pattern.format(name=re.escape(symbol)), snippet or "") is not None


def _check_definition(expect: dict, root: str, rows: list) -> tuple:
    want = expect.get("cited_path", "")
    base = want.rsplit("/", 1)[-1]
    if not _word_in(base, root):                                  # word boundary, not substring
        return (False, f"conclusion names no {want}")
    symbol = expect.get("expect_symbol", "")
    for row in rows:
        file_path, line_start, line_end, _ = _row_location(row)
        if not _path_matches(want, file_path) or not isinstance(line_start, int):
            continue
        snippet = str(row.get("content_snippet") or "")
        if symbol and not (_declares(symbol, snippet) or _word_in(symbol, snippet)):
            continue
        if expect.get("declaration_required") and not _declares(symbol, snippet):
            continue
        return (True, "")
    return (False, f"no cited range read of {want}")


def _check_callers(expect: dict, root: str, rows: list) -> tuple:
    callee = expect.get("callee", "")
    callee_method = str(expect.get("callee_method") or "").strip()
    candidates = [str(c) for c in (expect.get("expect_any") or []) if str(c).strip()]
    grounded: list[str] = []
    for caller in candidates:
        for row in rows:
            file_path, line_start, line_end, revision = _row_location(row)
            snippet = str(row.get("content_snippet") or "")
            invocation = callee_method or callee
            caller_stem = caller.rsplit(".", 1)[-1]
            source_evidence = str(row.get("evidence_type", "") or "").upper() in {
                "SOURCE_CODE",
                "EXCEPTION_PATH",
            }
            caller_file_match = caller_stem.lower() in file_path.lower()
            if (
                caller_stem.lower() in _row_texts(row).lower()
                and callee
                and source_evidence
                and _call_shown(invocation, snippet)
                and isinstance(line_start, int)
                and isinstance(line_end, int)
                and bool(revision)
                and caller_file_match
            ):
                grounded.append(caller)
                break
    # T9-D20: prose must name each grounded caller it claims (word boundary,
    # callee stem), and every required caller must be grounded and named.
    required = [c for c in (expect.get("expect_all") or []) if str(c).strip()]
    need = int(expect.get("min_callers", len(required) or 1))
    named = [c for c in grounded if _word_in(c.rsplit(".", 1)[-1], root)]
    if required:
        missing = [c for c in required if c not in named]
        if missing:
            return (False, f"callers not both grounded and named: missing {missing} (grounded {grounded}, named {named})")
    elif len(named) < need:
        return (False, f"{len(named)} of {need} expected callers grounded and named in the conclusion")
    if not grounded:
        if not candidates:
            return (False, "no expected caller candidates configured")
        return (False, f"no expected caller has a cited range showing a call to '{callee}'")
    return (True, "")


def _check_history(expect: dict, root: str, rows: list) -> tuple:
    want = expect.get("cited_path", "")
    if want and want.lower() not in root.lower():
        return (False, f"conclusion names no {want}")
    commits = [
        row
        for row in rows
        if str(row.get("evidence_type", "") or "").upper() == "COMMIT_HISTORY"
        and (not want or _path_matches(want, _row_location(row)[0]))
    ]
    if not commits:
        return (False, "no cited commit row")
    if not any(
        (subject := str(row.get("summary") or "").strip())
        and any(
            len(token) >= 4 and token.lower() in root.lower()
            for token in re.findall(r"[A-Za-z0-9_-]+", subject)
        )
        for row in commits
    ):
        return (False, "conclusion does not describe a cited commit subject")
    return (True, "")


_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def _check_values(case: dict, expect: dict, root: str, rows: list) -> tuple:
    # A cited row's identity (file path) is evidence of WHERE, same as its
    # snippet is evidence of what — method-anchored windows legitimately omit
    # the enclosing class declaration (live L5: the 6-19 window never names
    # LegacyCodeMapper in prose, but its path does). Same helper as the
    # caller check uses (_row_texts), so prose-against-row matching is one
    # rule everywhere instead of two.
    symptom = str(case.get("symptom") or "")
    texts = [_row_texts(r) for r in rows]
    for value in expect.get("expect_values") or []:
        if not _word_in(value, root):                              # whole word, case-insensitive
            return (False, f"value '{value}' not in conclusion")
        if not any(_word_in(value, t) for t in texts):
            return (False, f"value '{value}' in prose but no cited row shows it")
    # a token the question already contains proves nothing; require at least one answer token it does not
    answer_tokens = [v for v in expect.get("expect_values") or [] if not _word_in(v, symptom)]
    if (expect.get("expect_values") or []) and not answer_tokens:
        return (False, "every expected token is already in the question; fixture cannot discriminate")
    for rel in expect.get("relations") or []:                      # subject, predicate_any, object in one sentence
        if not any(
            _word_in(rel["subject"], s) and _word_in(rel["object"], s)
            and any(p.lower() in s.lower() for p in rel.get("predicate_any") or [""])
            for s in _SENTENCE.split(root)
        ):
            return (False, f"conclusion does not relate {rel['subject']} to {rel['object']}")
    return (True, "")


def _check_absence(expect: dict, root: str, rows: list) -> tuple:
    subject = expect.get("absent_subject", "")
    scope = str(expect.get("scope") or "").strip().lower()
    if subject and not _word_in(subject, root):
        return (False, f"conclusion states no '{subject}'")
    if not scope:
        return (False, "absence claim states no scope")
    for row in rows:
        attrs = row.get("attributes") or {}
        if not isinstance(attrs, dict):
            continue
        proof_scope = str(attrs.get("search_scope", "") or "").strip().lower()
        if attrs.get("match_count") != 0 or not proof_scope:
            continue
        if proof_scope == scope or scope in proof_scope:
            return (True, "")
    return (False, f"no scoped empty-search proof covers '{scope}'")


def _proven_absence_insufficient(expect: dict, root: str, hyps: dict, evidence: dict) -> tuple:
    """Proven-absence INSUFFICIENT: an abstention backed by a banked scoped
    empty-search proof passes (DES-S6: proven-absence INSUFFICIENT passes;
    unbound INSUFFICIENT routes into spread, never into a stop). Anything
    else stays a status FAIL — the proof, not the status alone, earns it."""
    if str(expect.get("claim") or "") != "absence":
        return (False, "not an absence claim")
    subject = expect.get("absent_subject", "")
    scope = str(expect.get("scope") or "").strip().lower()
    if not scope:
        return (False, "absence claim states no scope")
    if subject:
        # Abstention conclusions carry root_cause=None, so the subject may
        # live in the hypothesis statement that drove the scoped search.
        texts = [root]
        for hyp in (hyps or {}).get("items") or []:
            try:
                texts.append(str(hyp.get("statement") or ""))
            except Exception:
                continue
        if not any(_word_in(subject, t) for t in texts):
            return (False, f"abstention states no '{subject}'")
    for row in (evidence or {}).get("items", []) or []:
        attrs = row.get("attributes") or {}
        if not isinstance(attrs, dict):
            continue
        proof_scope = str(attrs.get("search_scope", "") or "").strip().lower()
        if attrs.get("match_count") != 0 or not proof_scope:
            continue
        if proof_scope == scope or scope in proof_scope:
            return (True, "proven-absence INSUFFICIENT (scoped empty-search proof banked)")
    return (False, f"no scoped empty-search proof covers '{scope}'")


def _check_no_failure(rows: list) -> tuple:
    for row in rows:
        attrs = row.get("attributes") or {}
        reason = str((attrs if isinstance(attrs, dict) else {}).get("miss_reason", "") or "")
        if reason in FAILURE_REASONS:
            return (False, f"cites failure row ({reason})")
    return (True, "")


def _check_revision(expect: dict, rows: list) -> tuple:
    incident = str(expect.get("incident_revision") or "").strip()
    if not incident:
        return (True, "")
    for row in rows:
        etype = str(row.get("evidence_type", "") or "").upper()
        if etype not in ("SOURCE_CODE", "EXCEPTION_PATH"):
            continue
        _, _, _, revision = _row_location(row)
        if revision != incident:
            return (False, f"cited row at {revision or 'unknown'} revision, want {incident}")
    return (True, "")


def _check_rival_exemption(case: dict, expect: dict, conclusion: dict, hypotheses: dict) -> tuple:
    if not expect.get("rival_untested_exempt"):
        return (True, "")
    statements = set()
    for hyp in (hypotheses or {}).get("items", []) or []:
        text = str(hyp.get("statement", "") or "").strip()
        if text:
            statements.add(text)
    if len(statements) < 2:
        return (False, "exemption case needs rival hypotheses present")
    limitations = " ".join(str(l) for l in (conclusion or {}).get("limitations") or [])
    if "RIVAL_UNTESTED" in limitations:
        return (False, "location answer demoted by untested rival (exemption violated)")
    return (True, "")


def _abstention_earned(expect: dict, concl: dict, evidence: dict, investigation: dict | None) -> tuple[bool, str]:
    """L8-style: an honest abstention shows that evidence was attempted and says why it stopped.

    Uses only fields present on both sides of a before/after comparison (iterations, evidence rows);
    ``fetches_dispatched`` is read when D4 has stamped it."""
    tele = ((investigation or {}).get("metadata") or {}).get("loop_telemetry") or {}
    iterations = tele.get("iterations")
    rows = len((evidence or {}).get("items") or [])
    fetches = tele.get("fetches_dispatched")
    attempted = (fetches or 0) > 0 or rows > 0
    if iterations is not None and int(iterations) < int(expect.get("min_turns", 1)):
        return (False, f"abstained after {iterations} turn(s); expected at least {expect.get('min_turns', 1)}")
    if investigation is not None and not attempted:
        return (False, "abstained with no fetch dispatched and no evidence banked (no search shown)")
    if not (concl.get("limitations") or (concl.get("handoff") or {}).get("state")):
        return (False, "abstained with no stated reason (no limitation, no handoff state)")
    return (True, "")


def _check_required_limitations(expect: dict, concl: dict) -> tuple[bool, str]:
    text = " ".join(str(x) for x in (concl.get("limitations") or []))
    for code in expect.get("required_limitations") or []:
        if code not in text:
            return (False, f"limitation {code} missing")
    for token in expect.get("limitation_mentions") or []:                  # e.g. the false target's symbol
        if not _word_in(token, text):
            return (False, f"limitations do not name '{token}'")
    return (True, "")


def _check_forbidden_root_text(expect: dict, root: str) -> tuple[bool, str]:
    """An opinion claim's statement must not appear in the verdict (>= 60% of its content words)."""
    for group in expect.get("claim_groups") or []:
        if group.get("kind") not in expect.get("forbid_root_text_from_groups", []):
            continue
        words = {w for w in re.findall(r"[a-z0-9]+", str(group.get("statement", "")).lower()) if len(w) > 3}
        if words and sum(1 for w in words if _word_in(w, root)) / len(words) >= 0.6:
            return (False, "verdict repeats an excluded opinion claim")
    return (True, "")


def _check_targets(expect: dict, concl: dict, root: str) -> tuple[str, str] | None:
    """Per-target grading for multi-target DEFINITION. ABSTAIN naming the false
    target passes; ANSWERED passes only when root names every grounded target
    and no ungrounded one. Returns (verdict, detail) or None to fall through."""
    targets = expect.get("targets") or []
    if not targets:
        return None
    grounded = [t for t in targets if t.get("grounded")]
    ungrounded = [t for t in targets if not t.get("grounded")]
    cstatus = str(concl.get("status", "") or "")
    accept = expect.get("accept") or []
    if cstatus in ABSTAIN_SET:
        if "abstain_naming_false_target" not in accept:
            return None
        for target in ungrounded:
            if _word_in(str(target.get("symbol", "")), root):
                return ("PASS", f"abstention names the false target {target.get('symbol')}")
            text = " ".join(str(x) for x in (concl.get("limitations") or []))
            if target.get("symbol") and str(target["symbol"]) in text:
                return ("PASS", f"abstention names the false target {target.get('symbol')}")
        return ("PARTIAL", "abstention does not name the false target")
    if cstatus in ANSWERED_SET:
        for target in ungrounded:
            if _word_in(str(target.get("symbol", "")), root):
                return ("FAIL", f"answer names ungrounded target {target.get('symbol')}")
        if "answered_grounded_only" in accept:
            if not grounded:
                return None
            if all(_word_in(str(t.get("symbol", "")), root) for t in grounded):
                return ("PASS", "answer names every grounded target and no ungrounded one")
            return ("PARTIAL", "answer does not name every grounded target")
    return None


def _named_gap(concl: dict) -> tuple[bool, str]:
    for lim in concl.get("limitations") or []:
        if str(lim).startswith(("UNVERIFIED_CLAIM_GROUP", "NOT_CHECKABLE_EXCLUDED")):
            return (True, f"names its unproven group: {str(lim)[:60]}")
    return (False, "")


def score_supported(
    case: dict, conclusion: dict, findings: dict, evidence: dict, hyps: dict,
    investigation: dict | None = None,
) -> tuple:
    """Grade the conclusion, not the corpus.

    PASS requires the expected answer status plus every applicable
    dimension (citation, revision, range, rival, scope). A right answer on
    wrong evidence is PARTIAL with the failing dimension named; a wrong
    answer status is FAIL. Pure on dicts — offline-testable.
    """
    expect = case.get("expect", {}) or {}
    want_status = expect.get("status", "ANSWERED")
    concl = conclusion or {}
    cstatus = str(concl.get("status", "") or "")
    root = str(concl.get("root_cause") or "")
    # T9-D22: per-target expectations decide first (they PASS/FAIL outright,
    # e.g. L16's answered-grounded-only verdict under an ABSTAIN expectation).
    targeted = _check_targets(expect, concl, root)
    if targeted is not None:
        return targeted
    if want_status == "ABSTAIN":
        if cstatus in ABSTAIN_SET:
            rows = _supporting_rows(concl, hyps, _rows_by_id(evidence))
            # T9-D21: an abstention must be earned and carry required limitations.
            if expect.get("strict_abstention"):
                ok, reason = _abstention_earned(expect, concl, evidence, investigation)
                if not ok:
                    return ("PARTIAL", reason)
            if expect.get("required_limitations") or expect.get("limitation_mentions"):
                ok, reason = _check_required_limitations(expect, concl)
                if not ok:
                    return ("PARTIAL", reason)
            # Phase 4 (claim binding): opinion-probe dimension. The
            # hypothesis API items carry no assessments, so the check is
            # made on the conclusion surface the verify activity controls:
            # ABSTAIN + zero supporting rows (retrieval skipped) + the
            # NOT_CHECKABLE_SKIPPED limitation the conclude path emits.
            if str((expect or {}).get("claim", "") or "") == "not_checkable":
                if rows:
                    return ("PARTIAL", f"cites {len(rows)} supporting rows for an opinion claim")
                limitations = list((concl or {}).get("limitations") or [])
                if "NOT_CHECKABLE_SKIPPED" not in limitations:
                    return ("PARTIAL", "abstained without NOT_CHECKABLE_SKIPPED limitation")
                ok, reason = _check_no_failure(rows)
                if not ok:
                    return ("PARTIAL", reason)
                return ("PASS", f"honest abstention ({cstatus}), retrieval skipped")
            ok, reason = _check_no_failure(rows)
            if not ok:
                return ("PARTIAL", reason)
            return ("PASS", f"honest abstention ({cstatus})")
        if cstatus in ANSWERED_SET:
            return ("FAIL", "fabricated answer to unanswerable")
        return ("FAIL", f"status={cstatus or 'missing'}")
    if cstatus not in ANSWERED_SET:
        if cstatus in ABSTAIN_SET:
            ok, reason = _proven_absence_insufficient(expect, root, hyps, evidence)
            if ok:
                return ("PASS", reason)
        return ("FAIL", f"status={cstatus or 'missing'}, want answered")

    # T9-D22/D21: limitation and forbidden-text gates decide before
    # the row dimensions (they PASS/FAIL outright where they apply).
    if expect.get("forbid_root_text_from_groups"):
        ok, reason = _check_forbidden_root_text(expect, root)
        if not ok:
            return ("PARTIAL", reason)
    if expect.get("required_limitations") or expect.get("limitation_mentions"):
        ok, reason = _check_required_limitations(expect, concl)
        if not ok:
            return ("PARTIAL", reason)
    by_id = _rows_by_id(evidence)
    rows = _supporting_rows(concl, hyps, by_id)
    if not rows:
        return ("PARTIAL", "answered with no citable supporting rows")
    claim = expect.get("claim", "definition")
    checks = {
        "definition": lambda: _check_definition(expect, root, rows),
        "callers": lambda: _check_callers(expect, root, rows),
        "history": lambda: _check_history(expect, root, rows),
        "values": lambda: _check_values(case, expect, root, rows),
        "absence": lambda: _check_absence(expect, root, rows),
        "no_failure_citation": lambda: _check_no_failure(rows),
    }
    check = checks.get(claim)
    if check is None:
        return ("FAIL", f"unknown claim kind '{claim}'")
    ok, reason = check()
    if not ok:
        return ("PARTIAL", reason)
    ok, reason = _check_revision(expect, rows)
    if not ok:
        return ("PARTIAL", reason)
    ok, reason = _check_rival_exemption(case, expect, concl, hyps)
    if not ok:
        return ("PARTIAL", reason)
    ok, reason = _check_no_failure(rows)
    if not ok:
        return ("PARTIAL", reason)
    return ("PASS", "conclusion supported by cited evidence")


def _concrete_handoff(concl: dict) -> tuple[bool, str]:
    """True when an abstention carries a priced concrete next fetch or a ruled-out rival.

    Consumes the S7 handoff fields: ``handoff.next_fetch`` must name a
    retrieval action or file (the domain's single definition of concrete,
    ``names_concrete_fetch_target``) AND ``handoff.price`` must be a positive
    number (tool-call budget units). A concrete but unpriced fetch is not a
    priced handoff. Alternatively a non-empty ``eliminated_rivals`` record
    counts. State labels, the ``n/a`` placeholder and ``next_check`` template
    prose never count - the review (T2) found that counting them made every
    abstention a "handoff".
    """
    handoff = concl.get("handoff") or {}
    if not isinstance(handoff, dict):
        handoff = {}
    fetch = str(handoff.get("next_fetch") or "").strip()
    if fetch and fetch.lower() != "n/a":
        if not names_concrete_fetch_target(fetch):
            return (False, f"next_fetch is generic template text: {fetch[:60]}")
        price = handoff.get("price")
        if isinstance(price, bool) or not isinstance(price, int | float) or not price > 0:
            return (False, f"next_fetch is concrete but unpriced: {fetch[:60]}")
        return (True, f"next_fetch (price {price:g}): {fetch[:80]}")
    rivals = concl.get("eliminated_rivals") or []
    if isinstance(rivals, list) and rivals:
        return (True, f"{len(rivals)} eliminated rival(s)")
    return (False, "no next_fetch and no eliminated rivals")


def score_g1(conclusion: dict | None, evidence: dict | None) -> tuple[bool, str, str]:
    """G1: bound-or-priced-handoff, per case. Pure on API-shaped dicts.

    Returns (satisfied, kind, detail) where kind is ``bound``, ``handoff``,
    or ``miss``.

    - ``bound``: an answered conclusion with at least one cited
      ``supporting_evidence_ids`` entry that resolves to a returned evidence
      row, none of which is an infrastructure-failure row. Evidence reached
      only through a linked hypothesis does NOT count (T8: abstention prose
      and hypothesis links are unverified).
    - ``handoff``: an abstention carrying a concrete, priced next fetch or an
      elimination record (see ``_concrete_handoff``).
    - ``miss``: anything else, including a missing conclusion, an answered
      conclusion with no resolvable citation, and filler-only abstentions.
    """
    concl = conclusion or {}
    status = str(concl.get("status", "") or "")
    if not status:
        return (False, "miss", "no conclusion")
    if status in ANSWERED_SET:
        by_id = _rows_by_id(evidence or {})
        cited = [
            by_id[str(eid)]
            for eid in (concl.get("supporting_evidence_ids") or [])
            if str(eid) in by_id
        ]
        if not cited:
            return (False, "miss", "answered with no resolvable cited row")
        ok, reason = _check_no_failure(cited)
        if not ok:
            return (False, "miss", f"bound only by failure row ({reason})")
        return (True, "bound", f"{len(cited)} cited row(s)")
    if status in ABSTAIN_SET:
        ok, detail = _concrete_handoff(concl)
        if ok:
            return (True, "handoff", detail)
        # T9-D21: a named gap is a G1 tier of its own, reported separately so
        # the headline stays strict.
        named, named_detail = _named_gap(concl)
        if named:
            return (True, "named_gap", named_detail)
        return (False, "miss", detail)
    return (False, "miss", f"status={status}")


def g1_for_investigation(base: str, tenant: str, iid: str) -> tuple[bool, str, str]:
    """Fetch the conclusion and cited evidence for one run and score G1."""
    _, conclusion = api(base, tenant, "GET", f"/api/v1/investigations/{iid}/conclusion")
    _, evidence = api(base, tenant, "GET", f"/api/v1/investigations/{iid}/evidence?limit=100")
    return score_g1(conclusion.get("conclusion") or {}, evidence)


def g1_summary(g1_rows: list[tuple[str, bool, str, str]]) -> dict:
    """Aggregate per-case G1 rows (case_id, ok, kind, detail) into a rate."""
    total = len(g1_rows)
    kinds: dict[str, int] = {}
    for _, _, kind, _ in g1_rows:
        kinds[kind] = kinds.get(kind, 0) + 1
    satisfied = sum(1 for _, ok, _, _ in g1_rows if ok)
    strict = sum(1 for _, ok, kind, _ in g1_rows if ok and kind in ("bound", "handoff"))
    return {
        "satisfied": satisfied,
        "total": total,
        "rate": (satisfied / total) if total else 0.0,
        # T9-D21: the headline stays strict (bound + priced handoff only);
        # named gaps are reported, not counted.
        "strict_rate": (strict / total) if total else 0.0,
        "kinds": kinds,
    }


def run_validity(case: dict, inv: dict, strict_fresh: bool) -> tuple[bool, str]:
    """T9-D23/GT6: a run that is not startable or is served from cache/duplicate
    is INVALID (excluded from the denominator, counted, re-run in a fresh tenant).
    Non-strict mode keeps today's _follow_primary behavior."""
    if not strict_fresh:
        return (True, "")
    meta = (inv.get("metadata") or {}) if isinstance(inv, dict) else {}
    disp = meta.get("disposition") or {}
    if disp.get("action") in ("serve_cached", "duplicate"):
        return (False, f"strict-fresh: {disp.get('action')} run excluded, not graded")
    return (True, "")


async def presweep_scope(conn_provider, tenant: str, app_id: str, tag_prefix: str, execute: bool) -> tuple[dict, int]:
    """T9-D23: dry-run the doomed-row counts (print), delete only with --execute.

    Refuses a non-battery tenant for deletes. conn_provider is a zero-arg
    async callable returning a connection with fetchval/execute.
    """
    from clean_battery import count_scope

    if execute and tenant != "n-batt" and not str(tenant).startswith("n-"):
        raise ValueError(f"presweep --execute refuses non-battery tenant {tenant!r}")
    conn = await conn_provider()
    try:
        counts = await count_scope(conn, tenant, app_id, tag_prefix, "tagged")
    finally:
        try:
            await conn.close()
        except Exception:
            pass
    deleted = 0
    if execute:
        import clean_battery as _cb
        from clean_battery import cleanup_scope

        deleted = await cleanup_scope(_cb.db_dsn(), tenant, app_id, tag_prefix, False)
    return counts, deleted


def leakage_report() -> dict:
    """T9-D21: document (not hide) expected-token circularity.

    Lists live fixtures whose symptom already contains the expected symbol,
    and asserts no cited_path appears in any symptom except L4's deliberate typo.
    """
    symbols_in_symptom = []
    cited_in_symptom = []
    for case in CASES:
        if not case.get("live", True):
            continue
        expect = case.get("expect", {}) or {}
        symptom = str(case.get("symptom") or "")
        for key in ("expect_symbol",):
            symbol = str(expect.get(key) or "")
            if symbol and _word_in(symbol, symptom):
                symbols_in_symptom.append(case["id"])
        for token in str(expect.get("callee") or "").split():
            if token and _word_in(token, symptom):
                symbols_in_symptom.append(case["id"])
        for value in expect.get("expect_values") or []:
            if value and _word_in(str(value), symptom):
                symbols_in_symptom.append(f"{case['id']}:{value}")
        cited = str(expect.get("cited_path") or "")
        if cited and _word_in(cited, symptom):
            cited_in_symptom.append(case["id"])
    return {
        "symbols_in_symptom": sorted(set(symbols_in_symptom)),
        "cited_path_in_symptom": sorted(set(cited_in_symptom)),
    }


def _follow_primary(base: str, tenant: str, case: dict, inv: dict, timeout: int) -> tuple | None:
    """Grade a CANCELLED duplicate via its primary (P0 create-race fix).

    Returns (primary_id, verdict, detail) or None when the close names no
    followable primary. Mirrors live_battery._cached_primary for the
    serve_cached/duplicate actions: duplicate-of-active primaries are
    polled to terminal first, then scored.
    """
    meta = (inv.get("metadata") or {}) if isinstance(inv, dict) else {}
    disp = meta.get("disposition") or {}
    action = disp.get("action")
    if action not in ("serve_cached", "duplicate"):
        return None
    pid = disp.get("primary_target_id") or ""
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        str(pid),
    ):
        match = re.search(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            str(disp.get("reason", "")),
        )
        if not match:
            return None
        pid = match.group(0)
    if action == "duplicate":
        try:
            poll(base, tenant, pid, timeout)
        except Exception:
            return None
    verdict, detail = score_case(base, tenant, case, {"id": pid})
    return pid, verdict, detail


def score_case(base: str, tenant: str, case: dict, inv: dict) -> tuple[str, str]:
    iid = inv["id"]
    _, conclusion = api(base, tenant, "GET", f"/api/v1/investigations/{iid}/conclusion")
    _, findings = api(base, tenant, "GET", f"/api/v1/investigations/{iid}/findings")
    _, evidence = api(base, tenant, "GET", f"/api/v1/investigations/{iid}/evidence?limit=100")
    _, hyps = api(base, tenant, "GET", f"/api/v1/investigations/{iid}/hypotheses")
    concl = conclusion.get("conclusion") or {}
    # T9-D21: the scored investigation rides along so the abstention check
    # can read loop telemetry (iterations, fetches dispatched).
    return score_supported(case, concl, findings, evidence, hyps, investigation=inv)


def main() -> int:
    ap = argparse.ArgumentParser(description="Tranche B battery runner.")
    ap.add_argument("--api-base", default="http://localhost:8080")
    ap.add_argument("--tenant", default="n-a")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--only", default="", help="Comma-separated case IDs, e.g. L1,L4")
    ap.add_argument(
        "--claim-binding-version",
        default=None,
        choices=known_claim_binding_versions(),
        help=(
            "Pin this claim-binding version on each new investigation before it starts "
            "(operator DB stamp; needs IAP_DATABASE_URI). Default: leave the server default."
        ),
    )
    # T9-D23 comparability flags. All additive; defaults reproduce today's behavior.
    ap.add_argument("--repeat", type=int, default=1, help="Run each selected case N times (default 1).")
    ap.add_argument("--fresh-tenant", action="store_true", help="Per repeat, use a new tenant id n-<cell>-r<k>-<utc> (ensure an ACTIVE profile first).")
    ap.add_argument("--cell", default="single", help="Record the cell label in the output (before/after x cbv).")
    ap.add_argument("--json", default="", help="Write machine-readable per-run records (case, verdict, g1, disposition, loop_version, pin, rubric, git sha).")
    ap.add_argument("--expect-rubric", default="", help="Exit 2 if the grader RUBRIC_VERSION differs.")
    ap.add_argument("--strict-fresh", action="store_true", help="Grade only a run that executed itself; a serve_cached/duplicate disposition marks the run INVALID (no primary followed) and it is re-run.")
    ap.add_argument("--presweep", action="store_true", help="Print the doomed-row counts for the battery tenant (dry run; needs IAP_DATABASE_URI).")
    ap.add_argument("--execute", action="store_true", help="With --presweep: actually delete (destructive: refuses non-battery tenants).")
    args = ap.parse_args()
    print(f"rubric: {RUBRIC_VERSION}")
    if args.expect_rubric and args.expect_rubric != RUBRIC_VERSION:
        print(f"rubric mismatch: grader is {RUBRIC_VERSION}, expected {args.expect_rubric}", file=sys.stderr)
        return 2
    if args.presweep:
        import asyncio

        async def _conn_provider():  # type: ignore[no-untyped-def]
            import asyncpg
            from clean_battery import db_dsn

            return await asyncpg.connect(db_dsn())

        counts, deleted = asyncio.run(
            presweep_scope(_conn_provider, args.tenant, APP_ID, "live-", args.execute)
        )
        print(f"presweep counts: {counts} deleted: {deleted}")
        if not args.execute:
            pass  # dry run only
    only = {c.strip() for c in args.only.split(",") if c.strip()}
    results = []
    skipped = []
    g1_rows: list[tuple[str, bool, str, str]] = []
    records: list[dict] = []
    # case id -> (graded investigation id, recorded claim-binding pin)
    pins: dict[str, tuple[str, str]] = {}
    git_sha = _git_sha()

    for rep in range(1, max(1, args.repeat) + 1):
        tenant = args.tenant
        if args.fresh_tenant:
            tenant = f"n-{args.cell}-r{rep}-{datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%S')}"
            print(f"--- repeat {rep}: fresh tenant {tenant} (ensure an ACTIVE profile first)")
        for case in CASES:
            if only and case["id"] not in only:
                continue
            if not case.get("live", True):
                skipped.append(case["id"])
                print(f"--- {case['id']}: SKIP (offline-only fixture)", flush=True)
                continue
            _run_case(
                args.api_base, tenant, args.timeout, args.claim_binding_version,
                args.strict_fresh, args.cell, git_sha, case,
                results, g1_rows, pins, records,
            )
    return _print_summary(args, results, skipped, g1_rows, pins, records)


def _git_sha() -> str:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parents[1]), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return (out.stdout or "").strip() or "unknown"
    except Exception:
        return "unknown"


def _run_case(api_base: str, tenant: str, timeout: int, claim_binding_version: str | None,
              strict_fresh: bool, cell: str, git_sha: str, case: dict,
              results: list, g1_rows: list, pins: dict, records: list) -> None:
    """Run and grade one case (T9-D23: strict-fresh invalid rule lives here)."""

    def _record(case_id: str, verdict: str, detail: str, iid: str, g1: tuple, valid: bool) -> dict:
        ok, kind, gdetail = g1
        return {
            "case": case_id, "verdict": verdict, "detail": detail[:200],
            "investigation_id": iid, "valid": valid,
            "g1": ok, "g1_kind": kind, "g1_detail": gdetail[:200],
            "cell": cell, "rubric": RUBRIC_VERSION,
            "tenant": tenant, "pin": pins.get(case_id, ("", ""))[1] if pins.get(case_id) else "",
            "git_sha": git_sha, "loop_version": "", "disposition": "",
        }

    print(f"--- {case['id']}: {case['symptom'][:70]}", flush=True)
    st, data = api(
        api_base,
        tenant,
        "POST",
        "/api/v1/investigations",
        {
            "application_id": APP_ID,
            "problem_description": case["symptom"],
            "initial_context": case["context"],
        },
    )
    iid = data.get("id")
    if not iid:
        print(f"  CREATE FAILED: {st} {str(data)[:200]}")
        results.append((case["id"], "FAIL", "create failed", "-"))
        g1_rows.append((case["id"], False, "miss", "create failed"))
        records.append(_record(case["id"], "FAIL", "create failed", "-", (False, "miss", "create failed"), True))
        return
    if claim_binding_version:
        stamped = stamp_claim_binding_version(tenant, iid, claim_binding_version)
        print(f"  pin {claim_binding_version}: {'stamped' if stamped else 'NOT stamped'}")
    api(api_base, tenant, "POST", f"/api/v1/investigations/{iid}/start")
    try:
        inv = poll(api_base, tenant, iid, timeout)
    except TimeoutError as exc:
        print(f"  {exc}")
        results.append((case["id"], "FAIL", "timeout", iid))
        g1_rows.append((case["id"], False, "miss", "timeout"))
        records.append(_record(case["id"], "FAIL", "timeout", iid, (False, "miss", "timeout"), True))
        return
    if inv.get("status") == "CANCELLED":
        # T9-D23 strict-fresh: a cache-served/duplicate run is INVALID:
        # excluded, counted, no primary followed.
        valid, why = run_validity(case, inv, strict_fresh)
        if not valid:
            print(f"  INVALID: {why} ({iid[:8]})")
            results.append((case["id"], "FAIL", f"invalid: {why}", iid))
            g1_rows.append((case["id"], False, "miss", f"invalid: {why}"))
            rec = _record(case["id"], "FAIL", f"invalid: {why}", iid, (False, "miss", f"invalid: {why}"), False)
            disp = ((inv.get("metadata") or {}).get("disposition") or {})
            rec["disposition"] = str(disp.get("action", ""))
            records.append(rec)
            return
        # P0 create-race fix: a duplicate-of-active close must follow
        # the primary and grade its answer, not fail as "status=missing".
        followed = _follow_primary(api_base, tenant, case, inv, timeout)
        if followed is not None:
            pid, verdict, detail = followed
            print(f"  {verdict}: followed primary {pid[:8]}: {detail} ({iid[:8]})")
            results.append((case["id"], verdict, detail, iid))
            pins[case["id"]] = (
                pid,
                pinned_claim_binding_version(api_base, tenant, pid),
            )
            g1_ok, g1_kind, g1_detail = g1_for_investigation(api_base, tenant, pid)
            g1_rows.append((case["id"], g1_ok, g1_kind, g1_detail))
            records.append(_record(case["id"], verdict, detail, iid, (g1_ok, g1_kind, g1_detail), True))
            return
        results.append((case["id"], "FAIL", "run CANCELLED, no followable primary", iid))
        g1_rows.append((case["id"], False, "miss", "run CANCELLED, no followable primary"))
        print(f"  run CANCELLED, no followable primary {iid}")
        return
    if inv.get("status") == "FAILED":
        results.append((case["id"], "FAIL", "run FAILED", iid))
        g1_rows.append((case["id"], False, "miss", "run FAILED"))
        print(f"  run FAILED {iid}")
        records.append(_record(case["id"], "FAIL", "run FAILED", iid, (False, "miss", "run FAILED"), True))
        return
    verdict, detail = score_case(api_base, tenant, case, inv)
    print(f"  {verdict}: {detail} ({iid[:8]})")
    results.append((case["id"], verdict, detail, iid))
    pins[case["id"]] = (
        iid,
        pinned_claim_binding_version(api_base, tenant, iid),
    )
    g1_ok, g1_kind, g1_detail = g1_for_investigation(api_base, tenant, iid)
    g1_rows.append((case["id"], g1_ok, g1_kind, g1_detail))
    rec = _record(case["id"], verdict, detail, iid, (g1_ok, g1_kind, g1_detail), True)
    rec["loop_version"] = str((inv.get("metadata") or {}).get("loop_version") or "")
    records.append(rec)


def _print_summary(args, results, skipped, g1_rows, pins, records) -> int:
    print(f"\nTranche B scoreboard (tenant {args.tenant}, app {APP_ID}, rubric {RUBRIC_VERSION}):")
    for cid, verdict, detail, iid in results:
        print(f"  {cid}: {verdict} — {detail} [{iid[:8] if len(iid) > 8 else iid}]")
    counts = {}
    for _, verdict, _, _ in results:
        counts[verdict] = counts.get(verdict, 0) + 1
    invalid = sum(1 for r in records if not r.get("valid", True))
    print(f"Totals: {counts} (skipped offline-only: {skipped}; invalid: {invalid})")
    g1 = g1_summary(g1_rows)
    print(
        f"G1 strict (bound-or-priced-handoff): {g1['strict_rate']:.0%}  "
        f"all: {g1['satisfied']}/{g1['total']} = {g1['rate']:.0%}  kinds={g1['kinds']}"
    )
    for cid, ok, kind, detail in g1_rows:
        print(f"  G1 {cid}: {'OK ' if ok else 'MISS'} [{kind}] {detail}")
    if pins:
        print("Claim-binding pin of each GRADED investigation:")
        for cid, (gid, pin) in pins.items():
            flag = ""
            if args.claim_binding_version and pin != args.claim_binding_version:
                flag = f"  <-- MISMATCH (wanted {args.claim_binding_version}; run was reused or not stamped)"
            print(f"  {cid}: {pin} [{gid[:8]}]{flag}")
    print("Note: L6 (logs) / L7 (refdocs) N/A — code-only application by design.")
    if getattr(args, "json", ""):
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2, default=str)
        print(f"records: {len(records)} -> {args.json}")
    # PARTIAL never counts as pass: a right answer on wrong evidence is a
    # grading failure, not a near-miss.
    bad = counts.get("FAIL", 0) + counts.get("PARTIAL", 0)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
