"""운영자 지시문 (D 주요 메시지·S 물품 세부 정보) — 사전단가·동일품보다 먼저, 결정적 패턴은 코드가 계산 (지시계산 high),
그 밖은 AI 프롬프트 맨 위 '※ 운영자 지시(최우선)' + 결과 최대 mid. 지시 행 산출근거는 '[지시]'."""
import json
from datetime import date
from types import SimpleNamespace

import pytest

import pricer
import pricer_job
from cases import DbLine, LineIndex
from pricer_ai import AiPricer, build_user_message
from pricer_job import M_DIRECTIVE, M_PENDING, M_PRESET, M_SAME, JobInput, directive_text, quote_ladder

REG = "111-11-11111"
CUT = date(2026, 11, 28)
E160 = "Ø160+0.0~+0.05/2380/2개"


def ln(i, d, price, dia=50.0, length=None, qty=2, item="시린더호닝"):
    return DbLine(i, date.fromisoformat(d), "가사", REG, item, "", qty, price, price, dia, length, 0.0, 0.05)


def job(e, d="", s="", on=CUT):
    return JobInput.from_raw({"main_msg": d, "spec_qty": e, "pre_note": s}, "가사", REG, on)


def ladder(inp, lines=()):
    return quote_ladder(inp, LineIndex.build(list(lines)), CUT)


# ---------------------------------------------------------------- 감지
@pytest.mark.parametrize("d,s,hit", [
    ("센티스퀘어당 20원으로 계산해줘", "", True),
    ("cm2/18", "", True),                    # 시트에 실제로 쓰는 표기
    ("", "개당 45,000원으로 해줘", True),     # S 칸
    ("임가공이지 소재 아님", "", True),
    ("10% 올려", "", True),
    ("1000원인상", "", True),
    ("8", "", False),                         # 사전단가 숫자
    ("-0.5", "", False),                      # 연마량
    ("지난번 1개 불량 처리", "", False),       # '지난번'만으로는 지시 아님
    ("택배 상아테크에 문의후 주소학인하고보낼것", "", False),  # '학인하고'의 '인하'
    ("", "", False),
])
def test_directive_detection(d, s, hit):
    assert bool(directive_text({"main_msg": d, "pre_note": s})) == hit


# ---------------------------------------------------------------- a. cm²당 N원
def test_area_rate_cm2():
    q = ladder(job(E160, d="센티스퀘어당 20원으로 계산해줘"))
    # π × 16cm × 238cm × 20원 = 239,263.7 → 1,000원 버림 239,000
    assert (q.method, q.unit_price, q.confidence) == (M_DIRECTIVE, 239_000, "high")
    assert "= 239,264 → 239,000 (1,000원 버림)" in q.basis
    assert q.label().startswith("[지시] 지시계산 | cm²당 20원: π×16cm×238cm×20원")


@pytest.mark.parametrize("d", ["cm²당 20원", "cm2당 20원", "㎠당 20원", "cm2/20", "cm2/20원"])
def test_area_rate_spellings(d):
    assert ladder(job(E160, d=d)).unit_price == 239_000


def test_area_rate_multi_items_each():
    segs = pricer_job.split_items(job("Ø160+0.0~+0.05/2380/2개 + Ø100+0.0~+0.05/1000/1개", d="cm²당 20원"))
    assert [ladder(s).unit_price for s in segs] == [239_000, 62_000]   # π×10×100×20 = 62,831 → 62,000


def test_area_rate_without_length_needs_check():
    q = ladder(job("Ø160+0.0~+0.05/2개", d="cm²당 20원"))
    assert q.method == M_DIRECTIVE and q.unit_price is None
    assert q.label().startswith("[지시: cm²당 20원 — 기장 없음]")
    price, basis, st, _, _ = pricer.Pricer({}, None).quote_job_row(job("Ø160+0.0~+0.05/2개", d="cm²당 20원"),
                                                                  LineIndex.build([]), CUT)
    assert (price, st) == ("", pricer.ST_CHECK) and basis.startswith("[지시: cm²당 20원 — 기장 없음]")


# ---------------------------------------------------------------- b. 개당 N원 / N원으로
@pytest.mark.parametrize("d,s,price", [
    ("개당 45,300원", "", 45_300), ("", "45000원으로 해줘", 45_000),   # 개당 N원은 그대로 (버림 없음) ("5만원으로 해줘", "", 50_000),
    ("원통연마개당3만원이라함", "", 30_000)])
def test_per_piece(d, s, price):
    q = ladder(job("Ø50/300/2개", d=d, s=s))
    assert (q.method, q.unit_price, q.confidence) == (M_DIRECTIVE, price, "high")


# ---------------------------------------------------------------- c. 직전 ±N% / d. 지난번 그대로
def test_prev_plus_10pct():
    hist = [ln(1, "2026-09-01", 38_500, length=300), ln(2, "2025-01-01", 30_000, length=300)]
    q = ladder(job("Ø50+0.0~+0.05/300/2개", d="직전 단가 +10%"), hist)
    assert (q.method, q.unit_price, q.confidence) == (M_DIRECTIVE, 42_000, "high")  # 38,500×1.1=42,350 → 42,000
    assert "직전 38,500원 (2026-09-01)" in q.basis


