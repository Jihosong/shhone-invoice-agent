"""임가공 발행 트리거 = 작업시작(I) + 명세표발행대기 → 파일 담기 유예 + 발행후수정-확인.

- 묶음 안 모든 품목 행 작업시작 TRUE + 그중 하나 이상 FALSE→TRUE 전환을 봄 → 명세표발행대기. 작업완료(J)는 안 봄.
- 처음 봤을 때 이미 작업시작 TRUE(+ 작성자 없음) → 기존건-확인필요. 트리거 칸이 바뀐 옛 DB 행은 전환으로 안 봄.
- 명세표발행대기 뒤 bundle_grace_min(2분) 안에 H·D·E·F가 바뀌면 견적완료 이후 변경 규칙(큰 H 재견적 등) + 유예 다시 셈.
- 파일에 담긴 뒤·발행완료 뒤 단가 칸 변경 → 발행후수정-확인, '확인' → 원래 상태. S 변경은 표시만.
"""
from datetime import datetime, timedelta

import xlrd
from gspread.utils import a1_to_rowcol

import feedback
import pricer
import watcher
from issuer.outbox import Issuer
from tests.test_issuer_job import REG
from tests.test_post_quote_change import E50, HighAi, NoteAi
from tests.test_status_rename_cycle import RecWS
from tests.test_watcher_legacy import make_watcher
from tests.test_watcher_settle import age_all
from watcher import (ST_ISSUED, ST_LEGACY, ST_PENDING, ST_POST_EDIT, ST_QUOTED, ST_TYPING, load_config)

COLS = {"date": 1, "client": 2, "main_msg": 4, "spec_qty": 5, "difficulty": 6, "stock_removal": 8, "work_start": 9,
        "work_end": 10, "author": 15, "pre_note": 19, "status": 22, "unit_price": 23, "basis": 24, "row_id": 26}
SCFG = {"spreadsheet_id": "x", "label": "임가공", "kind": "job", "data_start_row": 3, "columns": COLS,
        "hash_columns": ["client", "main_msg", "spec_qty", "difficulty", "stock_removal"],
        "item_columns": ["spec_qty", "main_msg"], "issue_trigger": "work_start"}
S, P, B, ID = COLS["status"] - 1, COLS["unit_price"] - 1, COLS["basis"] - 1, COLS["row_id"] - 1
E, H, I, J, PRE = (COLS[k] - 1 for k in ("spec_qty", "stock_removal", "work_start", "work_end", "pre_note"))
AUTHOR = COLS["author"] - 1
E60 = "Ø60 +0.0~+0.03 / 300 / 1개"
MEMO = "메모 (AI)"


class Rig:
    def __init__(self, *specs, ai=None, start="FALSE", grace=2):
        rows = [[""] * 28]
        for i, spec in enumerate(specs):
            row = [""] * 28
            if i == 0:
                row[0], row[1] = "11/26", "가사"
            row[E], row[I], row[J] = spec, start, "FALSE"
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
        self.p._ai = ai or HighAi()
        cfg = {"sheets": {"imgagong": SCFG}, "common": {"author_ai_value": "AI"},
               "issue": {"memo_text": MEMO, "bundle_grace_min": grace}}
        self.iss = Issuer(cfg, self.w.con)

    def row(self, i=0):
        return self.w.ws.values[2 + i]

    def col(self, c):
        return [r[c] for r in self.w.ws.values[2:]]

    def cycle(self, bundle=False, out=None):
        self.w.ws.batches.clear()
        self.w.process_sheet("imgagong", SCFG)
        self.p.run_once(self.w.snapshots)
        self.w.flush_writes()
        self.w.con.commit()
        path = self.iss.run_once(self.w.snapshots, bundle=bundle, out_dir=out)
        self.statuses = [u["values"][0][0] for b in self.w.ws.batches for u in b
                         if a1_to_rowcol(u["range"])[1] == S + 1]
        return path

    def quoted(self):
        self.cycle()
        age_all(self.w)
        self.cycle()
        assert set(self.col(S)) == {ST_QUOTED}
        return self

    def start(self, rows=None):
        for i in rows if rows is not None else range(len(self.w.ws.values) - 2):
            self.row(i)[I] = "TRUE"
        return self

    def grace_over(self):
        past = (datetime.now() - timedelta(minutes=3)).isoformat(timespec="seconds")
        self.w.con.execute("UPDATE rows SET pending_at=? WHERE pending_at IS NOT NULL", (past,))
        self.w.con.commit()

    def files(self):
        return [b for (b,) in self.w.con.execute("SELECT block_id FROM issue_file_blocks")]


