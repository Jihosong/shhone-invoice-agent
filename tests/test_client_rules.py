"""동명 규칙(homonyms: 법인/개인 표시) + 동명확인 + manual_clients + clients add (수동 등록)."""
import sqlite3
from datetime import date
from decimal import Decimal

import pytest

import clients_cli
import feedback
from pricer import ST_QUOTED, PriceEntry, Pricer
from tests.test_watcher_settle import C, SCFG, age_all, make, poll, r, statuses
from watcher import (ST_CONFIRMED, ST_HCHECK, ST_HOMONYM, ST_TYPING, ClientMaster, entity_mark, load_client_master,
                     strip_block_tag)

TODAY = date(2026, 11, 25)
DAEYANG = [("(주)대양상사", "134-81-32293", 15, "2026-11-01"), ("대양상사", "127-14-79906", 2, "2026-10-01")]
GYEONGDO = [("(주)경도정밀", "113-81-51309", 1, "2026-01-01"), ("경도정밀", "140-08-09552", 9, "2026-11-01")]
HOMONYMS = {"대양상사": {"법인": "134-81-32293", "개인": "127-14-79906", "기본": "법인"},
            "경도정밀": {"법인": "113-81-51309", "개인": "140-08-09552", "기본": "개인"}}
NOTICE = ("[동명확인: 기본 (주)대양상사 134-81-32293 / 다른 후보 대양상사 127-14-79906"
          " → 거래처에 주) 또는 (개인) 붙여주세요]")
RULES_YAML = '''
aliases:
  "큐머시스": "108-12-95838"
homonyms:
  대양상사:
    법인: "134-81-32293"
    개인: "127-14-79906"
    기본: 법인
manual_clients:
  "도선": "사업자 없음·현금거래"
'''


def master(entries=DAEYANG + GYEONGDO, **kw):
    return ClientMaster(entries, kw.pop("aliases", None), TODAY, kw.pop("homonyms", HOMONYMS), kw.pop("manual", None))


# ---------------------------------------------------------------- 표시 판정 (정규화 전 원문)
@pytest.mark.parametrize("text,mark", [
    ("주)대양상사", "법인"), ("(주)대양상사", "법인"), ("㈜대양상사", "법인"), ("주식회사 대양상사", "법인"),
    ("대양상사(주)", "법인"), ("대양상사(개인)", "개인"), ("대양상사 ( 개인 )", "개인"), ("대양상사", None),
    ("주)대양상사(개인)", "둘다"),
])
def test_entity_mark(text, mark):
    assert entity_mark(text) == mark


@pytest.mark.parametrize("text,reg,note", [
    ("주)대양상사", "134-81-32293", ""),
    ("㈜대양상사 급함", "134-81-32293", "급함"),
    ("주식회사 대양상사", "134-81-32293", ""),
    ("대양상사(개인)", "127-14-79906", ""),
    ("대양상사(개인) 택배", "127-14-79906", "택배"),
])
def test_homonym_marked_confirmed(text, reg, note):
    res = master().resolve_full(text)
    assert (res.reg_no, res.hold, res.note, res.notice) == (reg, None, note, "")


def test_homonym_unmarked_is_check_with_default():
    res = master().resolve_full("대양상사")
    assert (res.name, res.reg_no, res.hold) == ("(주)대양상사", "134-81-32293", ST_HCHECK)
    assert f"[{res.notice}]" == NOTICE


def test_homonym_default_individual():
    res = master().resolve_full("경도정밀")
    assert (res.name, res.reg_no, res.hold) == ("경도정밀", "140-08-09552", ST_HCHECK)


def test_both_marks_is_check():
    assert master().resolve_full("주)대양상사(개인)").hold == ST_HCHECK


def test_homonyms_beat_recent_rule_and_hold():
    """homonyms에 있으면 최근거래 자동확정·보류-동명 규칙을 안 씀."""
    only_recent = [("(주)대양상사", "134-81-32293", 15, "2026-11-01"), ("대양상사", "127-14-79906", 2, "2024-01-01")]
    assert master(only_recent).resolve_full("대양상사").hold == ST_HCHECK
    assert master(only_recent, homonyms={}).resolve_full("대양상사").reg_no == "134-81-32293"  # 최근거래 규칙
    same = [("세종정밀", "111-11-11111", 1, "2026-11-01"), ("세종정밀", "222-22-22222", 1, "2026-11-02")]
    assert master(same, homonyms={}).resolve_full("세종정밀").hold == ST_HOMONYM
    hom = {"세종정밀": {"법인": "", "개인": "222-22-22222", "기본": "개인"}}
    assert master(same, homonyms=hom).resolve_full("세종정밀").hold == ST_HCHECK


