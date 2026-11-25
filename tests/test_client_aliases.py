"""거래처 별칭표 + 동명 처리 (최근 12개월 거래 기준) + aliases check / clients find."""
from datetime import date, timedelta

import pytest

import clients_cli
from tests.test_watcher_settle import C, age_all, make, poll, r, statuses
from watcher import ST_CONFIRMED, ST_HOMONYM, ST_TYPING, ST_UNREG, ClientMaster

TODAY = date(2026, 11, 25)
RECENT, OLD = "2026-11-23", "2025-06-11"  # OLD = 12개월 넘게 거래 없음
STM = [("에스티엠", "134-29-95780", 16, "2026-09-03"), ("에스티엠(STM)", "140-03-58500", 12, "2026-11-25")]
NAKA = [("코리아 나까조(주)", "137-81-33044", 135, RECENT)]
SEJONG_ONE = [("세종정밀", "124-01-94801", 7, OLD), ("세종정밀", "140-04-14907", 93, RECENT)]
SEJONG_TWO = [("세종정밀", "124-01-94801", 7, "2026-10-01"), ("세종정밀", "140-04-14907", 93, RECENT)]


def master(entries, aliases=None):
    return ClientMaster(entries, aliases, TODAY)


def got(m, text):
    res = m.resolve_full(text)
    return res.name, res.reg_no, res.hold, res.note


# ---------------------------------------------------------------- 별칭
STM_ALIASES = {"STM": "에스티엠(STM)", "에스티엠": "에스티엠"}


@pytest.mark.parametrize("text,want", [
    ("STM", ("에스티엠(STM)", "140-03-58500", None, "")),
    ("stm", ("에스티엠(STM)", "140-03-58500", None, "")),
    ("STM 급함", ("에스티엠(STM)", "140-03-58500", None, "급함")),
    ("에스티엠", ("에스티엠", "134-29-95780", None, "")),
    ("에스티엠(STM)", ("에스티엠(STM)", "140-03-58500", None, "")),
    ("에스티엠 2차", ("에스티엠", "134-29-95780", None, "2차")),
])
def test_stm_and_estiem_never_mix(text, want):
    assert got(master(STM, STM_ALIASES), text) == want


def test_stm_without_alias_is_unregistered():
    assert got(master(STM), "STM") == (None, None, ST_UNREG, "STM")


def test_nakajo_alias_nfkc_name():
    m = master(NAKA, {"나까조": "코리아 나까조㈜"})  # ㈜ = (주)
    assert got(m, "나까조") == ("코리아 나까조(주)", "137-81-33044", None, "")
    assert got(m, "나까조 택배") == ("코리아 나까조(주)", "137-81-33044", None, "택배")


def test_alias_by_reg_no():
    m = master(SEJONG_TWO, {"세종": "124-01-94801"})
    assert got(m, "세종")[:3] == ("세종정밀", "124-01-94801", None)


def test_invalid_alias_ignored():
    m = master(STM, {"STM": "없는회사", "X": "999-99-99999"})
    assert len(m.alias_errors) == 2
    assert got(m, "STM")[2] == ST_UNREG


def test_longer_master_match_beats_shorter_alias():
    m = master(STM, {"에스티": "에스티엠"})
    assert got(m, "에스티엠(STM)")[1] == "140-03-58500"


# ---------------------------------------------------------------- 동명
def test_homonym_one_active_confirmed():
    res = master(SEJONG_ONE).resolve_full("세종정밀")
    assert (res.name, res.reg_no, res.hold) == ("세종정밀", "140-04-14907", None)
    assert res.basis == "동명→최근거래 140-04-14907"


def test_homonym_two_active_held():
    assert got(master(SEJONG_TWO), "세종정밀")[:3] == ("세종정밀", None, ST_HOMONYM)


def test_homonym_none_active_held():
    old = [(n, reg, w, "2024-01-01") for n, reg, w, _ in SEJONG_TWO]
    assert got(master(old), "세종정밀")[2] == ST_HOMONYM


def test_alias_reg_no_beats_recent_rule():
    """별칭에 사업자번호 → 최근거래 규칙보다 우선 (오래된 번호라도)."""
    res = master(SEJONG_ONE, {"세종정밀": "124-01-94801"}).resolve_full("세종정밀")
    assert (res.reg_no, res.hold, res.basis) == ("124-01-94801", None, "")


def test_alias_to_homonym_name_still_uses_recent_rule():
    m = master(SEJONG_ONE, {"세종": "세종정밀"})
    assert got(m, "세종")[1:3] == ("140-04-14907", None)


def test_same_key_different_names_ambiguous_unregistered():
    """'(주)대양상사'/'대양상사' 같은 키, 둘 다 활성(또는 둘 다 비활성) → 추측 없이 보류-미등록 + 후보."""
    ents = [("(주)대양상사", "134-81-32293", 15, RECENT), ("대양상사", "127-14-79906", 2, RECENT)]
    res = master(ents).resolve_full("대양상사")
    assert (res.name, res.hold) == (None, ST_UNREG)
    assert {c.reg_no for c in res.candidates} == {"134-81-32293", "127-14-79906"}


