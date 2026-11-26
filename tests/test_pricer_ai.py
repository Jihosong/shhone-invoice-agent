"""임가공 B단계(pricer_ai: Claude 견적) — 가짜 클라이언트로 요청 형태·참고 사례·비용 로그·상한·운영 상태."""
import json
from datetime import date
from types import SimpleNamespace

import pytest

import backtest
import pricer
import pricer_ai
from cases import DbLine, LineIndex
from pricer_ai import AiError, AiPricer, build_user_message, call_cost, calls_today, parse_answer
from pricer_job import JobInput

REG = "111-11-11111"
CUT = date(2026, 6, 1)


def ln(i, d, price, dia=50.0, length=None, qty=1, lo=0.0, hi=0.05, item="시린더호닝", reg=REG, client="가사"):
    return DbLine(i, date.fromisoformat(d), client, reg, item, "", qty, price, price, dia, length, lo, hi)


def job(e, d="", reg=REG, client="가사", on=CUT):
    return JobInput.from_raw({"main_msg": d, "spec_qty": e}, client, reg, on)


class FakeMessages:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        a = self.answers.pop(0) if self.answers else {"unit_price": 50000, "method": "유사품",
                                                       "basis": "x", "confidence": "mid"}
        stop = "end_turn"
        if isinstance(a, tuple):
            a, stop = a
        text = a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)
        usage = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0,
                                cache_read_input_tokens=20000)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=usage,
                               stop_reason=stop, _request_id="req_1")


def fake_ai(tmp_path, answers=(), limit=None, source="backtest"):
    client = SimpleNamespace(messages=FakeMessages(answers))
    return AiPricer(client=client, rules="RULES", source=source, daily_limit=limit,
                    log_path=tmp_path / "calls.jsonl")


def test_request_shape_and_quote(tmp_path):
    ai = fake_ai(tmp_path, [{"unit_price": 57349, "method": "유사품", "basis": "Ø50 유사  2건",
                             "confidence": "mid"}])
    q = ai.quote(job("Ø50+0.0~+0.05 / 2개"), LineIndex.build([]), CUT, tag="t")
    assert (q.method, q.unit_price, q.confidence, q.basis) == ("AI-유사품", 57300, "mid", "Ø50 유사 2건")
    kw = ai.client.messages.calls[0]
    assert kw["model"] == "claude-sonnet-5-5" and kw["max_tokens"] == 300
    assert "temperature" not in kw  # Sonnet 5.5는 기본값 아닌 temperature 거절
    assert kw["thinking"] == {"type": "between_tools"}
    assert kw["system"] == [{"type": "text", "text": "RULES", "cache_control": {"type": "ephemeral"}}]
    assert kw["output_config"]["format"]["schema"]["required"] == ["unit_price", "method", "basis", "confidence"]
    rec = json.loads((tmp_path / "calls.jsonl").read_text(encoding="utf-8"))
    assert rec["cost_usd"] == pytest.approx(call_cost(SimpleNamespace(
        input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=20000)))
    assert rec["answer"]["unit_price"] == 57300 and rec["source"] == "backtest"


def test_call_cost():
    u = SimpleNamespace(input_tokens=1_000_000, output_tokens=1_000_000, cache_creation_input_tokens=1_000_000,
                        cache_read_input_tokens=1_000_000)
    assert call_cost(u) == pytest.approx(2.0 + 10.0 + 2.5 + 0.1)


@pytest.mark.parametrize("ans,stop,msg", [("not json", "end_turn", "JSON"),
                                          ({"unit_price": 0, "method": "유사품", "basis": "", "confidence": "low"},
                                           "end_turn", "단가 이상"),
                                          ('{"unit_price": 5', "max_tokens", "잘림"),
                                          ("", "refusal", "거절")])
