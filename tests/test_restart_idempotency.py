"""재시작 멱등성 + 사람 결정 보호.

- 시트 내용 그대로인 채 프로세스를 껐다 켜면 쓰기 0건·AI 호출 0건.
- 켠 뒤 첫 바퀴는 보호 모드: 지문(hash_columns)이 DB와 같은 행은 상태·단가·산출근거를 바꾸지 않음.
  견적 가진 행 중 다시 견적 단계로 갈 행이 20% 초과(3행 이상)면 ERROR '재시작 후 대량 재견적 감지' + 중단.
- 견적완료(사람수정/사람확인) 행은 어떤 경로로 재견적이 일어나도 90일 이내 사람 단가 + 사양(구경·공차·기장·수량)
  같으면 그 단가로 견적완료(사람수정) 복원, AI 호출 없음. 사양 칸 밖(거래처 등) 변경도 AI 재견적 대신 재확인.
- 내용 그대로 다시 확정으로 온 자동 견적 행은 지난 견적 그대로 (AI 재호출 없음).
(실제 사례: 2026-11-26 19:29·11-27 10:02 재시작 첫 바퀴에 13~14행이 확정 → 사람수정 행까지 AI 재견적)
"""
import json
from datetime import datetime, timedelta

import pytest

import agent
import pricer
import watcher
from tests.test_post_quote_change import B, E, E50, E300, PRE, HighAi, P, Rig, S, SCFG, age_all
from watcher import (ST_CHECK, ST_QUOTED, ST_QUOTED_EDIT, ST_QUOTED_OK, ST_TYPING, RestartGuardError,
                     Watcher)

E40 = "Ø40 +0.0~+0.03 / 200 / 1개"
E60 = "Ø60 +0.0~+0.05 / 100 / 1개"
F = SCFG["columns"]["difficulty"] - 1
CLIENT = SCFG["columns"]["client"] - 1


class CountAi(HighAi):
    """HighAi(Ø50 → 30000 high, 그 밖 → 20000 low) + 호출 수."""
    def __init__(self):
        self.calls = 0

    def quote(self, *a, **kw):
        self.calls += 1
        return super().quote(*a, **kw)

    def quote_multi(self, *a, **kw):
        self.calls += 1
        return super().quote_multi(*a, **kw)


def restart(rig, ai=None):
    """프로세스 재시작 흉내: 같은 DB·같은 시트, 메모리 상태(첫 바퀴 기억 등)는 새로."""
    old = rig.w
    w = Watcher.__new__(Watcher)
    for name in ("cfg", "common", "dry_run", "ws", "gc", "con", "today_fn", "post_quote"):
        setattr(w, name, getattr(old, name))
    w.snapshots, w.defer_flush = {}, True
    rig.w = w
    rig.p = pricer.Pricer({"sheets": {"imgagong": SCFG}}, w.con)
    rig.p._ai = ai or CountAi()
    return rig.p._ai


def writes(rig):
    return [u for b in rig.w.ws.batches for u in b]


def sheet(rig):
    return [list(r) for r in rig.w.ws.values]


def db_status(rig):
    return [s for (s,) in rig.w.con.execute("SELECT status FROM rows ORDER BY row_number")]


def mixed():
    """견적 끝난 다섯 행 (한 묶음): 자동 견적완료 / 사람수정 / 사람확인 / 견적확인필요 가견적 / 자동 견적완료."""
    rig = Rig(E50, E300, E40, E60, E50.replace("2개", "4개"), ai=CountAi()).quoted()
    assert [rig.row(i)[S] for i in range(5)] == [ST_QUOTED, ST_CHECK, ST_CHECK, ST_CHECK, ST_QUOTED]
    rig.row(1)[P] = "5000"   # 견적확인필요 단가 수정 → 사람수정
    rig.row(2)[S] = "확인"   # 상태 칸 '확인' → 사람확인
    rig.cycle()
    assert [rig.row(i)[S] for i in range(5)] == [ST_QUOTED, ST_QUOTED_EDIT, ST_QUOTED_OK, ST_CHECK, ST_QUOTED]
    assert rig.cycle() == [] and writes(rig) == []   # 다 끝난 상태: 한 바퀴 더 돌아도 쓰기 없음
    return rig


# ---------------------------------------------------------------- 3) 시트 그대로 재시작 → 쓰기 0·AI 0
def test_restart_with_unchanged_sheet_writes_nothing_and_calls_no_ai():
    rig = mixed()
    before, statuses = sheet(rig), db_status(rig)
    ai = restart(rig)
    for _ in range(4):   # 첫 바퀴(보호) + 평소 바퀴들
        rig.cycle()
        assert writes(rig) == []
    assert ai.calls == 0
    assert sheet(rig) == before and db_status(rig) == statuses


