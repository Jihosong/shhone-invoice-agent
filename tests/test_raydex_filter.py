"""레이덱스 유사품 필터 (raydex_filter) + 확정 대기 초 단위.

- 거래처가 누구든 (주)레이덱스 과거 품목과 SUS/미기재·구경 ±0.1·공차 상·하한 각 ±0.02·기장 ±10mm가 맞으면
  산출근거 맨 앞 '※레이덱스 유사품 (…) ' + 최대 mid(견적확인필요) + AI 프롬프트 참고 문구.
- 거래처가 (주)레이덱스 본인 → 묶음 사람처리 + '[수동처리: 레이덱스 — 거래 단절]' + WARNING.
"""
from datetime import date, datetime, timedelta

import pytest
from loguru import logger

import feedback
import pricer
import watcher
from cases import DbLine
from pricer_job import JobInput, Quote, raydex_hint, raydex_ok
from tests.test_status_rename_cycle import RecWS
from tests.test_watcher_legacy import make_watcher
from watcher import ST_CHECK, ST_TYPING, load_config, settle_seconds

REG, RX_REG = "111-11-11111", "222-22-22222"
COLS = {"date": 1, "client": 2, "main_msg": 4, "spec_qty": 5, "difficulty": 6, "stock_removal": 8, "work_start": 9,
        "work_end": 10, "author": 15, "pre_note": 19, "status": 22, "unit_price": 23, "basis": 24, "row_id": 26}
SCFG = {"spreadsheet_id": "x", "label": "임가공", "kind": "job", "data_start_row": 3, "columns": COLS,
        "hash_columns": ["client", "main_msg", "spec_qty", "difficulty", "stock_removal"],
        "item_columns": ["spec_qty", "main_msg"], "issue_trigger": "work_start"}
RX = {"client": "(주)레이덱스", "enabled": True}
S, P, B, E = COLS["status"] - 1, COLS["unit_price"] - 1, COLS["basis"] - 1, COLS["spec_qty"] - 1
SPEC = "SUS 사각 Ø28 +0.05~+0.06 / 300 / 2개"
MARK = "※레이덱스 유사품 (2026-11-19 Ø28 +0.05~+0.06 L300 150,000원) "


class HighAi:
    """AI는 high 40000 — 레이덱스 유사품이면 상한 mid가 걸려야 함. 받은 hint·max_conf 기록."""

    def __init__(self):
        self.calls = []

    def quote(self, inp, index, cutoff, hint="", tag="", max_conf=None, directive=""):
        self.calls.append((hint, max_conf))
        conf = "mid" if max_conf == "mid" else "high"
        return Quote("AI-유사품", 40000, conf, "자동")


class Rig:
    def __init__(self, spec=SPEC, client="가사", raydex=RX):
        row = [""] * 28
        row[0], row[1], row[E], row[8], row[9] = "11/26", client, spec, "FALSE", "FALSE"
        clients = [("가사", REG, 5), ("(주)레이덱스", RX_REG, 5)]
        self.w = make_watcher([[""] * 28, row], clients=clients, scfg=SCFG, header=[""] * 28)
        self.w.ws = RecWS(self.w.ws.values[1:], [""] * 28)
        self.w.gc.ws = self.w.ws
        self.w.cfg = {"sheets": {"imgagong": SCFG}, "raydex_filter": raydex}
        self.w.common["status_values"] = load_config()["common"]["status_values"]
        self.w.common.pop("quote_settle_min")
        self.w.common["quote_settle_sec"] = 15
        self.w.post_quote = watcher.post_quote_states(self.w.common["status_values"])
        self.w.defer_flush = True
        feedback.ensure_columns(self.w.con)
        self.w.con.execute(
            "INSERT INTO lines(date, client, item, spec, qty, unit_price, supply, kind, dia, tol_lo, tol_hi, length,"
            " source, reg_no) VALUES('2026-11-19', '(주)레이덱스', 'SUS 사각 시린더호닝', 'Ø28 +0.05~+0.06 / 300',"
            " 1, 150000, 150000, 'job', 28.0, 0.05, 0.06, 300, 'export', ?)", (RX_REG,))
        self.p = pricer.Pricer({"sheets": {"imgagong": SCFG}, "raydex_filter": raydex}, self.w.con)
        self.ai = self.p._ai = HighAi()

    def row(self):
        return self.w.ws.values[2]

    def cycle(self):
        self.w.process_sheet("imgagong", SCFG)
        self.p.run_once(self.w.snapshots)
        self.w.flush_writes()
        self.w.con.commit()

    def settle(self, seconds=20):
        past = (datetime.now() - timedelta(seconds=seconds)).isoformat(timespec="seconds")
        self.w.con.execute("UPDATE rows SET last_change=?", (past,))

    def quoted(self):
        self.cycle()
        self.settle()
        self.cycle()
        return self


