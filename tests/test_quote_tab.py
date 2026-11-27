"""임가공 '견적' 탭 — 당월 탭과 같은 열 배치로 견적만 (사다리·사람 확인·즉시 학습 동일, 작업완료·발행 없음),
30일 지난 행은 '[견적만]' 붙이고 동결, 견적 탭 학습 단가가 당월 탭 같은 사양 행에 동일품 high로."""
from datetime import date, timedelta

import pytest

import feedback
import issuer.outbox as ob
import pricer
import watcher
from tests.test_human_quote_status import StubAi
from tests.test_issuer_job import JOB_COLS, REG
from tests.test_status_rename_cycle import JOB_SCFG, RecWS
from tests.test_watcher_legacy import make_watcher
from tests.test_watcher_settle import age_all
from watcher import QUOTE_SUFFIX, ST_DELETED, ST_LEGACY, ST_QUOTED, ST_QUOTED_EDIT

S, P, B, ID = (JOB_COLS[k] - 1 for k in ("status", "unit_price", "basis", "row_id"))
E = "Ø50+0.0~+0.05 / 300 / 2개"
TODAY = date.today()   # pricer·즉시 학습은 실제 오늘 기준 → watcher도 같은 날로


def job_row(day: date, e=E, client="가사", work_end="FALSE"):
    row = [""] * 28
    row[0], row[1], row[4], row[9] = f"{day.month}/{day.day}", client, e, work_end
    return row


class GC:
    def __init__(self, *tabs):
        self.tabs = list(tabs)

    def open_by_key(self, _key):
        return self

    def worksheets(self):
        return list(self.tabs)


class Rig:
    def __init__(self, quote_rows, month_rows=()):
        self.w = make_watcher([], clients=[("가사", REG, 5)], scfg=JOB_SCFG, header=[""] * 28)
        self.month = RecWS([[""] * 28, *month_rows], [""] * 28)
        self.month.title, self.month.index = f"{TODAY.month}월", 0
        self.quote = RecWS([[""] * 28, *quote_rows], [""] * 28)
        self.quote.title, self.quote.index = "견적", 1
        self.w.gc = GC(self.month, self.quote)
        self.w.today_fn = lambda: TODAY
        self.w.cfg = {"sheets": {"imgagong": JOB_SCFG}}
        self.w.common["status_values"] = watcher.load_config()["common"]["status_values"]
        self.w.post_quote = watcher.post_quote_states(self.w.common["status_values"])
        self.w.defer_flush = True
        feedback.ensure_columns(self.w.con)
        self.p = pricer.Pricer({"sheets": {"imgagong": JOB_SCFG}}, self.w.con)
        self.p._ai = StubAi()   # 이력 없음 → AI 20000 low → 견적확인필요

    def cycle(self):
        self.w.process_sheet("imgagong", JOB_SCFG)
        self.p.run_once(self.w.snapshots)
        self.w.flush_writes()
        self.w.con.commit()

    def settle(self):
        self.cycle()
        age_all(self.w)   # 견적 탭 확정 대기(1분)·당월(5분) 모두 지남
        self.cycle()

    def q(self, n=0):
        return self.quote.values[2 + n]

    def m(self, n=0):
        return self.month.values[2 + n]


def confirmed_rig():
    """견적 탭 행 견적 → 사람이 단가 45000 + '확인' → 견적완료(사람수정) + 즉시 학습."""
    rig = Rig([job_row(TODAY, work_end="TRUE")])   # 작업완료 TRUE여도 견적 탭은 무시 (기존건 아님)
    rig.cycle()
    assert rig.q()[S] == "입력중"
    age_all(rig.w)
    rig.cycle()
    assert (rig.q()[S], rig.q()[P]) == (pricer.ST_CHECK, "20000")
    assert QUOTE_SUFFIX and "imgagong" + QUOTE_SUFFIX in rig.w.snapshots
    rig.q()[S], rig.q()[P] = "확인", "45000"
    rig.cycle()
    assert rig.q()[S] == ST_QUOTED_EDIT and "사람 수정 45000 (에이전트 20000)" in rig.q()[B]
    return rig


