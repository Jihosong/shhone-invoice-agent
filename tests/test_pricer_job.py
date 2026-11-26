"""임가공 판정 사다리(pricer_job: 사전단가·동일품·미결) + 표기 변환표(normalize) + 백테스트 채점."""
from datetime import date

import pytest

import backtest
import clients_cli
from cases import DbLine, LineIndex
from pricer_job import M_PENDING, M_PRESET, M_SAME, JobInput, item_keywords, preset_price, quote_ladder
from watcher import ClientMaster, NameNormalizer, load_client_rules

REG = "111-11-11111"
CUT = date(2026, 6, 1)


def ln(i, d, price, dia=50.0, length=None, qty=1, lo=0.0, hi=0.05, item="시린더호닝", reg=REG, client="가사"):
    return DbLine(i, date.fromisoformat(d), client, reg, item, "", qty, price, price, dia, length, lo, hi)


def job(e, d="", reg=REG, client="가사", on=CUT):
    return JobInput.from_raw({"main_msg": d, "spec_qty": e}, client, reg, on)


def q(inp, lines, exclude=()):
    return quote_ladder(inp, LineIndex.build(lines), inp.on_date, exclude)


# ---------------------------------------------------------------- a. 사전단가
@pytest.mark.parametrize("d,price", [("8", 80000), ("1.5", 15000), ("5.5", 55000), (" 8 ", 80000),
                                     ("0.5", 5000), ("5000", 5000), ("45000", 45000), ("1,400,000", 1400000),
                                     ("500", 5000000)])
def test_preset_price(d, price):
    assert preset_price(d) == price


@pytest.mark.parametrize("d", ["", "긴급", "8만", "33,42,48", "Ø50", "0", "A84401413-004",
                               "0.4", "1000", "1,400", "4999", "501", "999", "5000001"])  # 5천 미만·500만 초과
def test_preset_price_not_number_or_out_of_range(d):
    assert preset_price(d) is None


def test_preset_wins_over_same_item():
    got = q(job("Ø50+0.0~+0.05 / 2개", d="8"), [ln(1, "2026-05-01", 30000)])
    assert (got.method, got.unit_price, got.confidence) == (M_PRESET, 80000, "high")
    assert "D열 8 ×10,000" in got.label()


def test_preset_out_of_range_falls_to_same_item():
    got = q(job("Ø125+0.0~+0.05 / 1개", d="600"), [ln(1, "2026-05-01", 40000, dia=125)])
    assert (got.method, got.unit_price) == (M_SAME, 40000)


def test_preset_confidence_by_client_history():
    e = "Ø50+0.0~+0.05 / 2개"
    assert q(job(e, d="20"), []).confidence == "mid"                       # 과거 단가 없음
    hist = [ln(1, "2025-01-01", 10000, dia=10), ln(2, "2025-02-01", 12000, dia=11)]  # 중앙값 11,000
    assert q(job(e, d="3"), hist).confidence == "high"                     # 30,000 < 33,000
    low = q(job(e, d="20"), hist)                                          # 200,000 ≥ 3배
    assert (low.method, low.unit_price, low.confidence) == (M_PRESET, 200000, "low")
    assert "3배 이상 차이" in low.basis
    assert q(job(e, d="0.5"), [ln(1, "2025-01-01", 15000, dia=10)]).confidence == "low"  # ⅓ 이하
    # 다른 거래처·cutoff 이후·정답 줄은 과거 단가로 안 봄
    other = [ln(3, "2025-01-01", 11000, reg="999-99-99999", client="나사"), ln(4, "2026-07-01", 11000)]
    assert q(job(e, d="20"), other).confidence == "mid"
    assert q(job(e, d="20"), hist, exclude={1, 2}).confidence == "mid"


def test_preset_multi_per_item():
    e = "SUSØ35+0.0~+0.03 / 330 / 1개 + SUSØ40+0.0~+0.03 / 175 / 1개"
    got = q(job(e, d="4 /2.5"), [])
    assert (got.method, got.unit_price, got.item_prices) == (M_PRESET, 40000, [(35.0, 40000), (40.0, 25000)])
    assert "Ø35 40,000 / Ø40 25,000" in got.basis


@pytest.mark.parametrize("d,e", [("11/15", "Ø75-0.005~-0.008 /1개"),          # 숫자 2개, 품목 1개
                                 ("20/30/40", "Ø30 / 1개 + Ø40 / 1개"),
                                 ("5.5/0.1", "Ø30 / 1개 + Ø40 / 1개")])        # 하나라도 범위 밖
def test_preset_multi_mismatch_pending(d, e):
    assert q(job(e, d=d), []).method == M_PENDING


def test_backtest_picks_item_price_by_line_dia():
    rows = [{"match": "exact", "date": "2026-06-01", "client": "가사", "reg_no": REG, "tab": "6월", "row": "3",
             "raw_main_msg": "5.5/7", "raw_spec_qty": "Ø30.18 +0~+0.02 / 20개 + Ø70 +0~+0.02 / 20개",
             "line_id": "9", "actual_unit_price": "70000", "actual_qty": "20"}]
    res = backtest.score_rows(rows, LineIndex.build([ln(9, "2026-06-02", 70000, dia=70)]))
    assert (res[0]["method"], res[0]["pred"], res[0]["err_rate"]) == (M_PRESET, 70000, 0)


