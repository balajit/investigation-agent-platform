# scripts/gate_preflight.py
"""G-T6-D26: pre-flight probe (per tree, before any graded run).

Pure evaluation (Check/evaluate/main_ok) is unit-tested. The I/O wrapper is
thin and not unit-tested: it reads the processes' working directories with
ps/lsof, runs git rev-parse/status, creates one investigation (CREATED, never
started) to read the stamped loop_version and exercise the pin stamp, and runs
the L1 canary in a SEPARATE canary tenant. The canary and stamp probe write
rows: requires owner approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def evaluate(*, expect_sha: str, expect_loop_version: str, api_cwd_sha: str, worker_cwd_sha: str,
             api_dirty: bool, worker_dirty: bool, health_ready: bool, profile: dict,
             tenant_investigation_count: int, canary_loop_version: str, pin_stamp_rows: int,
             canary_verdict: str) -> list[Check]:
    caps = (profile.get("investigation") or {}).get("allowed_capabilities") or profile.get("allowed_capabilities") or []
    return [
        Check("api tree", api_cwd_sha == expect_sha and not api_dirty, f"{api_cwd_sha[:8]} dirty={api_dirty}"),
        Check("worker tree", worker_cwd_sha == expect_sha and not worker_dirty, f"{worker_cwd_sha[:8]} dirty={worker_dirty}"),
        Check("readiness", health_ready, "GET /health/ready"),
        Check("profile ACTIVE", str(profile.get("status")) == "ACTIVE", str(profile.get("status"))),
        Check("capabilities declared", bool(caps), f"{len(caps)} declared"),
        Check("tenant is fresh", tenant_investigation_count == 0, f"{tenant_investigation_count} investigations"),
        Check("loop version stamped", canary_loop_version == expect_loop_version, canary_loop_version),
        Check("claim-binding pin stamps", pin_stamp_rows == 1, f"{pin_stamp_rows} row(s)"),
        Check("canary L1 passes", canary_verdict == "PASS", canary_verdict),
    ]


def main_ok(checks: list[Check]) -> bool:
    return all(c.ok for c in checks)


def _cwd_of(pid: int) -> str:
    import subprocess

    try:
        out = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        for line in (out.stdout or "").splitlines():
            if line.startswith("n/"):
                return line[1:]
    except Exception:
        pass
    return "unknown"


def _git(path: str, *args: str) -> str:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", path, *args], capture_output=True, text=True, timeout=10, check=False
        )
        return (out.stdout or "").strip()
    except Exception:
        return "unknown"


def main() -> int:
    import argparse
    import json
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    ap = argparse.ArgumentParser(description="G-T6 pre-flight probe (writes canary rows; needs owner approval).")
    ap.add_argument("--api-base", default="http://localhost:8080")
    ap.add_argument("--tenant", default="n-a")
    ap.add_argument("--canary-tenant", default="")
    ap.add_argument("--api-pid-file", default="logs/api.pid")
    ap.add_argument("--worker-pid-file", default="logs/worker.pid")
    ap.add_argument("--expect-sha", required=True)
    ap.add_argument("--expect-loop-version", required=True)
    ap.add_argument("--expect-pin", default="cbv1")
    ap.add_argument("--profile-json", default="", help="Operator-supplied profile doc (status ACTIVE + capabilities).")
    ap.add_argument("--skip-canary", action="store_true", help="Skip the L1 live canary (default runs it).")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    from tranche_b_battery import _run_case, load_cases, stamp_claim_binding_version
    from tranche_b_battery import api as call

    def read_pid(path: str) -> int:
        try:
            with open(path, encoding="utf-8") as fh:
                return int(fh.read().strip().split()[0])
        except Exception:
            return -1

    api_pid, worker_pid = read_pid(args.api_pid_file), read_pid(args.worker_pid_file)
    api_cwd, worker_cwd = _cwd_of(api_pid), _cwd_of(worker_pid)
    api_sha, worker_sha = _git(api_cwd, "rev-parse", "HEAD"), _git(worker_cwd, "rev-parse", "HEAD")
    api_dirty = bool(_git(api_cwd, "status", "--porcelain"))
    worker_dirty = bool(_git(worker_cwd, "status", "--porcelain"))
    st, _ = call(args.api_base, args.tenant, "GET", "/api/v1/health/live")
    health_ready = st == 200
    profile = json.loads(Path(args.profile_json).read_text(encoding="utf-8")) if args.profile_json else {}
    st, listed = call(args.api_base, args.tenant, "GET", "/api/v1/investigations?limit=200")
    tenant_count = len(listed.get("items") or []) if st == 200 else -1

    # Stamp probe: one CREATED investigation, stamp the pin, read loop_version.
    st, data = call(args.api_base, args.tenant, "POST", "/api/v1/investigations",
                    {"application_id": "spring-sample-service",
                     "problem_description": "preflight stamp probe",
                     "initial_context": "probe"})
    probe_id = (data or {}).get("id", "")
    pin_rows = 0
    canary_lv = ""
    if probe_id:
        pin_rows = 1 if stamp_claim_binding_version(args.tenant, probe_id, args.expect_pin) else 0
        _, probe = call(args.api_base, args.tenant, "GET", f"/api/v1/investigations/{probe_id}")
        canary_lv = str(((probe.get("investigation") or {}).get("metadata") or {}).get("loop_version") or "")

    canary_verdict = "SKIP"
    if not args.skip_canary:
        canary_tenant = args.canary_tenant or (args.tenant + "-canary")
        l1 = next(c for c in load_cases() if c["id"] == "L1")
        results: list = []
        g1_rows: list = []
        pins: dict = {}
        records: list = []
        _run_case(args.api_base, canary_tenant, 600, args.expect_pin, True,
                  "preflight", "preflight", l1, results, g1_rows, pins, records)
        if results and results[0][1] == "PASS":
            canary_verdict = "PASS"
        else:
            canary_verdict = (results[0][1] if results else "NO-RESULT")

    checks = evaluate(
        expect_sha=args.expect_sha, expect_loop_version=args.expect_loop_version,
        api_cwd_sha=api_sha, worker_cwd_sha=worker_sha,
        api_dirty=api_dirty, worker_dirty=worker_dirty, health_ready=health_ready,
        profile=profile, tenant_investigation_count=tenant_count,
        canary_loop_version=canary_lv, pin_stamp_rows=pin_rows,
        canary_verdict=canary_verdict,
    )
    for c in checks:
        print(f"[{'ok' if c.ok else 'FAIL'}] {c.name}: {c.detail}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks], fh, indent=2)
    return 0 if main_ok(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
