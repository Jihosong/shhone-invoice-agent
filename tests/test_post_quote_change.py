"""견적완료 이후 변경 처리 (임가공).

모든 견적완료 이후 변경: 산출근거 끝 '[변경 11/27 14:05: H -0.3 추가]'(최근 3개).
post_quote_recheck: false — 견적완료(자동·사람확인·사람수정)·명세표발행대기·발행완료 행은 단가·상태 유지.
post_quote_recheck: true(기본) — 큰 변경(D·E·F, H 치대 ≥ 0.4%·1mm): 자동 행 재견적(AI에 변경 명시,
high → 견적완료 '연마량 반영 재견적 a→b', 아니면 견적확인필요), 사람 단가 행 단가 유지 + 견적확인필요.
S 변경·작은 H·명세표발행대기 이후는 표시만. 견적 전 행은 재견적. 파일에 담긴 명세표발행대기는 '-수정됨' 그대로.
"""
import re

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
from watcher import (CHIDAE_DEFAULTS, ST_CHECK, ST_ISSUED, ST_PENDING, ST_PENDING_EDITED, ST_QUOTED, ST_QUOTED_EDIT,
                     ST_QUOTED_OK, ST_TYPING, append_change_tag, load_config, stock_change)

COLS = {"date": 1, "client": 2, "main_msg": 4, "spec_qty": 5, "difficulty": 6, "stock_removal": 8, "work_end": 10,
        "author": 15, "pre_note": 19, "status": 22, "unit_price": 23, "basis": 24,
        "row_id": 26}
SCFG = {"spreadsheet_id": "x", "label": "임가공", "kind": "job", "data_start_row": 3, "columns": COLS,
        "hash_columns": ["client", "main_msg", "spec_qty", "difficulty", "stock_removal"],
        "item_columns": ["spec_qty", "main_msg"]}
S, P, B, ID = COLS["status"] - 1, COLS["unit_price"] - 1, COLS["basis"] - 1, COLS["row_id"] - 1
D, E, H, PRE = COLS["main_msg"] - 1, COLS["spec_qty"] - 1, COLS["stock_removal"] - 1, COLS["pre_note"] - 1
E50 = "Ø50 +0.0~+0.03 / 300 / 2개"
E50B = "Ø50 +0.0~+0.03 / 300 / 3개"
E300 = "Ø300 +0.0~+0.05 / 500 / 1개"
STAMP = r"\d{1,2}/\d{1,2} \d{2}:\d{2}"


def tag_re(text):
    return re.compile(rf"\[변경 {STAMP}: {re.escape(text)}\]$")


class HighAi(StubAi):
    """Ø50 → 30000 high (견적완료), 그 밖 → 20000 low (견적확인필요)."""
    def quote(self, inp, index, cutoff, hint="", tag="", max_conf=None):
        if "Ø50" in inp.raw.get("spec_qty", ""):
            return Quote("AI-유사품", 30000, "high", "자동")
        return super().quote(inp, index, cutoff)


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


def auto():
    rig = Rig(E50, ai=HighAi()).quoted()
    assert (rig.row()[S], rig.row()[P]) == (ST_QUOTED, "30000")
    return rig


def human_ok():
    rig = Rig(E50).quoted()
    assert rig.row()[S] == ST_CHECK
    rig.row()[S] = "확인"
    rig.cycle()
    assert (rig.row()[S], rig.row()[P]) == (ST_QUOTED_OK, "20000")
    return rig


def human_edit(spec=E50):
    rig = Rig(spec).quoted()
    rig.row()[P] = "5000"
    rig.cycle()
    assert (rig.row()[S], rig.row()[P]) == (ST_QUOTED_EDIT, "5000")
    return rig


def recheck_on(monkeypatch):
    monkeypatch.setattr(watcher, "load_config", lambda: {"pricing": {"post_quote_recheck": True}})


def recheck_off(monkeypatch):
    monkeypatch.setattr(watcher, "load_config", lambda: {"pricing": {"post_quote_recheck": False}})