# ---------------------------------------------------------------- 트리거 = 작업시작
def test_work_start_flip_goes_pending(tmp_path):
    rig = Rig(E50).quoted()
    rig.row()[J] = "TRUE"  # 작업완료만 체크 → 아무 일 없음
    rig.cycle()
    assert rig.col(S) == [ST_QUOTED]
    rig.row()[J] = "FALSE"
    rig.start().cycle()
    assert rig.col(S) == [ST_PENDING]


def test_all_rows_need_work_start():
    rig = Rig(E50, E60, ai=NoteAi()).quoted()
    rig.start([0]).cycle()
    assert rig.col(S) == [ST_QUOTED, ST_QUOTED]  # 다른 줄 작업시작 대기
    rig.start([1]).cycle()
    assert rig.col(S) == [ST_PENDING, ST_PENDING]


def test_work_start_true_when_first_seen_is_legacy():
    rig = Rig(E50, start="TRUE")
    rig.cycle()
    assert rig.col(S) == [ST_LEGACY]
    assert rig.w.con.execute("SELECT work_end_flipped_at FROM rows").fetchone() == (None,)


def test_trigger_switch_on_old_db_row_is_not_a_flip():
    """J 기준으로 기록된 행(작업완료 FALSE)인데 I가 이미 TRUE → 트리거를 I로 바꾼 첫 바퀴에 전환으로 보지 않음."""
    rig = Rig(E50).quoted()
    rig.w.con.execute("UPDATE rows SET trigger_col='work_end', work_end_state='FALSE'")
    rig.w.con.commit()
    rig.start().cycle()
    assert rig.col(S) == [ST_QUOTED]
    assert rig.w.con.execute("SELECT work_end_flipped_at, trigger_col FROM rows").fetchone() == (None, "work_start")
    rig.cycle()
    assert rig.col(S) == [ST_QUOTED]


# ---------------------------------------------------------------- 파일 담기 유예
def test_grace_then_bundle(tmp_path):
    rig = Rig(E50).quoted().start()
    assert rig.cycle(bundle=True, out=tmp_path) is None  # 명세표발행대기 된 바퀴 — 유예 중
    assert rig.col(S) == [ST_PENDING] and rig.files() == []
    assert rig.cycle(bundle=True, out=tmp_path) is None  # 매 바퀴 다시 표시하지 않음 (유예가 계속 밀리지 않음)
    rig.grace_over()
    path = rig.cycle(bundle=True, out=tmp_path)
    assert path is not None and rig.files() == [rig.row()[ID]]
    assert f"[→ {path.name}]" in rig.row()[B]


def test_grace_restarts_on_h_change():
    rig = Rig(E50).quoted().start()
    rig.cycle()
    rig.grace_over()
    rig.row()[H] = "-0.1"  # 작은 H (0.2%) → 표시만, 그래도 유예 다시 셈
    assert rig.cycle(bundle=True) is None
    assert rig.col(S) == [ST_PENDING] and rig.files() == []


def test_grace_large_h_requotes_then_bundles_new_price(tmp_path):
    ai = NoteAi((36000, "high"))
    rig = Rig(E50, ai=ai).quoted()
    assert rig.row()[P] == "30000"
    rig.start().cycle()
    assert rig.col(S) == [ST_PENDING]
    rig.row()[H] = "-0.5"  # 1.0% → 재견적
    rig.cycle(bundle=True, out=tmp_path)
    assert rig.statuses == [ST_TYPING] and rig.files() == []
    age_all(rig.w)
    rig.cycle(bundle=True, out=tmp_path)  # 확정 → 재견적 견적완료 (같은 바퀴에는 상태가 막 바뀌어 대기 표시 안 함)
    assert (rig.row()[S], rig.row()[P]) == (ST_QUOTED, "36000")
    assert ai.notes[-1].startswith("연마량 -0.5 추가됨")
    assert rig.cycle(bundle=True, out=tmp_path) is None  # 다시 명세표발행대기 + 유예 새로
    assert rig.col(S) == [ST_PENDING]
    rig.grace_over()
    path = rig.cycle(bundle=True, out=tmp_path)
    sh = xlrd.open_workbook(str(path)).sheet_by_index(0)
    assert sh.cell_value(1, 13) == 36000  # 새 단가로 파일


