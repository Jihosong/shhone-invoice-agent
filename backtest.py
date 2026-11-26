"""임가공 견적 엔진 백테스트 정답지 (python agent.py backtest build).

임가공 시트의 그 해 월 탭 전부(왼쪽 탭만이 아니라 1월~현재 달)를 **읽기만** 해서 품목 행을 모으고,
행마다 입력 정보(D 주요 메시지, E 공차x갯수, F 난이도, 호닝 전 치수/연마량, 호닝 전·후 노트, 호닝 후 치수)를
파싱해 열로 만든 뒤, DB lines(kind='job')의 실제 발행 단가와 짝을 지어
data/backtest/imgagong_<연도>.csv/.xlsx 로 저장한다. 시트·DB에 쓰지 않는다.

탭마다 열 배치가 다르다 (1~7월: S 호닝 전 치수 / T 전 노트 / V 후 치수 / W 후 노트, 8~10월: 없음,
11월~: H 연마량/치대 / S 전 노트 / T 후 치수 / U 후 노트) → 2행(한국어 헤더) 글자로 열을 찾는다.

묶음 규칙은 watcher와 같다: 날짜 칸이 있는 행 = 묶음 첫 행(거래처는 이 행에서만 해석),
날짜 없는 행 = 위 묶음의 이어지는 줄, D·E 둘 다 비면 메모 행(건너뜀).

짝 찾기: 같은 사업자번호(없으면 정규화 거래처명), 날짜 ±10일, 구경 ±0.5mm(행의 구경 중 하나라도),
양쪽에 기장이 있으면 ±10%. 후보 하나 → exact, 여럿 → 날짜 가장 가까운 것 nearest, 없음 → none(+사유).
"""
from __future__ import annotations

import random
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "data" / "backtest"

DATE_WINDOW = 10      # 일
DIA_TOL = 0.5         # mm
LEN_TOL = 0.10        # 비율

# 2행 헤더 → 필드 (공백 무시 비교). 앞쪽 후보 우선.
HEADER_FIELDS = {
    "date": ("날짜",),
    "client": ("거래처",),
    "main_msg": ("주요메시지",),
    "spec_qty": ("공차x갯수,조도", "공차x갯수"),
    "difficulty": ("난이도",),
    "pre_size": ("호닝전치수", "연마량/치대"),
    "pre_note": ("호닝전매니저노트",),
    "post_size": ("호닝후치수",),
    "post_note": ("호닝후매니저노트",),
}
RAW_FIELDS = ("main_msg", "spec_qty", "difficulty", "pre_size", "pre_note", "post_size", "post_note")
RAW_LETTERS = {"main_msg": "D", "spec_qty": "E", "difficulty": "F", "pre_size": "H", "pre_note": "S",
               "post_size": "T", "post_note": "U"}  # 콘솔 표시용 (11월 탭 기준 글자)

# ---------------------------------------------------------------- 파서
_N = r"\d+(?:\.\d+)?"
_NUM_RE = re.compile(_N)
_DIA_RE = re.compile(r"(?:Ø|파이|내경)\s*(" + _N + r")")
_PAREN_RE = re.compile(r"\([^()]*\)")
_QTY_UNIT = r"(?:개|ea|pcs|본|set|조)"
_QTY_RE = re.compile(r"(\d+)\s*" + _QTY_UNIT + r"(?![a-z])", re.I)
_HOLE_RE = re.compile(r"(\d+)\s*hole", re.I)


def norm(text) -> str:
    """NFKC, Φ/φ/∅ → Ø, 줄바꿈·연속 공백 → 한 칸."""
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = re.sub(r"[ΦφØ∅⌀]", "Ø", s)
    return re.sub(r"\s+", " ", s).strip()


