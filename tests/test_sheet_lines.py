"""시트 확정 단가 즉시 학습 (lines source='sheet') + 월간 import 때 공식 줄로 대체 + job_quotes 에이전트 vs 사람."""
from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

import feedback
import issuer.outbox as ob
import pricer
import sheet_lines
from cases import load_job_lines
from issuer.excel_upload import Line
from pricer_job import M_SAME
from tests.test_issuer_job import JOB, con_db, job_row
from tests.test_pricer_ai import COLS, REG, FakeWriter, sheet_row

TODAY = date.today()   # 운영 pricer는 date.today() 기준
E = "Ø50+0.0~+0.05 / 300 / 2개"


def export_line(con, d, price, dia=50.0, reg=REG, client="가사", spec="Ø50 +0~+0.05 / 300"):
    con.execute("INSERT INTO lines(date, client, item, spec, qty, unit_price, supply, kind, dia, tol_lo, tol_hi, length,"
                " source, reg_no, category) VALUES(?,?,?,?,1,?,?,'job',?,0.0,0.05,300,'export',?,'임가공')",
                (d.isoformat(), client, "시린더호닝", spec, price, price, dia, reg))


def row_snap(*rows):
    return {"label": "임가공", "ws": SimpleNamespace(title=f"{TODAY.month}월"), "rows": [
        {"rownum": n, "row_id": rid, "row": row, "fp": "fp-" + rid, "status": st, "author": "", "legacy": False,
         "client_resolved": "가사", "client_reg_no": REG, "block_date": TODAY.isoformat()}
        for n, rid, row, st in rows]}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(pricer, "SheetWriter", FakeWriter)
    monkeypatch.setattr("pricer_job.FREEZE_CONF", "mid")
    con = con_db()
    p = pricer.Pricer({}, con)
    p._ai = False   # AI 없음 — 사다리만
    scfg = {"columns": COLS, "data_start_row": 3, "hash_columns": ["client", "main_msg", "spec_qty"]}
    return p, scfg, con


def sheet_rows(con):
    return con.execute("SELECT row_id, date, client, reg_no, item, spec, qty, unit_price, kind, dia, length, note"
                       " FROM lines WHERE source='sheet' ORDER BY id").fetchall()


# ---------------------------------------------------------------- 사람 확정 → 같은 달 재주문 동일품 high
def test_human_fix_then_same_month_reorder_is_high_with_new_price(env):
    p, scfg, con = env
    export_line(con, TODAY - timedelta(days=250), 40000)   # 6~12개월 전 → 동결 유지 mid
    p.process_job("imgagong", scfg, row_snap((5, "R1", sheet_row(e=E), "확정")))
    w = FakeWriter.last.writes
    assert (w[(5, "unit_price")], w[(5, "status")]) == (40000, pricer.ST_CHECK)
    assert w[(5, "basis")].startswith("[가견적-mid] 동일품") and "동결 유지 — 확인" in w[(5, "basis")]
    assert sheet_rows(con) == []   # 에이전트 가견적만으로는 학습 안 함

    # 사람이 단가를 45,000으로 고침 → 견적완료 + lines(source='sheet') 즉시 추가
    p.process_job("imgagong", scfg, row_snap((5, "R1", sheet_row(e=E, status="견적확인필요", price="45,000",
                                                                basis=w[(5, "basis")]), "견적확인필요")))
    assert FakeWriter.last.writes[(5, "status")] == pricer.ST_QUOTED
    assert sheet_rows(con) == [("R1", TODAY.isoformat(), "가사", REG, "시린더호닝", "Ø50 +0~+0.05 / 300", 2.0, 45000,
                                "job", 50.0, 300.0, "시트 확정")]   # 규격 표기는 직전 명세표 그대로 (발행 줄과 같음)
    # job_quotes: 에이전트 단가 vs 사람 단가 둘 다 남음
    assert con.execute("SELECT price, agent_price, agent_method, agent_conf, human_price FROM job_quotes"
                       " WHERE row_id='R1'").fetchone() == ("45000", "40000", M_SAME, "mid", "45000")

    # 같은 달 재주문 → 새 단가로 동일품 high (6개월 이내)
    p.process_job("imgagong", scfg, row_snap((7, "R2", sheet_row(e="Ø50+0.0~+0.05 / 300 / 3개"), "확정")))
    w = FakeWriter.last.writes
    assert (w[(7, "unit_price")], w[(7, "status")]) == (45000, pricer.ST_QUOTED)
    assert w[(7, "basis")].startswith(f"동일품 | {TODAY.isoformat()} 45,000원")
    assert con.execute("SELECT agent_price, human_price FROM job_quotes WHERE row_id='R2'").fetchone() == ("45000", None)


