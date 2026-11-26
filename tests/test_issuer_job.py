"""임가공 발행: 줄 만들기(job_lines — 품목명·규격·수량·단가) + 발행 조건 + 호닝파이프와 한 파일에 섞기."""
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest
import xlrd

import feedback
import watcher
from cases import DbLine, LineIndex
from issuer.excel_upload import Line, LineCtx, LineError, job_lines, job_name, job_segments, job_spec
from issuer.outbox import Issuer

REG = "111-11-11111"
DAY = date(2026, 11, 26)
MEMO = "(주)삼화호닝 기업 451-079229-04-018, 감사합니다. (AI)"


def get(e, d="", price="45000"):
    cells = {"main_msg": d, "spec_qty": e, "unit_price": price}
    return lambda name: cells.get(name, "")


def ctx(lines=()):
    return LineCtx("가사", REG, DAY, LineIndex.build(list(lines)))


def ln(i, d, item, spec, dia, length=None, lo=0.0, hi=0.05, price=45000, reg=REG):
    return DbLine(i, date.fromisoformat(d), "가사", reg, item, spec, 1, price, price, dia, length, lo, hi)


# ---------------------------------------------------------------- job_lines
def test_one_item_generated_label():
    assert job_lines(get("Ø50+0.0~+0.05 / 300 / 2개"), ctx()) == [
        Line("시린더호닝", "Ø50+0.0~+0.05 / 300", 2, 45000)]


def test_two_items_different_prices():
    e = "SUSØ35+0.0~+0.03 / 330 / 1개 + SUSØ40+0.0~+0.03 / 175 / 3개"
    assert job_lines(get(e, price="40000/25,000"), ctx()) == [
        Line("SUS 시린더호닝", "Ø35+0.0~+0.03 / 330", 1, 40000),
        Line("SUS 시린더호닝", "Ø40+0.0~+0.03 / 175", 3, 25000)]


def test_two_items_one_price_applies_to_all():
    e = "Ø30.18 +0~+0.02 / 20개 + Ø70 +0~+0.02 / 10개"
    got = job_lines(get(e, price="55,000"), ctx())
    assert [(x.spec, x.qty, x.unit_price) for x in got] == [("Ø30.18 +0~+0.02", 20, 55000),
                                                            ("Ø70 +0~+0.02", 10, 55000)]


def test_follows_history_label():
    hist = [ln(1, "2026-03-24", "엔겔 시린더(1차원통연마)", "Ø50 +0~+0.05 / 300", 50.0, 300),
            ln(2, "2025-01-10", "옛 이름", "옛 규격", 50.0, 300)]
    assert job_lines(get("막힘 Ø50+0.0~+0.05 / 300 / 2개"), ctx([ln(9, "2026-01-01", "막힘시린더", "x", 50.0, 300)])
                     )[0].name == "막힘시린더"
    got = job_lines(get("Ø50+0.0~+0.05 / 300 / 2개"), ctx(hist))
    assert got == [Line("엔겔 시린더(1차원통연마)", "Ø50 +0~+0.05 / 300", 2, 45000)]  # 최근 건 그대로


def test_history_name_but_own_spec_when_length_differs():
    hist = [ln(1, "2026-03-24", "엔겔 시린더", "Ø50 +0~+0.05 / 290", 50.0, 290)]  # 기장 3% 차이 = 동일품
    got = job_lines(get("Ø50+0.0~+0.05 / 300 / 2개"), ctx(hist))[0]
    assert (got.name, got.spec) == ("엔겔 시린더", "Ø50+0.0~+0.05 / 300")


def test_no_history_other_client_or_future_ignored():
    other = [DbLine(1, date(2026, 3, 24), "나사", "999-99-99999", "남의 이름", "s", 1, 1, 1, 50.0, 300, 0.0, 0.05),
             ln(2, "2026-11-27", "내일 이름", "s", 50.0, 300)]
    assert job_lines(get("Ø50+0.0~+0.05 / 300 / 2개"), ctx(other))[0].name == "시린더호닝"


@pytest.mark.parametrize("text,name", [("막힘 Ø50", "막힘 시린더호닝"), ("사각 SUS Ø50", "SUS 사각 시린더호닝"),
                                       ("수리품 면작업 Ø100", "수리품 면작업"), ("열처리 후 호닝 Ø9.55", "열처리 시린더호닝"),
                                       ("AL Ø50 용접", "알미늄 용접 시린더호닝"), ("Ø50 긴급 Ra0.2", "시린더호닝")])
