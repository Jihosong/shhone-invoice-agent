"""동일품 기간 정책(6/12/24개월·인상 제안·인상형) + 거래처 성향(clients trend) + 파서 보정(OD·'+'로 이어진 Ø)."""
from datetime import date

import pytest

import clients_cli
import inflation
import pricer_ai
from cases import DbLine, LineIndex
from job_parse import item_segments, parse_dias, parse_tol
from pricer_job import M_PENDING, M_SAME, JobInput, item_count, quote_ladder, raised_price, split_items

REG = "111-11-11111"
CUT = date(2026, 6, 1)


def ln(i, d, price, dia=50.0, length=None, qty=1, lo=0.0, hi=0.05, item="시린더호닝", reg=REG, client="가사"):
    return DbLine(i, date.fromisoformat(d), client, reg, item, "", qty, price, price, dia, length, lo, hi)


def job(e, d="", reg=REG, client="가사"):
    return JobInput.from_raw({"main_msg": d, "spec_qty": e}, client, reg, CUT)


def q(inp, lines):
    return quote_ladder(inp, LineIndex.build(lines), CUT)


def raise_history(rates=(0.10, 0.10, 0.10)):
    """다른 품목 3개가 해마다 재주문 → 최근 3쌍 변화율 = rates."""
    out, i = [], 100
    for k, r in enumerate(rates):
        dia, base = 70 + 10 * k, 100000 * (k + 1)
        out += [ln(i, f"{2023 + k}-03-01", base, dia=dia), ln(i + 1, f"{2024 + k}-03-01", round(base * (1 + r)), dia=dia)]
        i += 2
    return out


# ---------------------------------------------------------------- 기간 정책
@pytest.mark.parametrize("d,months,method,price,conf,basis", [
    ("2026-01-01", 5, M_SAME, 40000, "high", "2026-01-01 40,000원 Ø50 1개"),
    ("2025-09-01", 9, M_SAME, 40000, "mid", "직전 2025-09, 동결 유지 — 확인 / 2025-09-01 40,000원"),
    ("2025-04-01", 14, M_SAME, 42000, "mid", "직전 40,000 (2025-04) → +5% 인상 제안 / 2025-04-01 40,000원"),
    ("2023-12-01", 30, M_PENDING, None, None, "동일품 미확정 (직전 동일품 단가 40,000원 (2023-12-01, 1개)"),
])
def test_period_policy(d, months, method, price, conf, basis):
    got = q(job("Ø50+0.0~+0.05 / 1개"), [ln(1, d, 40000)])
    assert (got.method, got.unit_price, got.confidence) == (method, price, conf)
    assert got.basis.startswith(basis)
    if method == M_PENDING:
        assert got.max_conf == "mid" and "30개월 전 — 24개월 초과, 최소 +5% 이상 제안할 것" in got.hint


def test_freeze_confidence_switch(tmp_path, monkeypatch):
    import pricer_job

    import os

    cfg = tmp_path / "sheets.yaml"
    for k, (value, want) in enumerate([("high", "high"), ("mid", "mid"), ("low", "mid")], 1):  # 그 외 값 → mid
        cfg.write_text(f"pricing:\n  same_item_6_12m_confidence: {value}\n", encoding="utf-8")
        os.utime(cfg, (k, k))   # 파일이 바뀌면(mtime) 다시 읽음
        assert pricer_job.freeze_conf(cfg) == want
    cfg.write_text("common: {}\n", encoding="utf-8")
    os.utime(cfg, (99, 99))
    assert pricer_job.freeze_conf(cfg) == "mid"                                # 설정 없음 → mid
    assert pricer_job.freeze_conf(tmp_path / "없음.yaml") == "mid"
    # high로 바꾸면 견적완료 대상(high), 근거에 '확인' 없음
    monkeypatch.setattr(pricer_job, "FREEZE_CONF", "high")
    got = q(job("Ø50+0.0~+0.05 / 1개"), [ln(1, "2025-09-01", 40000)])
    assert (got.confidence, got.unit_price) == ("high", 40000)
    assert got.basis.startswith("직전 2025-09, 동결 유지 / ")


def test_repo_config_default_is_mid():
    import pricer_job

    assert pricer_job.freeze_conf() == "mid"


def test_raise_rounds_down_to_100():
    assert raised_price(38500, 0.05) == 40400          # 40,425 → 40,400
    assert raised_price(40000, 0.05) == 42000          # 부동소수 오차로 41,900이 되지 않음
    got = q(job("Ø50+0.0~+0.05 / 1개"), [ln(1, "2025-04-01", 38500)])
    assert (got.unit_price, got.confidence) == (40400, "mid")
    assert got.basis.startswith("직전 38,500 (2025-04) → +5% 인상 제안")


