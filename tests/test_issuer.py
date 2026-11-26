"""발행기 1단계 '반자동': 발행 조건 → 발행대기 → outbox .xls → 작성자 AI → AI발행.

watcher·pricer·issuer를 가짜 시트·메모리 DB로 한 바퀴씩 구동한다.
파일 형식은 실제 업로드에 성공한 tests/fixtures/gyeongri/*.xls 와 셀 타입·값·서식을 비교한다.
"""
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import xlrd
from loguru import logger

from issuer.excel_upload import Line, LineError, Statement, block_date, pipe_line, write_xls
from issuer.outbox import Issuer, format_outbox
from pricer import PriceEntry, Pricer
from tests.test_watcher_settle import age_all, make
from watcher import ST_CONFIRMED, ST_ISSUED, ST_PENDING, ST_PENDING_EDITED, ST_QUOTED, ST_TYPING

FIX = Path(__file__).resolve().parent / "fixtures" / "gyeongri"
CLIENT, REG = "테스트거래처(삼화)", "247-81-03793"
TODAY = date(2026, 11, 26)

# A 날짜, B 회사, C 구분, D 규격, E 길이, F 갯수, G 작업완료, H 출고완료, I 작성자, J 상태, K 단가, L 산출근거, M 행ID
COLS = {"date": 1, "client": 2, "pipe_type": 3, "size": 4, "length": 5, "qty": 6, "work_end": 7, "shipped": 8,
        "author": 9, "status": 10, "unit_price": 11, "basis": 12, "row_id": 13}
SCFG = {"spreadsheet_id": "x", "label": "파이프", "kind": "pipe", "data_start_row": 2, "columns": COLS,
        "hash_columns": ["client", "pipe_type", "size", "length", "qty"], "item_columns": ["size", "length", "qty"]}
C = {name: i for i, name in enumerate(COLS)}
TABLE = {k: PriceEntry(k, Decimal(p), Decimal(1)) for k, p in {
    "80x90": 51700, "40x50": 28600, "50x60": 29700, "70x80": 42911, "60x70": 38500, "105x126": 238000}.items()}


def r(date_, client, size, length, qty, pipe_type="H.P"):
    return [date_, client, pipe_type, size, length, qty, "FALSE", "FALSE", "", "", "", "", ""]


A_ROWS = [r("11/26", CLIENT, "80x90", "760", "1개")]
B_ROWS = [r("11/26", CLIENT, "40x50", "500", "5개"), r("", "", "50x60", "500", "2개"),
          r("", "", "70x80", "500", "2개", "소관")]
C_ROWS = [r("11/26", CLIENT, "60x70", "2106", "1개"), r("11/26", CLIENT, "105x126", "580", "1개")]


class Rig:
    """가짜 시트 + watcher/pricer/issuer 한 벌."""

    def __init__(self, rows, memo=True, dry_run=False):
        self.w = make([list(x) for x in rows])
        self.w.con.execute("INSERT INTO clients(name, reg_no, n_statements) VALUES(?,?,5)", (CLIENT, REG))
        self.p = Pricer({"sheets": {"t": SCFG}}, self.w.con)
        self.p.table_for = lambda _scfg: TABLE
        cfg = {"sheets": {"t": SCFG}, "common": {"author_ai_value": "AI"}, "issue": {"memo_block_id": memo}}
        self.iss = Issuer(cfg, self.w.con, dry_run=dry_run, today=TODAY)

    @property
    def values(self):
        return self.w.ws.values

    def col(self, name, rownums=None):
        rownums = rownums or range(2, len(self.values) + 1)
        return [self.values[n - 1][C[name]] for n in rownums]

    def set(self, rownum, name, value):
        self.values[rownum - 1][C[name]] = value

    def cycle(self, bundle=False, out=None):
        self.w.process_sheet("t", SCFG)
        self.w.con.commit()
        self.p.run_once(self.w.snapshots)
        return self.iss.run_once(self.w.snapshots, bundle=bundle, out_dir=out)

    def quoted(self):
        """입력 → 5분 경과 확정 → 견적완료 (작업완료는 아직 FALSE)."""
        self.cycle()
        age_all(self.w)
        self.cycle()  # 확정 → pricer 견적완료
        assert set(self.col("status")) == {ST_QUOTED}
        return self

    def finish(self, rownums=None):
        for n in rownums or range(2, len(self.values) + 1):
            self.set(n, "work_end", "TRUE")
        return self

    def db(self, sql, *args):
        return self.w.con.execute(sql, args).fetchall()


def ready(rows, tmp_path, **kw):
    """견적완료 → 작업완료 체크 → 발행대기 + outbox 파일."""
    rig = Rig(rows, **kw).quoted().finish()
    path = rig.cycle(bundle=True, out=tmp_path)
    return rig, path


