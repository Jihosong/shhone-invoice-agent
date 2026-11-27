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
     품목 성질 키워드(ITEM_KEYWORDS 7개) 집합 동일 → 가장 최근 건 기준, 경과 기간별:
       6개월 이내 직전 단가 그대로 high /
       6~12개월 그대로, confidence는 config/sheets.yaml pricing.same_item_6_12m_confidence (기본 mid)
         — mid면 '직전 YYYY-MM, 동결 유지 — 확인', high면 '직전 YYYY-MM, 동결 유지' /
       12~24개월 직전 × 1.05 (100원 버림) mid '직전 40,000 (2025-09) → +5% 인상 제안'
         — 거래처가 인상형(최근 3쌍 평균 인상률 > 5%, inflation.client_trend)이면 그 평균(상한 15%) /
       24개월 초과 → 미결 (Quote.hint에 직전 단가·날짜 + '최소 +5% 이상 제안할 것', max_conf=mid).
     수량이 직전의 3배 이상 / ⅓ 이하면 동일품으로 확정하지 않고 미결 (Quote.hint로 직전 건을 B단계에 넘김).
  c. 미결 — B단계(Claude 견적, pricer_ai)로 넘김.
운영자 지시 (D 주요 메시지·S 물품 세부 정보에 '계산해줘·로 해줘·적용·cm²당·원으로·올려·그대로·무시…' 같은 지시 어휘):
  사전단가(a)·동일품(b)을 건너뛰고 지시가 최우선. 결정적 패턴은 코드가 계산 (method=지시계산, high):
    'cm²당(센티스퀘어당) N원' → π × 내경(cm) × 기장(cm) × N, 100원 버림 (내경·기장 없으면 확인필요)
    '직전 단가 +N%' / 'N% 올려·내려' → 같은 거래처 직전 동일품 × (1±N%), 100원 버림 (직전 없으면 확인필요)
    '지난번 그대로' / '이전 단가로' → 직전 동일품 단가 그대로 / '개당 N원' / 'N원으로' → N
  그 밖의 지시 → 미결(Quote.directive)로 B단계: 프롬프트 맨 위 '※ 운영자 지시(최우선)', 결과 최대 mid
  (config/sheets.yaml pricing.directive_ai_high: true면 high 허용). 지시 행 산출근거 본문은 '[지시]'로 시작.
모든 DB 검색은 cutoff_date **이전**(당일 제외) 자료만 — 백테스트 정답 누출 방지.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from cases import DbLine, LineIndex
from inflation import RAISE_DEFAULT
from job_parse import (RAW_FIELDS, is_stock_removal, item_texts, norm, parse_chidae, parse_dias, parse_row,
                       parse_specials)

M_PRESET = "사전단가"
M_SAME = "동일품"
M_PENDING = "미결"
M_DIRECTIVE = "지시계산"
DIRECTIVE_TAG = "[지시]"

SAME_DIA_TOL = 0.3       # mm
SAME_LEN_TOL = 0.05      # 비율
FRESH_DAYS = 183         # 6개월 이내 → 그대로 high
FREEZE_DAYS = 365        # 6~12개월 → 그대로 (동결 유지), confidence는 freeze_conf()
FREEZE_CONF_DEFAULT = "mid"
FREEZE_CONF: str | None = None   # 지정하면 config보다 우선 (테스트용)
STALE_DAYS = 730         # 12~24개월 → 인상 제안 mid, 24개월 초과 → AI(최대 mid, 최소 +5% 제안)
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
    change_note: str = ""         # 견적완료 후 큰 변경으로 재견적할 때 (watcher requote) — AI 프롬프트에 명시

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
    directive: str = ""   # 운영자 지시문 (D·S) — 미결이면 B단계 프롬프트 맨 위에 최우선으로
    tag: str = ""         # 산출근거 본문 맨 앞: '[지시]' / 계산 못 한 지시 '[지시: cm²당 20원 — 기장 없음]'

    def label(self) -> str:
        """산출근거 본문 '[tag] method | basis | conf' (블록태그·가견적 태그는 pricer가 앞에 붙임)."""
        body = f"{self.method} | {self.basis} | {self.confidence or '-'}"
        return f"{self.tag} {body}" if self.tag else body


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


