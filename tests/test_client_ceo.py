"""동명 거래처를 괄호 안 대표자명으로 구분 + 거래처 목록(대표자명) import."""
import sqlite3
from datetime import date

import pandas as pd
import pytest

import feedback
from tests.test_watcher_settle import C, make, poll, r
from watcher import ST_HCHECK, ST_HOMONYM, ClientMaster, load_client_master

TODAY = date(2026, 11, 26)
# 이름이 같은 두 사업자 — 둘 다 최근 거래 있음 → 최근거래 규칙으로는 못 정함
DAEJIN = [("대진엔지니어링", "111-11-11111", 5, "2026-11-01", "김영주"),
          ("대진엔지니어링", "222-22-22222", 3, "2026-10-01", "박민수")]
# 동명이인: 대표자명까지 같음
SEJONG = [("세종정밀", "333-33-33333", 2, "2026-11-01", "이철수"), ("세종정밀", "444-44-44444", 2, "2026-11-02", "이철수")]
GEUMGANG = [("(주)금강ENG", "487-88-00321", 9, "2026-11-01", "홍길동"),
            ("금강ENG", "130-46-26653", 4, "2026-11-01", "김철수"),
            ("금강ENG", "121-64-64259", 1, "2025-08-01", "최영희")]  # homonyms에서 뺀 개인
HOMONYMS = {"금강ENG": {"법인": "487-88-00321", "개인": "130-46-26653", "기본": "법인"}}


def master(entries=DAEJIN + SEJONG + GEUMGANG, **kw):
    return ClientMaster(entries, kw.pop("aliases", None), TODAY, kw.pop("homonyms", HOMONYMS), None)


# ---------------------------------------------------------------- 대표자명 일치
@pytest.mark.parametrize("text", ["대진엔지니어링 (김영주)", "대진엔지니어링(김영주)", "대진엔지니어링  ( 김영주 )",
                                  "대진엔지니어링（김영주）", "대진엔지니어링 （ 김 영 주 ）"])
def test_ceo_in_parens_confirms(text):
    res = master().resolve_full(text)
    assert (res.name, res.reg_no, res.hold, res.note) == ("대진엔지니어링", "111-11-11111", None, "")
    assert res.basis == "대표명 일치 김영주"


def test_other_ceo_picks_other_reg():
    res = master().resolve_full("대진엔지니어링(박민수)")
    assert res.reg_no == "222-22-22222" and res.basis == "대표명 일치 박민수"


def test_two_parens_one_ceo_one_note():
    res = master().resolve_full("대진엔지니어링(택배)(김영주) 4~5시")
    assert (res.reg_no, res.note) == ("111-11-11111", "(택배) 4~5시")
    res = master().resolve_full("대진엔지니어링 (박민수) (TEST)")
    assert (res.reg_no, res.note) == ("222-22-22222", "(TEST)")


@pytest.mark.parametrize("text,note", [("대진엔지니어링(택배)", "(택배)"), ("대진엔지니어링 (TEST)", "(TEST)"),
                                       ("대진엔지니어링(4~5시)", "(4~5시)"), ("대진엔지니어링(홍길동)", "(홍길동)")])
def test_unknown_paren_is_note(text, note):
    """어떤 대표자명과도 안 맞는 괄호는 비고 — 대표자명 확정 없이 기존 규칙(둘 다 최근거래 → 보류-동명)."""
    res = master().resolve_full(text)
    assert (res.note, res.hold, res.basis) == (note, ST_HOMONYM, "")


def test_single_reg_ceo_paren_dropped_from_note():
    res = master([("오성기계공구", "555-55-55555", 3, "2026-11-01", "정우성")]).resolve_full("오성기계공구(정우성) 택배")
    assert (res.reg_no, res.note, res.hold) == ("555-55-55555", "택배", None)
    res = master([("오성기계공구", "555-55-55555", 3, "2026-11-01", "정우성")]).resolve_full("오성기계공구(택배)")
    assert res.note == "(택배)"  # 지금과 동일


def test_same_ceo_two_regs_not_confirmed():
    """동명이인(대표자명도 같음) → 최근거래로 추측하지 않고 보류."""
    res = master().resolve_full("세종정밀(이철수)")
    assert res.hold == ST_HOMONYM and res.reg_no is None


def test_ceo_beats_recent_rule():
    only_recent = [("대양상사", "666-66-66666", 9, "2026-11-01", "가나다"), ("대양상사", "777-77-77777", 1, "2023-01-01", "라마바")]
    m = master(only_recent)
    assert m.resolve_full("대양상사").basis == "동명→최근거래 666-66-66666"
    res = m.resolve_full("대양상사(라마바)")
    assert (res.reg_no, res.basis) == ("777-77-77777", "대표명 일치 라마바")


# ---------------------------------------------------------------- homonyms 이름 + 대표자명
@pytest.mark.parametrize("text,reg", [("금강ENG(김철수)", "130-46-26653"), ("금강ENG （홍길동）", "487-88-00321"),
                                      ("주)금강ENG(홍길동)", "487-88-00321"), ("금강ENG(개인)(김철수)", "130-46-26653")])
def test_homonym_ceo_confirms(text, reg):
    res = master().resolve_full(text)
    assert (res.reg_no, res.hold) == (reg, None)
    assert res.basis.startswith("대표명 일치 ")


def test_homonym_notice_lists_ceo_and_reg():
    res = master().resolve_full("금강ENG")
    assert res.hold == ST_HCHECK and res.reg_no == "487-88-00321"  # 기본 쪽으로 가견적
    assert res.notice == ("동명확인: (주)금강ENG(홍길동) 487-88-00321 / 금강ENG(김철수) 130-46-26653"
                          " → 괄호에 대표명 또는 주)/(개인)을 적어주세요")