# ---------------------------------------------------------------- b. 동일품
def test_same_item_most_recent_high():
    got = q(job("Ø50.2+0.0~+0.05 / 1개"), [ln(1, "2026-01-05", 40000), ln(2, "2026-03-01", 45000)])
    assert (got.method, got.unit_price, got.confidence, got.ref_line_id) == (M_SAME, 45000, "high", 2)
    assert got.basis.startswith("2026-03-01 45,000원")


def test_same_item_cutoff_and_exclude():
    lines = [ln(1, "2026-01-05", 40000), ln(2, "2026-06-01", 99999), ln(3, "2026-07-01", 88888)]
    assert q(job("Ø50+0.0~+0.05 / 1개"), lines).unit_price == 40000  # 당일·이후 안 봄
    assert q(job("Ø50+0.0~+0.05 / 1개"), lines, exclude={1}).method == M_PENDING  # 정답 줄 제외


@pytest.mark.parametrize("e,line,ok", [
    ("Ø50.3+0.0~+0.05 / 1개", ln(1, "2026-01-01", 1000), True),       # 구경 +0.3
    ("Ø50.4+0.0~+0.05 / 1개", ln(1, "2026-01-01", 1000), False),      # 구경 +0.4
    ("Ø50+0.0~+0.03 / 1개", ln(1, "2026-01-01", 1000), False),        # 공차 다름
    ("Ø50 / 1개", ln(1, "2026-01-01", 1000), True),                   # 공차 한쪽만 → 비교 안 함
    ("Ø50+0.0~+0.05 / 300 / 1개", ln(1, "2026-01-01", 1000, length=314), True),   # 기장 +4.7%
    ("Ø50+0.0~+0.05 / 300 / 1개", ln(1, "2026-01-01", 1000, length=320), False),  # 기장 +6.7%
    ("Ø50+0.0~+0.05 / 300 / 1개", ln(1, "2026-01-01", 1000), True),               # 기장 한쪽만
    ("막힘 Ø50+0.0~+0.05 / 1개", ln(1, "2026-01-01", 1000), False),   # 키워드 {막힘} ≠ {}
    ("막힘 Ø50+0.0~+0.05 / 1개", ln(1, "2026-01-01", 1000, item="막힘형 시린더"), True),
    ("긴급 Ø50+0.0~+0.05 / 1개", ln(1, "2026-01-01", 1000), True),    # 긴급은 품목 키워드 아님
    ("Ø50+0.0~+0.05 / 1개", ln(1, "2026-01-01", 1000, reg="999-99-99999", client="나사"), False),  # 다른 거래처
])
def test_same_item_criteria(e, line, ok):
    assert (q(job(e), [line]).method == M_SAME) is ok


def test_same_item_by_name_when_no_reg():
    got = q(job("Ø50+0.0~+0.05 / 1개", reg="", client="(주)가사"), [ln(1, "2026-01-01", 1000, reg="")])
    assert got.unit_price == 1000


def test_same_item_stale_keeps_price_mid():
    got = q(job("Ø50+0.0~+0.05 / 1개"), [ln(1, "2024-06-01", 100000)])  # 정확히 730일 = 24개월
    assert (got.method, got.unit_price, got.confidence) == (M_SAME, 100000, "mid")  # 보정 없음
    assert "직전 2024-06" in got.basis
    recent = q(job("Ø50+0.0~+0.05 / 1개"), [ln(1, "2024-06-03", 100000)])  # 728일
    assert (recent.confidence, recent.unit_price) == ("high", 100000)
    assert "직전 " not in recent.basis


@pytest.mark.parametrize("qty,method", [(3, M_PENDING), (2, M_SAME), (1, M_SAME)])
def test_same_item_qty_ratio_up(qty, method):
    got = q(job(f"Ø50+0.0~+0.05 / {qty}개"), [ln(1, "2026-01-01", 410000, qty=1)])
    assert got.method == method
    if method == M_SAME:
        assert got.confidence == "high"


def test_same_item_qty_ratio_down_and_hint():
    got = q(job("Ø50+0.0~+0.05 / 3개"), [ln(7, "2026-01-01", 410000, qty=9)])
    assert (got.method, got.unit_price, got.ref_line_id) == (M_PENDING, None, 7)
    assert got.hint.startswith("직전 동일품 9개 410,000 / 이번 3개")
    assert q(job("Ø50+0.0~+0.05 / 4개"), [ln(1, "2026-01-01", 1000, qty=9)]).confidence == "high"


@pytest.mark.parametrize("text,kw", [
    ("막힘 사각 다공 3구멍 딥홀 용접 열처리 도금", {"막힘", "사각", "다공", "딥홀", "용접", "열처리", "도금"}),
    ("Ra0.2 치대 0.3 긴급 각도 얇은 수리 면작업 릴리프", set()),   # 작업 조건·지시는 비교 안 함
])
def test_item_keywords_only_seven(text, kw):
    assert item_keywords(text) == frozenset(kw)


