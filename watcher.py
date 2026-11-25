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
  날짜 없는 행 = 바로 위 묶음의 이어지는 줄 — 거래처 칸은 통째로 client_note.
  item_columns 중 하나라도 있으면 품목 행(견적·발행 대상), 없으면 메모 행(건너뜀, 상태 비움).
  첫 행 작성자가 사람이면 묶음 전체 사람처리, 첫 행 거래처를 못 찾으면 묶음 전체 '보류-미등록'.
  첫 묶음보다 위의 날짜 없는 행은 건너뜀. block_id = 묶음 첫 행의 row_id.
  확정 판정도 묶음 단위: 묶음 last_change = 묶음 안 품목 행 last_change 중 최신.
  quote_settle_min이 안 지났으면 묶음 전체 '입력중', 지났으면 묶음 전체 함께 '확정'.
  한 행이라도 바뀌면(알던 묶음에 행이 추가돼도) 견적 이후 단계 행까지 묶음 전체가 다시 입력중.
  보류-* 행이 하나라도 있으면 제외·사람처리·AI발행 행을 뺀 묶음 전체를 같은 보류로.
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

from config.settings import settings
from feedback import norm_text

SHEETS_YAML = Path(__file__).parent / "config" / "sheets.yaml"
SHEET_LABELS = {"imgagong": "임가공", "pipe": "파이프"}  # 로그 표시용 (yaml에 label 키가 있으면 그것 우선)

ST_TYPING = "입력중"
ST_CONFIRMED = "확정"
ST_QUOTED = "견적완료"
ST_ISSUED = "AI발행"
ST_SKIP = "제외-월말일괄"
ST_LEGACY = "기존건-확인필요"
ST_UNREG = "보류-미등록"
ST_HOMONYM = "보류-동명"
ST_SCREEN = "보류-화면이상"
ST_DELETED = "삭제됨"  # DB 전용 (시트에 쓰지 않음): 시트에서 row_id가 사라진 행
HOLD_PREFIX = "보류-"
# 산출근거 칸 맨 앞 묶음 표시 "[거래처(비고) 날짜 n/총줄수] "
BLOCK_TAG_RE = re.compile(r"^\[[^\[\]]* \d+/\d+\] ?")
ROW_ID_RE = re.compile(r"^[0-9a-f]{8}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rows(
  row_id TEXT PRIMARY KEY, sheet TEXT, tab TEXT, row_number INTEGER,
  fingerprint TEXT, first_seen TEXT, last_change TEXT,
  status TEXT, author TEXT, issued_at TEXT);
