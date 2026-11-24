from decimal import Decimal
from pathlib import Path

import pytest

from pricer import (H_P, SOGWAN, PriceEntry, QuoteError, load_price_table, parse_pipe_type, parse_qty,
                    quote_pipe, round10, size_key)

TABLE = {k: PriceEntry(k, Decimal(p), Decimal(1)) for k, p in
         {"80x90": 51700, "60x70": 38500, "50x60": 29700, "45x55": 29700}.items()}


def test_80x90_760_hp():
    price, basis = quote_pipe(TABLE, "80x90", "760", "2개", "H.P")
    assert price == 39_290  # 51,700 × 0.76 = 39,292 → round10
    assert basis == "단가표 80x90 51,700/m × 760mm"


def test_60x70_2106():
    assert quote_pipe(TABLE, "60X70", "2106", "1", "HP")[0] == 81_080  # 81,081


def test_sogwan_50x60_1000():
    price, basis = quote_pipe(TABLE, "50*60", "1000", "144개", "소관")
    assert price == 26_730
    assert basis.endswith("× 0.9(소관)")


def test_basis_format():
    assert quote_pipe(TABLE, "45/55", "55", "10개", "H.P.")[1] == "단가표 45x55 29,700/m × 55mm"


def test_factor_applied():
    t = {"80x90": PriceEntry("80x90", Decimal(51700), Decimal("1.1"))}
    assert quote_pipe(t, "80x90", "1000", "1", "H.P")[0] == 56_870


@pytest.mark.parametrize("pipe_type, size, length, qty, reason", [
    ("", "80x90", "760", "1", "구분(H.P/소관) 없음"),
    ("H.P", "81x90", "760", "1", "단가표에 규격 없음"),
    ("H.P", "80x90", "칠백", "1", "길이 파싱 실패"),
    ("H.P", "80x90", "760", "몇개", "수량 파싱 실패"),
])
def test_quote_errors(pipe_type, size, length, qty, reason):
    with pytest.raises(QuoteError, match=reason.replace("(", r"\(").replace(")", r"\)")):
        quote_pipe(TABLE, size, length, qty, pipe_type)


def test_parsers():
    assert size_key("25x33(32)") == "25x33"
    assert size_key("63x76.3") == "63x76.3"
    assert size_key("75X95") == "75x95"
    assert parse_qty("144개") == 144
    assert parse_pipe_type("호닝파이프") == H_P
    assert parse_pipe_type("소관파이프") == SOGWAN
    assert round10(Decimal("39295")) == 39_300


PRICE_XLSX = Path(__file__).resolve().parent.parent / "data" / "price_table.xlsx"


@pytest.mark.skipif(not PRICE_XLSX.exists(), reason="data/price_table.xlsx 없음")
def test_real_price_table():
    t = load_price_table(PRICE_XLSX)
    assert t["80x90"].per_m == 51700
    assert t["45x55"].per_m == 29700
    assert "25x33" in t and "75x95" in t
    assert quote_pipe(t, "80x90", "760", "1", "H.P")[0] == 39_290
