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
        self.conf, self.err, self.calls, self.kw = conf, err, 0, {}

    def quote(self, inp, index, cutoff, hint="", tag="", max_conf=None):
        self.calls += 1
        self.kw = {"hint": hint, "max_conf": max_conf}
        if self.err:
            raise AiError(self.err)
        from pricer_job import Quote
        return Quote("AI-유사품", 61000, self.conf, "근거")


def mk_pricer(ai, dry_run=False, con=None):
    p = pricer.Pricer({}, con, dry_run=dry_run)
    p._ai = ai
    return p


@pytest.mark.parametrize("conf,status,tag", [("high", pricer.ST_QUOTED, ""),
                                             ("mid", pricer.ST_CHECK, "[가견적-mid] "),
                                             ("low", pricer.ST_CHECK, "[가견적-low] ")])
def test_ops_only_high_is_quoted(conf, status, tag):
    price, basis, st, method, c = mk_pricer(StubAi(conf)).quote_job_row(job("Ø80 / 1개"), LineIndex.build([]), CUT)
    assert (price, st, method, c) == (61000, status, "AI-유사품", conf)
    assert basis.startswith(tag + "AI-유사품 | 근거")


def test_ops_ladder_mid_is_gagyeon_too():
    price, basis, st, method, _ = mk_pricer(StubAi()).quote_job_row(
        job("Ø50+0.0~+0.05 / 1개"), LineIndex.build([ln(1, "2025-04-01", 45000)]), CUT)  # 14개월 전 → mid +5%
    assert (price, st, method) == (47200, pricer.ST_CHECK, "동일품")
    assert basis.startswith("[가견적-mid] 동일품 | 직전 45,000 (2025-04) → +5% 인상 제안")


def test_ops_stale_same_item_passes_hint_and_cap():
    ai = StubAi("high")
    mk_pricer(ai).quote_job_row(job("Ø50+0.0~+0.05 / 1개"), LineIndex.build([ln(1, "2023-01-01", 45000)]), CUT)
    assert ai.kw["max_conf"] == "mid" and "24개월 초과" in ai.kw["hint"]


def test_ops_ladder_first_and_ai_failure():
    ai = StubAi()
    price, _, st, _, _ = mk_pricer(ai).quote_job_row(job("Ø50+0.0~+0.05 / 1개"), LineIndex.build(
        [ln(1, "2026-05-01", 45000)]), CUT)
    assert (price, st, ai.calls) == (45000, pricer.ST_QUOTED, 0)
    price, basis, st, _, _ = mk_pricer(StubAi(err="AI 일일 상한 60건 도달")).quote_job_row(
        job("Ø80 / 1개"), LineIndex.build([]), CUT)
    assert (price, st) == ("", pricer.ST_CHECK) and "일일 상한" in basis


def test_ops_preset_low_and_multi():
    hist = [ln(1, "2025-01-01", 10000, dia=10)]
    price, basis, st, _, _ = mk_pricer(StubAi()).quote_job_row(job("Ø50 / 1개", d="20"), LineIndex.build(hist), CUT)
    assert (price, st) == (200000, pricer.ST_CHECK) and basis.startswith("[가견적-low] 사전단가")
    price, basis, st, _, _ = mk_pricer(StubAi()).quote_job_row(job("Ø35 / 1개 + Ø40 / 1개", d="4/2.5"),
                                                               LineIndex.build([]), CUT)
    assert (price, st) == ("40000/25000", pricer.ST_CHECK) and basis.startswith("[가견적-mid]")  # 과거 없음 → mid


def test_ops_dry_run_skips_ai():
    ai = StubAi()
    _, basis, st, _, _ = mk_pricer(ai, dry_run=True).quote_job_row(job("Ø80 / 1개"), LineIndex.build([]), CUT)
    assert (ai.calls, st) == (0, pricer.ST_CHECK) and "dry-run" in basis


@pytest.mark.parametrize("cell,norm", [("61,000", "61000"), (" 40,000 / 25,000 ", "40000/25000"), ("", ""),
                                       ("확인중", ""), ("6만", "")])
def test_norm_price_cell(cell, norm):
    assert pricer.norm_price_cell(cell) == norm


# ---------------------------------------------------------------- 운영 process_job: 사람 확인·수정 흐름
COLS = {"date": 1, "client": 2, "main_msg": 4, "spec_qty": 5, "difficulty": 6, "stock_removal": 8, "pre_note": 19,
        "post_size": 20, "post_note": 21, "author": 15, "status": 24, "unit_price": 25, "basis": 26, "row_id": 28}


