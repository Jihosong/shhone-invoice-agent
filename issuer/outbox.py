"""발행기 1단계 '반자동' — 발행 조건이 맞은 묶음을 경리나라 엑셀일괄등록 파일로 data/outbox/에 둔다.

업로드는 사람이 한다. 흐름 (매 바퀴 watcher → pricer 다음):
0) 작성자 잠금(묶음 단위): 묶음 안 어느 행이든 사람 작성자가 있으면(다른 줄은 비어 있거나 AI여도) 사람 것 —
   마감·발행대기·파일 담기 모두 안 함 (watcher가 이미 묶음 전체 사람처리로 둠).
1) 마감: 상태 발행대기/발행대기-수정됨인 묶음에서 사람이 아무 행의 작성자를 'AI'로 바꿨고 그 묶음이 파일에
   담겨 있으면 → 묶음 전체 AI발행, 모든 행 작성자 'AI'로 통일, rows.issued_at·issue_file_blocks.issued_at 기록.
2) 발행대기 표시: 묶음 단위 조건 —
   - 모든 품목 행 상태 견적완료(또는 견적확인필요인데 사람이 단가를 적음), 이번 바퀴에 바뀐 상태가 아님
   - 모든 행 작성자 빈칸, 기존건 아님 (보류·동명확인·사람처리·제외는 위 상태 조건에서 이미 빠짐)
   - 모든 행 작업완료 TRUE, 그중 하나 이상은 에이전트가 FALSE→TRUE를 직접 봄(work_end_flipped_at)
   - 줄을 만들 수 있음(거래처 사업자번호·날짜·규격·수량·단가) — 시트 kind에 builder가 있어야 함(지금은 pipe)
   - 아직 어느 파일에도 담긴 적 없는 묶음
   일부만 작업완료면 기다림. 작업완료가 먼저고 견적이 늦으면 견적완료된 다음 바퀴에 발행대기.
3) 묶음 파일(bundle — `agent.py issue`, run 루프에서는 issue.bundle_interval_min마다):
   발행대기인데 아직 파일에 없는 묶음을 모아 data/outbox/YYYYMMDD_HHMM.xls 한 파일(묶음 하나 = 명세표 한 장).
   쓰기 직전 시트를 다시 읽어 상태 발행대기·내용 그대로인 묶음만 담는다. DB issue_file_blocks의 block_id가
   PRIMARY KEY라 같은 묶음이 두 파일에 들어갈 수 없다(동시에 돌아도 DB가 막음). 산출근거 태그 뒤 '[→ 파일명]'.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from loguru import logger

from issuer.excel_upload import DEFAULT_MEMO_TEXT, LINE_BUILDERS, LineError, Statement, block_date, parse_price, write_xls
from watcher import (REG_NO_RE, ST_ISSUED, ST_PENDING, ST_PENDING_EDITED, ST_SKIP, SheetWriter, bool_state, now_iso,
                     warn_once, with_block_tag)

ROOT = Path(__file__).resolve().parent.parent
ST_QUOTED = "견적완료"
ST_CHECK = "견적확인필요"
DEFAULT_OUTBOX = "data/outbox"


def group_blocks(snap_rows: list[dict]) -> list[tuple[str, list[dict]]]:
    blocks: dict[str, list[dict]] = {}
    for r in snap_rows:
        blocks.setdefault(r["block_id"], []).append(r)
    return list(blocks.items())


@dataclass
class Candidate:
    sheet: str
    tab: str
    rows: list[dict]
    statement: Statement


class Issuer:
    def __init__(self, cfg: dict, con: sqlite3.Connection, dry_run: bool = False, today: date | None = None):
        self.cfg = cfg
        self.con = con
        self.dry_run = dry_run
        self.today = today
        self.icfg = cfg.get("issue") or {}
        self.ai = cfg["common"].get("author_ai_value", "AI")

    # ------------------------------------------------------------------ 설정
    @property
    def memo_text(self) -> str:
        v = self.icfg.get("memo_text")
        return DEFAULT_MEMO_TEXT if v is None else str(v)

    def outbox_dir(self) -> Path:
        p = Path(self.icfg.get("outbox_dir") or DEFAULT_OUTBOX)
        return p if p.is_absolute() else ROOT / p

    def filed(self, sheet: str) -> dict[str, tuple[str, str | None]]:
        return {bid: (fn, rm) for bid, fn, rm in self.con.execute(
            "SELECT block_id, file_name, removed_at FROM issue_file_blocks WHERE sheet=?", (sheet,))}

    # ------------------------------------------------------------------ 바퀴
    def run_once(self, snapshots: dict[str, dict], bundle: bool = False, out_dir: Path | None = None) -> Path | None:
        for key, scfg in self.cfg["sheets"].items():
            if key not in snapshots:
                continue
            try:
                self.process_sheet(key, scfg, snapshots[key])
            except Exception:
                logger.exception("issuer sheet={} 처리 실패", key)
        if not self.dry_run:
            self.con.commit()
        return self.bundle(snapshots, out_dir) if bundle else None

    def _cell(self, scfg, r, name) -> str:
        cols = scfg["columns"]
        row = r["row"]
        i = cols[name] - 1
        return str(row[i] if row[i] is not None else "").strip() if i < len(row) else ""

    def process_sheet(self, key: str, scfg: dict, snap: dict) -> None:
        cols = scfg["columns"]
        label = snap.get("label", key)
        writer = SheetWriter(cols, scfg["data_start_row"], scfg["hash_columns"], label)
        filed = self.filed(key)
        counts = Counter()
        for block_id, rows in group_blocks(snap["rows"]):
            statuses = {r["status"] for r in rows}
            authors = [self._cell(scfg, r, "author") for r in rows]
            if self.human_author(scfg, rows):
                continue  # 묶음 안 사람 작성자가 하나라도 있으면 사람 것 — 마감·발행대기 안 함
            if statuses & {ST_PENDING, ST_PENDING_EDITED} and self.ai in authors:
                if block_id in filed:
                    self._close(key, label, scfg, block_id, rows, writer, filed[block_id][0])
                    counts[ST_ISSUED] += 1
                else:
                    warn_once(f"issuer sheet={label} 묶음 {block_id} 작성자 AI인데 아직 파일에 없음 → 마감 안 함")
                continue
            if block_id in filed or scfg.get("kind") not in LINE_BUILDERS:
                continue
            reason = self.waiting_reason(scfg, rows)
            if reason:
                continue
            try:
                self.statement(key, scfg, snap, block_id, rows)
            except LineError as e:
                warn_once(f"issuer sheet={label} row={rows[0]['rownum']} 묶음 {block_id} 발행 조건은 맞지만 줄을 못 만듦: {e}")
                continue
            for r in rows:
                writer.add(r["rownum"], "status", ST_PENDING, r["row"], row_id=r["row_id"], expect_fp=r.get("fp"))
                r["status"] = ST_PENDING
                if not self.dry_run:
                    self.con.execute("UPDATE rows SET status=? WHERE row_id=?", (ST_PENDING, r["row_id"]))
            counts[ST_PENDING] += 1
            logger.info("issuer sheet={} row={} client={} 묶음 {} ({}줄) → 발행대기", label, rows[0]["rownum"],
                        rows[0].get("client_resolved"), block_id, len(rows))
        written = writer.flush(snap["ws"], self.dry_run)
        if counts:
            logger.info("issuer sheet={} 발행대기={} AI발행={}{}", label, counts[ST_PENDING], counts[ST_ISSUED],
                        " (dry-run)" if self.dry_run else f" 쓰기={written}")

    def human_author(self, scfg: dict, rows: list[dict]) -> str | None:
        """묶음 작성자 잠금: 시트 칸 또는 watcher가 정한 묶음 작성자(메모 행·다른 줄 포함) 중 사람 값."""
        for r in rows:
            for a in (self._cell(scfg, r, "author"), r.get("author") or ""):
                if a and a != self.ai:
                    return a
        return None

    def waiting_reason(self, scfg: dict, rows: list[dict]) -> str | None:
        """발행대기로 갈 수 없는 이유 (None = 조건 충족). 대부분 '아직 기다림'이라 로그는 안 남김."""
        for r in rows:
            st = r["status"]
            if st != self._cell(scfg, r, "status"):
                return "이번 바퀴에 상태가 바뀜 (단가 칸이 아직 시트 값 기준이 아님)"
            if st == ST_CHECK:
                try:
                    parse_price(self._cell(scfg, r, "unit_price"))
                except LineError:
                    return "견적확인필요 (사람 단가 없음)"
            elif st != ST_QUOTED:
                return f"상태 {st or '사람처리'}"
            if self._cell(scfg, r, "author") or r.get("author"):
                return "작성자 있음"
            if r.get("legacy"):
                return "기존건"
            if bool_state(self._cell(scfg, r, "work_end")) != "TRUE":
                return "작업완료 대기"
        if not any(r.get("work_end_flipped_at") for r in rows):
            return "작업완료 전환을 에이전트가 보지 못함"
        return None

    def statement(self, key: str, scfg: dict, snap: dict, block_id: str, rows: list[dict]) -> Statement:
        first = rows[0]
        client, reg_no = first.get("client_resolved"), first.get("client_reg_no") or ""
        if not client:
            raise LineError("거래처 미해석")
        if not REG_NO_RE.match(reg_no):
            raise LineError(f"사업자번호 없음/형식 아님({reg_no!r})")
        day = block_date(first.get("block_date") or "", snap["ws"].title, self.today)
        builder = LINE_BUILDERS[scfg["kind"]]
        lines = []
        for r in rows:
            try:
                lines.append(builder(lambda name, r=r: self._cell(scfg, r, name)))
            except LineError as e:
                raise LineError(f"row={r['rownum']} {e}") from None
        return Statement(block_id, client, reg_no, day, lines, sheet=key)

    def _close(self, key, label, scfg, block_id, rows, writer: SheetWriter, file_name: str) -> None:
        """사람이 업로드 후 작성자를 AI로 → 묶음 전체 AI발행 + 작성자 AI 통일."""
        ts = now_iso()
        for r in rows:
            if r["status"] in ("", None, ST_SKIP):  # 사람처리·제외 행은 손대지 않음
                continue
            if r["status"] != ST_ISSUED:
                writer.add(r["rownum"], "status", ST_ISSUED, r["row"], row_id=r["row_id"], expect_fp=r.get("fp"))
            if self._cell(scfg, r, "author") != self.ai:
                writer.add(r["rownum"], "author", self.ai, r["row"], row_id=r["row_id"], expect_fp=r.get("fp"))
            r["status"] = ST_ISSUED
            if not self.dry_run:
                self.con.execute("UPDATE rows SET status=?, author=?, issued_at=COALESCE(issued_at, ?) WHERE row_id=?",
                                 (ST_ISSUED, self.ai, ts, r["row_id"]))
        if not self.dry_run:
            self.con.execute("UPDATE issue_file_blocks SET issued_at=COALESCE(issued_at, ?) WHERE block_id=?",
                             (ts, block_id))
        logger.info("issuer sheet={} row={} client={} 묶음 {} 작성자 AI 확인 → AI발행 ({})", label, rows[0]["rownum"],
                    rows[0].get("client_resolved"), block_id, file_name)

    # ------------------------------------------------------------------ 파일 묶음
    def candidates(self, snapshots: dict[str, dict]) -> list[Candidate]:
        """발행대기인데 아직 파일에 없는 묶음. 실제 실행이면 시트를 다시 읽어 상태·내용 그대로인 것만."""
        out = []
        for key, scfg in self.cfg["sheets"].items():
            snap = snapshots.get(key)
            if not snap or scfg.get("kind") not in LINE_BUILDERS:
                continue
            filed = self.filed(key)
            blocks = [(bid, rows) for bid, rows in group_blocks(snap["rows"])
                      if bid not in filed and all(r["status"] == ST_PENDING for r in rows)
                      and not any(self._cell(scfg, r, "author") or r.get("author") for r in rows)]
            if not blocks:
                continue
            fresh = None if self.dry_run else self._fresh_rows(scfg, snap)
            for bid, rows in blocks:
                if fresh is not None and not self._still_pending(scfg, rows, fresh):
                    logger.info("issuer sheet={} 묶음 {} 읽은 뒤 시트가 바뀜 → 이번 파일에서 뺌 (다음 바퀴에 재판정)",
                                snap.get("label", key), bid)
                    continue
                try:
                    st = self.statement(key, scfg, snap, bid, rows)
                except LineError as e:
                    warn_once(f"issuer sheet={snap.get('label', key)} 묶음 {bid} 발행대기지만 줄을 못 만듦: {e}")
                    continue
                out.append(Candidate(key, snap["ws"].title, rows, st))
        return out

    def _fresh_rows(self, scfg, snap) -> dict[str, list[str]]:
        id_col = scfg["columns"]["row_id"] - 1
        fresh = {}
        for row in snap["ws"].get_all_values()[scfg["data_start_row"] - 1:]:
            rid = (row[id_col] if id_col < len(row) else "").strip()
            fresh.setdefault(rid, row)
        return fresh

    def _still_pending(self, scfg, rows, fresh) -> bool:
        writer = SheetWriter(scfg["columns"], scfg["data_start_row"], scfg["hash_columns"])
        for r in rows:
            row = fresh.get(r["row_id"])
            if row is None:
                return False
            now = {"row": row}
            if (self._cell(scfg, now, "status") != ST_PENDING or self._cell(scfg, now, "author")
                    or writer.fp(row) != r.get("fp")):
                return False
        return True

    def _file_name(self, out_dir: Path) -> str:
        base = datetime.now().strftime("%Y%m%d_%H%M")
        used = {fn for (fn,) in self.con.execute("SELECT file_name FROM issue_files")}
        name, n = f"{base}.xls", 1
        while name in used or (out_dir / name).exists():
            n += 1
            name = f"{base}_{n}.xls"
        return name

    def bundle(self, snapshots: dict[str, dict], out_dir: Path | None = None) -> Path | None:
        if not self.dry_run:
            self.con.commit()  # 아래 실패 시 rollback이 앞 단계 기록까지 되돌리지 않게
        cands = self.candidates(snapshots)
        if not cands:
            logger.info("issuer 파일 묶음: 새로 담을 발행대기 묶음 없음")
            return None
        if out_dir is None:
            out_dir = Path(tempfile.mkdtemp(prefix="outbox_dryrun_")) if self.dry_run else self.outbox_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        name = self._file_name(out_dir)
        path = out_dir / name
        statements = [c.statement for c in cands]
        n_lines = sum(len(s.lines) for s in statements)
        total = sum(s.supply + s.tax for s in statements)
        if self.dry_run:
            write_xls(statements, path, self.memo_text)
        else:
            try:
                self.con.execute("INSERT INTO issue_files(file_name, created_at, path, n_blocks, n_lines, total)"
                                 " VALUES(?,?,?,?,?,?)", (name, now_iso(), str(path), len(cands), n_lines, total))
                self.con.executemany(
                    "INSERT INTO issue_file_blocks(block_id, file_name, sheet, tab, client, reg_no, block_date,"
                    " n_lines, supply, tax, row_ids) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    [(c.statement.block_id, name, c.sheet, c.tab, c.statement.client, c.statement.reg_no,
                      c.statement.day.isoformat(), len(c.statement.lines), c.statement.supply, c.statement.tax,
                      json.dumps([r["row_id"] for r in c.rows])) for c in cands])
                write_xls(statements, path, self.memo_text)
                self.con.commit()
            except Exception:
                self.con.rollback()
                path.unlink(missing_ok=True)
                logger.exception("issuer 파일 {} 만들기 실패 → 기록 되돌림 (같은 묶음이 이미 다른 파일에 있을 수 있음)", name)
                return None
            self._mark_basis(snapshots, cands, name)
        for c in cands:
            s = c.statement
            logger.info("issuer {} ← {} {} {} {}줄 공급가 {:,} 세액 {:,} (묶음 {})", name, c.sheet, s.client,
                        s.day.isoformat(), len(s.lines), s.supply, s.tax, s.block_id)
        logger.info("issuer 파일 {}: 명세표 {}장 {}줄 합계 {:,}{}", path, len(cands), n_lines, total,
                    " (dry-run: 시트·DB 안 건드림)" if self.dry_run else " — 경리나라 엑셀일괄등록으로 올린 뒤 작성자를 AI로")
        return path

    def _mark_basis(self, snapshots, cands: list[Candidate], name: str) -> None:
        """산출근거 묶음 태그 뒤 '[→ 파일명]' (다음 바퀴부터는 watcher가 DB를 보고 같은 표시를 유지)."""
        by_sheet: dict[str, list[Candidate]] = {}
        for c in cands:
            by_sheet.setdefault(c.sheet, []).append(c)
        for key, cs in by_sheet.items():
            scfg = self.cfg["sheets"][key]
            if "basis" not in scfg["columns"]:
                continue
            writer = SheetWriter(scfg["columns"], scfg["data_start_row"], scfg["hash_columns"])
            for c in cs:
                for r in c.rows:
                    tag = (r.get("block_tag") or "") + f" [→ {name}]"
                    writer.add(r["rownum"], "basis", with_block_tag(tag.strip(), self._cell(scfg, r, "basis")),
                               r["row"], row_id=r["row_id"], expect_fp=r.get("fp"))
            writer.flush(snapshots[key]["ws"])


# ---------------------------------------------------------------- agent.py outbox
def format_outbox(con: sqlite3.Connection) -> list[str]:
    lines = []
    files = con.execute("SELECT file_name, created_at, n_blocks, n_lines, total FROM issue_files"
                        " ORDER BY created_at, file_name").fetchall()
    for fn, created, n_blocks, n_lines, total in files:
        lines.append(f"{fn}  ({created}, 명세표 {n_blocks}장 {n_lines}줄 합계 {total:,})")
        for bid, sheet, client, bdate, nl, supply, tax, removed, issued in con.execute(
                "SELECT block_id, sheet, client, block_date, n_lines, supply, tax, removed_at, issued_at"
                " FROM issue_file_blocks WHERE file_name=? ORDER BY rowid", (fn,)):
            st = Counter(s or "사람처리" for (s,) in con.execute(
                "SELECT status FROM rows WHERE block_id=? AND COALESCE(status, '') != '삭제됨'", (bid,)))
            status = next(iter(st)) if len(st) == 1 else ", ".join(f"{k}×{v}" for k, v in st.items()) or "-"
            extra = f" 뺌(수정됨 {removed})" if removed else ""
            extra += f" 발행 {issued}" if issued else ""
            lines.append(f"  {sheet} / {client} / {bdate} / {nl}줄 {supply + tax:,} / {status}{extra} / 묶음 {bid}")
    if not files:
        lines.append("outbox 파일 없음")
    return lines