def test_job_name(text, name):
    assert job_name(text) == name


@pytest.mark.parametrize("e,specs", [
    ("Ø50+0.0~+0.05 / 300 / 2개 긴급", ["Ø50+0.0~+0.05 / 300"]),
    ("Ø11.005~Ø11.014 / 190 / 1개", ["Ø11.005~Ø11.014 / 190"]),          # 범위는 한 품목
    ("Ø75 -0.005 × 2개", ["Ø75 -0.005"]),
    ("Ø14+-0.1 /1개 Ø15+-0.1 / 4개 + Ø16+-0.1 / 4개", ["Ø14+-0.1", "Ø15+-0.1", "Ø16+-0.1"]),
])
def test_segments_and_spec(e, specs):
    assert [job_spec(s) for s in job_segments(e)] == specs


def test_ra_prefix_kept_in_first_spec():
    got = job_lines(get("Ra0.4 Ø11 -0.012~+0.015 / 1개"), ctx())[0]
    assert got.spec == "Ra0.4 Ø11 -0.012~+0.015"


@pytest.mark.parametrize("e,price,msg", [("Ø50 / 2개", "", "단가"), ("Ø50 / 1개 + Ø60 / 1개", "1/2/3", "단가 3개"),
                                         ("STO-401 / 10개", "1/2", "단가 2개 ≠ 품목 1개")])
def test_job_lines_errors(e, price, msg):
    with pytest.raises(LineError, match=msg):
        job_lines(get(e, price=price), ctx())


def test_missing_qty_defaults_to_one():
    assert job_lines(get("Ø50 / 300"), ctx()) == [Line("시린더호닝", "Ø50 / 300", 1, 45000)]


# ---------------------------------------------------------------- 구경(Ø) 없는 행
def test_no_dia_name_is_text_without_qty():
    e = "STO-401 #2  NO2 각도30 조도 Ra0.8~1.0 / 10개"   # '30 조도'는 수량(조)이 아님
    assert job_lines(get(e, price="6800"), ctx()) == [Line("STO-401 #2 NO2 각도30 조도 Ra0.8~1.0", "", 10, 6800)]


def test_no_dia_uses_d_when_e_empty_and_keeps_text_after_qty():
    assert job_lines(get("", d="펌프케이스 3개 긴급", price="50000"), ctx()) == [Line("펌프케이스 긴급", "", 3, 50000)]


def test_no_dia_follows_history_by_text():
    hist = [ln(1, "2026-01-05", "70K 각도30 조도 Ra 0.8~1.0", "Ø45.43 0~ 0.05", 45.43),
            ln(2, "2025-01-05", "70K 각도30 조도 Ra 0.8~1.0", "옛 규격", 45.43)]
    got = job_lines(get("70K 각도30 조도 Ra 0.8~1.0 / 448개", price="6800"), ctx(hist))
    assert got == [Line("70K 각도30 조도 Ra 0.8~1.0", "Ø45.43 0~ 0.05", 448, 6800)]
    # 다른 거래처·짧은 글은 이력으로 안 봄
    other = [DbLine(3, date(2026, 1, 5), "나사", "999-99-99999", "70K 각도30 조도 Ra 0.8~1.0", "s", 1, 6800, 6800,
                    45.43, None, 0.0, 0.05)]
    assert job_lines(get("70K 각도30 조도 Ra 0.8~1.0 / 1개"), ctx(other))[0].spec == ""


def test_parse_qty_set_unit_not_jodo():
    from job_parse import parse_qty
    assert parse_qty("각도30 조도 Ra0.8 / 10개") == 10 and parse_qty("Ø50 / 3조") == 3


def test_dia_from_d_when_e_has_none():
    got = job_lines(get("3개", d="Ø45+0.0~+0.03 / 735"), ctx())
    assert got == [Line("시린더호닝", "Ø45+0.0~+0.03 / 735", 3, 45000)]


# ---------------------------------------------------------------- 발행 조건 + 혼합 파일
PIPE_COLS = {"date": 1, "client": 2, "pipe_type": 3, "size": 4, "length": 5, "qty": 6, "work_end": 7, "shipped": 8,
             "author": 9, "status": 10, "unit_price": 11, "basis": 12, "row_id": 13}
JOB_COLS = {"date": 1, "client": 2, "main_msg": 4, "spec_qty": 5, "difficulty": 6, "stock_removal": 8,
            "work_end": 10, "author": 15, "status": 24, "unit_price": 25, "basis": 26, "row_id": 28}
