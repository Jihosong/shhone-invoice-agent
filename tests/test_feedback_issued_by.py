"""월간 import: 명세표 목록의 공통메모가 '(AI)'로 끝나면 statements.issued_by='AI'."""
import sqlite3

import pandas as pd
import pytest

import feedback

MEMO = "(주)삼화호닝 기업 451-079229-04-018, 감사합니다. (AI)"


def write_stm(path, memos=None):
    rows = [["2026-11-20", "호닝파이프 / 80x90 L760", "대진엔지니어링", "111-11-11111", 51700, 5170, 56870, None, None],
            ["2026-11-21", "호닝파이프 / 40x50 L500 외 2건", "대진엔지니어링", "111-11-11111", 100000, 10000, 110000, None, None],
            ["2026-11-22", "임가공 / Ø50", "새로운상사", "888-88-88888", 30000, 3000, 33000, None, "메모"]]
    cols = ["거래일", "품목/규격", "거래처명", "등록번호", "공급가액", "세액", "합계금액", "컬러", "메모"]
    if memos is not None:
        cols.append("공통메모")
        rows = [r + [m] for r, m in zip(rows, memos)]
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame([["매출거래명세표 목록"]]).to_excel(xw, index=False, header=False, startrow=0)
        pd.DataFrame(rows, columns=cols).to_excel(xw, index=False, startrow=1)
    return path


def write_lines(path):
    cols = ["작성일자", "회사명", "품목", "규격", "수량", "단가", "공급가", "부가세", "합계", "비고"]
    rows = [["2026-11-20", "대진엔지니어링", "호닝파이프", "80x90 L760", 1, 51700, 51700, 5170, 56870, None]]
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(rows, columns=cols).to_excel(xw, index=False, startrow=2)
    return path


def run(tmp_path, memos):
    db = tmp_path / "agent.db"
    feedback.main(write_stm(tmp_path / "stm.xlsx", memos), write_lines(tmp_path / "lines.xlsx"), db,
                  tmp_path / "master.xlsx")
    con = sqlite3.connect(db)
    return [r for r in con.execute("SELECT date, issued_by FROM statements ORDER BY date")]


def test_ai_memo_marks_issued_by(tmp_path, capsys):
    got = run(tmp_path, [MEMO, "사람이 쓴 메모", None])
    assert got == [("2026-11-20", "AI"), ("2026-11-21", None), ("2026-11-22", None)]
    assert "AI 발행 명세표" in capsys.readouterr().out


def test_no_common_memo_column_skips_with_log(tmp_path, capsys):
    got = run(tmp_path, None)
    assert [b for _, b in got] == [None, None, None]
    assert "공통메모 열 없음" in capsys.readouterr().out


@pytest.mark.parametrize("memo, want", [(MEMO, "AI"), ("감사합니다. （AI） ", "AI"), ("AI 확인", None),
                                        ("", None), (None, None), (float("nan"), None)])
def test_issued_by_of(memo, want):
    assert feedback.issued_by_of(memo) == want


def test_existing_db_gets_issued_by_column(tmp_path):
    con = sqlite3.connect(tmp_path / "old.db")
    con.execute("CREATE TABLE statements(id INTEGER PRIMARY KEY, date TEXT, client TEXT, reg_no TEXT, item TEXT,"
                " spec TEXT, extra_lines INTEGER, supply INTEGER, tax INTEGER, total INTEGER, color TEXT, memo TEXT,"
                " source TEXT DEFAULT 'export')")
    feedback.ensure_columns(con)
    assert "issued_by" in {r[1] for r in con.execute("PRAGMA table_info(statements)")}
