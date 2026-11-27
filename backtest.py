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
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from cases import DbLine, LineIndex, load_job_lines
from job_parse import (  # noqa: F401 — 공용 파서 (예전 import 경로 유지)
    MATERIALS, RAW_FIELDS, SPECIALS, norm, parse_dias, parse_length, parse_materials, parse_qty, parse_ra,
    parse_row, parse_specials, parse_tol,
)

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "data" / "backtest"

DATE_WINDOW = 15      # 일
DIA_TOL = 1.0         # mm — 구경 허용폭은 max(DIA_TOL, 구경 × DIA_TOL_PCT)
DIA_TOL_PCT = 0.01
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
}
RAW_LETTERS = {"main_msg": "D", "spec_qty": "E", "difficulty": "F", "pre_size": "H", "pre_note": "S"}  # 콘솔 표시용 (11월 탭 기준 글자)

# ---------------------------------------------------------------- 날짜
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


def tab_month(title: str, today: date | None = None) -> int | None:
    """'N월' → N, '당월' → 오늘 달 (운영 감시 탭과 같은 규칙), 그 밖 None."""
    if title.strip() == "당월":
        return (today or date.today()).month
    m = re.fullmatch(r"(\d{1,2})월", title.strip())
    return int(m.group(1)) if m else None


def tab_label(title: str, tab_year: int, year: int) -> str:
    """주 연도 탭은 '3월', 다른 해 탭은 '12월(2025)'."""
    return title if tab_year == year else f"{title}({tab_year})"


def tab_years(titles: list[str], year: int, today: date | None = None) -> dict[str, int]:
    """월 탭 제목 → 연도. 올해면 이번 달 이하 = 올해, 그 뒤 달(12월 등) = 작년 탭. 지난해 이전이면 모두 그 해."""
    today = today or date.today()
    out = {}
    for t in titles:
        m = tab_month(t, today)
        if m:
            out[t] = year - 1 if year == today.year and m > today.month else year
    return out


def collect_rows(tabs: list[tuple], year: int, master, data_start_row: int = 3):
    """[(탭 제목, 탭 연도, get_all_values)] → (품목 행 list, 탭별 행 수, 중복 제거 수). 탭은 (연도, 달) 순서로.
    (옛 형식 [(탭 제목, values)]이면 모두 year)"""
    rows, per_tab, dup = [], Counter(), 0
    seen = set()
    tabs = [t if len(t) == 3 else (t[0], year, t[1]) for t in tabs]
    for title, tyear, values in sorted(tabs, key=lambda t: (t[1], tab_month(t[0]) or 0)):
        tm = tab_month(title)
        label = tab_label(title, tyear, year)
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
                block = {"date_text": cell("date"), "date": parse_sheet_date(cell("date"), tyear, tm),
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
                "tab": label, "row": idx + 1, "block_row": block["rownum"],
                "date": block["date"].isoformat() if block["date"] else "",
                "date_text": block["date_text"], "client_text": block["client_text"],
                "client": res.name if res and resolved else "",
                "reg_no": (res.reg_no or "") if res and resolved else "",
                "client_how": (res.hold or res.how or "") if res else "거래처 칸 비어 있음",
                **{f"raw_{f}": raw[f] for f in RAW_FIELDS},
            }
            rec.update(parse_row(raw))
            rows.append(rec)
            per_tab[label] += 1
    return rows, per_tab, dup


# ---------------------------------------------------------------- DB 짝 찾기
def dia_width(d: float) -> float:
    return max(DIA_TOL, d * DIA_TOL_PCT)


def _dia_ok(dias: list[float], ln: DbLine) -> bool:
    return ln.dia is not None and any(abs(d - ln.dia) <= dia_width(d) for d in dias)


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
    out.append("탭별: " + "  ".join(f"{k}:{v}" for k, v in per_tab.items()))  # collect_rows가 (연도, 달) 순
    out.append(f"짝 기준: 날짜 ±{DATE_WINDOW}일, 구경 ±max({DIA_TOL:g}mm, {DIA_TOL_PCT:.0%}), 기장 ±{LEN_TOL:.0%}")
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


