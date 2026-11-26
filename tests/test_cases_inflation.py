"""cases.find_similar_cases (cutoff 누출 방지) + inflation 품목 묶기·연도쌍 + backtest 탭 연도/구경 허용폭."""
from datetime import date

import pytest

from backtest import dia_width, tab_years
from cases import CaseQuery, DbLine, LineIndex, find_similar_cases
from inflation import conclusion, group_items, year_pairs


def ln(i, d, price, dia=50.0, length=None, reg="111-11-11111", client="가사", lo=0.0, hi=0.05):
    return DbLine(i, date.fromisoformat(d), client, reg, "호닝", "", 1, price, price, dia, length, lo, hi)


# ---------------------------------------------------------------- 유사건 검색
def test_find_similar_cases_never_sees_cutoff_or_later():
    idx = LineIndex.build([ln(1, "2026-01-05", 40000), ln(2, "2026-03-01", 45000), ln(3, "2026-03-02", 99999),
                           ln(4, "2026-05-01", 50000)])
    got = find_similar_cases(idx, CaseQuery(dia=50, reg_no="111-11-11111"), date(2026, 3, 2))
    assert [c.line.id for c in got] == [2, 1]  # 당일(3)·이후(4) 제외, 최근 먼저


def test_find_similar_cases_requires_cutoff():
    with pytest.raises(ValueError):
        find_similar_cases(LineIndex.build([]), CaseQuery(dia=50), None)


def test_find_similar_cases_same_client_first_and_dia_width():
    idx = LineIndex.build([ln(1, "2025-01-01", 1, dia=50.4, reg="222-22-22222", client="나사"),
                           ln(2, "2024-01-01", 2, dia=50.9),
                           ln(3, "2025-01-01", 3, dia=52.0)])  # Ø50의 허용폭 max(1.0, 0.5) 밖
    got = find_similar_cases(idx, CaseQuery(dia=50, reg_no="111-11-11111"), date(2026, 1, 1))
    assert [(c.line.id, c.same_client) for c in got] == [(2, True), (1, False)]
    only = find_similar_cases(idx, CaseQuery(dia=50, client="가사"), date(2026, 1, 1), same_client_only=True)
    assert [c.line.id for c in only] == [2]


# ---------------------------------------------------------------- inflation
def test_group_items_rules():
    lines = [ln(1, "2024-01-01", 100, dia=50.0, length=500), ln(2, "2025-01-01", 110, dia=50.3, length=540),
             ln(3, "2025-01-01", 110, dia=50.0, length=600),          # 기장 +20% → 다른 품목
             ln(4, "2025-01-01", 110, dia=50.0),                      # 기장 없음 → 다른 품목
             ln(5, "2025-01-01", 110, dia=50.0, length=500, hi=0.03),  # 공차 다름
             ln(6, "2025-01-01", 110, dia=50.0, length=500, reg="999-99-99999"),  # 다른 거래처
             ln(7, "2025-02-01", 0, dia=50.0, length=500)]            # 단가 0 제외
    sizes = sorted(len(it.lines) for it in group_items(lines))
    assert sizes == [1, 1, 1, 1, 2]


def test_year_pairs_consecutive_present_years_annualized():
    lines = [ln(1, "2023-11-01", 100), ln(2, "2024-03-01", 100), ln(3, "2024-06-01", 120),
             ln(4, "2026-02-01", 121)]
    pairs = year_pairs(group_items(lines))
    assert [(p["pair"], p["price_a"], p["price_b"]) for p in pairs] == [("23→24", 100, 110), ("24→26", 110, 121)]
    assert pairs[0]["rate"] == pytest.approx(0.10)
    assert pairs[1]["rate"] == pytest.approx((121 / 110) ** 0.5 - 1)  # 2년에 10% → 연 ~4.9%


def test_year_pairs_rate_two_year_gap():
    pairs = year_pairs(group_items([ln(1, "2024-01-01", 100), ln(2, "2026-01-01", 121)]))
    assert pairs[0]["rate"] == pytest.approx(0.10)  # (1.21)^(1/2) - 1


def test_conclusion_mentions_frozen_share():
    s = conclusion([0.0, 0.0, 0.0, 0.10, 0.20])
    assert "동결 60%" in s and "연 9%" in s


# ---------------------------------------------------------------- backtest 조정
def test_tab_years_previous_year_tabs():
    ys = tab_years(["11월", "1월", "12월"], 2026, today=date(2026, 11, 26))
    assert ys == {"11월": 2026, "1월": 2026, "12월": 2025}
    assert tab_years(["12월"], 2025, today=date(2026, 11, 26)) == {"12월": 2025}


def test_dia_width_max_of_abs_and_pct():
    assert dia_width(50) == 1.0
    assert dia_width(300) == pytest.approx(3.0)