def test_grace_requote_one_row_keeps_block_together(tmp_path):
    rig = Rig(E50, E60, ai=NoteAi((36000, "high"))).quoted().start()
    rig.cycle()
    rig.row(0)[H] = "-0.5"
    rig.cycle(bundle=True, out=tmp_path)
    assert rig.col(S) == [ST_TYPING, ST_PENDING]
    age_all(rig.w)
    rig.cycle()
    rig.cycle()
    assert rig.col(S) == [ST_PENDING, ST_PENDING]
    rig.grace_over()
    assert rig.cycle(bundle=True, out=tmp_path) is not None
    assert len(rig.files()) == 1


def test_pipe_style_zero_grace_bundles_same_cycle(tmp_path):
    rig = Rig(E50, grace=0).quoted().start()
    assert rig.cycle(bundle=True, out=tmp_path) is not None


# ---------------------------------------------------------------- 파일에 담긴 뒤
def filed(tmp_path, ai=None):
    rig = Rig(E50, ai=ai).quoted().start()
    rig.cycle()
    rig.grace_over()
    path = rig.cycle(bundle=True, out=tmp_path)
    assert path is not None
    return rig, path


def test_filed_h_change_post_edit_then_ok(tmp_path):
    rig, path = filed(tmp_path)
    rig.row()[H] = "-0.5"
    rig.cycle()
    assert rig.col(S) == [ST_POST_EDIT] and rig.row()[P] == "30000"  # 재견적 없음
    assert rig.row()[B].startswith(
        f"[발행 후 변경: H -0.5 추가 → 경리나라 수정 여부 확인] [가사 11/26 1/1] [→ {path.name}] "), rig.row()[B]
    age_all(rig.w)
    rig.cycle()
    assert rig.col(S) == [ST_POST_EDIT]
    rig.row()[S] = "확인"
    rig.cycle()
    assert rig.col(S) == [ST_PENDING] and not rig.row()[B].startswith("[발행 후 변경")
    rig.row()[AUTHOR] = "AI"  # 업로드 후 마감
    rig.cycle()
    assert rig.col(S) == [ST_ISSUED]


def test_issued_e_change_post_edit_then_ok(tmp_path):
    rig, _ = filed(tmp_path)
    rig.row()[AUTHOR] = "AI"
    rig.cycle()
    assert rig.col(S) == [ST_ISSUED]
    rig.row()[E] = "Ø50 +0.0~+0.03 / 300 / 3개"
    rig.cycle()
    assert rig.col(S) == [ST_POST_EDIT] and rig.row()[B].startswith("[발행 후 변경: E ")
    rig.row()[S] = "확인"
    rig.cycle()
    assert rig.col(S) == [ST_ISSUED]


def test_issued_s_change_only_tags(tmp_path):
    rig, _ = filed(tmp_path)
    rig.row()[AUTHOR] = "AI"
    rig.cycle()
    rig.row()[PRE] = "입구 찍힘"
    rig.cycle()
    assert rig.col(S) == [ST_ISSUED]
    assert "S 입구 찍힘 추가]" in rig.row()[B] and "발행 후 변경" not in rig.row()[B]


def test_close_while_post_edit_keeps_flag_until_ok(tmp_path):
    rig, _ = filed(tmp_path)
    rig.row()[H] = "-0.5"
    rig.cycle()
    rig.row()[AUTHOR] = "AI"  # 확인 전에 업로드·작성자 AI
    rig.cycle()
    assert rig.col(S) == [ST_POST_EDIT]
    assert rig.w.con.execute("SELECT issued_at IS NOT NULL FROM issue_file_blocks").fetchone() == (1,)
    rig.row()[S] = "확인"
    rig.cycle()
    assert rig.col(S) == [ST_ISSUED]