# ---------------------------------------------------------------- recheck 꺼짐: 견적완료 이후 = 유지 + 변경 표시
@pytest.mark.parametrize("make", [auto, human_ok, human_edit], ids=["자동", "사람확인", "사람수정"])
@pytest.mark.parametrize("col, value, desc", [
    (D, "급함", "D 급함 추가"),
    (E, E50B, f"E {E50[:20]}…→{E50B[:20]}…"),
    (H, "-0.3", "H -0.3 추가"),
    (PRE, "입구 찍힘", "S 입구 찍힘 추가"),
])
def test_quoted_row_change_keeps_price_and_status(make, col, value, desc, monkeypatch):
    recheck_off(monkeypatch)
    rig = make()
    status, price = rig.row()[S], rig.row()[P]
    rig.row()[col] = value
    assert rig.cycle() == []  # 상태 칸 쓰기 없음 (입력중으로 안 돌아감)
    assert (rig.row()[S], rig.row()[P]) == (status, price)
    assert tag_re(desc).search(rig.row()[B]), rig.row()[B]
    assert rig.row()[B].startswith("[가사 11/26 1/1] ")
    basis = rig.row()[B]
    assert rig.cycle() == [] and rig.row()[B] == basis  # 다음 바퀴 그대로 (같은 표시 다시 안 붙음)


def test_change_tags_accumulate_up_to_three(monkeypatch):
    recheck_off(monkeypatch)
    rig = human_edit()
    for v in ("-0.1", "-0.2", "-0.3", "-0.4"):
        rig.row()[H] = v
        rig.cycle()
    tags = re.findall(r"\[변경 [^\]]*\]", rig.row()[B])
    assert [t.split(": ", 1)[1] for t in tags] == ["H -0.1→-0.2]", "H -0.2→-0.3]", "H -0.3→-0.4]"]
    assert "사람 수정 5000" in rig.row()[B] and rig.row()[S] == ST_QUOTED_EDIT


def test_several_columns_in_one_tag_and_delete(monkeypatch):
    recheck_off(monkeypatch)
    rig = auto()
    rig.row()[H], rig.row()[PRE] = "-0.2", "메모"
    rig.cycle()
    assert tag_re("H -0.2 추가, S 메모 추가").search(rig.row()[B])
    rig.row()[H] = ""
    rig.cycle()
    assert tag_re("H -0.2 삭제").search(rig.row()[B])


def test_append_change_tag_keeps_last_three():
    body = "AI-유사품 [변경 1/1 00:00: a] [변경 1/1 00:01: b] [변경 1/1 00:02: c]"
    assert append_change_tag(body, "[변경 1/1 00:03: d]") == \
        "AI-유사품 [변경 1/1 00:01: b] [변경 1/1 00:02: c] [변경 1/1 00:03: d]"
    assert append_change_tag("", "[변경 1/1 00:03: d]") == "[변경 1/1 00:03: d]"


# ---------------------------------------------------------------- 견적 전 행은 재견적
def test_check_needed_row_e_change_requotes():
    rig = Rig(E50).quoted()
    assert rig.row()[S] == ST_CHECK
    rig.row()[E] = E50B
    assert rig.cycle() == [ST_TYPING]
    age_all(rig.w)
    assert rig.cycle() == [ST_CHECK]
    assert "[변경" not in rig.row()[B]


# ---------------------------------------------------------------- 같은 묶음 다른 행
def test_sibling_change_keeps_auto_quoted_row():
    rig = Rig(E50, "Ø40 +0.0~+0.03 / 200 / 1개", ai=HighAi()).quoted()
    assert (rig.row(0)[S], rig.row(1)[S]) == (ST_QUOTED, ST_CHECK)
    rig.row(1)[E] = "Ø40 +0.0~+0.03 / 200 / 2개"  # 견적 전 행 수정 → 그 행만 재견적
    assert rig.cycle() == [ST_TYPING]
    assert (rig.row(0)[S], rig.row(0)[P], rig.row(1)[S]) == (ST_QUOTED, "30000", ST_TYPING)
    age_all(rig.w)
    rig.cycle()
    assert (rig.row(0)[S], rig.row(1)[S]) == (ST_QUOTED, ST_CHECK)