@pytest.mark.parametrize("d,price", [("10% 올려", 42_000), ("5% 내려", 36_000), ("지난번 그대로", 38_500),
                                     ("이전 단가로 해줘", 38_500)])
def test_prev_variants(d, price):
    q = ladder(job("Ø50+0.0~+0.05/300/2개", d=d), [ln(1, "2026-09-01", 38_500, length=300)])
    assert (q.method, q.unit_price) == (M_DIRECTIVE, price)


def test_prev_missing_needs_check():
    q = ladder(job("Ø50/300/2개", d="10% 올려"), [ln(1, "2026-09-01", 38_500, dia=80.0)])
    assert q.unit_price is None and q.tag == "[지시: 직전 단가 +10% — 직전 동일품 없음]"


# ---------------------------------------------------------------- 지시가 있으면 지름길을 건너뜀
def test_directive_skips_same_item():
    hist = [ln(1, "2026-11-01", 900_000, dia=160.0, length=2380)]
    assert ladder(job(E160), hist).method == M_SAME                       # 지시 없음 → 동일품
    q = ladder(job(E160, d="cm²당 20원으로 계산해줘"), hist)
    assert (q.method, q.unit_price) == (M_DIRECTIVE, 239_000)


def test_directive_in_s_skips_preset():
    assert ladder(job("Ø50/2개", d="8")).method == M_PRESET
    q = ladder(job("Ø50/2개", d="8", s="임가공이지 소재값 무시"))
    assert q.method == M_PENDING and q.directive.startswith("S: 임가공이지") and q.tag == "[지시]"


# ---------------------------------------------------------------- 자유 지시 → AI (최대 mid)
class FakeMessages:
    def __init__(self, conf="high"):
        self.calls, self.conf = [], conf

    def create(self, **kw):
        self.calls.append(kw)
        text = json.dumps({"unit_price": 61000, "method": "규칙계산", "basis": "소재 제외 임가공비",
                           "confidence": self.conf}, ensure_ascii=False)
        usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=usage, stop_reason="end_turn")


def ai_pricer(tmp_path):
    return AiPricer(client=SimpleNamespace(messages=FakeMessages()), rules="R", source="backtest", daily_limit=None,
                    log_path=tmp_path / "calls.jsonl")


def test_free_directive_goes_to_ai_mid(tmp_path, monkeypatch):
    monkeypatch.setattr(pricer_job, "DIRECTIVE_AI_HIGH", None)
    monkeypatch.setattr(pricer_job, "_pricing_cfg", lambda path=None: {})
    p = pricer.Pricer({}, None)
    p._ai = ai_pricer(tmp_path)
    price, basis, st, method, conf = p.quote_job_row(job("Ø50/2개", d="임가공이지 소재 아님"), LineIndex.build([]), CUT)
    assert (price, st, method, conf) == (61000, pricer.ST_CHECK, "AI-규칙계산", "mid")
    assert basis.startswith("[가견적-mid] [지시] AI-규칙계산 | [지시 반영] 소재 제외 임가공비")
    msg = p._ai.client.messages.calls[0]["messages"][0]["content"]
    assert msg.startswith("※ 운영자 지시(최우선): D: 임가공이지 소재 아님")


def test_free_directive_high_allowed_by_config(tmp_path, monkeypatch):
    monkeypatch.setattr(pricer_job, "DIRECTIVE_AI_HIGH", None)
    monkeypatch.setattr(pricer_job, "_pricing_cfg", lambda path=None: {"directive_ai_high": True})
    p = pricer.Pricer({}, None)
    p._ai = ai_pricer(tmp_path)
    price, basis, st, _, conf = p.quote_job_row(job("Ø50/2개", d="임가공이지 소재 아님"), LineIndex.build([]), CUT)
    assert (st, conf) == (pricer.ST_QUOTED, "high") and basis.startswith("[지시] AI-규칙계산 | [지시 반영]")


def test_directive_row_quoted_high_in_ops():
    price, basis, st, method, conf = pricer.Pricer({}, None).quote_job_row(
        job(E160, d="센티스퀘어당 20원으로 계산해줘"), LineIndex.build([]), CUT)
    assert (price, st, method, conf) == (239_000, pricer.ST_QUOTED, M_DIRECTIVE, "high")
    assert basis.startswith("[지시] 지시계산")


def test_multi_items_all_directive_quoted():
    price, basis, st, _, conf = pricer.Pricer({}, None).quote_job_row(
        job("Ø160+0.0~+0.05/2380/2개 + Ø100+0.0~+0.05/1000/1개", d="cm²당 20원"), LineIndex.build([]), CUT)
    assert (price, st, conf) == ("239000/62000", pricer.ST_QUOTED, "high") and basis.startswith("[지시] 다품목 2구간")


# ---------------------------------------------------------------- 지시 없는 행은 기존 동작
def test_no_directive_unchanged(tmp_path):
    hist = [ln(1, "2026-09-01", 38_500, length=300)]
    q = ladder(job("Ø50+0.0~+0.05/300/2개"), hist)
    assert (q.method, q.unit_price, q.tag, q.directive) == (M_SAME, 38_500, "", "")
    assert not q.label().startswith("[지시")
    msg = build_user_message(job("Ø80/1개"), LineIndex.build([]), CUT)
    assert msg.startswith("# 견적 요청")
