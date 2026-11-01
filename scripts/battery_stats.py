# scripts/battery_stats.py
"""T9-D23: comparability statistics for the battery (stdlib only, pure, unit-testable)."""

from __future__ import annotations

from math import comb, sqrt


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def fisher_two_sided(a: int, n1: int, b: int, n2: int) -> float:
    """Exact two-sided p for a/n1 vs b/n2 passes."""
    total, k = n1 + n2, a + b
    pmf = lambda x: comb(n1, x) * comb(n2, k - x) / comb(total, k)
    p0 = pmf(a)
    return min(1.0, sum(pmf(x) for x in range(max(0, k - n2), min(n1, k) + 1) if pmf(x) <= p0 + 1e-12))


def summarize(cell: str, rubric: str, runs: list[dict]) -> dict:
    """runs: {case, verdict, valid, g1_kind}. Invalid runs (cache-served, wrong tree) are excluded and counted."""
    valid = [r for r in runs if r.get("valid", True)]
    k, n = sum(1 for r in valid if r["verdict"] == "PASS"), len(valid)
    lo, hi = wilson(k, n)
    return {"cell": cell, "rubric": rubric, "pass": k, "n": n, "invalid": len(runs) - n, "ci95": (lo, hi)}


def compare(a: dict, b: dict) -> dict:
    if a["rubric"] != b["rubric"]:
        raise ValueError(f"cross-rubric comparison refused: {a['rubric']} vs {b['rubric']}")
    return {"p_fisher": fisher_two_sided(a["pass"], a["n"], b["pass"], b["n"]),
            "delta": a["pass"] / a["n"] - b["pass"] / b["n"]}


def regression_p(after_pass: int, after_n: int, before_pass: int, before_n: int) -> float:
    """One-sided "after is lower than before" p (GT6.06 decision rule).

    The gate asks a directional, pre-registered question (did the after arm
    regress?), so the one-sided conditional exact p is the honest one: the
    two-sided fisher_two_sided cannot flag the table's own example (15/18 vs
    10/18 gives 0.146 two-sided, 0.073 one-sided).
    """
    total, k = after_n + before_n, after_pass + before_pass
    pmf = lambda x: comb(after_n, x) * comb(before_n, k - x) / comb(total, k)
    lo = max(0, k - before_n)
    return min(1.0, sum(pmf(x) for x in range(lo, after_pass + 1)))


def flags_regression(before: dict, after: dict, min_drop: float = 0.25, alpha: float = 0.10) -> tuple[bool, str]:
    """GT6.06 decision rule: flag iff after is lower by at least min_drop AND regression_p < alpha."""
    bp, bn = before["pass"], before["n"]
    ap, an = after["pass"], after["n"]
    drop = bp / bn - ap / an if bn and an else 0.0
    p = regression_p(ap, an, bp, bn)
    if drop >= min_drop and p < alpha:
        return (True, f"drop {drop:.0%}, p={p:.3f}")
    return (False, f"drop {drop:.0%}, p={p:.3f}")


def load_records(path: str, expect_rubric: str | None = None, expect_sha: str | None = None,
                 expect_pin: str | None = None) -> tuple[list[dict], list[dict]]:
    """GT6.04: garbage-in guard. Returns (records, rejections with reasons).

    Rejects records with a missing rubric/git sha, a rubric/sha/pin that does
    not match the requested one. Invalid runs (valid: false) are kept — they
    are excluded and counted by summarize, not rejected here.
    """
    import json as _json
    from pathlib import Path as _Path

    raw = _json.loads(_Path(path).read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else raw.get("records", [])
    records, rejections = [], []
    for r in rows:
        if not isinstance(r, dict):
            rejections.append({"record": str(r)[:80], "reason": "not a record"})
            continue
        problems = []
        if not r.get("rubric"):
            problems.append("missing rubric")
        elif expect_rubric and r.get("rubric") != expect_rubric:
            problems.append(f"rubric {r.get('rubric')} != {expect_rubric}")
        if not r.get("git_sha"):
            problems.append("missing git sha")
        elif expect_sha and r.get("git_sha") != expect_sha:
            problems.append("sha does not match the requested tree")
        if expect_pin and r.get("pin") and r["pin"] != expect_pin:
            problems.append(f"pin {r['pin']} != {expect_pin}")
        if problems:
            rejections.append({"record": {k: r.get(k) for k in ("case", "cell", "tenant")}, "reason": "; ".join(problems)})
            continue
        records.append(r)
    return records, rejections
