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
시트 쓰기는 탭당 batch_update 한 번으로 모은다.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
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
ROW_ID_RE = re.compile(r"^[0-9a-f]{8}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rows(
  row_id TEXT PRIMARY KEY, sheet TEXT, tab TEXT, row_number INTEGER,
  fingerprint TEXT, first_seen TEXT, last_change TEXT,
  status TEXT, author TEXT, issued_at TEXT);
"""
# 기존 DB에는 없을 수 있는 열 → 없으면 ALTER
EXTRA_COLUMNS = {"work_end_state": "TEXT", "shipped_state": "TEXT", "work_end_flipped_at": "TEXT",
                 "legacy": "INTEGER"}


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


def known_clients(con: sqlite3.Connection) -> set[str]:
    try:
        return {r[0] for r in con.execute("SELECT name FROM clients") if r[0]}
    except sqlite3.OperationalError:  # import 전이라 테이블 없음
        return set()


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
        self.post_quote = set(st[st.index(ST_QUOTED):]) - {ST_SKIP, ST_LEGACY}

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
        clients = known_clients(self.con) if scfg.get("continuation_row_rule") else None
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

        for idx in range(scfg["data_start_row"] - 1, len(values)):
            rownum = idx + 1
            row = values[idx] + [""] * (width - len(values[idx]))
            client = cell(row, "client")
            if not client:
                continue
            if clients is not None and not cell(row, "date"):
                # 연속행(윗 행 메모) 규칙: 날짜 없고 거래처 마스터에 없는 회사면 건너뜀
                if not clients or norm_text(client) not in clients:
                    continue

            counts["rows"] += 1
            fp = fingerprint([cell(row, c) for c in scfg["hash_columns"]])
            author = cell(row, "author")
            status = cell(row, "status")

            row_id = cell(row, "row_id")
            # 비었거나, 형식이 아니거나(다른 값이 들어 있음), 행 복사로 중복된 경우 새로 부여
            if not ROW_ID_RE.match(row_id) or row_id in seen_ids:
                if row_id:
                    why = "중복" if row_id in seen_ids else "형식 아님"
                    logger.warning("sheet={} row={} row_id={!r} {} → 새로 부여", label, rownum, row_id, why)
                row_id = uuid.uuid4().hex[:8]
                set_cell(rownum, "row_id", row_id)
            seen_ids.add(row_id)

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
                        logger.info("sheet={} row={} client={} 작업완료 체크 → 발행 후보", label, rownum, client)
                    elif work_end == "FALSE" and flipped_at and db_status != ST_ISSUED:
                        flipped_at = None  # 체크 해제 → 발행 후보에서 빠짐
                self.con.execute(
                    "UPDATE rows SET sheet=?, tab=?, row_number=?, fingerprint=?, last_change=?, author=? WHERE row_id=?",
                    (key, ws.title, rownum, fp, last_change.isoformat(timespec="seconds"), author, row_id))
            if legacy:
                flipped_at = None  # 기존건은 절대 발행 대상 아님
            self.con.execute(
                "UPDATE rows SET work_end_state=?, shipped_state=?, work_end_flipped_at=?, legacy=? WHERE row_id=?",
                (work_end, shipped, flipped_at, int(legacy), row_id))

            # --- 상태 결정 ---
            new_status = self.decide_status(label, rownum, client, author, status, changed,
                                            now - last_change, settle, ai, skip_clients, legacy=legacy)
            if new_status is None:  # 사람 처리
                counts["사람처리"] += 1
                new_status = ""
            elif new_status == ST_SKIP:
                counts["제외"] += 1
            else:
                counts[new_status] += 1
            if new_status != status:
                set_cell(rownum, "status", new_status)
            self.con.execute("UPDATE rows SET status=? WHERE row_id=?", (new_status, row_id))
            snap_rows.append({"rownum": rownum, "row_id": row_id, "row": row, "status": new_status,
                              "author": author, "legacy": legacy, "work_end_flipped_at": flipped_at})

        if updates and not self.dry_run:
            ws.batch_update(updates, value_input_option="RAW")

        extra = [f"{k}={v}" for k, v in counts.items()
                 if k not in ("rows", ST_TYPING, ST_CONFIRMED, "제외", "사람처리")]
        logger.info(
            "sheet={} tab={} rows={} 입력중={} 확정={} 제외={} 사람처리={}{}{}",
            label, ws.title, counts["rows"], counts[ST_TYPING], counts[ST_CONFIRMED],
            counts["제외"], counts["사람처리"],
            (" " + " ".join(extra)) if extra else "",
            f" (dry-run, 미기록 쓰기 {len(updates)}건)" if self.dry_run else f" 쓰기={len(updates)}",
        )

    def decide_status(self, label, rownum, client, author, status, changed, age, settle, ai, skip_clients,
                      legacy=False):
        """None = 사람이 처리(상태 비움).

        월말 일괄 거래처(skip_clients)는 작성자·기존건·완료/출고 여부와 무관하게 항상 '제외-월말일괄'.
        그 외: 작성자가 사람 값이면 기존건이어도 사람처리가 우선. '기존건-확인필요'는 작성자 빈칸인 기존건에만.
        (작성자 AI인 기존건은 아래 일반 규칙 — 발행 후보 여부는 work_end_flipped_at으로 이미 막혀 있음)
        """
        nclient = norm_text(client) or ""
        if any(s and s in nclient for s in skip_clients):
            return ST_SKIP
        if author and author != ai:
            return None
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
