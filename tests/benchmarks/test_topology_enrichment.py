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
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "github_loader"


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
    repo = module.RepoListing(full_name, "main", f"https://github.com/{full_name}.git",
                              False, False, False, False, 1, "Python")
    rid = module.repository_id_for(full_name)
    adapter = InMemoryTopologyAdapter()
    await module.register_identities(dest, tenant, repo, rid, "SERVICE", adapter)
    payload, truncated = await module.build_payload_for_repo(
        dest, tenant, f"{tenant}-{rid}", rid, sha)
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
    repo_b = module.RepoListing("acme/billing", "main", "https://github.com/acme/billing.git",
                                False, False, False, False, 1, "Python")
    rid_b = module.repository_id_for("acme/billing")
    await module.register_identities(dest, tenant, repo_b, rid_b, "SERVICE", adapter)
    payload_b, trunc_b = await module.build_payload_for_repo(
        dest, tenant, f"{tenant}-{rid_b}", rid_b, sha_a)
    assert trunc_b is False
    assert (await module.ingest_payload(payload_b, adapter))["status"] == "READY"
    # Second revision of payments for the reproducibility case.
    dest2 = tmp_path / "r3" / "acme--payments"
    _shutil.copytree(FIXTURES / "acme--payments", dest2)
    payload_b2, _ = await module.build_payload_for_repo(
        dest2, tenant, f"{tenant}-{rid_pay}", rid_pay, sha_b)
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
    exact = await adapter.resolve_source_location(tenant, "app", inv, rid_pay, sha_a,
                                                  "src/payments.py", line)
    ms = (time.perf_counter() - t) * 1000
    record("exact", exact.matched_node_qualified_name == "PaymentService.charge"
           and exact.domain_id == "team-payments"
           and exact.fallback_level.value == "AST_NODE", ms, str(exact.matched_node_qualified_name))

    # 2. overload disambiguation
    line = _find_line(pay_root, "src/payments.py", "webhook:")
    t = time.perf_counter()
    over = await adapter.resolve_source_location(tenant, "app", inv, rid_pay, sha_a,
                                                 "src/payments.py", line)
    ms = (time.perf_counter() - t) * 1000
    record("overload", over.matched_node_qualified_name == "PaymentWebhook.handle", ms,
           str(over.matched_node_qualified_name))

    # 3. cross-repo same-name symbols stay repo-scoped
    line = _find_line(bill_root, "src/billing.py", "billing-event:")
    t = time.perf_counter()
    cross = await adapter.resolve_source_location(tenant, "app", inv, rid_b, sha_a,
                                                  "src/billing.py", line)
    ms = (time.perf_counter() - t) * 1000
    record("cross_repo", cross.matched_node_qualified_name == "BillingService.handle"
           and cross.domain_id is None, ms, str(cross.matched_node_qualified_name))

    # 4. revision reproducibility
    charge_line = _find_line(pay_root, "src/payments.py", "charged:{amount_cents}")
    t = time.perf_counter()
    old = await adapter.resolve_source_location(tenant, "app", inv, rid_pay, sha_a,
                                                "src/payments.py", charge_line)
    new = await adapter.resolve_source_location(tenant, "app", inv, rid_pay, sha_b,
                                                "src/payments.py", charge_line)
    ms = (time.perf_counter() - t) * 1000
    record("revision", old.snapshot_id != new.snapshot_id
           and old.matched_node_qualified_name == new.matched_node_qualified_name, ms)

    total_ms = (time.perf_counter() - t0) * 1000
    passed = sum(1 for c in cases if c["pass"])
    report = {"passed": passed, "total": len(cases), "total_ms": round(total_ms, 2), "cases": cases}
    (tmp_path / "benchmark.json").write_text(json.dumps(report, indent=2))
    assert passed == len(cases), json.dumps(report, indent=2)
