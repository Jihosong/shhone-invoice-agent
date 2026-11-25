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
  (거래처 마스터 최장 일치, ㈜/(주)/주식회사/공백 무시), 일치 뒤 남은 글자는 client_note.
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
시트 쓰기는 탭당 batch_update 한 번으로 모은다.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
import unicodedata
import uuid
from collections import Counter
from datetime import datetime, timedelta
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
                 "legacy": "INTEGER", "block_id": "TEXT", "client_resolved": "TEXT", "client_note": "TEXT"}


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


class ClientMaster:
    """거래처 마스터 최장 일치. 같은 키(예: '(주)대양상사'/'대양상사')면 명세표가 많은 쪽."""

    def __init__(self, entries):
        self.by_key: dict[str, tuple[str, float]] = {}
        self.dup_names: set[str] = set()  # 같은 이름에 사업자번호가 둘 이상 (보류-동명)
        for name, weight in entries:
            key, _ = compact_name(name or "")
            if key and (key not in self.by_key or weight > self.by_key[key][1]):
                self.by_key[key] = (name, weight)

    def __len__(self) -> int:
        return len(self.by_key)

    def resolve(self, text: str) -> tuple[str | None, str]:
        """(거래처명 또는 None, 일치 뒤 남은 글자 = client_note)."""
        text = text or ""
        key, idx = compact_name(text)
        for n in range(len(key), 0, -1):
            hit = self.by_key.get(key[:n])
            if hit:
                note = text[idx[n - 1] + 1:]
                return hit[0], re.sub(r"^\s*(\(주\)|㈜|주식회사)", "", note).strip()  # '대성정밀(주) 급함'
        return None, text.strip()


