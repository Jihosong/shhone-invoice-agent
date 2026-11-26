"""견적완료 이후 변경 처리 (임가공): H 연마량 임계값, D·E 사양 변경, S·T·U 덧붙임, 자동 견적 행 재견적."""
import pytest
from gspread.utils import a1_to_rowcol

import feedback
import pricer
import watcher
from pricer_job import Quote
from tests.test_human_quote_status import StubAi
from tests.test_issuer_job import REG
from tests.test_status_rename_cycle import RecWS
from tests.test_watcher_legacy import make_watcher
from tests.test_watcher_settle import age_all
from watcher import CHIDAE_DEFAULTS, ST_CHECK, ST_QUOTED, ST_QUOTED_EDIT, ST_QUOTED_OK, ST_TYPING, load_config, stock_change

COLS = {"date": 1, "client": 2, "main_msg": 4, "spec_qty": 5, "difficulty": 6, "stock_removal": 8, "work_end": 10,
        "author": 15, "pre_note": 19, "post_size": 20, "post_note": 21, "status": 24, "unit_price": 25, "basis": 26,
        "row_id": 28}
SCFG = {"spreadsheet_id": "x", "label": "임가공", "kind": "job", "data_start_row": 3, "columns": COLS,
        "hash_columns": ["client", "main_msg", "spec_qty", "difficulty", "stock_removal"],
        "item_columns": ["spec_qty", "main_msg"]}
S, P, B = COLS["status"] - 1, COLS["unit_price"] - 1, COLS["basis"] - 1
D, E, H, PRE = COLS["main_msg"] - 1, COLS["spec_qty"] - 1, COLS["stock_removal"] - 1, COLS["pre_note"] - 1
E50 = "Ø50 +0.0~+0.03 / 300 / 2개"
E300 = "Ø300 +0.0~+0.05 / 500 / 1개"


class HighAi(StubAi):
    def quote(self, inp, index, cutoff, hint="", tag="", max_conf=None):
        return Quote("AI-유사품", 30000, "high", "자동")


class Rig:
    def __init__(self, *specs, ai=None):
        rows = [[""] * 28]
        for i, spec in enumerate(specs):
            row = [""] * 28
            if i == 0:
                row[0], row[1] = "11/26", "가사"
            row[E], row[9] = spec, "FALSE"
            rows.append(row)
        self.w = make_watcher(rows, clients=[("가사", REG, 5)], scfg=SCFG, header=[""] * 28)
        self.w.ws = RecWS(self.w.ws.values[1:], [""] * 28)
        self.w.gc.ws = self.w.ws
        self.w.cfg = {"sheets": {"imgagong": SCFG}}
        self.w.common["status_values"] = load_config()["common"]["status_values"]
        self.w.post_quote = watcher.post_quote_states(self.w.common["status_values"])
        self.w.defer_flush = True
        feedback.ensure_columns(self.w.con)
        self.p = pricer.Pricer({"sheets": {"imgagong": SCFG}}, self.w.con)
        self.p._ai = ai or StubAi()

    def row(self, i=0):
        return self.w.ws.values[2 + i]

    def cycle(self):
        self.w.ws.batches.clear()
        self.w.process_sheet("imgagong", SCFG)
        self.p.run_once(self.w.snapshots)
        self.w.flush_writes()
        self.w.con.commit()
        return [u["values"][0][0] for b in self.w.ws.batches for u in b if a1_to_rowcol(u["range"])[1] == S + 1]

    def quoted(self):
        self.cycle()
        age_all(self.w)
        self.cycle()
        return self

    def human_edit(self, price="5000"):
        """가견적(견적확인필요 20000) → 사람이 단가 수정 → 견적완료(사람수정)."""
        self.quoted()
        assert self.row()[S] == ST_CHECK
        self.row()[P] = price
        self.cycle()
        assert self.row()[S] == ST_QUOTED_EDIT
        return self


def human(spec=E50):
    return Rig(spec).human_edit()


# ---------------------------------------------------------------- H 연마량/치대
def test_h_small_on_d50_keeps_status_and_notes():
    rig = human()
    rig.row()[H] = "-0.1"  # 0.1 / 50 = 0.2% < 0.4%
    assert rig.cycle() == []
    assert rig.row()[S] == ST_QUOTED_EDIT and rig.row()[P] == "5000"
    assert rig.row()[B].endswith(" | 연마량 -0.1 추가(구경 대비 0.2%)")
    assert rig.row()[B].startswith("[가사 11/26 1/1] 사람 수정 5000")
    assert rig.cycle() == [] and rig.row()[B].count("연마량") == 1  # 다음 바퀴 그대로


def test_h_large_on_d50_rechecks_keeping_human_price():
    rig = human()
    rig.row()[H] = "-0.3"  # 0.6%
    assert rig.cycle() == [ST_CHECK]
    assert rig.row()[P] == "5000"
    assert rig.row()[B].startswith(
        "[가사 11/26 1/1] [연마량 -0.3 추가(구경 대비 0.6%) — 단가 재확인] 사람 단가 5,000 유지 | 사람 수정 5000")
    assert rig.cycle() == []  # 다음 바퀴 견적확인필요 그대로, 재견적 없음
    assert rig.row()[P] == "5000"


def test_recheck_then_ok_is_human_ok_with_same_price():
    rig = human()
    rig.row()[H] = "-0.3"
    rig.cycle()
    rig.row()[S] = "확인"
    age_all(rig.w)
    assert rig.cycle() == [ST_QUOTED_OK]
    assert rig.row()[P] == "5000"
    assert "사람 확인 | 연마량 -0.3 추가(구경 대비 0.6%) | 사람 수정 5000" in rig.row()[B]
    assert "단가 재확인" not in rig.row()[B]


