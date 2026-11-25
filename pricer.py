"""단가 산출 모듈.

v1: 호닝파이프(kind: pipe) — data/price_table.xlsx '삼화호닝' 시트 단가표로 결정적 계산.
    단가 = round10(1M당단가 × 가격인상상수 × 길이/1000), 소관이면 × 0.9
임가공(kind: job) — DB 과거 사례 + config/pricing_rules.md 규칙 + Claude 판단 (TODO)

watcher가 한 바퀴 돈 뒤 그 결과(snapshot)를 받아 처리한다.
대상: 상태 '확정' + 작성자 빈칸 + 기존건 아님. 시트 쓰기는 시트당 batch_update 한 번.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from loguru import logger

from watcher import SheetWriter, strip_block_tag, with_block_tag

ROOT = Path(__file__).parent
PRICE_SHEET = "삼화호닝"
SOGWAN_RATE = Decimal("0.9")

ST_CONFIRMED = "확정"
ST_QUOTED = "견적완료"
ST_CHECK = "견적확인필요"
ST_LEGACY = "기존건-확인필요"

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

    def run_once(self, snapshots: dict[str, dict]) -> None:
        for key, scfg in self.cfg["sheets"].items():
            if scfg.get("kind") != "pipe" or key not in snapshots:
                continue
            try:
                self.process_pipe(key, scfg, snapshots[key])
            except Exception:
                logger.exception("pricer sheet={} 처리 실패", key)
        if not self.dry_run:
            self.con.commit()

    def process_pipe(self, key: str, scfg: dict, snap: dict) -> None:
        cols = scfg["columns"]
        label = snap.get("label", key)
        table = self.table_for(scfg)
        # 쓰기는 row_id 기준 — watcher가 읽은 뒤 행이 삭제·이동되면 새 위치로, 내용이 바뀌었으면 보류
        writer = SheetWriter(cols, scfg["data_start_row"], scfg["hash_columns"], label)
        counts = Counter()

        def cell(row, name):
            return row[cols[name] - 1].strip()

        for r in snap["rows"]:
            if r["status"] != ST_CONFIRMED or r["author"] or r.get("legacy"):
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
            set_cell(rownum, "status", new_status)
            counts[new_status] += 1
            r["status"] = new_status
            logger.info("pricer sheet={} row={} client={} {} : {}", label, rownum, cell(row, "client"), new_status, msg)
            if not self.dry_run:
                self.con.execute("UPDATE rows SET status=? WHERE row_id=?", (new_status, r["row_id"]))

        written = writer.flush(snap["ws"], self.dry_run)
        logger.info("pricer sheet={} 견적완료={} 견적확인필요={}{}", label, counts[ST_QUOTED], counts[ST_CHECK],
                    f" (dry-run, 미기록 쓰기 {len(writer)}건)" if self.dry_run else f" 쓰기={written}")
