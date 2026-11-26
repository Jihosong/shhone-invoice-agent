"""경리나라 '엑셀일괄등록' 파일(.xls) 생성.

기준 샘플: tests/fixtures/gyeongri/{A_1줄, B_1장3줄, C_2장}.xls — 실제 업로드에 성공한 파일.
- xlwt BIFF .xls, 시트 'Sheet1', 1행 헤더 20열 고정.
- 명세표(Statement) 하나 = 첫 줄에 거래일자~창고 + 품목, 2번째 줄부터는 품목월일~세액만.
- 문자 칸은 텍스트 서식('@'), 수량·단가·공급가액·세액은 숫자. 빈 칸은 셀을 아예 쓰지 않음.
- 공급가액 = 단가 × 수량, 세액 = 공급가액의 10% 내림.
- 품목비고는 비움(명세표에 인쇄되어 고객에게 보임). 공통메모는 config issue.memo_text 그대로
  (명세표마다 첫 줄에만, 이어지는 품목 줄은 비움). '(AI)'로 끝나서 월간 import가 AI 발행을 알아본다.

줄 만들기는 시트 종류별 builder (블록의 한 행 → Line) — 지금은 호닝파이프(pipe)만. 임가공(job)은 나중에
LINE_BUILDERS에 같은 인터페이스로 붙인다.

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


# 시트 kind → (행 → Line). 임가공(job)은 아직 없음 → 그 시트는 발행 대상에서 빠짐
LINE_BUILDERS = {"pipe": pipe_line}


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
