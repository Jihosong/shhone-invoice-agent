"""상태 이름 변경(명세표…) + 옛 이름 자동 치환 + 사람 '확인'/단가 수정 한 바퀴 처리 + API 호출 수 기록."""
import sqlite3

import pytest
from gspread.utils import a1_to_rowcol

import feedback
import pricer
import watcher
from tests.test_human_quote_status import CALIBER_E, StubAi
from tests.test_issuer import TWO_ROWS, ready
from tests.test_issuer_job import JOB_COLS, REG
from tests.test_watcher_legacy import FakeWS, make_watcher
from tests.test_watcher_settle import age_all
from watcher import (LEGACY_STATUS_NAMES, ST_CONFIRMED, ST_HUMAN_CHECK, ST_ISSUED, ST_PENDING, ST_POST_EDIT,
                     ST_PRINTED, ST_QUOTED, ST_QUOTED_EDIT, ST_QUOTED_OK, ApiMeter, SheetWriter, ensure_columns,
                     known_states, load_config)

OLD = {"발행대기", "발행대기-수정됨", "AI발행", "AI발행-인쇄됨", "사람처리-발행확인", "명세표발행대기-수정됨"}


# ---------------------------------------------------------------- 이름·설정
def test_new_names():
    assert (ST_PENDING, ST_POST_EDIT, ST_ISSUED, ST_PRINTED, ST_HUMAN_CHECK) == (
        "명세표발행대기", "발행후수정-확인", "명세표발행완료(AI)", "명세표인쇄완료(AI)", "사람처리-명세표중복확인")
    assert set(LEGACY_STATUS_NAMES) == OLD


def test_sheets_yaml_uses_new_names_and_intervals():
    cfg = load_config()
    values = cfg["common"]["status_values"]
    assert not OLD & set(values)
    assert {ST_PENDING, ST_POST_EDIT, ST_ISSUED, ST_PRINTED, ST_HUMAN_CHECK} <= set(values)
    assert not OLD & known_states(values)
    assert cfg["common"]["poll_interval_sec"] == 15 and cfg["issue"]["bundle_interval_min"] == 3
    assert cfg["common"]["quote_settle_sec"] == 15 and cfg["common"]["quote_tab_settle_sec"] == 15
    assert cfg["issue"]["bundle_grace_min"] == 1 and cfg["sheets"]["pipe"]["bundle_grace_min"] == 0
    assert cfg["sheets"]["imgagong"]["issue_trigger"] == "work_start"
    assert cfg["sheets"]["pipe"]["issue_trigger"] == "work_end"


def test_db_migration_renames_old_status():
    con = sqlite3.connect(":memory:")
    ensure_columns(con)
    con.executemany("INSERT INTO rows(row_id, status) VALUES(?,?)",
                    [(f"r{i}", old) for i, old in enumerate(sorted(OLD))] + [("keep", "견적완료")])
    ensure_columns(con)  # 시작할 때마다 (두 번째부터는 바꿀 것 없음)
    got = dict(con.execute("SELECT row_id, status FROM rows"))
    assert got["keep"] == "견적완료"
    assert sorted(v for k, v in got.items() if k != "keep") == sorted(LEGACY_STATUS_NAMES[o] for o in OLD)


# ---------------------------------------------------------------- 옛 이름이 적힌 시트 → 새 이름, 발행기 동작 그대로
def test_old_pending_name_rewritten_then_close_works(tmp_path):
    rig, _ = ready(TWO_ROWS, tmp_path)  # 명세표발행대기 + 파일에 담김
    for n in (2, 3):
        rig.set(n, "status", "발행대기")  # 옛 이름 (이름 변경 전부터 시트에 있던 행)
    rig.cycle()
    assert rig.col("status") == [ST_PENDING] * 2  # 다음 폴링에 새 이름으로
    rig.set(2, "author", "AI")  # 업로드 후 작성자 AI → 마감
    rig.cycle()
    assert rig.col("status") == [ST_ISSUED] * 2
    assert rig.col("author") == ["AI"] * 2


def test_old_pending_name_edit_detected(tmp_path):
    rig, _ = ready(TWO_ROWS, tmp_path)
    for n in (2, 3):
        rig.set(n, "status", "발행대기")
    rig.set(3, "qty", "9개")  # 파일에 담긴 뒤 수정
    rig.cycle()
    assert rig.col("status") == [ST_PENDING, ST_POST_EDIT]