_DIA_SUFFIX_RE = re.compile(r"(" + _N + r")\s*파이")
# Ø 표기가 없을 때만: 숫자 바로 뒤 공차('스텐 100 +0.03~', 오타 'ㅊ55+0.0~'). 영문·숫자 뒤(모델명)는 제외
_DIA_BARE_RE = re.compile(r"(?<![A-Za-z0-9.#~/-])(" + _N + r")\s*(?=[+-]\s*\d*\.\d|\+\s*-|±)")


def _dia_spans(s: str) -> list[tuple[float, int, int]]:
    """정규화된 글자에서 구경 후보 (값, 시작, 끝) — 위치 순."""
    hits = [(float(m.group(1)), m.start(), m.end()) for rx in (_DIA_RE, _DIA_SUFFIX_RE) for m in rx.finditer(s)]
    if not hits:
        hits = [(float(m.group(1)), m.start(), m.end()) for m in _DIA_BARE_RE.finditer(s)
                if float(m.group(1)) >= 3]
    return sorted((h for h in hits if h[0] > 0), key=lambda h: h[1])


def parse_dias(text) -> list[float]:
    """'Ø100 ... + Ø58 ...' → [100.0, 58.0]. Ø·φ·파이·내경 앞뒤 숫자 (모델명 숫자 오인 방지)."""
    out = []
    for v, _, _ in _dia_spans(norm(text)):
        if v not in out:
            out.append(v)
    return out


def _first_segment(s: str) -> tuple[float | None, str]:
    """첫 구경과 그 뒤 품목 구간(다음 구경 앞까지)."""
    spans = _dia_spans(s)
    if not spans:
        return None, ""
    v, _, end = spans[0]
    nxt = next((st for _, st, _ in spans[1:] if st >= end), len(s))
    return v, s[end:nxt]


def _signed(sign: str, v: str) -> float:
    return -float(v) if sign == "-" else float(v)


def parse_tol(text) -> tuple[float | None, float | None]:
    """첫 구경 바로 뒤 공차 → (lo, hi). 괄호 안(실측 메모)은 무시.
    '+0.0~+0.07' / '0~ 0.1'(부호 없음=+) / '-0.0~-0.02' / '+-0.05'·'±0.05' / '+0.02'(→0~0.02) /
    '-0.2'(→-0.2~0) / 'Ø88.722~88.773'(절대치수 → 0~0.051)."""
    dia, seg = _first_segment(re.sub(r"~\s*Ø", "~", norm(text)))  # 'Ø11.005~Ø11.014'
    if dia is None:
        return None, None
    seg = _PAREN_RE.sub(" ", seg.split("/")[0])
    seg = re.sub(r"Ra\s*" + _N + r"(?:\s*~\s*" + _N + r")?", " ", seg, flags=re.I)
    m = re.match(r"\s*~\s*Ø?\s*(" + _N + r")", seg)
    if m and float(m.group(1)) >= 1:  # 절대치수 범위
        return 0.0, round(float(m.group(1)) - dia, 4)
    m = re.search(r"(?:±|\+\s*-|-\s*\+)\s*(" + _N + r")", seg)
    if m:
        v = float(m.group(1))
        return -v, v
    m = re.search(r"([+-]?)\s*(" + _N + r")\s*~\s*([+-]?)\s*(" + _N + r")", seg)
    if m and float(m.group(2)) < 1 and float(m.group(4)) < 1:
        a, b = _signed(m.group(1), m.group(2)), _signed(m.group(3), m.group(4))
        return min(a, b), max(a, b)
    m = re.search(r"(?<![\w.])([+-]?)\s*(" + _N + r")(?![\w.])", seg)
    if m and float(m.group(2)) < 1:
        v = _signed(m.group(1), m.group(2))
        return (v, 0.0) if v < 0 else (0.0, v)
    return None, None


