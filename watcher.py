"""구글시트 작업현황표 감시 모듈.

config/sheets.yaml 설정에 따라 시트별 활성 월 탭을 폴링한다.
- 행 지문(hash_columns)으로 변경을 감지해 SQLite rows 테이블에 기록
- 비어 있는 row_id 칸에 ID 부여
- 상태 열(입력중/확정/제외-월말일괄 …) 갱신
- 기존건 규칙: 처음 발견했을 때 이미 작업완료/출고완료가 TRUE인 행은 '기존건-확인필요'로
  고정(견적·발행 대상 아님). 알고 있던 행의 작업완료가 FALSE→TRUE로 바뀐 순간만
  work_end_flipped_at에 기록 → 발행 대상은 이 값이 있는 행뿐.
  기존건 판정은 rows.legacy에 고정 저장하고, 시트 상태는 작성자 우선:
  작성자가 사람 값이면 기존건이어도 상태를 비움(사람처리), 빈칸일 때만 '기존건-확인필요'.
묶음(block) 규칙 (두 시트 공통):
  날짜 칸이 있는 행 = 묶음 첫 행. 거래처(client_resolved)는 이 행의 거래처 칸에서만 해석
  (별칭표 config/client_aliases.yaml > 마스터 정확일치 > 최장 일치, ㈜/(주)/주식회사/공백 무시),
  일치 뒤 남은 글자는 client_note. 동명(같은 이름에 사업자번호 둘 이상)은 별칭 사업자번호 >
  최근 12개월 거래가 있는 번호가 하나뿐이면 그쪽(산출근거 '동명→최근거래 번호') > 보류-동명.
  homonyms(별칭표의 동명 규칙) 이름: 원문의 (주)/(개인) 표시로 확정, 표시 없으면 묶음 전체 '동명확인'
  (기본 쪽으로 단가만, 발행 안 함). 거래처 칸만 고치면 다음 바퀴에 바로 확정.
  manual_clients 표기: 해석 없이 묶음 전체 사람처리, 산출근거에 '[수동처리: 사유]'.
  날짜 없는 행 = 바로 위 묶음의 이어지는 줄 — 거래처 칸은 통째로 client_note.
  item_columns 중 하나라도 있으면 품목 행(견적·발행 대상), 없으면 메모 행(건너뜀, 상태 비움).
  작성자 잠금은 묶음 단위: 묶음 안 어느 행이든(보통 날짜 있는 첫 행, 메모 행 포함) 사람 작성자가 있으면
  묶음 전체 사람처리 — 다른 줄 작성자가 비어 있어도. 기존건-확인필요·명세표발행대기·명세표발행완료(AI) 판정보다 우선.
  서로 다른 작성자가 섞이면(예: 1행 고문, 2행 AI) 사람처리 + WARNING.
  첫 행 거래처를 못 찾으면 묶음 전체 '보류-미등록'.
  로그: 거래처 판정(동명확인·수동처리·미등록 등)·작성자 섞임은 그 묶음의 판정이 바뀔 때만 찍음(매 폴링 반복 안 함).
  '작업완료 체크 → 발행 후보'는 묶음이 발행 조건(작성자 없음·기존건/보류/제외 아님·전 행 작업완료)을 만족할 때만.
  첫 묶음보다 위의 날짜 없는 행은 건너뜀. block_id = 묶음 첫 행의 row_id.
  확정 판정도 묶음 단위: 묶음 last_change = 묶음 안 품목 행 last_change 중 최신.
  quote_settle_min이 안 지났으면 묶음 전체 '입력중', 지났으면 묶음 전체 함께 '확정'.
  한 행이라도 바뀌면(알던 묶음에 행이 추가돼도) 견적 이후 단계 행까지 묶음 전체가 다시 입력중.
  보류-* 행이 하나라도 있으면 제외·사람처리·명세표발행완료(AI) 행을 뺀 묶음 전체를 같은 보류로.
  보류-미등록·보류-동명(같은 이름에 사업자번호 둘 이상)은 매 바퀴 재검사 → 풀리면 자동 해제.
  보류-화면이상은 유지, 사람이 묶음 안 아무 행의 상태 칸을 비우면 묶음 전체 재평가.
  산출근거 칸 맨 앞에 "[거래처(비고) 날짜 n/총줄수]" 묶음 표시 (품목 행 기준 순번) —
  에이전트가 처리하는 행에만 (사람처리·제외-월말일괄 행은 안 건드림).
행 정체성 = row_id(V열 등)만, 변경 여부 = hash_columns 지문만. 행 번호 이동, 묶음 안 순번/총줄수 변화,
  위쪽 행 삭제는 '변경'이 아님 (산출근거 태그 n/m만 갱신). 시트에서 row_id가 사라진 행은 DB status
  '삭제됨'(시트에는 안 씀) → 스냅샷·pricer 대상 아님. 같은 row_id가 다시 나타나면(실행 취소) 복원.
시트 쓰기는 탭당 batch_update 한 번으로 모으되, 행 번호가 아니라 row_id 기준(SheetWriter):
  쓰기 직전 다시 읽어 현재 행 번호로 옮겨 쓰고, 그 사이 삭제·수정된 행에는 쓰지 않음.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import gspread
import yaml
from gspread.utils import rowcol_to_a1
from loguru import logger

import notifier
from config.settings import settings
from feedback import norm_text
from issuer.excel_upload import LineError, block_date
from job_parse import norm as norm_job_text, parse_chidae, parse_dias

SHEETS_YAML = Path(__file__).parent / "config" / "sheets.yaml"
SHEET_LABELS = {"imgagong": "임가공", "pipe": "파이프"}  # 로그 표시용 (yaml에 label 키가 있으면 그것 우선)

ST_TYPING = "입력중"
ST_CONFIRMED = "확정"
ST_QUOTED = "견적완료"
ST_QUOTED_EDIT = "견적완료(사람수정)"   # 견적확인필요 행에서 사람이 단가 칸을 고쳐 확정
ST_QUOTED_OK = "견적완료(사람확인)"     # 가견적 행 상태 칸만 비워 적힌 단가로 확정
QUOTED_STATES = frozenset({ST_QUOTED, ST_QUOTED_EDIT, ST_QUOTED_OK})  # 발행기·watcher는 셋 다 견적완료로 봄
ST_ISSUED = "명세표발행완료(AI)"  # 사람이 업로드 후 작성자 AI → 묶음 마감
ST_PRINTED = "명세표인쇄완료(AI)"  # (2단계용 예약) 발행 후 인쇄까지 끝남 — watcher는 발행완료와 똑같이 유지
ISSUED_STATES = frozenset({ST_ISSUED, ST_PRINTED})
ST_SKIP = "제외-월말일괄"
ST_LEGACY = "기존건-확인필요"
ST_UNREG = "보류-미등록"
ST_HOMONYM = "보류-동명"
ST_SCREEN = "보류-화면이상"
ST_CHECK = "견적확인필요"
ST_HCHECK = "동명확인"  # homonyms 이름인데 법인/개인 표시 없음 → 기본 쪽으로 가견적만, 발행 안 함
ST_DELETED = "삭제됨"  # DB 전용 (시트에 쓰지 않음): 시트에서 row_id가 사라진 행
ST_PENDING = "명세표발행대기"  # 발행 조건 충족 → data/outbox 파일에 담김(또는 담길 예정), 사람이 업로드
ST_PENDING_EDITED = "명세표발행대기-수정됨"  # 파일에 담긴 뒤 행이 수정됨 → 올린 파일과 어긋남, 사람이 봐야 함
# 파일에 담긴 뒤 사람 작성자가 붙음 → 사람처리지만 경리나라 중복 발행 확인 필요. 사람이 상태 칸을 비우면 보통 사람처리
ST_HUMAN_CHECK = "사람처리-명세표중복확인"
# 옛 상태 이름 → 새 이름. 시트에 옛 이름이 남아 있으면 새 이름으로 보고 다음 쓰기에 바꿔 씀 (DB는 ensure_columns가 이전)
LEGACY_STATUS_NAMES = {"발행대기": ST_PENDING, "발행대기-수정됨": ST_PENDING_EDITED, "AI발행": ST_ISSUED,
                       "AI발행-인쇄됨": ST_PRINTED, "사람처리-발행확인": ST_HUMAN_CHECK}
# 상태 칸에 사람이 적는 '확인' 표시 — 상태 칸을 비운 것과 똑같이 취급 (견적확인필요 → 견적완료(사람확인) 등)
HUMAN_OK_WORDS = frozenset({"확인", "ok"})  # casefold 비교: ok / OK / Ok
HOLD_PREFIX = "보류-"
# 산출근거 칸 맨 앞 묶음 표시 "[거래처(비고) 날짜 n/총줄수] "
# 뒤에 붙는 안내 "[동명확인: …]" / "[수동처리: …]" / "[주의: …]" / 발행 파일 "[→ 20261126_1430.xls]"도
# 에이전트 표시로 함께 뗀다
BLOCK_TAG_RE = re.compile(r"^(?:\[(?:[^\[\]]* \d+/\d+|(?:동명확인|수동처리|주의):[^\[\]]*|→ [^\[\]]*)\] ?)+")
ROW_ID_RE = re.compile(r"^[0-9a-f]{8}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rows(
  row_id TEXT PRIMARY KEY, sheet TEXT, tab TEXT, row_number INTEGER,
  fingerprint TEXT, first_seen TEXT, last_change TEXT,
  status TEXT, author TEXT, issued_at TEXT);
-- 발행기 1단계(반자동): data/outbox/에 만든 엑셀일괄등록 파일과 거기 담긴 묶음
CREATE TABLE IF NOT EXISTS issue_files(
  file_name TEXT PRIMARY KEY, created_at TEXT, path TEXT, n_blocks INTEGER, n_lines INTEGER, total INTEGER);
-- block_id PRIMARY KEY → 같은 묶음이 두 파일에 들어갈 수 없음 (DB가 막음)
CREATE TABLE IF NOT EXISTS issue_file_blocks(
  block_id TEXT PRIMARY KEY, file_name TEXT NOT NULL, sheet TEXT, tab TEXT, client TEXT, reg_no TEXT,
  block_date TEXT, n_lines INTEGER, supply INTEGER, tax INTEGER, row_ids TEXT,
  removed_at TEXT, issued_at TEXT);
"""
# issue_file_blocks: 파일에 담긴 뒤 사람 작성자 → 사람처리-명세표중복확인 표시한 때 / 사람이 상태 칸을 비워 확인한 때
FILE_BLOCK_EXTRA = {"human_flag_at": "TEXT", "human_ack_at": "TEXT"}
# 기존 DB에는 없을 수 있는 열 → 없으면 ALTER
EXTRA_COLUMNS = {"work_end_state": "TEXT", "shipped_state": "TEXT", "work_end_flipped_at": "TEXT",
                 "legacy": "INTEGER", "block_id": "TEXT", "client_resolved": "TEXT", "client_note": "TEXT",
                 "deleted_at": "TEXT", "client_reg_no": "TEXT", "item_fp": "TEXT", "watch_vals": "TEXT",
                 "tab_gid": "TEXT"}

