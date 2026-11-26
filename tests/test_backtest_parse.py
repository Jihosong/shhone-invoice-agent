"""backtest 파서 단위 테스트 — 임가공 시트 E열(공차x갯수) 실제 표기에서 뽑은 케이스."""
from datetime import date

import pytest

from backtest import (header_map, parse_dias, parse_length, parse_materials, parse_qty, parse_ra,
                      parse_row, parse_sheet_date, parse_specials, parse_tol)


@pytest.mark.parametrize("text, dias", [
    ("Ø50+0.0~+0.05 / 305 / 45개", [50.0]),
    ("SUS Ø100+0.0~+0.035 / 120 / 1개 + Ø58+0.0~+0.046 / 280 /1개", [100.0, 58.0]),
    ("Φ220 +0.02~+0.03 / 625 / 2개", [220.0]),                       # Φ → Ø
    ("TG450N N180 Ø310 +0.03 / 1개", [310.0]),                        # 모델명 숫자 무시
    ("18파이+-0.02×315 / 100EA", [18.0]),                             # 숫자 뒤 '파이'
    ("스텐 100 +0.03~+0.05 / 1개", [100.0]),                          # Ø 없이 숫자+공차
    ("Ø11.005~Ø11.014 / 190 / 1개", [11.005, 11.014]),
    ("Ra 1.8 각도 30도 2개 + 각도 40도 2개", []),
    ("TID-200 각도15 Ra1.7 / 496개", []),
])
def test_parse_dias(text, dias):
    assert parse_dias(text) == dias


@pytest.mark.parametrize("text, tol", [
    ("Ø50+0.0~+0.05 / 305 / 45개", (0.0, 0.05)),
    ("막힘형 Ø30 0~ 0.1 / 165 / 10개", (0.0, 0.1)),                    # 부호 없음 = +
    ("Ø158.1-0.0~-0.02 / 6개", (-0.02, 0.0)),
    ("Ø127+-0.05 / 862 / 149개", (-0.05, 0.05)),
    ("Ø40 -0.005~+0.005 / 1Hole", (-0.005, 0.005)),
    ("Ø32+0.1 / 240 / 18개", (0.0, 0.1)),
    ("Ø84-0.2 ( Ø84) / 1개", (-0.2, 0.0)),
    ("Ø210+0.0~+0.07(+0.06) Ø180+0.0~+0.06(+0.06) / 2개", (0.0, 0.07)),  # 괄호 실측 무시, 첫 품목
    ("Ø88.722~88.773 / 10개 Ra 0.4 bawah", (0.0, 0.051)),              # 절대치수 범위
    ("Ø11.005~Ø11.014 / 190 / 1개", (0.0, 0.009)),
    ("Ø82.426 -0.025~+0.025, Ra0.1~0.3 / 13개", (-0.025, 0.025)),
    ("#120 Ø180 / 1개", (None, None)),
])
def test_parse_tol(text, tol):
    assert parse_tol(text) == tol


@pytest.mark.parametrize("text, length", [
    ("Ø50+0.0~+0.05 / 305 / 45개", 305.0),
    ("Ø300+0.0~+0.05 /1740/1개", 1740.0),
    ("Ø61+0.05/1000(250) / 1개", 1000.0),                           # 괄호 구간 무시
    ("SUS 면작업 Ø65 / 170L / 1개", 170.0),
    ("18파이+-0.02×315 / 100EA", 315.0),
    ("Ø52+0.03~+0.05/235~255 / 40개", 235.0),
    ("Ø100+0.0~+0.035 / 120 / 1개 + Ø58+0.0~+0.046 / 280 /1개", 120.0),  # 첫 품목만
    ("Ø80 +0~+0.04 / 1260L 1개 + 460L 1개", 1260.0),                 # 'L 1개'를 L1로 읽지 않음
    ("L300 Ø40 / 2개", None),                                         # 구경 앞 글자는 첫 품목 구간 밖
    ("Ø40 L300 / 2개", 300.0),
    ("Ø60+0.0~+0.05 / 60개", None),                                  # 수량만
    ("Ø150-0.02~-0.03 / Ø72-0.02~-0.03 / 8개", None),
])
def test_parse_length(text, length):
    assert parse_length(text) == length