def test_restart_twice_in_a_row_is_still_idempotent():
    rig = mixed()
    before = sheet(rig)
    for _ in range(3):
        ai = restart(rig)
        rig.cycle()
        rig.cycle()
        assert writes(rig) == [] and ai.calls == 0
    assert sheet(rig) == before


# ---------------------------------------------------------------- 4) 첫 바퀴 보호 + 대량 재견적 감지
def test_restart_after_status_column_wiped_aborts_without_writes():
    """꺼져 있는 동안 상태 칸이 통째로 지워짐(시트 되돌리기·열 실수 등) → 첫 바퀴에 중단, 아무것도 안 씀."""
    rig = mixed()
    for i in range(5):
        rig.row(i)[S] = ""
    before, statuses = sheet(rig), db_status(rig)
    ai = restart(rig)
    with pytest.raises(RestartGuardError, match="재시작 후 대량 재견적 감지"):
        rig.cycle()
    assert writes(rig) == [] and ai.calls == 0
    assert sheet(rig) == before and db_status(rig) == statuses   # DB도 되돌림
    with pytest.raises(RestartGuardError):   # 고치기 전에는 다시 켜도 또 멈춤
        restart(rig)
        rig.cycle()


def test_guard_can_be_turned_off():
    rig = mixed()
    for i in range(5):
        rig.row(i)[S] = ""
    restart(rig)
    rig.w.common["restart_requote_guard"] = 0
    rig.cycle()   # 첫 바퀴: 보호 — 상태 칸은 빈칸 그대로
    assert writes(rig) == [] and [rig.row(i)[S] for i in range(5)] == [""] * 5


def test_first_cycle_leaves_unchanged_rows_alone_then_human_ok_next_cycle(caplog):
    """꺼져 있는 동안 사람이 가견적 행에 '확인' → 첫 바퀴는 손대지 않고(경고만) 다음 바퀴에 사람확인."""
    rig = mixed()
    rig.row(3)[S] = "확인"
    ai = restart(rig)
    rig.cycle()
    assert writes(rig) == [] and rig.row(3)[S] == "확인"
    rig.cycle()
    assert (rig.row(3)[S], rig.row(3)[P]) == (ST_QUOTED_OK, "20000")
    assert ai.calls == 0


def test_first_cycle_changed_rows_go_through_normal_rules():
    """꺼져 있는 동안 내용이 바뀐 행은 보호 대상 아님 → 평소대로 (사람 단가 행은 단가 유지 + 재확인)."""
    rig = mixed()
    rig.row(1)[E] = E300.replace("1개", "2개")   # 사람수정 행 수량 변경
    ai = restart(rig)
    rig.cycle()
    assert (rig.row(1)[S], rig.row(1)[P]) == (ST_CHECK, "5000")
    assert "[사양 변경 — 단가 재확인] 사람 단가 5,000 유지" in rig.row(1)[B]
    assert [rig.row(i)[S] for i in (0, 2, 3, 4)] == [ST_QUOTED, ST_QUOTED_OK, ST_CHECK, ST_QUOTED]
    assert ai.calls == 0


# ---------------------------------------------------------------- 5) 사람 결정 보호
def test_human_row_status_wiped_restores_human_price_without_ai():
    """사람수정·사람확인 행 상태 칸이 지워져 확정으로 와도 AI 가견적이 아니라 사람 단가로 견적완료(사람수정)."""
    rig = mixed()
    rig.row(1)[S] = rig.row(2)[S] = ""
    ai = restart(rig)
    rig.cycle()             # 첫 바퀴: 보호 (2행은 기준 3행 미만이라 중단 안 함)
    assert writes(rig) == []
    rig.cycle()             # 확정 → 사람 단가 복원 (같은 writer라 중간 '확정' 없이)
    assert (rig.row(1)[S], rig.row(1)[P]) == (ST_QUOTED_EDIT, "5000")
    assert (rig.row(2)[S], rig.row(2)[P]) == (ST_QUOTED_EDIT, "20000")
    assert "사람 수정 5000 (복원:" in rig.row(1)[B] and "AI 호출 없음" in rig.row(1)[B]
    assert ai.calls == 0
    assert rig.cycle() == [] and writes(rig) == []   # 다음 바퀴 그대로


def test_auto_row_status_wiped_reuses_last_quote_without_ai():
    rig = mixed()
    price, basis = rig.row(0)[P], rig.row(0)[B]
    rig.row(0)[S] = ""
    ai = rig.p._ai
    ai.calls = 0
    rig.cycle()   # 실행 중 (첫 바퀴 아님): 확정 → 지난 견적 그대로
    assert (rig.row(0)[S], rig.row(0)[P], rig.row(0)[B]) == (ST_QUOTED, price, basis)
    assert ai.calls == 0