def same_item_lines(inp: JobInput, index: LineIndex, cutoff_date: date, exclude_ids=(),
                    loose_kw: bool = False) -> list[DbLine]:
    """동일품 후보 (최근 순). cutoff_date 당일·이후와 exclude_ids(백테스트 정답 줄)는 제외.
    품목 성질 키워드는 같아야 한다. loose_kw면 시트 쪽 키워드가 명세표 쪽의 부분집합이어도 후보
    (시트는 '용접' 같은 성질을 자주 빠뜨림 — 부록 B.2) — 키워드가 똑같은 후보가 먼저."""
    if cutoff_date is None:
        raise ValueError("cutoff_date 필수 (백테스트 정답 누출 방지)")
    if inp.dia is None or not (inp.reg_no or inp.client):
        return []
    kw = inp.item_keywords
    out = [ln for ln in index.client_lines(inp.reg_no, inp.client)
           if ln.date < cutoff_date and ln.id not in exclude_ids and ln.unit_price and ln.unit_price > 0
           and ln.dia is not None and abs(ln.dia - inp.dia) <= SAME_DIA_TOL
           and _tol_same(inp, ln) and _len_same(inp, ln) and _chidae_same(inp, ln)
           and (line_keywords(ln) == kw or (loose_kw and kw < line_keywords(ln)))]
    out.sort(key=lambda ln: (line_keywords(ln) == kw, ln.date, ln.id), reverse=True)
    return out


def quote_same_item(inp: JobInput, index: LineIndex, cutoff_date: date, exclude_ids=()) -> Quote | None:
    """동일품 Quote. 수량 차이가 크면 미결 Quote(hint=직전 건) — 사다리가 그대로 B단계로 넘긴다."""
    cands = same_item_lines(inp, index, cutoff_date, exclude_ids, loose_kw=True)
    if not cands:
        return None
    ln = cands[0]
    days = (cutoff_date - ln.date).days
    qty_off = bool(inp.qty and ln.qty and (inp.qty >= QTY_RATIO * ln.qty or inp.qty * QTY_RATIO <= ln.qty))
    if qty_off or days > STALE_DAYS:
        why = []
        if days > STALE_DAYS:
            why.append(f"{days // 30}개월 전 — 24개월 초과, 최소 +{RAISE_DEFAULT:.0%} 이상 제안할 것")
        if qty_off:
            why.append("수량 차이 큼")
        hint = (f"직전 동일품 단가 {ln.unit_price:,}원 ({ln.date.isoformat()}, {ln.qty:g}개) / "
                f"이번 {inp.qty or '?'}개 — " + ", ".join(why))
        return Quote(M_PENDING, basis=f"동일품 미확정 ({hint})", ref_line_id=ln.id, hint=hint,
                     max_conf="mid" if days > STALE_DAYS else None)
    detail = (f"{ln.date.isoformat()} {ln.unit_price:,}원 Ø{ln.dia:g}"
              + (f" L{ln.length:g}" if ln.length else "") + (f" {ln.qty:g}개" if ln.qty else ""))
    price, conf = int(ln.unit_price), "high"
    if days <= FRESH_DAYS:
        parts = [detail]
    elif days <= FREEZE_DAYS:
        conf = freeze_conf()
        parts = [f"직전 {ln.date:%Y-%m}, 동결 유지" + (" — 확인" if conf == "mid" else ""), detail]
    else:
        rate, why = raise_rate(inp, index, cutoff_date, exclude_ids)
        price, conf = raised_price(price, rate), "mid"
        parts = [f"직전 {ln.unit_price:,} ({ln.date:%Y-%m}) → {pct(rate)} 인상 제안" + (f" ({why})" if why else ""),
                 detail]
    extra = line_keywords(ln) - inp.item_keywords
    if extra:  # 시트에 없는 성질 키워드가 명세표에만 → 같은 품목일 가능성이 높지만 확인 필요
        conf = "mid"
        parts.append(f"명세표에만 '{','.join(sorted(extra))}' (시트엔 없음) → 최대 mid")
    if len(cands) > 1:
        parts.append(f"동일품 {len(cands)}건 중 최근")
    return Quote(M_SAME, price, conf, " / ".join(parts), ln.id)


_pricing_cache: tuple[str, float, dict] | None = None   # (경로, mtime, pricing 절)


def _pricing_cfg(path=None) -> dict:
    """config/sheets.yaml의 pricing 절 (파일이 바뀌면(mtime) 다시 읽음). 파일이 없으면 {}."""
    global _pricing_cache
    from pathlib import Path

    path = Path(path or Path(__file__).parent / "config" / "sheets.yaml")
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _pricing_cache is None or _pricing_cache[:2] != (str(path), mtime):
        import yaml

        with open(path, encoding="utf-8") as f:
            _pricing_cache = (str(path), mtime, (yaml.safe_load(f) or {}).get("pricing") or {})
    return _pricing_cache[2]