# ---------------------------------------------------------------- 파일 형식 = fixture
def _fmt(book, sheet, r, c):
    return book.format_map[book.xf_list[sheet.cell_xf_index(r, c)].format_key].format_str


def assert_like_fixture(path, fixture, memos):
    """셀 타입·값·서식이 fixture와 같음. 품목비고(16)는 우리가 비움, 공통메모(19)는 'AI <block_id>'."""
    fa = xlrd.open_workbook(str(FIX / fixture), formatting_info=True)
    ga = xlrd.open_workbook(str(path), formatting_info=True)
    assert ga.sheet_names() == ["Sheet1"]
    fs, gs = fa.sheet_by_index(0), ga.sheet_by_index(0)
    assert (gs.nrows, gs.ncols) == (fs.nrows, fs.ncols) == (fs.nrows, 20)
    for row in range(fs.nrows):
        for c in range(20):
            g = gs.cell(row, c)
            if c == 16 and row > 0:
                assert g.ctype == xlrd.XL_CELL_EMPTY, "품목비고는 비워야 함 (명세표에 인쇄됨)"
                continue
            if c == 19 and row > 0:
                expect = memos.get(row)
                assert (g.value if g.ctype else None) == expect
                if expect:
                    assert g.ctype == xlrd.XL_CELL_TEXT and _fmt(ga, gs, row, c) == "@"
                continue
            f = fs.cell(row, c)
            assert (g.ctype, g.value) == (f.ctype, f.value), (fixture, row, c)
            if f.ctype != xlrd.XL_CELL_EMPTY:
                assert _fmt(ga, gs, row, c) == _fmt(fa, fs, row, c), (fixture, row, c)


def block_ids(rig):
    return [b for (b,) in rig.db("SELECT block_id FROM issue_file_blocks ORDER BY rowid")]


def test_one_line_block_file_matches_fixture_A(tmp_path):
    rig, path = ready(A_ROWS, tmp_path)
    assert path.parent == tmp_path and path.suffix == ".xls"
    assert rig.col("status") == [ST_PENDING]
    (bid,) = block_ids(rig)
    assert_like_fixture(path, "A_1줄.xls", {1: f"AI {bid}"})


def test_three_line_block_file_matches_fixture_B(tmp_path):
    rig, path = ready(B_ROWS, tmp_path)
    assert rig.col("status") == [ST_PENDING] * 3
    (bid,) = block_ids(rig)
    assert_like_fixture(path, "B_1장3줄.xls", {1: f"AI {bid}"})  # 2·3번째 줄은 머리·메모 없음


def test_two_blocks_in_one_file_match_fixture_C(tmp_path):
    rig, path = ready(C_ROWS, tmp_path)
    b1, b2 = block_ids(rig)
    assert_like_fixture(path, "C_2장.xls", {1: f"AI {b1}", 2: f"AI {b2}"})
    assert rig.db("SELECT n_blocks, n_lines, total FROM issue_files") == [(2, 2, 81080 + 8108 + 138040 + 13804)]


def test_memo_can_be_turned_off(tmp_path):
    _, path = ready(A_ROWS, tmp_path, memo=False)
    assert_like_fixture(path, "A_1줄.xls", {})


def test_basis_shows_file_name(tmp_path):
    rig, path = ready(B_ROWS, tmp_path)
    assert rig.col("basis")[0] == f"[{CLIENT} 11/26 1/3] [→ {path.name}] 단가표 40x50 28,600/m × 500mm"
    rig.cycle()  # 다음 바퀴 watcher도 같은 표시 유지 (중복으로 붙지 않음)
    assert rig.col("basis")[2].startswith(f"[{CLIENT} 11/26 3/3] [→ {path.name}] 단가표 70x80")
    assert rig.col("status") == [ST_PENDING] * 3


# ---------------------------------------------------------------- 발행 조건
def test_partial_work_end_waits(tmp_path):
    rig = Rig(B_ROWS).quoted().finish([2, 3])
    assert rig.cycle(bundle=True, out=tmp_path) is None
    assert rig.col("status") == [ST_QUOTED] * 3
    rig.finish([4])
    assert rig.cycle(bundle=True, out=tmp_path) is not None
    assert rig.col("status") == [ST_PENDING] * 3


