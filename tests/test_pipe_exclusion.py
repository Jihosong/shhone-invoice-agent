"""임가공 이력 검색에서 호닝파이프 제외 — 품목명 호닝파이프·소관파이프·소관·H.P 줄은 규격 모양과 무관하게 pipe,
cases.load_job_lines(동일품·유사품·거래처 단가·AI 참고 사례의 공통 입력)는 kind와 무관하게 품목명으로도 뺀다.
원인 사례: 2026-11-11 선기산업 '호닝파이프 160-190-2340' 860,000원 — '-' 구분자라 옛 규칙에서 job으로 분류."""
import sqlite3
from datetime import date

import pytest

import feedback
import sheet_lines
from cases import LineIndex, load_job_lines
from issuer.excel_upload import Line
from pricer_job import M_DIRECTIVE, JobInput, quote_ladder

REG = "111-11-11111"


@pytest.mark.parametrize("item,spec,dims", [
    ("호닝파이프", "160-190-2340", (160, 190, 2340)),     # 선기산업 사례 ('-' 구분자)
    ("호닝파이프", "80x90 L760", (80, 90, 760)),          # 호닝파이프 시트 즉시 학습 줄 규격 (pipe_line)
    ("호닝파이프 소관", "80x90 L760", (80, 90, 760)),
    ("호닝파이프", "45x55 1228", (45, 55, 1228)),
    ("호닝파이프", "40/50/1191", (40, 50, 1191)),
    ("소관파이프", "Ø50", (None, None, None)),
    ("H.P", "", (None, None, None)),
])
def test_pipe_items_always_pipe(item, spec, dims):
    p = feedback.parse_spec(spec, item)
    assert p["kind"] == "pipe" and p["dia"] is None
    assert (p["pipe_id"], p["pipe_od"], p["length"]) == dims
    assert feedback.category_of(item) == "기성품"


@pytest.mark.parametrize("item", ["시린더호닝", "시린더막힘", "SHP 가공", "HPU 블럭"])
def test_job_items_not_pipe(item):
    assert not feedback.is_pipe_item(item)
    assert feedback.parse_spec("Ø50 / 300", item)["kind"] == "job"


def db():
    con = sqlite3.connect(":memory:")
    feedback.ensure_columns(con)
    return con


def add(con, item, spec, price, kind, source="export", d="2026-11-11", dia=None, length=None):
    con.execute("INSERT INTO lines(date, client, item, spec, qty, unit_price, supply, kind, dia, length, source, reg_no)"
                " VALUES(?,?,?,?,2,?,?,?,?,?,?,?)", (d, "(주)선기산업", item, spec, price, price, kind, dia, length,
                                                    source, REG))


def test_load_job_lines_excludes_pipe_items_even_if_kind_job():
    con = db()
    add(con, "호닝파이프", "160-190-2340", 860_000, "job", dia=160.0, length=2340.0)   # 옛 분류 그대로인 DB
    add(con, "소관파이프", "Ø50", 30_000, "job", dia=50.0)
    add(con, "H.P", "Ø60", 30_000, "job", dia=60.0)
    add(con, "시린더호닝", "Ø160 / 2380", 250_000, "job", dia=160.0, length=2380.0)
    assert [ln.item for ln in load_job_lines(con)] == ["시린더호닝"]


def test_sheet_learned_pipe_line_not_job():
    con = db()
    sheet_lines.add_sheet_lines(con, "P1", "(주)선기산업", REG, date(2026, 11, 20),
                                [Line("호닝파이프", "160x190 L2340", 2, 860_000)])
    assert con.execute("SELECT kind, category FROM lines WHERE row_id='P1'").fetchone() == ("pipe", "기성품")
    assert load_job_lines(con) == []


def test_reclassify_existing_db():
    con = db()
    add(con, "호닝파이프", "160-190-2340", 860_000, "job", dia=160.0, length=2340.0)
    add(con, "호닝파이프", "80x90 L760", 50_000, "job", source="sheet", dia=80.0)
    add(con, "시린더호닝", "Ø160 / 2380", 250_000, "job", dia=160.0, length=2380.0)
    assert feedback.reclassify_pipe_lines(con) == 2
    rows = con.execute("SELECT item, kind, dia, pipe_id, pipe_od, length, category FROM lines ORDER BY id").fetchall()
    assert rows == [("호닝파이프", "pipe", None, 160.0, 190.0, 2340.0, "기성품"),
                    ("호닝파이프", "pipe", None, 80.0, 90.0, 760.0, "기성품"),
                    ("시린더호닝", "job", 160.0, None, None, 2380.0, None)]
    assert feedback.reclassify_pipe_lines(con) == 0   # 멱등


def test_sunki_row_no_longer_matches_pipe_line():
    """선기산업 Ø160 / 2380 / 2개 행: 860,000 호닝파이프 줄은 동일품·참고 사례 후보가 아님."""
    con = db()
    add(con, "호닝파이프", "160-190-2340", 860_000, "job", dia=160.0, length=2340.0)
    idx = LineIndex.build(load_job_lines(con))
    inp = JobInput.from_raw({"spec_qty": "Ø160+0.0~+0.05/2380/2개"}, "(주)선기산업", REG, date(2026, 11, 27))
    q = quote_ladder(inp, idx, date(2026, 11, 28))
    assert q.method != "동일품" and q.method != M_DIRECTIVE and "860,000" not in q.basis