def test_bad_answers_raise_and_log(tmp_path, ans, stop, msg):
    ai = fake_ai(tmp_path, [(ans, stop)])
    with pytest.raises(AiError, match=msg):
        ai.quote(job("Ø50 / 1개"), LineIndex.build([]), CUT)
    assert "error" in json.loads((tmp_path / "calls.jsonl").read_text(encoding="utf-8"))


def test_parse_answer_unknown_method_falls_back():
    d = parse_answer('{"unit_price": 12345, "method": "기타", "basis": "a\\nb", "confidence": "high"}')
    assert (d["unit_price"], d["method"], d["basis"]) == (12300, "규칙계산", "a b")


def test_daily_limit_counts_ops_only(tmp_path):
    log = tmp_path / "calls.jsonl"
    today = date.today().isoformat()
    log.write_text("\n".join(json.dumps({"ts": f"{today}T09:00:00", "source": s}) for s in
                             ["ops", "ops", "backtest"]) + "\n", encoding="utf-8")
    assert calls_today("ops", log) == 2
    ai = AiPricer(client=SimpleNamespace(messages=FakeMessages([])), rules="R", source="ops", daily_limit=3,
                  log_path=log)
    ai.quote(job("Ø50 / 1개"), LineIndex.build([]), CUT)  # 3건째 OK
    with pytest.raises(AiError, match="일일 상한 3건"):
        ai.quote(job("Ø50 / 1개"), LineIndex.build([]), CUT)
    assert len(ai.client.messages.calls) == 1


def test_user_message_cases_cutoff_exclude_and_hint():
    lines = [ln(1, "2026-01-05", 40000, dia=50.1), ln(2, "2026-05-30", 44000),          # 2 = 정답 줄
             ln(3, "2026-07-01", 99999),                                                  # cutoff 이후
             ln(4, "2025-03-01", 30000, dia=120),                                         # 같은 거래처, 구경 다름
             ln(5, "2026-02-01", 61000, dia=50.5, reg="999-99-99999", client="나사")]     # 타 거래처
    msg = build_user_message(job("Ø50+0.0~+0.05 / 10개"), LineIndex.build(lines), CUT, exclude_ids={2},
                             hint="직전 동일품 1개 410,000 / 이번 10개")
    same = msg.split("# 참고: 같은 거래처")[1].split("# 참고: 다른 거래처")[0]
    other = msg.split("# 참고: 다른 거래처")[1].split("# 거래처 인상 성향")[0]
    assert same.index("※ 직전 동일품 1개 410,000 / 이번 10개") < same.index("40,000")
    assert "30,000" in same  # 구경 유사가 모자라면 같은 거래처 최근 건으로 채움
    assert "44,000" not in msg and "99,999" not in msg
    assert "나사" in other and "61,000" in other and "나사" not in same
    assert "수량 10개" in msg and "기장 없음" in msg


def test_client_tendency_raise_pattern():
    lines = [ln(i, f"{y}-03-01", p, dia=d) for i, (y, p, d) in enumerate(
        [(2023, 100, 30), (2024, 110, 30), (2023, 200, 40), (2024, 220, 40), (2024, 300, 60), (2025, 330, 60)])]
    got = pricer_ai.client_tendency(job("Ø50 / 1개"), LineIndex.build(lines), CUT)
    assert got.startswith("인상형") and "인상 3쌍" in got
    assert pricer_ai.client_tendency(job("Ø50 / 1개"), LineIndex.build([]), CUT).startswith("같은 품목")