def fetch_tabs(cfg: dict, year: int) -> list[tuple[str, int, list[list[str]]]]:
    """임가공 시트 월 탭 전부 ('N월' + '당월'=오늘 달, 읽기 전용 get_all_values) → [(제목, 탭 연도, values)].
    year 탭 전부 + 그 앞 해 탭(올해 기준 이번 달보다 뒤 달 = 작년 12월 등)도 포함."""
    import gspread

    scfg = cfg["sheets"]["imgagong"]
    gc = gspread.service_account(filename=cfg["common"]["credentials_file"])
    sh = gc.open_by_key(scfg["spreadsheet_id"])
    wss = [ws for ws in sh.worksheets() if tab_month(ws.title)]
    years = tab_years([ws.title.strip() for ws in wss], year)
    return [(ws.title.strip(), years[ws.title.strip()], ws.get_all_values()) for ws in wss]


def unresolved_lines(con, rows: list[dict]) -> list[str]:
    """미해석 거래처 표기 → aliases suggest 형식 줄 (행 많은 순, 끝 주석에 행 수)."""
    import clients_cli

    cnt = Counter(r["client_text"] for r in rows if not r["client"] and r["client_text"])
    return [f"{clients_cli.suggestion_line(con, t)}  ({n}행)" for t, n in cnt.most_common()]


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
    idx = LineIndex.build(load_job_lines(con, f"{year - 2}-10-01", f"{year + 1}-01-31", official_only=True))
    for r in rows:
        r.update(match_row(r, idx))
    csv, xlsx = write_outputs(rows, year)
    sug = unresolved_lines(con, rows)
    unres_path = OUT_DIR / "unresolved_clients.txt"
    header = ["# 임가공 백테스트 미해석 거래처 → config/client_aliases.yaml aliases: 에 옮길 후보",
              "# 형식은 agent.py aliases suggest 와 같음. 끝 (N행)은 시트 행 수"]
    unres_path.write_text("\n".join(header + sug) + "\n", encoding="utf-8")

    print(f"== 백테스트 정답지 {year} → {csv.relative_to(ROOT)} / {xlsx.name}")
    for line in summarize(rows, per_tab, dup):
        print("  " + line)
    print(f"  미해석 거래처 후보 {len(sug)}개 표기 → {unres_path.relative_to(ROOT)}")

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


# ---------------------------------------------------------------- 채점 (backtest run)
def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None  # NaN → None


def load_answer_rows(year: int) -> list[dict]:
    """정답지 CSV → 행 dict (빈칸은 '', 숫자 열은 그대로 문자열 — 쓰는 쪽에서 _f)."""
    import csv

    path = OUT_DIR / f"imgagong_{year}.csv"
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def score_rows(rows: list[dict], index: LineIndex) -> list[dict]:
    """match≠none 행마다 사다리 a·b → 예측·실제·오차율. cutoff = 행 날짜, 정답 줄(line_id)은 후보에서 제외."""
    from pricer_job import JobInput, quote_ladder

    out = []
    by_id = {ln.id: ln for ln in index.lines}
    for r in rows:
        if r.get("match") in ("", "none") or not r.get("date"):
            continue
        inp = JobInput.from_backtest(r)
        lid = _f(r.get("line_id"))
        q = quote_ladder(inp, index, inp.on_date, exclude_ids={int(lid)} if lid is not None else ())
        out.append(score_entry(r, inp, q, by_id.get(int(lid)) if lid is not None else None))
    return out


def pick_item_price(q, line: DbLine | None) -> int | None:
    """품목별 사전단가면 정답 줄 구경에 가장 가까운 품목의 단가, 아니면 q.unit_price."""
    if q.item_prices and line is not None and line.dia is not None:
        return min(q.item_prices, key=lambda it: abs(it[0] - line.dia))[1]
    return q.unit_price


def score_entry(r: dict, inp, q, line: DbLine | None = None) -> dict:
    """정답지 행 + 견적 → 결과 행 (예측·실제·오차율)."""
    pred = pick_item_price(q, line)
    actual = _f(r.get("actual_unit_price"))
    lid = _f(r.get("line_id"))
    err = (pred - actual) / actual if pred is not None and actual and actual > 0 else None
    return {
        "tab": r["tab"], "row": r["row"], "date": r["date"], "client": r["client"], "reg_no": r["reg_no"],
        "raw_main_msg": r["raw_main_msg"], "raw_spec_qty": r["raw_spec_qty"],
        "dia": inp.dia, "tol_lo": inp.tol_lo, "tol_hi": inp.tol_hi, "length": inp.length, "qty": inp.qty,
        "keywords": ",".join(sorted(inp.item_keywords)),
        "method": q.method, "pred": pred, "confidence": q.confidence or "", "basis": q.basis,
        "hint": q.hint, "ref_line_id": q.ref_line_id, "cost_usd": q.cost_usd,
        "actual_unit_price": actual, "actual_qty": _f(r.get("actual_qty")),
        "line_item": r.get("line_item", ""), "line_spec": r.get("line_spec", ""), "line_id": lid,
        "err_rate": err, "abs_err": abs(err) if err is not None else None,
    }