def test_recheck_then_price_edit_is_human_edit():
    rig = human()
    rig.row()[H] = "-0.3"
    rig.cycle()
    rig.row()[P] = "6000"
    assert rig.cycle() == [ST_QUOTED_EDIT]
    assert "사람 수정 6000 (에이전트 5000) | 연마량 -0.3 추가" in rig.row()[B]


def test_h_on_d300_keeps():
    rig = human(E300)
    rig.row()[H] = "-0.5"  # 0.5 / 300 = 0.17%
    assert rig.cycle() == []
    assert rig.row()[S] == ST_QUOTED_EDIT
    assert rig.row()[B].endswith("| 연마량 -0.5 추가(구경 대비 0.2%)")


def test_h_1mm_rechecks_even_on_large_bore():
    rig = human("Ø500 +0.0~+0.05 / 500 / 1개")
    rig.row()[H] = "-1.0"  # 0.2% 이지만 1mm 이상
    assert rig.cycle() == [ST_CHECK]
    assert "[연마량 -1.0 추가(구경 대비 0.2%) — 단가 재확인]" in rig.row()[B]


def test_stock_change_rules():
    r = CHIDAE_DEFAULTS
    assert stock_change("", "-0.2", "Ø50 / 2개", r) == ("연마량 -0.2 추가(구경 대비 0.4%)", True)  # 경계 0.4% 포함
    assert stock_change("-0.1", "-0.3", "Ø50", r) == ("연마량 -0.1→-0.3(구경 대비 0.6%)", True)
    assert stock_change("-0.3", "", "Ø50", r) == ("연마량 -0.3 삭제", False)
    assert stock_change("", "-0.1", "피스톤 2개", r) == ("연마량 -0.1 추가(구경 모름)", False)  # 구경 모름: 0.2mm 기준
    assert stock_change("", "-0.2", "피스톤 2개", r) == ("연마량 -0.2 추가(구경 모름)", True)
    assert stock_change("", "많이", "Ø50", r) == ("연마량 많이 추가(값 해석 못함)", True)
    assert stock_change("", "-0.1", "Ø100 + Ø20 / 2개", r)[1]  # 가장 작은 구경 기준 (0.5%)


def test_chidae_threshold_from_config(monkeypatch):
    rig = human()
    monkeypatch.setattr(watcher, "load_config", lambda: {"pricing": {"post_quote_chidae_ratio": 0.001}})
    rig.row()[H] = "-0.1"  # 0.2% ≥ 0.1%
    assert rig.cycle() == [ST_CHECK]


# ---------------------------------------------------------------- D·E·F / S·T·U
def test_e_edit_on_human_row_rechecks():
    rig = human()
    rig.row()[E] = "Ø50 +0.0~+0.03 / 300 / 3개"
    assert rig.cycle() == [ST_CHECK]
    assert rig.row()[P] == "5000"
    assert "[사양 변경 — 단가 재확인] 사람 단가 5,000 유지 | 사람 수정 5000" in rig.row()[B]


def test_d_edit_on_human_row_rechecks():
    rig = human()
    rig.row()[D] = "급함"
    assert rig.cycle() == [ST_CHECK]
    assert "[사양 변경 — 단가 재확인]" in rig.row()[B]


def test_second_reason_while_rechecking_is_joined():
    rig = human()
    rig.row()[E] = "Ø50 +0.0~+0.03 / 300 / 3개"
    rig.cycle()
    rig.row()[H] = "-0.3"
    assert rig.cycle() == []  # 이미 견적확인필요
    assert "[사양 변경, 연마량 -0.3 추가(구경 대비 0.6%) — 단가 재확인] 사람 단가 5,000 유지 | 사람 수정" in rig.row()[B]
    assert rig.row()[P] == "5000"


def test_s_edit_on_human_row_keeps_and_notes():
    rig = human()
    rig.row()[PRE] = "입구 찍힘 있음"
    assert rig.cycle() == []
    assert rig.row()[S] == ST_QUOTED_EDIT and rig.row()[P] == "5000"
    assert rig.row()[B].endswith(" | 호닝 전 노트 변경 '입구 찍힘 있음'")


def test_s_edit_on_auto_row_keeps_and_notes():
    rig = Rig(E50, ai=HighAi()).quoted()
    assert rig.row()[S] == ST_QUOTED
    rig.row()[PRE] = "메모"
    assert rig.cycle() == []
    assert rig.row()[S] == ST_QUOTED and rig.row()[B].endswith("| 호닝 전 노트 변경 '메모'")


@pytest.mark.parametrize("col, value", [(H, "-0.5"), (E, "Ø50 +0.0~+0.03 / 300 / 3개")])
def test_auto_row_h_or_e_change_requotes(col, value):
    rig = Rig(E50, ai=HighAi()).quoted()
    assert rig.row()[S] == ST_QUOTED
    rig.row()[col] = value
    assert rig.cycle() == [ST_TYPING]  # 예전처럼 묶음 재견적
    age_all(rig.w)
    assert rig.cycle() == [ST_QUOTED]


def test_human_row_untouched_when_sibling_changes():
    rig = Rig(E50, "Ø40 +0.0~+0.03 / 200 / 1개").quoted()
    assert (rig.row(0)[S], rig.row(1)[S]) == (ST_CHECK, ST_CHECK)
    rig.row(0)[P] = "5000"
    rig.cycle()
    assert rig.row(0)[S] == ST_QUOTED_EDIT
    rig.row(1)[E] = "Ø40 +0.0~+0.03 / 200 / 2개"  # 다른 행(자동 가견적) 수정 → 그 행만 재견적
    rig.cycle()
    assert (rig.row(0)[S], rig.row(1)[S]) == (ST_QUOTED_EDIT, ST_TYPING)
    assert rig.row(0)[P] == "5000"