def test_alias_to_homonym_name_uses_homonym_rule():
    m = master(aliases={"대양": "대양상사"})
    assert m.resolve_full("대양").hold == ST_HCHECK
    assert m.resolve_full("대양(개인)").reg_no == "127-14-79906"


def test_bad_homonym_config_ignored():
    m = master(homonyms={"대양상사": {"법인": "134-81-32293", "개인": "999-99-99999", "기본": "법인"},
                         "경도정밀": {"법인": "113-81-51309", "기본": "개인"}})
    assert len(m.alias_errors) == 2 and not m.homonym


# ---------------------------------------------------------------- watcher + pricer
def setup(tmp_path, client, entries=DAEYANG, extra_rows=1):
    w = make([r("11/25", client)] + [r("", "", "45x55") for _ in range(extra_rows)])
    w.con.execute("DELETE FROM clients")
    w.con.executemany("INSERT INTO clients(name, reg_no, n_statements, last_date) VALUES(?,?,?,?)", entries)
    p = tmp_path / "client_aliases.yaml"
    p.write_text(RULES_YAML, encoding="utf-8")
    w.alias_path = p
    return w


def price(w):
    p = Pricer({"sheets": {"t": SCFG}}, w.con)
    p.table_for = lambda _scfg: {"45x55": PriceEntry("45x55", Decimal(29700), Decimal(1))}
    p.run_once(w.snapshots)
    w.ws.values = [[str(v) for v in row] for row in w.ws.values]  # 실제 시트처럼 문자열로 읽힘


def settle(w):
    poll(w)
    price(w)
    age_all(w)
    snap = poll(w)
    price(w)
    return snap


@pytest.mark.parametrize("client,reg", [("주)대양상사", "134-81-32293"), ("대양상사(개인)", "127-14-79906")])
def test_watcher_marked_confirmed_and_quoted(tmp_path, client, reg):
    w = setup(tmp_path, client)
    snap = settle(w)
    assert snap[2]["client_reg_no"] == reg
    assert statuses(w, (2, 3)) == [ST_QUOTED, ST_QUOTED]
    assert "동명확인" not in w.ws.values[1][C["basis"]]


def test_watcher_unmarked_check_quotes_default_but_never_issues(tmp_path):
    w = setup(tmp_path, "대양상사")
    poll(w)
    assert statuses(w, (2, 3)) == [ST_TYPING, ST_TYPING]  # 입력 안정화 규칙은 그대로
    age_all(w)
    snap = poll(w)
    assert statuses(w, (2, 3)) == [ST_HCHECK, ST_HCHECK]  # 묶음 전체
    assert snap[2]["client_reg_no"] == "134-81-32293"
    price(w)
    # 기본 쪽으로 단가는 적음, 상태는 동명확인 그대로 → 견적완료/발행 단계로 안 감
    assert w.ws.values[1][C["unit_price"]] == "29700"
    assert w.ws.values[1][C["basis"]] == f"[(주)대양상사 11/25 1/2] {NOTICE} 단가표 45x55 29,700/m × 1000mm"
    assert w.ws.values[2][C["unit_price"]] == "29700"
    assert statuses(w, (2, 3)) == [ST_HCHECK, ST_HCHECK]
    for _ in range(3):  # 시간이 지나도 그대로
        age_all(w, 60)
        poll(w)
        price(w)
    assert statuses(w, (2, 3)) == [ST_HCHECK, ST_HCHECK]
    assert w.con.execute("SELECT DISTINCT status FROM rows").fetchall() == [(ST_HCHECK,)]


@pytest.mark.parametrize("fixed,reg", [("주)대양상사", "134-81-32293"), ("대양상사(개인)", "127-14-79906")])
def test_watcher_name_fix_confirms_next_poll(tmp_path, fixed, reg):
    w = setup(tmp_path, "대양상사")
    settle(w)
    assert statuses(w, (2, 3)) == [ST_HCHECK, ST_HCHECK]
    w.ws.values[1][C["client"]] = fixed  # 사람이 거래처 칸 수정
    snap = poll(w)
    assert statuses(w, (2, 3)) == [ST_CONFIRMED, ST_CONFIRMED]  # 안정화 대기 없이 바로 확정
    assert snap[2]["client_reg_no"] == reg
    assert "동명확인" not in w.ws.values[1][C["basis"]]
    price(w)
    assert statuses(w, (2, 3)) == [ST_QUOTED, ST_QUOTED]  # 이제 발행 단계로 진행 가능
    assert strip_block_tag(w.ws.values[1][C["basis"]]) == "단가표 45x55 29,700/m × 1000mm"


def test_watcher_check_block_item_edit_goes_typing(tmp_path):
    """동명확인 묶음에서 품목 칸이 바뀌면 일반 규칙(입력중)."""
    w = setup(tmp_path, "대양상사")
    settle(w)
    w.ws.values[2][C["length"]] = "2000"
    poll(w)
    assert statuses(w, (2, 3)) == [ST_TYPING, ST_TYPING]