# ---------------------------------------------------------------- 시간 설정
def test_config_15s_settle_1min_grace():
    cfg = load_config()
    assert settle_seconds(cfg["common"]) == 15 and settle_seconds(cfg["common"], quote=True) == 15
    assert cfg["issue"]["bundle_grace_min"] == 1
    assert cfg["raydex_filter"] == {"client": "(주)레이덱스", "enabled": True}


def test_settle_seconds_old_minute_keys_still_read():
    assert settle_seconds({"quote_settle_min": 5}) == 300
    assert settle_seconds({"quote_tab_settle_min": 1}, quote=True) == 60
    assert settle_seconds({}) == 15


def test_confirms_after_15_seconds():
    rig = Rig(raydex={"enabled": False})
    rig.cycle()
    assert rig.row()[S] == ST_TYPING
    rig.settle(10)
    rig.cycle()
    assert rig.row()[S] == ST_TYPING  # 10초 — 아직
    rig.settle(16)
    rig.cycle()
    assert rig.row()[S] == watcher.ST_QUOTED  # 16초 → 확정 → 견적 (필터 꺼짐, AI high)


# ---------------------------------------------------------------- 유사품
def test_other_client_sus_square_marks_and_caps_mid():
    rig = Rig().quoted()
    assert rig.row()[S] == ST_CHECK and rig.row()[P] == "40000"
    assert rig.row()[B].startswith("[가사 11/26 1/1] " + MARK + "[가견적-mid] AI-유사품"), rig.row()[B]
    hint, cap = rig.ai.calls[-1]
    assert cap == "mid"
    assert hint == "레이덱스 유사품: 과거 단가 150,000원 (2026-11-19 Ø28 +0.05~+0.06 L300) — 이보다 낮게 내지 말 것"


def test_human_ok_keeps_mark():
    rig = Rig().quoted()
    rig.row()[S] = "확인"
    rig.cycle()
    assert rig.row()[S] == watcher.ST_QUOTED_OK
    assert rig.row()[B].startswith("[가사 11/26 1/1] 사람 확인 | " + MARK), rig.row()[B]


def test_tolerance_upper_off_by_003_no_mark():
    rig = Rig(spec="SUS 사각 Ø28 +0.05~+0.09 / 300 / 2개").quoted()
    assert "레이덱스" not in rig.row()[B]
    assert rig.ai.calls[-1] == ("", None)


def test_length_off_by_15mm_no_mark():
    rig = Rig(spec="SUS 사각 Ø28 +0.05~+0.06 / 315 / 2개").quoted()
    assert "레이덱스" not in rig.row()[B]


def test_disabled_no_mark():
    rig = Rig(raydex={"client": "(주)레이덱스", "enabled": False}).quoted()
    assert "레이덱스" not in rig.row()[B]


def line(dia=28.0, lo=0.05, hi=0.06, length=300.0):
    return DbLine(1, date(2026, 11, 19), "(주)레이덱스", RX_REG, "x", "", 1, 150000, 150000, dia, length, lo, hi)


@pytest.mark.parametrize("spec, ok", [
    ("SUS 사각 Ø28 +0.05~+0.06 / 300", True),
    ("Ø28 +0.05~+0.06 / 300", True),                    # 재질·사각 미기재 → 통과
    ("AL Ø28 +0.05~+0.06 / 300", False),                # 다른 재질
    ("SUS Ø28.1 +0.05~+0.06 / 300", True),              # 구경 0.1 이내
    ("SUS Ø28.2 +0.05~+0.06 / 300", False),
    ("SUS Ø28 +0.03~+0.08 / 300", True),                # 상·하한 각각 0.02 이내
    ("SUS Ø28 / 300", True),                            # 공차 없음 → 통과
    ("SUS Ø28 +0.05~+0.06 / 310", True),                # 기장 10mm 이내
    ("SUS Ø28 +0.05~+0.06 / 2개", True),                # 기장 없음 → 통과
])
def test_raydex_ok_rules(spec, ok):
    assert raydex_ok(JobInput.from_raw({"spec_qty": spec}), line()) is ok


def test_example_tolerance_025_vs_045_same():
    ln = line(lo=0.0, hi=0.025)
    assert raydex_ok(JobInput.from_raw({"spec_qty": "SUS Ø28 +0~+0.045 / 300"}), ln)
    assert raydex_hint(ln).startswith("레이덱스 유사품: 과거 단가 150,000원 (2026-11-19 Ø28 +0~+0.025 L300)")


# ---------------------------------------------------------------- 레이덱스 본인
def test_raydex_itself_is_human_with_warning():
    warnings = []
    sink = logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        rig = Rig(client="(주)레이덱스")
        rig.cycle()
        rig.settle()
        rig.cycle()
    finally:
        logger.remove(sink)
    assert rig.row()[S] == "" and rig.row()[P] == ""
    assert rig.row()[B] == "[수동처리: 레이덱스 — 거래 단절]"
    assert any("레이덱스 — 거래 단절" in m for m in warnings)
    assert rig.ai.calls == []