def freeze_conf(path=None) -> str:
    """6~12개월 동일품 confidence: FREEZE_CONF > config/sheets.yaml pricing.same_item_6_12m_confidence > mid.
    파일이 바뀌면(mtime) 다시 읽는다. high/mid 외 값은 경고 후 mid."""
    if FREEZE_CONF:
        return FREEZE_CONF
    v = _pricing_cfg(path).get("same_item_6_12m_confidence", FREEZE_CONF_DEFAULT)
    if v not in ("high", "mid"):
        from loguru import logger

        logger.warning("sheets.yaml pricing.same_item_6_12m_confidence={!r} — high/mid만 가능 → mid", v)
        v = FREEZE_CONF_DEFAULT
    return v


DIRECTIVE_AI_HIGH: bool | None = None   # 지정하면 config보다 우선 (테스트용)


def directive_ai_high(path=None) -> bool:
    """자유 지시 AI 결과의 high 허용: config/sheets.yaml pricing.directive_ai_high (기본 false → 최대 mid)."""
    if DIRECTIVE_AI_HIGH is not None:
        return DIRECTIVE_AI_HIGH
    return _pricing_cfg(path).get("directive_ai_high", False) is True


def pct(rate: float) -> str:
    """0.05 → '+5%', 0.083 → '+8.3%'."""
    return f"{rate * 100:+.1f}".rstrip("0").rstrip(".") + "%"


