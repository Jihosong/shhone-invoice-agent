"""기존건 규칙 + 작성자 우선순위 (watcher.process_sheet를 가짜 시트·메모리 DB로 구동)."""
import sqlite3
from datetime import date

import pytest

from watcher import ST_CONFIRMED, ST_LEGACY, ST_SKIP, ST_TYPING, Watcher, ensure_columns, post_quote_states

# A 날짜, B 거래처, C 작업완료, D 출고완료, E 작성자, F 상태, G 행ID
COLS = {"date": 1, "client": 2, "work_end": 3, "shipped": 4, "author": 5, "status": 6, "row_id": 7}
SCFG = {"spreadsheet_id": "x", "label": "테스트", "data_start_row": 2, "columns": COLS,
        "hash_columns": ["client"]}
STATUS_VALUES = [ST_TYPING, ST_CONFIRMED, "견적완료", "견적확인필요", "명세표발행대기", "진행중", "명세표발행완료(AI)",
                 ST_SKIP, ST_LEGACY]


class FakeWS:
    title = "11월"
    index = 0

    def __init__(self, rows, header=None):
        self.values = [header or ["날짜", "거래처", "작업완료", "출고완료", "작성자", "상태", "행ID"]] + rows

    def get_all_values(self):
        return [list(r) for r in self.values]

    def batch_update(self, updates, value_input_option=None):
        for u in updates:
            col = ord(u["range"][0]) - ord("A")
            row = int(u["range"][1:]) - 1
            self.values[row][col] = u["values"][0][0]


class FakeGC:
    def __init__(self, ws):
        self.ws = ws

    def open_by_key(self, _key):
        return self

    def worksheets(self):
        return [self.ws]


def make_watcher(rows, skip_clients=(), clients=("가", "나", "다"), scfg=SCFG, header=None):
    w = Watcher.__new__(Watcher)
    w.cfg = {"sheets": {"t": scfg}}
    w.common = {"month_tab_pattern": r"^\d{1,2}월$", "quote_settle_min": 5, "author_ai_value": "AI",
                "skip_clients": list(skip_clients), "status_values": STATUS_VALUES}
    w.dry_run = False
    w.ws = FakeWS(rows, header)
    w.gc = FakeGC(w.ws)
    w.con = sqlite3.connect(":memory:")
    ensure_columns(w.con)
    w.con.execute("CREATE TABLE clients(name TEXT, reg_no TEXT, n_statements INTEGER, first_date TEXT,"
                  " last_date TEXT)")
    # 거래처: 이름 | (이름, 번호, 명세표수) | (이름, 번호, 명세표수, 최근거래일)
    w.con.executemany("INSERT INTO clients(name, reg_no, n_statements, last_date) VALUES(?,?,?,?)",
                      [(c + (None,))[:4] if isinstance(c, tuple) else (c, "", 1, None) for c in clients])
    w.snapshots = {}
    w.post_quote = post_quote_states(STATUS_VALUES)
    w.today_fn = lambda: date(2026, 11, 26)  # 감시 탭 선택(오늘 달 'N월')이 실제 날짜에 흔들리지 않게 — FakeWS 탭은 '11월'
    return w


def row(client, work_end="FALSE", shipped="FALSE", author="", status="", row_id=""):
    return ["11/24", client, work_end, shipped, author, status, row_id]


def poll(w):
    w.process_sheet("t", SCFG)
    return {r["row"][1]: r for r in w.snapshots["t"]["rows"]}


def db(w, client_row_id):
    return w.con.execute(
        "SELECT status, legacy, work_end_state, shipped_state, work_end_flipped_at FROM rows WHERE row_id=?",
        (client_row_id,)).fetchone()


def test_blank_author_legacy_gets_legacy_status():
    w = make_watcher([row("가", work_end="TRUE"), row("나", shipped="TRUE")])
    snap = poll(w)
    for c in ("가", "나"):
        assert snap[c]["legacy"] and snap[c]["status"] == ST_LEGACY
    assert w.ws.values[1][5] == ST_LEGACY


def test_human_author_legacy_is_human_handled():
    w = make_watcher([row("가", work_end="TRUE", shipped="TRUE", author="팀장")])
    snap = poll(w)
    r = snap["가"]
    assert r["legacy"] is True  # 판정 자체는 그대로
    assert r["status"] == ""    # 상태는 비움 (사람처리)
    assert r["work_end_flipped_at"] is None
    assert w.ws.values[1][5] == ""
    status, legacy, we, sh, flipped = db(w, r["row_id"])
    assert (status, legacy, we, sh, flipped) == ("", 1, "TRUE", "TRUE", None)