def test_human_clears_status_learns_agent_price(env):
    p, scfg, con = env
    export_line(con, TODAY - timedelta(days=250), 40000)
    p.process_job("imgagong", scfg, row_snap((5, "R1", sheet_row(e=E), "확정")))
    basis = FakeWriter.last.writes[(5, "basis")]
    # 사람이 상태 칸을 비움(내용 그대로) → 적힌 단가로 견적완료 + 학습
    p.process_job("imgagong", scfg, row_snap((5, "R1", sheet_row(e=E, status="", price="40000", basis=basis), "확정")))
    assert FakeWriter.last.writes[(5, "status")] == pricer.ST_QUOTED
    assert [r[7] for r in sheet_rows(con)] == [40000]
    assert con.execute("SELECT agent_price, human_price, method FROM job_quotes").fetchone() == ("40000", "40000",
                                                                                                "사람확인")


def test_dry_run_does_not_learn(env):
    p, scfg, con = env
    export_line(con, TODAY - timedelta(days=250), 40000)
    p.process_job("imgagong", scfg, row_snap((5, "R1", sheet_row(e=E), "확정")))
    p.dry_run = True
    p.process_job("imgagong", scfg, row_snap((5, "R1", sheet_row(e=E, status="견적확인필요", price="45000",
                                                                basis="[가견적-mid] x"), "견적확인필요")))
    assert sheet_rows(con) == []


# ---------------------------------------------------------------- add_sheet_lines
def test_same_row_replaces_previous_sheet_lines():
    con = con_db()
    sheet_lines.add_sheet_lines(con, "R1", "가사", REG, TODAY, [Line("시린더호닝", "Ø50 / 300", 2, 45000)])
    sheet_lines.add_sheet_lines(con, "R1", "가사", REG, TODAY, [Line("시린더호닝", "Ø35", 1, 40000),
                                                               Line("시린더호닝", "Ø40", 3, 25000)])
    assert [(r[9], r[7], r[6]) for r in sheet_rows(con)] == [(35.0, 40000, 1.0), (40.0, 25000, 3.0)]
    assert con.execute("SELECT supply FROM lines WHERE dia=40").fetchone() == (75000,)


def test_backtest_sees_official_lines_only():
    con = con_db()
    export_line(con, TODAY - timedelta(days=30), 40000)
    sheet_lines.add_sheet_lines(con, "R1", "가사", REG, TODAY, [Line("시린더호닝", "Ø50 / 300", 2, 45000)])
    assert [x.unit_price for x in load_job_lines(con)] == [40000, 45000]
    assert [x.unit_price for x in load_job_lines(con, official_only=True)] == [40000]


# ---------------------------------------------------------------- 월간 import → 공식 줄로 대체
def test_replace_by_official_rules():
    con = con_db()
    d = date(2026, 11, 26)
    sheet_lines.add_sheet_lines(con, "A", "가사", REG, d, [Line("시린더호닝", "Ø50 / 300", 2, 45000)])
    sheet_lines.add_sheet_lines(con, "B", "가사", REG, d, [Line("시린더호닝", "Ø80", 1, 60000)])          # 구경 다름
    sheet_lines.add_sheet_lines(con, "C", "가사", REG, d + timedelta(days=1), [Line("시린더호닝", "Ø50", 1, 1)])  # 날짜 다름
    sheet_lines.add_sheet_lines(con, "D", "가사", "", d, [Line("시린더호닝", "Ø50.2", 1, 1)])           # 번호 없음 → 이름
    sheet_lines.add_sheet_lines(con, "E", "나사", "999-99-99999", d, [Line("시린더호닝", "Ø50", 1, 1)])  # 다른 거래처
    sheet_lines.add_sheet_lines(con, "F", "가사", REG, d, [Line("모델 X-100", "", 1, 9000)])          # 구경 없음
    export_line(con, d, 46000, dia=50.1)
    con.execute("INSERT INTO lines(date, client, item, spec, qty, unit_price, kind, source, reg_no)"
                " VALUES(?, '가사', '모델 X-100', '', 1, 9000, 'job', 'export', ?)", (d.isoformat(), REG))
    assert sheet_lines.replace_by_official(con) == 3
    assert [r[0] for r in sheet_rows(con)] == ["B", "C", "E"]