def load_client_master(con: sqlite3.Connection, extra_names=()) -> ClientMaster:
    """DB clients(경리나라 import) + skip_clients(월말일괄 — 마스터에 없을 수 있음)."""
    try:
        rows = con.execute("SELECT name, COALESCE(n_statements, 0) FROM clients").fetchall()
        dups = con.execute("SELECT name FROM clients WHERE COALESCE(reg_no, '') != ''"
                           " GROUP BY name HAVING COUNT(DISTINCT reg_no) > 1").fetchall()
    except sqlite3.OperationalError:  # import 전이라 테이블 없음
        rows, dups = [], []
    master = ClientMaster(list(rows) + [(n, -1) for n in extra_names])
    master.dup_names = {n for (n,) in dups}
    return master


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
        master = load_client_master(self.con, self.common.get("skip_clients") or [])
        item_cols = scfg.get("item_columns") or []
        settle = timedelta(minutes=float(self.common["quote_settle_min"]))
        ai = self.common["author_ai_value"]
        skip_clients = [norm_text(c) for c in self.common.get("skip_clients") or []]
        now = datetime.now()

        updates: list[dict] = []
        counts = Counter()
        seen_ids: set[str] = set()
        snap_rows: list[dict] = []
        self.snapshots[key] = {"ws": ws, "values": values, "rows": snap_rows, "label": label}

        def cell(row: list[str], name: str) -> str:
            return row[cols[name] - 1].strip()

        def set_cell(rownum: int, name: str, value: str) -> None:
            updates.append({"range": rowcol_to_a1(rownum, cols[name]), "values": [[value]]})

        def ensure_row_id(rownum: int, row: list[str]) -> str:
            row_id = cell(row, "row_id")
            # 비었거나, 형식이 아니거나(다른 값이 들어 있음), 행 복사로 중복된 경우 새로 부여
            if not ROW_ID_RE.match(row_id) or row_id in seen_ids:
                if row_id:
                    why = "중복" if row_id in seen_ids else "형식 아님"
                    logger.warning("sheet={} row={} row_id={!r} {} → 새로 부여", label, rownum, row_id, why)
                row_id = uuid.uuid4().hex[:8]
                set_cell(rownum, "row_id", row_id)
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
            homonym = blk["client"] in master.dup_names
            for e in entries:
                status = "" if screen_cleared and e["status"] == ST_SCREEN else e["status"]
                e["new_status"] = self.decide_status(
                    label, e["rownum"], e["client_log"], e["author"], status, block_changed,
                    now - block_last, settle, ai, skip_clients, legacy=e["legacy"],
                    unregistered=blk["client"] is None, homonym=homonym)
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
                tag = f"[{tag_client} {blk['date']} {pos}/{len(entries)}]"
                # 묶음 표시는 에이전트가 처리하는 행에만 — 사람처리·제외-월말일괄 행의 산출근거는 안 건드림
                if "basis" in cols and e["new_status"] not in (None, ST_SKIP):
                    basis = cell(row, "basis")
                    new_basis = with_block_tag(tag, basis)
                    if new_basis != basis:
                        set_cell(rownum, "basis", new_basis)
                new_status = e["new_status"]
                if new_status is None:  # 사람 처리
                    counts["사람처리"] += 1
                    new_status = ""
                elif new_status == ST_SKIP:
                    counts["제외"] += 1
                else:
                    counts[new_status] += 1
                if new_status != e["status"]:
                    set_cell(rownum, "status", new_status)
                self.con.execute("UPDATE rows SET status=? WHERE row_id=?", (new_status, row_id))
                snap_rows.append({"rownum": rownum, "row_id": row_id, "row": row, "status": new_status,
                                  "author": e["author"], "legacy": e["legacy"],
                                  "work_end_flipped_at": e["flipped_at"], "block_id": blk["id"],
                                  "client_resolved": blk["client"], "client_note": e["client_note"],
                                  "block_date": blk["date"], "block_label": tag_client,
                                  "block_tag": tag})

        block: dict | None = None
        for idx in range(scfg["data_start_row"] - 1, len(values)):
            rownum = idx + 1
            row = values[idx] + [""] * (width - len(values[idx]))
            client_text = cell(row, "client")
            if cell(row, "date"):  # 묶음 첫 행
                finish_block(block)
                resolved, client_note = master.resolve(client_text) if client_text else (None, "")
                block = {"rownum": rownum, "row": row, "client": resolved, "author": cell(row, "author"),
                         "id": None, "date": cell(row, "date"), "client_text": client_text,
                         "client_note": client_note, "entries": []}
                counts["묶음"] += 1
                if client_text and resolved is None:
                    logger.info("sheet={} row={} 거래처 미등록 {!r}", label, rownum, client_text)
            elif block is None:
                continue  # 첫 묶음보다 위의 날짜 없는 행
            else:  # 이어지는 줄: 거래처 칸은 해석하지 않고 통째로 메모
                client_note = client_text

            if item_cols and not any(cell(row, c) for c in item_cols):
                # 메모 행: 건너뜀, 상태 비움
                if client_text:
                    counts["메모"] += 1
                if cell(row, "status"):
                    set_cell(rownum, "status", "")
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
                changed = db_fp != fp
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
                " block_id=?, client_resolved=?, client_note=? WHERE row_id=?",
                (work_end, shipped, flipped_at, int(legacy), block["id"], client, client_note, row_id))

            # 상태는 묶음이 끝날 때 함께 결정 (finish_block)
            block["entries"].append({
                "rownum": rownum, "row_id": row_id, "row": row, "author": author, "status": status,
                "changed": changed, "is_new": rec is None, "db_status": rec[2] if rec else None, "last_change": last_change, "legacy": legacy,
                "flipped_at": flipped_at, "client_log": client_log, "client_note": client_note})
        finish_block(block)

        if updates and not self.dry_run:
            ws.batch_update(updates, value_input_option="RAW")

        extra = [f"{k}={v}" for k, v in counts.items()
                 if k not in ("rows", "묶음", "메모", ST_TYPING, ST_CONFIRMED, "제외", "사람처리")]
        logger.info(
            "sheet={} tab={} 묶음={} 품목={} 메모={} 입력중={} 확정={} 제외={} 사람처리={}{}{}",
            label, ws.title, counts["묶음"], counts["rows"], counts["메모"], counts[ST_TYPING], counts[ST_CONFIRMED],
            counts["제외"], counts["사람처리"],
            (" " + " ".join(extra)) if extra else "",
            f" (dry-run, 미기록 쓰기 {len(updates)}건)" if self.dry_run else f" 쓰기={len(updates)}",
        )

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