def test_human_author_clears_existing_legacy_status_on_sheet():
    w = make_watcher([row("가", work_end="TRUE", author="사장", status=ST_LEGACY)])
    poll(w)
    assert w.ws.values[1][5] == ""


def test_states_recorded_for_every_row():
    w = make_watcher([row("가"), row("나", work_end="TRUE", author="팀장"), row("다", shipped="TRUE")])
    snap = poll(w)
    assert db(w, snap["가"]["row_id"])[2:4] == ("FALSE", "FALSE")
    assert db(w, snap["나"]["row_id"])[2:4] == ("TRUE", "FALSE")
    assert db(w, snap["다"]["row_id"])[2:4] == ("FALSE", "TRUE")


def test_human_author_legacy_stays_legacy_and_never_becomes_issue_candidate():
    w = make_watcher([row("가", work_end="TRUE", author="팀장")])
    poll(w)
    # 체크 해제 → 재체크 해도 기존건이므로 발행 후보 아님
    w.ws.values[1][2] = "FALSE"
    poll(w)
    w.ws.values[1][2] = "TRUE"
    snap = poll(w)
    assert snap["가"]["legacy"] and snap["가"]["work_end_flipped_at"] is None
    # 나중에 작성자를 지우면 '기존건-확인필요'로 드러남
    w.ws.values[1][4] = ""
    snap = poll(w)
    assert snap["가"]["status"] == ST_LEGACY


def test_blank_author_legacy_becomes_human_when_author_set():
    w = make_watcher([row("가", shipped="TRUE")])
    poll(w)
    w.ws.values[1][4] = "사장"
    snap = poll(w)
    assert snap["가"]["legacy"] and snap["가"]["status"] == ""


def test_legacy_status_without_legacy_column_still_sticky():
    """legacy 열 도입 전 DB: status='기존건-확인필요'로 기존건 유지."""
    w = make_watcher([row("가", row_id="abcd1234")])
    w.con.execute("INSERT INTO rows(row_id, fingerprint, last_change, status, work_end_state)"
                  " VALUES('abcd1234', 'x', '2026-11-01T00:00:00', ?, 'TRUE')", (ST_LEGACY,))
    snap = poll(w)
    assert snap["가"]["legacy"] and snap["가"]["status"] == ST_LEGACY
    assert db(w, "abcd1234")[1] == 1


def test_known_row_flip_is_issue_candidate():
    w = make_watcher([row("가")])
    poll(w)
    w.ws.values[1][2] = "TRUE"
    snap = poll(w)
    assert not snap["가"]["legacy"] and snap["가"]["work_end_flipped_at"]
    assert snap["가"]["status"] == ST_TYPING


@pytest.mark.parametrize("author", ["", "AI"])
def test_non_legacy_rows_unaffected(author):
    w = make_watcher([row("가", author=author)])
    snap = poll(w)
    assert not snap["가"]["legacy"] and snap["가"]["status"] == ST_TYPING


@pytest.mark.parametrize("work_end,shipped,author", [
    ("TRUE", "FALSE", ""),       # 기존건(작업완료) — 기존건-확인필요보다 우선
    ("FALSE", "TRUE", ""),       # 기존건(출고완료)
    ("TRUE", "TRUE", "팀장"),    # 사람 작성 기존건 — 사람처리보다 우선
    ("FALSE", "FALSE", ""),      # 신규
    ("FALSE", "FALSE", "AI"),
])
def test_skip_client_always_skipped(work_end, shipped, author):
    w = make_watcher([row("월말상사", work_end=work_end, shipped=shipped, author=author)],
                     skip_clients=["월말상사"])
    snap = poll(w)
    assert snap["월말상사"]["status"] == ST_SKIP
    assert w.ws.values[1][5] == ST_SKIP


def test_skip_client_stays_skipped_after_flip():
    w = make_watcher([row("월말상사")], skip_clients=["월말상사"])
    poll(w)
    w.ws.values[1][2] = "TRUE"
    snap = poll(w)
    assert snap["월말상사"]["status"] == ST_SKIP
