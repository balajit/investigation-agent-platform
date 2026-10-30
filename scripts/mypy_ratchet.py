#!/usr/bin/env python3
"""Per-file mypy error ratchet.

mypy stays ``strict`` repo-wide; this gate only fails when a file gets
WORSE than its recorded baseline (or a clean file gains an error). Legacy
debt therefore cannot grow, and every fix lowers the baseline permanently.

Usage:
    python scripts/mypy_ratchet.py            # check (CI)
    python scripts/mypy_ratchet.py --update   # lower baseline to current counts

``--update`` never raises a count: once a baseline exists it refuses when any
file regressed, so the baseline can only ratchet down.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "config" / "mypy_baseline.json"
TARGETS = ["src/investigation_agent_platform"]  # tests are mypy-exempt
_ERROR = re.compile(r"^(?P<path>[^:\n]+\.py):\d+(?::\d+)?: error:")


def run_mypy() -> Counter[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", *TARGETS, "--no-error-summary"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    counts: Counter[str] = Counter()
    for line in proc.stdout.splitlines():
        match = _ERROR.match(line)
        if match:
            counts[match.group("path")] += 1
    if proc.returncode not in (0, 1):
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(f"mypy crashed (exit {proc.returncode})")
    return counts


def load_baseline() -> dict[str, int]:
    if not BASELINE.exists():
        return {}
    return {str(k): int(v) for k, v in json.loads(BASELINE.read_text()).items()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()

    current = run_mypy()
    baseline = load_baseline()
    regressions = {
        path: (baseline.get(path, 0), count)
        for path, count in current.items()
        if count > baseline.get(path, 0)
    }

    if args.update:
        if baseline and regressions:
            _report(regressions)
            print("refusing to update: baseline may only ratchet down", file=sys.stderr)
            return 1
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(dict(sorted(current.items())), indent=2) + "\n")
        print(f"baseline written: {sum(current.values())} errors in {len(current)} files")
        return 0

    if regressions:
        _report(regressions)
        return 1
    improved = sum(1 for p, c in baseline.items() if current.get(p, 0) < c)
    print(
        f"mypy ratchet ok: {sum(current.values())} errors (baseline "
        f"{sum(baseline.values())}); {improved} file(s) improved - run --update to lock in"
    )
    return 0


def _report(regressions: dict[str, tuple[int, int]]) -> None:
    print("mypy ratchet FAILED - files worse than baseline:", file=sys.stderr)
    for path, (was, now) in sorted(regressions.items()):
        print(f"  {path}: {was} -> {now}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