def test_human_row_difficulty_change_and_status_wiped_restores_by_spec():
    """F(난이도)만 바뀌어 지문은 달라졌지만 구경·공차·기장·수량은 같음 → 재견적 경로로 가도 사람 단가 복원."""
    rig = mixed()
    rig.row(1)[F], rig.row(1)[S] = "어려움", ""
    ai = restart(rig)
    rig.cycle()
    assert rig.row(1)[S] == ST_TYPING              # 내용 변경 → 입력중 (보호 대상 아님)
    age_all(rig.w)
    rig.cycle()
    assert (rig.row(1)[S], rig.row(1)[P]) == (ST_QUOTED_EDIT, "5000")
    assert ai.calls == 0


def test_human_row_spec_change_on_requote_path_is_quoted_again():
    """사양(수량)이 바뀐 채 재견적 경로로 오면 복원하지 않음 (사람 단가는 옛 사양 것)."""
    rig = mixed()
    rig.row(1)[E], rig.row(1)[S] = E300.replace("1개", "3개"), ""
    ai = restart(rig)
    rig.cycle()
    age_all(rig.w)
    rig.cycle()
    assert rig.row(1)[S] == ST_CHECK and rig.row(1)[P] == "20000"   # AI 가견적 (StubAi 20000 low)
    assert ai.calls == 1


def test_old_human_price_is_not_restored():
    rig = mixed()
    old = (datetime.now() - timedelta(days=91)).isoformat(timespec="seconds")
    rig.w.con.execute("UPDATE job_quotes SET human_at=? WHERE human_price IS NOT NULL", (old,))
    rig.row(1)[S] = ""
    rig.cycle()
    assert "(복원:" not in rig.row(1)[B]   # 90일 지난 사람 단가는 복원 안 함 → 평소 견적 (여기선 학습된 동일품)
    assert rig.row(1)[B].split("] ", 1)[1].startswith("동일품")


def test_human_row_client_change_rechecks_instead_of_ai_requote():
    """사람 단가 행의 사양 칸 밖(거래처 칸) 변경 → AI 재견적이 아니라 단가 유지 + 견적확인필요."""
    rig = mixed()
    rig.row(1)[CLIENT] = "택배"   # 이어지는 줄의 거래처 칸 = 그 행 메모 (지문에 들어감, 감시 칸 아님)
    rig.p._ai.calls = 0
    rig.cycle()
    age_all(rig.w)
    rig.cycle()
    assert (rig.row(1)[S], rig.row(1)[P]) == (ST_CHECK, "5000")
    assert rig.row(1)[B].split("] ", 1)[1].startswith("[내용 변경 — 단가 재확인] 사람 단가 5,000 유지")
    assert rig.p._ai.calls == 0
    rig.row(1)[S] = "확인"   # 사람이 확인 → 적힌 단가로 사람확인
    rig.cycle()
    assert (rig.row(1)[S], rig.row(1)[P]) == (ST_QUOTED_OK, "5000")


# ---------------------------------------------------------------- 2) 감시 칸을 늘려도 거짓 변경 없음
def test_new_watch_field_is_not_a_change():
    rig = mixed()
    rig.row(0)[PRE] = "입구 찍힘"   # S는 지문 밖 — 예전 DB에 이 칸 기억이 없는 상황
    for (rid, vals) in rig.w.con.execute("SELECT row_id, watch_vals FROM rows").fetchall():
        d = json.loads(vals)
        d.pop("pre_note", None)
        rig.w.con.execute("UPDATE rows SET watch_vals=? WHERE row_id=?", (json.dumps(d), rid))
    ai = restart(rig)
    rig.cycle()
    rig.cycle()
    assert writes(rig) == [] and "[변경" not in rig.row(0)[B] and ai.calls == 0


def test_fingerprint_ignores_column_numbers():
    """지문은 hash_columns 이름 순서의 원문 문자열만 — 열 번호가 바뀌어도 같은 값이면 같은 지문."""
    vals = ["가사", "급함", E50, "", "-0.2"]
    cols_a = {"client": 2, "main_msg": 4, "spec_qty": 5, "difficulty": 6, "stock_removal": 8, "row_id": 26}
    cols_b = {"client": 3, "main_msg": 7, "spec_qty": 9, "difficulty": 10, "stock_removal": 1, "row_id": 12}
    hc = SCFG["hash_columns"]

    def row_for(cols):
        row = [""] * 30
        for name, v in zip(hc, vals):
            row[cols[name] - 1] = v
        return row

    fa = watcher.SheetWriter(cols_a, 3, hc).fp(row_for(cols_a))
    fb = watcher.SheetWriter(cols_b, 3, hc).fp(row_for(cols_b))
    assert fa == fb == watcher.fingerprint(vals)


def test_agent_guarded_exits_with_message():
    def boom():
        raise RestartGuardError("재시작 후 대량 재견적 감지: test")

    with pytest.raises(SystemExit, match="중단: 재시작 후 대량 재견적 감지"):
        agent._guarded(boom)