def accuracy_line(res: list[dict]) -> str:
    import statistics

    errs = [r["abs_err"] for r in res if r["abs_err"] is not None]
    if not errs:
        return f"{len(res)}건 (채점 가능 0)"
    w10 = sum(1 for e in errs if e <= 0.10 + 1e-9)
    w20 = sum(1 for e in errs if e <= 0.20 + 1e-9)
    exact = sum(1 for e in errs if e < 1e-9)
    skipped = len(res) - len(errs)
    return (f"{len(errs)}건  중앙값 절대오차율 {statistics.median(errs):.1%}  정확일치 {exact / len(errs):.0%}"
            f"  ±10% {w10 / len(errs):.0%}  ±20% {w20 / len(errs):.0%}"
            + (f"  (실제 단가 0·없음 {skipped}건 제외)" if skipped else ""))


def report_scores(res: list[dict], top: int = 20) -> list[str]:
    n = len(res)
    by = Counter(r["method"] for r in res)
    order = [m for m in ("사전단가", "동일품") if m in by or not by] + sorted(
        (m for m in by if m not in ("사전단가", "동일품", "미결", "AI실패")), key=lambda m: -by[m])
    tail = [m for m in ("AI실패", "미결") if m in by]
    out = [f"대상 {n}행",
           "적용 비율: " + "  /  ".join(f"{m} {_pct(by[m], n)}" for m in order + tail)]
    applied = [r for r in res if r["pred"] is not None]
    out.append("적용된 것 전체: " + accuracy_line(applied))
    for m in order:
        sub = [r for r in applied if r["method"] == m]
        if sub:
            out.append(f"  {m}: " + accuracy_line(sub))
    out.append("confidence별:")
    for c in ("high", "mid", "low"):
        sub = [r for r in applied if r["confidence"] == c]
        if sub:
            out.append(f"  {c}: " + accuracy_line(sub))
            for m in order:
                sm = [r for r in sub if r["method"] == m]
                if sm and len(sm) != len(sub):
                    out.append(f"    {m}: " + accuracy_line(sm))
    wrong = sorted((r for r in applied if r["abs_err"] is not None and r["abs_err"] > 1e-9),
                   key=lambda r: -r["abs_err"])[:top]
    out.append(f"틀린 것 상위 {len(wrong)}건 (오차율 큰 순):")
    for r in wrong:
        inp = (r["raw_main_msg"] + " | " if r["raw_main_msg"] else "") + r["raw_spec_qty"]
        out.append(f"  {r['err_rate']:+.0%}  {r['client']}  [{r['tab']} {r['row']}행]  {inp[:70]!r}"
                   f"  → 예측 {r['pred']:,} ({r['method']}/{r['confidence']})  실제 {r['actual_unit_price']:,.0f}"
                   f"  | {r['basis'][:60]}")
    return out


def preset_diagnosis(res: list[dict]) -> list[str]:
    """사전단가(D열 숫자) 가정 점검: 실제/D 배율 분포."""
    from job_parse import norm as _norm

    rows = [r for r in res if r["method"] == "사전단가" and r["actual_unit_price"]
            and "/" not in r["raw_main_msg"]]  # 품목별(20/30)은 배율 점검에서 뺀다
    if not rows:
        return ["사전단가 해당 행 없음"]
    ratios = Counter()
    for r in rows:
        d = float(_norm(r["raw_main_msg"]).replace(",", ""))
        k = r["actual_unit_price"] / d
        lbl = ("×10,000" if abs(k - 10000) < 1e-6 else "×1(원 그대로)" if abs(k - 1) < 1e-6
               else "×100,000" if abs(k - 100000) < 1e-6 else "×1,000" if abs(k - 1000) < 1e-6 else "기타")
        ratios[(lbl, "D<1000" if d < 1000 else "D≥1000")] += 1
    out = [f"사전단가 {len(rows)}건 — 실제 단가 / D값 배율:"]
    for (lbl, band), c in sorted(ratios.items(), key=lambda kv: -kv[1]):
        out.append(f"  {band} {lbl}: {c}")
    return out