class FakeWriter:
    last = None

    def __init__(self, cols, *a):
        self.writes = {}
        FakeWriter.last = self

    def add(self, rownum, name, value, row, row_id=None, expect_fp=None):
        self.writes[(rownum, name)] = value

    def flush(self, ws, dry_run):
        return len(self.writes)

    def __len__(self):
        return len(self.writes)


def sheet_row(e="Ø80 / 1개", d="", status="확정", price="", basis="", h=""):
    row = [""] * 28
    row[1], row[3], row[4], row[7], row[23], row[24], row[25] = "가사", d, e, h, status, price, basis
    return row


def snap_of(row, status, fp="fp1"):
    return {"label": "임가공", "ws": None, "rows": [{"rownum": 5, "row_id": "R1", "row": row, "fp": fp,
                                                     "status": status, "author": "", "legacy": False,
                                                     "client_resolved": "가사", "client_reg_no": REG}]}


@pytest.fixture
def job_env(monkeypatch):
    import sqlite3

    monkeypatch.setattr(pricer, "SheetWriter", FakeWriter)
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE rows (row_id TEXT, status TEXT)")
    con.execute("CREATE TABLE lines (id INTEGER, kind TEXT)")
    con.execute("INSERT INTO rows VALUES ('R1', '확정')")
    ai = StubAi("mid")
    p = mk_pricer(ai, con=con)
    monkeypatch.setattr(p, "job_index", lambda: LineIndex.build([]))
    scfg = {"columns": COLS, "data_start_row": 3, "hash_columns": ["client", "main_msg", "spec_qty"]}
    return p, ai, scfg, con


def test_process_job_flow_gagyeon_then_human_clears_status(job_env, caplog):
    p, ai, scfg, con = job_env
    p.process_job("imgagong", scfg, snap_of(sheet_row(), "확정"))
    w = FakeWriter.last.writes
    assert (w[(5, "unit_price")], w[(5, "status")]) == (61000, pricer.ST_CHECK)
    assert w[(5, "basis")].startswith("[가견적-mid] AI-유사품")
    assert con.execute("SELECT price, fp, confidence FROM job_quotes").fetchone() == ("61000", "fp1", "mid")
    # 다음 바퀴: 견적확인필요 그대로·단가 그대로 → 아무것도 안 함 (AI도 안 부름)
    p.process_job("imgagong", scfg, snap_of(sheet_row(status="견적확인필요", price="61,000",
                                                      basis=w[(5, "basis")]), "견적확인필요"))
    assert FakeWriter.last.writes == {} and ai.calls == 1
    # 사람이 상태 칸을 비움 → watcher가 확정으로(지문 같음) → 적힌 단가로 견적완료(사람확인), AI 안 부름
    p.process_job("imgagong", scfg, snap_of(sheet_row(price="61,000", basis=w[(5, "basis")]), "확정"))
    w2 = FakeWriter.last.writes
    assert w2[(5, "status")] == pricer.ST_QUOTED_OK and ai.calls == 1
    assert w2[(5, "basis")].startswith("사람 확인 | AI-유사품")


def test_process_job_human_edits_price(job_env):
    p, ai, scfg, con = job_env
    p.process_job("imgagong", scfg, snap_of(sheet_row(), "확정"))
    basis = FakeWriter.last.writes[(5, "basis")]
    p.process_job("imgagong", scfg, snap_of(sheet_row(status="견적확인필요", price="55,000", basis=basis),
                                            "견적확인필요"))
    w = FakeWriter.last.writes
    assert w[(5, "status")] == pricer.ST_QUOTED_EDIT and (5, "unit_price") not in w  # 사람 값 그대로 둠
    assert w[(5, "basis")].startswith("사람 수정 55,000 (에이전트 61000) | AI-유사품")
    assert con.execute("SELECT price FROM job_quotes").fetchone() == ("55000",)


def test_process_job_content_change_requotes(job_env):
    p, ai, scfg, con = job_env
    p.process_job("imgagong", scfg, snap_of(sheet_row(), "확정"))
    basis = FakeWriter.last.writes[(5, "basis")]
    # 내용이 바뀌어 확정으로 돌아옴(지문 다름) → 다시 견적
    p.process_job("imgagong", scfg, snap_of(sheet_row(e="Ø90 / 1개", price="61000", basis=basis), "확정", fp="fp2"))
    assert ai.calls == 2 and FakeWriter.last.writes[(5, "status")] == pricer.ST_CHECK