@pytest.mark.parametrize("old, new", [("AI발행", ST_ISSUED), ("AI발행-인쇄됨", ST_PRINTED),
                                      ("발행대기-수정됨", ST_POST_EDIT), ("명세표발행대기-수정됨", ST_POST_EDIT)])
def test_old_names_kept_as_new(tmp_path, old, new):
    rig, _ = ready(TWO_ROWS, tmp_path)
    for n in (2, 3):
        rig.set(n, "status", old)
    rig.cycle()
    rig.cycle()
    assert rig.col("status") == [new] * 2


def test_old_human_check_name_then_clear(tmp_path):
    rig, _ = ready(TWO_ROWS, tmp_path)
    rig.set(2, "author", "고문")
    rig.cycle()
    assert rig.col("status") == [ST_HUMAN_CHECK] * 2
    for n in (2, 3):
        rig.set(n, "status", "사람처리-발행확인")  # 옛 이름
    rig.cycle()
    assert rig.col("status") == [ST_HUMAN_CHECK] * 2  # 옛 이름은 확인(비움)이 아님 → 새 이름 유지
    rig.set(2, "status", "")
    rig.cycle()
    assert rig.col("status") == ["", ""]  # 비우면 보통 사람처리


# ---------------------------------------------------------------- 한 바퀴 처리 (watcher+pricer 같은 writer)
class RecWS(FakeWS):
    """A1 범위 아무 열이나 받는 가짜 시트 + batch_update 기록."""

    def __init__(self, rows, header):
        super().__init__(rows, header)
        self.batches = []

    def batch_update(self, updates, value_input_option=None):
        self.batches.append(updates)
        for u in updates:
            r, c = a1_to_rowcol(u["range"])
            self.values[r - 1][c - 1] = str(u["values"][0][0])  # 시트는 문자열로 돌려줌


JOB_SCFG = {"spreadsheet_id": "x", "label": "임가공", "kind": "job", "data_start_row": 3, "columns": JOB_COLS,
            "hash_columns": ["client", "main_msg", "spec_qty"], "item_columns": ["spec_qty", "main_msg"]}
S, P, B = JOB_COLS["status"] - 1, JOB_COLS["unit_price"] - 1, JOB_COLS["basis"] - 1


class JobRig:
    def __init__(self):
        row = [""] * 28
        row[0], row[1], row[4], row[9] = "11/26", "가사", CALIBER_E, "FALSE"
        self.w = make_watcher([[""] * 28, row], clients=[("가사", REG, 5)], scfg=JOB_SCFG, header=[""] * 28)
        self.w.ws = RecWS(self.w.ws.values[1:], [""] * 28)
        self.w.gc.ws = self.w.ws
        self.w.cfg = {"sheets": {"imgagong": JOB_SCFG}}
        self.w.common["status_values"] = load_config()["common"]["status_values"]
        self.w.post_quote = watcher.post_quote_states(self.w.common["status_values"])
        self.w.defer_flush = True  # agent.py run과 같음
        feedback.ensure_columns(self.w.con)
        self.p = pricer.Pricer({"sheets": {"imgagong": JOB_SCFG}}, self.w.con)
        self.p._ai = StubAi()

    @property
    def row(self):
        return self.w.ws.values[2]

    def cycle(self):
        self.w.ws.batches.clear()
        self.w.process_sheet("imgagong", JOB_SCFG)
        self.p.run_once(self.w.snapshots)
        self.w.flush_writes()
        self.w.con.commit()
        return [u["values"][0][0] for b in self.w.ws.batches for u in b
                if a1_to_rowcol(u["range"])[1] == S + 1]

    def checked(self):
        """입력 → 확정 → 가견적(견적확인필요). 확정도 시트에 안 남고 바로 견적확인필요."""
        assert self.cycle() == ["입력중"]
        age_all(self.w)
        assert self.cycle() == [pricer.ST_CHECK]  # '확정'은 pricer 결과에 덮여 시트에 안 써짐
        assert len(self.w.ws.batches) == 1  # watcher+pricer 쓰기를 한 번에
        assert (self.row[S], self.row[P]) == (pricer.ST_CHECK, "20000")
        return self


