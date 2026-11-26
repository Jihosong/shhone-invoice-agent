"""감시 탭 선택: '당월' > 오늘 달 'N월' > 직전 달 'N월'(+WARNING), 중복 → ERROR, 월초 미갱신 경고, 월 전환."""
from datetime import date

import pytest
from loguru import logger

import backtest
import notifier
from issuer.excel_upload import block_date
from tests.test_watcher_legacy import SCFG, FakeGC, FakeWS, make_watcher, poll, row
from watcher import pick_active_tab

PAT = r"^\d{1,2}월$"


class Tab:
    def __init__(self, title, index, gid=None):
        self.title, self.index, self.id = title, index, gid if gid is not None else index


def pick(titles, today, ignore=()):
    return pick_active_tab([Tab(t, i) for i, t in enumerate(titles)], PAT, list(ignore), today)


NOV = date(2026, 11, 26)


# ---------------------------------------------------------------- 선택 규칙
@pytest.mark.parametrize("titles", [["11월", "당월", "10월"], ["당월", "11월"], ["10월", "9월", "당월"],
                                    [" 당월 ", "12월"]])
def test_current_tab_wins_anywhere(titles):
    p = pick(titles, NOV)
    assert p.ws.title.strip() == "당월" and not p.error and not p.warning


@pytest.mark.parametrize("titles", [["10월", "11월", "9월"], ["9월", "10월", "11월"], ["11월", "12월"]])
def test_today_month_tab_regardless_of_order(titles):
    p = pick(titles, NOV)
    assert p.ws.title == "11월" and not p.warning


def test_fallback_to_previous_month_with_warning():
    p = pick(["9월", "10월", "재고"], NOV)
    assert p.ws.title == "10월" and "직전 달" in p.warning and not p.error


def test_january_falls_back_to_december():
    p = pick(["11월", "12월"], date(2027, 1, 3))
    assert p.ws.title == "12월" and p.warning


def test_december_and_january_tabs():
    assert pick(["12월", "1월"], date(2027, 1, 3)).ws.title == "1월"
    assert pick(["1월", "12월"], date(2026, 12, 31)).ws.title == "12월"


def test_no_tab():
    p = pick(["9월", "재고"], NOV)
    assert p.ws is None and not p.error


def test_ignore_tabs():
    assert pick(["당월", "11월"], NOV, ignore=["당월"]).ws.title == "11월"


@pytest.mark.parametrize("titles, what", [(["당월", " 당월"], "'당월' 탭이 2개"),
                                          (["11월", " 11월", "10월"], "당월(11월) 탭이 2개"),
                                          (["10월", "10월 "], "직전 달(10월) 탭이 2개")])
def test_duplicate_tabs_error(titles, what):
    p = pick(titles, NOV)
    assert p.ws is None and what in p.error


# ---------------------------------------------------------------- watcher 연결
class Book(FakeGC):
    def __init__(self, tabs):
        self.tabs = tabs
        self.ws = tabs[0]

    def worksheets(self):
        return self.tabs


def ws(title, rows, index=0, gid=None):
    w = FakeWS(rows)
    w.title, w.index, w.id = title, index, gid if gid is not None else index
    return w


@pytest.fixture
def logs():
    msgs = []
    hid = logger.add(lambda m: msgs.append((m.record["level"].name, m.record["message"])), level="INFO")
    notifier.sent.clear()
    yield msgs
    logger.remove(hid)


def watcher_with(tabs, today):
    w = make_watcher([])
    w.gc = Book(tabs)
    w.today_fn = lambda: today
    return w


def test_watcher_uses_current_tab(logs):
    cur = ws("당월", [row("가")], index=3)
    w = watcher_with([ws("11월", [row("나")], 0), ws("10월", [], 1), cur], NOV)
    assert set(poll(w)) == {"가"}
    assert w.snapshots["t"]["ws"] is cur


def test_watcher_duplicate_stops_sheet(logs):
    w = watcher_with([ws("당월", [row("가")], 0), ws("당월 ", [row("나")], 1)], NOV)
    w.process_sheet("t", SCFG)
    assert "t" not in w.snapshots
    errs = [m for lv, m in logs if lv == "ERROR"]
    assert len(errs) == 1 and "'당월' 탭이 2개" in errs[0]
    assert len(notifier.sent) == 1
    w.process_sheet("t", SCFG)
    assert len([m for lv, m in logs if lv == "ERROR"]) == 1  # 같은 오류는 판정이 바뀔 때만