def run_scores(year: int, ai: bool = False, limit: int = 200, seed: int = 0, top: int = 20) -> None:
    import pandas as pd

    from cases import load_job_lines
    from config.settings import settings

    db = (ROOT / settings.DB_PATH) if not Path(settings.DB_PATH).is_absolute() else Path(settings.DB_PATH)
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)  # DB 읽기 전용
    index = LineIndex.build(load_job_lines(con, official_only=True))  # 시트 확정 줄 제외
    if ai:
        return run_ai_scores(load_answer_rows(year), index, limit=limit, seed=seed, top=top)
    res = score_rows(load_answer_rows(year), index)
    path = OUT_DIR / "results_noai.csv"
    pd.DataFrame(res).to_csv(path, index=False, encoding="utf-8-sig")
    print(f"== 백테스트 채점 (사다리 a·b, API 없음) {year} → {path.relative_to(ROOT)}")
    for line in report_scores(res, top):
        print(("  " if not line.startswith("  ") else "") + line)
    print("\n== 사전단가(D열) 가정 점검")
    for line in preset_diagnosis(res):
        print(("  " if not line.startswith("  ") else "") + line)


# ---------------------------------------------------------------- B단계 채점 (backtest run --ai)
def ai_pending(rows: list[dict], index: LineIndex):
    """사다리 결과 → (적용된 결과 행들, 미결 [(정답지 행, JobInput, 미결 Quote, 정답 줄 id, 정답 DbLine)])."""
    from pricer_job import M_PENDING, JobInput, quote_ladder

    by_id = {ln.id: ln for ln in index.lines}
    applied, pending = [], []
    for r in rows:
        if r.get("match") in ("", "none") or not r.get("date"):
            continue
        inp = JobInput.from_backtest(r)
        lid = _f(r.get("line_id"))
        lid = int(lid) if lid is not None else None
        q = quote_ladder(inp, index, inp.on_date, exclude_ids={lid} if lid is not None else ())
        if q.method == M_PENDING:
            pending.append((r, inp, q, lid, by_id.get(lid)))
        else:
            applied.append(score_entry(r, inp, q, by_id.get(lid)))
    return applied, pending


def run_ai_scores(rows: list[dict], index: LineIndex, *, limit: int = 200, seed: int = 0, top: int = 20,
                  ai=None, workers: int = 4, out_path: Path | None = None) -> list[dict]:
    """미결 행에서 seed로 limit개를 뽑아 Claude 견적 → results_ai.csv + 리포트. ai: AiPricer (테스트 주입)."""
    from concurrent.futures import ThreadPoolExecutor

    import pandas as pd

    from pricer_ai import AiError, AiPricer, sample_pending
    from pricer_job import Quote

    applied, pending = ai_pending(rows, index)
    picked = sample_pending(pending, limit, seed)
    ai = ai or AiPricer(source="backtest", daily_limit=None)

    def one(item):
        r, inp, q0, lid, line = item
        try:
            q = ai.quote(inp, index, inp.on_date, exclude_ids={lid} if lid is not None else (), hint=q0.hint,
                         tag=f"{r['tab']} {r['row']}행", max_conf=q0.max_conf)
        except AiError as e:
            q = Quote("AI실패", basis=str(e))
        q.hint = q0.hint
        return score_entry(r, inp, q, line)

    res = []
    if picked:
        res.append(one(picked[0]))  # 첫 호출 단독 → 시스템 프롬프트 캐시를 만든 뒤 병렬
        with ThreadPoolExecutor(max_workers=workers) as ex:
            res += list(ex.map(one, picked[1:]))
    path = out_path or OUT_DIR / "results_ai.csv"
    pd.DataFrame(res).to_csv(path, index=False, encoding="utf-8-sig")

    total = len(applied) + len(pending)
    cost = sum(r["cost_usd"] or 0 for r in res)
    print(f"== 백테스트 채점 B단계 (Claude {len(res)}건, seed={seed}) → {path.name}")
    print(f"  사다리: 대상 {total}행 중 사전단가·동일품 {len(applied)} / 미결 {len(pending)} → 표본 {len(picked)}")
    print(f"  비용: 총 ${cost:.3f}  (건당 ${cost / max(len(res), 1):.4f})")
    print("\n== AI 표본")
    for line in report_scores(res, top):
        print(("  " if not line.startswith("  ") else "") + line)
    fails = Counter(r["basis"] for r in res if r["method"] == "AI실패")
    if fails:
        print("  AI실패 사유: " + "  ".join(f"{k} {v}" for k, v in fails.most_common()))
    return res