def raised_price(price: int, rate: float) -> int:
    """직전 단가 × (1 + rate), 100원 단위 버림 (38,500 × 1.05 = 40,425 → 40,400)."""
    return int(Decimal(price) * (1 + Decimal(str(rate))) // 100 * 100)


def raise_rate(inp: JobInput, index: LineIndex, cutoff_date: date, exclude_ids=()) -> tuple[float, str]:
    """12~24개월 동일품 인상률: 거래처 인상형이면 최근 3쌍 평균(상한 15%), 아니면 5%. (률, 표시 사유)"""
    from inflation import client_trend

    lines = [ln for ln in index.client_lines(inp.reg_no, inp.client)
             if ln.date < cutoff_date and ln.id not in exclude_ids]
    return client_trend(lines).raise_rate()


# ---------------------------------------------------------------- 운영자 지시 (D·S)
DIRECTIVE_FIELDS = (("main_msg", "D"), ("pre_note", "S"))   # D 주요 메시지, S 물품 세부 정보
_AREA = r"(?:센[티치]\s*스퀘어|평방\s*센[티치]|제곱\s*센[티치]|cm\s*(?:²|2|\^2)|㎠)"
# 지시 어휘 — 하나라도 있으면 '지시 있음' (사전단가·동일품 지름길을 건너뜀)
DIRECTIVE_RE = re.compile(
    r"계산\s*해|로\s*해\s*(?:줘|주세요|주십시오|라|요)|적용|" + _AREA + r"|원\s*으로|올려|내려|"
    r"\d\s*(?:만\s*)?(?:원|%|퍼센트|프로)?\s*(?:인상|인하)|"   # '1000원인상' ('학인하고' 같은 오타는 제외)
    r"그대로|무시|임가공\s*이지|(?:지난\s*번|이전|직전)\s*(?:단가|가격)|개당\s*\d", re.I)
_WON = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(만)?\s*원"
# 'cm²당 20원' · '센티스퀘어당 20원' · 시트에 실제로 쓰는 'cm2/18' · 'cm2/23원' (원 생략 가능)
_AREA_RATE_RE = re.compile(_AREA + r"\s*(?:당|에|/)\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(만)?\s*(?:원)?", re.I)
_PCT = r"(\d+(?:\.\d+)?)\s*(?:%|퍼센트|프로)"
_PREV_PCT_RE = re.compile(r"(?:직전|이전|지난\s*번)\s*(?:단가|가격)?\s*(?:에서|대비|의)?\s*([+-])\s*" + _PCT)
_PCT_WORD_RE = re.compile(_PCT + r"\s*(올려|올리|인상|내려|내리|인하)")
_PREV_SAME_RE = re.compile(r"(?:지난\s*번|직전|이전)\s*(?:이랑|과|하고|와)?\s*(?:단가|가격)?\s*(?:으로|로)?\s*"
                           r"(?:그대로|동일|같이|똑같이)|(?:이전|직전|지난\s*번)\s*(?:단가|가격)\s*(?:으로|로)")
_PER_PIECE_RE = re.compile(r"개당\s*" + _WON)
_WON_TO_RE = re.compile(_WON + r"\s*으로")


def directive_text(raw: dict) -> str:
    """D·S 중 지시 어휘가 있는 칸 → 'D: … / S: …' (지시 없으면 '')."""
    return " / ".join(f"{lab}: {norm(raw.get(f, ''))}" for f, lab in DIRECTIVE_FIELDS
                      if DIRECTIVE_RE.search(norm(raw.get(f, ""))))


def _won(num: str, man: str | None) -> int:
    v = Decimal(num.replace(",", ""))
    return int(v * 10000 if man else v)


def floor100(v) -> int:
    """100원 단위 버림."""
    return int(Decimal(str(v)) // 100 * 100)


def area_price(dia_mm: float, length_mm: float, rate: int) -> tuple[int, str]:
    """π × 내경(cm) × 기장(cm) × cm²당 단가, 100원 버림 → (단가, 식). Ø160 L2380 20원 → 239,267.6 → 239,200."""
    import math

    raw = math.pi * (dia_mm / 10) * (length_mm / 10) * rate
    price = floor100(raw)
    return price, f"π×{dia_mm / 10:g}cm×{length_mm / 10:g}cm×{rate:,}원 = {raw:,.0f} → {price:,} (100원 버림)"


def quote_directive(inp: JobInput, text: str, index: LineIndex | None, cutoff_date: date,
                    exclude_ids=()) -> Quote:
    """운영자 지시가 있는 행 (사전단가·동일품 지름길 없이). 결정적 패턴이면 지시계산 high,
    계산할 수 없으면 단가 없이 '[지시: … — 사유]' (pricer가 견적확인필요로), 그 밖은 미결(directive) → B단계."""
    def fail(what: str, why: str) -> Quote:
        return Quote(M_DIRECTIVE, None, None, f"운영자 지시 '{text}'", tag=f"[지시: {what} — {why}]",
                     directive=text)

    def ok(price: int, how: str) -> Quote:
        return Quote(M_DIRECTIVE, price, "high", how, tag=DIRECTIVE_TAG, directive=text)

    def prev_line():
        if index is None:
            return None
        cands = same_item_lines(inp, index, cutoff_date, exclude_ids, loose_kw=True)
        return cands[0] if cands else None

    # a. cm²당 N원 (먼저: 'cm²당 20원으로'의 '20원으로'를 개당 단가로 읽지 않게)
    if m := _AREA_RATE_RE.search(text):
        rate = _won(m.group(1), m.group(2))
        what = f"cm²당 {rate:,}원"
        if inp.dia is None:
            return fail(what, "내경 없음")
        if not inp.length:
            return fail(what, "기장 없음")
        price, how = area_price(inp.dia, inp.length, rate)
        return ok(price, f"{what}: {how}")
    # c. 직전 단가 ±N% / N% 올려·내려
    if m := _PREV_PCT_RE.search(text):
        rate = float(m.group(2)) / 100 * (1 if m.group(1) == "+" else -1)
    elif m := _PCT_WORD_RE.search(text):
        rate = float(m.group(1)) / 100 * (1 if m.group(2) in ("올려", "올리", "인상") else -1)
    if m:
        what = f"직전 단가 {pct(rate)}"
        ln = prev_line()
        if ln is None:
            return fail(what, "직전 동일품 없음")
        price = raised_price(int(ln.unit_price), rate)
        return ok(price, f"{what}: 직전 {ln.unit_price:,}원 ({ln.date.isoformat()}) → {price:,} (100원 버림)")
    # d. 지난번 그대로 / 이전 단가로
    if _PREV_SAME_RE.search(text):
        what = "직전 단가 그대로"
        ln = prev_line()
        if ln is None:
            return fail(what, "직전 동일품 없음")
        return ok(int(ln.unit_price), f"{what}: 직전 {ln.unit_price:,}원 ({ln.date.isoformat()})")
    # b. 개당 N원 / N원으로
    if m := (_PER_PIECE_RE.search(text) or _WON_TO_RE.search(text)):
        price = _won(m.group(1), m.group(2))
        if price <= 0:
            return fail(m.group(0).strip(), "단가 0")
        return ok(price, f"개당 {price:,}원 (지시 '{m.group(0).strip()}')")
    return Quote(M_PENDING, basis="운영자 지시 → AI", directive=text, tag=DIRECTIVE_TAG)


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
    """운영자 지시(있으면 최우선) → a 사전단가 → b 동일품 → c 미결 (B단계에서 Claude).
    미결 Quote도 hint·directive를 가질 수 있다."""
    if cutoff_date is None:
        raise ValueError("cutoff_date 필수 (백테스트 정답 누출 방지)")
    d = directive_text(inp.raw)
    if d:
        return quote_directive(inp, d, index, cutoff_date, exclude_ids)
    return (quote_preset(inp, index, cutoff_date, exclude_ids)
            or quote_same_item(inp, index, cutoff_date, exclude_ids)
            or Quote(M_PENDING, basis="사전단가·동일품 없음"))