def test_raise_type_client_uses_mean_rate_14_months():
    hist = raise_history((0.10, 0.08, 0.12)) + [ln(1, "2025-04-01", 38500)]
    got = q(job("Ø50+0.0~+0.05 / 1개"), hist)
    assert (got.method, got.unit_price, got.confidence) == (M_SAME, 42350 // 100 * 100, "mid")   # ×1.10
    assert got.basis.startswith("직전 38,500 (2025-04) → +10% 인상 제안 (인상형: 최근 3쌍 평균 +10.0%)")


def test_raise_type_capped_15_and_freeze_type_gets_5():
    got = q(job("Ø50+0.0~+0.05 / 1개"), raise_history((0.30, 0.30, 0.30)) + [ln(1, "2025-04-01", 40000)])
    assert got.unit_price == 46000 and "+15% 인상 제안 (인상형: 최근 3쌍 평균 +30.0%, 상한 15%)" in got.basis
    # 3쌍 중 2쌍 인상이어도 평균이 5% 이하면 동결형 → 기본 5%
    got = q(job("Ø50+0.0~+0.05 / 1개"), raise_history((0.03, 0.04, 0.0)) + [ln(1, "2025-04-01", 40000)])
    assert got.unit_price == 42000 and got.basis.startswith("직전 40,000 (2025-04) → +5% 인상 제안 /")


def test_raise_trend_ignores_future_and_other_reg():
    hist = [ln(i, d, p, dia=30, reg=r) for i, (d, p, r) in enumerate(
        [("2024-03-01", 100000, REG), ("2025-03-01", 200000, "999-99-99999"),   # 동명 다른 번호 → 제외
         ("2026-07-01", 300000, REG)], 100)] + [ln(1, "2025-04-01", 40000)]       # cutoff 이후 → 제외
    assert q(job("Ø50+0.0~+0.05 / 1개"), hist).unit_price == 42000


def test_loose_keyword_match_capped_mid():
    # 명세표엔 '용접', 시트엔 없음 → 동일품이되 최대 mid (대진엔지니어링 2025-09 사례)
    line = ln(1, "2026-03-01", 40000, dia=180, length=300, qty=20, item="시린더 호닝(파이프 용접된것)")
    got = q(job("Ø180 +0~ +0.05 / 300 / 20개"), [line])
    assert (got.method, got.unit_price, got.confidence) == (M_SAME, 40000, "mid")
    assert "명세표에만 '용접' (시트엔 없음) → 최대 mid" in got.basis
    # 키워드가 똑같은 후보가 있으면 그것이 먼저 (더 오래됐어도)
    plain = ln(2, "2026-01-01", 38000, dia=180, length=300, qty=20)
    got = q(job("Ø180 +0~ +0.05 / 300 / 20개"), [line, plain])
    assert (got.unit_price, got.confidence) == (38000, "high")
    # 반대(시트에 '막힘', 명세표엔 없음)는 다른 품목
    assert q(job("막힘 Ø180 +0~ +0.05 / 300 / 20개"), [plain]).method == M_PENDING


def test_client_lines_excludes_homonym_with_other_reg():
    idx = LineIndex.build([ln(1, "2025-01-01", 1, reg=REG), ln(2, "2025-01-01", 2, reg="801-02-02915"),
                           ln(3, "2025-01-01", 3, reg="")])
    assert {x.id for x in idx.client_lines(REG, "가사")} == {1, 3}
    assert {x.id for x in idx.client_lines("", "가사")} == {1, 2, 3}


def test_ai_prompt_stale_hint_and_tendency_definition():
    hist = raise_history((0.03, 0.04, 0.0)) + [ln(1, "2023-12-01", 40000)]
    quote = q(job("Ø50+0.0~+0.05 / 1개"), hist)
    msg = pricer_ai.build_user_message(job("Ø50+0.0~+0.05 / 1개"), LineIndex.build(hist), CUT, hint=quote.hint)
    assert "※ 직전 동일품 단가 40,000원 (2023-12-01, 1개)" in msg and "최소 +5% 이상 제안할 것" in msg
    assert "동결형 — 최근 3쌍 평균 +2.3% (인상 2쌍)" in msg


# ---------------------------------------------------------------- clients trend
def test_trend_report_pairs_and_verdict():
    out = clients_cli.trend_report(raise_history((0.10, 0.08, 0.12)), "가사", REG, "대표명 일치 홍길동")
    text = "\n".join(out)
    assert out[0].startswith("== 가사 (111-11-11111, 대표명 일치 홍길동) 임가공 6줄 2023-03-01~2026-03-01")
    assert "재주문 쌍 3개" in out[1]
    assert "2023-03-01   100,000(1건) → 2024-03-01   110,000(1건)  +10.0%" in text
    assert "판정: 인상형 — 최근 3쌍(*) 평균 (+10.0%, +8.0%, +12.0%) = +10.0% > 5%" in text
    assert out[-1] == "12~24개월 동일품 인상률: +10% (인상형: 최근 3쌍 평균 +10.0%)"
    empty = clients_cli.trend_report([], "가사", REG)
    assert empty[-1].startswith("판정: 모름")


def test_trend_cli_pick_client_by_reg_and_ceo(tmp_path, monkeypatch):
    import sqlite3

    import watcher

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE clients(name TEXT, reg_no TEXT, n_statements INTEGER, last_date TEXT, ceo_name TEXT)")
    con.executemany("INSERT INTO clients VALUES (?,?,?,?,?)", [
        ("대진엔지니어링", "117-03-32271", 5, "2026-03-11", "김영주"),
        ("대진엔지니어링", "801-02-02915", 5, "2026-10-21", "이현진")])
    monkeypatch.setattr(watcher, "ALIASES_YAML", tmp_path / "none.yaml")
    assert clients_cli.pick_client(con, "대진엔지니어링(김영주)")[0]["reg_no"] == "117-03-32271"
    assert clients_cli.pick_client(con, "801-02-02915")[0]["reg_no"] == "801-02-02915"
    hit, rows = clients_cli.pick_client(con, "대진엔지니어링")
    assert hit is None and len(rows) == 2


# ---------------------------------------------------------------- 파서: OD/외경
@pytest.mark.parametrize("text,dias,tol", [
    ("Ra0.1 (OD Ø130) Ø31.75 +0~+0.05 / 370 / 6개", [31.75], (0.0, 0.05)),
    ("외경Ø80 내경Ø50 +0~+0.02 / 1개", [50.0], (0.0, 0.02)),
    ("O.D Ø60 Ø25.4 -0.05~+0.05", [25.4], (-0.05, 0.05)),
    ("OD Ø130 / 1개", [], (None, None)),            # 외경만 → 구경 없음 (맨 숫자로도 안 잡음)
    ("Ø130 GOOD Ø50", [130.0, 50.0], (None, None)),  # 'GOOD'의 OD는 표시 아님
])
def test_od_diameter_not_bore(text, dias, tol):
    assert parse_dias(text) == dias
    assert parse_tol(text) == tol


def test_od_rows_split_on_bore():
    e = "Ra0.1 (OD Ø130) Ø31.75 +0~+0.05 / 370 / 6개 + (OD Ø60) Ø31.75 +0~+0.05 / 370 / 6개"
    assert item_segments(e) == [("Ra0.1 (OD Ø130)", "Ø31.75 +0~+0.05 / 370 / 6개"),
                                ("Ra0.1 (OD Ø60)", "Ø31.75 +0~+0.05 / 370 / 6개")]
    assert [s.dia for s in split_items(job(e))] == [31.75, 31.75]


# ---------------------------------------------------------------- 파서: '+'로 이어진 Ø
def test_plus_joined_dias_share_following_qty_with_warning():
    from loguru import logger

    msgs = []
    hid = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        segs = item_segments("Ø67 / 1개 + Ø45 + Ø25 / 3개")
    finally:
        logger.remove(hid)
    assert segs == [("", "Ø67 / 1개"), ("", "Ø45 / 3개"), ("", "Ø25 / 3개")]
    assert any("품목 2개에 수량 '3개' 하나" in m for m in msgs)
    assert [(s.dia, s.qty) for s in split_items(job("Ø67 / 1개 + Ø45 + Ø25 / 3개"))] == [
        (67.0, 1), (45.0, 3), (25.0, 3)]
    assert item_count({"spec_qty": "Ø67 / 1개 + Ø45 + Ø25 / 3개"}) == 3


def test_plus_joined_dias_qty_in_order_and_each():
    assert item_segments("Ø45 + Ø25 / 2개 3개") == [("", "Ø45 / 2개"), ("", "Ø25 / 3개")]
    from loguru import logger

    msgs = []
    hid = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    try:
        segs = item_segments("중요 Ø76.2 +0.0~+0.05 + Ø87.3+0.0~0.03 / 각10개")
    finally:
        logger.remove(hid)
    assert [s for _, s in segs] == ["Ø76.2 +0.0~+0.05 / 10개", "Ø87.3+0.0~0.03 / 각10개"]
    assert not msgs   # '각'이면 같은 수량이 맞으니 경고 없음


@pytest.mark.parametrize("text,n", [
    ("Ø40 -0.005~+0.005 / 1Hole + Ø28 +0.01~+0.03 / 8 Hole", 1),   # 한 부품의 구멍들
    ("양쪽호닝 Ø250+0.08 Ø300+0.08 / 1개", 1),                        # '+' 없이 이어진 Ø = 한 품목
    ("Ø150-0.02~-0.03 / Ø72-0.02~-0.03 / 10개", 1),
    ("Ø72.5 (Ø73)(No36) / 1개", 1),
    ("Ø50+0.0~+0.05 / 300 / 2개", 1),                                  # 공차 '+'
])
def test_not_split(text, n):
    assert max(1, len(item_segments(text))) == n
