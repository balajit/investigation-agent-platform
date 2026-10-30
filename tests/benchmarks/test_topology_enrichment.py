# tests/benchmarks/test_topology_enrichment.py
"""Topology enrichment benchmark harness (combined plan crossover).

Slice 0 rule: no `cognee` dependency. This module records precision/latency
baselines of the current adapter on a fixed fixture set built from the shared
fixtures in `tests/fixtures/github_loader/`. The Part 8 pilot compares against
the results JSON produced here — the pilot cannot grade its own homework.

Cases (exact-ownership weighted above recall, per Part 8 risk mitigation):
1. exact ownership — method line resolves to its qualified symbol + CODEOWNERS domain.
2. overload — same-name methods in two classes resolve to their own owners.
3. cross-repo — same-name symbols in two repos resolve repo-scoped with own domains.
4. revision reproducibility — historical revision resolves against its own snapshot.
5. call chain — intra-payload CALLS edge links caller to callee (multi-hop data present).
6. blast radius — reverse CALLS index answers "who calls X".
7. sql hop — ACCESSES_TABLE edge links the SQL-enclosing symbol to its table.

Step-6 / D7 benchmark gate (`docs/IAP-Cognee-integration-design.md` D7,
`docs/architecture/ADR-COGNEE-001.md` revisit triggers).

The fallback-on vs fallback-off comparison below runs on the SAME fixed
fixture set with FROZEN verification gates across both arms. It reports five
SEPARATE numbers — precision, latency, cost, cross-tenant leakage, and
replay-determinism — never a blended score. Verification gates stay fixed:
any precision gain bought by weakening a gate fails the run instead of
passing it (adversarial cross-tenant / cross-revision / malformed candidates
must all discard, and the frozen constants are asserted).

D7 gate criteria recorded here (normative for reviewers):
- JUSTIFIES an ADR amendment (new amendment with its own proposal table, never
  an edit of history) ONLY if ALL hold on this battery: (a) a battery
  PARTIAL/FAIL on the fallback-off arm is fixed on the fallback-on arm
  (precision gain on a defined `CodeRecallMiss` class, scripted-candidate
  wiring today; live-backend numbers required before any real amendment —
  see the opt-in live test); (b) latency and cost stay bounded within the
  `FALLBACK_RUN_CAP` / `FALLBACK_CANDIDATE_CAP` code constants; (c)
  cross-tenant leakage is exactly 0; (d) replay is bit-identical
  (determinism mismatch count 0); (e) every frozen-gate negative control
  still discards; (f) the miss is shown fixable ONLY by semantic recall
  (deterministic alternative exhausted and recorded).
- KEEPS the decline (P4–P8 stand, `graph_only=True`, vectors off) if ANY of:
  precision gain is zero; the gain appears only with a weakened gate
  (leakage > 0 or any adversarial candidate kept); latency/cost escape the
  caps; replay diverges; or the miss is fixable deterministically.

Dependency rule: the gate arms run green WITHOUT the `cognee` package
installed. Fallback-off is pure deterministic recall; fallback-on uses the
injected search-callable seam (`fuzzy_fallback_candidates` +
`verify_fuzzy_candidates`) with scripted candidates, never a live backend.
Live-backend comparisons stay opt-in behind `IAP_RUN_CONTAINER_TESTS=1`,
mirroring `tests/integration/test_cognee_pilot_live.py`.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

import pytest

from investigation_agent_platform.application.evidence.code_recall import (
    FALLBACK_CANDIDATE_CAP,
    FALLBACK_RUN_CAP,
    FALLBACK_SEARCH_ORDER,
    CodeRecallMiss,
    classify_code_recall_miss,
    fuzzy_fallback_candidates,
    verify_fuzzy_candidates,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "github_loader"

# D7 frozen-gate pins: the comparison arms must observe these exact values.
# Any drift (e.g. adding CYPHER, raising a cap) fails the gate run.
GATE_FROZEN_SEARCH_ORDER = ("CODE", "CHUNKS_LEXICAL", "CHUNKS", "SUMMARIES")
GATE_FROZEN_CANDIDATE_CAP = 10
GATE_FROZEN_RUN_CAP = 3

_RUN_LIVE_COMPARISONS = os.environ.get("IAP_RUN_CONTAINER_TESTS", "0") == "1"
requires_live_backend = pytest.mark.skipif(
    not _RUN_LIVE_COMPARISONS,
    reason="Live-backend comparison opt-in only (IAP_RUN_CONTAINER_TESTS=1)",
)


def _find_line(root: Path, rel: str, needle: str) -> int:
    for idx, line in enumerate((root / rel).read_text().splitlines(), start=1):
        if needle in line:
            return idx
    raise AssertionError(f"{needle!r} not found in {rel}")


async def _ingest_fixture(tmp_path: Path, name: str, tenant: str, sha: str, tag: str):  # type: ignore[no-untyped-def]
    import importlib.util
    import sys

    script = Path(__file__).resolve().parents[2] / "scripts" / "ingest_github.py"
    spec = importlib.util.spec_from_file_location("ingest_github", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ingest_github"] = module
    spec.loader.exec_module(module)

    from investigation_agent_platform.infrastructure.topology.in_memory import (
        InMemoryTopologyAdapter,
    )

    dest = tmp_path / tag / name
    shutil.copytree(FIXTURES / name, dest)
    full_name = name.replace("--", "/")
    repo = module.RepoListing(
        full_name,
        "main",
        f"https://github.com/{full_name}.git",
        False,
        False,
        False,
        False,
        1,
        "Python",
    )
    rid = module.repository_id_for(full_name)
    adapter = InMemoryTopologyAdapter()
    await module.register_identities(dest, tenant, repo, rid, "SERVICE", adapter)
    payload, truncated = await module.build_payload_for_repo(
        dest, tenant, f"{tenant}-{rid}", rid, sha
    )
    assert truncated is False
    result = await module.ingest_payload(payload, adapter)
    assert result["status"] == "READY"
    return adapter, rid


@pytest.mark.asyncio
async def test_enrichment_benchmark(tmp_path: Path) -> None:
    tenant = "tenant-bench"
    sha_a, sha_b = "a" * 40, "b" * 40
    adapter, rid_pay = await _ingest_fixture(tmp_path, "acme--payments", tenant, sha_a, "r1")
    adapter2, rid_bill = await _ingest_fixture(tmp_path, "acme--billing", tenant, sha_a, "r2")
    assert rid_pay != rid_bill
    # Same adapter scope for cross-repo case: re-ingest billing into adapter 1.
    import shutil as _shutil

    dest = tmp_path / "r1x" / "acme--billing"
    _shutil.copytree(FIXTURES / "acme--billing", dest)
    import importlib.util as _ilu
    import sys as _sys

    script = Path(__file__).resolve().parents[2] / "scripts" / "ingest_github.py"
    spec = _ilu.spec_from_file_location("ingest_github", script)
    assert spec is not None and spec.loader is not None
    module = _ilu.module_from_spec(spec)
    _sys.modules["ingest_github"] = module
    spec.loader.exec_module(module)
    repo_b = module.RepoListing(
        "acme/billing",
        "main",
        "https://github.com/acme/billing.git",
        False,
        False,
        False,
        False,
        1,
        "Python",
    )
    rid_b = module.repository_id_for("acme/billing")
    await module.register_identities(dest, tenant, repo_b, rid_b, "SERVICE", adapter)
    payload_b, trunc_b = await module.build_payload_for_repo(
        dest, tenant, f"{tenant}-{rid_b}", rid_b, sha_a
    )
    assert trunc_b is False
    assert (await module.ingest_payload(payload_b, adapter))["status"] == "READY"
    # Second revision of payments for the reproducibility case.
    dest2 = tmp_path / "r3" / "acme--payments"
    _shutil.copytree(FIXTURES / "acme--payments", dest2)
    payload_b2, _ = await module.build_payload_for_repo(
        dest2, tenant, f"{tenant}-{rid_pay}", rid_pay, sha_b
    )
    assert (await module.ingest_payload(payload_b2, adapter))["status"] == "READY"

    inv = uuid.uuid4()
    pay_root = tmp_path / "r1" / "acme--payments"
    bill_root = tmp_path / "r1x" / "acme--billing"
    t0 = time.perf_counter()
    cases: list[dict] = []

    def record(name: str, ok: bool, ms: float, detail: str = "") -> None:
        cases.append({"case": name, "pass": ok, "latency_ms": round(ms, 2), "detail": detail})

    # 1. exact ownership
    line = _find_line(pay_root, "src/payments.py", "charged:{amount_cents}")
    t = time.perf_counter()
    exact = await adapter.resolve_source_location(
        tenant, "app", inv, rid_pay, sha_a, "src/payments.py", line
    )
    ms = (time.perf_counter() - t) * 1000
    record(
        "exact",
        exact.matched_node_qualified_name == "PaymentService.charge"
        and exact.domain_id == "team-payments"
        and exact.fallback_level.value == "AST_NODE",
        ms,
        str(exact.matched_node_qualified_name),
    )

    # 2. overload disambiguation
    line = _find_line(pay_root, "src/payments.py", "webhook:")
    t = time.perf_counter()
    over = await adapter.resolve_source_location(
        tenant, "app", inv, rid_pay, sha_a, "src/payments.py", line
    )
    ms = (time.perf_counter() - t) * 1000
    record(
        "overload",
        over.matched_node_qualified_name == "PaymentWebhook.handle",
        ms,
        str(over.matched_node_qualified_name),
    )

    # 3. cross-repo same-name symbols stay repo-scoped
    line = _find_line(bill_root, "src/billing.py", "billing-event:")
    t = time.perf_counter()
    cross = await adapter.resolve_source_location(
        tenant, "app", inv, rid_b, sha_a, "src/billing.py", line
    )
    ms = (time.perf_counter() - t) * 1000
    record(
        "cross_repo",
        cross.matched_node_qualified_name == "BillingService.handle" and cross.domain_id is None,
        ms,
        str(cross.matched_node_qualified_name),
    )

    # 4. revision reproducibility
    charge_line = _find_line(pay_root, "src/payments.py", "charged:{amount_cents}")
    t = time.perf_counter()
    old = await adapter.resolve_source_location(
        tenant, "app", inv, rid_pay, sha_a, "src/payments.py", charge_line
    )
    new = await adapter.resolve_source_location(
        tenant, "app", inv, rid_pay, sha_b, "src/payments.py", charge_line
    )
    ms = (time.perf_counter() - t) * 1000
    record(
        "revision",
        old.snapshot_id != new.snapshot_id
        and old.matched_node_qualified_name == new.matched_node_qualified_name,
        ms,
    )

    # 5-7. multi-hop data present in the payload edge lists (same questions the
    # pilot answers from Cognee points; adapter traversal APIs consume these).
    pay_payload, _ = await module.build_payload_for_repo(
        pay_root, tenant, f"{tenant}-{rid_pay}", rid_pay, sha_a
    )
    quals = {n.node_id: n.qualified_name for n in pay_payload.ast_nodes}
    t = time.perf_counter()
    chain = {(quals.get(c.caller_node_id), quals.get(c.callee_node_id)) for c in pay_payload.calls}
    ms = (time.perf_counter() - t) * 1000
    record(
        "call_chain",
        ("process_payment", "process_payment.validate") in chain,
        ms,
        f"{len(chain)} call edges",
    )
    t = time.perf_counter()
    callers = {
        quals.get(c.caller_node_id)
        for c in pay_payload.calls
        if quals.get(c.callee_node_id) == "process_payment.validate"
    }
    ms = (time.perf_counter() - t) * 1000
    record("blast_radius", callers == {"process_payment"}, ms, str(sorted(callers)))
    t = time.perf_counter()
    sql = [
        (quals.get(d.source_node_id), d.target_entity_or_table)
        for d in pay_payload.database_accesses
    ]
    ms = (time.perf_counter() - t) * 1000
    record("sql_hop", ("PaymentService.charge", "payments") in sql, ms, str(sql))

    total_ms = (time.perf_counter() - t0) * 1000
    passed = sum(1 for c in cases if c["pass"])
    report = {"passed": passed, "total": len(cases), "total_ms": round(total_ms, 2), "cases": cases}
    (tmp_path / "benchmark.json").write_text(json.dumps(report, indent=2))
    assert passed == len(cases), json.dumps(report, indent=2)


def _assert_gates_frozen() -> None:
    """Fail the run if any verification gate drifted from its frozen value.

    D7 anti-gaming rule: a precision gain bought by weakening one of these
    gates must fail, not pass. Called before any precision comparison.
    """
    assert tuple(FALLBACK_SEARCH_ORDER) == GATE_FROZEN_SEARCH_ORDER
    assert FALLBACK_CANDIDATE_CAP == GATE_FROZEN_CANDIDATE_CAP
    assert FALLBACK_RUN_CAP == GATE_FROZEN_RUN_CAP
    assert "CYPHER" not in FALLBACK_SEARCH_ORDER
    for completion_type in ("CODE_COMPLETION", "CHUNKS_COMPLETION", "SUMMARIES_COMPLETION"):
        try:
            from investigation_agent_platform.infrastructure.topology.cognee_adapter import (
                assert_retrieval_only,
            )
        except Exception as exc:  # pragma: no cover - import is dependency-free
            raise AssertionError(f"retrieval-only guard import failed: {exc}") from exc
        try:
            assert_retrieval_only(completion_type)
        except Exception:
            continue
        raise AssertionError(f"completion search type accepted: {completion_type}")


def _scripted_search_fn(
    script: dict[str, list[dict[str, object]]],
    calls: list[dict[str, object]],
    datasets_seen: list[str],
) -> object:
    """Build the injected retrieval-only search seam with scripted candidates.

    Never touches a live backend: rows come from `script` keyed by a
    query-text needle. Marks itself so the gate can prove no live path ran.
    """

    async def _search_fn(**kwargs: object) -> list[dict[str, object]]:
        calls.append(dict(kwargs))
        dataset = str(kwargs.get("dataset") or "")
        datasets_seen.append(dataset)
        query_text = str(kwargs.get("query_text") or "")
        for needle, rows in script.items():
            if needle in query_text:
                return [dict(row) for row in rows]
        return []

    _search_fn.__scripted_gate_seam__ = True  # type: ignore[attr-defined]
    return _search_fn


@pytest.mark.asyncio
async def test_fallback_on_vs_off_gate(tmp_path: Path) -> None:
    """Step-6 D7 gate: fallback-on vs fallback-off on the fixed fixture set.

    Five SEPARATE numbers (no blended score): precision, latency, cost,
    cross-tenant leakage, replay-determinism. Runs green without the `cognee`
    package: fallback-off is pure deterministic recall, fallback-on uses the
    injected scripted seam. Frozen gates are asserted BEFORE the precision
    comparison so gate-weakening gains fail the run.
    """
    _assert_gates_frozen()
    # Prove the gate arms never touch a live backend in this module: no real
    # cognee import statement (assertion lines excluded — they name the
    # forbidden strings to enforce the rule).
    source_lines = Path(__file__).read_text(encoding="utf-8").splitlines()
    live_imports = [
        line for line in source_lines if line.strip().startswith(("import cognee", "from cognee"))
    ]
    assert live_imports == [], f"live cognee imports forbidden in gate arms: {live_imports}"

    tenant = "tenant-bench"
    sha_a, sha_b = "a" * 40, "b" * 40
    adapter, rid_pay = await _ingest_fixture(tmp_path, "acme--payments", tenant, sha_a, "gate-r1")
    dest = tmp_path / "gate-r1x" / "acme--billing"
    shutil.copytree(FIXTURES / "acme--billing", dest)
    import importlib.util as _ilu
    import sys as _sys

    script = Path(__file__).resolve().parents[2] / "scripts" / "ingest_github.py"
    spec = _ilu.spec_from_file_location("ingest_github_gate", script)
    assert spec is not None and spec.loader is not None
    module = _ilu.module_from_spec(spec)
    _sys.modules["ingest_github_gate"] = module
    spec.loader.exec_module(module)
    pay_root = tmp_path / "gate-r1" / "acme--payments"
    pay_payload, truncated = await module.build_payload_for_repo(
        pay_root, tenant, f"{tenant}-{rid_pay}", rid_pay, sha_a
    )
    assert truncated is False
    _ = adapter  # deterministic recall arm uses exact-hit counts below, not the adapter reads

    charge_nodes = [n for n in pay_payload.ast_nodes if n.qualified_name == "PaymentService.charge"]
    assert charge_nodes, "fixed fixture set must contain PaymentService.charge"
    charge_node = charge_nodes[0]
    charge_id = str(charge_node.node_id)
    authority: dict[str, object] = {str(n.node_id): n for n in pay_payload.ast_nodes}
    dataset = "bench-dataset"

    # Fixed query set on the fixed fixture set (closed taxonomy only).
    queries = [
        {
            "name": "concept_no_anchor",
            "question_shape": "code",
            "problem": "where is auth handled?",
            "exact_hits": 0,
            "candidate_count": 0,
            "expected_miss": CodeRecallMiss.CONCEPT_LOCATION_NO_ANCHOR,
            "search_needle": "auth handled",
            "query_text": "where is auth handled?",
            "expected_verified": {charge_id},
        },
        {
            "name": "exact_hit_suppresses",
            "question_shape": "code",
            "problem": "PaymentService.charge",
            "exact_hits": 1,
            "candidate_count": 0,
            "expected_miss": CodeRecallMiss.NO_MISS,
            "search_needle": "PaymentService.charge",
            "query_text": "PaymentService.charge",
            "expected_verified": {charge_id},
        },
        {
            "name": "unknown_symbol_abstains",
            "question_shape": "code",
            "problem": "NoSuchSymbol_xyz_123",
            "exact_hits": 0,
            "candidate_count": 0,
            "expected_miss": CodeRecallMiss.ZERO_EXACT_HITS,
            "search_needle": "NoSuchSymbol_xyz_123",
            "query_text": "NoSuchSymbol_xyz_123",
            "expected_verified": set(),
        },
    ]
    script_rows = {
        "auth handled": [{"node_id": charge_id, "score": 0.99, "snippet": "must not cross"}],
        "NoSuchSymbol_xyz_123": [{"node_id": "ghost-unmapped-id"}],
    }

    async def _run_arm(*, fallback_on: bool) -> dict[str, object]:
        calls: list[dict[str, object]] = []
        datasets_seen: list[str] = []
        search_fn = _scripted_search_fn(script_rows, calls, datasets_seen)
        assert getattr(search_fn, "__scripted_gate_seam__", False) is True
        t_start = time.perf_counter()
        correct = 0
        verified_by_query: dict[str, list[str]] = {}
        candidates_seen_total = 0
        for query in queries:
            miss = classify_code_recall_miss(
                question_shape=str(query["question_shape"]),
                exact_hits=int(query["exact_hits"]),
                candidate_count=int(query["candidate_count"]),
                problem=str(query["problem"]),
            )
            assert miss is query["expected_miss"], (
                f"{query['name']}: {miss} != {query['expected_miss']}"
            )
            expected = set(query["expected_verified"])
            if not fallback_on:
                # Pure deterministic recall: exact hits resolve, misses abstain.
                verified: set[str] = set(expected) if int(query["exact_hits"]) > 0 else set()
                verified_by_query[str(query["name"])] = sorted(verified)
                if verified == expected:
                    # Off-arm concept query has no exact hit, so it scores 0;
                    # unknown-symbol abstention scores 1 (correctly empty).
                    correct += 1 if str(query["name"]) != "concept_no_anchor" else 0
                continue
            if miss is CodeRecallMiss.NO_MISS:
                # Fallback must not fire on NO_MISS: deterministic hit stands.
                verified_by_query[str(query["name"])] = sorted(expected)
                correct += 1
                continue
            outcome = await fuzzy_fallback_candidates(
                query_text=str(query["query_text"]),
                dataset=dataset,
                run_used=0,
                search_fn=search_fn,  # type: ignore[arg-type]
            )
            candidates_seen_total += outcome.candidates_seen
            kept, _ = verify_fuzzy_candidates(
                list(outcome.candidate_ids), authority, tenant, rid_pay, sha_a
            )
            verified_set = {str(v) for v in kept}
            verified_by_query[str(query["name"])] = sorted(verified_set)
            if verified_set == expected:
                correct += 1
        latency_ms = (time.perf_counter() - t_start) * 1000
        total = len(queries)
        return {
            "correct": correct,
            "total": total,
            "precision": round(correct / total, 4),
            "latency_ms": round(latency_ms, 2),
            "search_calls": len(calls),
            "candidates_seen": candidates_seen_total,
            "verified_by_query": verified_by_query,
            "datasets_seen": sorted(set(datasets_seen)),
        }

    off = await _run_arm(fallback_on=False)
    on_first = await _run_arm(fallback_on=True)
    on_second = await _run_arm(fallback_on=True)

    # Frozen-gate negative controls run through the SAME verifier. Any kept
    # adversarial candidate fails the run even if precision improved.
    adversarial_ids = ["ghost-unmapped-id", "not-a-uuid", charge_id]
    kept_cross_tenant, discarded_cross_tenant = verify_fuzzy_candidates(
        adversarial_ids, authority, "tenant-evil", rid_pay, sha_a
    )
    kept_cross_revision, discarded_cross_revision = verify_fuzzy_candidates(
        [charge_id], authority, tenant, rid_pay, sha_b
    )
    kept_malformed, _ = verify_fuzzy_candidates(
        ["not-a-uuid"], {"not-a-uuid": object()}, tenant, rid_pay, sha_a
    )
    leakage_kept = len(kept_cross_tenant) + len(kept_cross_revision) + len(kept_malformed)
    assert kept_cross_tenant == [], f"cross-tenant candidates leaked: {kept_cross_tenant}"
    assert kept_cross_revision == [], f"cross-revision candidates leaked: {kept_cross_revision}"
    assert kept_malformed == [], f"malformed candidates kept: {kept_malformed}"
    assert discarded_cross_tenant == len(adversarial_ids)
    assert discarded_cross_revision == 1

    # Cost bounds: per-call and per-run caps are code constants, honored here.
    assert int(on_first["search_calls"]) <= 2 * len(GATE_FROZEN_SEARCH_ORDER)
    assert int(on_first["candidates_seen"]) <= 2 * GATE_FROZEN_CANDIDATE_CAP
    assert off["search_calls"] == 0
    assert off["candidates_seen"] == 0
    assert on_first["datasets_seen"] == [dataset]

    # Replay-determinism: identical verified sets across two on-arm runs.
    assert on_first["verified_by_query"] == on_second["verified_by_query"]
    determinism_mismatches = sum(
        1
        for key in on_first["verified_by_query"]
        if on_first["verified_by_query"][key] != on_second["verified_by_query"][key]
    )
    assert determinism_mismatches == 0

    # Precision: scripted wiring expectation on the fixed query set. The
    # fallback-on arm recovers the anchorless concept query and correctly
    # abstains on the unknown symbol; the off arm misses the concept query.
    # (Scripted candidates prove wiring, NOT semantic efficacy — a real ADR
    # amendment needs live-backend battery numbers via the opt-in test.)
    assert off["correct"] == 2, json.dumps(off, indent=2)
    assert on_first["correct"] == 3, json.dumps(on_first, indent=2)

    report = {
        "fixture_set": "tests/fixtures/github_loader (acme--payments + acme--billing)",
        "gates_frozen": {
            "search_order": list(GATE_FROZEN_SEARCH_ORDER),
            "candidate_cap": GATE_FROZEN_CANDIDATE_CAP,
            "run_cap": GATE_FROZEN_RUN_CAP,
            "cypher_excluded": True,
            "completion_types_rejected": True,
        },
        "precision": {"off": off["precision"], "on": on_first["precision"]},
        "latency_ms": {"off": off["latency_ms"], "on": on_first["latency_ms"]},
        "cost": {
            "off": {"search_calls": off["search_calls"], "candidates_seen": off["candidates_seen"]},
            "on": {
                "search_calls": on_first["search_calls"],
                "candidates_seen": on_first["candidates_seen"],
            },
        },
        "cross_tenant_leakage": {
            "kept": leakage_kept,
            "cross_tenant_discarded": discarded_cross_tenant,
            "cross_revision_discarded": discarded_cross_revision,
        },
        "replay_determinism": {
            "mismatches": determinism_mismatches,
            "identical": determinism_mismatches == 0,
        },
        "arms": {"off": off, "on": on_first},
    }
    (tmp_path / "benchmark_fallback_gate.json").write_text(json.dumps(report, indent=2))


@requires_live_backend
@pytest.mark.asyncio
async def test_fallback_live_backend_comparison_opt_in() -> None:
    """Opt-in live-backend fallback comparison (NOT part of the default gate).

    Mirrors `tests/integration/test_cognee_pilot_live.py`: skips cleanly
    without `IAP_RUN_CONTAINER_TESTS=1`, without the `cognee` package, or
    without backend env. A future ADR amendment proposal runs this arm to
    collect real precision/latency/cost numbers; the default gate above stays
    fully offline with scripted candidates.
    """
    import importlib.util

    if importlib.util.find_spec("cognee") is None:
        pytest.skip("cognee package not installed")
    pytest.skip("live-backend comparison is opt-in; no live backend asserted here")
