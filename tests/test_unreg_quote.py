"""보류-미등록 묶음 가견적 (임가공).

거래처가 미등록이어도 안정화 뒤 사다리를 돈다: D열 사전단가 → (동일품은 이력 없어 건너뜀) → AI(최대 mid).
상태는 보류-미등록 그대로(발행 차단), 단가 칸에 값, 산출근거 '[가견적-미등록 거래처] …'.
거래처가 나중에 해석되면(월말 import·clients add·별칭) 재호출 없이 사전단가 → 견적완료, AI → 견적확인필요(가견적 유지).
"""
from pricer import CONF_RANK
from pricer_job import Quote
from tests.test_post_quote_change import B, D, E, E50, P, S, Rig
from tests.test_watcher_settle import age_all
from watcher import ST_CHECK, ST_QUOTED, ST_QUOTED_OK, ST_TYPING, ST_UNREG

NEW = "신규정밀"


class CapAi:
    """항상 30000 high를 내되 max_conf 상한을 지킴 (실제 AiPricer._to_quote처럼). 호출 수를 셈."""
    def __init__(self):
        self.calls, self.caps = 0, []

    def quote(self, inp, index, cutoff, hint="", tag="", max_conf=None):
        self.calls += 1
        self.caps.append(max_conf)
        conf = "high" if not max_conf or CONF_RANK[max_conf] >= CONF_RANK["high"] else max_conf
        return Quote("AI-유사품", 30000, conf, "유사 구경 보간")


def unreg_rig(main_msg="", spec=E50, ai=None):
    rig = Rig(spec, ai=ai or CapAi())
    rig.row()[1] = NEW  # 마스터에 없는 거래처
    if main_msg:
        rig.row()[D] = main_msg
    return rig


def register(rig, name=NEW):
    """clients add / 월말 import 흉내 — 다음 바퀴 watcher가 마스터를 다시 읽는다."""
    rig.w.con.execute("INSERT INTO clients(name, reg_no, n_statements, last_date) VALUES(?,?,?,?)",
                      (name, "999-99-99999", 1, None))


def test_unreg_preset_quotes_then_resolves_to_quoted():
    rig = unreg_rig("6.5")
    rig.cycle()
    assert rig.row()[S] == ST_UNREG and rig.row()[P] == ""  # 안정화 전엔 견적 안 함
    age_all(rig.w)
    rig.cycle()
    assert (rig.row()[S], rig.row()[P]) == (ST_UNREG, "65000")
    assert "[가견적-미등록 거래처] 사전단가" in rig.row()[B], rig.row()[B]
    assert rig.p._ai.calls == 0
    basis = rig.row()[B]
    rig.cycle()  # 다음 바퀴: 그대로
    assert (rig.row()[S], rig.row()[P], rig.row()[B]) == (ST_UNREG, "65000", basis)

    register(rig)
    rig.cycle()
    assert (rig.row()[S], rig.row()[P]) == (ST_QUOTED, "65000")
    assert "가견적" not in rig.row()[B] and "사전단가" in rig.row()[B] and "| high" in rig.row()[B], rig.row()[B]
    assert rig.row()[B].startswith(f"[{NEW} 11/26 1/1] ")
    assert rig.p._ai.calls == 0


def test_unreg_ai_quotes_mid_then_resolves_to_check():
    rig = unreg_rig()
    rig.quoted()
    assert (rig.row()[S], rig.row()[P]) == (ST_UNREG, "30000")
    assert "[가견적-미등록 거래처] AI-유사품" in rig.row()[B] and "| mid" in rig.row()[B], rig.row()[B]
    assert rig.p._ai.calls == 1 and rig.p._ai.caps == ["mid"]
    rig.cycle()
    assert rig.p._ai.calls == 1  # 내용 그대로 → 다시 안 부름

    register(rig)
    rig.cycle()
    assert (rig.row()[S], rig.row()[P]) == (ST_CHECK, "30000")
    assert "[가견적-mid] AI-유사품" in rig.row()[B] and "미등록" not in rig.row()[B], rig.row()[B]
    assert rig.p._ai.calls == 1  # 재호출 없음

    rig.row()[S] = "확인"  # 사람 확인 → 적힌 단가로
    rig.cycle()
    assert (rig.row()[S], rig.row()[P]) == (ST_QUOTED_OK, "30000")
    assert rig.p._ai.calls == 1


def test_unreg_content_change_requotes_provisional():
    rig = unreg_rig("6.5")
    rig.quoted()
    assert rig.row()[P] == "65000"
    rig.row()[D] = "7"
    rig.cycle()
    assert (rig.row()[S], rig.row()[P]) == (ST_UNREG, "65000")  # 바뀌어도 보류-미등록, 안정화 전엔 단가 그대로
    age_all(rig.w)
    rig.cycle()
    assert (rig.row()[S], rig.row()[P]) == (ST_UNREG, "70000")


def test_unreg_human_price_without_basis_is_kept():
    rig = unreg_rig()
    rig.row()[P] = "12000"
    rig.quoted()
    assert (rig.row()[S], rig.row()[P]) == (ST_UNREG, "12000")
    assert rig.p._ai.calls == 0


def test_registered_client_unaffected():
    rig = Rig(E50, ai=CapAi())
    rig.row()[D] = "6.5"
    rig.cycle()
    assert rig.row()[S] == ST_TYPING
    age_all(rig.w)
    rig.cycle()
    assert rig.row()[P] == "65000" and "미등록" not in rig.row()[B]  # 기존 거래처(가사)는 평소 사다리