def test_sibling_change_keeps_human_row():
    rig = Rig(E50, "Ø40 +0.0~+0.03 / 200 / 1개").quoted()
    rig.row(0)[P] = "5000"
    rig.cycle()
    rig.row(1)[E] = "Ø40 +0.0~+0.03 / 200 / 2개"
    rig.cycle()
    assert (rig.row(0)[S], rig.row(0)[P], rig.row(1)[S]) == (ST_QUOTED_EDIT, "5000", ST_TYPING)


# ---------------------------------------------------------------- 명세표발행대기 이후
def pending(rig, filed):
    rig.row()[S] = ST_PENDING
    rig.cycle()
    assert rig.row()[S] == ST_PENDING
    if filed:
        rig.w.con.execute("INSERT INTO issue_file_blocks(block_id, file_name, sheet) VALUES(?,?,?)",
                          (rig.row()[ID], "20261127_1400.xls", "imgagong"))
        rig.w.con.commit()
        rig.cycle()
    return rig


def test_unfiled_pending_change_keeps_and_tags():
    rig = pending(auto(), filed=False)
    rig.row()[H] = "-0.3"
    assert rig.cycle() == []
    assert (rig.row()[S], rig.row()[P]) == (ST_PENDING, "30000")
    assert tag_re("H -0.3 추가").search(rig.row()[B])


def test_filed_pending_change_is_edited_rule():
    rig = pending(auto(), filed=True)
    rig.row()[E] = E50B
    assert rig.cycle() == [ST_PENDING_EDITED]  # 예전 '-수정됨' 규칙 그대로
    assert rig.row()[P] == "30000" and "[변경" in rig.row()[B]
    assert rig.w.con.execute("SELECT removed_at IS NOT NULL FROM issue_file_blocks").fetchone() == (1,)


def test_issued_change_keeps_and_tags():
    rig = auto()
    rig.row()[S], rig.row()[COLS["author"] - 1] = ST_ISSUED, "AI"
    rig.cycle()
    rig.row()[E] = E50B
    assert rig.cycle() == []
    assert rig.row()[S] == ST_ISSUED and "[변경" in rig.row()[B]


# ---------------------------------------------------------------- post_quote_recheck: true (기본 켜짐) — 사람 단가 행
def test_recheck_on_by_default():
    assert CHIDAE_DEFAULTS["post_quote_recheck"] is True
    assert load_config()["pricing"]["post_quote_recheck"] is True


def test_recheck_h_small_keeps(monkeypatch):
    rig = human_edit()
    recheck_on(monkeypatch)
    rig.row()[H] = "-0.1"  # 0.2% < 0.4%
    assert rig.cycle() == []
    assert rig.row()[S] == ST_QUOTED_EDIT and tag_re("H -0.1 추가").search(rig.row()[B])


def test_recheck_h_large_then_ok(monkeypatch):
    rig = human_edit()
    recheck_on(monkeypatch)
    rig.row()[H] = "-0.3"  # 0.6%
    assert rig.cycle() == [ST_CHECK]
    assert rig.row()[P] == "5000"
    assert rig.row()[B].startswith(
        "[가사 11/26 1/1] [연마량 -0.3 추가(구경 대비 0.6%) — 단가 재확인] 사람 단가 5,000 유지 | 사람 수정 5000")
    assert rig.cycle() == [] and rig.row()[P] == "5000"
    rig.row()[S] = "확인"
    age_all(rig.w)
    assert rig.cycle() == [ST_QUOTED_OK]
    assert rig.row()[P] == "5000" and "사람 확인 | 연마량 -0.3 추가(구경 대비 0.6%) | 사람 수정 5000" in rig.row()[B]