# ---------------------------------------------------------------- 백테스트 --ai
def test_run_ai_scores_samples_pending_only(tmp_path, capsys):
    rows = [{"match": "exact", "date": "2026-06-01", "client": "가사", "reg_no": REG, "tab": "6월", "row": str(i),
             "raw_main_msg": "", "raw_spec_qty": e, "line_id": lid, "actual_unit_price": "50000",
             "actual_qty": "1"}
            for i, (e, lid) in enumerate([("Ø50+0.0~+0.05 / 1개", "9"),     # 동일품(사다리)
                                         ("Ø80+0.0~+0.05 / 1개", "10"),    # 미결 → AI
                                         ("Ø90+0.0~+0.05 / 1개", "11")])]  # 미결 → AI(실패)
    idx = LineIndex.build([ln(1, "2026-01-01", 50000), ln(9, "2026-06-01", 50000), ln(10, "2026-06-01", 50000, dia=80),
                           ln(11, "2026-06-01", 50000, dia=90)])
    ai = fake_ai(tmp_path, [{"unit_price": 50000, "method": "유사품", "basis": "b", "confidence": "mid"},
                            "garbage"])
    res = backtest.run_ai_scores(rows, idx, limit=5, seed=0, ai=ai, workers=1, out_path=tmp_path / "r.csv")
    assert sorted(r["method"] for r in res) == ["AI-유사품", "AI실패"]
    assert (tmp_path / "r.csv").exists()
    out = capsys.readouterr().out
    assert "비용: 총 $" in out and "AI-유사품" in out
    for kw in ai.client.messages.calls:  # 정답 줄은 참고 사례에 없음
        assert "2026-06-01" not in kw["messages"][0]["content"].split("# 참고")[1]


# ---------------------------------------------------------------- 운영 (pricer.quote_job_row)
class StubAi:
    def __init__(self, conf="mid", err=None):
        self.conf, self.err, self.calls = conf, err, 0

    def quote(self, inp, index, cutoff, hint="", tag=""):
        self.calls += 1
        if self.err:
            raise AiError(self.err)
        from pricer_job import Quote
        return Quote("AI-유사품", 61000, self.conf, "근거")


def mk_pricer(ai, dry_run=False):
    p = pricer.Pricer({}, None, dry_run=dry_run)
    p._ai = ai
    return p


@pytest.mark.parametrize("conf,status", [("high", pricer.ST_QUOTED), ("mid", pricer.ST_QUOTED),
                                         ("low", pricer.ST_CHECK)])
def test_ops_ai_confidence_to_status(conf, status):
    price, basis, st = mk_pricer(StubAi(conf)).quote_job_row(job("Ø80 / 1개"), LineIndex.build([]), CUT)
    assert (price, st) == (61000, status)
    assert basis.startswith(pricer.LOW_TAG) is (conf == "low")


def test_ops_ladder_first_and_ai_failure():
    ai = StubAi()
    price, basis, st = mk_pricer(ai).quote_job_row(job("Ø50+0.0~+0.05 / 1개"), LineIndex.build(
        [ln(1, "2026-01-01", 45000)]), CUT)
    assert (price, st, ai.calls) == (45000, pricer.ST_QUOTED, 0)
    price, basis, st = mk_pricer(StubAi(err="AI 일일 상한 300건 도달")).quote_job_row(
        job("Ø80 / 1개"), LineIndex.build([]), CUT)
    assert (price, st) == ("", pricer.ST_CHECK) and "일일 상한" in basis


def test_ops_preset_low_and_multi():
    hist = [ln(1, "2025-01-01", 10000, dia=10)]
    price, basis, st = mk_pricer(StubAi()).quote_job_row(job("Ø50 / 1개", d="20"), LineIndex.build(hist), CUT)
    assert (price, st) == (200000, pricer.ST_CHECK) and basis.startswith("[가견적-확인필요] 사전단가")
    price, _, st = mk_pricer(StubAi()).quote_job_row(job("Ø35 / 1개 + Ø40 / 1개", d="4/2.5"),
                                                     LineIndex.build([]), CUT)
    assert (price, st) == ("40000/25000", pricer.ST_QUOTED)


def test_ops_dry_run_skips_ai():
    ai = StubAi()
    _, basis, st = mk_pricer(ai, dry_run=True).quote_job_row(job("Ø80 / 1개"), LineIndex.build([]), CUT)
    assert (ai.calls, st) == (0, pricer.ST_CHECK) and "dry-run" in basis
