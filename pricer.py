"""단가 산출 모듈.

v1: 호닝파이프(kind: pipe) — data/price_table.xlsx '삼화호닝' 시트 단가표로 결정적 계산.
    단가 = round10(1M당단가 × 가격인상상수 × 길이/1000), 소관이면 × 0.9
임가공(kind: job) — pricer_job 사다리(사전단가·동일품) → 미결은 pricer_ai(Claude, 일일 상한).
    confidence high만 견적완료. mid/low는 단가 + '[가견적-mid]'/'[가견적-low]' + 견적확인필요(발행 차단).
    사람이 상태 칸을 비우면 적힌 단가로 '견적완료(사람확인)', 단가를 고치면 고친 값으로 '견적완료(사람수정)'
    (job_quotes 표와 비교). 발행기·watcher는 둘 다 견적완료로 본다 (watcher.QUOTED_STATES)
    → 그 행은 lines(source='sheet')로 즉시 학습 (sheet_lines.py).

watcher가 한 바퀴 돈 뒤 그 결과(snapshot)를 받아 처리한다.
대상: 상태 '확정' + 작성자 빈칸 + 기존건 아님. 시트 쓰기는 시트당 batch_update 한 번
(agent.py run·issue: watcher와 같은 writer — 사람 '확인'/단가 수정 행은 중간 '확정' 없이 바로 견적완료(사람확인/사람수정)).
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from loguru import logger

from watcher import QUOTED_STATES, RECHECK_RE, ST_HCHECK, ST_QUOTED_EDIT, ST_QUOTED_OK, SheetWriter, strip_block_tag, with_block_tag

ROOT = Path(__file__).parent
PRICE_SHEET = "삼화호닝"
SOGWAN_RATE = Decimal("0.9")

ST_CONFIRMED = "확정"
HUMAN_BASIS_RE = re.compile(r"^사람 (?:수정|확인)")  # 사람 확정 행의 산출근거 머리 (process_job)
ST_QUOTED = "견적완료"
ST_CHECK = "견적확인필요"
ST_LEGACY = "기존건-확인필요"
GAGYEON_RE = re.compile(r"^\[가견적-(mid|low)\]\s*")  # 임가공 가견적 표시 (단가는 적되 발행 차단)
_PRICE_CELL_RE = re.compile(r"\d[\d,]*(?:/\d[\d,]*)*")


CONF_RANK = {"low": 0, "mid": 1, "high": 2}
MULTI_MAX_CONF = "mid"   # 다품목 한 행 (E에 '+'로 품목 여럿) confidence 상한 → 항상 견적확인필요
_MISMATCH_RE = re.compile(r"^단가 개수 불일치 \([^)]*\) \| ")


def _job_body(body: str) -> str:
    """산출근거에서 에이전트 표시('[가견적-…]', '단가 개수 불일치 …')를 뗀 본문.
    재확인 표시 '[연마량 -0.3 추가(…) — 단가 재확인] 사람 단가 N 유지'는 사유만 남김 ('연마량 -0.3 추가(…) | …')."""
    body = _MISMATCH_RE.sub("", GAGYEON_RE.sub("", body))
    m = RECHECK_RE.match(body)
    return f"{m.group(1)} | {body[m.end():]}".rstrip(" |") if m else body


def awaiting_human(body: str) -> bool:
    """단가는 적혀 있고 사람 확인을 기다리는 행: 가견적, 또는 사람 단가 유지 + 재확인 (watcher 견적 이후 변경)."""
    return bool(GAGYEON_RE.match(body) or RECHECK_RE.match(body))


def _fmt_won(price: str) -> str:
    """'25000' → '25,000', '6000/5000' → '6,000/5,000'."""
    return "/".join(f"{int(p):,}" if p.isdigit() else p for p in re.sub(r"[\s,]", "", price or "").split("/"))


def requote_result(rq: dict, price: str, basis: str, status: str) -> tuple[str, str, str]:
    """견적완료 후 큰 변경 재견적 결과 → (단가, 산출근거, 상태).
    high(견적완료) → '연마량 반영 재견적 25,000→32,000 | …'. mid/low → '[가견적-mid] 연마량 반영 재견적 … | …'
    + 견적확인필요. 단가를 못 냄(미결·AI 실패) → 이전 단가 유지 + '[가견적-low] … 실패 — 이전 단가 유지' + 견적확인필요
    (사람이 '확인'/비우면 적힌 이전 단가로 사람확인)."""
    old, kind = rq.get("old_price") or "", rq.get("kind") or "재견적"
    price = "" if price in (None, "") else str(price)
    if not price:
        if not old:
            return price, f"{kind} 실패 | {basis}", ST_CHECK
        return old, f"{gagyeon_tag('low')} {kind} 실패 — 이전 단가 {_fmt_won(old)} 유지 | {basis}", ST_CHECK
    head = f"{kind} {_fmt_won(old) or '없음'}→{_fmt_won(price)}"
    g = GAGYEON_RE.match(basis)
    if g:
        return price, f"{g.group(0)}{head} | {basis[g.end():]}", status
    return price, f"{head} | {basis}", status


def gagyeon_tag(conf: str) -> str:
    return f"[가견적-{conf}]"


def norm_price_cell(text) -> str:
    """'61,000' → '61000', '40,000 / 25,000' → '40000/25000'. 단가 모양이 아니면 ''."""
    s = re.sub(r"\s+", "", str(text or ""))
    return s.replace(",", "") if _PRICE_CELL_RE.fullmatch(s) else ""

H_P = "H.P"
SOGWAN = "소관"

_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[xX×*/]\s*(\d+(?:\.\d+)?)")


class QuoteError(ValueError):
    """견적확인필요 사유."""


@dataclass(frozen=True)
class PriceEntry:
    key: str            # '45x55'
    per_m: Decimal      # 1M당 단가 (원)
    factor: Decimal     # 가격인상 상수


# ---------------------------------------------------------------- 파싱
def _num(s: str) -> str:
    """'45.0' → '45', '76.30' → '76.3'"""
    d = Decimal(s).normalize()
    return format(d, "f")


def size_key(text) -> str | None:
    """'45x55' / '45X55' / '45*55' / '45/55' / '25x33(32)' → '45x55' (괄호 부분 무시)."""
    if text is None:
        return None
    s = re.sub(r"\(.*?\)", "", str(text))
    m = _SIZE_RE.search(s)
    if not m:
        return None
    return f"{_num(m.group(1))}x{_num(m.group(2))}"


def parse_length(text) -> Decimal:
    s = str(text or "").replace(",", "").strip().lower()
    s = re.sub(r"\s*(mm|l)$", "", s)
    try:
        v = Decimal(s)
    except Exception:
        raise QuoteError(f"길이 파싱 실패({text!r})") from None
    if v <= 0:
        raise QuoteError(f"길이 파싱 실패({text!r})")
    return v


def parse_qty(text) -> int:
    s = str(text or "").replace(",", "").strip()
    m = re.fullmatch(r"(\d+)\s*(개|ea|EA|본|pcs|PCS)?", s)
    if not m or int(m.group(1)) <= 0:
        raise QuoteError(f"수량 파싱 실패({text!r})")
    return int(m.group(1))


def parse_pipe_type(text) -> str:
    raw = str(text or "").strip()
    if not raw:
        raise QuoteError("구분(H.P/소관) 없음")
    if "소관" in raw:
        return SOGWAN
    compact = re.sub(r"[\s.]", "", raw).upper()
    if compact == "HP" or "호닝" in raw:
        return H_P
    raise QuoteError(f"구분(H.P/소관) 인식 불가({raw!r})")


def round10(v: Decimal) -> int:
    """10원 단위 반올림."""
    return int((v / 10).quantize(Decimal(1), rounding=ROUND_HALF_UP) * 10)


# ---------------------------------------------------------------- 단가표
def load_price_table(path: str | Path) -> dict[str, PriceEntry]:
    """'삼화호닝' 시트: A 규격, D 가격인상 상수, E 가격(2023기준, 1M당)."""
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb[PRICE_SHEET]
    table: dict[str, PriceEntry] = {}
    for row in ws.iter_rows(min_row=1, values_only=True):
        row = tuple(row) + (None,) * (5 - len(row))
        spec, factor, per_m = row[0], row[3], row[4]
        key = size_key(spec)
        if key is None or not isinstance(per_m, (int, float)) or per_m <= 0:
            continue  # 제목/헤더/합계/빈 행
        f = Decimal(str(factor)) if isinstance(factor, (int, float)) and factor > 0 else Decimal(1)
        if key in table:
            logger.warning("단가표 규격 중복 {} ({!r}) → 첫 값 사용", key, spec)
            continue
        table[key] = PriceEntry(key, Decimal(str(per_m)), f)
    wb.close()
    return table


def quote_pipe(table: dict[str, PriceEntry], size, length, qty, pipe_type) -> tuple[int, str]:
    """(단가, 산출근거). 실패 시 QuoteError(사유)."""
    kind = parse_pipe_type(pipe_type)
    key = size_key(size)
    if key is None:
        raise QuoteError(f"규격 파싱 실패({size!r})")
    entry = table.get(key)
    if entry is None:
        raise QuoteError(f"단가표에 규격 없음({key})")
    length_mm = parse_length(length)
    parse_qty(qty)

    rate = SOGWAN_RATE if kind == SOGWAN else Decimal(1)
    price = round10(entry.per_m * entry.factor * length_mm / 1000 * rate)

    per_m = f"{entry.per_m:,.0f}" if entry.per_m == entry.per_m.to_integral() else f"{entry.per_m:,}"
    basis = f"단가표 {key} {per_m}/m × {_num(str(length_mm))}mm"
    if entry.factor != 1:
        basis += f" × 상수 {_num(str(entry.factor))}"
    if kind == SOGWAN:
        basis += " × 0.9(소관)"
    return price, basis


# ---------------------------------------------------------------- 시트 처리
class Pricer:
    def __init__(self, cfg: dict, con, dry_run: bool = False):
        self.cfg = cfg
        self.con = con
        self.dry_run = dry_run
        self._tables: dict[str, tuple[float, dict[str, PriceEntry]]] = {}

    def table_for(self, scfg: dict) -> dict[str, PriceEntry]:
        path = Path(scfg.get("price_table") or "data/price_table.xlsx")
        if not path.is_absolute():
            path = ROOT / path
        mtime = path.stat().st_mtime
        cached = self._tables.get(str(path))
        if cached is None or cached[0] != mtime:  # 파일이 바뀌면 다시 읽음
            table = load_price_table(path)
            logger.info("단가표 로드 {} 규격 {}개", path.name, len(table))
            cached = (mtime, table)
            self._tables[str(path)] = cached
        return cached[1]

    @staticmethod
    def writer_for(snap: dict, cols: dict, scfg: dict, label: str):
        """(writer, watcher와 같이 쓰는지, 시작 시 쌓인 쓰기 수). agent.py run은 watcher writer를 같이 써서
        watcher의 중간 상태('확정')가 이 단계의 최종 상태로 덮인 채 한 번에 써짐."""
        w = snap.get("writer")
        if w is not None:
            return w, True, len(w)
        return SheetWriter(cols, scfg["data_start_row"], scfg["hash_columns"], label), False, 0

    def flush_own(self, writer, shared: bool, base: int, snap: dict) -> str:
        if shared:
            return f" 쓰기 대기={len(writer) - base} (watcher와 함께 씀)"
        if self.dry_run:
            return f" (dry-run, 미기록 쓰기 {len(writer)}건)"
        return f" 쓰기={writer.flush(snap['ws'], self.dry_run)}"

    def run_once(self, snapshots: dict[str, dict]) -> None:
        for key, scfg in self.cfg["sheets"].items():
            kind = scfg.get("kind")
            if kind not in ("pipe", "job") or key not in snapshots:
                continue
            try:
                if kind == "pipe":
                    self.process_pipe(key, scfg, snapshots[key])
                else:
                    self.process_job(key, scfg, snapshots[key])
            except Exception:
                logger.exception("pricer sheet={} 처리 실패", key)
        if not self.dry_run:
            self.con.commit()

    def process_pipe(self, key: str, scfg: dict, snap: dict) -> None:
        cols = scfg["columns"]
        label = snap.get("label", key)
        table = self.table_for(scfg)
        # 쓰기는 row_id 기준 — watcher가 읽은 뒤 행이 삭제·이동되면 새 위치로, 내용이 바뀌었으면 보류
        writer, shared, base = self.writer_for(snap, cols, scfg, label)
        counts = Counter()

        def cell(row, name):
            return row[cols[name] - 1].strip()

        for r in snap["rows"]:
            # 동명확인: 기본 쪽 거래처로 가견적만 (동명확인에 막 들어온 바퀴에 한 번). 상태는 동명확인 그대로
            # → 견적완료가 안 되므로 발행 대상이 아님. 거래처 칸을 고치면 watcher가 확정 → 여기서 정식 견적.
            hcheck = r["status"] == ST_HCHECK and r.get("needs_quote")
            if (r["status"] != ST_CONFIRMED and not hcheck) or r["author"] or r.get("legacy"):
                continue
            row, rownum = r["row"], r["rownum"]

            def set_cell(rownum, name, value, row=row, r=r):
                writer.add(rownum, name, value, row, row_id=r["row_id"], expect_fp=r.get("fp"))

            tag = r.get("block_tag") or ""
            # 산출근거의 묶음 표시는 watcher가 붙인 것 — 그것만 있으면 근거 없는 것으로 봄
            if cell(row, "unit_price") and not strip_block_tag(cell(row, "basis")):
                # 사람이 적은 가격 → 건드리지 않음
                new_status, msg = ST_QUOTED, "사람 입력 가격 유지"
            else:
                try:
                    price, basis = quote_pipe(table, cell(row, "size"), cell(row, "length"),
                                              cell(row, "qty"), cell(row, "pipe_type"))
                    set_cell(rownum, "unit_price", price)
                    set_cell(rownum, "basis", with_block_tag(tag, basis) if tag else basis)
                    new_status, msg = ST_QUOTED, f"{price:,} ({basis})"
                except QuoteError as e:
                    set_cell(rownum, "unit_price", "")
                    set_cell(rownum, "basis", with_block_tag(tag, str(e)) if tag else str(e))
                    new_status, msg = ST_CHECK, str(e)
            if hcheck:
                new_status = ST_HCHECK  # 단가만 적고 발행 보류
                msg += " (동명확인 — 발행 안 함)"
            else:
                set_cell(rownum, "status", new_status)
            counts[new_status] += 1
            r["status"] = new_status
            logger.info("pricer sheet={} row={} client={} {} : {}", label, rownum, cell(row, "client"), new_status, msg)
            if not self.dry_run:
                self.con.execute("UPDATE rows SET status=? WHERE row_id=?", (new_status, r["row_id"]))

        tail = self.flush_own(writer, shared, base, snap)
        logger.info("pricer sheet={} 견적완료={} (사람수정 {} 사람확인 {}) 견적확인필요={} 동명확인가견적={}{}", label,
                    sum(counts[s] for s in QUOTED_STATES), counts[ST_QUOTED_EDIT], counts[ST_QUOTED_OK],
                    counts[ST_CHECK], counts[ST_HCHECK], tail)

    # ------------------------------------------------------------ 임가공 (kind: job)
    def job_index(self):
        """DB lines(kind=job) 색인. 줄 수·마지막 id가 바뀌면(import 후) 다시 만든다."""
        from cases import LineIndex, load_job_lines

        key = self.con.execute("SELECT COUNT(*), MAX(id) FROM lines WHERE kind='job'").fetchone()
        if getattr(self, "_job_index", None) is None or self._job_index[0] != key:
            self._job_index = (key, LineIndex.build(load_job_lines(self.con)))
        return self._job_index[1]

    def ai(self):
        """AiPricer (처음 필요할 때 생성). 만들 수 없으면 None → 미결 행은 견적확인필요."""
        if getattr(self, "_ai", None) is None:
            try:
                from pricer_ai import AiPricer

                self._ai = AiPricer(source="ops")
            except Exception as e:  # 키 없음·규칙 파일 없음 등
                logger.warning("AI 견적기 준비 실패 → 미결 행은 견적확인필요: {}", e)
                self._ai = False
        return self._ai or None

    # 에이전트가 적은 임가공 단가·그때 지문 — 사람이 상태를 비웠는지/단가를 고쳤는지 판정용.
    # price·fp는 마지막으로 확인된 단가(사람 확정 뒤엔 사람 단가) — 판정용.
    # agent_*는 에이전트 견적 그대로 남기고 human_*는 사람 확정 단가 → "에이전트 vs 사람" 교정 세션 재료.
    JOB_QUOTES_EXTRA = {"agent_price": "TEXT", "agent_method": "TEXT", "agent_conf": "TEXT",
                        "human_price": "TEXT", "human_at": "TEXT"}

    def _job_quotes_table(self) -> None:
        if getattr(self, "_jq_ready", False):
            return
        self.con.execute("CREATE TABLE IF NOT EXISTS job_quotes (row_id TEXT PRIMARY KEY, price TEXT, fp TEXT,"
                         " method TEXT, confidence TEXT, basis TEXT, quoted_at TEXT)")
        have = {r[1] for r in self.con.execute("PRAGMA table_info(job_quotes)")}
        new = [c for c in self.JOB_QUOTES_EXTRA if c not in have]
        for c in new:
            self.con.execute(f"ALTER TABLE job_quotes ADD COLUMN {c} {self.JOB_QUOTES_EXTRA[c]}")
        if new:  # 옛 행 채우기: 에이전트 견적 행은 그대로, 사람 행은 산출근거 '(에이전트 X)'에서
            self.con.execute("UPDATE job_quotes SET agent_price=price, agent_method=method, agent_conf=confidence"
                             " WHERE agent_price IS NULL AND method NOT IN ('사람수정', '사람확인')")
            for rid, basis, price in self.con.execute(
                    "SELECT row_id, basis, price FROM job_quotes WHERE method IN ('사람수정', '사람확인')").fetchall():
                m = re.search(r"\(에이전트 ([^)]*)\)", basis or "")
                agent = (m.group(1) if m else price) if (m or (basis or "").startswith("사람 확인")) else None
                self.con.execute("UPDATE job_quotes SET agent_price=?, human_price=? WHERE row_id=?",
                                 (None if agent in (None, "없음") else norm_price_cell(agent), price, rid))
        self._jq_ready = True

    def job_quote_get(self, row_id) -> tuple[str, str] | None:
        """(에이전트가 적은 단가, 그때 행 지문) 또는 None."""
        self._job_quotes_table()
        return self.con.execute("SELECT price, fp FROM job_quotes WHERE row_id=?", (row_id,)).fetchone()

    def job_quote_put(self, row_id, price, fp, method="", conf="", basis="") -> None:
        """에이전트 견적 기록 (agent_*도 같은 값, human_* 비움)."""
        if self.dry_run:
            return
        from datetime import datetime

        self._job_quotes_table()
        p = norm_price_cell(price)
        self.con.execute("INSERT OR REPLACE INTO job_quotes(row_id, price, fp, method, confidence, basis, quoted_at,"
                         " agent_price, agent_method, agent_conf, human_price, human_at)"
                         " VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                         (row_id, p, fp or "", method, conf or "", basis, datetime.now().isoformat(timespec="seconds"),
                          p, method, conf or ""))

    def job_quote_human(self, row_id, price, fp, method, basis) -> None:
        """사람 확정 기록: price·fp·method·basis는 사람 것으로, agent_*는 그대로 두고 human_price·human_at 기록."""
        if self.dry_run:
            return
        from datetime import datetime

        self._job_quotes_table()
        p, ts = norm_price_cell(price), datetime.now().isoformat(timespec="seconds")
        self.con.execute("UPDATE job_quotes SET price=?, fp=?, method=?, confidence='', basis=?, quoted_at=?,"
                         " human_price=?, human_at=? WHERE row_id=?", (p, fp or "", method, basis, ts, p, ts, row_id))

    def learn_sheet_row(self, r: dict, row: list, cols: dict, title: str) -> None:
        """사람이 확정한 임가공 행 → lines(source='sheet') 즉시 추가 (sheet_lines). 줄을 못 만들면 로그만."""
        if self.dry_run:
            return
        from issuer.excel_upload import LineCtx, LineError, block_date, job_lines
        import sheet_lines

        client, reg_no = r.get("client_resolved") or "", r.get("client_reg_no") or ""
        if not client:
            return
        try:
            day = block_date(r.get("block_date") or "", title)
            get = lambda name: (row[cols[name] - 1].strip() if cols.get(name) and cols[name] - 1 < len(row) else "")
            lines = job_lines(get, LineCtx(client, reg_no, day, self.job_index()))
        except LineError as e:
            logger.warning("시트 확정 줄 학습 건너뜀 row_id={} 거래처={}: {}", r["row_id"], client, e)
            return
        n = sheet_lines.add_sheet_lines(self.con, r["row_id"], client, reg_no, day, lines, note="시트 확정")
        logger.info("시트 확정 줄 학습 row_id={} 거래처={} {} 줄 {}", r["row_id"], client, day, n)

    def quote_job_row(self, inp, index, cutoff) -> tuple[str, str, str, str, str]:
        """JobInput → (단가 칸, 산출근거, 상태, method, confidence). 사다리 → 미결이면 Claude.
        confidence high만 견적완료, mid/low는 '[가견적-mid/low]' + 견적확인필요, 실패는 단가 없이 견적확인필요."""
        from pricer_ai import AiError
        from pricer_job import M_PENDING, quote_ladder, split_items

        segs = split_items(inp)
        if len(segs) > 1:
            return self.quote_job_multi(inp, segs, index, cutoff)
        q = quote_ladder(inp, index, cutoff)
        if q.method == M_PENDING:
            if self.dry_run:  # 시트에 안 쓰면 다음 바퀴에 또 미결 → 매 바퀴 API 비용. dry-run은 호출 안 함
                return "", f"미결 | {q.basis} | dry-run AI 생략", ST_CHECK, M_PENDING, ""
            ai = self.ai()
            if ai is None:
                return "", f"미결 | {q.basis} | AI 없음", ST_CHECK, M_PENDING, ""
            try:
                q = ai.quote(inp, index, cutoff, hint=q.hint, tag=inp.client, max_conf=q.max_conf)
            except AiError as e:
                return "", f"미결 | {q.basis} | {e}", ST_CHECK, M_PENDING, ""
        price = "/".join(str(p) for _, p in q.item_prices) if q.item_prices else q.unit_price
        if q.confidence == "high":
            return price, q.label(), ST_QUOTED, q.method, q.confidence
        return price, f"{gagyeon_tag(q.confidence)} {q.label()}", ST_CHECK, q.method, q.confidence or ""

    def quote_job_multi(self, inp, segs, index, cutoff) -> tuple[str, str, str, str, str]:
        """다품목 한 행 (E에 '+'로 품목 여럿): 구간마다 사다리, 미결 구간은 Claude 한 번 호출로 함께.
        단가 칸 '60000/55000/50000', 산출근거는 구간별 '; ', confidence는 구간 중 최저이되 최대 mid
        → 항상 '[가견적-…]' + 견적확인필요. 한 구간이라도 단가를 못 내면 단가 비우고 견적확인필요."""
        from pricer_ai import AiError
        from pricer_job import M_PENDING, quote_ladder

        n = len(segs)
        qs = [quote_ladder(s, index, cutoff) for s in segs]
        pending = [i for i, q in enumerate(qs) if q.method == M_PENDING]

        def joined(quotes):
            return "; ".join(f"구간{i + 1} {q.label()}" for i, q in enumerate(quotes))

        if pending:
            why = None
            if self.dry_run:  # 매 바퀴 API 비용 방지 (단품과 같음)
                why = "dry-run AI 생략"
            elif (ai := self.ai()) is None:
                why = "AI 없음"
            else:
                try:
                    for i, q in zip(pending, ai.quote_multi(inp, segs, qs, index, cutoff, tag=inp.client)):
                        qs[i] = q
                except AiError as e:
                    why = str(e)
            if why:
                return "", f"미결 | 다품목 {n}구간: {joined(qs)} | {why}", ST_CHECK, M_PENDING, ""
        conf = min((q.confidence or "low" for q in qs), key=lambda c: CONF_RANK[c])
        if CONF_RANK[conf] > CONF_RANK[MULTI_MAX_CONF]:
            conf = MULTI_MAX_CONF
        price = "/".join(str(q.unit_price) for q in qs)
        basis = f"{gagyeon_tag(conf)} 다품목 {n}구간 (최대 {MULTI_MAX_CONF}): {joined(qs)}"
        return price, basis, ST_CHECK, "다품목", conf

    def requote_get(self, row_id) -> dict | None:
        """watcher가 남긴 재견적 메모 (견적완료 후 큰 변경). 없으면 None."""
        try:
            got = self.con.execute("SELECT requote FROM rows WHERE row_id=?", (row_id,)).fetchone()
        except sqlite3.OperationalError:  # rows 표·열이 없는 DB (pricer 단독 테스트 등)
            return None
        try:
            return json.loads(got[0]) if got and got[0] else None
        except ValueError:
            return None

    def process_job(self, key: str, scfg: dict, snap: dict) -> None:
        """임가공 시트. 확정 행 → 사다리/Claude 견적. 견적확인필요 행 → 사람이 단가를 고쳤으면 그 값으로 견적완료.
        확정으로 돌아온 가견적 행(사람이 상태 칸을 비움, 내용 지문 그대로) → 적힌 단가로 견적완료.
        임가공은 아직 발행기 미연결."""
        from datetime import date, timedelta

        from pricer_job import JobInput, item_count

        cols = dict(scfg["columns"])
        cols.setdefault("pre_size", cols.get("stock_removal"))  # 시트 H 연마량/치대 = 정답지 pre_size
        label = snap.get("label", key)
        writer, shared, base = self.writer_for(snap, scfg["columns"], scfg, label)
        counts = Counter()
        index = None
        cutoff = date.today() + timedelta(days=1)  # 운영: 오늘까지의 DB 자료 전부

        def cell(row, name):
            i = cols.get(name)
            return row[i - 1].strip() if i and i - 1 < len(row) else ""

        for r in snap["rows"]:
            if r["author"] or r.get("legacy"):
                continue
            status = r["status"]
            hcheck = status == ST_HCHECK and r.get("needs_quote")
            row, rownum = r["row"], r["rownum"]
            price_cell, body = cell(row, "unit_price"), strip_block_tag(cell(row, "basis"))
            tag = r.get("block_tag") or ""

            def set_cell(rownum, name, value, row=row, r=r):
                writer.add(rownum, name, value, row, row_id=r["row_id"], expect_fp=r.get("fp"))

            if status == ST_QUOTED and HUMAN_BASIS_RE.match(body):
                # 사람 확정인데 상태가 그냥 '견적완료' (라벨 도입 전에 확정된 행 등) → 라벨만 바로잡음 (재학습 없음)
                new_status = ST_QUOTED_EDIT if body.startswith("사람 수정") else ST_QUOTED_OK
                set_cell(rownum, "status", new_status)
                counts[new_status] += 1
                r["status"] = new_status
                logger.info("pricer sheet={} row={} client={} {} → {} (산출근거 '{}')", label, rownum,
                            cell(row, "client"), status, new_status, body[:5])
                if not self.dry_run:
                    self.con.execute("UPDATE rows SET status=? WHERE row_id=?", (new_status, r["row_id"]))
                continue
            if status not in (ST_CONFIRMED, ST_CHECK) and not hcheck:
                continue

            def set_basis(text, tag=tag):
                set_cell(rownum, "basis", with_block_tag(tag, text) if tag else text)

            learn = False   # 사람 확정 → lines(source='sheet') 즉시 학습
            prev = self.job_quote_get(r["row_id"]) if status in (ST_CONFIRMED, ST_CHECK) else None
            human_price = norm_price_cell(price_cell)
            human_edit = human_price and prev is not None and (
                (status == ST_CHECK and human_price != (prev[0] or ""))
                or (status == ST_CONFIRMED and prev[1] == (r.get("fp") or "") and awaiting_human(body)))
            n_items = item_count(JobInput.from_sheet_row(row, cols, "", "").raw) if human_edit else 1
            if human_edit and len(human_price.split("/")) != n_items:
                # 단가 개수 ≠ 품목 수 → 견적확인필요 유지 + 산출근거에 표시 (같은 표시가 이미 있으면 그대로)
                g, rc = GAGYEON_RE.match(body), RECHECK_RE.match(body)
                note = f"단가 개수 불일치 (단가 {len(human_price.split('/'))}개 ≠ 품목 {n_items}개)"
                if rc:  # 재확인 표시는 남김 (사람이 '확인'하면 적힌 단가로 사람확인)
                    new_body = f"[{rc.group(1)} — 단가 재확인] | {note} | {_MISMATCH_RE.sub('', body[rc.end():])}"
                else:
                    new_body = f"{g.group(0) if g else ''}{note} | {_job_body(body)}"
                if new_body == body and status == ST_CHECK:
                    continue
                set_basis(new_body)
                new_status, msg = ST_CHECK, f"사람 단가 {price_cell} — {note}"
            elif status == ST_CHECK:
                # 견적확인필요 + 사람이 단가를 고침(빈칸이던 것을 채운 것 포함) → 그 값으로 견적완료
                if prev is None or not human_price or human_price == (prev[0] or ""):
                    continue
                new_status = ST_QUOTED_EDIT
                basis = f"사람 수정 {price_cell} (에이전트 {prev[0] or '없음'}) | {_job_body(body)}"
                set_basis(basis)
                self.job_quote_human(r["row_id"], human_price, r.get("fp"), "사람수정", basis)
                learn = True
                msg = f"사람 단가 {price_cell} → {new_status}"
            elif (status == ST_CONFIRMED and prev is not None and prev[1] == (r.get("fp") or "")
                  and human_price and awaiting_human(body)):
                # 가견적(또는 재확인) 행인데 내용 그대로 확정으로 돌아옴 = 사람이 상태 칸을 비움 → 적힌 단가로 견적완료
                # 단가도 고쳤으면 사람수정, 상태 칸만 비웠으면 사람확인
                same = human_price == prev[0]
                new_status = ST_QUOTED_OK if same else ST_QUOTED_EDIT
                who = "사람 확인" if same else f"사람 수정 {price_cell} (에이전트 {prev[0]})"
                basis = f"{who} | {_job_body(body)}"
                set_basis(basis)
                self.job_quote_human(r["row_id"], human_price, r.get("fp"), "사람확인" if same else "사람수정", basis)
                learn = True
                msg = f"{who} (상태 칸 비움) → {new_status}"
            elif price_cell and not body:
                new_status, msg = ST_QUOTED, "사람 입력 가격 유지"
            else:
                index = index or self.job_index()
                inp = JobInput.from_sheet_row(row, cols, r.get("client_resolved") or "",
                                              r.get("client_reg_no") or "", date.today())
                rq = self.requote_get(r["row_id"]) if status == ST_CONFIRMED else None
                if rq:
                    old = rq.get("old_price") or ""
                    inp.change_note = rq["note"] + (f" (변경 전 견적 {_fmt_won(old)}원)" if old else "")
                price, basis, new_status, method, conf = self.quote_job_row(inp, index, cutoff)
                if rq:
                    price, basis, new_status = requote_result(rq, price, basis, new_status)
                    if not self.dry_run:
                        self.con.execute("UPDATE rows SET requote=NULL WHERE row_id=?", (r["row_id"],))
                set_cell(rownum, "unit_price", price)
                set_basis(basis)
                self.job_quote_put(r["row_id"], price, r.get("fp"), method, conf, basis)
                raw = " ".join(f"{k}={v!r}" for k, v in inp.raw.items() if v)
                logger.info("임가공 견적 sheet={} row={} 거래처={} | 입력 {} | 단가 {} | {} | {} | {}", label, rownum,
                            inp.client or cell(row, "client"), raw, price or "-", method, conf or "-", new_status)
                msg = f"{price} ({basis})"
            if hcheck:
                new_status = ST_HCHECK
                msg += " (동명확인 — 발행 안 함)"
            else:
                set_cell(rownum, "status", new_status)
                if learn:  # 동명확인(거래처 미정)은 학습 안 함
                    self.learn_sheet_row(r, row, cols, getattr(snap.get("ws"), "title", ""))
            counts[new_status] += 1
            r["status"] = new_status
            logger.info("pricer sheet={} row={} client={} {} : {}", label, rownum, cell(row, "client"),
                        new_status, msg)
            if not self.dry_run:
                self.con.execute("UPDATE rows SET status=? WHERE row_id=?", (new_status, r["row_id"]))

        tail = self.flush_own(writer, shared, base, snap)
        logger.info("pricer sheet={} 견적완료={} (사람수정 {} 사람확인 {}) 견적확인필요={} 동명확인가견적={}{}", label,
                    sum(counts[s] for s in QUOTED_STATES), counts[ST_QUOTED_EDIT], counts[ST_QUOTED_OK],
                    counts[ST_CHECK], counts[ST_HCHECK], tail)