def test_watcher_qmesys_resolved(tmp_path):
    con_entries = DAEYANG + [("QMESYS(큐머시스)", "108-12-95838", 0, None)]
    w = setup(tmp_path, "큐머시스", con_entries)
    snap = settle(w)
    assert (snap[2]["client_resolved"], snap[2]["client_reg_no"]) == ("QMESYS(큐머시스)", "108-12-95838")
    assert statuses(w, (2, 3)) == [ST_QUOTED, ST_QUOTED]


def test_watcher_manual_client_human_with_reason(tmp_path):
    w = setup(tmp_path, "도선 오후")
    w.ws.values[2][C["basis"]] = "현금 3만원"  # 사람이 적은 메모는 보존
    snap = settle(w)
    assert statuses(w, (2, 3)) == ["", ""]
    assert snap[2]["client_resolved"] is None
    assert w.ws.values[1][C["basis"]] == "[수동처리: 사업자 없음·현금거래]"
    assert w.ws.values[2][C["basis"]] == "[수동처리: 사업자 없음·현금거래] 현금 3만원"
    assert w.ws.values[1][C["unit_price"]] == w.ws.values[2][C["unit_price"]] == ""  # 견적 안 함


# ---------------------------------------------------------------- clients add / import
@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    feedback.ensure_columns(con)
    con.executemany("INSERT INTO clients(name, reg_no, n_statements, first_date, last_date, source)"
                    " VALUES(?,?,?,?,?,'import')", [(n, reg, cnt, None, last) for n, reg, cnt, last in DAEYANG])
    return con


def test_clients_add_and_resolve(db):
    msg, ok = feedback.add_manual_client(db, "QMESYS(큐머시스)", "108-12-95838")
    assert ok and "수동 등록" in msg
    m = load_client_master(db, aliases={"큐머시스": "108-12-95838"})
    res = m.resolve_full("큐머시스")
    assert (res.name, res.reg_no, res.hold) == ("QMESYS(큐머시스)", "108-12-95838", None)
    assert feedback.add_manual_client(db, "딴이름", "134-81-32293")[1] is False  # import에 있는 번호는 거절


def test_import_keeps_manual_unless_same_reg(db):
    feedback.add_manual_client(db, "QMESYS(큐머시스)", "108-12-95838")
    feedback.add_manual_client(db, "새거래처", "222-22-22222")
    imported = [("(주)대양상사", "134-81-32293", 16, "2023-01-01", "2026-11-30"),
                ("큐머시스(QMESYS)", "108-12-95838", 1, "2026-11-28", "2026-11-28")]
    assert feedback.replace_clients(db, imported) == (1, 1)
    rows = db.execute("SELECT name, reg_no, n_statements, source FROM clients ORDER BY reg_no").fetchall()
    assert rows == [("큐머시스(QMESYS)", "108-12-95838", 1, "import"), ("(주)대양상사", "134-81-32293", 16, "import"),
                    ("새거래처", "222-22-22222", 0, "manual")]


# ---------------------------------------------------------------- aliases check (homonyms / manual)
def test_aliases_check_homonyms_and_manual(db, tmp_path):
    feedback.add_manual_client(db, "QMESYS(큐머시스)", "108-12-95838")
    p = tmp_path / "a.yaml"
    p.write_text(
        "aliases:\n"
        '  "큐머시스": "108-12-95838"\n'      # 2 ok
        '  "대양상사": "(주)대양상사"\n'      # 3 homonyms와 같은 표기 → 오류(아래 13행)
        "homonyms:\n"
        "  대양상사:\n"                       # 5 ok
        '    법인: "134-81-32293"\n'
        '    개인: "127-14-79906"\n'
        "    기본: 법인\n"
        "  경도정밀:\n"
        '    법인: "113-81-5130"\n'           # 10 형식 오류
        '    개인: "140-08-09552"\n'          # 11 clients에 없음
        "    기본: 둘다\n"                    # 12 기본 오류
        "  대양 상사:\n"                      # 13 중복(정규화 같음) → 다른 곳
        '    개인: "127-14-79906"\n'
        "    기본: 법인\n"                    # 15 기본 법인인데 법인 번호 없음
        "manual_clients:\n"
        '  "도선": "사업자 없음·현금거래"\n'  # 17 ok
        '  "현금손님": ""\n',                 # 18 사유 없음 → 경고
        encoding="utf-8")
    problems = clients_cli.check_aliases(p, db)
    assert {line for line, level, _ in problems if level == "오류"} == {5, 10, 11, 12, 13, 15}
    assert {line for line, level, _ in problems if level == "경고"} == {18}