def test_work_end_before_quote_issues_after_quote(tmp_path):
    rig = Rig(A_ROWS)
    rig.cycle()
    rig.finish()
    rig.cycle(bundle=True, out=tmp_path)  # 작업완료 먼저 (입력중)
    assert rig.col("status") == [ST_TYPING]
    age_all(rig.w)
    assert rig.cycle(bundle=True, out=tmp_path) is None  # 확정 → 이번 바퀴 견적완료 (단가가 아직 시트 값 아님)
    assert rig.col("status") == [ST_QUOTED]
    assert rig.cycle(bundle=True, out=tmp_path) is not None
    assert rig.col("status") == [ST_PENDING]


def test_work_end_already_true_when_first_seen_never_issues(tmp_path):
    rows = [list(A_ROWS[0])]
    rows[0][C["work_end"]] = "TRUE"  # 최초 발견 때 이미 완료 → 기존건
    rig = Rig(rows)
    rig.cycle()
    age_all(rig.w)
    assert rig.cycle(bundle=True, out=tmp_path) is None
    assert rig.col("status") != [ST_PENDING]


def test_human_author_blocks_issue(tmp_path):
    rig = Rig(B_ROWS).quoted()
    rig.set(3, "author", "팀장")
    rig.finish()
    assert rig.cycle(bundle=True, out=tmp_path) is None
    assert ST_PENDING not in rig.col("status")


def test_check_needed_with_human_price_issues(tmp_path):
    rig = Rig([r("11/26", CLIENT, "99x99", "500", "1개")])  # 단가표에 없는 규격 → 견적확인필요
    rig.cycle()
    age_all(rig.w)
    rig.cycle()
    assert rig.col("status") == ["견적확인필요"]
    rig.finish()
    assert rig.cycle(bundle=True, out=tmp_path) is None  # 단가 없음 → 대기
    rig.set(2, "unit_price", "12,340")
    path = rig.cycle(bundle=True, out=tmp_path)
    assert rig.col("status") == [ST_PENDING]
    s = xlrd.open_workbook(str(path)).sheet_by_index(0)
    assert s.row_values(1)[9:16] == ["호닝파이프", "99x99 L500", 1.0, "", 12340.0, 12340.0, 1234.0]


# ---------------------------------------------------------------- 마감 / 수정
def test_author_ai_closes_block_as_issued(tmp_path):
    rig, _ = ready(B_ROWS, tmp_path)
    rig.set(3, "author", "AI")  # 사람이 업로드 후 아무 행 하나만 AI로
    rig.cycle()
    assert rig.col("status") == [ST_ISSUED] * 3
    assert rig.col("author") == ["AI"] * 3
    assert all(t for (t,) in rig.db("SELECT issued_at FROM rows"))
    assert all(t for (t,) in rig.db("SELECT issued_at FROM issue_file_blocks"))
    rig.cycle()
    assert rig.col("status") == [ST_ISSUED] * 3  # 다음 바퀴에도 유지


def test_edit_after_issued_keeps_status(tmp_path):
    rig, _ = ready(A_ROWS, tmp_path)
    rig.set(2, "author", "AI")
    rig.cycle()
    rig.set(2, "qty", "2개")
    rig.cycle()
    assert rig.col("status") == [ST_ISSUED]  # 발행후수정: 상태 유지 + 로그


def test_edit_while_pending_marks_edited(tmp_path):
    warnings = []
    sink = logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        rig, path = ready(B_ROWS, tmp_path)
        rig.set(3, "qty", "3개")
        rig.cycle()
    finally:
        logger.remove(sink)
    assert rig.col("status") == [ST_PENDING_EDITED] * 3
    assert rig.col("basis")[0].startswith(f"[{CLIENT} 11/26 1/3] [→ {path.name} 뺌(수정됨)] ")
    assert rig.db("SELECT removed_at IS NOT NULL FROM issue_file_blocks") == [(1,)]
    assert any("발행대기-수정됨" in m for m in warnings)
    # 사람이 볼 때까지 유지 — 시간이 지나도 재견적·재발행 안 함
    age_all(rig.w)
    assert rig.cycle(bundle=True, out=tmp_path) is None
    assert rig.col("status") == [ST_PENDING_EDITED] * 3
    # 사람이 정리하고 작성자 AI → AI발행
    rig.set(2, "author", "AI")
    rig.cycle()
    assert rig.col("status") == [ST_ISSUED] * 3


def test_row_added_to_pending_block_marks_whole_block_edited(tmp_path):
    rig, _ = ready(A_ROWS, tmp_path)
    rig.values.append(r("", "", "50x60", "500", "1개"))
    rig.cycle()
    assert rig.col("status") == [ST_PENDING_EDITED] * 2


def test_edit_before_file_requotes(tmp_path):
    """아직 파일에 안 담긴 발행대기는 아무도 올릴 수 없었으므로 일반 규칙(입력중 → 재견적)."""
    rig = Rig(A_ROWS).quoted().finish()
    rig.cycle()  # 발행대기 (bundle 안 함)
    assert rig.col("status") == [ST_PENDING]
    rig.set(2, "length", "800")
    rig.cycle(bundle=True, out=tmp_path)
    assert rig.col("status") == [ST_TYPING]
    assert block_ids(rig) == []


