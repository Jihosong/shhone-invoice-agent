"""구글시트 작업현황표 감시 모듈.

config/sheets.yaml 설정에 따라 시트별 활성 월 탭을 폴링한다.
- 행 지문(hash_columns)으로 변경을 감지해 SQLite rows 테이블에 기록
- 비어 있는 row_id 칸에 ID 부여
- 상태 열(입력중/확정/제외-월말일괄 …) 갱신
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
ROW_ID_RE = re.compile(r"^[0-9a-f]{8}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rows(
  row_id TEXT PRIMARY KEY, sheet TEXT, tab TEXT, row_number INTEGER,
  fingerprint TEXT, first_seen TEXT, last_change TEXT,
  status TEXT, author TEXT, issued_at TEXT);
"""


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
        self.con.executescript(SCHEMA)
        st = self.common["status_values"]
        # '견적완료' 이후 단계로 보는 상태들 (제외는 별도 규칙)
        self.post_quote = set(st[st.index(ST_QUOTED):]) - {ST_SKIP}

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

            # --- DB 갱신 ---
            rec = self.con.execute("SELECT fingerprint, last_change FROM rows WHERE row_id=?", (row_id,)).fetchone()
            if rec is None:
                changed = False  # 최초 발견은 '변경'으로 보지 않음
                last_change = now
                self.con.execute(
                    "INSERT INTO rows(row_id, sheet, tab, row_number, fingerprint, first_seen, last_change, status, author)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (row_id, key, ws.title, rownum, fp, now_iso(), now_iso(), status, author))
            else:
                changed = rec[0] != fp
                last_change = now if changed else datetime.fromisoformat(rec[1])
                self.con.execute(
                    "UPDATE rows SET sheet=?, tab=?, row_number=?, fingerprint=?, last_change=?, author=? WHERE row_id=?",
                    (key, ws.title, rownum, fp, last_change.isoformat(timespec="seconds"), author, row_id))

            # --- 상태 결정 ---
            new_status = self.decide_status(label, rownum, client, author, status, changed,
                                            now - last_change, settle, ai, skip_clients)
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

    def decide_status(self, label, rownum, client, author, status, changed, age, settle, ai, skip_clients):
        """None = 사람이 처리(상태 비움)."""
        if author and author != ai:
            return None
        nclient = norm_text(client) or ""
        if any(s and s in nclient for s in skip_clients):
            return ST_SKIP
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
