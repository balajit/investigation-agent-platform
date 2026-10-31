"""Recorder for F8-D16 golden master. Run once before D15, never again for D15."""

from __future__ import annotations

import json
import subprocess
import sys

from tests.unit.cascade_corpus import corpus, run_case


def main() -> None:
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    cases = corpus()
    records = [run_case(c) for c in cases]
    payload = {
        "header": {"sha": sha, "cases": len(cases), "seed": 20261031, "pins": ["lv5", "lv6", "lv7"]},
        "cases": [list(c) for c in cases],
        "records": records,
    }
    with open("tests/fixtures/cascade_golden_v1.json", "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True, default=str)
    print(f"recorded {len(cases)} cases at {sha}")


if __name__ == "__main__":
    sys.exit(main())