@pytest.mark.parametrize("word", ["확인", "ok", ""])
def test_ok_word_goes_to_human_ok_in_one_cycle(word):
    rig = JobRig().checked()
    rig.row[S] = word
    writes = rig.cycle()
    assert writes == [ST_QUOTED_OK]  # 중간 '확정'을 시트에 쓰지 않음
    assert ST_CONFIRMED not in writes
    assert rig.row[S] == ST_QUOTED_OK and rig.row[B].startswith("[가사 11/26 1/1] 사람 확인 | AI-유사품")
    assert rig.cycle() == []  # 다음 바퀴 그대로


def test_ok_word_plus_price_edit_is_human_edit_in_one_cycle():
    rig = JobRig().checked()
    rig.row[S], rig.row[P] = "확인", "5000"
    assert rig.cycle() == [ST_QUOTED_EDIT]
    assert "사람 수정 5000 (에이전트 20000)" in rig.row[B]
    assert rig.w.con.execute("SELECT status FROM rows").fetchone() == (ST_QUOTED_EDIT,)


def test_price_edit_only_is_human_edit_in_one_cycle():
    rig = JobRig().checked()
    rig.row[P] = "5000"
    assert rig.cycle() == [ST_QUOTED_EDIT]
    assert rig.row[S] == ST_QUOTED_EDIT


def test_watcher_alone_still_flushes_itself():
    """defer_flush 없이 watcher만 돌면 예전처럼 바로 씀 (python watcher.py 등)."""
    rig = JobRig()
    rig.w.defer_flush = False
    rig.w.process_sheet("imgagong", JOB_SCFG)
    assert rig.row[S] == "입력중" and "writer" not in rig.w.snapshots["imgagong"]


def test_sheet_writer_last_value_wins():
    cols = {"status": 1, "row_id": 2}
    sw = SheetWriter(cols, 2, ["status"])
    row = ["", "abcdef01"]
    sw.add(2, "status", ST_CONFIRMED, row, row_id="abcdef01")
    sw.add(2, "status", ST_QUOTED, row, row_id="abcdef01")
    updates, dropped = sw.resolve([["h", "h"], row])
    assert updates == [{"range": "A2", "values": [[ST_QUOTED]]}] and dropped == 0


# ---------------------------------------------------------------- API 호출 수
def test_api_meter_counts_and_reports():
    from loguru import logger

    t = [0.0]
    m = ApiMeter(clock=lambda: t[0])

    class Http:
        def request(self, method, endpoint, **kw):
            return method

    class GC:
        http_client = Http()

    gc = GC()
    m.install(gc)
    m.install(gc)  # 두 번 감싸지 않음
    msgs = []
    hid = logger.add(lambda s: msgs.append(str(s)), level="INFO")
    try:
        assert m.report() is None  # 첫 호출은 기준 시각만
        t[0] = 10
        for _ in range(3):
            gc.http_client.request("get", "x")
        gc.http_client.request("post", "x")
        t[0] = 30
        assert m.report() is None  # 1분 안 됨
        t[0] = 61
        c = m.report()
    finally:
        logger.remove(hid)
    assert (c["read"], c["write"]) == (3, 1)
    assert any("지난 1분 호출 읽기=3 쓰기=1" in s for s in msgs)


def test_spreadsheet_opened_once():
    rig = JobRig()
    rig.w.defer_flush = False
    opened = []
    orig = rig.w.gc.open_by_key
    rig.w.gc.open_by_key = lambda k: opened.append(k) or orig(k)
    rig.w.process_sheet("imgagong", JOB_SCFG)
    rig.w.process_sheet("imgagong", JOB_SCFG)
    assert opened == ["x"]


def test_old_edited_name_ok_returns_to_pending(tmp_path):
    """옛 '명세표발행대기-수정됨'(파일에 담김, 마감 전) → 발행후수정-확인 → '확인' → 명세표발행대기."""
    rig, _ = ready(TWO_ROWS, tmp_path)
    for n in (2, 3):
        rig.set(n, "status", "명세표발행대기-수정됨")
    rig.cycle()
    assert rig.col("status") == [ST_POST_EDIT] * 2
    assert rig.col("basis")[0].startswith("[발행 후 변경: 예전 '수정됨' 상태 → 경리나라 수정 여부 확인] ")
    for n in (2, 3):
        rig.set(n, "status", "확인")
    rig.cycle()
    assert rig.col("status") == [ST_PENDING] * 2