# ---------------------------------------------------------------- 견적완료 이후 변경 (임가공)
# 어느 칸이 바뀌었는지 알려고 행마다 이 칸들의 마지막 값을 rows.watch_vals(JSON)에 둔다.
WATCH_SPEC = ("main_msg", "spec_qty", "difficulty")   # D·E·F: 사양
WATCH_STOCK = "stock_removal"                         # H: 연마량/치대
WATCH_NOTES = {"pre_note": "호닝 전 노트"}  # S (옛 T·U 호닝 후 칸은 시트에서 삭제)
WATCH_FIELDS = (*WATCH_SPEC, WATCH_STOCK, *WATCH_NOTES)
# 사람 단가를 유지한 채 재확인을 요청한 표시 (산출근거 맨 앞, 묶음 표시 뒤). 사람이 '확인'/비우면 pricer가
# 견적완료(사람확인), 단가를 고치면 견적완료(사람수정) — 가견적 표시 '[가견적-…]'과 같은 대접
RECHECK_RE = re.compile(r"^\[([^\[\]]*) — 단가 재확인\](?: 사람 단가 [\d,/]+ 유지)?(?: \| |\s*$)")
CHIDAE_DEFAULTS = {"post_quote_recheck": False,         # True면 사람 단가 행의 사양·큰 치대 변경 → 견적확인필요 (기본 꺼짐)
                   "post_quote_chidae_ratio": 0.004,   # |치대| ÷ 구경 이 이상이면 재확인
                   "post_quote_chidae_mm": 1.0,         # 또는 |치대| 이 이상(mm)이면 재확인
                   "post_quote_chidae_nodia_mm": 0.2}   # 구경을 모르면 |치대| 이 이상(mm)이면 재확인
# 산출근거 끝의 변경 표시 "[변경 11/27 14:05: H -0.3 추가]" — 한 행에 최근 CHANGE_TAG_KEEP개까지 누적
CHANGE_TAG_RE = re.compile(r"\[변경 [^\[\]]*\]")
CHANGE_TAIL_RE = re.compile(r"(?:\s*\[변경 [^\[\]]*\])+\s*$")
CHANGE_TAG_KEEP = 3


def _short(v: str, n: int = 20) -> str:
    v = " ".join((v or "").split()).replace("[", "(").replace("]", ")")
    return v[:n] + "…" if len(v) > n else v


def change_desc(letter: str, old: str, new: str) -> str:
    """'H -0.3 추가' / 'H -0.3 삭제' / 'E Ø50 …→Ø50 …'."""
    old, new = _short(old), _short(new)
    if not old:
        return f"{letter} {new} 추가"
    if not new:
        return f"{letter} {old} 삭제"
    return f"{letter} {old}→{new}"


def append_change_tag(body: str, tag: str, keep: int = CHANGE_TAG_KEEP) -> str:
    """산출근거 끝 변경 표시들에 tag를 더하고 최근 keep개만 남김."""
    m = CHANGE_TAIL_RE.search(body)
    tags = CHANGE_TAG_RE.findall(m.group(0)) if m else []
    base = body[:m.start()] if m else body
    return " ".join(x for x in [base.rstrip()] + (tags + [tag])[-keep:] if x)


def _fmt_price(cell_text: str) -> str:
    """'5000' → '5,000', '5000/4000' → '5,000/4,000'."""
    parts = re.sub(r"[\s,]", "", cell_text or "").split("/")
    return "/".join(f"{int(p):,}" if p.isdigit() else p for p in parts)


def _blank_stock(v: str) -> bool:
    return norm_job_text(v) in ("", "일반")


def stock_change(old: str, new: str, spec_text: str, rules: dict) -> tuple[str, bool]:
    """H(연마량/치대) 변경 → (설명, 재확인 필요). 구경(spec_text의 Ø 중 가장 작은 것) 대비 |치대|가
    ratio 이상이거나 mm 이상이면 재확인. 구경을 모르면 nodia_mm 기준. 값을 못 읽으면 재확인. 지우면 설명만."""
    old, new = (old or "").strip(), (new or "").strip()
    if _blank_stock(new):
        return f"연마량 {old} 삭제", False
    desc = f"연마량 {new} 추가" if _blank_stock(old) else f"연마량 {old}→{new}"
    v = parse_chidae(new)
    if v is None:
        return f"{desc}(값 해석 못함)", True
    dias = [d for d in parse_dias(spec_text) if d > 0]
    if dias:
        ratio = v / min(dias)
        need = ratio >= float(rules["post_quote_chidae_ratio"]) - 1e-12 or v >= float(rules["post_quote_chidae_mm"])
        return f"{desc}(구경 대비 {ratio * 100:.1f}%)", need
    return f"{desc}(구경 모름)", v >= float(rules["post_quote_chidae_nodia_mm"]) - 1e-12


def post_quote_states(status_values: list[str]) -> set[str]:
    """'견적완료' 이후 단계로 보는 상태들 — 내용이 그대로면 유지 (제외는 별도 규칙).
    견적완료(사람수정/사람확인)은 status_values에 없어도 포함.
    기존건은 시트 값이 아니라 DB(rows.status)로만 판정한다
    보류-미등록/보류-동명은 매 바퀴 거래처 마스터로 다시 판정 (조건이 풀리면 자동 해제)
    보류-화면이상은 유지 — 사람이 묶음 안 한 행의 상태 칸을 비우면 묶음 전체 재평가
    동명확인은 매 바퀴 다시 판정 (거래처 칸을 고치면 확정으로)"""
    st = list(status_values)
    return (set(st[st.index(ST_QUOTED):]) | QUOTED_STATES) - {ST_SKIP, ST_LEGACY, ST_UNREG, ST_HOMONYM, ST_HCHECK,
                                                              ST_HUMAN_CHECK}


def known_states(status_values: list[str]) -> set[str]:
    """시트 상태 칸에 에이전트가 쓰는 값 전부. 이 밖의 글자(+ 사람 확인 표시 제외)는 모르는 값 → 손대지 않음."""
    return set(status_values) | QUOTED_STATES | {
        ST_TYPING, ST_CONFIRMED, ST_ISSUED, ST_PRINTED, ST_SKIP, ST_LEGACY, ST_UNREG, ST_HOMONYM, ST_SCREEN, ST_HCHECK,
        ST_PENDING, ST_PENDING_EDITED, ST_HUMAN_CHECK, ST_CHECK}