def test_monthly_import_replaces_sheet_lines(tmp_path, monkeypatch):
    db = tmp_path / "agent.db"
    import sqlite3

    con = sqlite3.connect(db)
    feedback.ensure_columns(con)
    d = date(2026, 11, 26)
    sheet_lines.add_sheet_lines(con, "R1", "가사", REG, d, [Line("시린더호닝", "Ø50+0.0~+0.05 / 300", 2, 45000)])
    sheet_lines.add_sheet_lines(con, "R2", "가사", REG, d, [Line("시린더호닝", "Ø63.5", 1, 70000)])
    con.commit()
    con.close()
    st = pd.DataFrame([{"거래일": d.isoformat(), "거래처명": "가사", "등록번호": REG, "item": "시린더호닝",
                        "spec": "Ø50 +0~+0.05 / 300", "extra": 0, "공급가액": 90000, "세액": 9000, "합계금액": 99000,
                        "컬러": None}])
    ln = pd.DataFrame([{"작성일자": d.isoformat(), "회사명": "가사", "item": "시린더호닝", "spec": "Ø50 +0~+0.05 / 300",
                        "수량": 2, "단가": 45000, "공급가": 90000, "비고": None}])
    monkeypatch.setattr(feedback, "load_statements", lambda p: st)
    monkeypatch.setattr(feedback, "load_lines", lambda p: ln)
    feedback.main("s.xls", "l.xls", str(db), tmp_path / "master.xlsx")
    con = sqlite3.connect(db)
    rows = con.execute("SELECT source, row_id, dia, unit_price FROM lines ORDER BY source, id").fetchall()
    assert rows == [("export", None, 50.0, 45000), ("sheet", "R2", 63.5, 70000)]   # Ø50은 공식 줄로 대체
    # 마스터 엑셀 거래내역은 공식 줄만
    assert len(pd.read_excel(tmp_path / "master.xlsx", sheet_name="거래내역")) == 1


# ---------------------------------------------------------------- AI발행 마감 → 학습
def test_ai_issue_close_learns_block_rows(monkeypatch):
    con = con_db()
    iss = ob.Issuer({"sheets": {"imgagong": JOB}, "common": {"author_ai_value": "AI"}, "issue": {}}, con,
                    today=date(2026, 11, 26))
    rows = [job_row("막힘 Ø50+0.0~+0.05 / 300 / 2개", "45000"), job_row("Ø35 / 1개 + Ø40 / 3개", "40000/25000")]
    rows[0][14] = "AI"   # 사람이 업로드 후 작성자 AI
    s = {"label": "11월", "ws": SimpleNamespace(title="11월"), "rows": [
        {"rownum": i + 3, "row_id": f"J{i}", "row": r, "fp": "f", "status": "발행대기", "author": "", "legacy": False,
         "block_id": "B1", "client_resolved": "가사", "client_reg_no": REG, "block_date": "11/26"}
        for i, r in enumerate(rows)]}
    con.execute("INSERT INTO issue_file_blocks(block_id, file_name, sheet) VALUES('B1', 'f.xls', 'imgagong')")

    class W:
        def __init__(self, *a):
            pass

        def add(self, *a, **k):
            pass

        def flush(self, ws, dry_run=False):
            return 0

    monkeypatch.setattr(ob, "SheetWriter", W)
    iss.process_sheet("imgagong", JOB, s)
    got = [(r[0], r[1], r[4], r[9], r[6], r[7], r[11]) for r in sheet_rows(con)]
    assert got == [("J0", "2026-11-26", "막힘 시린더호닝", 50.0, 2.0, 45000, "AI발행"),
                   ("J1", "2026-11-26", "시린더호닝", 35.0, 1.0, 40000, "AI발행"),
                   ("J1", "2026-11-26", "시린더호닝", 40.0, 3.0, 25000, "AI발행")]
    # 한 번 더 돌아도 (이미 AI발행) 중복 없음
    iss.process_sheet("imgagong", JOB, s)
    assert len(sheet_rows(con)) == 3