def test_same_item_ignores_non_item_keywords():
    got = q(job("Ø50+0.0~+0.05 Ra0.2 / 1개"), [ln(1, "2026-01-01", 1000, item="시린더 치대작업")])
    assert got.method == M_SAME


def test_pending_when_nothing():
    got = q(job("Ø50+0.0~+0.05 / 1개"), [])
    assert got.method == M_PENDING and got.unit_price is None


def test_cutoff_required():
    with pytest.raises(ValueError):
        quote_ladder(job("Ø50 / 1개"), LineIndex.build([]), None)


def test_from_sheet_row_uses_columns():
    cols = {"main_msg": 4, "spec_qty": 5, "difficulty": 6, "pre_size": 8}
    row = ["8/20", "가사", "", "", "Ø50+0.0~+0.05 / 300 / 2개", "상", "", "치대 0.2"]
    inp = JobInput.from_sheet_row(row, cols, "가사", REG, CUT)
    assert (inp.dia, inp.length, inp.qty, inp.tol_hi) == (50.0, 300.0, 2, 0.05)
    assert "치대" in inp.specials and inp.item_keywords == frozenset()  # 노트(H) 키워드는 동일품 비교에서 제외


# ---------------------------------------------------------------- 백테스트 채점
def test_score_rows_excludes_answer_line_and_scores():
    rows = [{"match": "exact", "date": "2026-06-01", "client": "가사", "reg_no": REG, "tab": "6월", "row": "3",
             "raw_main_msg": "", "raw_spec_qty": "Ø50+0.0~+0.05 / 1개", "line_id": "2",
             "actual_unit_price": "44000", "actual_qty": "1"},
            {"match": "none", "date": "2026-06-01"}]
    idx = LineIndex.build([ln(1, "2026-01-01", 40000), ln(2, "2026-05-30", 44000)])  # 2 = 정답(날짜가 앞서도 제외)
    res = backtest.score_rows(rows, idx)
    assert len(res) == 1
    assert (res[0]["method"], res[0]["pred"], res[0]["ref_line_id"]) == (M_SAME, 40000, 1)
    assert res[0]["err_rate"] == pytest.approx(40000 / 44000 - 1)
    assert any("동일품" in line for line in backtest.report_scores(res))


# ---------------------------------------------------------------- 표기 변환표
PAIRS = [("기게", "기계"), ("ENG", "이엔지"), ("프랜트", "플랜트"), ("컴패니", "컴퍼니"),
         ("테크놀러지", "테크놀로지"), ("J&C", "제이앤씨"), ("앤", "엔")]


@pytest.mark.parametrize("a,b", [("한비기게", "한비기계"), ("금강ENG", "금강이엔지"), ("제이프랜트", "제이플랜트"),
                                 ("제이앤컴패니", "제이엔컴퍼니"), ("대한테크놀러지", "대한테크놀로지"),
                                 ("에스앤에이치", "에스엔에이치"), ("J&C", "제이엔씨"), ("청우 J & C", "청우제이앤씨")])
def test_normalizer_same_key(a, b):
    n = NameNormalizer(PAIRS)
    assert n.key(a)[0] == n.key(b)[0]


def test_normalizer_latin_word_boundary():
    n = NameNormalizer(PAIRS)
    assert n.key("ENGINEERING")[0] == "engineering"
    assert n.key("다온 ENG")[0] == "다온이엔지"


def test_master_resolves_through_normalize_and_keeps_note():
    m = ClientMaster([("한비기계주식회사", "123-45-67890", 5), ("(주)명성이엔지", "222-22-22222", 3)],
                     today=date(2026, 11, 25), normalize=PAIRS)
    r1 = m.resolve_full("한비기게 급함")
    assert (r1.reg_no, r1.note) == ("123-45-67890", "급함")
    r2 = m.resolve_full("명성ENG(주) 2차")
    assert (r2.reg_no, r2.note) == ("222-22-22222", "2차")
    assert ClientMaster([("한비기계주식회사", "123-45-67890", 5)]).resolve_full("한비기게").reg_no is None


def test_normalize_applies_before_aliases():
    m = ClientMaster([("(주)은성종합기계", "333-33-33333", 1)], {"은성기계": "(주)은성종합기계"},
                     today=date(2026, 11, 25), normalize=PAIRS)
    assert m.resolve_full("은성기게").reg_no == "333-33-33333"


def test_load_rules_and_check_normalize(tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text('normalize:\n  "기게": "기계"\n  "빈칸": ""\naliases:\n', encoding="utf-8")
    assert load_client_rules(p)["normalize"] == [("기게", "기계")]
    import sqlite3

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE clients (name TEXT, reg_no TEXT, n_statements INT, last_date TEXT)")
    probs = clients_cli.check_aliases(p, con)
    assert any("normalize '빈칸'" in msg for _, _, msg in probs)
    assert not any("모르는 구역" in msg for _, _, msg in probs)