def test_process_job_logs_each_item(job_env):
    from loguru import logger

    p, ai, scfg, con = job_env
    msgs = []
    hid = logger.add(lambda m: msgs.append(str(m)), level="INFO")
    try:
        p.process_job("imgagong", scfg, snap_of(sheet_row(e="Ø80 / 1개", d="긴급", h="-0.3"), "확정"))
    finally:
        logger.remove(hid)
    line = next(m for m in msgs if "임가공 견적" in m)
    for part in ("거래처=가사", "main_msg='긴급'", "spec_qty='Ø80 / 1개'", "pre_size='-0.3'", "단가 61000",
                 "AI-유사품", "mid", "견적확인필요"):
        assert part in line


# ---------------------------------------------------------------- AI 상한·confidence 상한·프롬프트
def test_first_day_limit_60_then_300(tmp_path):
    log = tmp_path / "calls.jsonl"
    ai = AiPricer(client=SimpleNamespace(messages=FakeMessages([])), rules="R", source="ops", log_path=log)
    today = date.today()
    assert ai.limit_for(today) == 60  # 운영 호출 기록 없음 = 첫날
    log.write_text(json.dumps({"ts": "2026-01-01T09:00:00", "source": "ops"}) + "\n", encoding="utf-8")
    ai2 = AiPricer(client=SimpleNamespace(messages=FakeMessages([])), rules="R", source="ops", log_path=log)
    assert ai2.limit_for(today) == 300
    assert ai2.limit_for(date(2026, 1, 1)) == 60


def test_max_conf_caps_ai_result(tmp_path):
    ai = fake_ai(tmp_path, [{"unit_price": 52000, "method": "동일품", "basis": "직전 유지", "confidence": "high"}])
    q = ai.quote(job("Ø50 / 1개"), LineIndex.build([]), CUT, max_conf="mid")
    assert q.confidence == "mid" and "최대 mid" in q.basis


def test_prompt_lists_all_raw_fields_and_chidae():
    raw = {"main_msg": "-0.5", "spec_qty": "Ø77+0.0~+0.03 / 1개", "difficulty": "상", "pre_size": "-0.3",
           "pre_note": "흑피", "post_size": "77.02", "post_note": "OK"}
    msg = build_user_message(JobInput.from_raw(raw, "가사", REG, CUT), LineIndex.build([]), CUT)
    for lbl in ("D 주요 메시지: -0.5", "E 공차x갯수·조도", "F 난이도: 상", "H 연마량/치대: -0.3",
                "S 호닝 전 매니저 노트: 흑피", "T 호닝 후 치수: 77.02", "U 호닝 후 매니저 노트: OK"):
        assert lbl in msg
    assert "치대 0.3mm" in msg and "연마량(치대) 표기다. 단가가 아니다" in msg
    empty = build_user_message(job("Ø77 / 1개"), LineIndex.build([]), CUT)
    assert "F 난이도" not in empty and "치대 없음" in empty  # 빈 칸 생략


def test_prompt_hint_then_tendency_on_top():
    msg = build_user_message(job("Ø50 / 1개"), LineIndex.build([ln(1, "2026-01-01", 40000)]), CUT,
                             hint="직전 동일품 1개 500,000 / 이번 1개 (2023-11-02; 24개월 초과)")
    same = msg.split("# 참고: 같은 거래처")[1]
    assert same.index("※ 직전 동일품") < same.index("※ 이 거래처 인상 성향") < same.index("40,000")


def test_ai_preset_capped_at_mid(tmp_path):
    ai = fake_ai(tmp_path, [{"unit_price": 80000, "method": "사전단가", "basis": "D열 8", "confidence": "high"},
                            {"unit_price": 80000, "method": "사전단가", "basis": "D열 8", "confidence": "low"}])
    q = ai.quote(job("Ø50 / 1개", d="8개"), LineIndex.build([]), CUT)
    assert (q.method, q.confidence) == ("AI-사전단가", "mid") and "AI 사전단가 → 최대 mid" in q.basis
    assert ai.quote(job("Ø50 / 1개"), LineIndex.build([]), CUT).confidence == "low"  # 낮은 건 그대로