PIPE = {"kind": "pipe", "data_start_row": 2, "columns": PIPE_COLS, "hash_columns": ["client", "size"]}
JOB = {"kind": "job", "data_start_row": 3, "columns": JOB_COLS, "hash_columns": ["client", "main_msg", "spec_qty"]}


def con_db():
    con = sqlite3.connect(":memory:")
    watcher.ensure_columns(con)
    feedback.ensure_columns(con)
    return con


def pipe_row(status="발행대기"):
    return ["11/26", "파이프사", "H.P", "80x90", "760", "1개", "TRUE", "FALSE", "", status, "39290", "", "P1"]


def job_row(e, price, status="발행대기", d=""):
    row = [""] * 28
    row[0], row[1], row[3], row[4], row[9], row[23], row[24], row[27] = "11/26", "가사", d, e, "TRUE", status, price, "J"
    return row


def snap(rows, client, reg, tab="11월"):
    return {"label": tab, "ws": SimpleNamespace(title=tab),
            "rows": [{"rownum": i + 3, "row_id": f"{client}{i}", "row": r, "fp": "f", "status": r[23 if len(r) > 13 else 9],
                      "author": "", "legacy": False, "block_id": f"B-{client}", "client_resolved": client,
                      "client_reg_no": reg, "block_date": "11/26", "work_end_flipped_at": "2026-11-26T10:00:00"}
                     for i, r in enumerate(rows)]}


def issuer(con, sheets):
    cfg = {"sheets": sheets, "common": {"author_ai_value": "AI"}, "issue": {"memo_text": MEMO}}
    return Issuer(cfg, con, dry_run=True, today=DAY)


def test_mixed_pipe_and_job_in_one_file(tmp_path):
    con = con_db()
    iss = issuer(con, {"pipe": PIPE, "imgagong": JOB})
    snaps = {"pipe": snap([pipe_row()], "파이프사", "222-22-22222"),
             "imgagong": snap([job_row("막힘 Ø50+0.0~+0.05 / 300 / 2개", "45000"),
                               job_row("Ø35 / 1개 + Ø40 / 3개", "40000/25000")], "가사", REG)}
    path = iss.bundle(snaps, out_dir=tmp_path)
    sh = xlrd.open_workbook(str(path)).sheet_by_index(0)
    got = [[sh.cell_value(r, c) for c in (0, 2, 3, 9, 10, 11, 13, 14, 15, 19)] for r in range(1, sh.nrows)]
    assert got == [
        ["2026-11-26", "파이프사", "222-22-22222", "호닝파이프", "80x90 L760", 1, 39290, 39290, 3929, MEMO],
        ["2026-11-26", "가사", REG, "막힘 시린더호닝", "Ø50+0.0~+0.05 / 300", 2, 45000, 90000, 9000, MEMO],
        ["", "", "", "시린더호닝", "Ø35", 1, 40000, 40000, 4000, ""],
        ["", "", "", "시린더호닝", "Ø40", 3, 25000, 75000, 7500, ""],
    ]


def test_job_check_needed_never_issues_even_with_price():
    con = con_db()
    iss = issuer(con, {"imgagong": JOB})
    rows = snap([job_row("Ø50 / 1개", "61000", status="견적확인필요")], "가사", REG)["rows"]
    assert "임가공은 견적완료만" in iss.waiting_reason(JOB, rows)
    rows_ok = snap([job_row("Ø50 / 1개", "61000", status="견적완료")], "가사", REG)["rows"]
    assert iss.waiting_reason(JOB, rows_ok) is None


def test_job_block_goes_pending(tmp_path):
    con = con_db()
    iss = issuer(con, {"imgagong": JOB})
    s = snap([job_row("Ø50 / 1개", "61000", status="견적완료")], "가사", REG)
    s["ws"] = SimpleNamespace(title="11월")
    import issuer.outbox as ob

    writes = []

    class W:
        def __init__(self, *a):
            pass

        def add(self, rownum, name, value, *a, **k):
            writes.append((rownum, name, value))

        def flush(self, ws, dry_run=False):
            return len(writes)

    orig = ob.SheetWriter
    ob.SheetWriter = W
    try:
        iss.process_sheet("imgagong", JOB, s)
    finally:
        ob.SheetWriter = orig
    assert writes == [(3, "status", "발행대기")]