def parse_length(text) -> float | None:
    """첫 품목 구간의 기장: '/ 862 / 149개' · '/1000(250) / 1개' · '/ 170L' · 'L300' · '기장 300'."""
    s = norm(text)
    dia, seg = _first_segment(s)
    seg = seg if dia is not None else s
    m = re.search(r"(?:기장|길이|(?<![A-Za-z0-9])L)\s*[:=]?\s*(" + _N + r")", seg)
    if m:
        return float(m.group(1))
    m = re.search(r"(" + _N + r")\s*L(?![a-z])", seg, re.I) or re.search(r"×\s*(" + _N + r")", seg)
    if m:
        return float(m.group(1))
    # '/ 862 /', '/1000(250) /', '/ 788~797 /'(앞 값), 끝의 '/ 300'
    m = re.search(r"/\s*(" + _N + r")\s*(?:~\s*" + _N + r")?\s*(?:\([^()]*\))?\s*(?=/|\+|$|,)", seg)
    return float(m.group(1)) if m else None


def parse_qty(text) -> int | None:
    """'N개'·'N EA'·'N본' 합계 ('/6개긴급 +8개' → 14, 여러 품목 → 합). 없으면 'N hole' 합."""
    s = norm(text)
    vals = [int(m.group(1)) for m in _QTY_RE.finditer(s)]
    if not vals:
        vals = [int(m.group(1)) for m in _HOLE_RE.finditer(s) if not re.match(r"\s*Ø", s[m.end():])]
    vals = [v for v in vals if v > 0]
    return sum(vals) if vals else None


MATERIALS = [  # (라벨, 패턴) — 짧은 영문은 앞뒤 영문자 없을 때만
    ("SUS", r"SUS|STS|스텐|스테인"),
    ("AL", r"(?<![A-Z])AL(?![A-Z])|알루미|알미늄|A[5-7]0\d\d"),
    ("FC", r"(?<![A-Z])FCD?(?![A-Z])|(?<![A-Z])FCD?\d+|주물|주철|덕타일"),
    ("SCM", r"SCM"),
    ("S45C", r"(?<![A-Z])SM?\d\dC(?![A-Z])"),
    ("SKD", r"SKD"),
    ("STKM", r"STKM"),
    ("티타늄", r"티타늄|(?<![A-Z])TI(?![A-Z])"),
    ("황동", r"황동|신주|BRASS"),
    ("청동", r"청동|구리|COPPER"),
    ("PVC", r"PVC"),
    ("테프론", r"테프론|PTFE"),
    ("인코넬", r"인코넬|INCONEL"),
]
SPECIALS = [
    ("막힘", r"막힘|막힌|블라인드|BLIND"),
    ("사각", r"사각"),
    ("열처리", r"열처리|침탄|고주파|질화"),
    ("도금", r"도금|크롬"),
    ("조도", r"(?<![A-Z])RA\s*\d|(?<![A-Z])RZ\s*\d|조도|#\d{3,4}"),
    ("치대", r"치대|연마량|호닝\s*전\s*[-+Ø]|MAKAN"),
    ("용접", r"용접"),
    ("얇은T", r"얇|박육|두께"),
    ("수리/재작업", r"수리|재호닝|재생|2차\s*호닝|스크[랏래]치"),
    ("면작업", r"면작업"),
    ("릴리프", r"릴리프|도피"),
    ("딥홀", r"딥홀|딮홀"),
    ("다공", r"\d\s*(?:구멍|HOLE)"),
    ("각도", r"각도"),
    ("긴급", r"긴급|급함|PAGI"),
]


def _labels(table, text) -> list[str]:
    s = norm(text).upper()
    return [label for label, pat in table if re.search(pat, s)]


def parse_materials(text) -> list[str]:
    return _labels(MATERIALS, text)


def parse_specials(text) -> list[str]:
    return _labels(SPECIALS, text)


def parse_ra(text) -> float | None:
    m = re.search(r"Ra\s*(" + _N + r")", norm(text), re.I)
    return float(m.group(1)) if m else None