"""
# 기존 DB에는 없을 수 있는 열 → 없으면 ALTER
EXTRA_COLUMNS = {"work_end_state": "TEXT", "shipped_state": "TEXT", "work_end_flipped_at": "TEXT",
                 "legacy": "INTEGER", "block_id": "TEXT", "client_resolved": "TEXT", "client_note": "TEXT",
                 "deleted_at": "TEXT", "client_reg_no": "TEXT"}


def ensure_columns(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(rows)")}
    for name, typ in EXTRA_COLUMNS.items():
        if name not in have:
            con.execute(f"ALTER TABLE rows ADD COLUMN {name} {typ}")
    con.commit()


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


def pick_active_tab(worksheets, pattern: str, ignore: list[str]):
    """월 패턴에 맞는 탭 중 가장 왼쪽(인덱스 최소) 하나."""
    rx = re.compile(pattern)
    for ws in sorted(worksheets, key=lambda w: w.index):
        title = ws.title.strip()
        if title in ignore:
            continue
        if rx.match(title):
            return ws
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


REG_NO_RE = re.compile(r"^\d{3}-\d{2}-\d{5}$")
ALIASES_YAML = Path(__file__).parent / "config" / "client_aliases.yaml"
_warned: set[str] = set()


def warn_once(msg: str) -> None:
    """매 폴링 다시 만드는 마스터의 같은 경고를 프로세스당 한 번만."""
    if msg not in _warned:
        _warned.add(msg)
        logger.warning(msg)


def load_aliases(path: Path | None) -> dict[str, str]:
    """config/client_aliases.yaml의 aliases: {시트 표기: 정식 등록명 또는 사업자번호}. 없으면 {}."""
    if not path or not Path(path).exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {str(k): str(v) for k, v in (data.get("aliases") or {}).items() if k is not None and v is not None}


@dataclass
class ClientRecord:
    name: str
    reg_no: str
    weight: float
    last_date: str | None = None


@dataclass
class Resolution:
    """거래처 해석 결과. hold=None이면 확정(reg_no 있음, 마스터에 번호가 없는 월말일괄 거래처는 빈 값)."""
    name: str | None
    note: str
    reg_no: str | None = None
    hold: str | None = None  # None / ST_UNREG / ST_HOMONYM
    how: str = ""  # 별칭 / 마스터
    basis: str = ""  # 산출근거 묶음 표시에 붙일 말 (예: '동명→최근거래 140-04-14907')
    candidates: list[ClientRecord] = field(default_factory=list)  # 보류 시 후보 (로그용)


class ClientMaster:
    """거래처 해석: 별칭표 > 마스터 정확일치 > 마스터 최장(앞부분) 일치 (긴 쪽 우선, 같은 길이면 별칭 우선).

    정규화 키(compact_name)가 같은 등록이 여럿이면(같은 이름의 동명, 또는 '(주)대양상사'/'대양상사'):
    사업자번호가 하나뿐이면 확정, 둘 이상이면 최근 12개월(last_date) 거래가 있는 번호가 정확히
    하나일 때만 그쪽으로 확정('동명→최근거래 번호'). 아니면 이름이 하나 → 보류-동명,
    표기가 다른 등록 여럿 → 추측하지 않고 보류-미등록(후보는 로그). 별칭에 사업자번호를 적으면 그게 최우선."""

    def __init__(self, entries, aliases: dict[str, str] | None = None, today: date | None = None):
        self.by_key: dict[str, list[ClientRecord]] = {}
        self.by_reg: dict[str, ClientRecord] = {}
        self.by_name: dict[str, list[ClientRecord]] = {}
        self.alias: dict[str, tuple[str, str]] = {}  # 키 → ('reg', 번호) | ('name', 정식명)
        self.alias_errors: list[str] = []
        today = today or date.today()
        self.cutoff = (today - timedelta(days=365)).isoformat()
        for e in entries:
            rec = ClientRecord(e[0], "", e[1]) if len(e) == 2 else ClientRecord(*e)
            key, _ = compact_name(rec.name or "")
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
            akey, _ = compact_name(alias)
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
        if len(regs) <= 1:
            best = max(cands, key=lambda c: (bool(c.reg_no), c.weight))
            return Resolution(best.name, note, best.reg_no or None, how=how)
        active = list(dict.fromkeys(c.reg_no for c in cands if c.reg_no and self.active(c)))
        if len(active) == 1:
            best = max((c for c in cands if c.reg_no == active[0]), key=lambda c: c.weight)
            return Resolution(best.name, note, best.reg_no, how=how, basis=f"동명→최근거래 {best.reg_no}",
                              candidates=cands)
        if len(names) == 1:
            return Resolution(names[0], note, None, ST_HOMONYM, how, candidates=cands)
        return Resolution(None, note, None, ST_UNREG, how, candidates=cands)

    def resolve_full(self, text: str) -> Resolution:
        text = text or ""
        key, idx = compact_name(text)
        for n in range(len(key), 0, -1):
            alias, cands = self.alias.get(key[:n]), self.by_key.get(key[:n])
            if not alias and not cands:
                continue
            note = re.sub(r"^\s*(\(주\)|㈜|주식회사)", "", text[idx[n - 1] + 1:]).strip()  # '대성정밀(주) 급함'
            if alias:
                kind, target = alias
                if kind == "reg":
                    rec = self.by_reg[target]
                    return Resolution(rec.name, note, rec.reg_no, how="별칭")
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
        updates, dropped = [], 0
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
            updates.append({"range": rowcol_to_a1(target, self.cols[name]), "values": [[value]]})
        return updates, dropped

    def flush(self, ws, dry_run: bool = False) -> int:
        """쓰기 직전 다시 읽고 row_id 기준으로 옮겨 batch_update. 실제 쓴 건수 반환."""
        if not self.items or dry_run:
            return 0
        updates, _ = self.resolve(ws.get_all_values())
        if updates:
            ws.batch_update(updates, value_input_option="RAW")
        return len(updates)


def load_client_master(con: sqlite3.Connection, extra_names=(), aliases: dict[str, str] | None = None,
                       today: date | None = None) -> ClientMaster:
    """DB clients(경리나라 import) + skip_clients(월말일괄 — 마스터에 없을 수 있음) + 별칭표."""
    try:
        rows = con.execute("SELECT name, COALESCE(reg_no, ''), COALESCE(n_statements, 0), last_date"
                           " FROM clients").fetchall()
    except sqlite3.OperationalError:  # import 전이라 테이블 없음
        rows = []
    return ClientMaster(list(rows) + [(n, "", -1, None) for n in extra_names], aliases, today)


class Watcher:
    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        self.common = cfg["common"]
        self.dry_run = dry_run
        self.gc = gspread.service_account(filename=self.common["credentials_file"])
        Path(settings.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(settings.DB_PATH)
        ensure_columns(self.con)
        # 시트별 마지막 바퀴 결과 (pricer 등 후속 단계가 사용)
        self.snapshots: dict[str, dict] = {}
        self.alias_path: Path | None = ALIASES_YAML  # 매 바퀴 다시 읽음 (고치면 재시작 없이 반영)
        st = self.common["status_values"]
        # '견적완료' 이후 단계로 보는 상태들 (제외는 별도 규칙)
        # 기존건은 시트 값이 아니라 DB(rows.status)로만 판정한다
        # 보류-미등록/보류-동명은 매 바퀴 거래처 마스터로 다시 판정 (조건이 풀리면 자동 해제)
        # 보류-화면이상은 유지 — 사람이 묶음 안 한 행의 상태 칸을 비우면 묶음 전체 재평가
        self.post_quote = set(st[st.index(ST_QUOTED):]) - {ST_SKIP, ST_LEGACY, ST_UNREG, ST_HOMONYM}

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
        sh = self.gc.open_by_key(scfg["spreadsheet_id"])
        ws = pick_active_tab(sh.worksheets(), self.common["month_tab_pattern"], scfg.get("ignore_tabs") or [])
        if ws is None:
            logger.warning("sheet={} 활성 월 탭 없음", label)
            return

        values = ws.get_all_values()
        width = max(cols.values())
        master = load_client_master(self.con, self.common.get("skip_clients") or [],
                                    load_aliases(getattr(self, "alias_path", None)))
        item_cols = scfg.get("item_columns") or []
        settle = timedelta(minutes=float(self.common["quote_settle_min"]))
        ai = self.common["author_ai_value"]
        skip_clients = [norm_text(c) for c in self.common.get("skip_clients") or []]
        now = datetime.now()

        writer = SheetWriter(cols, scfg["data_start_row"], scfg["hash_columns"], label)
        counts = Counter()
        seen_ids: set[str] = set()
        snap_rows: list[dict] = []
        self.snapshots[key] = {"ws": ws, "values": values, "rows": snap_rows, "label": label}

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
            그 뒤 보류 행이 하나라도 있으면 (제외·사람처리·AI발행 행을 뺀) 묶음 전체를 보류로.
            보류 우선순위: 미등록 > 동명 > 화면이상."""
            if not blk or not blk["entries"]:
                return
            entries = blk["entries"]
            block_last = max(e["last_change"] for e in entries)
            # 묶음 안 한 행이라도 바뀌었거나, 알던 묶음에 새 행이 붙으면 묶음 전체가 '변경'
            news = [e["is_new"] for e in entries]
            block_changed = any(e["changed"] for e in entries) or (any(news) and not all(news))
            # 보류-화면이상이던 행의 상태 칸을 사람이 비웠으면 → 묶음 안 보류-화면이상 모두 풀고 재평가
            screen_cleared = any(e["db_status"] == ST_SCREEN and not e["status"] for e in entries)
            if screen_cleared:
                logger.info("sheet={} row={} client={} 보류-화면이상 해제 → 묶음 재평가",
                            label, blk["rownum"], entries[0]["client_log"])
            homonym = blk["hold"] == ST_HOMONYM
            for e in entries:
                status = "" if screen_cleared and e["status"] == ST_SCREEN else e["status"]
                e["new_status"] = self.decide_status(
                    label, e["rownum"], e["client_log"], e["author"], status, block_changed,
                    now - block_last, settle, ai, skip_clients, legacy=e["legacy"],
                    unregistered=blk["client"] is None or blk["hold"] == ST_UNREG, homonym=homonym)
            holds = [e["new_status"] for e in entries if (e["new_status"] or "").startswith(HOLD_PREFIX)]
            if holds:
                hold = next((h for h in (ST_UNREG, ST_HOMONYM, ST_SCREEN) if h in holds), holds[0])
                for e in entries:
                    if e["new_status"] not in (None, ST_SKIP, ST_ISSUED):
                        e["new_status"] = hold

            tag_client = blk["client"] or blk["client_text"] or "?"
            note = blk["client_note"]
            if blk["client"] and note:  # '오성기계공구(택배)' 처럼 이미 괄호면 그대로
                tag_client += note if note.startswith("(") and note.endswith(")") else f"({note})"
            for pos, e in enumerate(entries, 1):
                rownum, row_id, row = e["rownum"], e["row_id"], e["row"]
                extra = f" {blk['basis']}" if blk["basis"] else ""  # '동명→최근거래 번호'
                tag = f"[{tag_client}{extra} {blk['date']} {pos}/{len(entries)}]"
                # 묶음 표시는 에이전트가 처리하는 행에만 — 사람처리·제외-월말일괄 행의 산출근거는 안 건드림
                if "basis" in cols and e["new_status"] not in (None, ST_SKIP):
                    basis = cell(row, "basis")
                    new_basis = with_block_tag(tag, basis)
                    if new_basis != basis:  # 태그 n/m 갱신은 '변경'이 아님 — 지문(hash_columns)에 없는 칸
                        writer.add(rownum, "basis", new_basis, row, row_id=row_id, expect_fp=e["fp"])
                new_status = e["new_status"]
                if new_status is None:  # 사람 처리
                    counts["사람처리"] += 1
                    new_status = ""
                elif new_status == ST_SKIP:
                    counts["제외"] += 1
                else:
                    counts[new_status] += 1
                if new_status != e["status"]:
                    writer.add(rownum, "status", new_status, row, row_id=row_id, expect_fp=e["fp"])
                self.con.execute("UPDATE rows SET status=? WHERE row_id=?", (new_status, row_id))
                snap_rows.append({"rownum": rownum, "row_id": row_id, "row": row, "fp": e["fp"], "status": new_status,
                                  "author": e["author"], "legacy": e["legacy"],
                                  "work_end_flipped_at": e["flipped_at"], "block_id": blk["id"],
                                  "client_resolved": blk["client"], "client_reg_no": blk["reg_no"],
                                  "client_note": e["client_note"],
                                  "block_date": blk["date"], "block_label": tag_client,
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
                resolved = None if res.hold == ST_UNREG else res.name
                block = {"rownum": rownum, "row": row, "client": resolved, "author": cell(row, "author"),
                         "id": None, "date": cell(row, "date"), "client_text": client_text,
                         "client_note": client_note, "entries": [], "reg_no": res.reg_no, "hold": res.hold,
                         "basis": res.basis}
                counts["묶음"] += 1
                cands = " | ".join(f"{c.name} {c.reg_no or '-'} 최근{c.last_date or '-'}" for c in res.candidates)
                if client_text and res.hold == ST_UNREG:
                    if res.candidates:
                        logger.info("sheet={} row={} 거래처 후보 여럿 {!r} → 보류-미등록 (후보: {})",
                                    label, rownum, client_text, cands)
                    else:
                        logger.info("sheet={} row={} 거래처 미등록 {!r}", label, rownum, client_text)
                elif res.basis:
                    logger.info("sheet={} row={} 거래처 {!r} → {} {} (후보: {})",
                                label, rownum, client_text, res.name, res.basis, cands)
            elif block is None:
                continue  # 첫 묶음보다 위의 날짜 없는 행
            else:  # 이어지는 줄: 거래처 칸은 해석하지 않고 통째로 메모
                client_note = client_text

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
            # 묶음 첫 행(또는 이 행)의 작성자가 사람 값이면 사람처리
            author = next((a for a in (block["author"], cell(row, "author")) if a and a != ai),
                          cell(row, "author"))
            status = cell(row, "status")

            work_end = bool_state(cell(row, "work_end")) if "work_end" in cols else "FALSE"
            shipped = bool_state(cell(row, "shipped")) if "shipped" in cols else "FALSE"

            # --- DB 갱신 ---
            rec = self.con.execute(
                "SELECT fingerprint, last_change, status, work_end_state, work_end_flipped_at, legacy"
                " FROM rows WHERE row_id=?",
                (row_id,)).fetchone()
            if rec is None:
                changed = False  # 최초 발견은 '변경'으로 보지 않음
                last_change = now
                legacy = "TRUE" in (work_end, shipped)
                flipped_at = None
                self.con.execute(
                    "INSERT INTO rows(row_id, sheet, tab, row_number, fingerprint, first_seen, last_change, status, author)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (row_id, key, ws.title, rownum, fp, now_iso(), now_iso(), status, author))
            else:
                db_fp, db_last, db_status, prev_work_end, flipped_at, db_legacy = rec
                # 정체성은 row_id, 변경 여부는 hash_columns 지문으로만 — 행 번호·묶음 위치·n/m은 보지 않음
                changed = db_fp != fp
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
                    legacy = db_status != ST_ISSUED and "TRUE" in (work_end, shipped)
                else:
                    legacy = False
                    if prev_work_end == "FALSE" and work_end == "TRUE":
                        flipped_at = now_iso()
                        logger.info("sheet={} row={} client={} 작업완료 체크 → 발행 후보", label, rownum, client_log)
                    elif work_end == "FALSE" and flipped_at and db_status != ST_ISSUED:
                        flipped_at = None  # 체크 해제 → 발행 후보에서 빠짐
                self.con.execute(
                    "UPDATE rows SET sheet=?, tab=?, row_number=?, fingerprint=?, last_change=?, author=? WHERE row_id=?",
                    (key, ws.title, rownum, fp, last_change.isoformat(timespec="seconds"), author, row_id))
            if legacy:
                flipped_at = None  # 기존건은 절대 발행 대상 아님
            self.con.execute(
                "UPDATE rows SET work_end_state=?, shipped_state=?, work_end_flipped_at=?, legacy=?,"
                " block_id=?, client_resolved=?, client_reg_no=?, client_note=? WHERE row_id=?",
                (work_end, shipped, flipped_at, int(legacy), block["id"], client, block["reg_no"], client_note,
                 row_id))

            # 상태는 묶음이 끝날 때 함께 결정 (finish_block)
            block["entries"].append({
                "rownum": rownum, "row_id": row_id, "row": row, "author": author, "status": status,
                "fp": fp, "changed": changed, "is_new": rec is None, "db_status": db_status if rec else None, "last_change": last_change, "legacy": legacy,
                "flipped_at": flipped_at, "client_log": client_log, "client_note": client_note})
        finish_block(block)
        self.mark_deleted(key, label, ws.title, values, cols, scfg["data_start_row"], seen_ids)

        written = writer.flush(ws, self.dry_run)

        extra = [f"{k}={v}" for k, v in counts.items()
                 if k not in ("rows", "묶음", "메모", ST_TYPING, ST_CONFIRMED, "제외", "사람처리")]
        logger.info(
            "sheet={} tab={} 묶음={} 품목={} 메모={} 입력중={} 확정={} 제외={} 사람처리={}{}{}",
            label, ws.title, counts["묶음"], counts["rows"], counts["메모"], counts[ST_TYPING], counts[ST_CONFIRMED],
            counts["제외"], counts["사람처리"],
            (" " + " ".join(extra)) if extra else "",
            f" (dry-run, 미기록 쓰기 {len(writer)}건)" if self.dry_run
            else f" 쓰기={written}" + (f" (보류 {len(writer) - written})" if written != len(writer) else ""),
        )

    def mark_deleted(self, key, label, tab, values, cols, data_start_row, seen_ids) -> None:
        """이 탭에서 row_id가 더 이상 보이지 않는 행 → DB status '삭제됨' (이후 처리 안 함).
        메모 행으로 바뀌었어도 V열에 row_id가 남아 있으면 삭제가 아님. 다시 나타나면 process_sheet가 복원."""
        id_col = cols["row_id"] - 1
        present = set(seen_ids)
        for row in values[data_start_row - 1:]:
            rid = (row[id_col] if id_col < len(row) else "").strip()
            if ROW_ID_RE.match(rid):
                present.add(rid)
        gone = [rid for (rid,) in self.con.execute(
            "SELECT row_id FROM rows WHERE sheet=? AND tab=? AND COALESCE(status, '') != ?",
            (key, tab, ST_DELETED)) if rid not in present]
        if not gone:
            return
        self.con.executemany("UPDATE rows SET status=?, deleted_at=? WHERE row_id=?",
                             [(ST_DELETED, now_iso(), rid) for rid in gone])
        logger.info("sheet={} tab={} 시트에서 사라진 행 {}개 → 삭제됨 ({})", label, tab, len(gone),
                    ", ".join(gone[:10]) + (" …" if len(gone) > 10 else ""))

    def decide_status(self, label, rownum, client, author, status, changed, age, settle, ai, skip_clients,
                      legacy=False, unregistered=False, homonym=False):
        """None = 사람이 처리(상태 비움).

        client는 묶음 첫 행에서 해석한 거래처명(미등록이면 원문). 우선순위:
        제외-월말일괄 > 사람처리 > 보류-미등록 > 보류-동명 > 기존건-확인필요 > 일반 규칙.

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
        if unregistered:
            return ST_UNREG
        if homonym:
            return ST_HOMONYM
        if legacy and not author:
            return ST_LEGACY
        if status == ST_ISSUED:
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