# ---------------------------------------------------------------- watcher 연동
def days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def watcher_with(entries, aliases_yaml=None, tmp_path=None, client="세종정밀"):
    w = make([r("11/25", client), r("", "", "50x60")])
    w.con.execute("DELETE FROM clients")
    w.con.executemany("INSERT INTO clients(name, reg_no, n_statements, last_date) VALUES(?,?,?,?)", entries)
    if aliases_yaml is not None:
        p = tmp_path / "client_aliases.yaml"
        p.write_text(aliases_yaml, encoding="utf-8")
        w.alias_path = p
    return w


def test_watcher_homonym_recent_confirmed_with_basis_tag():
    w = watcher_with([("세종정밀", "124-01-94801", 7, days_ago(400)), ("세종정밀", "140-04-14907", 93, days_ago(2))])
    snap = poll(w)
    assert statuses(w, (2, 3)) == [ST_TYPING, ST_TYPING]
    assert snap[2]["client_reg_no"] == "140-04-14907"
    assert w.ws.values[1][C["basis"]] == "[세종정밀 동명→최근거래 140-04-14907 11/25 1/2]"
    age_all(w)
    poll(w)
    assert statuses(w, (2, 3)) == [ST_CONFIRMED, ST_CONFIRMED]
    assert w.con.execute("SELECT DISTINCT client_reg_no FROM rows").fetchall() == [("140-04-14907",)]


def test_watcher_homonym_two_active_held():
    w = watcher_with([("세종정밀", "124-01-94801", 7, days_ago(20)), ("세종정밀", "140-04-14907", 93, days_ago(2))])
    poll(w)
    assert statuses(w, (2, 3)) == [ST_HOMONYM, ST_HOMONYM]


def test_watcher_alias_file(tmp_path):
    w = watcher_with(STM, 'aliases:\n  "STM": "에스티엠(STM)"\n', tmp_path, client="STM")
    snap = poll(w)
    assert (snap[2]["client_resolved"], snap[2]["client_reg_no"]) == ("에스티엠(STM)", "140-03-58500")
    assert statuses(w, (2, 3)) == [ST_TYPING, ST_TYPING]


# ---------------------------------------------------------------- aliases check / clients find
@pytest.fixture
def con():
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE clients(name TEXT, reg_no TEXT, n_statements INTEGER, first_date TEXT, last_date TEXT)")
    c.execute("CREATE TABLE statements(date TEXT, client TEXT, reg_no TEXT)")
    for name, reg, n, last in STM + NAKA + SEJONG_ONE:
        c.execute("INSERT INTO clients VALUES(?,?,?,?,?)", (name, reg, n, None, last))
    c.executemany("INSERT INTO statements VALUES(?,?,?)",
                  [("2026-03-01", "에스티엠(STM)", "140-03-58500")] * 3 + [("2025-03-01", "에스티엠", "134-29-95780")])
    return c


def test_aliases_check(con, tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text(
        "aliases:\n"
        '  "STM": "에스티엠(STM)"\n'          # 2 ok
        '  "에스티엠": "에스티엠"\n'          # 3 ok
        '  "나까조": "코리아 나까조㈜"\n'      # 4 ok (NFKC)
        '  "나카조": "코리아 나까조(주)"\n'    # 5 ok — 한 등록명에 약칭 여럿
        '  "세종": "123-45-6789"\n'           # 6 형식 오류
        '  "세종2": "999-99-99999"\n'         # 7 마스터에 없음
        '  "없는": "없는회사"\n'              # 8 마스터에 없음
        '  "stm": "에스티엠"\n'               # 9 약칭 중복(정규화 같음) → 다른 곳
        '  "STM": "에스티엠(STM)"\n',         # 10 같은 약칭 두 번 (같은 곳) → 중복 경고
        encoding="utf-8")
    problems = clients_cli.check_aliases(p, con)
    errors = {line for line, level, _ in problems if level == "오류"}
    assert errors == {6, 7, 8, 9}
    assert {line for line, level, _ in problems if level == "경고"} == {10}


def test_aliases_check_homonym_name_warns(con, tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text('aliases:\n  "세종": "세종정밀"\n', encoding="utf-8")
    assert [(line, level) for line, level, _ in clients_cli.check_aliases(p, con)] == [(2, "안내")]


def test_clients_find(con):
    rows = clients_cli.find_clients(con, "stm", year="2026")
    assert [(r["name"], r["reg_no"], r["n_year"]) for r in rows] == [("에스티엠(STM)", "140-03-58500", 3)]
    rows = clients_cli.find_clients(con, "에스티엠", year="2026")
    assert [r["name"] for r in rows] == ["에스티엠(STM)", "에스티엠"]  # 2026 건수 많은 순