def test_watcher_fallback_warns(logs):
    w = watcher_with([ws("10월", [["10/30", "가", "FALSE", "FALSE", "", "", ""]], 0)], NOV)
    poll(w)
    assert any(lv == "WARNING" and "직전 달 탭 '10월'" in m for lv, m in logs)


# ---------------------------------------------------------------- 월초 점검
def dec_row(d, client):
    return [d, client, "FALSE", "FALSE", "", "", ""]


def test_month_start_not_updated_warns_every_poll(logs):
    w = watcher_with([ws("당월", [dec_row("11/29", "가"), dec_row("11/30", "나")])], date(2026, 12, 1))
    poll(w)
    poll(w)
    warns = [m for lv, m in logs if lv == "WARNING" and "당월 탭 갱신 필요" in m]
    assert len(warns) == 2 and "탭 '당월'에 12월 행 없음" in warns[0]  # 매 폴링
    assert notifier.sent == ["[테스트] 당월 탭 갱신 필요(탭 '당월'에 12월 행 없음)"]  # 알림은 한 번


def test_month_start_ok_once_new_month_row_appears(logs):
    tab = ws("당월", [dec_row("11/30", "가")])
    w = watcher_with([tab], date(2026, 12, 1))
    poll(w)
    tab.values.append(dec_row("12/1", "나"))
    logs.clear()
    poll(w)
    assert not [m for lv, m in logs if "당월 탭 갱신 필요" in m]


def test_month_start_no_warning_mid_month_or_empty(logs):
    poll(watcher_with([ws("당월", [dec_row("11/20", "가")])], NOV))
    poll(watcher_with([ws("당월", [])], date(2026, 12, 1)))
    assert not [m for lv, m in logs if "당월 탭 갱신 필요" in m]


def test_january_month_start_sees_december_rows(logs):
    poll(watcher_with([ws("당월", [dec_row("12/30", "가")])], date(2027, 1, 2)))
    assert any("탭 '당월'에 1월 행 없음" in m for lv, m in logs if lv == "WARNING")


# ---------------------------------------------------------------- 월 전환: '당월' → '11월'로 이름 바꾸고 새 '당월'
def test_month_switch_rename_keeps_old_rows(logs):
    old = ws("당월", [dec_row("11/30", "가")], 0, gid=100)
    w = watcher_with([old], NOV)
    poll(w)
    rid = w.con.execute("SELECT row_id FROM rows").fetchone()[0]
    old.title = "11월"
    new = ws("당월", [dec_row("12/1", "나")], 0, gid=200)
    old.index = 1
    w.gc.tabs = [new, old]
    w.today_fn = lambda: date(2026, 12, 1)
    assert set(poll(w)) == {"나"}
    assert w.con.execute("SELECT status FROM rows WHERE row_id=?", (rid,)).fetchone()[0] != "삭제됨"
    assert not [m for lv, m in logs if "당월 탭 갱신 필요" in m]


def test_month_switch_by_month_tabs():
    w = watcher_with([ws("11월", [dec_row("11/30", "가")], 0), ws("12월", [dec_row("12/1", "나")], 1)], NOV)
    assert set(poll(w)) == {"가"}
    w.today_fn = lambda: date(2026, 12, 1)
    assert set(poll(w)) == {"나"}


# ---------------------------------------------------------------- 날짜 연도는 오늘 기준
def test_block_date_year_from_today_not_tab():
    assert block_date("1/2", "12월", date(2026, 12, 31)) == date(2027, 1, 2)
    assert block_date("1/2", "당월", date(2026, 12, 31)) == date(2027, 1, 2)
    assert block_date("12/30", "당월", date(2027, 1, 5)) == date(2026, 12, 30)
    assert block_date("12/30", "12월", date(2027, 1, 5)) == date(2026, 12, 30)
    assert block_date("11/26", "당월", NOV) == date(2026, 11, 26)
    assert block_date("10/30", "12월", NOV) == date(2026, 10, 30)  # 탭 이름(12월)에 끌려 작년이 되지 않음


# ---------------------------------------------------------------- backtest: '당월'도 월 탭
def test_backtest_tab_month_includes_current():
    assert backtest.tab_month("당월", NOV) == 11 and backtest.tab_month("3월") == 3
    assert backtest.tab_month("재고") is None
    assert backtest.tab_years(["당월", "3월", "12월"], 2026, NOV) == {"당월": 2026, "3월": 2026, "12월": 2025}