# ---------------------------------------------------------------- 중복 방지
def test_same_block_never_in_two_files(tmp_path):
    rig, first = ready(A_ROWS, tmp_path)
    assert rig.cycle(bundle=True, out=tmp_path) is None  # 이미 담긴 묶음은 다시 안 담음
    # 사람이 상태를 견적완료로 되돌려도 재발행 안 함
    rig.set(2, "status", ST_QUOTED)
    assert rig.cycle(bundle=True, out=tmp_path) is None
    assert rig.col("status") == [ST_QUOTED]
    assert [p.name for p in tmp_path.iterdir()] == [first.name]
    # DB도 막음 (동시에 두 프로세스가 묶어도)
    with pytest.raises(sqlite3.IntegrityError):
        rig.w.con.execute("INSERT INTO issue_file_blocks(block_id, file_name) VALUES(?, 'x.xls')", (block_ids(rig)[0],))


def test_new_block_goes_to_new_file(tmp_path):
    rig, first = ready(A_ROWS, tmp_path)
    rig.values.append(r("11/26", CLIENT, "60x70", "2106", "1개"))
    rig.cycle()
    age_all(rig.w)
    rig.cycle()
    rig.finish([3])
    rig.cycle()
    second = rig.cycle(bundle=True, out=tmp_path)
    assert second is not None and second.name != first.name
    assert xlrd.open_workbook(str(second)).sheet_by_index(0).nrows == 2  # 새 묶음만
    assert len(block_ids(rig)) == 2


def test_dry_run_touches_nothing(tmp_path):
    rig = Rig(A_ROWS).quoted().finish()
    rig.iss.dry_run = True
    path = rig.cycle(bundle=True)
    assert path is not None and path.exists() and path.parent != tmp_path
    assert rig.col("status") == [ST_QUOTED]
    assert rig.db("SELECT COUNT(*) FROM issue_files") == [(0,)]
    assert rig.db("SELECT status FROM rows") == [(ST_QUOTED,)]


def test_format_outbox(tmp_path):
    rig, path = ready(C_ROWS, tmp_path)
    lines = format_outbox(rig.w.con)
    assert lines[0].startswith(f"{path.name}  (") and "명세표 2장 2줄" in lines[0]
    assert lines[1].startswith(f"  t / {CLIENT} / 2026-11-26 / 1줄 89,188 / 발행대기 / 묶음 ")


# ---------------------------------------------------------------- 줄 / 값
def _get(**cells):
    return lambda name: cells.get(name, "")


def test_pipe_line_names_and_spec():
    assert pipe_line(_get(pipe_type="H.P", size="80X90", length="760", qty="5개", unit_price="39290")) == \
        Line("호닝파이프", "80x90 L760", 5, 39290)
    assert pipe_line(_get(pipe_type="소관", size="70x80", length="500", qty="2개", unit_price="19,310")).name == \
        "호닝파이프 소관"
    with pytest.raises(LineError):
        pipe_line(_get(pipe_type="H.P", size="80x90", length="760", qty="1", unit_price=""))


def test_amounts():
    ln = Line("호닝파이프", "x", 3, 4115)
    assert ln.supply == 12345 and ln.tax == 1234  # 10% 내림


def test_block_date_year():
    assert block_date("11/26", "11월", TODAY) == date(2026, 11, 26)
    assert block_date("11.26", "11월", TODAY) == date(2026, 11, 26)
    assert block_date("12/31", "12월", date(2027, 1, 3)) == date(2026, 12, 31)  # 1월에 보는 12월 탭
    assert block_date("1/2", "12월", date(2026, 12, 31)) == date(2027, 1, 2)    # 12월 탭에 적힌 새해
    assert block_date("12/30", "1월", date(2027, 1, 5)) == date(2026, 12, 30)   # 1월 탭에 적힌 작년 말
    with pytest.raises(LineError):
        block_date("2/30", "2월", TODAY)


def test_write_xls_continuation_rows_have_no_header(tmp_path):
    st = Statement("abcd1234", CLIENT, REG, TODAY, [Line("호닝파이프", "a", 1, 10), Line("호닝파이프", "b", 1, 20)])
    s = xlrd.open_workbook(str(write_xls([st], tmp_path / "x.xls"))).sheet_by_index(0)
    assert s.row_values(2)[:7] == [""] * 7 and s.row_values(2)[7] == "1126" and s.row_values(2)[19] == ""
