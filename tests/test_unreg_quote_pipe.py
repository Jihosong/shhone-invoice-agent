"""보류-미등록 묶음 가견적 (호닝파이프).

안정화 뒤 단가표로 단가·산출근거 '[가견적-미등록 거래처] 단가표 …'를 적되 상태는 보류-미등록 그대로(발행 차단).
거래처가 해석되면 단가표로 다시 계산해 견적완료. 보류 중 사람이 고친 단가는 견적완료(사람수정).
"""
from decimal import Decimal

from pricer import UNREG_TAG, PriceEntry, Pricer
from tests.test_watcher_settle import C, age_all, make, poll, r
from watcher import ST_CHECK, ST_QUOTED, ST_QUOTED_EDIT, ST_TYPING, ST_UNREG

NEW = "신규정밀"
TABLE = {"45x55": PriceEntry("45x55", Decimal(29700), Decimal(1)),
         "50x60": PriceEntry("50x60", Decimal(30000), Decimal(1))}
S, P, B = C["status"], C["unit_price"], C["basis"]


class Rig:
    def __init__(self, *rows):
        self.w = make([list(x) for x in rows])
        self.p = Pricer({"sheets": {"t": self.w.cfg["sheets"]["t"]}}, self.w.con)
        self.p.table_for = lambda _scfg: TABLE

    def row(self, i=0):
        return self.w.ws.values[1 + i]

    def cycle(self):
        poll(self.w)
        self.p.run_once(self.w.snapshots)
        self.w.con.commit()
        self.w.ws.values = [[str(v) for v in row] for row in self.w.ws.values]  # 실제 시트처럼 문자열로 읽힘

    def quoted(self):
        self.cycle()
        age_all(self.w)
        self.cycle()


def register(rig, name=NEW):
    rig.w.con.execute("INSERT INTO clients(name, reg_no, n_statements, last_date) VALUES(?,?,?,?)",
                      (name, "999-99-99999", 1, None))


def test_unreg_pipe_quotes_then_resolves_to_quoted():
    rig = Rig(r("11/25", NEW), r("", "", "50x60"))
    rig.cycle()
    assert rig.row()[S] == ST_UNREG and rig.row()[P] == ""  # 안정화 전엔 견적 안 함
    age_all(rig.w)
    rig.cycle()
    assert [rig.row(i)[S] for i in (0, 1)] == [ST_UNREG, ST_UNREG]
    assert [rig.row(i)[P] for i in (0, 1)] == ["29700", "30000"]
    assert rig.row()[B] == f"[{NEW} 11/25 1/2] {UNREG_TAG} 단가표 45x55 29,700/m × 1000mm", rig.row()[B]
    basis = rig.row()[B]
    rig.cycle()  # 그대로
    assert (rig.row()[S], rig.row()[P], rig.row()[B]) == (ST_UNREG, "29700", basis)

    register(rig)
    rig.cycle()  # 해제 → 확정 → 다시 계산해 견적완료 (같은 바퀴)
    assert [rig.row(i)[S] for i in (0, 1)] == [ST_QUOTED, ST_QUOTED]
    assert rig.row()[P] == "29700"
    assert rig.row()[B] == f"[{NEW} 11/25 1/2] 단가표 45x55 29,700/m × 1000mm | 거래처 등록 후 확정", rig.row()[B]


def test_unreg_pipe_human_edit_during_hold_kept():
    rig = Rig(r("11/25", NEW))
    rig.quoted()
    assert (rig.row()[S], rig.row()[P]) == (ST_UNREG, "29700")
    rig.row()[P] = "31,000"
    rig.cycle()
    assert (rig.row()[S], rig.row()[P]) == (ST_UNREG, "31,000")  # 보류 중엔 그대로
    register(rig)
    rig.cycle()
    assert (rig.row()[S], rig.row()[P]) == (ST_QUOTED_EDIT, "31,000")
    assert f"사람 수정 31,000 (에이전트 29700) | 단가표 45x55" in rig.row()[B], rig.row()[B]


def test_unreg_pipe_content_change_requotes():
    rig = Rig(r("11/25", NEW))
    rig.quoted()
    rig.row()[C["size"]] = "50x60"
    rig.cycle()
    assert (rig.row()[S], rig.row()[P]) == (ST_UNREG, "29700")  # 안정화 전엔 그대로
    age_all(rig.w)
    rig.cycle()
    assert (rig.row()[S], rig.row()[P]) == (ST_UNREG, "30000")


def test_unreg_pipe_error_then_check():
    rig = Rig(r("11/25", NEW, size="99x99"))
    rig.quoted()
    assert (rig.row()[S], rig.row()[P]) == (ST_UNREG, "")
    assert f"{UNREG_TAG} 단가표에 규격 없음(99x99)" in rig.row()[B], rig.row()[B]
    register(rig)
    rig.cycle()
    assert rig.row()[S] == ST_CHECK and "미등록" not in rig.row()[B]


def test_unreg_pipe_human_price_without_basis_kept():
    rig = Rig(r("11/25", NEW, unit_price="12000"))
    rig.quoted()
    assert (rig.row()[S], rig.row()[P]) == (ST_UNREG, "12000")
    assert UNREG_TAG not in rig.row()[B]


def test_registered_pipe_unaffected():
    rig = Rig(r("11/25", "선기산업"))
    rig.cycle()
    assert rig.row()[S] == ST_TYPING
    age_all(rig.w)
    rig.cycle()
    assert rig.row()[S] == ST_QUOTED and "미등록" not in rig.row()[B]