def test_recheck_h_on_d300_keeps(monkeypatch):
    rig = human_edit(E300)
    recheck_on(monkeypatch)
    rig.row()[H] = "-0.5"  # 0.17%
    assert rig.cycle() == [] and rig.row()[S] == ST_QUOTED_EDIT


def test_recheck_e_edit_rechecks_s_edit_keeps(monkeypatch):
    rig = human_edit()
    recheck_on(monkeypatch)
    rig.row()[PRE] = "메모"
    assert rig.cycle() == [] and rig.row()[S] == ST_QUOTED_EDIT
    rig.row()[E] = E50B
    assert rig.cycle() == [ST_CHECK]
    assert "[사양 변경 — 단가 재확인] 사람 단가 5,000 유지 | 사람 수정 5000" in rig.row()[B]


def test_human_s_change_keeps():
    rig = human_ok()
    rig.row()[PRE] = "입구 찍힘"
    assert rig.cycle() == [] and rig.row()[S] == ST_QUOTED_OK
    assert tag_re("S 입구 찍힘 추가").search(rig.row()[B])


# ---------------------------------------------------------------- 자동 견적 행: 큰 변경 → 재견적
class NoteAi(StubAi):
    """재견적 메모(change_note)가 있으면 answer(None = AI 실패), 없으면 Ø50 → 30000 high. 받은 메모를 notes에."""
    def __init__(self, answer=(32000, "high")):
        super().__init__()
        self.answer, self.notes = answer, []

    def quote(self, inp, index, cutoff, hint="", tag="", max_conf=None):
        self.notes.append(inp.change_note)
        if inp.change_note:
            if self.answer is None:
                from pricer_ai import AiError
                raise AiError("API 오류")
            return Quote("AI-유사품", self.answer[0], self.answer[1], "치대 반영")
        return Quote("AI-유사품", 30000, "high", "자동")


def auto_note(answer=(32000, "high")):
    ai = NoteAi(answer)
    rig = Rig(E50, ai=ai).quoted()
    assert (rig.row()[S], rig.row()[P]) == (ST_QUOTED, "30000")
    return rig, ai


def requote(rig):
    """큰 변경 → 입력중(산출근거에 변경 표시) → 안정화 후 재견적. 재견적 바퀴의 상태 쓰기 목록."""
    assert rig.cycle() == [ST_TYPING]
    assert "[변경" in rig.row()[B]
    age_all(rig.w)
    return rig.cycle()


def test_auto_h_small_keeps():
    rig, ai = auto_note()
    rig.row()[H] = "-0.1"  # Ø50 대비 0.2% < 0.4%
    assert rig.cycle() == []
    assert (rig.row()[S], rig.row()[P]) == (ST_QUOTED, "30000") and tag_re("H -0.1 추가").search(rig.row()[B])
    assert ai.notes == [""]  # 재견적 없음


def test_auto_h_large_requotes_high():
    rig, ai = auto_note((32000, "high"))
    rig.row()[H] = "-0.5"  # 1.0%
    assert requote(rig) == [ST_QUOTED]
    assert ai.notes[-1] == "연마량 -0.5 추가됨(구경 대비 1.0%), 치대 반영 (변경 전 견적 30,000원)"
    assert rig.row()[P] == "32000"
    assert rig.row()[B].startswith("[가사 11/26 1/1] 연마량 반영 재견적 30,000→32,000 | AI-유사품"), rig.row()[B]
    assert rig.w.con.execute("SELECT requote FROM rows").fetchone() == (None,)  # 메모 지움
    assert rig.cycle() == [] and rig.row()[P] == "32000"  # 다음 바퀴 그대로


def test_auto_h_large_requotes_mid_needs_check():
    rig, _ = auto_note((28000, "mid"))
    rig.row()[H] = "-0.5"
    assert requote(rig) == [ST_CHECK]
    assert rig.row()[P] == "28000"
    assert rig.row()[B].startswith("[가사 11/26 1/1] [가견적-mid] 연마량 반영 재견적 30,000→28,000 | "), rig.row()[B]
    rig.row()[S] = "확인"  # 사람이 적힌 단가 확인 → 사람확인
    assert rig.cycle() == [ST_QUOTED_OK] and rig.row()[P] == "28000"


