"""임가공 과거 사례(DB lines, kind='job') 적재 + 유사건 검색 인터페이스.

견적 엔진(아직 없음)과 백테스트가 함께 쓴다. **유사건 검색은 반드시 cutoff_date 이전 자료만** 본다 —
백테스트에서 그 행의 실제 발행 단가(=정답)나 그 뒤 자료가 후보로 새어 들어오지 않게.

DB lines에는 사업자번호가 비어 있어, 같은 날·같은 거래처 명세표(statements)에서 번호와 statement_id를 가져오고
(없으면 clients 표에서 그 이름의 번호가 하나뿐일 때만) 붙인다.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date


@dataclass
class DbLine:
    id: int
    date: date
    client: str
    reg_no: str
    item: str
    spec: str
    qty: float | None
    unit_price: int | None
    supply: int | None
    dia: float | None
    length: float | None
    tol_lo: float | None = None
    tol_hi: float | None = None
    statement_id: str = ""

    @property
    def client_key(self) -> str:
        """같은 거래처 판정 키: 사업자번호, 없으면 정규화 거래처명."""
        from watcher import compact_name

        return self.reg_no or "name:" + compact_name(self.client)[0]


def load_job_lines(con: sqlite3.Connection, start: str = "0000-00-00", end: str = "9999-99-99") -> list[DbLine]:
    """lines(kind='job') + 같은 날·같은 거래처 명세표 → 사업자번호·statement_id. 날짜·id 순."""
    stm = defaultdict(list)
    for sid, d, client, reg, item, spec in con.execute(
            "SELECT id, date, client, reg_no, item, spec FROM statements WHERE date BETWEEN ? AND ?", (start, end)):
        stm[(d, client)].append((sid, reg or "", item, spec))
    name_reg = defaultdict(set)
    for name, reg in con.execute("SELECT name, reg_no FROM clients WHERE reg_no IS NOT NULL AND reg_no<>''"):
        name_reg[name].add(reg)
    out = []
    for lid, d, client, item, spec, qty, up, sup, dia, length, lo, hi, reg in con.execute(
            "SELECT id, date, client, item, spec, qty, unit_price, supply, dia, length, tol_lo, tol_hi, reg_no"
            " FROM lines WHERE kind='job' AND date BETWEEN ? AND ? ORDER BY date, id", (start, end)):
        cands = stm.get((d, client), [])
        pick = [c for c in cands if c[2] == item and (c[3] or "") == (spec or "")] or cands
        sid = "|".join(str(c[0]) for c in pick) if len(pick) > 1 and len(cands) > 1 else (
            str(pick[0][0]) if pick else "")
        regs = {c[1] for c in pick if c[1]} or name_reg.get(client, set())
        reg = reg if reg and not str(reg).startswith("동명:") else (next(iter(regs)) if len(regs) == 1 else "")
        out.append(DbLine(lid, date.fromisoformat(d), client or "", reg, item or "", spec or "", qty, up, sup, dia,
                          length, lo, hi, sid))
    return out


@dataclass
class LineIndex:
    """거래처별 색인 (사업자번호 / 정규화 이름)."""
    by_reg: dict = field(default_factory=lambda: defaultdict(list))
    by_name: dict = field(default_factory=lambda: defaultdict(list))
    lines: list = field(default_factory=list)

    @classmethod
    def build(cls, lines: list[DbLine]) -> "LineIndex":
        from watcher import compact_name

        idx = cls(lines=list(lines))
        for ln in lines:
            if ln.reg_no:
                idx.by_reg[ln.reg_no].append(ln)
            key = compact_name(ln.client)[0]
            if key:
                idx.by_name[key].append(ln)
        return idx

    def client_lines(self, reg_no: str, name: str) -> list[DbLine]:
        from watcher import compact_name

        out = list(self.by_reg.get(reg_no, [])) if reg_no else []
        ids = {ln.id for ln in out}
        key = compact_name(name or "")[0]
        out += [ln for ln in self.by_name.get(key, []) if ln.id not in ids] if key else []
        return out


# ---------------------------------------------------------------- 유사건 검색 (엔진용 인터페이스)
@dataclass
class CaseQuery:
    """견적할 품목 하나. backtest 행(parse_row 결과)에서 그대로 만들 수 있다."""
    dia: float
    reg_no: str = ""
    client: str = ""
    tol_lo: float | None = None
    tol_hi: float | None = None
    length: float | None = None
    qty: int | None = None


@dataclass
class Case:
    line: DbLine
    same_client: bool
    score: float  # 작을수록 비슷함


def find_similar_cases(index: LineIndex, query: CaseQuery, cutoff_date: date, *, limit: int = 20,
                       dia_tol: float = 1.0, same_client_only: bool = False) -> list[Case]:
    """cutoff_date **이전**(당일 제외) DB 사례 중 구경이 ±max(dia_tol, 1%) 안인 것, 비슷한 순.

    순서: 같은 거래처 > 구경 차 > 공차 차 > 기장 비율 차 > 최근. 점수 가중치는 엔진에서 다시 정한다
    (지금은 정렬 기준만 고정). cutoff_date는 필수 — 백테스트에서 정답 누출 방지."""
    if cutoff_date is None:
        raise ValueError("cutoff_date 필수 (백테스트 정답 누출 방지)")
    from watcher import compact_name

    width = max(dia_tol, query.dia * 0.01)
    qkey = compact_name(query.client)[0] if query.client else ""
    out = []
    for ln in index.lines:
        if ln.date >= cutoff_date or ln.dia is None or abs(ln.dia - query.dia) > width:
            continue
        same = bool((query.reg_no and ln.reg_no == query.reg_no)
                    or (qkey and compact_name(ln.client)[0] == qkey))
        if same_client_only and not same:
            continue
        score = abs(ln.dia - query.dia) / width
        if query.tol_hi is not None and ln.tol_hi is not None:
            score += min(1.0, abs((ln.tol_hi - (ln.tol_lo or 0)) - (query.tol_hi - (query.tol_lo or 0))) / 0.05)
        if query.length and ln.length:
            score += min(1.0, abs(ln.length - query.length) / query.length)
        score += (cutoff_date - ln.date).days / 3650  # 10년에 1점: 같은 조건이면 최근
        out.append(Case(ln, same, score if same else score + 10))
    out.sort(key=lambda c: (c.score, -c.line.id))
    return out[:limit]
