"""사람 확정 표시: 견적확인필요 → 단가 수정 = '견적완료(사람수정)', 상태 칸만 비움 = '견적완료(사람확인)'.
watcher·발행기는 둘 다 견적완료로 취급. (실제 사례: 캘리버 20000→5000, 한승 50000/40000→60000/50000)"""
import pytest

import pricer
from issuer.outbox import ST_CHECK
from pricer_job import M_PENDING, Quote
from tests.test_issuer_job import JOB, issuer, job_row, snap as issuer_snap
from tests.test_pricer_ai import REG, FakeWriter, sheet_row
from tests.test_sheet_lines import env as _env  # noqa: F401  (fixture 재사용: con_db + 사다리)
from tests.test_sheet_lines import row_snap, sheet_rows
from tests.test_watcher_settle import C, poll, settled_block, statuses
from watcher import QUOTED_STATES, ST_QUOTED, ST_QUOTED_EDIT, ST_QUOTED_OK, ST_TYPING

CALIBER_E = "면곱게 Ø6.03~Ø6.04 / 200개"
HANSEUNG_E = "Ø150 +0.03~ +0.05 / 320 / 3개 + Ø120 +0.03~ +0.05 / 215 / 4개"


class StubAi:
    """캘리버: 단품 20000 low / 한승: 다품목 50000, 40000 mid."""
    def quote(self, inp, index, cutoff, hint="", tag="", max_conf=None):
        return Quote("AI-유사품", 20000, "low", "캘리버 이력 없음")

    def quote_multi(self, inp, segs, qs, index, cutoff, exclude_ids=(), tag=""):
        return [Quote("AI-유사품", 50000 - 10000 * k, "mid", "보간") for k, q in enumerate(qs)
                if q.method == M_PENDING]


@pytest.fixture
def env(_env):
    p, scfg, con = _env
    p._ai = StubAi()
    return p, scfg, con


def agent_quote(p, scfg, e):
    p.process_job("imgagong", scfg, row_snap((5, "R1", sheet_row(e=e), "확정")))
    return FakeWriter.last.writes


def test_status_values_are_distinct_and_quoted():
    assert QUOTED_STATES == {"견적완료", "견적완료(사람수정)", "견적완료(사람확인)"}
    assert pricer.ST_QUOTED_EDIT == ST_QUOTED_EDIT and pricer.ST_QUOTED_OK == ST_QUOTED_OK


# ---------------------------------------------------------------- 캘리버: 견적확인필요 + 단가 수정
def test_check_row_price_edit_is_human_edit_and_learns(env):
    p, scfg, con = env
    w = agent_quote(p, scfg, CALIBER_E)
    assert (w[(5, "unit_price")], w[(5, "status")]) == (20000, pricer.ST_CHECK)
    basis = w[(5, "basis")]
    assert basis.startswith("[가견적-low] AI-유사품")
    # 다음 바퀴 그대로 → 쓰기 없음
    p.process_job("imgagong", scfg, row_snap((5, "R1", sheet_row(e=CALIBER_E, status="견적확인필요", price="20000",
                                                                basis=basis), "견적확인필요")))
    assert FakeWriter.last.writes == {}
    # 사람이 단가 칸만 5000으로 (상태는 견적확인필요 그대로) → 견적완료(사람수정) + 즉시 학습
    p.process_job("imgagong", scfg, row_snap((5, "R1", sheet_row(e=CALIBER_E, status="견적확인필요", price="5000",
                                                                basis=basis), "견적확인필요")))
    w = FakeWriter.last.writes
    assert w[(5, "status")] == ST_QUOTED_EDIT and (5, "unit_price") not in w
    assert w[(5, "basis")].startswith("사람 수정 5000 (에이전트 20000) | AI-유사품 | 캘리버 이력 없음")
    assert [r[7] for r in sheet_rows(con)] == [5000]
    assert con.execute("SELECT agent_price, human_price, method FROM job_quotes").fetchone() == ("20000", "5000",
                                                                                                "사람수정")


# ---------------------------------------------------------------- 상태 칸만 비움 → 사람확인
def test_clear_status_only_is_human_ok(env):
    p, scfg, con = env
    basis = agent_quote(p, scfg, CALIBER_E)[(5, "basis")]
    p.process_job("imgagong", scfg, row_snap((5, "R1", sheet_row(e=CALIBER_E, status="", price="20,000",
                                                                basis=basis), "확정")))
    w = FakeWriter.last.writes
    assert w[(5, "status")] == ST_QUOTED_OK
    assert w[(5, "basis")].startswith("사람 확인 | AI-유사품")
    assert [r[7] for r in sheet_rows(con)] == [20000]
    assert con.execute("SELECT method FROM job_quotes").fetchone() == ("사람확인",)


def test_clear_status_and_change_price_is_human_edit(env):
    p, scfg, con = env
    basis = agent_quote(p, scfg, CALIBER_E)[(5, "basis")]
    p.process_job("imgagong", scfg, row_snap((5, "R1", sheet_row(e=CALIBER_E, status="", price="5000",
                                                                basis=basis), "확정")))
    w = FakeWriter.last.writes
    assert w[(5, "status")] == ST_QUOTED_EDIT
    assert w[(5, "basis")].startswith("사람 수정 5000 (에이전트 20000) | AI-유사품")
    assert con.execute("SELECT method, human_price FROM job_quotes").fetchone() == ("사람수정", "5000")


# ---------------------------------------------------------------- 한승: 다품목 "50000/40000" → "60000/50000"
def test_multi_item_price_edit(env):
    p, scfg, con = env
    w = agent_quote(p, scfg, HANSEUNG_E)
    assert (w[(5, "unit_price")], w[(5, "status")]) == ("50000/40000", pricer.ST_CHECK)
    basis = w[(5, "basis")]
    p.process_job("imgagong", scfg, row_snap((5, "R1", sheet_row(e=HANSEUNG_E, status="견적확인필요",
                                                                price="60000/50000", basis=basis), "견적확인필요")))
    w = FakeWriter.last.writes
    assert w[(5, "status")] == ST_QUOTED_EDIT
    assert w[(5, "basis")].startswith("사람 수정 60000/50000 (에이전트 50000/40000) | 다품목 2구간")
    assert sorted(r[7] for r in sheet_rows(con)) == [50000, 60000]   # 구간별 줄 즉시 학습


# ---------------------------------------------------------------- watcher·발행기: 견적완료로 취급
@pytest.mark.parametrize("st", [ST_QUOTED_EDIT, ST_QUOTED_OK])
def test_watcher_keeps_human_quoted_status(st):
    w = settled_block()
    for n in (2, 3, 4):
        w.ws.values[n - 1][C["status"]] = st
    poll(w)
    assert statuses(w) == [st] * 3          # 변경 없으면 유지 (견적 이후 단계)
    w.ws.values[2][C["qty"]] = "2개"
    poll(w)
    assert statuses(w) == [ST_TYPING] * 3   # 내용이 바뀌면 견적완료와 똑같이 재견적 대상


@pytest.mark.parametrize("st", [ST_QUOTED_EDIT, ST_QUOTED_OK])
def test_issuer_treats_human_quoted_as_quoted(st):
    from tests.test_issuer_job import con_db

    iss = issuer(con_db(), {"imgagong": JOB})
    rows = issuer_snap([job_row("Ø50 / 1개", "61000", status=st)], "가사", REG)["rows"]
    assert iss.waiting_reason(JOB, rows) is None
    rows = issuer_snap([job_row("Ø50 / 1개", "61000", status=ST_CHECK)], "가사", REG)["rows"]
    assert iss.waiting_reason(JOB, rows) is not None