def ensure_columns(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(rows)")}
    for name, typ in EXTRA_COLUMNS.items():
        if name not in have:
            con.execute(f"ALTER TABLE rows ADD COLUMN {name} {typ}")
    have = {r[1] for r in con.execute("PRAGMA table_info(issue_file_blocks)")}
    for name, typ in FILE_BLOCK_EXTRA.items():
        if name not in have:
            con.execute(f"ALTER TABLE issue_file_blocks ADD COLUMN {name} {typ}")
    migrate_status_names(con)
    con.commit()


def migrate_status_names(con: sqlite3.Connection) -> int:
    """DB에 남은 옛 상태 이름(rows.status) → 새 이름. sheet 학습 줄 비고('AI발행')도 함께. 바꾼 행 수."""
    n = 0
    for old, new in LEGACY_STATUS_NAMES.items():
        n += con.execute("UPDATE rows SET status=? WHERE status=?", (new, old)).rowcount
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='lines'").fetchone():
        have = {r[1] for r in con.execute("PRAGMA table_info(lines)")}
        if {"note", "source"} <= have:
            con.execute("UPDATE lines SET note=? WHERE source='sheet' AND note='AI발행'", (ST_ISSUED,))
    if n:
        logger.info("DB 상태 이름 이전 {}행 ({})", n, ", ".join(f"{o}→{v}" for o, v in LEGACY_STATUS_NAMES.items()))
    return n


def bool_state(value: str) -> str:
    """체크박스 셀 값 → 'TRUE'/'FALSE'."""
    return "TRUE" if value.strip().upper() == "TRUE" else "FALSE"


def strip_block_tag(basis: str) -> str:
    """산출근거에서 맨 앞 묶음 표시를 뗀 나머지."""
    return BLOCK_TAG_RE.sub("", basis or "", count=1)


def with_block_tag(tag: str, basis: str) -> str:
    rest = strip_block_tag(basis)
    return f"{tag} {rest}" if rest else tag


def load_config(path: Path = SHEETS_YAML) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def fingerprint(values: list[str]) -> str:
    return hashlib.sha1("|".join(values).encode("utf-8")).hexdigest()


CURRENT_TAB = "당월"  # 이 이름의 탭이 있으면 위치·달과 무관하게 그것을 감시


@dataclass
class TabPick:
    """감시할 탭 선택 결과. error가 있으면 그 시트 처리 중단, warning은 처리하되 경고."""
    ws: object = None
    error: str = ""
    warning: str = ""


def prev_month(month: int) -> int:
    return 12 if month == 1 else month - 1


def pick_active_tab(worksheets, pattern: str, ignore: list[str], today: date | None = None) -> TabPick:
    """① '당월' 탭(위치 무관) ② 오늘 달의 'N월' 탭 ③ 직전 달 'N월' 탭 + 경고 (1월이면 12월).
    같은 단계에서 맞는 탭이 둘 이상이면(' 당월'·'당월', '12월'·' 12월') 오류 → 처리 중단."""
    today = today or date.today()
    rx = re.compile(pattern)
    tabs = [ws for ws in sorted(worksheets, key=lambda w: w.index) if ws.title.strip() not in ignore]

    def one(hit, what):
        if len(hit) > 1:
            names = ", ".join(repr(ws.title) for ws in hit)
            return TabPick(error=f"{what} 탭이 {len(hit)}개 ({names}) → 이 시트 처리 중단 (하나만 남기세요)")
        return TabPick(hit[0]) if hit else None

    def month_of(title):
        t = title.strip()
        m = re.match(r"(\d{1,2})", t) if rx.match(t) else None
        return int(m.group(1)) if m else None

    pick = one([ws for ws in tabs if ws.title.strip() == CURRENT_TAB], f"'{CURRENT_TAB}'")
    if pick:
        return pick
    pick = one([ws for ws in tabs if month_of(ws.title) == today.month], f"당월({today.month}월)")
    if pick:
        return pick
    pm = prev_month(today.month)
    pick = one([ws for ws in tabs if month_of(ws.title) == pm], f"직전 달({pm}월)")
    if pick and pick.ws is not None:
        pick.warning = (f"'{CURRENT_TAB}'·'{today.month}월' 탭 없음 → 직전 달 탭 '{pick.ws.title}' 감시 "
                        f"(새 달 탭을 만들거나 '{CURRENT_TAB}'으로 이름을 바꾸세요)")
    return pick or TabPick()


def date_cell_month(text: str, today: date) -> int | None:
    """날짜 칸 → 달 (해석 못 하면 None). 연도 보정은 block_date와 같이 오늘 기준."""
    if not (text or "").strip():
        return None
    try:
        return block_date(text, "", today).month
    except LineError:
        return None


_CORP_RE = re.compile(r"\(주\)|주식회사|^주\)")


def compact_name(text: str) -> tuple[str, list[int]]:
    """비교용 키: NFKC(㈜→(주))·소문자, 공백·(주)·주식회사 제거.
    반환 (키, 키의 각 글자가 원문 몇 번째 글자에서 왔는지)."""
    chars: list[tuple[str, int]] = []
    for i, ch in enumerate(text or ""):
        for c in unicodedata.normalize("NFKC", ch).lower():
            if not c.isspace():
                chars.append((c, i))
    joined = "".join(c for c, _ in chars)
    drop = {k for m in _CORP_RE.finditer(joined) for k in range(m.start(), m.end())}
    kept = [(c, i) for k, (c, i) in enumerate(chars) if k not in drop]
    return "".join(c for c, _ in kept), [i for _, i in kept]


class NameNormalizer:
    """표기 변환표 (client_aliases.yaml normalize:) — compact_name 키에 차례로 적용.
    시트 표기·마스터 등록명·별칭 모두 같은 변환을 거치므로 'ENG↔이엔지'처럼 양방향인 것도
    한쪽으로 모으면 된다. 영문만으로 된 원표기(ENG 등)는 앞뒤가 영문자가 아닐 때만 바꾼다
    (ENGINEERING 안의 eng는 그대로). 키 글자 → 원문 위치 대응은 바뀐 구간의 마지막 원문 위치로 유지."""

    def __init__(self, pairs: list[tuple[str, str]] | None = None):
        self.rules = []
        for src, dst in pairs or []:
            s, d = compact_name(src)[0], compact_name(dst)[0]
            if not s or s == d:
                continue
            pat = re.escape(s)
            if re.fullmatch(r"[a-z0-9]+", s):
                pat = r"(?<![a-z])" + pat + r"(?![a-z])"
            self.rules.append((re.compile(pat), d))

    def __bool__(self) -> bool:
        return bool(self.rules)

    def apply(self, key: str, idx: list[int]) -> tuple[str, list[int]]:
        for rx, dst in self.rules:
            out, oidx, pos = [], [], 0
            for m in rx.finditer(key):
                out.append(key[pos:m.start()])
                oidx += idx[pos:m.start()]
                out.append(dst)
                oidx += [idx[m.end() - 1]] * len(dst)
                pos = m.end()
            if pos:
                out.append(key[pos:])
                oidx += idx[pos:]
                key, idx = "".join(out), oidx
        return key, idx

    def key(self, text: str) -> tuple[str, list[int]]:
        k, idx = compact_name(text)
        return self.apply(k, idx) if self.rules else (k, idx)


REG_NO_RE = re.compile(r"^\d{3}-\d{2}-\d{5}$")
ALIASES_YAML = Path(__file__).parent / "config" / "client_aliases.yaml"
_warned: set[str] = set()


def warn_once(msg: str) -> None:
    """매 폴링 다시 만드는 마스터의 같은 경고를 프로세스당 한 번만."""
    if msg not in _warned:
        _warned.add(msg)
        logger.warning(msg)


def load_client_rules(path: Path | None) -> dict:
    """config/client_aliases.yaml → {'aliases': {표기: 정식명|번호}, 'homonyms': {이름: {법인, 개인, 기본}},
    'manual': {표기: 사유}, 'normalize': [(원표기, 바꿀 표기)]}. 파일이 없으면 모두 빈 값."""
    data = {}
    if path and Path(path).exists():
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    def strmap(section):
        return {str(k): str(v) for k, v in (data.get(section) or {}).items() if k is not None and v is not None}

    homonyms = {str(k): {str(a): str(b) for a, b in (v or {}).items() if b is not None}
                for k, v in (data.get("homonyms") or {}).items() if isinstance(v, dict) or v is None}
    return {"aliases": strmap("aliases"), "homonyms": homonyms, "manual": strmap("manual_clients"),
            "normalize": [(k, v) for k, v in strmap("normalize").items() if k.strip() and v.strip()]}


_CORP_MARK_RE = re.compile(r"\(주\)|주\)|주식회사")  # NFKC 후 (㈜ → (주))
_INDIV_MARK_RE = re.compile(r"\(\s*개인\s*\)")
NOTE_LEAD_RE = re.compile(r"^\s*(\(주\)|㈜|주식회사|주\)|\(\s*개인\s*\))")


_PAREN_RE = re.compile(r"\(([^()]*)\)")  # NFKC 뒤 — 전각 （ ） 도 ( ) 로


def ceo_key(text: str | None) -> str:
    """대표자명 비교 키: NFKC·소문자, 공백 전부 제거."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).lower()


def client_label(rec) -> str:
    """안내문용 '이름(대표자명)' — 대표자명이 비어 있으면 이름만."""
    return f"{rec.name}({rec.ceo})" if rec.ceo else rec.name


@dataclass
class CeoHit:
    """시트 비고의 괄호 문구가 후보 대표자명과 일치한 결과."""
    recs: list  # 일치한 후보 (사업자번호별 하나) — 둘 이상이면 동명이인
    ceo: str  # 일치한 대표자명 (등록 표기)
    note: str  # 일치한 괄호를 뺀 비고


def ceo_pick(note: str, pool) -> CeoHit | None:
    """비고(note)의 괄호 문구들을 후보(pool)의 대표자명과 하나씩 대조 (공백 무시, ( ) / （ ） 모두).
    일치한 괄호는 식별자로 보고 비고에서 뺌. 어떤 대표자명과도 안 맞는 괄호는 그대로 비고."""
    t = unicodedata.normalize("NFKC", note or "")
    spans, recs, ceo = [], {}, ""
    for m in _PAREN_RE.finditer(t):
        k = ceo_key(m.group(1))
        hit = [c for c in pool if c.ceo and ceo_key(c.ceo) == k] if k else []
        if hit:
            spans.append(m.span())
            ceo = ceo or hit[0].ceo
            for c in hit:
                recs.setdefault(c.reg_no or c.name, c)
    if not spans:
        return None
    rest = "".join(t[a:b] for a, b in zip([0] + [e for _, e in spans], [s for s, _ in spans] + [len(t)]))
    rest = NOTE_LEAD_RE.sub("", re.sub(r"\s+", " ", rest)).strip()
    return CeoHit(list(recs.values()), ceo, rest)


def entity_mark(raw: str) -> str | None:
    """정규화 전 원문의 법인/개인 표시: '법인' / '개인' / None(없음) / '둘다'."""
    t = unicodedata.normalize("NFKC", raw or "")
    corp, indiv = bool(_CORP_MARK_RE.search(t)), bool(_INDIV_MARK_RE.search(t))
    return "둘다" if corp and indiv else "법인" if corp else "개인" if indiv else None


@dataclass
class ClientRecord:
    name: str
    reg_no: str
    weight: float
    last_date: str | None = None
    ceo: str | None = None  # 대표자명 (거래처 목록 import) — 시트 '거래처(대표명)'으로 동명 구분


@dataclass
class Resolution:
    """거래처 해석 결과. hold=None이면 확정(reg_no 있음, 마스터에 번호가 없는 월말일괄 거래처는 빈 값)."""
    name: str | None
    note: str
    reg_no: str | None = None
    hold: str | None = None  # None / ST_UNREG / ST_HOMONYM / ST_HCHECK(동명확인: 기본 쪽 번호로 가견적, 발행 안 함)
    how: str = ""  # 별칭 / 마스터 / 동명규칙 / 수동
    basis: str = ""  # 산출근거 묶음 표시에 붙일 말 (예: '동명→최근거래 140-04-14907')
    candidates: list[ClientRecord] = field(default_factory=list)  # 보류 시 후보 (로그용)
    notice: str = ""  # 산출근거 묶음 표시 뒤 별도 [..] (동명확인 안내)
    manual: str | None = None  # manual_clients 사유 → 묶음 전체 사람처리


class ClientMaster:
    """거래처 해석: 별칭표 > 마스터 정확일치 > 마스터 최장(앞부분) 일치 (긴 쪽 우선,
    같은 길이면 manual_clients > 별칭 > homonyms > 마스터).

    manual_clients: 해석 없이 묶음 전체 사람처리 (사유는 산출근거에).
    homonyms(동명 규칙): 원문에 (주)/주)/㈜/주식회사 → 법인 번호, (개인) → 개인 번호로 확정.
    괄호 안 대표자명이 homonyms에 적힌 후보 중 정확히 한 곳과 일치해도 확정(표시와 같은 순위 — 둘이 다른
    곳을 가리키면 동명확인). homonyms에 안 적힌 번호의 대표자명은 보지 않음(yaml 명시 지정이 우선).
    표시·대표자명 모두 없거나, 대표자명이 두 후보 이상에 같으면(동명이인) '동명확인' — 기본 쪽 번호로
    가견적만, 발행 안 함. 아래 최근거래·보류-동명 규칙은 homonyms에 없는 이름에만.
    별칭에 사업자번호를 적은 표기(예: "금강ENG(김철수)": "000-00-00000")는 무엇보다 우선.

    정규화 키(compact_name)가 같은 등록이 여럿이면(같은 이름의 동명, 또는 '(주)대양상사'/'대양상사'):
    사업자번호가 하나뿐이면 확정, 둘 이상이면 괄호 안 대표자명이 정확히 한 곳과 일치하면 그쪽('대표명 일치 X'),
    대표자명이 둘 이상에 같으면(동명이인) 추측하지 않고 보류. 대표자명 일치가 없을 때만
    최근 12개월(last_date) 거래가 있는 번호가 정확히 하나면 그쪽으로 확정('동명→최근거래 번호'). 아니면 이름이 하나 → 보류-동명,
    표기가 다른 등록 여럿 → 추측하지 않고 보류-미등록(후보는 로그). 별칭에 사업자번호를 적으면 그게 최우선."""

    def __init__(self, entries, aliases: dict[str, str] | None = None, today: date | None = None,
                 homonyms: dict[str, dict] | None = None, manual: dict[str, str] | None = None,
                 normalize: list[tuple[str, str]] | None = None):
        self.norm = NameNormalizer(normalize)  # 표기 변환표 — 별칭보다 먼저 (모든 키에 적용)
        self.by_key: dict[str, list[ClientRecord]] = {}
        self.by_reg: dict[str, ClientRecord] = {}
        self.by_name: dict[str, list[ClientRecord]] = {}
        self.alias: dict[str, tuple[str, str]] = {}  # 키 → ('reg', 번호) | ('name', 정식명)
        self.alias_errors: list[str] = []
        self.homonym: dict[str, tuple[str, dict[str, ClientRecord], str]] = {}  # 키 → (이름, {법인/개인: 등록}, 기본)
        self.manual: dict[str, str] = {}  # 키 → 사유
        today = today or date.today()
        self.cutoff = (today - timedelta(days=365)).isoformat()
        for e in entries:
            rec = ClientRecord(e[0], "", e[1]) if len(e) == 2 else ClientRecord(*e)
            key, _ = self.norm.key(rec.name or "")
            if not key:
                continue
            if rec.weight < 0 and key in self.by_key:  # skip_clients 보충분: 마스터에 이미 있으면 무시
                continue
            rec.reg_no = (rec.reg_no or "").strip()
            self.by_key.setdefault(key, []).append(rec)
            self.by_name.setdefault(rec.name, []).append(rec)
            if rec.reg_no and (rec.reg_no not in self.by_reg or rec.weight > self.by_reg[rec.reg_no].weight):
                self.by_reg[rec.reg_no] = rec
        for alias, target in (aliases or {}).items():
            akey, _ = self.norm.key(alias)
            target = (target or "").strip()
            if not akey or not target:
                self.alias_errors.append(f"별칭 {alias!r}: 빈 값")
            elif REG_NO_RE.match(target):
                if target in self.by_reg:
                    self.alias[akey] = ("reg", target)
                else:
                    self.alias_errors.append(f"별칭 {alias!r}: 사업자번호 {target} 가 마스터에 없음")
            else:
                name = self._find_name(target)
                if name:
                    self.alias[akey] = ("name", name)
                else:
                    self.alias_errors.append(f"별칭 {alias!r}: 등록명 {target!r} 가 마스터에 없음")
        for name, spec in (homonyms or {}).items():
            hkey, _ = self.norm.key(name)
            sides, bad = {}, []
            for side in ("법인", "개인"):
                reg = (spec.get(side) or "").strip()
                if not reg:
                    continue
                if REG_NO_RE.match(reg) and reg in self.by_reg:
                    sides[side] = self.by_reg[reg]
                else:
                    bad.append(f"{side} {reg!r}")
            default = (spec.get("기본") or "").strip()
            if bad:
                self.alias_errors.append(f"동명 {name!r}: 사업자번호 형식 아님/마스터에 없음 ({', '.join(bad)})")
            elif default not in sides:
                self.alias_errors.append(f"동명 {name!r}: 기본 {default!r} 는 법인/개인 중 적힌 쪽이어야 함")
            elif hkey:
                self.homonym[hkey] = (name, sides, default)
        for text, reason in (manual or {}).items():
            mkey, _ = self.norm.key(text)
            if mkey:
                self.manual[mkey] = reason.strip() or "수동처리"
        for msg in self.alias_errors:
            warn_once("client_aliases.yaml " + msg + " → 무시")

    def _find_name(self, target: str) -> str | None:
        """정식명 찾기: 그대로 → NFKC·공백 정리(㈜=(주)) 일치. (정규화 키만 같은 다른 등록은 안 받음)"""
        if target in self.by_name:
            return target
        nt = norm_text(target)
        return next((n for n in self.by_name if norm_text(n) == nt), None)

    def __len__(self) -> int:
        return len(self.by_key)

    def active(self, rec: ClientRecord) -> bool:
        return bool(rec.last_date) and str(rec.last_date) >= self.cutoff

    def _decide(self, cands: list[ClientRecord], note: str, how: str) -> Resolution:
        regs = list(dict.fromkeys(c.reg_no for c in cands if c.reg_no))
        names = list(dict.fromkeys(c.name for c in cands))
        hit = ceo_pick(note, cands)
        if hit:
            note = hit.note  # 대표자명 괄호는 식별자 — 비고에서 뺌
        if len(regs) <= 1:
            best = max(cands, key=lambda c: (bool(c.reg_no), c.weight))
            return Resolution(best.name, note, best.reg_no or None, how=how)
        if hit:
            if len(hit.recs) == 1:
                best = max((c for c in cands if c.reg_no == hit.recs[0].reg_no), key=lambda c: c.weight)
                return Resolution(best.name, note, best.reg_no, how=how, basis=f"대표명 일치 {hit.ceo}",
                                  candidates=cands)
            cands = hit.recs  # 동명이인: 최근거래로 추측하지 않고 보류
            names = list(dict.fromkeys(c.name for c in cands))
            if len(names) == 1:
                return Resolution(names[0], note, None, ST_HOMONYM, how, candidates=cands)
            return Resolution(None, note, None, ST_UNREG, how, candidates=cands)
        active = list(dict.fromkeys(c.reg_no for c in cands if c.reg_no and self.active(c)))
        if len(active) == 1:
            best = max((c for c in cands if c.reg_no == active[0]), key=lambda c: c.weight)
            return Resolution(best.name, note, best.reg_no, how=how, basis=f"동명→최근거래 {best.reg_no}",
                              candidates=cands)
        if len(names) == 1:
            return Resolution(names[0], note, None, ST_HOMONYM, how, candidates=cands)
        return Resolution(None, note, None, ST_UNREG, how, candidates=cands)

    def _homonym(self, entry, text: str, note: str) -> Resolution:
        """homonyms 이름: 주)/(개인) 표시 = 괄호 대표자명 일치 (같은 순위, 후보는 yaml에 적힌 번호만)."""
        name, sides, default = entry
        hit = ceo_pick(note, list(sides.values()))
        if hit:
            note = hit.note
        mark = entity_mark(text)
        by_mark = sides.get(mark) if mark in sides else None
        by_ceo = hit.recs[0] if hit and len(hit.recs) == 1 else None
        if by_ceo and (by_mark is None or by_mark.reg_no == by_ceo.reg_no):
            return Resolution(by_ceo.name, note, by_ceo.reg_no, how="동명규칙", basis=f"대표명 일치 {hit.ceo}")
        if by_mark and not by_ceo:  # 동명이인(대표자명 여럿 일치)이어도 표시가 있으면 표시대로
            return Resolution(by_mark.name, note, by_mark.reg_no, how="동명규칙")
        # 표시·대표자명 없음 / 대표자명이 두 후보에 같음 / 표시와 대표자명이 서로 다른 곳 → 동명확인
        rec = sides[default]
        order = [rec] + [r for s, r in sides.items() if s != default]
        notice = ("동명확인: " + " / ".join(f"{client_label(r)} {r.reg_no}" for r in order)
                  + " → 괄호에 대표명 또는 주)/(개인)을 적어주세요")
        return Resolution(rec.name, note, rec.reg_no, ST_HCHECK, "동명규칙", notice=notice,
                          candidates=list(sides.values()))

    def resolve_full(self, text: str) -> Resolution:
        text = text or ""
        key, idx = self.norm.key(text)
        for n in range(len(key), 0, -1):
            k = key[:n]
            manual, alias, hom, cands = self.manual.get(k), self.alias.get(k), self.homonym.get(k), self.by_key.get(k)
            if not (manual or alias or hom or cands):
                continue
            note = NOTE_LEAD_RE.sub("", text[idx[n - 1] + 1:]).strip()  # '대성정밀(주) 급함', '대양상사(개인)'
            if manual:
                return Resolution(text.strip(), "", how="수동", manual=manual)
            if hom and not alias:
                return self._homonym(hom, text, note)
            if alias:
                kind, target = alias
                if kind == "reg":
                    rec = self.by_reg[target]
                    return Resolution(rec.name, note, rec.reg_no, how="별칭")
                hom_target = self.homonym.get(self.norm.key(target)[0])
                if hom_target:  # 별칭이 homonyms 이름을 가리키면 동명 규칙 (최근거래 규칙 아님)
                    return self._homonym(hom_target, text, note)
                return self._decide(self.by_name[target], note, "별칭")
            return self._decide(cands, note, "마스터")
        return Resolution(None, text.strip(), hold=ST_UNREG)

    def resolve(self, text: str) -> tuple[str | None, str]:
        """(거래처명 또는 None, 일치 뒤 남은 글자 = client_note). 보류-동명이면 이름은 돌려줌."""
        res = self.resolve_full(text)
        return res.name, res.note


class SheetWriter:
    """시트 쓰기 모음 — 행 번호가 아니라 row_id로 묶어 두었다가, 쓰기 직전 시트를 다시 읽어
    현재 행 번호로 옮겨 쓴다.

    읽기(get_all_values)와 batch_update 사이(watcher → pricer 포함 수 초)에 사람이 위쪽 행을
    지우거나 끼워 넣으면, 읽을 때의 행 번호로 쓰는 값(상태·단가·산출근거·row_id)이 엉뚱한 행에
    떨어진다. 그래서:
    - row_id가 있는 행: 다시 읽은 시트에서 그 row_id가 있는 행으로 옮겨 씀. 없어졌으면(삭제) 버림.
    - row_id가 아직 없는/새로 부여하는 행: 같은 행 번호의 내용이 읽을 때와 똑같을 때만 씀.
    - expect_fp(hash_columns 지문)를 주면 다시 읽은 행의 지문이 같을 때만 씀
      (읽은 뒤 내용이 바뀌었으면 낡은 판단을 쓰지 않음 — 다음 바퀴가 다시 판단).
    - 같은 칸을 여러 번 add하면 마지막 값만 씀 — watcher·pricer가 한 writer를 같이 쓰면(agent.py run)
      watcher의 중간 상태('확정')는 pricer의 최종 상태(견적완료(사람확인) 등)에 덮여 시트에 안 써짐.
    """

    def __init__(self, cols: dict, data_start_row: int, hash_columns: list[str], label: str = ""):
        self.cols = cols
        self.data_start_row = data_start_row
        self.hash_columns = hash_columns
        self.label = label
        self.width = max(cols.values())
        # (anchor_id, rownum, snapshot_row, col_name, value, expect_fp)
        self.items: list[tuple] = []
        self.new_ids: set[str] = set()  # 이번 바퀴에 새로 부여해 아직 시트에 없는 row_id

    def __len__(self) -> int:
        return len(self.items)

    def _pad(self, row: list[str]) -> list[str]:
        return list(row) + [""] * (self.width - len(row))

    def fp(self, row: list[str]) -> str:
        row = self._pad(row)
        return fingerprint([row[self.cols[c] - 1].strip() for c in self.hash_columns])

    def add(self, rownum: int, name: str, value, row: list[str], row_id: str | None = None,
            expect_fp: str | None = None) -> None:
        self.items.append((row_id, rownum, self._pad(row), name, value, expect_fp))

    def mark_new_id(self, row_id: str) -> None:
        self.new_ids.add(row_id)

    def resolve(self, fresh: list[list[str]]) -> tuple[list[dict], int]:
        """다시 읽은 값으로 각 쓰기의 현재 행 번호를 찾음 → (batch_update 목록, 버린 건수)."""
        id_col = self.cols["row_id"] - 1
        where: dict[str, int] = {}
        for idx in range(self.data_start_row - 1, len(fresh)):
            rid = (fresh[idx][id_col] if id_col < len(fresh[idx]) else "").strip()
            if ROW_ID_RE.match(rid) and rid not in where:  # 중복이면 위쪽이 주인 (ensure_row_id와 같은 규칙)
                where[rid] = idx + 1
        updates: dict[tuple[int, str], dict] = {}  # (행, 열) → 쓰기. 같은 칸을 여러 번 add하면 마지막 값만
        dropped = 0
        for row_id, rownum, snap_row, name, value, expect_fp in self.items:
            if row_id and row_id not in self.new_ids:
                target = where.get(row_id)
            else:  # 시트에 아직 row_id가 없는 행 → 같은 자리에 같은 내용이 있을 때만
                same = rownum <= len(fresh) and self._pad(fresh[rownum - 1])[:self.width] == snap_row[:self.width]
                target = rownum if same else None
            if target is not None and expect_fp is not None and self.fp(fresh[target - 1]) != expect_fp:
                target = None
            if target is None:
                dropped += 1
                logger.info("sheet={} row={} row_id={} {} 쓰기 보류 — 읽은 뒤 행이 삭제·이동·수정됨 (다음 바퀴에 재판정)",
                            self.label, rownum, row_id or "-", name)
                continue
            if target != rownum:
                logger.info("sheet={} row_id={} 행 이동 {}→{} ({} 쓰기를 새 위치로)",
                            self.label, row_id, rownum, target, name)
            updates.pop((target, name), None)
            updates[(target, name)] = {"range": rowcol_to_a1(target, self.cols[name]), "values": [[value]]}
        return list(updates.values()), dropped

    def flush(self, ws, dry_run: bool = False) -> int:
        """쓰기 직전 다시 읽고 row_id 기준으로 옮겨 batch_update. 실제 쓴 건수 반환."""
        if not self.items or dry_run:
            return 0
        updates, _ = self.resolve(ws.get_all_values())
        if updates:
            ws.batch_update(updates, value_input_option="RAW")
        return len(updates)


def load_client_master(con: sqlite3.Connection, extra_names=(), aliases: dict[str, str] | None = None,
                       today: date | None = None, rules: dict | None = None) -> ClientMaster:
    """DB clients(경리나라 import) + skip_clients(월말일괄 — 마스터에 없을 수 있음) + 별칭표."""
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(clients)")}
        ceo = "ceo_name" if "ceo_name" in have else "NULL"  # 거래처 목록 import 전 DB
        rows = con.execute("SELECT name, COALESCE(reg_no, ''), COALESCE(n_statements, 0), last_date, "
                           f"{ceo} FROM clients").fetchall() if have else []
    except sqlite3.OperationalError:  # import 전이라 테이블 없음
        rows = []
    rules = rules or {}
    return ClientMaster(list(rows) + [(n, "", -1, None, None) for n in extra_names], aliases or rules.get("aliases"),
                        today, rules.get("homonyms"), rules.get("manual"), rules.get("normalize"))


# Google Sheets API 할당량: 읽기·쓰기 각각 프로젝트당 300회/분, 사용자당 60회/분.
# 서비스 계정 하나로 도는 에이전트는 '사용자' 하나 → 실제 한도는 60회/분 (넘으면 HTTP 429).
SHEETS_LIMIT_PER_MIN = 60


class ApiMeter:
    """gspread HTTP 요청 수 세기 (GET=읽기, 그 밖=쓰기). report()는 60초마다 지난 1분 호출 수를 로그로."""

    def __init__(self, window: float = 60.0, clock=time.monotonic):
        self.window = window
        self.clock = clock
        self.calls: list[tuple[float, str]] = []
        self.last_report: float | None = None

    def install(self, gc) -> None:
        http = getattr(gc, "http_client", None)
        if http is None or getattr(http, "_metered", False):
            return
        orig = http.request

        def request(method, *a, **kw):
            self.record("read" if str(method).upper() == "GET" else "write")
            return orig(method, *a, **kw)

        http.request = request
        http._metered = True

    def record(self, kind: str) -> None:
        self.calls.append((self.clock(), kind))

    def per_min(self) -> Counter:
        cut = self.clock() - self.window
        self.calls = [c for c in self.calls if c[0] > cut]
        return Counter(k for _, k in self.calls)

    def report(self, force: bool = False) -> Counter | None:
        now = self.clock()
        if self.last_report is None:
            self.last_report = now  # 첫 1분이 다 찬 뒤부터
            if not force:
                return None
        if not force and now - self.last_report < self.window:
            return None
        self.last_report = now
        c = self.per_min()
        worst = max(c["read"], c["write"])
        level = "WARNING" if worst >= SHEETS_LIMIT_PER_MIN * 0.8 else "INFO"
        logger.log(level, "sheets API 지난 1분 호출 읽기={} 쓰기={} (한도 사용자당 각 {}/분)", c["read"], c["write"],
                   SHEETS_LIMIT_PER_MIN)
        return c


class Watcher:
    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        self.common = cfg["common"]
        self.dry_run = dry_run
        self.gc = gspread.service_account(filename=self.common["credentials_file"])
        self.api = ApiMeter()
        self.api.install(self.gc)
        # True면 process_sheet가 시트 쓰기를 미뤄 두고(snapshots[key]['writer']) pricer가 같은 writer에 더한 뒤
        # flush_writes()로 한 번에 씀 → 중간 상태가 시트에 안 남고, 시트당 다시 읽기도 한 번 줄어듦
        self.defer_flush = False
        Path(settings.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(settings.DB_PATH)
        ensure_columns(self.con)
        # 시트별 마지막 바퀴 결과 (pricer 등 후속 단계가 사용)
        self.snapshots: dict[str, dict] = {}
        self.alias_path: Path | None = ALIASES_YAML  # 매 바퀴 다시 읽음 (고치면 재시작 없이 반영)
        self.post_quote = post_quote_states(self.common["status_values"])
        self._log_state: dict[tuple, tuple | None] = {}

    def _log_on_change(self, ident: tuple, level: str | None, msg: str = "", *args) -> None:
        """같은 판정이면 매 바퀴 반복하지 않음 — ident(시트·묶음·종류)의 판정 문구가 바뀔 때만 로그.
        level=None이면 '해당 없음'으로 기억만 (다시 생기면 다시 찍힘)."""
        state = self.__dict__.setdefault("_log_state", {})
        sig = (level, msg.format(*args)) if level else None
        if state.get(ident) == sig:
            return
        state[ident] = sig
        if level:
            logger.log(level, msg, *args)

    # ------------------------------------------------------------------
    def run_once(self) -> None:
        for key, scfg in self.cfg["sheets"].items():
            try:
                self.process_sheet(key, scfg)
            except Exception:
                logger.exception("sheet={} 처리 실패", key)
        if self.dry_run:
            self.con.rollback()
        else:
            self.con.commit()

    def loop(self, once: bool = False) -> None:
        interval = int(self.common.get("poll_interval_sec", 30))
        while True:
            started = time.monotonic()
            self.run_once()
            if once:
                return
            time.sleep(max(0.0, interval - (time.monotonic() - started)))

    # ------------------------------------------------------------------
    def process_sheet(self, key: str, scfg: dict) -> None:
        label = scfg.get("label") or SHEET_LABELS.get(key, key)
        cols = scfg["columns"]
        books = self.__dict__.setdefault("_books", {})
        sh = books.get(key)
        if sh is None:  # open_by_key = 메타데이터 읽기 1회 → 한 번만 열고, 탭 목록은 매 바퀴 worksheets()로 새로
            sh = books[key] = self.gc.open_by_key(scfg["spreadsheet_id"])
        try:
            tabs = sh.worksheets()
        except Exception:
            books.pop(key, None)
            raise
        today = getattr(self, "today_fn", date.today)()
        pick = pick_active_tab(tabs, self.common["month_tab_pattern"], scfg.get("ignore_tabs") or [], today)
        if pick.error:
            self._log_on_change((key, "tab"), "ERROR", "sheet={} {}", label, pick.error)
            self.notify((key, "tab"), f"[{label}] {pick.error}")
            return
        if pick.ws is None:
            self._log_on_change((key, "tab"), "WARNING", "sheet={} 활성 월 탭 없음 ('{}' 또는 'N월')", label,
                                CURRENT_TAB)
            return
        ws = pick.ws
        if pick.warning:
            self._log_on_change((key, "tab"), "WARNING", "sheet={} {}", label, pick.warning)
            self.notify((key, "tab"), f"[{label}] {pick.warning}")
        else:
            self._log_on_change((key, "tab"), "INFO", "sheet={} 감시 탭 '{}'", label, ws.title)
            self.notify((key, "tab"), None)

        values = ws.get_all_values()
        gid = None if getattr(ws, "id", None) is None else str(ws.id)  # 탭 정체성 (이름이 같아도 다른 탭 구분)
        self.month_start_check(key, label, ws.title, values, scfg, today)
        width = max(cols.values())
        master = load_client_master(self.con, self.common.get("skip_clients") or [],
                                    rules=load_client_rules(getattr(self, "alias_path", None)))
        item_hash_cols = [c for c in scfg["hash_columns"] if c != "client"]
        item_cols = scfg.get("item_columns") or []
        # 견적완료 이후 변경 규칙은 임가공(job)만 — 파이프는 예전처럼 지문이 바뀌면 재견적
        watch_fields = [f for f in WATCH_FIELDS if f in cols] if scfg.get("kind") == "job" else []
        pq_rules = dict(CHIDAE_DEFAULTS)
        if watch_fields:  # pricing 절은 매 바퀴 다시 읽음 (고치면 재시작 없이 반영 — pricer_job과 같음)
            try:
                pricing = load_config().get("pricing") or {}
            except Exception:
                pricing = self.cfg.get("pricing") or {}
            pq_rules.update({k: v for k, v in pricing.items() if k in CHIDAE_DEFAULTS})
        settle = timedelta(minutes=float(self.common["quote_settle_min"]))
        ai = self.common["author_ai_value"]
        skip_clients = [norm_text(c) for c in self.common.get("skip_clients") or []]
        now = datetime.now()
        # 발행 파일에 담긴 묶음: block_id → (파일명, removed_at) — 산출근거 '[→ 파일]' 표시·명세표발행대기-수정됨 판정
        filed_map, human_map = {}, {}
        for bid, fn, rm, hf, ha in self.con.execute(
                "SELECT block_id, file_name, removed_at, human_flag_at, human_ack_at FROM issue_file_blocks"
                " WHERE sheet=?", (key,)):
            filed_map[bid] = (fn, rm)
            human_map[bid] = (hf, ha)  # 사람처리-명세표중복확인 표시한 때 / 사람이 확인(상태 칸 비움)한 때

        writer = SheetWriter(cols, scfg["data_start_row"], scfg["hash_columns"], label)
        stamp = f"{now.month}/{now.day} {now:%H:%M}"  # 변경 표시 '[변경 11/27 14:05: …]'
        known = known_states(self.common["status_values"])
        counts = Counter()
        seen_ids: set[str] = set()
        snap_rows: list[dict] = []
        self.snapshots[key] = {"ws": ws, "values": values, "rows": snap_rows, "label": label}
        if getattr(self, "defer_flush", False):
            self.snapshots[key]["writer"] = writer  # pricer가 같은 writer에 더함 → flush_writes()

        def cell(row: list[str], name: str) -> str:
            return row[cols[name] - 1].strip()

        def ensure_row_id(rownum: int, row: list[str]) -> str:
            row_id = cell(row, "row_id")
            # 비었거나, 형식이 아니거나(다른 값이 들어 있음), 행 복사로 중복된 경우 새로 부여
            if not ROW_ID_RE.match(row_id) or row_id in seen_ids:
                if row_id:
                    why = "중복" if row_id in seen_ids else "형식 아님"
                    logger.warning("sheet={} row={} row_id={!r} {} → 새로 부여", label, rownum, row_id, why)
                row_id = uuid.uuid4().hex[:8]
                writer.mark_new_id(row_id)
                writer.add(rownum, "row_id", row_id, row, row_id=row_id)
            seen_ids.add(row_id)
            return row_id

        if not len(master):
            logger.warning("sheet={} 거래처 마스터 비어 있음 → 모든 묶음 보류-미등록 (agent.py import 필요)", label)

        def finish_block(blk: dict | None) -> None:
            """묶음 단위 상태 결정: 묶음 last_change(행들 중 최신) 기준으로 함께 입력중/확정.
            우선순위(제외 > 사람처리 > 보류 > 기존건 > 일반)는 행별 decide_status 그대로,
            그 뒤 보류 행이 하나라도 있으면 (제외·사람처리·명세표발행완료(AI) 행을 뺀) 묶음 전체를 보류로.
            보류 우선순위: 미등록 > 동명 > 화면이상.
            manual_clients 묶음: 해석·판정 없이 전체 사람처리, 산출근거에 '[수동처리: 사유]'.
            동명확인 묶음(homonyms 이름, 법인/개인 표시 없음): 확정 이후 단계(명세표발행완료(AI) 제외) → '동명확인'.
            pricer가 기본 쪽 번호로 단가만 적고 상태는 동명확인 그대로 (발행 대상 아님).
            동명확인이던 묶음에서 거래처 칸만 고쳐 확정되면(품목 칸 지문 그대로) 안정화 대기 없이 바로 확정."""
            if not blk or not blk["entries"]:
                return
            entries = blk["entries"]
            where = f"sheet={label} row={blk['rownum']}"
            if blk["client_log"]:
                self._log_on_change((key, blk["id"], "client"), "INFO", where + " " + blk["client_log"][0],
                                    *blk["client_log"][1:])
            else:
                self._log_on_change((key, blk["id"], "client"), None)
            # 작성자 잠금 (묶음 단위): 묶음 안 어느 행이든(메모 행 포함) 사람 작성자가 있으면 묶음 전체 사람처리.
            # 2번째 줄 이후 작성자가 비어 있어도 마찬가지 — 기존건·명세표발행대기·명세표발행완료(AI) 판정도 사람 것으로 봄.
            filled = [(n, a) for n, a in blk["authors"] if a]
            human = next((a for _, a in filled if a != ai), None)
            if len({a for _, a in filled}) > 1:
                self._log_on_change((key, blk["id"], "authors"), "WARNING",
                                    "{} client={} 묶음 안 작성자가 섞임 ({}) → 묶음 전체 사람처리", where,
                                    entries[0]["client_log"], ", ".join(f"{n}행 {a}" for n, a in filled))
            else:
                self._log_on_change((key, blk["id"], "authors"), None)
            if human:
                for e in entries:
                    if e["author"] != human:
                        e["author"] = human
                        self.con.execute("UPDATE rows SET author=? WHERE row_id=?", (human, e["row_id"]))
            block_last = max(e["last_change"] for e in entries)
            # 견적완료 이후 변경 (임가공): 견적완료·명세표발행대기·발행완료 행의 D·E·F·H·S 변경은 재견적 없이
            # 산출근거 끝에 '[변경 …]'만. 그 행의 변경은 그 행으로 끝 — 묶음 재견적 안 함
            for e in entries:
                e["pq"] = None
                e["final"] = watch_fields and self.post_quote_final(
                    e["status"], cell(e["row"], "basis") if "basis" in cols else "", pq_rules)
                if e["final"] and not e["unknown_status"]:
                    e["pq"] = self.post_quote_change(e, cols, cell(e["row"], "unit_price") if "unit_price" in cols
                                                     else "", pq_rules, stamp)
                if e["pq"]:
                    logger.info("sheet={} row={} client={} 견적 이후 변경 ({}) {} → {}", label, e["rownum"],
                                e["client_log"], e["status"], ", ".join(e["pq"]["changes"]),
                                "견적확인필요 (사람 단가 유지: " + ", ".join(e["pq"]["reasons"]) + ")"
                                if e["pq"]["recheck"] else "단가·상태 유지, 산출근거에 표시")
                    if e["pq"]["recheck"]:
                        self.sync_quote_fp(e["row_id"], e["fp"])
            # 묶음 안 한 행이라도 바뀌었거나, 알던 묶음에 새 행이 붙으면 묶음 전체가 '변경'
            news = [e["is_new"] for e in entries]
            block_changed = any(e["changed"] and not e["pq"] for e in entries) or (any(news) and not all(news))
            # 보류-화면이상이던 행의 상태 칸을 사람이 비웠으면 → 묶음 안 보류-화면이상 모두 풀고 재평가
            screen_cleared = any(e["db_status"] == ST_SCREEN and not e["status"] for e in entries)
            if screen_cleared:
                logger.info("sheet={} row={} client={} 보류-화면이상 해제 → 묶음 재평가",
                            label, blk["rownum"], entries[0]["client_log"])
            homonym = blk["hold"] == ST_HOMONYM
            hcheck = blk["hold"] == ST_HCHECK
            # 동명확인 → 거래처 칸만 고쳐 확정된 경우: 바뀐 건 이름뿐이니 안정화 대기 없이 재판정
            client_fix = (blk["hold"] is None and not blk["manual"] and not any(news)
                          and any(e["db_status"] == ST_HCHECK for e in entries)
                          and not any(e["items_changed"] for e in entries))
            if client_fix:
                logger.info("sheet={} row={} 거래처 수정 {!r} → {} {} (동명확인 해제)",
                            label, blk["rownum"], blk["client_text"], blk["client"], blk["reg_no"])
            filed = filed_map.get(blk["id"])  # (파일명, removed_at) | None
            for e in entries:
                if blk["manual"]:
                    e["new_status"] = None  # 사람처리
                    continue
                status = "" if screen_cleared and e["status"] == ST_SCREEN else e["status"]
                row_changed = block_changed
                if e["final"]:
                    if status in (ST_PENDING_EDITED, *ISSUED_STATES) or (status == ST_PENDING and filed):
                        # 파일에 담긴 명세표발행대기·발행완료: 예전 '-수정됨'/'발행후수정' 규칙 그대로
                        row_changed = block_changed or e["changed"]
                    else:  # 견적완료(·파일 전 명세표발행대기): 다른 행이 바뀌어도, 자기 칸이 바뀌어도 그대로
                        row_changed = e["changed"] and not e["pq"]
                row_changed = row_changed and not client_fix
                new_status = self.decide_status(
                    label, e["rownum"], e["client_log"], e["author"], status, row_changed,
                    settle if client_fix else now - block_last, settle, ai, skip_clients, legacy=e["legacy"],
                    unregistered=blk["client"] is None or blk["hold"] == ST_UNREG, homonym=homonym,
                    filed=filed is not None)
                if hcheck and new_status and new_status not in (ST_TYPING, ST_SKIP, ST_LEGACY, ST_PENDING,
                                                                ST_PENDING_EDITED, *ISSUED_STATES) \
                        and not new_status.startswith(HOLD_PREFIX):
                    new_status = ST_HCHECK
                if e["pq"] and e["pq"]["recheck"] and new_status in (*QUOTED_STATES, ST_CHECK):
                    new_status = ST_CHECK  # 사람 단가 유지 + 재확인 요청
                e["new_status"] = new_status
            # 파일에 담긴 명세표발행대기 묶음은 보류로 덮지 않음 (올린 파일과의 관계가 우선)
            keep = {None, ST_SKIP, ST_PENDING_EDITED, *ISSUED_STATES} | ({ST_PENDING} if filed else set())
            holds = [e["new_status"] for e in entries if (e["new_status"] or "").startswith(HOLD_PREFIX)]
            if holds:
                hold = next((h for h in (ST_UNREG, ST_HOMONYM, ST_SCREEN) if h in holds), holds[0])
                for e in entries:
                    if e["new_status"] not in keep:
                        e["new_status"] = hold
            # 파일에 담긴 뒤 묶음이 수정됨 → 묶음 전체 명세표발행대기-수정됨 (새로 붙은 행 포함), 파일에서 뺀 것으로 기록
            if filed and any(e["new_status"] == ST_PENDING_EDITED for e in entries):
                for e in entries:
                    if e["new_status"] not in (None, ST_SKIP, *ISSUED_STATES):
                        e["new_status"] = ST_PENDING_EDITED
                if not filed[1]:
                    logger.warning("sheet={} row={} client={} 명세표발행대기 중 수정 → 명세표발행대기-수정됨 ({}에서 뺌 — "
                                   "이미 올렸다면 경리나라 명세표와 어긋남, 확인 필요)",
                                   label, blk["rownum"], entries[0]["client_log"], filed[0])
                    self.con.execute("UPDATE issue_file_blocks SET removed_at=? WHERE block_id=?",
                                     (now_iso(), blk["id"]))
                    filed = filed_map[blk["id"]] = (filed[0], now_iso())
            if any(e["just_flipped"] for e in entries):
                why = self.issue_block_reason(blk, entries, human, ai)
                if why:
                    logger.debug("{} client={} 작업완료 체크 (발행 후보 아님: {})", where, entries[0]["client_log"], why)
                else:
                    logger.info("{} client={} 작업완료 체크 → 발행 후보", where, entries[0]["client_log"])
            # 파일에 담긴 뒤 사람 작성자가 붙음 → 사람처리이되 상태 '사람처리-명세표중복확인' + 산출근거 주의 + WARNING.
            # 사람이 묶음 안 아무 행의 상태 칸을 비우면 확인한 것으로 보고 보통 사람처리 (다시 표시 안 함).
            caution = ""
            human_acked = False
            if filed and human and not blk["manual"]:
                flag_at, ack_at = human_map.get(blk["id"], (None, None))
                if not ack_at and flag_at and any(e["db_status"] == ST_HUMAN_CHECK and not e["status"]
                                                  for e in entries):
                    ack_at = now_iso()
                    self.con.execute("UPDATE issue_file_blocks SET human_ack_at=? WHERE block_id=?",
                                     (ack_at, blk["id"]))
                    logger.info("{} client={} 사람처리-명세표중복확인 해제 (상태 칸 비움) → 사람처리", where,
                                entries[0]["client_log"])
                if ack_at:
                    human_acked = True
                else:
                    if not flag_at:
                        self.con.execute("UPDATE issue_file_blocks SET human_flag_at=? WHERE block_id=?",
                                         (now_iso(), blk["id"]))
                        logger.warning("{} client={} 파일 {}에 담긴 뒤 작성자 변경({}) → 사람처리-명세표중복확인 "
                                       "(경리나라 중복 발행 확인 필요)", where, entries[0]["client_log"], filed[0], human)
                    human_map[blk["id"]] = (flag_at or now_iso(), None)
                    caution = f" [주의: 파일 {filed[0]}에 담긴 뒤 작성자 변경 → 경리나라 중복 발행 확인]"
                    for e in entries:
                        e["new_status"] = ST_HUMAN_CHECK
            file_mark = ""
            if filed:
                file_mark = f" [→ {filed[0]}" + (" 뺌(수정됨)]" if filed[1] else "]")

            tag_client = blk["client"] or blk["client_text"] or "?"
            note = blk["client_note"]
            if blk["client"] and note:  # '오성기계공구(택배)' 처럼 이미 괄호면 그대로
                tag_client += note if note.startswith("(") and note.endswith(")") else f"({note})"
            for pos, e in enumerate(entries, 1):
                rownum, row_id, row = e["rownum"], e["row_id"], e["row"]
                extra = f" {blk['basis']}" if blk["basis"] else ""  # '동명→최근거래 번호'
                if blk["manual"]:
                    tag = f"[수동처리: {blk['manual']}]"
                else:
                    tag = f"[{tag_client}{extra} {blk['date']} {pos}/{len(entries)}]"
                    if hcheck:
                        tag += f" [{blk['notice']}]"
                    tag += file_mark + caution
                # 묶음 표시는 에이전트가 처리하는 행에만 — 사람처리·제외-월말일괄 행의 산출근거는 안 건드림
                # (manual_clients 묶음은 사람처리지만 사유를 적음)
                if "basis" in cols and (e["new_status"] not in (None, ST_SKIP) or blk["manual"]):
                    basis = cell(row, "basis")
                    rest = strip_block_tag(basis)
                    if e["pq"]:
                        rest = e["pq"]["body"](rest)
                    new_basis = with_block_tag(tag, rest)
                    if new_basis != basis:  # 태그 n/m 갱신은 '변경'이 아님 — 지문(hash_columns)에 없는 칸
                        writer.add(rownum, "basis", new_basis, row, row_id=row_id, expect_fp=e["fp"])
                elif "basis" in cols and human_acked:
                    # 사람이 확인한 사람처리-명세표중복확인: 에이전트 표시([주의: …] 포함)만 떼고 사람 글은 그대로
                    basis = cell(row, "basis")
                    rest = strip_block_tag(basis)
                    if rest != basis and "[주의:" in basis[:len(basis) - len(rest)]:
                        writer.add(rownum, "basis", rest, row, row_id=row_id, expect_fp=e["fp"])
                new_status = e["new_status"]
                if e["unknown_status"]:  # 모르는 글자: 상태 칸·DB 상태 그대로, 견적·발행 단계로도 안 넘어감
                    counts["모르는상태"] += 1
                    new_status = e["status_raw"]
                elif new_status is None:  # 사람 처리
                    counts["사람처리"] += 1
                    new_status = ""
                elif new_status == ST_SKIP:
                    counts["제외"] += 1
                else:
                    counts[new_status] += 1
                if new_status != e["status_raw"]:  # '확인'/'ok'는 비운 것으로 판정했어도 칸에는 남아 있음
                    writer.add(rownum, "status", new_status, row, row_id=row_id, expect_fp=e["fp"])
                if not e["unknown_status"]:
                    self.con.execute("UPDATE rows SET status=? WHERE row_id=?", (new_status, row_id))
                snap_rows.append({"rownum": rownum, "row_id": row_id, "row": row, "fp": e["fp"], "status": new_status,
                                  "author": e["author"], "legacy": e["legacy"],
                                  "work_end_flipped_at": e["flipped_at"], "block_id": blk["id"],
                                  "client_resolved": blk["client"], "client_reg_no": blk["reg_no"],
                                  "client_note": e["client_note"],
                                  "block_date": blk["date"], "block_label": tag_client,
                                  "needs_quote": e["new_status"] == ST_HCHECK and e["status"] != ST_HCHECK,
                                  "block_tag": tag})

        block: dict | None = None
        for idx in range(scfg["data_start_row"] - 1, len(values)):
            rownum = idx + 1
            row = values[idx] + [""] * (width - len(values[idx]))
            client_text = cell(row, "client")
            if cell(row, "date"):  # 묶음 첫 행
                finish_block(block)
                res = master.resolve_full(client_text) if client_text else Resolution(None, "", hold=ST_UNREG)
                client_note = res.note
                resolved = None if res.hold == ST_UNREG or res.manual else res.name
                block = {"rownum": rownum, "row": row, "client": resolved, "authors": [],
                         "id": None, "client_log": None, "date": cell(row, "date"), "client_text": client_text,
                         "client_note": client_note, "entries": [], "reg_no": res.reg_no, "hold": res.hold,
                         "basis": res.basis, "notice": res.notice, "manual": res.manual}
                counts["묶음"] += 1
                cands = " | ".join(f"{c.name} {c.reg_no or '-'} 최근{c.last_date or '-'}" for c in res.candidates)
                # 거래처 판정 로그는 finish_block에서 판정이 바뀔 때만 (매 폴링 반복 금지)
                if res.manual:
                    block["client_log"] = ("거래처 {!r} 수동처리 ({}) → 묶음 사람처리", client_text, res.manual)
                elif res.hold == ST_HCHECK:
                    block["client_log"] = ("거래처 {!r} {}", client_text, res.notice)
                elif client_text and res.hold == ST_UNREG:
                    if res.candidates:
                        block["client_log"] = ("거래처 후보 여럿 {!r} → 보류-미등록 (후보: {})", client_text, cands)
                    else:
                        block["client_log"] = ("거래처 미등록 {!r}", client_text)
                elif res.basis:
                    block["client_log"] = ("거래처 {!r} → {} {} (후보: {})", client_text, res.name, res.basis, cands)
            elif block is None:
                continue  # 첫 묶음보다 위의 날짜 없는 행
            else:  # 이어지는 줄: 거래처 칸은 해석하지 않고 통째로 메모
                client_note = client_text
            block["authors"].append((rownum, cell(row, "author")))  # 메모 행 포함 — 작성자 잠금은 묶음 단위

            if item_cols and not any(cell(row, c) for c in item_cols):
                # 메모 행: 건너뜀, 상태 비움
                if client_text:
                    counts["메모"] += 1
                if cell(row, "status"):
                    rid = cell(row, "row_id")
                    writer.add(rownum, "status", "", row, row_id=rid if ROW_ID_RE.match(rid) else None)
                continue

            if block["id"] is None:
                block["id"] = ensure_row_id(block["rownum"], block["row"])
            row_id = block["id"] if block["rownum"] == rownum else ensure_row_id(rownum, row)
            client = block["client"]
            client_log = client or client_text

            counts["rows"] += 1
            fp = fingerprint([cell(row, c) for c in scfg["hash_columns"]])
            item_fp = fingerprint([cell(row, c) for c in item_hash_cols])  # 거래처 칸 뺀 지문
            author = cell(row, "author")  # 묶음 작성자 잠금은 finish_block에서 (묶음 안 어느 행이든 사람 → 전체)
            status_raw = status = cell(row, "status")
            unknown_status = False
            if status in LEGACY_STATUS_NAMES:  # 옛 이름 → 새 이름으로 판정, 칸은 이번 쓰기에 새 이름으로 바뀜
                status = LEGACY_STATUS_NAMES[status]
                self._log_on_change(("legacy_name", key, row_id), "INFO", "sheet={} row={} 상태 옛 이름 {!r} → {!r}",
                                    label, rownum, status_raw, status)
            if status.casefold() in HUMAN_OK_WORDS:
                status = ""  # 사람이 '확인'/'ok'라고 씀 = 상태 칸 비움
                self._log_on_change(("human_ok", key, row_id), "INFO", "sheet={} row={} client={} 상태 칸 {!r} → "
                                    "상태 칸 비움과 같이 처리 (사람 확인)", label, rownum, client_log, status_raw)
            elif status and status not in known:
                unknown_status = True  # 모르는 글자 → 상태 칸 그대로 두고 처리 보류
                self._log_on_change(("unknown_status", key, row_id), "WARNING",
                                    "sheet={} row={} client={} 상태 칸에 모르는 값 {!r} → 건드리지 않음 "
                                    "(확인은 '확인' 입력 또는 비우기)", label, rownum, client_log, status_raw)
            else:
                self._log_on_change(("human_ok", key, row_id), None)
            if not unknown_status:
                self._log_on_change(("unknown_status", key, row_id), None)

            work_end = bool_state(cell(row, "work_end")) if "work_end" in cols else "FALSE"
            just_flipped = False
            shipped = bool_state(cell(row, "shipped")) if "shipped" in cols else "FALSE"

            # --- DB 갱신 ---
            rec = self.con.execute(
                "SELECT fingerprint, last_change, status, work_end_state, work_end_flipped_at, legacy, item_fp,"
                " watch_vals FROM rows WHERE row_id=?",
                (row_id,)).fetchone()
            if rec is None:
                changed = False  # 최초 발견은 '변경'으로 보지 않음
                items_changed = True
                last_change = now
                legacy = "TRUE" in (work_end, shipped)
                flipped_at = None
                self.con.execute(
                    "INSERT INTO rows(row_id, sheet, tab, row_number, fingerprint, first_seen, last_change, status, author)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (row_id, key, ws.title, rownum, fp, now_iso(), now_iso(), status, author))
            else:
                db_fp, db_last, db_status, prev_work_end, flipped_at, db_legacy, db_item_fp, db_watch = rec
                # 정체성은 row_id, 변경 여부는 hash_columns 지문으로만 — 행 번호·묶음 위치·n/m은 보지 않음
                changed = db_fp != fp
                items_changed = db_item_fp != item_fp  # 예전 DB(값 없음)면 변경으로 봄
                if db_status == ST_DELETED:
                    logger.info("sheet={} row={} row_id={} client={} 삭제됨 → 다시 나타남 (복원)",
                                label, rownum, row_id, client_log)
                    db_status = None
                    self.con.execute("UPDATE rows SET deleted_at=NULL WHERE row_id=?", (row_id,))
                last_change = now if changed else datetime.fromisoformat(db_last)
                if db_legacy or db_status == ST_LEGACY:
                    # 한 번 기존건이면 계속 (지문·작성자가 바뀌어도). 상태는 작성자에 따라 비어 있을 수
                    # 있으므로 rows.legacy로 판정; status 비교는 legacy 열 도입 전 DB 호환용
                    legacy = True
                elif prev_work_end is None:
                    # 이 규칙 이전에 발견된 행: 이전 상태를 모르므로 최초 발견처럼 판정 (이미 발행된 건 제외)
                    legacy = db_status not in ISSUED_STATES and "TRUE" in (work_end, shipped)
                else:
                    legacy = False
                    if prev_work_end == "FALSE" and work_end == "TRUE":
                        flipped_at = now_iso()
                        just_flipped = True  # 로그는 finish_block에서 — 묶음이 발행 조건을 만족할 때만 '발행 후보'
                    elif work_end == "FALSE" and flipped_at and db_status not in ISSUED_STATES:
                        flipped_at = None  # 체크 해제 → 발행 후보에서 빠짐
                self.con.execute(
                    "UPDATE rows SET sheet=?, tab=?, row_number=?, fingerprint=?, last_change=?, author=? WHERE row_id=?",
                    (key, ws.title, rownum, fp, last_change.isoformat(timespec="seconds"), author, row_id))
            if legacy:
                flipped_at = None  # 기존건은 절대 발행 대상 아님
            # 견적완료 이후 변경 처리용: 지난 바퀴 값과 비교해 어느 칸이 바뀌었는지 (None = 예전 DB라 모름)
            watch_now = {f: cell(row, f) for f in watch_fields}
            try:
                watch_prev = json.loads(db_watch) if rec is not None and db_watch else None
            except ValueError:
                watch_prev = None
            watch_diff = None if watch_prev is None else {
                f: (watch_prev.get(f, ""), v) for f, v in watch_now.items() if watch_prev.get(f, "") != v}
            self.con.execute(
                "UPDATE rows SET work_end_state=?, shipped_state=?, work_end_flipped_at=?, legacy=?,"
                " block_id=?, client_resolved=?, client_reg_no=?, client_note=?, item_fp=?, watch_vals=?,"
                " tab_gid=? WHERE row_id=?",
                (work_end, shipped, flipped_at, int(legacy), block["id"], client, block["reg_no"], client_note,
                 item_fp, json.dumps(watch_now, ensure_ascii=False) if watch_fields else None, gid, row_id))

            # 상태는 묶음이 끝날 때 함께 결정 (finish_block)
            block["entries"].append({
                "rownum": rownum, "row_id": row_id, "row": row, "author": author, "status": status,
                "status_raw": status_raw, "unknown_status": unknown_status,
                "work_end": work_end, "just_flipped": just_flipped,
                "fp": fp, "changed": changed, "items_changed": items_changed, "is_new": rec is None, "db_status": db_status if rec else None, "last_change": last_change, "legacy": legacy,
                "flipped_at": flipped_at, "client_log": client_log, "client_note": client_note,
                "watch_diff": watch_diff})
        finish_block(block)
        self.mark_deleted(key, label, ws.title, values, cols, scfg["data_start_row"], seen_ids, gid)

        deferred = getattr(self, "defer_flush", False)
        written = 0 if deferred else writer.flush(ws, self.dry_run)

        extra = [f"{k}={v}" for k, v in counts.items()
                 if k not in ("rows", "묶음", "메모", ST_TYPING, ST_CONFIRMED, "제외", "사람처리")]
        logger.info(
            "sheet={} tab={} 묶음={} 품목={} 메모={} 입력중={} 확정={} 제외={} 사람처리={}{}{}",
            label, ws.title, counts["묶음"], counts["rows"], counts["메모"], counts[ST_TYPING], counts[ST_CONFIRMED],
            counts["제외"], counts["사람처리"],
            (" " + " ".join(extra)) if extra else "",
            f" (dry-run, 미기록 쓰기 {len(writer)}건)" if self.dry_run
            else f" 쓰기 대기={len(writer)} (pricer와 함께 씀)" if deferred
            else f" 쓰기={written}" + (f" (보류 {len(writer) - written})" if written != len(writer) else ""),
        )

    def month_start_check(self, key, label, title, values, scfg, today: date) -> None:
        """월초 점검: 감시 탭에 오늘 달 날짜 행이 없고 지난달 행만 있으면 매 폴링 WARNING (알림은 상태가 바뀔 때)."""
        col = scfg["columns"]["date"] - 1
        months = {m for row in values[scfg["data_start_row"] - 1:]
                  if col < len(row) and (m := date_cell_month(row[col], today))}
        ident = (key, "month_start")
        if today.month not in months and prev_month(today.month) in months:
            msg = f"당월 탭 갱신 필요(탭 '{title}'에 {today.month}월 행 없음)"
            logger.warning("sheet={} {}", label, msg)
            self.notify(ident, f"[{label}] {msg}")
        else:
            self.notify(ident, None)

    def notify(self, ident: tuple, msg: str | None) -> None:
        """알림은 같은 내용을 반복하지 않음 — ident의 문구가 바뀔 때만 notifier로 (None = 해소, 기억만)."""
        sent = self.__dict__.setdefault("_notified", {})
        if sent.get(ident) == msg:
            return
        sent[ident] = msg
        if msg:
            notifier.notify(msg)

    def flush_writes(self) -> int:
        """defer_flush 모드: 이번 바퀴 watcher+pricer 쓰기를 시트당 batch_update 한 번으로. 쓴 칸 수 반환."""
        total = 0
        for key, snap in self.snapshots.items():
            writer = snap.pop("writer", None)
            if writer is None:
                continue
            try:
                written = writer.flush(snap["ws"], self.dry_run)
            except Exception:
                logger.exception("sheet={} 쓰기 실패", snap.get("label", key))
                continue
            total += written
            if len(writer) and not self.dry_run:
                logger.info("sheet={} 쓰기={}{}", snap.get("label", key), written,
                            f" (요청 {len(writer)}, 같은 칸 덮어씀·보류 {len(writer) - written})"
                            if written != len(writer) else "")
        return total

    @staticmethod
    def issue_block_reason(blk: dict, entries: list[dict], human: str | None, ai: str) -> str | None:
        """작업완료 체크된 묶음이 발행 후보가 아닌 이유 (None = 후보: 견적만 끝나면 명세표발행대기로 감)."""
        if blk["manual"]:
            return "수동처리"
        if human:
            return f"사람처리 (작성자 {human})"
        if any(e["legacy"] for e in entries):
            return "기존건"
        for e in entries:
            st = e["new_status"]
            if e.get("unknown_status"):
                return f"상태 칸 모르는 값 {e['status_raw']!r}"
            if st in (ST_SKIP, ST_HCHECK, ST_PENDING, ST_PENDING_EDITED, *ISSUED_STATES) or (st or "").startswith(HOLD_PREFIX):
                return st
            if e["author"]:
                return f"작성자 {e['author']}"
        if not all(e["work_end"] == "TRUE" for e in entries):
            return "다른 줄 작업완료 대기"
        return None

    def mark_deleted(self, key, label, tab, values, cols, data_start_row, seen_ids, gid=None) -> None:
        """이 탭에서 row_id가 더 이상 보이지 않는 행 → DB status '삭제됨' (이후 처리 안 함).
        메모 행으로 바뀌었어도 V열에 row_id가 남아 있으면 삭제가 아님. 다시 나타나면 process_sheet가 복원.
        같은 이름의 다른 탭('당월'을 '12월'로 바꾸고 새 '당월'을 만든 경우)의 행은 건드리지 않음 — 탭 gid로 구분."""
        id_col = cols["row_id"] - 1
        present = set(seen_ids)
        for row in values[data_start_row - 1:]:
            rid = (row[id_col] if id_col < len(row) else "").strip()
            if ROW_ID_RE.match(rid):
                present.add(rid)
        gone = [rid for (rid,) in self.con.execute(
            "SELECT row_id FROM rows WHERE sheet=? AND tab=? AND COALESCE(status, '') != ?"
            " AND (tab_gid IS NULL OR tab_gid = ?)",
            (key, tab, ST_DELETED, gid)) if rid not in present]
        if not gone:
            return
        self.con.executemany("UPDATE rows SET status=?, deleted_at=? WHERE row_id=?",
                             [(ST_DELETED, now_iso(), rid) for rid in gone])
        logger.info("sheet={} tab={} 시트에서 사라진 행 {}개 → 삭제됨 ({})", label, tab, len(gone),
                    ", ".join(gone[:10]) + (" …" if len(gone) > 10 else ""))

    @staticmethod
    def human_priced(status: str, basis: str) -> bool:
        """사람이 확정한 단가가 적힌 행: 견적완료(사람수정/사람확인), 또는 그 단가를 유지한 채 재확인 중인 견적확인필요."""
        return status in (ST_QUOTED_EDIT, ST_QUOTED_OK) or (
            status == ST_CHECK and bool(RECHECK_RE.match(strip_block_tag(basis))))

    def post_quote_final(self, status: str, basis: str, rules: dict) -> bool:
        """재견적하지 않고 변경 표시만 하는 행: 견적완료(자동·사람확인·사람수정)·명세표발행대기(-수정됨)·발행완료.
        post_quote_recheck가 켜져 있으면 사람 단가 유지 + 재확인 중인 견적확인필요도."""
        return status in (*QUOTED_STATES, ST_PENDING, ST_PENDING_EDITED, *ISSUED_STATES) or (
            bool(rules.get("post_quote_recheck")) and self.human_priced(status, basis))

    def post_quote_change(self, e: dict, cols: dict, price: str, rules: dict, stamp: str) -> dict | None:
        """견적완료 이후 행(post_quote_final)의 D·E·F·H·S 변경 → 산출근거 끝에 '[변경 11/27 14:05: H -0.3 추가]'.
        None = 해당 없음 (변경 없음, 또는 지문 변경이 이 칸들이 아님 — 거래처 칸 등 → 예전 규칙).
        dict: changes(바뀐 칸 설명), recheck(견적확인필요로), reasons(재확인 사유), body(산출근거 본문 → 새 본문).
        post_quote_recheck(기본 꺼짐)가 켜져 있으면 사람 단가 행만 예전 재확인 규칙도:
        - D·E·F 변경 → '[사양 변경 — 단가 재확인] 사람 단가 N 유지' + 견적확인필요
        - H 변경 → |치대|÷구경 ≥ ratio 또는 ≥ mm(구경 모르면 nodia_mm)이면 재확인
        - 예전 DB(watch_vals 없음)라 어느 칸인지 모름 → '[내용 변경 — 단가 재확인]'"""
        diff = e["watch_diff"]
        basis = e["row"][cols["basis"] - 1] if "basis" in cols else ""
        human = bool(rules.get("post_quote_recheck")) and self.human_priced(e["status"], basis)
        reasons = []
        if diff is None:  # 예전 DB: 어느 칸인지 모름
            if not (e["changed"] and e["items_changed"]):
                return None
            changes = ["내용(칸 모름)"]
            if human:
                reasons.append("내용 변경")
        else:
            spec = [f for f in WATCH_SPEC if f in diff]
            stock = WATCH_STOCK in diff
            if not diff or (e["changed"] and not (spec or stock)):
                return None  # 지문은 바뀌었는데 사양·연마량 칸이 아님 (거래처 칸 등) → 예전 규칙
            changes = [change_desc(re.sub(r"\d", "", rowcol_to_a1(1, cols[f])), *diff[f])
                       for f in WATCH_FIELDS if f in diff]
            if human and spec:
                reasons.append("사양 변경")
            if human and stock:
                def get(name):
                    i = cols.get(name)
                    return e["row"][i - 1] if i and i - 1 < len(e["row"]) else ""

                spec_text = get("spec_qty") if parse_dias(get("spec_qty")) else get("main_msg")
                desc, need = stock_change(*diff[WATCH_STOCK], spec_text, rules)
                if need:
                    reasons.append(desc)
        recheck = bool(reasons)
        keep = f" 사람 단가 {_fmt_price(price)} 유지" if price else ""
        tag = f"[변경 {stamp}: {', '.join(changes)}]"

        def body(rest: str) -> str:
            m = RECHECK_RE.match(rest)
            if recheck:  # 재확인 중인 행에 또 사유가 생기면 사유를 이어 붙임
                tail = rest[m.end():] if m else rest
                why = ", ".join(([m.group(1)] if m else []) + reasons)
                rest = f"[{why} — 단가 재확인]{keep}" + (f" | {tail}" if tail else "")
            return append_change_tag(rest, tag)

        return {"recheck": recheck, "reasons": reasons, "changes": changes, "body": body}

    def sync_quote_fp(self, row_id: str, fp: str) -> None:
        """재확인 표시한 행: pricer job_quotes 지문을 지금 내용으로 → 사람이 '확인'/비우면 적힌 단가로 사람확인."""
        if self.con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_quotes'").fetchone():
            self.con.execute("UPDATE job_quotes SET fp=? WHERE row_id=?", (fp, row_id))

    def decide_status(self, label, rownum, client, author, status, changed, age, settle, ai, skip_clients,
                      legacy=False, unregistered=False, homonym=False, filed=False):
        """None = 사람이 처리(상태 비움).

        client는 묶음 첫 행에서 해석한 거래처명(미등록이면 원문). 우선순위:
        제외-월말일괄 > 사람처리 > 명세표발행대기(파일에 담김) > 보류-미등록 > 보류-동명 > 기존건-확인필요 > 일반 규칙.

        명세표발행대기-수정됨은 사람이 볼 때까지 유지 (작성자 AI → 명세표발행완료(AI), 사람 이름 → 사람처리).
        명세표발행대기 + 파일에 담김(filed) + 묶음 수정 → 명세표발행대기-수정됨 (올린 파일과 어긋남).
        아직 파일에 안 담긴 명세표발행대기가 수정되면 일반 규칙(입력중 → 재견적)으로 — 아무도 올릴 수 없었으므로.

        월말 일괄 거래처(skip_clients)는 작성자·기존건·완료/출고 여부와 무관하게 항상 '제외-월말일괄'.
        그 외: 작성자가 사람 값이면 기존건이어도 사람처리가 우선. '기존건-확인필요'는 작성자 빈칸인 기존건에만.
        (작성자 AI인 기존건은 아래 일반 규칙 — 발행 후보 여부는 work_end_flipped_at으로 이미 막혀 있음)
        changed·age는 묶음 단위 값(묶음 안 어느 행이든 변경 / 묶음 최신 last_change 이후 경과).
        """
        nclient = norm_text(client) or ""
        if any(s and s in nclient for s in skip_clients):
            return ST_SKIP
        if author and author != ai:
            return None
        if status == ST_PENDING_EDITED:
            return status
        if status == ST_PENDING and filed:
            return ST_PENDING_EDITED if changed else status
        if unregistered:
            return ST_UNREG
        if homonym:
            return ST_HOMONYM
        if legacy and not author:
            return ST_LEGACY
        if status in ISSUED_STATES:
            if changed:
                logger.warning("sheet={} row={} client={} 발행후수정", label, rownum, client)
            return status
        if status in self.post_quote and not changed:
            return status
        # 신규/입력중/확정, 또는 견적 이후 단계에서 내용이 바뀐 행 → 안정화 규칙
        return ST_TYPING if age < settle else ST_CONFIRMED


def run(once: bool = False, dry_run: bool = False) -> None:
    w = Watcher(load_config(), dry_run=dry_run)
    w.loop(once=once)
