"""행 정체성 = row_id, 변경 = hash_columns 지문만.

행 번호 이동·묶음 안 n/m 변화·위쪽 행 삭제는 '변경'이 아님 (산출근거 태그만 갱신).
시트에서 사라진 row_id는 DB '삭제됨'. 읽기와 쓰기 사이에 행이 지워져도 쓰기는 row_id가 있는 행으로.
"""
from decimal import Decimal

from pricer import ST_QUOTED, PriceEntry, Pricer
from tests.test_watcher_settle import C, HEADER, SCFG, age_all, poll, r
from tests.test_watcher_legacy import make_watcher
from watcher import ST_CONFIRMED, ST_DELETED, ST_SKIP, ST_TYPING, strip_block_tag

MASTER = [("선기산업", "1", 5), ("세종정밀", "2", 3), ("광일유압", "3", 4), ("영성기공", "4", 3)]
BLANK = [""] * len(HEADER)


def make(rows, blank_tail=5):
    # 실제 시트처럼 데이터 아래 빈 행이 있음 (어긋난 쓰기가 떨어질 자리)
    return make_watcher([list(x) for x in rows] + [list(BLANK) for _ in range(blank_tail)],
                        skip_clients=["영성기공"], clients=MASTER, scfg=SCFG, header=HEADER)


def quoted(w):
    """확정까지 진행 후 견적완료 + 단가를 사람이 아니라 견적 단계가 적은 것처럼 채움."""
    poll(w)
    age_all(w)
    poll(w)
    for row in w.ws.values[1:]:
        if row[C["status"]] == ST_CONFIRMED:
            row[C["status"]] = ST_QUOTED
            row[C["unit_price"]] = "12340"
            row[C["basis"]] = row[C["basis"]] + " 105x126 1M당 …"
    poll(w)


def find(w, client):
    return next(row for row in w.ws.values[1:] if row[C["client"]] == client)


def db_status(w, row_id):
    return w.con.execute("SELECT status FROM rows WHERE row_id=?", (row_id,)).fetchone()[0]


TEST_ROWS = [r("11/25", "선기산업(TEST)"), r("", "", "50x60"), r("", "", "60x70"), r("11/25", "세종정밀(TEST2)")]
KWANGIL = r("11/25", "광일유압(택배)", "105x126")


# ---------------------------------------------------------------- (a) 위 행 삭제 → 상태 유지
def test_deleting_rows_above_keeps_quoted_status():
    w = make(TEST_ROWS + [KWANGIL])
    quoted(w)
    before = list(find(w, "광일유압(택배)"))
    assert before[C["status"]] == ST_QUOTED
    gone_ids = [row[C["row_id"]] for row in w.ws.values[1:5]]

    del w.ws.values[1:5]  # 테스트 행 4개 삭제 → 광일유압이 6행에서 2행으로
    snap = poll(w)

    after = w.ws.values[1]
    assert after[C["client"]] == "광일유압(택배)"
    assert after == before  # 상태·단가·산출근거·row_id 모두 그대로 (쓰기 없음)
    assert snap[2]["status"] == ST_QUOTED and not snap[2]["row_id"] in gone_ids
    assert db_status(w, after[C["row_id"]]) == ST_QUOTED
    assert w.con.execute("SELECT row_number FROM rows WHERE row_id=?", (after[C["row_id"]],)).fetchone()[0] == 2
    # 지워진 행은 DB에서 삭제됨, 이후 스냅샷(→ pricer)에도 없음
    assert [db_status(w, i) for i in gone_ids] == [ST_DELETED] * 4
    assert {x["row_id"] for x in snap.values()} == {after[C["row_id"]]}


def test_deleting_rows_one_by_one_keeps_status():
    w = make(TEST_ROWS + [KWANGIL])
    quoted(w)
    before = list(find(w, "광일유압(택배)"))
    for _ in range(4):
        del w.ws.values[1]  # 맨 위부터 한 줄씩, 매번 폴링
        poll(w)
    assert w.ws.values[1] == before