def test_homonym_notice_omits_blank_ceo():
    entries = [("(주)금강ENG", "487-88-00321", 9, "2026-11-01", None), GEUMGANG[1]]
    assert master(entries).resolve_full("금강ENG").notice == (
        "동명확인: (주)금강ENG 487-88-00321 / 금강ENG(김철수) 130-46-26653 → 괄호에 대표명 또는 주)/(개인)을 적어주세요")


def test_homonym_mark_and_ceo_conflict_is_check():
    res = master().resolve_full("주)금강ENG(김철수)")  # 법인 표시인데 대표자명은 개인 쪽
    assert res.hold == ST_HCHECK


def test_homonym_same_ceo_both_sides_is_check():
    entries = [("(주)금강ENG", "487-88-00321", 9, "2026-11-01", "홍길동"), ("금강ENG", "130-46-26653", 4, "2026-11-01", "홍길동")]
    m = master(entries)
    assert m.resolve_full("금강ENG(홍길동)").hold == ST_HCHECK  # 동명이인 → 동명확인 유지
    assert m.resolve_full("금강ENG(홍길동)(개인)").reg_no == "130-46-26653"  # 표시가 있으면 표시대로


def test_homonyms_yaml_beats_ceo():
    """yaml 명시 지정이 대표자명보다 우선: homonyms에서 뺀 번호의 대표자명은 안 봄, 별칭 사업자번호는 최우선."""
    res = master().resolve_full("금강ENG(최영희)")  # 121-64-64259는 homonyms에 없음
    assert res.hold == ST_HCHECK and res.note == "(최영희)"
    m = master(aliases={"금강ENG(김철수)": "487-88-00321"})
    res = m.resolve_full("금강ENG (김철수)")
    assert (res.reg_no, res.hold) == ("487-88-00321", None)


# ---------------------------------------------------------------- watcher: 산출근거 표시
def test_watcher_basis_shows_ceo_match():
    w = make([r("11/25", "대진엔지니어링 ( 김영주 )"), r("", "", "50x60")])
    w.con.execute("ALTER TABLE clients ADD COLUMN ceo_name TEXT")
    w.con.executemany("INSERT INTO clients(name, reg_no, n_statements, last_date, ceo_name) VALUES(?,?,?,?,?)", DAEJIN)
    w.alias_path = None
    snap = poll(w)
    assert snap[2]["client_reg_no"] == "111-11-11111" and snap[2]["client_note"] == ""
    assert w.ws.values[1][C["basis"]] == "[대진엔지니어링 대표명 일치 김영주 11/25 1/2]"


# ---------------------------------------------------------------- 거래처 목록 import
@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    feedback.ensure_columns(con)
    con.execute("INSERT INTO clients(name, reg_no, n_statements, last_date, source) VALUES"
                " ('대진엔지니어링', '111-11-11111', 5, '2026-11-01', 'import')")
    return con


def write_list(path, header_rows=1):
    rows = [["거래처 목록"] + [""] * 3] * (header_rows - 1) + [
        ["상호(거래처명)", "사업자 등록번호", "대표자명", "전화"],
        ["대진엔지니어링", "1111111111", "김영주", "02"],
        ["새로운상사", "888-88-88888", " 이 순 신 ", ""],
        ["", "", "", ""]]
    pd.DataFrame(rows).to_excel(path, header=False, index=False)
    return path


@pytest.mark.parametrize("header_rows", [1, 3])
def test_import_client_list_updates_and_adds(db, tmp_path, header_rows):
    entries = feedback.load_client_list(write_list(tmp_path / "clients.xlsx", header_rows))
    assert entries == [("대진엔지니어링", "111-11-11111", "김영주"), ("새로운상사", "888-88-88888", "이 순 신")]
    assert feedback.import_client_list(db, entries) == (1, 1, 0)
    rows = db.execute("SELECT name, reg_no, ceo_name, source FROM clients ORDER BY reg_no").fetchall()
    assert rows == [("대진엔지니어링", "111-11-11111", "김영주", "import"), ("새로운상사", "888-88-88888", "이 순 신", "list")]
    m = load_client_master(db, today=TODAY)
    assert m.resolve_full("새로운상사(이순신)").note == ""


def test_import_client_list_requires_three_columns(tmp_path):
    path = tmp_path / "bad.xlsx"
    pd.DataFrame([["거래처명", "사업자번호"], ["가", "1111111111"]]).to_excel(path, header=False, index=False)
    with pytest.raises(ValueError, match="대표자명"):
        feedback.load_client_list(path)


def test_statement_import_keeps_ceo_and_list_rows(db):
    feedback.import_client_list(db, [("대진엔지니어링", "111-11-11111", "김영주"), ("새로운상사", "888-88-88888", "이순신"),
                                     ("목록만", "999-99-99999", "홍")])
    feedback.replace_clients(db, [("대진엔지니어링", "111-11-11111", 6, "2026-01-01", "2026-11-20"),
                                  ("새로운상사", "888-88-88888", 1, "2026-11-20", "2026-11-20")])
    rows = db.execute("SELECT name, reg_no, n_statements, ceo_name, source FROM clients ORDER BY reg_no").fetchall()
    assert rows == [("대진엔지니어링", "111-11-11111", 6, "김영주", "import"),
                    ("새로운상사", "888-88-88888", 1, "이순신", "import"),
                    ("목록만", "999-99-99999", 0, "홍", "list")]
