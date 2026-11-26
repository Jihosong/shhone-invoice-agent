"""임가공 행 → 견적 (판정 사다리). 운영(pricer)과 백테스트가 같은 함수를 쓴다.

입력은 JobInput 하나로 통일: watcher가 넘기는 행(거래처 해석 결과 + D/E/F/H/S 원문)이든
백테스트 정답지 행이든 원문(RAW_FIELDS)을 job_parse.parse_row로 다시 파싱한다.

판정 사다리 (위에서 먼저 맞는 것):
  a. 사전단가 — D열(주요 메시지)이 숫자만: 1000 미만 ×10,000 (8→80,000 / 1.5→15,000), 1000 이상은 원 그대로.
     confidence=high.
  b. 동일품 — 같은 거래처(사업자번호, 없으면 정규화 이름)의 DB lines(kind=job) 중
     구경 ±0.3mm, 공차 동일(둘 다 있을 때), 기장 ±5%(둘 다 있을 때), 특이사항 키워드 집합 동일
     → 가장 최근 건의 단가. 직전 거래가 24개월 이상 전이면 연 +5% 복리 보정 + mid.
     수량이 직전의 3배 이상 / ⅓ 이하면 mid. 그 외 high.
  c. 미결 — B단계(Claude 견적)로 넘김.
모든 DB 검색은 cutoff_date **이전**(당일 제외) 자료만 — 백테스트 정답 누출 방지.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from cases import DbLine, LineIndex
from job_parse import RAW_FIELDS, norm, parse_row, parse_specials

M_PRESET = "사전단가"
M_SAME = "동일품"
M_PENDING = "미결"

SAME_DIA_TOL = 0.3       # mm
SAME_LEN_TOL = 0.05      # 비율
STALE_DAYS = 730         # 24개월
STALE_ANNUAL = 0.05      # 연 +5%
QTY_RATIO = 3.0

# 동일품 판정에서 빼는 키워드: 품목 성질이 아니라 일정·지시 (긴급) — 명세표 품목명에는 거의 안 남는다
NON_ITEM_KEYWORDS = frozenset({"긴급"})

_PRESET_RE = re.compile(r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?")


@dataclass
class JobInput:
    """견적할 임가공 행 하나 (거래처 해석 결과 + 원문 + 파싱 필드)."""
    client: str
    reg_no: str
    on_date: date | None
    raw: dict
    dia: float | None = None
    dia_all: list = field(default_factory=list)
    tol_lo: float | None = None
    tol_hi: float | None = None
    length: float | None = None
    qty: int | None = None
    material: list = field(default_factory=list)
    specials: list = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: dict, client: str = "", reg_no: str = "", on_date: date | None = None) -> "JobInput":
        raw = {f: str(raw.get(f) or "").strip() for f in RAW_FIELDS}
        p = parse_row(raw)
        return cls(client or "", reg_no or "", on_date, raw, p["dia"],
                   [float(x) for x in p["dia_all"].split()] if p["dia_all"] else [],
                   p["tol_lo"], p["tol_hi"], p["length"], p["qty"],
                   [x for x in p["material"].split(",") if x], [x for x in p["specials"].split(",") if x])

    @classmethod
    def from_sheet_row(cls, row: list[str], cols: dict, client: str, reg_no: str,
                       on_date: date | None = None) -> "JobInput":
        """watcher 스냅샷 행(1-based 열 번호 cols) → JobInput. 시트에 없는 열은 빈 값."""
        def cell(name):
            i = cols.get(name)
            return row[i - 1].strip() if i and i - 1 < len(row) else ""
        return cls.from_raw({f: cell(f) for f in RAW_FIELDS}, client, reg_no, on_date)

    @classmethod
    def from_backtest(cls, rec: dict) -> "JobInput":
        """백테스트 정답지 행 (raw_* 열) → JobInput. 정답지의 파싱 열은 쓰지 않고 다시 파싱."""
        d = rec.get("date") or ""
        return cls.from_raw({f: rec.get("raw_" + f, "") for f in RAW_FIELDS}, rec.get("client", ""),
                            rec.get("reg_no", ""), date.fromisoformat(d) if d else None)

    @property
    def item_keywords(self) -> frozenset:
        """동일품 비교용 키워드: 품목 서술(D·E)에서만 (매니저 노트 제외), 일정 키워드 제외."""
        return item_keywords(f"{self.raw.get('main_msg', '')} {self.raw.get('spec_qty', '')}")


@dataclass
class Quote:
    method: str
    unit_price: int | None = None
    confidence: str | None = None   # high / mid / low
    basis: str = ""
    ref_line_id: int | None = None

    def label(self) -> str:
        """산출근거 본문 'method | basis | conf' (블록태그는 pricer가 붙임)."""
        return f"{self.method} | {self.basis} | {self.confidence or '-'}"


def item_keywords(text: str) -> frozenset:
    return frozenset(parse_specials(text)) - NON_ITEM_KEYWORDS


def line_keywords(ln: DbLine) -> frozenset:
    return item_keywords(f"{ln.item} {ln.spec}")


def round_price(v: float) -> int:
    """보정 단가 반올림: 1만 원 이상은 천 원, 그 아래는 백 원 단위."""
    unit = 1000 if v >= 10000 else 100
    return int(round(v / unit) * unit)


# ---------------------------------------------------------------- a. 사전단가
def preset_price(main_msg: str) -> int | None:
    """D열이 숫자만이면 단가 (1000 미만 ×10,000, 이상은 원 그대로). 아니면 None."""
    s = norm(main_msg)
    if not s or not _PRESET_RE.fullmatch(s):
        return None
    v = float(s.replace(",", ""))
    if v <= 0:
        return None
    return int(round(v * 10000)) if v < 1000 else int(round(v))


def quote_preset(inp: JobInput) -> Quote | None:
    price = preset_price(inp.raw.get("main_msg", ""))
    if price is None:
        return None
    d = norm(inp.raw.get("main_msg", ""))
    how = f"D열 {d} ×10,000" if float(d.replace(",", "")) < 1000 else f"D열 {d}원"
    return Quote(M_PRESET, price, "high", how)


# ---------------------------------------------------------------- b. 동일품
def _tol_same(inp: JobInput, ln: DbLine) -> bool:
    if inp.tol_hi is None or ln.tol_hi is None:
        return True
    r = lambda v: round(float(v or 0), 4)  # noqa: E731
    return (r(inp.tol_lo), r(inp.tol_hi)) == (r(ln.tol_lo), r(ln.tol_hi))


def _len_same(inp: JobInput, ln: DbLine) -> bool:
    return not inp.length or not ln.length or abs(ln.length - inp.length) <= SAME_LEN_TOL * inp.length


def same_item_lines(inp: JobInput, index: LineIndex, cutoff_date: date, exclude_ids=()) -> list[DbLine]:
    """동일품 후보 (최근 순). cutoff_date 당일·이후와 exclude_ids(백테스트 정답 줄)는 제외."""
    if cutoff_date is None:
        raise ValueError("cutoff_date 필수 (백테스트 정답 누출 방지)")
    if inp.dia is None or not (inp.reg_no or inp.client):
        return []
    kw = inp.item_keywords
    out = [ln for ln in index.client_lines(inp.reg_no, inp.client)
           if ln.date < cutoff_date and ln.id not in exclude_ids and ln.unit_price and ln.unit_price > 0
           and ln.dia is not None and abs(ln.dia - inp.dia) <= SAME_DIA_TOL
           and _tol_same(inp, ln) and _len_same(inp, ln) and line_keywords(ln) == kw]
    out.sort(key=lambda ln: (ln.date, ln.id), reverse=True)
    return out


def quote_same_item(inp: JobInput, index: LineIndex, cutoff_date: date, exclude_ids=()) -> Quote | None:
    cands = same_item_lines(inp, index, cutoff_date, exclude_ids)
    if not cands:
        return None
    ln = cands[0]
    price, conf = float(ln.unit_price), "high"
    parts = [f"{ln.date.isoformat()} {ln.unit_price:,}원 Ø{ln.dia:g}"
             + (f" L{ln.length:g}" if ln.length else "") + (f" {ln.qty:g}개" if ln.qty else "")]
    days = (cutoff_date - ln.date).days
    if days >= STALE_DAYS:
        years = days / 365
        price *= (1 + STALE_ANNUAL) ** years
        conf = "mid"
        parts.append(f"{years:.1f}년 경과 연+{STALE_ANNUAL:.0%}")
    if inp.qty and ln.qty and (inp.qty >= QTY_RATIO * ln.qty or inp.qty * QTY_RATIO <= ln.qty):
        conf = "mid"
        parts.append(f"수량 {inp.qty}개 vs 직전 {ln.qty:g}개")
    if len(cands) > 1:
        parts.append(f"동일품 {len(cands)}건 중 최근")
    return Quote(M_SAME, round_price(price) if days >= STALE_DAYS else int(ln.unit_price), conf,
                 " / ".join(parts), ln.id)


# ---------------------------------------------------------------- 사다리
def quote_ladder(inp: JobInput, index: LineIndex, cutoff_date: date, exclude_ids=()) -> Quote:
    """a 사전단가 → b 동일품 → c 미결 (B단계에서 Claude)."""
    return (quote_preset(inp) or quote_same_item(inp, index, cutoff_date, exclude_ids)
            or Quote(M_PENDING, basis="사전단가·동일품 없음"))
