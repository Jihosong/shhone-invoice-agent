"""묶음(block) 규칙: 날짜 열로 묶음 판정, 첫 행에서만 거래처 해석 (파이프 시트 모양으로 구동)."""
import pytest

from tests.test_watcher_legacy import make_watcher
from watcher import ST_SKIP, ST_TYPING, ST_UNREG, ClientMaster

# A 날짜, B 회사, C 규격, D 길이, E 갯수, F 작업완료, G 출고완료, H 작성자, I 상태, J 행ID
COLS = {"date": 1, "client": 2, "size": 3, "length": 4, "qty": 5, "work_end": 6, "shipped": 7,
        "author": 8, "status": 9, "row_id": 10}
SCFG = {"spreadsheet_id": "x", "label": "파이프", "data_start_row": 2, "columns": COLS,
        "hash_columns": ["client", "size", "length", "qty"], "item_columns": ["size", "length", "qty"]}
HEADER = ["날짜", "회사", "규격", "길이", "갯수", "작업완료", "출고완료", "작성자", "상태", "행ID"]
SKIP = ["대성연마", "삼화연마", "한국유압", "한국하이드로릭", "영성기공"]
MASTER = [("영성기공", "1", 5), ("영성정밀", "2", 5), ("오성기계공구", "3", 4),
          ("(주)대양상사", "134-81-32293", 15), ("대양상사", "127-14-79906", 2),
          ("주식회사 대성정밀", "4", 3), ("대성ENG", "5", 1)]


def r(date, client, size="", length="", qty="", author="", status="", row_id=""):
    return [date, client, size, length, qty, "FALSE", "FALSE", author, status, row_id]


def poll(rows, **kw):
    w = make_watcher(rows, skip_clients=SKIP, clients=MASTER, scfg=SCFG, header=HEADER, **kw)
    w.process_sheet("t", SCFG)
    return w, {x["rownum"]: x for x in w.snapshots["t"]["rows"]}


CAPTURE = [
    r("11/23", "영성기공", "80x90", "2670", "2개"),
    r("", "교체(이전것은 2개에서 1개로 바꿔놓음)", "90x100", "3420", "1개"),
    r("11/23", "대성연마 4~5시", "60x70", "1000", "3개"),
    r("11/24", "오성기계공구(택배)", "45x55", "1500", "2개"),
    r("11/24", "대양상사panjang sama", "50x60", "2000", "1개"),
]


def test_capture_rows():
    w, snap = poll(CAPTURE)
    first, cont = snap[2], snap[3]
    assert first["client_resolved"] == cont["client_resolved"] == "영성기공"
    assert first["block_id"] == cont["block_id"] == first["row_id"] != cont["row_id"]
    assert first["client_note"] == ""
    assert cont["client_note"] == "교체(이전것은 2개에서 1개로 바꿔놓음)"
    assert first["status"] == cont["status"] == ST_SKIP

    assert (snap[4]["client_resolved"], snap[4]["client_note"], snap[4]["status"]) == ("대성연마", "4~5시", ST_SKIP)
    assert (snap[5]["client_resolved"], snap[5]["client_note"]) == ("오성기계공구", "(택배)")
    assert (snap[6]["client_resolved"], snap[6]["client_note"]) == ("(주)대양상사", "panjang sama")
    assert snap[5]["status"] == snap[6]["status"] == ST_TYPING
    # DB에도 기록
    assert w.con.execute("SELECT block_id, client_resolved, client_note FROM rows WHERE row_id=?",
                         (cont["row_id"],)).fetchone() == (first["row_id"], "영성기공", cont["client_note"])


def test_continuation_client_text_never_resolved():
    """날짜 없는 행의 회사 칸이 마스터 이름이어도 거래처로 보지 않음 — 윗 묶음 소속."""
    _, snap = poll([r("11/24", "오성기계공구", "45x55", "1", "1개"), r("", "(주)대양상사", "50x60", "1", "1개")])
    assert snap[3]["client_resolved"] == "오성기계공구"
    assert snap[3]["client_note"] == "(주)대양상사"
    assert snap[3]["block_id"] == snap[2]["row_id"]


def test_rows_above_first_block_and_memo_rows_skipped():
    w, snap = poll([
        r("", "탭 맨 위 메모", "45x55", "1", "1개"),            # 2: 첫 묶음 위 → 건너뜀
        r("11/24", "오성기계공구", "45x55", "1000", "2개"),       # 3
        r("", "오후 3시 픽업", status="입력중"),                 # 4: 메모 행 → 상태 비움
        r("", "", "50x60", "1000", "1개"),                       # 5: 품목 행
    ])
    assert set(snap) == {3, 5}
    assert snap[5]["block_id"] == snap[3]["row_id"]
    assert w.ws.values[1][9] == ""          # 맨 위 행엔 row_id 안 씀
    assert w.ws.values[3][8] == ""          # 메모 행 상태 비움
    assert w.ws.values[3][9] == ""          # 메모 행 row_id 없음


def test_memo_first_row_still_gives_block_id():
    w, snap = poll([r("11/24", "오성기계공구 내일 출고"), r("", "", "45x55", "1000", "2개")])
    assert set(snap) == {3}
    assert snap[3]["client_resolved"] == "오성기계공구"
    assert snap[3]["block_id"] == w.ws.values[1][9] != ""


def test_unregistered_block():
    _, snap = poll([r("11/24", "모르는상사", "45x55", "1", "1개"), r("", "추가", "50x60", "1", "1개")])
    assert snap[2]["status"] == snap[3]["status"] == ST_UNREG
    assert snap[2]["client_resolved"] is None


def test_human_author_on_first_row_covers_block():
    w, snap = poll([r("11/24", "오성기계공구", "45x55", "1", "1개", author="팀장"),
                    r("", "", "50x60", "1", "1개")])
    assert snap[2]["status"] == snap[3]["status"] == ""
    assert snap[3]["author"] == "팀장"  # pricer가 건너뜀


@pytest.mark.parametrize("text,name,note", [
    ("㈜대양상사", "(주)대양상사", ""),
    ("대양 상사 2차", "(주)대양상사", "2차"),
    ("대성정밀(주) 급함", "주식회사 대성정밀", "급함"),
    ("영성정밀기계", "영성정밀", "기계"),
    ("대성", None, "대성"),
])
def test_master_longest_match(text, name, note):
    m = ClientMaster([(n, w) for n, _, w in MASTER])
    assert m.resolve(text) == (name, note)