def test_row_removed_inside_block_only_updates_tag():
    w = make([r("11/25", "선기산업"), r("", "", "50x60"), r("", "", "60x70")])
    quoted(w)
    ids = [row[C["row_id"]] for row in w.ws.values[1:4]]
    del w.ws.values[2]  # 묶음 가운데 줄 삭제 → 남은 두 줄은 1/2, 2/2
    poll(w)
    rows = w.ws.values[1:3]
    assert [x[C["status"]] for x in rows] == [ST_QUOTED, ST_QUOTED]
    assert [x[C["unit_price"]] for x in rows] == ["12340", "12340"]
    assert [x[C["basis"]].split("]")[0] + "]" for x in rows] == ["[선기산업 11/25 1/2]", "[선기산업 11/25 2/2]"]
    assert [strip_block_tag(x[C["basis"]]) for x in rows] == ["105x126 1M당 …"] * 2  # 근거 본문 유지
    assert db_status(w, ids[1]) == ST_DELETED


def test_block_head_deleted_rows_join_previous_block_without_change():
    # 오성 묶음 머리 행을 지우면 이어지는 줄이 위 묶음(선기산업)에 붙음 — 내용 변경 아님
    w = make([r("11/25", "선기산업"), r("11/25", "세종정밀"), r("", "", "50x60")])
    quoted(w)
    del w.ws.values[2]
    poll(w)
    assert [x[C["status"]] for x in w.ws.values[1:3]] == [ST_QUOTED, ST_QUOTED]


# ---------------------------------------------------------------- (b) 월말처럼 30행 삭제
def test_month_end_bulk_delete_keeps_remaining_statuses():
    rows = []
    for i in range(10):  # 3줄 묶음 10개 = 30행 (지울 것)
        rows += [r("11/%d" % (i + 1), "선기산업"), r("", "", "50x60"), r("", "", "60x70")]
    keep = [r("11/25", "광일유압(택배)", "105x126"), r("", "", "80x90"),
            r("11/25", "세종정밀"), r("11/25", "영성기공")]
    w = make(rows + keep)
    quoted(w)
    kept_before = [list(x) for x in w.ws.values[31:35]]
    assert [x[C["status"]] for x in kept_before] == [ST_QUOTED, ST_QUOTED, ST_QUOTED, ST_SKIP]
    gone_ids = [x[C["row_id"]] for x in w.ws.values[1:31]]

    del w.ws.values[1:31]
    snap = poll(w)

    assert [list(x) for x in w.ws.values[1:5]] == kept_before  # 4행 모두 그대로
    assert sorted(snap) == [2, 3, 4, 5]
    assert all(db_status(w, i) == ST_DELETED for i in gone_ids)
    assert w.con.execute("SELECT COUNT(*) FROM rows WHERE status=?", (ST_DELETED,)).fetchone()[0] == 30


# ---------------------------------------------------------------- (c) 내용 변경은 여전히 입력중
def test_content_change_still_resets_to_typing():
    w = make(TEST_ROWS + [KWANGIL])
    quoted(w)
    del w.ws.values[1:5]
    poll(w)
    w.ws.values[1][C["qty"]] = "2개"  # 지문 칸 변경
    poll(w)
    assert w.ws.values[1][C["status"]] == ST_TYPING


def test_moved_and_changed_in_same_poll_resets():
    w = make(TEST_ROWS + [KWANGIL])
    quoted(w)
    del w.ws.values[1:5]
    w.ws.values[1][C["length"]] = "600"
    poll(w)
    assert w.ws.values[1][C["status"]] == ST_TYPING


def test_non_hash_column_edit_is_not_change():
    w = make([KWANGIL])
    quoted(w)
    w.ws.values[1][C["work_end"]] = "FALSE"
    w.ws.values[1][C["basis"]] = "[엉뚱한 11/1 3/9] 105x126 1M당 …"  # 태그만 틀어짐 → 고쳐 쓰기만
    poll(w)
    row = w.ws.values[1]
    assert row[C["status"]] == ST_QUOTED
    assert row[C["basis"]] == "[광일유압(택배) 11/25 1/1] 105x126 1M당 …"


# ---------------------------------------------------------------- 삭제 후 되살리기 (실행 취소)
def test_deleted_row_restored_when_it_reappears():
    w = make(TEST_ROWS + [KWANGIL])
    quoted(w)
    saved = [list(x) for x in w.ws.values[1:5]]
    del w.ws.values[1:5]
    poll(w)
    assert db_status(w, saved[0][C["row_id"]]) == ST_DELETED
    w.ws.values[1:1] = saved  # Ctrl+Z
    poll(w)
    assert [x[C["status"]] for x in w.ws.values[1:6]] == [ST_QUOTED] * 3 + [w.ws.values[4][C["status"]], ST_QUOTED]
    assert db_status(w, saved[0][C["row_id"]]) == ST_QUOTED


