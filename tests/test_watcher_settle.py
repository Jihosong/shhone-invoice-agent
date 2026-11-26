"""확정 판정 묶음 단위 + 산출근거 묶음 표시 + agent.py blocks 출력."""
from datetime import datetime, timedelta
from decimal import Decimal

from agent import format_blocks
from pricer import ST_QUOTED, PriceEntry, Pricer
from tests.test_watcher_legacy import make_watcher
from watcher import (ST_CONFIRMED, ST_HOMONYM, ST_ISSUED, ST_SCREEN, ST_SKIP, ST_TYPING, ST_UNREG, strip_block_tag,
                     with_block_tag)

# A 날짜, B 회사, C 구분, D 규격, E 길이, F 갯수, G 작업완료, H 출고완료, I 작성자, J 상태, K 단가, L 산출근거, M 행ID
COLS = {"date": 1, "client": 2, "pipe_type": 3, "size": 4, "length": 5, "qty": 6, "work_end": 7, "shipped": 8,
        "author": 9, "status": 10, "unit_price": 11, "basis": 12, "row_id": 13}
SCFG = {"spreadsheet_id": "x", "label": "파이프", "kind": "pipe", "data_start_row": 2, "columns": COLS,
        "hash_columns": ["client", "pipe_type", "size", "length", "qty"], "item_columns": ["size", "length", "qty"]}
HEADER = ["날짜", "회사", "구분", "규격", "길이", "갯수", "작업완료", "출고완료", "작성자", "상태", "단가", "산출근거", "행ID"]
MASTER = [("선기산업", "1", 5), ("오성기계공구", "2", 4), ("영성기공", "3", 3)]
C = {name: i for i, name in enumerate(COLS)}  # 0-based 열 인덱스


def r(date, client, size="45x55", author="", status="", basis="", unit_price=""):
    return [date, client, "H.P", size, "1000", "1개", "FALSE", "FALSE", author, status, unit_price, basis, ""]


BLOCK3 = [r("11/25", "선기산업"), r("", "", "50x60"), r("", "", "60x70")]


def make(rows):
    w = make_watcher(rows, skip_clients=["영성기공"], clients=MASTER, scfg=SCFG, header=HEADER)
    w.post_quote |= {ST_SCREEN}  # 실제 sheets.yaml status_values처럼 견적 이후 단계 (보류-동명은 매 바퀴 재검사)
    return w


def poll(w):
    w.process_sheet("t", SCFG)
    return {x["rownum"]: x for x in w.snapshots["t"]["rows"]}


def statuses(w, rownums=(2, 3, 4)):
    return [w.ws.values[n - 1][C["status"]] for n in rownums]