def parse_sheet_date(text, year: int, tab_month: int) -> date | None:
    """'2/13', '2.13', '2월13일', '2026-02-13'. 연도 없으면 탭 달 기준 (1월 탭의 12/29 → 전년)."""
    s = norm(text)
    m = re.search(r"(20\d\d)[-./](\d{1,2})[-./](\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(\d{1,2})\s*(?:/|\.|월)\s*(\d{1,2})", s)
        if not m:
            return None
        mo, d = int(m.group(1)), int(m.group(2))
        y = year - 1 if mo - tab_month > 6 else year + 1 if tab_month - mo > 6 else year
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_row(raw: dict) -> dict:
    """시트 원문(RAW_FIELDS) → 파싱 열. 치수·수량은 E 우선, 없으면 D."""
    e, d = raw.get("spec_qty", ""), raw.get("main_msg", "")
    src = e if parse_dias(e) else d
    dias = parse_dias(src)
    tol_lo, tol_hi = parse_tol(src)
    qty = parse_qty(e) or parse_qty(d)
    alltext = " ".join(raw.get(k, "") for k in RAW_FIELDS)
    return {
        "dia": dias[0] if dias else None,
        "dia_all": " ".join(f"{v:g}" for v in dias),
        "tol_lo": tol_lo, "tol_hi": tol_hi,
        "length": parse_length(src) if dias else parse_length(e),
        "qty": qty,
        "material": ",".join(parse_materials(alltext)),
        "specials": ",".join(parse_specials(alltext)),
        "ra": parse_ra(e) or parse_ra(alltext),
    }