# ---------------------------------------------------------------- 읽기~쓰기 사이에 행 삭제 (경합)
def race(w, delete_at):
    """다음 폴링에서 get_all_values 직후 사람이 delete_at 행(0-based 인덱스)을 지운 것처럼."""
    orig = w.ws.get_all_values
    state = {"done": False}

    def gav():
        v = orig()
        if not state["done"]:
            state["done"] = True
            del w.ws.values[delete_at]
        return v
    w.ws.get_all_values = gav
    return lambda: setattr(w.ws, "get_all_values", orig)


def test_tag_write_follows_row_id_when_rows_deleted_mid_poll():
    w = make([r("11/25", "선기산업"), r("", "", "50x60"), r("", "", "60x70"), KWANGIL])
    quoted(w)
    del w.ws.values[3]  # 묶음 셋째 줄 삭제 → 다음 폴링에서 1/2, 2/2로 고쳐 씀
    undo = race(w, 1)   # ...그런데 읽은 직후 첫 줄도 삭제됨
    poll(w)
    # 2행은 원래 둘째 줄, 3행은 광일유압 — 바로 그 바퀴에 광일유압 산출근거에 선기산업 태그가 떨어지면 안 됨
    assert w.ws.values[2][C["client"]] == "광일유압(택배)"
    assert w.ws.values[2][C["basis"]].startswith("[광일유압(택배) 11/25 1/1]")
    undo()
    poll(w)
    assert w.ws.values[2][C["basis"]].startswith("[광일유압(택배) 11/25 1/1]")
    assert w.ws.values[2][C["status"]] == ST_QUOTED
    assert all(not any(x) for x in w.ws.values[3:])  # 빈 행에 어긋난 쓰기 없음


def test_new_row_id_not_written_to_wrong_row_mid_poll():
    w = make([KWANGIL, r("11/25", "세종정밀")])
    quoted(w)
    kid = w.ws.values[1][C["row_id"]]
    w.ws.values.insert(3, r("11/25", "선기산업"))  # 새 행 (row_id 없음)
    undo = race(w, 1)  # 읽은 직후 광일유압 행 삭제 → 새 행 위치가 한 칸 당겨짐
    poll(w)
    undo()
    poll(w)
    ids = [x[C["row_id"]] for x in w.ws.values[1:3]]
    assert w.ws.values[2][C["client"]] == "선기산업" and ids[1]  # 결국 자기 자리에서 row_id 받음
    assert ids[0] != kid and len(set(ids)) == 2
    assert db_status(w, kid) == ST_DELETED


def test_pricer_writes_follow_row_id():
    w = make([r("11/25", "선기산업(TEST)"), KWANGIL])
    poll(w)
    age_all(w)
    poll(w)  # 둘 다 확정
    p = Pricer({"sheets": {"t": SCFG}}, w.con)
    p.table_for = lambda scfg: {"45x55": PriceEntry("45x55", Decimal("10000"), Decimal("1")),
                                "105x126": PriceEntry("105x126", Decimal("50000"), Decimal("1"))}
    snap = w.snapshots["t"]
    del w.ws.values[1]  # watcher가 읽은 뒤, pricer가 쓰기 전에 위 행 삭제
    p.process_pipe("t", SCFG, snap)
    row = w.ws.values[1]
    assert row[C["client"]] == "광일유압(택배)"
    assert row[C["status"]] == ST_QUOTED and row[C["unit_price"]] == 50000
    assert row[C["basis"]].startswith("[광일유압(택배) 11/25 1/1]")
    assert all(not any(x) for x in w.ws.values[2:])


def test_pricer_skips_row_edited_after_read():
    w = make([KWANGIL])
    poll(w)
    age_all(w)
    poll(w)
    p = Pricer({"sheets": {"t": SCFG}}, w.con)
    p.table_for = lambda scfg: {"105x126": PriceEntry("105x126", Decimal("50000"), Decimal("1"))}
    snap = w.snapshots["t"]
    w.ws.values[1][C["length"]] = "2000"  # 읽은 뒤 길이 수정 → 낡은 견적을 쓰면 안 됨
    p.process_pipe("t", SCFG, snap)
    assert w.ws.values[1][C["status"]] == ST_CONFIRMED and w.ws.values[1][C["unit_price"]] == ""
    poll(w)
    assert w.ws.values[1][C["status"]] == ST_TYPING