def age_all(w, minutes=10):
    """DB의 모든 행 last_change를 과거로 → 시간 경과 흉내."""
    past = (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    w.con.execute("UPDATE rows SET last_change=?", (past,))


def settled_block():
    w = make([list(x) for x in BLOCK3])
    poll(w)
    age_all(w)
    poll(w)
    assert statuses(w) == [ST_CONFIRMED] * 3
    return w


# ---------------------------------------------------------------- (a) 한 행만 변경 → 묶음 전체 입력중
def test_first_row_client_edit_reverts_whole_block():
    w = settled_block()
    w.ws.values[1][C["client"]] = "선기산업 급함"
    poll(w)
    assert statuses(w) == [ST_TYPING] * 3  # 2·3행이 먼저 확정되면 안 됨


def test_last_row_edit_reverts_first_row_too():
    w = settled_block()
    w.ws.values[3][C["length"]] = "1200"
    poll(w)
    assert statuses(w) == [ST_TYPING] * 3  # 1행이 확정으로 남으면 안 됨


def test_one_row_recent_keeps_block_typing():
    w = make([list(x) for x in BLOCK3])
    snap = poll(w)
    age_all(w)
    recent = datetime.now().isoformat(timespec="seconds")
    w.con.execute("UPDATE rows SET last_change=? WHERE row_id=?", (recent, snap[4]["row_id"]))
    poll(w)
    assert statuses(w) == [ST_TYPING] * 3


def test_edit_after_quote_reverts_quoted_rows():
    w = settled_block()
    for n in (2, 3, 4):
        w.ws.values[n - 1][C["status"]] = ST_QUOTED
    poll(w)
    assert statuses(w) == [ST_QUOTED] * 3  # 변경 없으면 유지
    w.ws.values[2][C["qty"]] = "2개"
    poll(w)
    assert statuses(w) == [ST_TYPING] * 3  # 묶음 전체 재견적 대상


def test_row_added_to_known_block_reverts_block():
    w = settled_block()
    w.ws.values.append(r("", "", "80x90"))
    poll(w)
    assert statuses(w, (2, 3, 4, 5)) == [ST_TYPING] * 4


def test_issued_row_stays_issued_when_block_edited():
    w = settled_block()
    w.ws.values[1][C["status"]] = ST_ISSUED
    w.ws.values[3][C["length"]] = "1200"
    poll(w)
    assert statuses(w) == [ST_ISSUED, ST_TYPING, ST_TYPING]


# ---------------------------------------------------------------- (b) 5분 경과 → 묶음 전체 확정
def test_settle_confirms_whole_block_together():
    w = make([list(x) for x in BLOCK3])
    poll(w)
    assert statuses(w) == [ST_TYPING] * 3
    age_all(w, minutes=6)
    poll(w)
    assert statuses(w) == [ST_CONFIRMED] * 3


def test_blocks_settle_independently():
    w = make([list(x) for x in BLOCK3] + [r("11/25", "오성기계공구")])
    snap = poll(w)
    age_all(w)
    recent = datetime.now().isoformat(timespec="seconds")
    w.con.execute("UPDATE rows SET last_change=? WHERE row_id=?", (recent, snap[5]["row_id"]))
    poll(w)
    assert statuses(w, (2, 3, 4, 5)) == [ST_CONFIRMED] * 3 + [ST_TYPING]


# ---------------------------------------------------------------- (c) 보류 → 묶음 전체 보류
def test_unregistered_block_all_held():
    w = make([r("11/25", "모르는상사"), r("", "", "50x60", status=ST_QUOTED), r("", "", "60x70")])
    poll(w)
    assert statuses(w) == [ST_UNREG] * 3
    age_all(w)
    poll(w)
    assert statuses(w) == [ST_UNREG] * 3  # 시간이 지나도 확정 안 됨


def test_one_held_row_holds_whole_block():
    """후속 단계가 한 행에 보류-화면이상을 달면 나머지 행도 같은 보류 (확정·견적으로 넘어가지 않음)."""
    w = settled_block()
    w.ws.values[2][C["status"]] = ST_SCREEN
    poll(w)
    assert statuses(w) == [ST_SCREEN] * 3
    poll(w)
    assert statuses(w) == [ST_SCREEN] * 3  # 사람이 손대기 전까지 유지


def test_hold_does_not_override_human_or_skip():
    w = make([r("11/25", "선기산업"), r("", "", "50x60", author="팀장"), r("", "", "60x70", status=ST_SCREEN)])
    poll(w)
    assert statuses(w) == ["", "", ""]  # 묶음 안 사람 작성자 → 보류보다 우선, 묶음 전체 사람처리
    w2 = make([r("11/25", "영성기공"), r("", "", "50x60", status=ST_SCREEN)])
    poll(w2)
    assert statuses(w2, (2, 3)) == [ST_SKIP, ST_SKIP]


# ---------------------------------------------------------------- 보류 해제
def add_client(w, name, reg_no):
    w.con.execute("INSERT INTO clients(name, reg_no, n_statements) VALUES(?,?,1)", (name, reg_no))


def test_homonym_block_held_and_auto_released():
    w = settled_block()
    add_client(w, "선기산업", "999-99-99999")  # 같은 이름, 다른 사업자번호 → 동명
    poll(w)
    assert statuses(w) == [ST_HOMONYM] * 3
    age_all(w)
    poll(w)
    assert statuses(w) == [ST_HOMONYM] * 3  # 시간이 지나도 확정 안 됨
    w.con.execute("DELETE FROM clients WHERE reg_no='999-99-99999'")  # 마스터 정리 → 다음 폴링에 자동 해제
    poll(w)
    assert statuses(w) == [ST_CONFIRMED] * 3


def test_homonym_same_reg_no_twice_is_not_homonym():
    w = make([list(x) for x in BLOCK3])
    add_client(w, "선기산업", "1")
    poll(w)
    assert statuses(w) == [ST_TYPING] * 3


def test_stale_homonym_status_on_sheet_released():
    """동명 조건이 없는데 시트에 보류-동명이 남아 있으면 (예: 예전 판정) 다음 폴링에 풀림."""
    w = settled_block()
    w.ws.values[2][C["status"]] = ST_HOMONYM
    poll(w)
    assert statuses(w) == [ST_CONFIRMED] * 3


def test_homonym_beats_screen_priority():
    w = make([r("11/25", "선기산업"), r("", "", "50x60", status=ST_SCREEN)])
    add_client(w, "선기산업", "999")
    poll(w)
    assert statuses(w, (2, 3)) == [ST_HOMONYM, ST_HOMONYM]  # 동명 > 화면이상


def screen_held_block():
    w = settled_block()
    w.ws.values[1][C["status"]] = ST_SCREEN
    poll(w)
    assert statuses(w) == [ST_SCREEN] * 3
    return w


def test_screen_hold_released_when_any_row_cleared():
    w = screen_held_block()
    w.ws.values[3][C["status"]] = ""  # 사람이 3번째 행 상태 칸만 비움
    poll(w)
    assert statuses(w) == [ST_CONFIRMED] * 3  # 묶음 전체 재평가 (5분 지났으므로 함께 확정)


def test_screen_hold_cleared_recent_edit_goes_typing():
    w = screen_held_block()
    w.ws.values[2][C["status"]] = ""
    w.ws.values[2][C["length"]] = "1500"  # 비우면서 내용도 고침
    poll(w)
    assert statuses(w) == [ST_TYPING] * 3


def test_screen_hold_not_released_by_other_value():
    w = screen_held_block()
    w.ws.values[3][C["status"]] = ST_CONFIRMED  # 비운 게 아님 → 해제 아님
    poll(w)
    assert statuses(w) == [ST_SCREEN] * 3


def test_screen_hold_release_still_respects_homonym():
    w = screen_held_block()
    add_client(w, "선기산업", "999")
    w.ws.values[2][C["status"]] = ""
    poll(w)
    assert statuses(w) == [ST_HOMONYM] * 3


# ---------------------------------------------------------------- 산출근거 묶음 표시
def test_block_tag_written_to_basis():
    w = make([r("11/25", "선기산업(TEST)"), r("", "추가분", "50x60"), r("", "메모만", ""),
              r("", "", "60x70", basis="사람 메모"), r("11/26", "모르는상사")])
    w.ws.values[3][C["length"]] = w.ws.values[3][C["qty"]] = ""  # 4행 = 메모 행
    snap = poll(w)
    basis = [w.ws.values[n - 1][C["basis"]] for n in (2, 3, 4, 5, 6)]
    assert basis == ["[선기산업(TEST) 11/25 1/3]", "[선기산업(TEST) 11/25 2/3]", "",
                     "[선기산업(TEST) 11/25 3/3] 사람 메모", "[모르는상사 11/26 1/1]"]
    assert snap[2]["block_tag"] == "[선기산업(TEST) 11/25 1/3]"
    # 다시 돌려도 중복으로 붙지 않음, 순번이 바뀌면 교체
    w.ws.values.insert(2, r("", "", "80x90"))
    poll(w)
    assert w.ws.values[5][C["basis"]] == "[선기산업(TEST) 11/25 4/4] 사람 메모"


def test_block_tag_not_written_to_human_or_skip_rows():
    w = make([r("11/25", "선기산업", basis="사람 근거"), r("", "", "50x60", author="팀장", basis="팀장 메모"),
              r("", "", "60x70"),
              r("11/25", "영성기공", basis="월말 메모"), r("", "", "50x60"),
              r("11/26", "오성기계공구", author="사장"), r("", "", "50x60", basis="[예전 11/1 1/1] 그대로")])
    snap = poll(w)
    basis = [w.ws.values[n - 1][C["basis"]] for n in range(2, 9)]
    # 선기산업 묶음은 2행 작성자 '팀장' → 묶음 전체 사람처리라 태그 안 씀
    assert basis == ["사람 근거", "팀장 메모", "",
                     "월말 메모", "", "", "[예전 11/1 1/1] 그대로"]
    assert snap[3]["block_tag"] == "[선기산업 11/25 2/3]"  # blocks 출력·순번은 그대로


def test_block_tag_helpers():
    assert with_block_tag("[가 11/25 1/2]", "[가 11/24 1/1] 단가표 45x55") == "[가 11/25 1/2] 단가표 45x55"
    assert strip_block_tag("[대양상사(panjang sama) 11/24 2/3] 근거") == "근거"
    assert strip_block_tag("[참고] 사람 근거") == "[참고] 사람 근거"


def test_pricer_keeps_tag_and_human_price():
    w = make([r("11/25", "선기산업"), r("", "", "50x60", unit_price="99000")])
    poll(w)
    age_all(w)
    poll(w)  # 확정 (2행 산출근거는 묶음 표시만 → 사람 입력 가격으로 봄)
    snap = w.snapshots["t"]
    snap["rows"][1]["row"][C["basis"]] = w.ws.values[2][C["basis"]]
    p = Pricer({"sheets": {"t": SCFG}}, w.con)
    p.table_for = lambda _scfg: {"45x55": PriceEntry("45x55", Decimal(29700), Decimal(1))}
    p.run_once(w.snapshots)
    assert w.ws.values[1][C["basis"]] == "[선기산업 11/25 1/2] 단가표 45x55 29,700/m × 1000mm"
    assert w.ws.values[2][C["unit_price"]] == "99000"
    assert w.ws.values[2][C["basis"]] == "[선기산업 11/25 2/2]"
    assert statuses(w, (2, 3)) == [ST_QUOTED, ST_QUOTED]


# ---------------------------------------------------------------- agent.py blocks
def test_format_blocks():
    w = make([list(x) for x in BLOCK3] + [r("11/26", "오성기계공구(택배)", author="팀장")])
    poll(w)
    w.ws.values[2][C["status"]] = ST_QUOTED  # 섞인 상태 표시 확인용 (변경 아님 → 유지)
    snap = poll(w)
    assert format_blocks(list(snap.values())) == [
        "선기산업 / 11/25 / 행 2,3,4 / 입력중×2, 견적완료×1",
        "오성기계공구(택배) / 11/26 / 행 5 / 사람처리",
    ]