def test_auto_requote_ai_failure_keeps_old_price():
    rig, _ = auto_note(None)
    rig.row()[H] = "-0.5"
    assert requote(rig) == [ST_CHECK]
    assert rig.row()[P] == "30000"
    assert "[가견적-low] 연마량 반영 재견적 실패 — 이전 단가 30,000 유지 | 미결" in rig.row()[B], rig.row()[B]


def test_auto_e_change_requotes_with_spec_note():
    rig, ai = auto_note((36000, "high"))
    rig.row()[E] = E50B
    assert requote(rig) == [ST_QUOTED]
    assert ai.notes[-1].startswith("사양 변경됨(E ") and ai.notes[-1].endswith("바뀐 사양 반영 (변경 전 견적 30,000원)")
    assert "사양 변경 재견적 30,000→36,000 | " in rig.row()[B] and rig.row()[P] == "36000"


def test_auto_s_change_keeps():
    rig, ai = auto_note()
    rig.row()[PRE] = "입구 찍힘"
    assert rig.cycle() == []
    assert (rig.row()[S], rig.row()[P]) == (ST_QUOTED, "30000") and tag_re("S 입구 찍힘 추가").search(rig.row()[B])
    assert ai.notes == [""]


@pytest.mark.parametrize("filed", [False, True], ids=["파일 전", "파일에 담김"])
def test_pending_h_large_only_tags(filed):
    rig = pending(auto(), filed=filed)
    rig.row()[H] = "-0.5"  # 1.0% — 견적완료였다면 재견적
    if filed:
        assert rig.cycle() == [ST_PENDING_EDITED]  # 예전 '-수정됨' 규칙 그대로
    else:
        assert rig.cycle() == [] and rig.row()[S] == ST_PENDING
    assert rig.row()[P] == "30000" and tag_re("H -0.5 추가").search(rig.row()[B])
    assert rig.w.con.execute("SELECT requote FROM rows").fetchone() == (None,)


def test_prompt_states_change_note():
    from datetime import date

    from pricer_ai import build_user_message
    from pricer_job import JobInput, LineIndex
    inp = JobInput.from_raw({"spec_qty": E50, "pre_size": "-0.5"}, "가사", REG, date(2026, 11, 27))
    inp.change_note = "연마량 -0.5 추가됨(구경 대비 1.0%), 치대 반영 (변경 전 견적 30,000원)"
    msg = build_user_message(inp, LineIndex.build([]), date(2026, 11, 28))
    assert "※ 견적완료 후 변경: 연마량 -0.5 추가됨(구경 대비 1.0%), 치대 반영 (변경 전 견적 30,000원)" in msg


def test_stock_change_rules():
    r = CHIDAE_DEFAULTS
    assert stock_change("", "-0.2", "Ø50 / 2개", r) == ("연마량 -0.2 추가(구경 대비 0.4%)", True)  # 경계 0.4% 포함
    assert stock_change("-0.1", "-0.3", "Ø50", r) == ("연마량 -0.1→-0.3(구경 대비 0.6%)", True)
    assert stock_change("-0.3", "", "Ø50", r) == ("연마량 -0.3 삭제", False)
    assert stock_change("", "-1.0", "Ø500", r) == ("연마량 -1.0 추가(구경 대비 0.2%)", True)  # 1mm 이상
    assert stock_change("", "-0.1", "피스톤 2개", r) == ("연마량 -0.1 추가(구경 모름)", False)  # 구경 모름: 0.2mm
    assert stock_change("", "-0.2", "피스톤 2개", r) == ("연마량 -0.2 추가(구경 모름)", True)
    assert stock_change("", "많이", "Ø50", r) == ("연마량 많이 추가(값 해석 못함)", True)
