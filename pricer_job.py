"""임가공 행 → 견적 (판정 사다리). 운영(pricer)과 백테스트가 같은 함수를 쓴다.

입력은 JobInput 하나로 통일: watcher가 넘기는 행(거래처 해석 결과 + D/E/F/H/S 원문)이든
백테스트 정답지 행이든 원문(RAW_FIELDS)을 job_parse.parse_row로 다시 파싱한다.

판정 사다리 (위에서 먼저 맞는 것):
  a. 사전단가 — D열(주요 메시지)이 숫자만: 1000 미만 ×10,000 (8→80,000 / 1.5→15,000), 1000 이상은 원 그대로.
     부호(-0.5, +0.3)나 mm가 붙은 값은 연마량(치대) 표기 → 사전단가 아님.
     안전장치: 결과가 5,000원 미만·5,000,000원 초과면 사전단가 아님(→ 다음 단계).
     D가 '20/30'처럼 여럿이면 E의 품목(구경) 수와 같을 때만 품목별 단가, 아니면 사전단가 아님.
     confidence: 같은 거래처 과거 단가 중앙값과 3배 이상 차이 → low(단가는 적되 확인필요),
     과거 단가 없음 → mid, 그 외 high.
  b. 동일품 — 같은 거래처(사업자번호, 없으면 정규화 이름)의 DB lines(kind=job) 중
     구경 ±0.3mm, 공차 동일(둘 다 있을 때), 기장 ±5%(둘 다 있을 때), 치대 동일(둘 다 있을 때),
     품목 성질 키워드(ITEM_KEYWORDS 7개) 집합 동일 → 가장 최근 건의 단가 그대로 (연 % 보정 없음):
       6개월 이내 high / 6~24개월 mid '직전 YYYY-MM — 인상 검토' /
       24개월 초과 → 미결 (Quote.hint에 직전 단가·날짜, B단계에서 인상 성향 보고 판단, max_conf=mid).
     수량이 직전의 3배 이상 / ⅓ 이하면 동일품으로 확정하지 않고 미결 (Quote.hint로 직전 건을 B단계에 넘김).
  c. 미결 — B단계(Claude 견적, pricer_ai)로 넘김.
모든 DB 검색은 cutoff_date **이전**(당일 제외) 자료만 — 백테스트 정답 누출 방지.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import date

from cases import DbLine, LineIndex
from job_parse import (RAW_FIELDS, is_stock_removal, item_texts, norm, parse_chidae, parse_dias, parse_row,
                       parse_specials)

M_PRESET = "사전단가"
M_SAME = "동일품"
M_PENDING = "미결"

SAME_DIA_TOL = 0.3       # mm
SAME_LEN_TOL = 0.05      # 비율
FRESH_DAYS = 183         # 6개월 이내 → high
STALE_DAYS = 730         # 6~24개월 → mid(인상 검토), 24개월 초과 → AI(최대 mid)
CHIDAE_TOL = 1e-6
QTY_RATIO = 3.0
PRESET_MIN = 5_000       # 사전단가로 인정하는 범위 (원)
PRESET_MAX = 5_000_000
PRESET_OUTLIER = 3.0     # 같은 거래처 과거 단가 중앙값 대비 이 배율 이상 차이 → low

# 동일품 비교 키워드: 품목 성질만 (조도·치대·긴급 같은 작업 조건·지시는 명세표 품목명에 잘 안 남는다)
ITEM_KEYWORDS = frozenset({"막힘", "사각", "다공", "딥홀", "용접", "열처리", "도금"})

_NUM = r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?"
_PRESET_RE = re.compile(_NUM)
_PRESET_MULTI_RE = re.compile(rf"(?:{_NUM})(?:\s*/\s*(?:{_NUM}))+")


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
    chidae: float | None = None   # 치대(연마량) mm, 절댓값

    @classmethod
    def from_raw(cls, raw: dict, client: str = "", reg_no: str = "", on_date: date | None = None) -> "JobInput":
        raw = {f: str(raw.get(f) or "").strip() for f in RAW_FIELDS}
        p = parse_row(raw)
        return cls(client or "", reg_no or "", on_date, raw, p["dia"],
                   [float(x) for x in p["dia_all"].split()] if p["dia_all"] else [],
                   p["tol_lo"], p["tol_hi"], p["length"], p["qty"],
                   [x for x in p["material"].split(",") if x], [x for x in p["specials"].split(",") if x],
                   p["chidae"])

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
        """동일품 비교용 키워드: 품목 서술(D·E)에서만 (매니저 노트 제외), 품목 성질 7개만."""
        return item_keywords(f"{self.raw.get('main_msg', '')} {self.raw.get('spec_qty', '')}")


@dataclass
class Quote:
    method: str
    unit_price: int | None = None
    confidence: str | None = None   # high / mid / low
    basis: str = ""
    ref_line_id: int | None = None
    item_prices: list = field(default_factory=list)  # 품목별 사전단가 [(구경, 단가)] — D '20/30'
    hint: str = ""   # 미결일 때 B단계 참고 사례 맨 위에 둘 한 줄 (예: 수량 차이 큰 직전 동일품)
    max_conf: str | None = None   # B단계 결과 confidence 상한 (24개월 초과 동일품 → mid)
    cost_usd: float = 0.0   # B단계 API 비용

    def label(self) -> str:
        """산출근거 본문 'method | basis | conf' (블록태그는 pricer가 붙임)."""
        return f"{self.method} | {self.basis} | {self.confidence or '-'}"


def item_keywords(text: str) -> frozenset:
    return frozenset(parse_specials(text)) & ITEM_KEYWORDS


def line_keywords(ln: DbLine) -> frozenset:
    return item_keywords(f"{ln.item} {ln.spec}")


def line_chidae(ln: DbLine) -> float | None:
    return parse_chidae("", f"{ln.item} {ln.spec}")


# ---------------------------------------------------------------- a. 사전단가
def _preset_value(tok: str) -> int | None:
    v = float(tok.replace(",", ""))
    price = int(round(v * 10000)) if v < 1000 else int(round(v))
    return price if PRESET_MIN <= price <= PRESET_MAX else None


def preset_prices(main_msg: str) -> list[int] | None:
    """D열이 숫자만(또는 '20/30'처럼 / 로 여럿)이면 단가 목록. 하나라도 범위(5천~500만 원) 밖이면 None."""
    s = norm(main_msg)
    if not s or is_stock_removal(s) or not (_PRESET_RE.fullmatch(s) or _PRESET_MULTI_RE.fullmatch(s)):
        return None
    out = [_preset_value(t.strip()) for t in s.split("/")]
    return None if any(p is None for p in out) else out


def preset_price(main_msg: str) -> int | None:
    """D열이 숫자 하나면 단가 (1000 미만 ×10,000, 이상은 원 그대로, 5천~500만 원 밖이면 None)."""
    got = preset_prices(main_msg)
    return got[0] if got and len(got) == 1 else None


def client_prices(inp: JobInput, index: LineIndex | None, cutoff_date: date | None, exclude_ids=()) -> list[int]:
    """같은 거래처 과거 단가 (cutoff 이전, 정답 줄 제외)."""
    if index is None or cutoff_date is None or not (inp.reg_no or inp.client):
        return []
    return [ln.unit_price for ln in index.client_lines(inp.reg_no, inp.client)
            if ln.date < cutoff_date and ln.id not in exclude_ids and ln.unit_price and ln.unit_price > 0]


def quote_preset(inp: JobInput, index: LineIndex | None = None, cutoff_date: date | None = None,
                 exclude_ids=()) -> Quote | None:
    d = norm(inp.raw.get("main_msg", ""))
    prices = preset_prices(d)
    if not prices:
        return None
    items = []
    if len(prices) > 1:
        e_dias = parse_dias(inp.raw.get("spec_qty", ""))
        if len(e_dias) != len(prices):
            return None  # D 숫자 수 ≠ E 품목 수 → 무엇의 단가인지 모름
        items = list(zip(e_dias, prices))
        how = "D열 " + " / ".join(f"Ø{dia:g} {p:,}" for dia, p in items)
    else:
        how = f"D열 {d} ×10,000" if float(d.replace(",", "")) < 1000 else f"D열 {d}원"
    past = client_prices(inp, index, cutoff_date, exclude_ids)
    if not past:
        conf = "mid"
        how += " / 거래처 과거 단가 없음"
    else:
        med = statistics.median(past)
        off = [p for p in prices if p >= PRESET_OUTLIER * med or p * PRESET_OUTLIER <= med]
        conf = "low" if off else "high"
        if off:
            how += f" / 거래처 과거 중앙값 {med:,.0f}과 {PRESET_OUTLIER:g}배 이상 차이"
    return Quote(M_PRESET, prices[0], conf, how, item_prices=items)


# ---------------------------------------------------------------- b. 동일품
def _tol_same(inp: JobInput, ln: DbLine) -> bool:
    if inp.tol_hi is None or ln.tol_hi is None:
        return True
    r = lambda v: round(float(v or 0), 4)  # noqa: E731
    return (r(inp.tol_lo), r(inp.tol_hi)) == (r(ln.tol_lo), r(ln.tol_hi))


def _chidae_same(inp: JobInput, ln: DbLine) -> bool:
    if inp.chidae is None:
        return True
    v = line_chidae(ln)
    return v is None or abs(v - inp.chidae) <= CHIDAE_TOL


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
           and _tol_same(inp, ln) and _len_same(inp, ln) and line_keywords(ln) == kw and _chidae_same(inp, ln)]
    out.sort(key=lambda ln: (ln.date, ln.id), reverse=True)
    return out


def quote_same_item(inp: JobInput, index: LineIndex, cutoff_date: date, exclude_ids=()) -> Quote | None:
    """동일품 Quote. 수량 차이가 크면 미결 Quote(hint=직전 건) — 사다리가 그대로 B단계로 넘긴다."""
    cands = same_item_lines(inp, index, cutoff_date, exclude_ids)
    if not cands:
        return None
    ln = cands[0]
    days = (cutoff_date - ln.date).days
    qty_off = bool(inp.qty and ln.qty and (inp.qty >= QTY_RATIO * ln.qty or inp.qty * QTY_RATIO <= ln.qty))
    if qty_off or days > STALE_DAYS:
        why = []
        if days > STALE_DAYS:
            why.append(f"{days // 30}개월 전 — 24개월 초과, 인상 성향 보고 판단")
        if qty_off:
            why.append("수량 차이 큼")
        hint = (f"직전 동일품 {ln.qty:g}개 {ln.unit_price:,} / 이번 {inp.qty or '?'}개 ({ln.date.isoformat()}; "
                + ", ".join(why) + ")")
        return Quote(M_PENDING, basis=f"동일품 미확정 ({hint})", ref_line_id=ln.id, hint=hint,
                     max_conf="mid" if days > STALE_DAYS else None)
    conf = "high"
    parts = [f"{ln.date.isoformat()} {ln.unit_price:,}원 Ø{ln.dia:g}"
             + (f" L{ln.length:g}" if ln.length else "") + (f" {ln.qty:g}개" if ln.qty else "")]
    if days > FRESH_DAYS:
        conf = "mid"
        parts.append(f"직전 {ln.date:%Y-%m} — 인상 검토")
    if len(cands) > 1:
        parts.append(f"동일품 {len(cands)}건 중 최근")
    return Quote(M_SAME, int(ln.unit_price), conf, " / ".join(parts), ln.id)


# ---------------------------------------------------------------- 다품목 (한 행에 품목 여럿)
def _item_src_field(raw: dict) -> str:
    """품목 구간을 나눌 칸: E에 구경이 있으면 E, 아니면 D (parse_row·발행 줄과 같은 기준)."""
    return "spec_qty" if parse_dias(raw.get("spec_qty", "")) else "main_msg"


def item_count(raw: dict) -> int:
    """행의 품목 수 (발행 줄 수와 같음). 구경 없는 행은 1."""
    return len(item_texts(raw.get(_item_src_field(raw), ""))) or 1


def split_items(inp: JobInput) -> list[JobInput]:
    """다품목 행 → 품목 구간마다 JobInput (구간 글에 공통 머리 상속, 다른 칸 원문은 그대로).
    D가 '6/5.5/5'처럼 구간 수와 같은 개수의 숫자면 구간마다 제 몫의 D. 품목이 하나면 [inp]."""
    field_ = _item_src_field(inp.raw)
    texts = item_texts(inp.raw.get(field_, ""))
    if len(texts) <= 1:
        return [inp]
    d = inp.raw.get("main_msg", "")
    tokens = [t.strip() for t in norm(d).split("/")]
    per_d = tokens if preset_prices(d) and len(tokens) == len(texts) else None
    out = []
    for i, t in enumerate(texts):
        raw = dict(inp.raw, **{field_: t})
        if per_d:
            raw["main_msg"] = per_d[i]
        out.append(JobInput.from_raw(raw, inp.client, inp.reg_no, inp.on_date))
    return out


# ---------------------------------------------------------------- 사다리
def quote_ladder(inp: JobInput, index: LineIndex, cutoff_date: date, exclude_ids=()) -> Quote:
    """a 사전단가 → b 동일품 → c 미결 (B단계에서 Claude). 미결 Quote도 hint를 가질 수 있다."""
    if cutoff_date is None:
        raise ValueError("cutoff_date 필수 (백테스트 정답 누출 방지)")
    return (quote_preset(inp, index, cutoff_date, exclude_ids)
            or quote_same_item(inp, index, cutoff_date, exclude_ids)
            or Quote(M_PENDING, basis="사전단가·동일품 없음"))
