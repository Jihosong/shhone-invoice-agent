"""경리나라 '엑셀일괄등록' 파일(.xls) 생성.

기준 샘플: tests/fixtures/gyeongri/{A_1줄, B_1장3줄, C_2장}.xls — 실제 업로드에 성공한 파일.
- xlwt BIFF .xls, 시트 'Sheet1', 1행 헤더 20열 고정.
- 명세표(Statement) 하나 = 첫 줄에 거래일자~창고 + 품목, 2번째 줄부터는 품목월일~세액만.
- 문자 칸은 텍스트 서식('@'), 수량·단가·공급가액·세액은 숫자. 빈 칸은 셀을 아예 쓰지 않음.
- 공급가액 = 단가 × 수량, 세액 = 공급가액의 10% 내림.
- 품목비고는 비움(명세표에 인쇄되어 고객에게 보임). 공통메모는 config issue.memo_text 그대로
  (명세표마다 첫 줄에만, 이어지는 품목 줄은 비움). '(AI)'로 끝나서 월간 import가 AI 발행을 알아본다.

줄 만들기는 시트 종류별 builder: builder(get, ctx) → [Line, …] (블록의 한 행 → 줄 하나 이상).
- 호닝파이프(pipe): 한 행 = 한 줄 (pipe_line).
- 임가공(job): 한 행에 품목(구경)이 여럿이면 여러 줄 (job_lines). 같은 거래처 동일품 이력이 있으면 그 명세표의
  품목명·규격 표기를 따르고, 없으면 '막힘 시린더호닝' / 'Ø50+0.0~+0.05 / 300' 식으로 만든다.

업로드 자동화(화면 조작)는 2단계 — 지금은 사람이 data/outbox/의 파일을 올린다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

HEADERS = ["거래일자", "구분", "거래처명", "사업자번호", "부가세구분", "프로젝트/현장", "창고", "품목월일", "품목코드",
           "품목명", "규격", "수량", "단위", "단가", "공급가액", "세액", "품목비고", "입금액", "인수자", "공통메모"]
COL = {name: i for i, name in enumerate(HEADERS)}
SHEET_NAME = "Sheet1"
BIZ_TYPE = "사업자"
VAT_TYPE = "별도"
PIPE_NAME = "호닝파이프"
DEFAULT_MEMO_TEXT = "(주)삼화호닝 기업 451-079229-04-018, 감사합니다. (AI)"


class LineError(ValueError):
    """줄을 만들 수 없음 (발행 보류 사유)."""


@dataclass(frozen=True)
class Line:
    name: str           # 품목명
    spec: str           # 규격
    qty: int            # 수량
    unit_price: int     # 단가

    @property
    def supply(self) -> int:
        return self.unit_price * self.qty

    @property
    def tax(self) -> int:
        return self.supply // 10  # 10% 내림 (정수 금액이라 int(supply*0.1)과 같고 부동소수 오차 없음)


@dataclass
class Statement:
    """명세표 한 장 = 시트 묶음(block) 하나."""
    block_id: str
    client: str         # 마스터 정식명
    reg_no: str         # 000-00-00000
    day: date           # 거래일자 = 품목월일
    lines: list[Line] = field(default_factory=list)
    sheet: str = ""

    @property
    def supply(self) -> int:
        return sum(ln.supply for ln in self.lines)

    @property
    def tax(self) -> int:
        return sum(ln.tax for ln in self.lines)


# ---------------------------------------------------------------- 값 파싱
def parse_price(text) -> int:
    """단가 칸 '39290' / '39,290' / '39,290원' → 39290. 양의 정수가 아니면 LineError."""
    s = re.sub(r"[,\s원]", "", str(text or ""))
    if not re.fullmatch(r"\d+", s) or int(s) <= 0:
        raise LineError(f"단가 없음/형식 아님({text!r})")
    return int(s)


_DATE_YMD = re.compile(r"^(\d{4})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})\.?$")
_DATE_MD = [re.compile(r"^(\d{1,2})\s*[/.\-]\s*(\d{1,2})\.?$"), re.compile(r"^(\d{1,2})\s*월\s*(\d{1,2})\s*일?$")]


def tab_month(title: str) -> int | None:
    m = re.match(r"^\s*(\d{1,2})\s*월", title or "")
    return int(m.group(1)) if m and 1 <= int(m.group(1)) <= 12 else None


def block_date(text: str, tab_title: str, today: date | None = None) -> date:
    """블록 날짜 칸('11/26', '11.26', '11월 26일', '2026-11-26') → date.

    연도는 월 탭 기준: 탭 월이 오늘 월보다 크면 작년 탭(1월에 보는 12월 탭). 날짜 월이 탭 월과 6개월
    넘게 떨어지면 해를 넘긴 것(12월 탭에 적힌 1/2 → 다음 해, 1월 탭에 적힌 12/30 → 전 해)."""
    today = today or date.today()
    s = str(text or "").strip()
    try:
        m = _DATE_YMD.match(s)
        if m:
            return date(*(int(g) for g in m.groups()))
        for rx in _DATE_MD:
            m = rx.match(s)
            if m:
                mo, d = (int(g) for g in m.groups())
                tm = tab_month(tab_title) or today.month
                y = today.year - 1 if tm > today.month else today.year
                if mo - tm > 6:
                    y -= 1
                elif tm - mo > 6:
                    y += 1
                return date(y, mo, d)
    except ValueError:
        pass
    raise LineError(f"날짜 해석 불가({text!r})")


# ---------------------------------------------------------------- 줄 builder (행 → Line)
def pipe_line(get) -> Line:
    """호닝파이프 한 행 → Line. get(열이름) = 셀 문자열.
    품목명 '호닝파이프'(소관이면 '호닝파이프 소관'), 규격 '80x90 L760', 수량 '5개' → 5."""
    from pricer import SOGWAN, QuoteError, _num, parse_length, parse_pipe_type, parse_qty, size_key

    try:
        kind = parse_pipe_type(get("pipe_type"))
        key = size_key(get("size"))
        if key is None:
            raise LineError(f"규격 파싱 실패({get('size')!r})")
        length = _num(str(parse_length(get("length"))))
        qty = parse_qty(get("qty"))
    except QuoteError as e:
        raise LineError(str(e)) from None
    name = f"{PIPE_NAME} {SOGWAN}" if kind == SOGWAN else PIPE_NAME
    return Line(name, f"{key} L{length}", qty, parse_price(get("unit_price")))


# ---------------------------------------------------------------- 임가공
@dataclass
class LineCtx:
    """builder에 넘기는 묶음 정보 (임가공 이력 표기용)."""
    client: str = ""
    reg_no: str = ""
    day: date | None = None
    index: object = None   # cases.LineIndex — 임가공일 때만


JOB_BASE = "시린더호닝"
JOB_FACE = "면작업"
# 품목명 앞에 붙이는 말 (순서대로): (job_parse 라벨, 품목명 표기)
JOB_NAME_PREFIX = [("수리/재작업", "수리품"), ("SUS", "SUS"), ("AL", "알미늄"), ("막힘", "막힘"), ("사각", "사각"),
                   ("용접", "용접"), ("열처리", "열처리"), ("도금", "도금"), ("딥홀", "딥홀"), ("다공", "다공")]
_QTY_TAIL_RE = re.compile(r"\s*[/×xX*]?\s*\d+\s*(?:개|ea|pcs|본|set|조(?!도))(?![a-z]).*$", re.I | re.S)
_QTY_ANY_RE = re.compile(r"\s*[/×xX*]?\s*\d+\s*(?:개|ea|pcs|본|set|조(?!도))(?![a-z])", re.I)
_RA_RE = re.compile(r"Ra\s*\d+(?:\.\d+)?(?:\s*~\s*\d+(?:\.\d+)?)?", re.I)


def job_name(text: str) -> str:
    """D·E 글의 품목 성질·재질 키워드 → '수리품 SUS 막힘 시린더호닝'. 면작업이면 '시린더호닝' 대신 '면작업'."""
    from job_parse import parse_materials, parse_specials

    labels = set(parse_specials(text)) | set(parse_materials(text))
    words = [w for lbl, w in JOB_NAME_PREFIX if lbl in labels]
    return " ".join(words + [JOB_FACE if "면작업" in labels else JOB_BASE])


def job_segments(text: str) -> list[str]:
    """E(구경 없으면 D) 글 → 품목 구간들. 구경(Ø)마다 한 구간, '~Ø'로 이어진 범위·수량 없는 앞 구간은 다음에 붙임."""
    from job_parse import _dia_spans, norm

    s = norm(text)
    spans = [st for _, st, _ in _dia_spans(s)]
    starts = [st for i, st in enumerate(spans) if i == 0 or not s[max(0, st - 2):st].strip().endswith("~")]
    if not starts:
        return []
    segs = [s[a:b].strip(" +,") for a, b in zip(starts, starts[1:] + [len(s)])]
    out: list[str] = []
    carry = ""
    for i, seg in enumerate(segs):
        seg = f"{carry} {seg}".strip() if carry else seg
        if i < len(segs) - 1 and not _QTY_TAIL_RE.search(seg):
            carry = seg
            continue
        carry = ""
        out.append(seg)
    return out


def job_spec(seg: str, prefix: str = "") -> str:
    """품목 구간 → 규격 (수량 부분과 그 뒤를 뺀 Ø·공차·기장). prefix의 Ra 표기는 앞에 붙임."""
    spec = _QTY_TAIL_RE.sub("", seg).strip(" /+,")
    ra = _RA_RE.search(prefix or "")
    return f"{ra.group(0)} {spec}" if ra and ra.group(0) not in spec else spec


def _history_label(seg_text: str, d: str, ctx: LineCtx) -> tuple[str, str] | None:
    """같은 거래처 동일품 최근 명세표의 (품목명, 규격). 규격은 구경·기장이 똑같을 때만 그대로."""
    from datetime import timedelta

    from pricer_job import JobInput, same_item_lines

    if ctx.index is None or not (ctx.client or ctx.reg_no):
        return None
    inp = JobInput.from_raw({"main_msg": d, "spec_qty": seg_text}, ctx.client, ctx.reg_no, ctx.day)
    cands = same_item_lines(inp, ctx.index, (ctx.day or date.today()) + timedelta(days=1))
    if not cands or not cands[0].item:
        return None
    ln = cands[0]
    exact = (ln.dia is not None and inp.dia is not None and abs(ln.dia - inp.dia) < 1e-6
             and (ln.length or None) == (inp.length or None))
    return ln.item, (ln.spec if exact and ln.spec else "")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "").upper()


def strip_qty(text: str) -> str:
    """원문에서 수량 표기('/ 10개', '× 2EA')만 뺀 글."""
    return re.sub(r"\s{2,}", " ", _QTY_ANY_RE.sub(" ", text or "")).strip(" /+,×*")


def _history_by_text(text: str, ctx: LineCtx) -> tuple[str, str] | None:
    """구경 없는 행: 같은 거래처 최근 명세표 중 품목명(+규격)이 이 글과 같거나 서로 포함(공백 무시, 4자 이상)."""
    from datetime import timedelta

    key = _compact(text)
    if ctx.index is None or len(key) < 4 or not (ctx.client or ctx.reg_no):
        return None
    cutoff = (ctx.day or date.today()) + timedelta(days=1)
    for ln in sorted(ctx.index.client_lines(ctx.reg_no, ctx.client), key=lambda x: (x.date, x.id), reverse=True):
        item = _compact(ln.item)
        if ln.date < cutoff and len(item) >= 4 and (item == key or item in key or key in item):
            return ln.item, ln.spec or ""
    return None


def job_lines(get, ctx: LineCtx) -> list[Line]:
    """임가공 한 행 → 줄들. 단가 '40000/25000'은 품목별, 단가 하나면 모든 줄 같은 단가. 수량은 구간별 'N개'.
    구경(Ø)이 없으면 한 줄: 이력 표기(품목명 글 일치) 또는 품목명 = 원문에서 수량만 뺀 글, 규격 비움.
    수량이 없으면 1 (경고 로그) — 사람이 파일을 보고 거른다. 보류는 단가 개수 ≠ 품목 개수일 때만."""
    from loguru import logger

    from job_parse import _dia_spans, norm, parse_dias, parse_qty

    d, e = norm(get("main_msg")), norm(get("spec_qty"))
    src = e if parse_dias(e) else d
    segs = job_segments(src)
    n_items = len(segs) or 1
    tokens = [t for t in re.sub(r"\s+", "", str(get("unit_price") or "")).split("/") if t]
    prices = [parse_price(t) for t in tokens] or [parse_price("")]
    if len(prices) == 1:
        prices = prices * n_items
    elif len(prices) != n_items:
        raise LineError(f"단가 {len(prices)}개 ≠ 품목 {n_items}개 ({get('unit_price')!r})")
    if len(segs) <= 1:
        qtys = [parse_qty(e) or parse_qty(d)]
    else:
        qtys = [parse_qty(seg) for seg in segs]
    if any(not q for q in qtys):
        logger.warning("임가공 줄 수량 없음 → 1로 ({!r})", (e or d)[:60])
        qtys = [q or 1 for q in qtys]
    if not segs:  # 구경 없음 — 모델명·품번만 적는 거래처
        text = strip_qty(e or d)
        name, spec = _history_by_text(text, ctx) or (text, "")
        return [Line(name or (e or d), spec, qtys[0], prices[0])]
    spans = _dia_spans(src)
    prefix = src[:spans[0][1]] if spans else ""   # 첫 구경 앞 글 (Ra 표기 등)
    name0 = job_name(f"{d} {e}")
    out = []
    for i, (seg, qty, price) in enumerate(zip(segs, qtys, prices)):
        hist = _history_label(seg if len(segs) > 1 else src, d, ctx)
        spec = job_spec(seg, prefix if i == 0 else "")
        name = name0
        if hist:
            name, spec = hist[0], hist[1] or spec
        out.append(Line(name, spec, qty, price))
    return out


# 시트 kind → builder(get, ctx) → [Line, …]
LINE_BUILDERS = {"pipe": lambda get, ctx=None: [pipe_line(get)], "job": job_lines}


# ---------------------------------------------------------------- .xls 쓰기
def build_workbook(statements: list[Statement], memo_text: str = DEFAULT_MEMO_TEXT):
    import xlwt

    text = xlwt.easyxf(num_format_str="@")
    wb = xlwt.Workbook(encoding="utf-8")
    ws = wb.add_sheet(SHEET_NAME)
    for c, h in enumerate(HEADERS):
        ws.write(0, c, h)

    def put(r, name, value):
        if value is None or value == "":
            return  # 빈 칸은 셀을 쓰지 않음 (샘플과 같게)
        if isinstance(value, str):
            ws.write(r, COL[name], value, text)
        else:
            ws.write(r, COL[name], value)

    r = 1
    for st in statements:
        if not st.lines:
            raise LineError(f"묶음 {st.block_id}: 줄 없음")
        for i, ln in enumerate(st.lines):
            if i == 0:  # 명세표 머리 — 2번째 줄부터는 비움 (B 샘플)
                put(r, "거래일자", st.day.isoformat())
                put(r, "구분", BIZ_TYPE)
                put(r, "거래처명", st.client)
                put(r, "사업자번호", st.reg_no)
                put(r, "부가세구분", VAT_TYPE)
                put(r, "공통메모", memo_text)  # 빈 문자열이면 안 씀
            put(r, "품목월일", st.day.strftime("%m%d"))
            put(r, "품목명", ln.name)
            put(r, "규격", ln.spec)
            put(r, "수량", ln.qty)
            put(r, "단가", ln.unit_price)
            put(r, "공급가액", ln.supply)
            put(r, "세액", ln.tax)
            r += 1
    return wb


def write_xls(statements: list[Statement], path: str | Path, memo_text: str = DEFAULT_MEMO_TEXT) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    build_workbook(statements, memo_text).save(str(path))
    return path