def test_quote_tab_quote_confirm_learn_then_month_same_item_high():
    rig = confirmed_rig()
    got = rig.w.con.execute("SELECT date, unit_price, source, row_id FROM lines").fetchall()
    assert got == [(TODAY.isoformat(), 45000, "sheet", rig.q()[ID])]   # 날짜 = 견적 탭 행 날짜
    rig.month.values.append(job_row(TODAY))   # 당월 탭에 같은 사양 행
    rig.settle()
    assert rig.m()[S] == ST_QUOTED and rig.m()[P] == "45000"
    assert "동일품" in rig.m()[B] and rig.m()[B].endswith("| high")


def test_row_copied_with_id_to_month_gets_new_id():
    rig = confirmed_rig()
    qid = rig.q()[ID]
    rig.month.values.append(list(rig.q()))   # 행ID·상태·단가째 복사
    rig.cycle()
    assert rig.m()[ID] != qid and rig.q()[ID] == qid
    assert rig.w.con.execute("SELECT tab, status FROM rows WHERE row_id=?", (qid,)).fetchone() == ("견적", ST_QUOTED_EDIT)


def test_quote_tab_never_issued(tmp_path):
    rig = confirmed_rig()
    iss = ob.Issuer({"sheets": {"imgagong": JOB_SCFG}, "common": {"author_ai_value": "AI"}, "issue": {}}, rig.w.con,
                    dry_run=True, today=TODAY)
    iss.run_once(rig.w.snapshots)
    assert iss.candidates(rig.w.snapshots) == []
    assert rig.q()[S] == ST_QUOTED_EDIT   # 명세표발행대기로 안 감


def test_quote_tab_settle_one_minute(monkeypatch):
    rig = Rig([job_row(TODAY)])
    rig.cycle()
    past = (watcher.datetime.now() - timedelta(seconds=90)).isoformat(timespec="seconds")
    rig.w.con.execute("UPDATE rows SET last_change=?", (past,))   # 1분 30초 → 견적 탭은 확정(기본 1분)
    rig.cycle()
    assert rig.q()[S] == pricer.ST_CHECK
    assert rig.q()[S] != ST_LEGACY


def test_old_quote_row_frozen_then_deleted():
    old = TODAY - timedelta(days=40)
    rig = Rig([job_row(old)])
    rig.settle()
    assert rig.q()[B] == "[견적만]" and rig.q()[S] == "" and rig.q()[P] == ""   # 견적·상태 쓰기 없음
    rig.q()[4] = "Ø60 / 1개"   # 바뀌어도 그대로
    rig.settle()
    assert rig.q()[B] == "[견적만]" and rig.q()[S] == ""
    rid = rig.q()[ID]
    del rig.quote.values[2]
    rig.cycle()
    assert rig.w.con.execute("SELECT status FROM rows WHERE row_id=?", (rid,)).fetchone() == (ST_DELETED,)


def test_quoted_row_turns_frozen_keeps_basis():
    rig = confirmed_rig()
    basis = rig.q()[B]
    old = TODAY - timedelta(days=31)
    rig.q()[0] = f"{old.month}/{old.day}"
    rig.cycle()
    assert rig.q()[B] == f"[견적만] {basis}" and rig.q()[S] == ST_QUOTED_EDIT
    rig.cycle()
    assert rig.q()[B] == f"[견적만] {basis}"   # 한 번만


def test_no_quote_tab_unchanged():
    rig = Rig([])
    rig.w.gc.tabs = [rig.month]
    rig.month.values.append(job_row(TODAY))
    rig.settle()
    assert "imgagong" + QUOTE_SUFFIX not in rig.w.snapshots and rig.m()[S] == pricer.ST_CHECK
