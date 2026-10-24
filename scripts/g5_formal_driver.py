#!/usr/bin/env python3
"""G5-formal operator driver (WP-C2/G5 Step 4 execution).

Runs the live-DB interleave + role-review protocol against a staging /
quiet live-test database and prints a verdict block. Read-only except:
fixture hypothesis rows it creates, the interleave writes themselves,
and optional test-role create/drop. Never touches production: aborts
when the database name looks like production unless --force is given.

Usage:
  G5_ADMIN_DSN='postgresql://admin@host:port/db' \\
  G5_DSN='postgresql://iap_test1@host:port/db' \\
  .venv/bin/python scripts/g5_formal_driver.py --tenant T --create-test-role

  The tenant key travels only on the command line / environment of this
  invocation. This file contains no tenant identifiers and no secrets.
  DSNs are never printed (host/db only) and never written to the report.

Exit code 0 = every check green. Anything else = HOLD; see verdict.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_TENANT_RE = re.compile(r"^[A-Za-z0-9_.\-:]{1,128}$")

CASE1_A_PAYLOAD = "g5formal-case1-winner"
CASE1_B_PAYLOAD = "g5formal-case1-loser"
CASE2_PAYLOAD = "g5formal-case2-pair"


def _redact(dsn: str) -> str:
    try:
        p = urlparse(dsn)
        host = p.hostname or "?"
        port = p.port or 5432
        db = (p.path or "?").lstrip("/")
        return f"{host}:{port}/{db}"
    except Exception:
        return "?"


def _validate_dsn(label: str, dsn: str) -> None:
    """Fail fast on malformed DSNs (e.g. unsubstituted placeholders).

    asyncpg's own parse errors are cryptic (a literal '<...>' placeholder
    surfaces as an int() failure on the host part); this names the problem.
    """
    p = urlparse(dsn)
    if (
        p.scheme not in ("postgresql", "postgres")
        or not p.hostname
        or not p.path.lstrip("/")
        or "<" in dsn
        or ">" in dsn
    ):
        raise SystemExit(
            f"bad {label}: need postgresql://user:password@host:port/db "
            f"(got host={p.hostname!r} db={p.path!r}); "
            "check for unsubstituted <placeholders> or illegal URI characters"
        )


def _require_tenant(value: str) -> str:
    if not _TENANT_RE.fullmatch(value):
        raise SystemExit(f"refusing unsafe tenant value: {value!r}")
    return value


class Check:
    """One gate check with a stable name and PASS/FAIL outcome."""

    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    def failed(self) -> list[str]:
        return [n for n, ok, _ in self.results if not ok]


async def _conn(dsn: str):
    import asyncpg

    return await asyncpg.connect(dsn)


async def preflight(app_dsn: str, admin_dsn: str, check: Check) -> None:
    """Protocol §1: role flags, FORCE RLS, policy predicate, single writer.

    The gate-critical check is on the case-runner connection (G5_DSN):
    a superuser/bypassrls case-runner silently bypasses RLS and voids
    every case. The admin connection may be superuser (grant mirroring
    needs it) but must not be the same role as the case-runner.
    """
    admin = await _conn(admin_dsn)
    app = await _conn(app_dsn)
    try:
        dbname = await admin.fetchval("SELECT current_database()")
        admin_user = await admin.fetchval("SELECT current_user")
        app_user = await app.fetchval("SELECT current_user")
        app_flags = await app.fetchrow(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        check.record(
            "preflight.case-runner-not-bypass",
            not app_flags["rolbypassrls"] and not app_flags["rolsuper"],
            f"db={dbname} role={app_user} super={app_flags['rolsuper']} "
            f"bypassrls={app_flags['rolbypassrls']}",
        )
        same_role = admin_user == app_user
        check.record(
            "preflight.admin-separated",
            not same_role,
            f"admin={admin_user} case-runner={app_user}"
            + (" — SAME ROLE: cases would run with admin privilege, RLS void" if same_role else ""),
        )
        force = await admin.fetchval(
            "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'hypotheses'"
        )
        check.record("preflight.force-rls", bool(force), "hypotheses FORCE RLS")
        pol = await admin.fetchrow(
            "SELECT qual, with_check FROM pg_policies "
            "WHERE tablename = 'hypotheses' AND policyname = 'tenant_isolation'"
        )
        uses_guc = pol is not None and "app.tenant_id" in f"{pol['qual']} {pol['with_check']}"
        check.record("preflight.policy-guc", uses_guc, "predicate keys on app.tenant_id")
    finally:
        await admin.close()
        await app.close()
    # Single-writer re-grep (local tree, not the DB).
    hits = []
    for path in (_REPO_ROOT / "src").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text()
        if "update(HypothesisORM)" in text or "INSERT INTO hypotheses" in text:
            hits.append(str(path.relative_to(_REPO_ROOT)))
    check.record(
        "preflight.single-writer",
        hits
        == ["src/investigation_agent_platform/infrastructure/persistence/hypothesis_repository.py"],
        f"writers={hits}",
    )


async def setup_role(admin_dsn: str, check: Check) -> None:
    """Create iap_test1 mirroring iap_app grants (operator-authorized)."""
    admin = await _conn(admin_dsn)
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = 'iap_test1'")
        if not exists:
            await admin.execute(
                "CREATE ROLE iap_test1 LOGIN NOINHERIT NOSUPERUSER "
                "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            )
        secret = getpass.getpass("Password for iap_test1 (never stored): ")
        if not secret:
            raise SystemExit("empty password refused")
        stmt = await admin.fetchval(
            "SELECT format('ALTER ROLE iap_test1 PASSWORD %L', $1::text)", secret
        )
        await admin.execute(stmt)
        await admin.execute(
            """
            DO $$ DECLARE r record; BEGIN
              FOR r IN SELECT table_schema, table_name, privilege_type
                       FROM information_schema.role_table_grants WHERE grantee = 'iap_app'
              LOOP EXECUTE format('GRANT %I ON %I.%I TO iap_test1',
                                  r.privilege_type, r.table_schema, r.table_name);
              END LOOP;
            END $$;
            REVOKE iap_migrator FROM iap_test1;
            REVOKE iap_test1 FROM iap_migrator, iap_app, iap_worker, iap_registry_admin;
            REVOKE CREATE ON SCHEMA public FROM iap_test1;
            """
        )
        row = await admin.fetchrow(
            "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
            "FROM pg_roles WHERE rolname = 'iap_test1'"
        )
        ok = not any(row)
        check.record("role.attributes", ok, f"super={row[0]} bypassrls={row[1]}")
    finally:
        await admin.close()


async def drop_role(admin_dsn: str, check: Check) -> None:
    admin = await _conn(admin_dsn)
    try:
        await admin.execute("DROP ROLE IF EXISTS iap_test1")
        check.record("role.dropped", True, "iap_test1 removed")
    finally:
        await admin.close()


async def _begin_tenant(conn, tenant: str) -> None:
    await conn.execute("BEGIN")
    await conn.execute(f"SET LOCAL app.tenant_id = '{tenant}'")


async def ensure_fixture(dsn: str, tenant: str, check: Check) -> tuple[str, str, int]:
    """Insert (or reuse) fixture investigation + hypothesis; return (hid, iid, version).

    investigation_id is a real FK value (never NULL) so the app-level
    save() predicate matches exactly as in production.
    """
    import uuid

    conn = await _conn(dsn)
    try:
        await _begin_tenant(conn, tenant)
        row = await conn.fetchrow(
            "SELECT id, investigation_id, version FROM hypotheses WHERE tenant_id = $1 "
            "AND statement LIKE 'g5formal%' ORDER BY created_at DESC LIMIT 1",
            tenant,
        )
        if row is not None:
            await conn.execute("ROLLBACK")
            check.record("setup.fixture-reused", True, f"version={row['version']}")
            return str(row["id"]), str(row["investigation_id"]), int(row["version"])
        inv = await conn.fetchrow(
            "SELECT id FROM investigations WHERE tenant_id = $1 LIMIT 1", tenant
        )
        if inv is None:
            iid = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO investigations (id, tenant_id, application_id, environment, "
                "title, problem_description, status, phase, created_by, session_id, "
                "created_at, updated_at, version) "
                "VALUES ($1, $2, 'g5formal', 'test', 'g5formal fixture', 'fixture', "
                "'DRAFT', 'CREATED', 'g5formal-driver', 'g5formal', NOW(), NOW(), 1)",
                iid,
                tenant,
            )
        else:
            iid = str(inv["id"])
        hid = str(uuid.uuid4())
        await conn.execute(
            "INSERT INTO hypotheses (id, tenant_id, investigation_id, title, "
            "description, statement, status, created_at, updated_at, "
            "confidence_score, support_score, supporting_evidence_ids, "
            "refuting_evidence_ids, assessments_json, causal_steps_json) "
            "VALUES ($1, $2, $3, $4, $5, $6, 'PROPOSED', NOW(), NOW(), "
            "0.5, 0.5, '[]', '[]', '[]', '[]')",
            hid,
            tenant,
            iid,
            "g5formal fixture",
            "fixture",
            "g5formal-fixture",
        )
        await conn.execute("COMMIT")
        check.record("setup.fixture-inserted", True, "version=1")
        return hid, iid, 1
    finally:
        await conn.close()


async def case1_overlap(
    dsn: str, tenant: str, hid: str, iid: str, version: int, check: Check
) -> int:
    """Overlapping xacts: A wins, B blocks then gets rowcount 0. Returns new version."""
    a = await _conn(dsn)
    b = await _conn(dsn)
    try:
        await _begin_tenant(a, tenant)
        n = await a.fetchval("SELECT version FROM hypotheses WHERE id = $1", hid)
        r1 = await a.execute(
            "UPDATE hypotheses SET statement = $1, version = $2 WHERE id = $3 "
            "AND tenant_id = $4 AND investigation_id = $5 AND version = $6",
            CASE1_A_PAYLOAD,
            n + 1,
            hid,
            tenant,
            iid,
            n,
        )
        ok_a = r1 == "UPDATE 1"

        async def blocked_update():
            await _begin_tenant(b, tenant)
            await b.execute("SET LOCAL lock_timeout = '10s'")
            return await b.execute(
                "UPDATE hypotheses SET statement = $1, version = $2 WHERE id = $3 "
                "AND tenant_id = $4 AND investigation_id = $5 AND version = $6",
                CASE1_B_PAYLOAD,
                n + 1,
                hid,
                tenant,
                iid,
                n,
            )

        task = asyncio.create_task(blocked_update())
        await asyncio.sleep(1.0)
        await a.execute("COMMIT")
        r2 = await asyncio.wait_for(task, timeout=20.0)
        ok_b = r2 == "UPDATE 0"
        await b.execute("ROLLBACK")
        # SET LOCAL died with the rolled-back xact: re-establish tenant
        # context or RLS hides the row from this connection.
        await _begin_tenant(b, tenant)
        final = await b.fetchrow("SELECT statement, version FROM hypotheses WHERE id = $1", hid)
        await b.execute("ROLLBACK")
        ok_row = final["statement"] == CASE1_A_PAYLOAD and final["version"] == n + 1
        check.record(
            "case1.loser-denied",
            ok_a and ok_b and ok_row,
            f"A={r1} B={r2} final_v={final['version']}",
        )
        return int(final["version"])
    finally:
        await a.close()
        await b.close()


def _hypothesis(tenant: str, hid: str, iid: str, statement: str):
    import uuid

    from investigation_agent_platform.domain.hypothesis.models import Hypothesis

    return Hypothesis(
        tenant_id=tenant,
        statement=statement,
        status="PROPOSED",
        title="g5formal",
        description="g5formal",
        id=uuid.UUID(hid),
        investigation_id=uuid.UUID(iid),
    )


async def case2_app_ladder(
    dsn: str, tenant: str, hid: str, iid: str, rungs: list[int], check: Check
) -> int:
    """True app-level concurrency via SqlAlchemyHypothesisRepository.save.

    Each pair races two save() calls on the same expected_version; exactly
    one commits, exactly one raises ConcurrencyError. Re-reads N per pair.
    """
    import uuid

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from investigation_agent_platform.domain.common.exceptions import ConcurrencyError
    from investigation_agent_platform.infrastructure.persistence.hypothesis_repository import (
        SqlAlchemyHypothesisRepository,
    )

    engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://"))
    repo = SqlAlchemyHypothesisRepository(async_sessionmaker(engine, expire_on_commit=False))
    try:
        version = None
        for pairs in rungs:
            wins = losses = 0
            for _ in range(pairs):
                current = await repo.get_by_id(tenant, uuid.UUID(hid))
                assert current is not None
                n = current.version
                h1 = _hypothesis(tenant, hid, iid, f"{CASE2_PAYLOAD}-a")
                h2 = _hypothesis(tenant, hid, iid, f"{CASE2_PAYLOAD}-b")

                async def one(h, _n=n):
                    await repo.save(tenant, h, uuid.UUID(iid), _n)
                    return True

                res = await asyncio.gather(one(h1), one(h2), return_exceptions=True)
                pwins = sum(1 for r in res if r is True)
                plosses = sum(1 for r in res if isinstance(r, ConcurrencyError))
                others = [r for r in res if r is not True and not isinstance(r, ConcurrencyError)]
                if others:
                    raise others[0]
                wins += pwins
                losses += plosses
                if pwins != 1 or plosses != 1:
                    ok = False
                    break
            else:
                ok = True
            if not ok:
                check.record(f"case2.rung-{pairs}", False, f"wins={wins} losses={losses}")
                current = await repo.get_by_id(tenant, uuid.UUID(hid))
                return current.version
            current = await repo.get_by_id(tenant, uuid.UUID(hid))
            version = current.version
            check.record(f"case2.rung-{pairs}", True, f"pairs={pairs} v={version}")
        return version
    finally:
        await engine.dispose()


async def case3_cross_tenant(dsn: str, tenant: str, hid: str, version: int, check: Check) -> None:
    conn = await _conn(dsn)
    try:
        await _begin_tenant(conn, "other-tenant-probe")
        r = await conn.execute(
            "UPDATE hypotheses SET statement = 'x', version = $1 WHERE id = $2 "
            "AND tenant_id = 'other-tenant-probe' AND version = $3",
            version + 1,
            hid,
            version,
        )
        await conn.execute("ROLLBACK")
        check.record("case3.cross-tenant-denied", r == "UPDATE 0", f"rowcount={r}")
    finally:
        await conn.close()
    # Victim row untouched under the real tenant.
    conn = await _conn(dsn)
    try:
        await _begin_tenant(conn, tenant)
        v = await conn.fetchval("SELECT version FROM hypotheses WHERE id = $1", hid)
        await conn.execute("ROLLBACK")
        check.record("case3.victim-unchanged", v == version, f"v={v}")
    finally:
        await conn.close()


async def case4_concurrent_insert(dsn: str, tenant: str, iid: str, check: Check) -> None:
    import uuid

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from investigation_agent_platform.domain.common.exceptions import ConcurrencyError
    from investigation_agent_platform.infrastructure.persistence.hypothesis_repository import (
        SqlAlchemyHypothesisRepository,
    )

    engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://"))
    repo = SqlAlchemyHypothesisRepository(async_sessionmaker(engine, expire_on_commit=False))
    try:
        hid = str(uuid.uuid4())
        h = _hypothesis(tenant, hid, iid, "g5formal-case4")
        res = await asyncio.gather(
            repo.save(tenant, h, uuid.UUID(iid), 0),
            repo.save(tenant, h, uuid.UUID(iid), 0),
            return_exceptions=True,
        )
        wins = sum(1 for r in res if r is None)
        conflicts = sum(1 for r in res if isinstance(r, ConcurrencyError))
        others = [r for r in res if r is not None and not isinstance(r, ConcurrencyError)]
        check.record(
            "case4.single-insert-winner",
            wins == 1 and conflicts == 1 and not others,
            f"wins={wins} conflicts={conflicts}",
        )
    finally:
        await engine.dispose()


async def case5_reader_snapshot(dsn: str, tenant: str, hid: str, check: Check) -> None:
    reader = await _conn(dsn)
    writer = await _conn(dsn)
    try:
        await reader.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
        await reader.execute(f"SET LOCAL app.tenant_id = '{tenant}'")
        before = await reader.fetchrow(
            "SELECT statement, version FROM hypotheses WHERE id = $1", hid
        )
        await _begin_tenant(writer, tenant)
        await writer.execute(
            "UPDATE hypotheses SET statement = 'g5formal-case5', version = $1 "
            "WHERE id = $2 AND tenant_id = $3",
            before["version"] + 1,
            hid,
            tenant,
        )
        # Read inside the writer xact: SET LOCAL dies at COMMIT, and the
        # post-commit connection would see zero rows under RLS.
        fresh = await writer.fetchrow(
            "SELECT statement, version FROM hypotheses WHERE id = $1", hid
        )
        await writer.execute("COMMIT")
        during = await reader.fetchrow(
            "SELECT statement, version FROM hypotheses WHERE id = $1", hid
        )
        await reader.execute("ROLLBACK")
        stable = (
            during["version"] == before["version"] and during["statement"] == before["statement"]
        )
        check.record(
            "case5.no-mixed-snapshot",
            stable and fresh["statement"] == "g5formal-case5",
            f"reader_stable={stable}",
        )
    finally:
        await reader.close()
        await writer.close()


async def part_b(admin_dsn: str, dsn: str, tenant: str, check: Check) -> None:
    """B1 column/backfill sanity (non-destructive) + B2 role review outputs."""
    admin = await _conn(admin_dsn)
    try:
        col = await admin.fetchrow(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'hypotheses' AND column_name = 'version'"
        )
        check.record(
            "b1.version-column",
            col is not None and col["is_nullable"] == "NO",
            f"default={col['column_default'] if col else None}",
        )
        nulls = await admin.fetchval("SELECT count(*) FROM hypotheses WHERE version IS NULL")
        check.record("b1.no-null-versions", nulls == 0, f"nulls={nulls}")
        row = (
            await admin.fetchrow(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'iap_test1'"
            )
            if await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = 'iap_test1'")
            else None
        )
        if row is None:
            check.record("b2.test-role-flags", True, "iap_test1 absent (cases ran as app role)")
        else:
            check.record("b2.test-role-flags", not row[0] and not row[1], "no super/bypassrls")
    finally:
        await admin.close()
    conn = await _conn(dsn)
    try:
        await _begin_tenant(conn, tenant)
        await conn.execute("ROLLBACK")
        check.record("b2.tenant-session", True, "SET LOCAL accepted")
    finally:
        await conn.close()


async def amain(args: argparse.Namespace) -> int:
    import os

    admin_dsn = os.environ.get("G5_ADMIN_DSN", "")
    app_dsn = os.environ.get("G5_DSN", "")
    if not admin_dsn or not app_dsn:
        raise SystemExit(
            "need G5_ADMIN_DSN and G5_DSN in the environment (never on a command line)"
        )
    _validate_dsn("G5_ADMIN_DSN", admin_dsn)
    _validate_dsn("G5_DSN", app_dsn)
    tenant = _require_tenant(args.tenant)
    check = Check()

    probe = await _conn(admin_dsn)
    try:
        dbname = await probe.fetchval("SELECT current_database()")
        # Cluster fingerprint, not the server port: every docker-mapped
        # instance reports 5432 server-side, so the port cannot tell 5452 /
        # 5473 / 5552 apart. system_identifier is unique per initdb and
        # stable across restarts; start time spots a recycled container.
        cluster = await probe.fetchval("SELECT system_identifier::text FROM pg_control_system()")
        started = await probe.fetchval("SELECT pg_postmaster_start_time()")
    finally:
        await probe.close()
    print(f"target: {_redact(admin_dsn)} (cluster {cluster}, started {started})")
    if "prod" in dbname and not args.force:
        raise SystemExit(f"refusing prod-like database {dbname!r} without --force")
    if not args.yes:
        ok = input(f"Run G5-formal interleave against {dbname} as tenant {tenant!r}? Type YES: ")
        if ok.strip() != "YES":
            raise SystemExit("aborted")

    await preflight(app_dsn, admin_dsn, check)
    if check.failed():
        print("pre-flight failed; refusing to run cases")
        return 1
    if args.create_test_role:
        await setup_role(admin_dsn, check)
    hid, iid, version = await ensure_fixture(app_dsn, tenant, check)
    if args.cases and 1 in args.cases:
        version = await case1_overlap(app_dsn, tenant, hid, iid, version, check)
    if args.cases and 2 in args.cases:
        version = await case2_app_ladder(app_dsn, tenant, hid, iid, args.rungs, check)
    if args.cases and 3 in args.cases:
        await case3_cross_tenant(app_dsn, tenant, hid, version, check)
    if args.cases and 4 in args.cases:
        await case4_concurrent_insert(app_dsn, tenant, iid, check)
    if args.cases and 5 in args.cases:
        await case5_reader_snapshot(app_dsn, tenant, hid, check)
    await part_b(admin_dsn, app_dsn, tenant, check)
    if args.drop_test_role:
        await drop_role(admin_dsn, check)

    failed = check.failed()
    verdict = "PASS" if not failed else "HOLD"
    print(f"\nG5-formal verdict: {verdict}")
    print(f"checks: {len(check.results) - len(failed)}/{len(check.results)} green")
    if failed:
        print("gaps:")
        for name in failed:
            detail = next(d for n, ok, d in check.results if n == name)
            print(f"  - {name}: {detail}")
    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "verdict": verdict,
                    "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in check.results],
                },
                indent=2,
            )
        )
        print(f"report: {args.out}")
    return 0 if not failed else 1


def main() -> None:
    ap = argparse.ArgumentParser(
        description="G5-formal operator driver (live-DB interleave + role review)"
    )
    ap.add_argument(
        "--tenant", required=True, help="test tenant key (never defaulted, never logged)"
    )
    ap.add_argument(
        "--cases",
        type=int,
        nargs="*",
        default=[1, 2, 3, 4, 5],
        help="subset of interleave cases to run",
    )
    ap.add_argument(
        "--rungs",
        type=int,
        nargs="*",
        default=[1, 2, 5, 10, 25, 50, 100],
        help="case-2 concurrency ladder (pairs per rung)",
    )
    ap.add_argument(
        "--create-test-role", action="store_true", help="create iap_test1 via admin DSN first"
    )
    ap.add_argument("--drop-test-role", action="store_true", help="drop iap_test1 at the end")
    ap.add_argument("--out", default="", help="write JSON verdict report to PATH")
    ap.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    ap.add_argument("--force", action="store_true", help="allow prod-like database names")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
