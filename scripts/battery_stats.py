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