@pytest.mark.parametrize("text, qty", [
    ("Ø50+0.0~+0.05 / 305 / 45개", 45),
    ("Ø55+0.0~+0.046/6개긴급 +8개", 14),
    ("TGX80M80 Ø153+0.02 /1개 TG80GL80Ø148.1+0.02/4개", 5),
    ("Ø70 +0.02~+0.03 / 4EA", 4),
    ("Ø40 -0.005~+0.005 / 1Hole + Ø28 +0.01~+0.03 / 8 Hole", 9),     # 개/EA 없으면 hole
    ("4hole Ø80-0.01~-0.03 / 4개", 4),                                # 개 우선
    ("하부Ø125+0.025~+0.035 중부Ø135+0.025~+0.035", None),
])
def test_parse_qty(text, qty):
    assert parse_qty(text) == qty


@pytest.mark.parametrize("text, mats", [
    ("SUS사각 Ø40 +0~+0.025 / 85 / 20개", ["SUS"]),
    ("스텐 100 +0.03~+0.05", ["SUS"]),
    ("FC Ø165.11 / 1개", ["FC"]),
    ("AL 블록 Ø20", ["AL"]),
    ("SCM440 열처리", ["SCM"]),
    ("티타늄 Ø30", ["티타늄"]),
    ("TID-200 각도15 Ra1.7", []),                                     # TI 뒤 영문 → 티타늄 아님
    ("PVC호닝 ALL", ["PVC"]),
    ("인코넬 Ø14.75~14.78 / 88 / 130개", ["인코넬"]),                                          # ALL ≠ AL
])
def test_parse_materials(text, mats):
    assert parse_materials(text) == mats


def test_parse_specials():
    assert parse_specials("2구멍 막힘 Ø55+0.0~+0.03 / 2개") == ["막힘", "다공"]
    assert parse_specials("SUS열처리된것 Ø102") == ["열처리"]
    assert parse_specials("용접 후 호닝 Ø60 얇은 T") == ["용접", "얇은T"]
    assert parse_specials("각도130, Ra0.8~1.0, Rz7 Ø95") == ["조도", "각도"]
    assert parse_specials("호닝전-0.4 연마량") == ["치대"]
    assert parse_specials("Ø50+0.0~+0.05 / 305 / 45개") == []


def test_parse_ra():
    assert parse_ra("각도30 조도 Ra0.8~1.0 / 274개") == 0.8
    assert parse_ra("Ra 1.7") == 1.7
    assert parse_ra("Ø50 / 2개") is None


@pytest.mark.parametrize("text, tab, expected", [
    ("2/13", 3, date(2026, 2, 13)),
    ("12/29", 1, date(2025, 12, 29)),   # 1월 탭의 12월 날짜 → 전년
    ("3.3", 3, date(2026, 3, 3)),
    ("11월 2일", 11, date(2026, 11, 2)),
    ("2/30", 2, None),
    ("긴급", 2, None),
])
def test_parse_sheet_date(text, tab, expected):
    assert parse_sheet_date(text, 2026, tab) == expected


def test_parse_row_falls_back_to_main_msg_and_keeps_blank():
    r = parse_row({"main_msg": "Ø153긴급", "spec_qty": "각도15 Ra1.7 / 68개"})
    assert r["dia"] == 153.0 and r["qty"] == 68 and r["ra"] == 1.7
    r = parse_row({"main_msg": "", "spec_qty": "NO100에 재기록함"})
    assert r["dia"] is None and r["length"] is None and r["qty"] is None and r["material"] == ""


def test_header_map_differs_per_tab():
    old = ["날짜", "거래처", "물품사진", "주요 메시지", "공차x갯수, 조도", "난이도", "작업자", "세팅 도움 "] + [""] * 10 + [
        "호닝 전 치수", "호닝 전 매니저 노트", "문자 보고", "호닝 후 치수", "호닝 후 매니저 노트"]
    new = ["날짜", "거래처", "물품사진", "주요 메시지", "공차x갯수, 조도", "난이도", "작업자", "연마량 / 치대"] + [""] * 10 + [
        "호닝 전 매니저 노트", "호닝 후 치수", "호닝 후 매니저 노트"]
    assert header_map(old)["pre_size"] == 18 and header_map(old)["post_note"] == 22
    assert header_map(new)["pre_size"] == 7 and header_map(new)["post_size"] == 19