# ---------------------------------------------------------------- 시트 읽기
def _hkey(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def header_map(header: list[str]) -> dict[str, int]:
    """2행 헤더 → {필드: 0-based 열}."""
    keys = [_hkey(h) for h in header]
    out = {}
    for fld, names in HEADER_FIELDS.items():
        for n in names:
            if _hkey(n) in keys:
                out[fld] = keys.index(_hkey(n))
                break
    return out


def tab_month(title: str) -> int | None:
    m = re.fullmatch(r"(\d{1,2})월", title.strip())
    return int(m.group(1)) if m else None


def collect_rows(tabs: list[tuple[str, list[list[str]]]], year: int, master, data_start_row: int = 3):
    """[(탭 제목, get_all_values)] → (품목 행 list, 탭별 행 수, 중복 제거 수). 탭은 달 순서로."""
    rows, per_tab, dup = [], Counter(), 0
    seen = set()
    for title, values in sorted(tabs, key=lambda t: tab_month(t[0]) or 0):
        tm = tab_month(title)
        if not values or len(values) < 2:
            continue
        hm = header_map(values[1])
        if "date" not in hm or "client" not in hm:
            continue
        block = None
        for idx in range(data_start_row - 1, len(values)):
            row = values[idx]

            def cell(fld, row=row):
                i = hm.get(fld)
                return row[i].strip() if i is not None and i < len(row) else ""

            if cell("date"):
                ctext = cell("client")
                res = master.resolve_full(ctext) if ctext else None
                block = {"date_text": cell("date"), "date": parse_sheet_date(cell("date"), year, tm),
                         "client_text": ctext, "res": res, "rownum": idx + 1}
            elif block is None:
                continue
            if not (cell("main_msg") or cell("spec_qty")):
                continue  # 메모 행
            raw = {f: cell(f) for f in RAW_FIELDS}
            key = (block["date_text"], block["client_text"], raw["main_msg"], raw["spec_qty"])
            if key in seen:  # 다음 달 탭으로 이월된 같은 행
                dup += 1
                continue
            seen.add(key)
            res = block["res"]
            # 보류-동명(번호 없음)·동명확인(기본 쪽 번호)도 이름은 있음 → 이름으로 짝 찾기
            resolved = bool(res and res.name and res.hold != "보류-미등록" and not res.manual)
            rec = {
                "tab": title, "row": idx + 1, "block_row": block["rownum"],
                "date": block["date"].isoformat() if block["date"] else "",
                "date_text": block["date_text"], "client_text": block["client_text"],
                "client": res.name if res and resolved else "",
                "reg_no": (res.reg_no or "") if res and resolved else "",
                "client_how": (res.hold or res.how or "") if res else "거래처 칸 비어 있음",
                **{f"raw_{f}": raw[f] for f in RAW_FIELDS},
            }
            rec.update(parse_row(raw))
            rows.append(rec)
            per_tab[title] += 1
    return rows, per_tab, dup


# ---------------------------------------------------------------- DB 짝 찾기
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
    statement_id: str = ""


@dataclass
class LineIndex:
    by_reg: dict = field(default_factory=lambda: defaultdict(list))
    by_name: dict = field(default_factory=lambda: defaultdict(list))

    def client_lines(self, reg_no: str, name: str) -> list[DbLine]:
        from watcher import compact_name

        out = list(self.by_reg.get(reg_no, [])) if reg_no else []
        ids = {ln.id for ln in out}
        key = compact_name(name or "")[0]
        out += [ln for ln in self.by_name.get(key, []) if ln.id not in ids] if key else []
        return out


def load_lines(con: sqlite3.Connection, start: str, end: str) -> LineIndex:
    """lines(kind='job') + 같은 날·같은 거래처 명세표 → 사업자번호·statement_id."""
    from watcher import compact_name

    stm = defaultdict(list)
    for sid, d, client, reg, item, spec in con.execute(
            "SELECT id, date, client, reg_no, item, spec FROM statements WHERE date BETWEEN ? AND ?", (start, end)):
        stm[(d, client)].append((sid, reg or "", item, spec))
    name_reg = defaultdict(set)
    for name, reg in con.execute("SELECT name, reg_no FROM clients WHERE reg_no IS NOT NULL AND reg_no<>''"):
        name_reg[name].add(reg)
    idx = LineIndex()
    for lid, d, client, item, spec, qty, up, sup, dia, length, reg in con.execute(
            "SELECT id, date, client, item, spec, qty, unit_price, supply, dia, length, reg_no FROM lines"
            " WHERE kind='job' AND date BETWEEN ? AND ?", (start, end)):
        cands = stm.get((d, client), [])
        pick = [c for c in cands if c[2] == item and (c[3] or "") == (spec or "")] or cands
        sid = "|".join(str(c[0]) for c in pick) if len(pick) > 1 and len(cands) > 1 else (
            str(pick[0][0]) if pick else "")
        regs = {c[1] for c in pick if c[1]} or name_reg.get(client, set())
        reg = reg if reg and not str(reg).startswith("동명:") else (next(iter(regs)) if len(regs) == 1 else "")
        ln = DbLine(lid, date.fromisoformat(d), client or "", reg, item or "", spec or "", qty, up, sup, dia,
                    length, sid)
        if reg:
            idx.by_reg[reg].append(ln)
        key = compact_name(client or "")[0]
        if key:
            idx.by_name[key].append(ln)
    return idx


def _dia_ok(dias: list[float], ln: DbLine) -> bool:
    return ln.dia is not None and any(abs(d - ln.dia) <= DIA_TOL for d in dias)


def _len_ok(length, ln: DbLine) -> bool:
    return not length or not ln.length or abs(ln.length - length) <= LEN_TOL * length


def match_row(rec: dict, idx: LineIndex) -> dict:
    """정답지 열: match(exact/nearest/none), match_reason, actual_*, statement_id, line_*."""
    out = {"match": "none", "match_reason": "", "n_candidates": 0, "actual_unit_price": None, "actual_qty": None,
           "actual_supply": None, "statement_id": "", "line_id": None, "line_date": "", "date_diff": None,
           "line_client": "", "line_item": "", "line_spec": ""}
    if not rec["client"]:
        out["match_reason"] = "거래처 미해석"
        return out
    if not rec["date"]:
        out["match_reason"] = "날짜 파싱 실패"
        return out
    dias = [float(x) for x in rec["dia_all"].split()] if rec["dia_all"] else []
    if not dias:
        out["match_reason"] = "구경 파싱 실패"
        return out
    d0 = date.fromisoformat(rec["date"])
    lines = idx.client_lines(rec["reg_no"], rec["client"])
    if not lines:
        out["match_reason"] = "DB에 없음(거래처)"
        return out
    win = [ln for ln in lines if abs((ln.date - d0).days) <= DATE_WINDOW]
    dia_any = [ln for ln in lines if _dia_ok(dias, ln)]
    if not win:
        near = min(lines, key=lambda ln: abs((ln.date - d0).days))
        out["match_reason"] = f"날짜 밖 (가장 가까운 {(near.date - d0).days:+d}일)"
        return out
    by_dia = [ln for ln in win if _dia_ok(dias, ln)]
    if not by_dia:
        if dia_any:
            near = min(dia_any, key=lambda ln: abs((ln.date - d0).days))
            out["match_reason"] = f"날짜 밖 (구경 일치 {(near.date - d0).days:+d}일)"
        else:
            seen = sorted({f"{ln.dia:g}" for ln in win if ln.dia is not None})
            out["match_reason"] = "구경 불일치 (기간 내 DB Ø " + (",".join(seen[:6]) or "없음") + ")"
        return out
    cands = [ln for ln in by_dia if _len_ok(rec["length"], ln)]
    if not cands:
        out["match_reason"] = f"기장 불일치 (DB {','.join(f'{ln.length:g}' for ln in by_dia if ln.length)})"
        return out
    best = min(cands, key=lambda ln: (abs((ln.date - d0).days), min(abs(d - ln.dia) for d in dias), ln.id))
    out.update(match="exact" if len(cands) == 1 else "nearest", n_candidates=len(cands),
               actual_unit_price=best.unit_price, actual_qty=best.qty, actual_supply=best.supply,
               statement_id=best.statement_id, line_id=best.id, line_date=best.date.isoformat(),
               date_diff=(best.date - d0).days, line_client=best.client, line_item=best.item,
               line_spec=best.spec)
    return out


def reason_group(reason: str) -> str:
    return re.sub(r"\s*\(.*$", "", reason)


# ---------------------------------------------------------------- 실행
def _pct(n, total) -> str:
    return f"{n}/{total} ({n / total:.0%})" if total else "0/0"


def summarize(rows: list[dict], per_tab: Counter, dup: int) -> list[str]:
    n = len(rows)
    out = [f"품목 행 {n} (탭 이월 중복 제거 {dup})"]
    months = Counter(r["date"][:7] if r["date"] else "(날짜없음)" for r in rows)
    out.append("월별(행 날짜): " + "  ".join(f"{k}:{v}" for k, v in sorted(months.items())))
    out.append("탭별: " + "  ".join(f"{k}:{per_tab[k]}" for k in sorted(per_tab, key=lambda t: tab_month(t) or 0)))
    m = Counter(r["match"] for r in rows)
    out.append("match: " + "  ".join(f"{k} {_pct(m[k], n)}" for k in ("exact", "nearest", "none")))
    out.append("파싱 성공: " + "  ".join(
        f"{lbl} {_pct(sum(1 for r in rows if r[k] not in (None, '')), n)}"
        for lbl, k in (("구경", "dia"), ("공차", "tol_hi"), ("기장", "length"), ("수량", "qty"),
                       ("재질", "material"), ("특이사항", "specials"))))
    unres = [r for r in rows if not r["client"]]
    out.append(f"거래처 미해석 {len(unres)}행 / {len({r['client_text'] for r in unres})}개 표기")
    reasons = Counter(reason_group(r["match_reason"]) for r in rows if r["match"] == "none")
    out.append("none 사유: " + "  ".join(f"{k} {v}" for k, v in reasons.most_common()))
    return out


def sample_rows(rows: list[dict], n: int, rng: random.Random) -> list[dict]:
    """거래처가 겹치지 않게 무작위 n행 (거래처 수가 모자라면 나머지에서 채움)."""
    by_client = defaultdict(list)
    for r in rows:
        by_client[r["client"] or r["client_text"]].append(r)
    keys = list(by_client)
    rng.shuffle(keys)
    picked = [rng.choice(by_client[k]) for k in keys[:n]]
    if len(picked) < n:
        rest = [r for r in rows if r not in picked]
        picked += rng.sample(rest, min(n - len(picked), len(rest)))
    return picked


def format_raw(r: dict) -> str:
    head = f"[{r['tab']} {r['row']}행] {r['date_text']} {r['client_text']!r}"
    parts = [f"  {RAW_LETTERS[f]}={r['raw_' + f]!r}" for f in RAW_FIELDS if r["raw_" + f]]
    return head + "\n" + "\n".join(parts)


def write_outputs(rows: list[dict], year: int) -> tuple[Path, Path]:
    import pandas as pd

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    csv = OUT_DIR / f"imgagong_{year}.csv"
    xlsx = csv.with_suffix(".xlsx")
    df.to_csv(csv, index=False, encoding="utf-8-sig")
    df.to_excel(xlsx, index=False)
    return csv, xlsx


def fetch_tabs(cfg: dict, year: int) -> list[tuple[str, list[list[str]]]]:
    """임가공 시트의 그 해 월 탭 전부 (읽기 전용 get_all_values). 올해면 이번 달 뒤 탭(=작년 것)은 뺌."""
    import gspread

    scfg = cfg["sheets"]["imgagong"]
    gc = gspread.service_account(filename=cfg["common"]["credentials_file"])
    sh = gc.open_by_key(scfg["spreadsheet_id"])
    today = date.today()
    last = today.month if year == today.year else 12
    tabs = []
    for ws in sh.worksheets():
        m = tab_month(ws.title)
        if m and m <= last:
            tabs.append((ws.title.strip(), ws.get_all_values()))
    return tabs


def build(year: int, samples: int = 30, none_samples: int = 20, seed: int = 0) -> None:
    import watcher
    from config.settings import settings

    cfg = watcher.load_config()
    db = (ROOT / settings.DB_PATH) if not Path(settings.DB_PATH).is_absolute() else Path(settings.DB_PATH)
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)  # DB 읽기 전용
    master = watcher.load_client_master(con, cfg["common"].get("skip_clients") or [],
                                        rules=watcher.load_client_rules(watcher.ALIASES_YAML))
    tabs = fetch_tabs(cfg, year)
    rows, per_tab, dup = collect_rows(tabs, year, master, cfg["sheets"]["imgagong"]["data_start_row"])
    idx = load_lines(con, f"{year - 1}-11-01", f"{year + 1}-01-31")
    for r in rows:
        r.update(match_row(r, idx))
    csv, xlsx = write_outputs(rows, year)

    print(f"== 백테스트 정답지 {year} → {csv.relative_to(ROOT)} / {xlsx.name}")
    for line in summarize(rows, per_tab, dup):
        print("  " + line)

    rng = random.Random(seed)
    print(f"\n== 시트 원문 샘플 {samples}행 (거래처 다양하게, seed={seed}; 열 글자는 11월 탭 기준)")
    for r in sample_rows(rows, samples, rng):
        print(format_raw(r))

    nones = [r for r in rows if r["match"] == "none"]
    print(f"\n== match=none 사유 (전체 {len(nones)})")
    for k, v in Counter(reason_group(r["match_reason"]) for r in nones).most_common():
        print(f"  {k}: {v}")
    print(f"\n== match=none 샘플 {min(none_samples, len(nones))}건")
    for r in rng.sample(nones, min(none_samples, len(nones))):
        print(f"  [{r['tab']} {r['row']}행] {r['date']} {r['client_text']!r}→{r['client'] or '-'} "
              f"Ø{r['dia_all'] or '?'} L{r['length'] if r['length'] else '-'} : {r['match_reason']}"
              f"\n      E={r['raw_spec_qty']!r}")
