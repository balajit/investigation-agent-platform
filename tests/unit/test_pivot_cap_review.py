# tests/unit/test_pivot_cap_review.py
"""F4-D23: cap-verdict evaluator (pure). Thresholds fixed before any data is seen (O-F4-1)."""

from investigation_agent_platform.domain.investigation.pivot_cap_review import (
    RunRow,
    review,
)


def _row(case: str, pivots: int | None, capped: int | None, productive: int | None, passed: bool | None, scoped: int | None = 0) -> RunRow:
    return RunRow(case_id=case, pivots=pivots, capped=capped, scoped_skipped=scoped, productive=productive, passed=passed)


def _harmed_pair(case: str, starved_pass: list[bool], free_pass: list[bool]) -> list[RunRow]:
    rows = [_row(case, 2, 1, 2, passed=p) for p in starved_pass]
    rows += [_row(case, 1, 0, 1, passed=p) for p in free_pass]
    return rows


def test_raises_on_starvation_harm() -> None:
    rows: list[RunRow] = []
    for i in range(3):
        rows += _harmed_pair(f"h{i}", [False, False, True], [True, True, True])
    rows += [_row(f"pad{i}", 1, 0, 1, passed=True) for i in range(22)]
    out = review(rows)
    assert out.verdict == "raise", out
    assert out.stats["harmed_cases"] == 3.0


def test_lowers_on_unproductive_pivots_even_with_starvation() -> None:
    rows = [_row(f"c{i % 5}", 3, 2, 0, passed=False) for i in range(8)]
    rows += [_row(f"c{i % 5}", 1, 0, 0, passed=True) for i in range(32)]
    out = review(rows)
    assert out.verdict == "lower", out


def test_too_little_or_incomplete_data() -> None:
    rows = [_row(f"c{i}", 1, 0, 1, passed=True) for i in range(29)]
    assert review(rows).verdict == "insufficient"
    mixed: list[RunRow] = [_row(f"c{i}", 1, 0, 1, passed=True) for i in range(25)]
    mixed += [_row(f"c{i}", None, None, None, passed=None) for i in range(15)]
    out = review(mixed)
    assert out.verdict == "insufficient" and out.stats["runs"] == 25.0


def test_starved_share_boundary() -> None:
    # 40 runs; 3 harmed cases; 8 starved = 0.20 -> raise; 7 starved = 0.175 -> keep
    def _rows(n_starved: int) -> list[RunRow]:
        rows: list[RunRow] = []
        per = [3, 3, 2] if n_starved == 8 else [3, 2, 2]
        for i, n_s in enumerate(per):
            rows += _harmed_pair(f"h{i}", [False] * n_s, [True] * 3)
        used = len(rows)
        rows += [_row(f"pad{i}", 1, 0, 1, passed=True) for i in range(40 - used)]
        return rows

    assert review(_rows(8)).verdict == "raise"
    assert review(_rows(7)).verdict == "keep"


def test_harmed_cases_boundary() -> None:
    # 8 starved of 40 = 0.20 but only 2 harmed cases -> keep (harmed gate isolates)
    base: list[RunRow] = []
    for i in range(2):
        base += _harmed_pair(f"h{i}", [False, False, False], [True, True, True])
    base += [_row("h2", 2, 1, 2, passed=True) for _ in range(2)]
    base += [_row("h2", 1, 0, 1, passed=True) for _ in range(2)]
    base += [_row(f"pad{i}", 1, 0, 1, passed=True) for i in range(24)]
    out = review(base)
    assert out.stats["harmed_cases"] == 2.0
    assert out.verdict == "keep"


def test_productivity_boundary() -> None:
    # 40 pivots: 20 productive -> share 0.50 -> not lower; 10 productive (0.25) -> lower
    even = [_row(f"c{j % 4}", 2, 0, 1, passed=True) for j in range(20)]
    even += [_row(f"c{j % 4}", 1, 0, 1, passed=True) for j in range(20)]
    assert review(even).verdict == "keep"
    odd = [_row(f"c{j % 4}", 2, 0, 1, passed=True) for j in range(10)]
    odd += [_row(f"c{j % 4}", 2, 0, 0, passed=True) for j in range(20)]
    assert review(odd).verdict == "lower"
