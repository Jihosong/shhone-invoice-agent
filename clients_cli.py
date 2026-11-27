"""거래처 별칭표 도구 (agent.py aliases check / aliases suggest / clients find / clients trend).

- check_aliases: config/client_aliases.yaml 각 줄 검사 → [(줄번호, 오류|경고|안내, 설명)]
- find_clients: 마스터에서 이름 부분일치(정규화 키) → 정식명/사업자번호/최근거래일/올해 건수
- suggest: 시트 표기 → 이름 유사도 + 2025~2026 거래 건수 순 후보 (별칭 채울 때 참고용)
"""
from __future__ import annotations

import difflib
import re
import sqlite3
import unicodedata
from pathlib import Path

import yaml

from feedback import norm_text
from watcher import REG_NO_RE, NameNormalizer, compact_name

REG_LIKE_RE = re.compile(r"^[\d\-\s]+$")
SUGGEST_SINCE = "2025-01-01"


# ---------------------------------------------------------------- 공통
def _clients(con: sqlite3.Connection) -> list[tuple[str, str, str | None]]:
    return con.execute("SELECT name, COALESCE(reg_no, ''), last_date FROM clients").fetchall()


def _counts(con: sqlite3.Connection, since: str, until: str | None = None) -> dict[tuple[str, str], int]:
    """(거래처명, 사업자번호) → 명세표 수 (since ≤ 날짜 < until)."""
    try:
        sql = "SELECT client, reg_no, COUNT(*) FROM statements WHERE date >= ?"
        args = [since]
        if until:
            sql += " AND date < ?"
            args.append(until)
        return {(c, r): n for c, r, n in con.execute(sql + " GROUP BY client, reg_no", args)}
    except sqlite3.OperationalError:
        return {}


# ---------------------------------------------------------------- clients find
def find_clients(con: sqlite3.Connection, query: str, year: str) -> list[dict]:
    qk, _ = compact_name(query)
    counts = _counts(con, f"{year}-01-01", f"{int(year) + 1}-01-01")
    out = []
    for name, reg, last in _clients(con):
        if qk and qk in compact_name(name)[0]:
            out.append({"name": name, "reg_no": reg, "last_date": last or "", "n_year": counts.get((name, reg), 0)})
    out.sort(key=lambda d: (d["n_year"], d["last_date"]), reverse=True)
    return out


def _pad(text: str, width: int) -> str:
    """한글(전각) 2칸으로 세어 왼쪽 정렬."""
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(0, width - w)


def _width(text: str) -> int:
    return len(_pad(text, 0))


def print_find(con: sqlite3.Connection, query: str, year: str) -> None:
    rows = find_clients(con, query, year)
    if not rows:
        print(f"'{query}' 일치 없음")
        return
    w = max(_width(r["name"]) for r in rows + [{"name": "정식명"}])
    print(f"{_pad('정식명', w)}  {_pad('사업자번호', 12)}  {_pad('최근거래', 10)}  {year}건수")
    for r in rows:
        print(f"{_pad(r['name'], w)}  {r['reg_no'] or '-':<12}  {r['last_date'] or '-':<10}  {r['n_year']:>8}")


# ---------------------------------------------------------------- aliases check
SECTIONS = ("normalize", "aliases", "homonyms", "manual_clients")
EMPTY = ("", "~", "null")


def check_aliases(path: Path, con: sqlite3.Connection) -> list[tuple[int, str, str]]:
    """틀린 줄 찾기 → [(줄번호, 오류|경고|안내, 설명)].
    aliases: 한 등록명에 약칭 여럿은 OK, 한 약칭(정규화 후)이 두 곳을 가리키면 오류.
    homonyms: 법인/개인 번호 형식·존재, 기본은 법인/개인 중 적힌 쪽.
    manual_clients: 사유. 세 구역을 통틀어 같은 표기(정규화 후)가 두 번 나오면 오류."""
    text = Path(path).read_text(encoding="utf-8")
    root = yaml.compose(text)
    if root is None or not isinstance(root, yaml.MappingNode):
        return [(1, "오류", "최상위에 aliases:/homonyms:/manual_clients: 구역이 없음")]
    problems: list[tuple[int, str, str]] = []
    nodes = {}
    for k, v in root.value:
        if k.value in SECTIONS:
            nodes[k.value] = v
        else:
            problems.append((k.start_mark.line + 1, "경고", f"모르는 구역 '{k.value}' (무시됨)"))
    if not nodes:
        return [(1, "오류", "최상위에 aliases:/homonyms:/manual_clients: 구역이 없음")]

    # normalize: 표기 변환표 — "원표기": "바꿀 표기". 아래 표기 비교도 해석과 같게 변환표를 거친 키로
    pairs = []
    nnode = nodes.get("normalize")
    if isinstance(nnode, yaml.MappingNode):
        for knode, vnode in nnode.value:
            src = str(knode.value or "").strip()
            dst = str(vnode.value or "").strip() if isinstance(vnode, yaml.ScalarNode) else ""
            if not src or not dst:
                problems.append((knode.start_mark.line + 1, "오류", f"normalize '{src}': 원표기·바꿀 표기 둘 다 필요"))
            else:
                pairs.append((src, dst))
    elif nnode is not None and not (isinstance(nnode, yaml.ScalarNode) and nnode.value in EMPTY):
        problems.append((nnode.start_mark.line + 1, "오류", "'normalize:' 아래는 '원표기: 바꿀 표기' 목록이어야 함"))
    normalizer = NameNormalizer(pairs)

    def compact_name(text):  # noqa: F811 — 해석(ClientMaster)과 같은 키
        return normalizer.key(text)

    rows = _clients(con)
    names: dict[str, set[str]] = {}
    for name, reg, _ in rows:
        names.setdefault(name, set()).add(reg)
    by_norm = {norm_text(n): n for n in names}
    by_key: dict[str, list[str]] = {}
    for n in names:
        by_key.setdefault(compact_name(n)[0], []).append(n)
    regs = {reg: name for name, reg, _ in rows if reg}
    hom_keys = set()
    if isinstance(nodes.get("homonyms"), yaml.MappingNode):
        hom_keys = {compact_name(str(k.value))[0] for k, _ in nodes["homonyms"].value}

    seen: dict[str, tuple[int, str, str]] = {}  # 표기 키 → (줄, 원래 표기, 가리키는 곳)

    def mapping(section):
        node = nodes.get(section)
        if node is None or (isinstance(node, yaml.ScalarNode) and node.value in EMPTY):
            return []
        if not isinstance(node, yaml.MappingNode):
            problems.append((node.start_mark.line + 1, "오류", f"'{section}:' 아래는 '표기: 값' 목록이어야 함"))
            return []
        return node.value

    def reg_ok(line, who, reg) -> bool:
        if not REG_NO_RE.match(reg):
            problems.append((line, "오류", f"{who}: 사업자번호 형식 아님 {reg!r} (000-00-00000)"))
            return False
        if reg not in regs:
            problems.append((line, "오류", f"{who}: 사업자번호 {reg} 가 clients에 없음"))
            return False
        return True

    def register(line, label, key, dest):
        """세 구역 공통: 같은 표기가 다른 곳을 가리키면 오류."""
        if key in seen:
            pline, plabel, pdest = seen[key]
            if pdest == dest:
                problems.append((line, "경고", f"'{label}' 중복 ({pline}행과 같은 대상)"))
            else:
                problems.append((line, "오류", f"'{label}' 가 두 곳을 가리킴 ({pline}행 '{plabel}' → "
                                              f"{pdest.split(':', 1)[1]}, 여기 → {dest.split(':', 1)[1]})"))
            return
        seen[key] = (line, label, dest)

    # ---- aliases
    for knode, vnode in mapping("aliases"):
        line = knode.start_mark.line + 1
        alias = str(knode.value or "").strip()
        target = str(vnode.value or "").strip() if isinstance(vnode, yaml.ScalarNode) else ""
        akey = compact_name(alias)[0]
        if not akey:
            problems.append((line, "오류", "약칭이 비어 있음"))
            continue
        if not target:
            problems.append((line, "오류", f"'{alias}': 대상(등록명 또는 사업자번호)이 비어 있음"))
            continue
        if REG_LIKE_RE.match(target):
            if not reg_ok(line, f"'{alias}'", target):
                continue
            dest, dest_name = "reg:" + target, regs[target]
        else:
            name = target if target in names else by_norm.get(norm_text(target))
            if not name:
                near = by_key.get(compact_name(target)[0]) or [c["name"] for c in suggest(con, target, top=2)]
                hint = f" (비슷한 등록: {' / '.join(near)})" if near else ""
                problems.append((line, "오류", f"'{alias}': 등록명 {target!r} 가 clients에 없음{hint}"))
                continue
            dest, dest_name = "name:" + name, name
            nreg = {r for r in names[name] if r}
            if len(nreg) > 1 and compact_name(name)[0] not in hom_keys:
                problems.append((line, "안내", f"'{alias}' → '{name}' 은 동명 ({', '.join(sorted(nreg))}) — "
                                              "최근거래 규칙 적용. 고정하려면 사업자번호나 homonyms로"))
        clash = [n for n in by_key.get(akey, []) if n != dest_name]
        if clash:
            problems.append((line, "안내", f"약칭 '{alias}' 가 등록명 '{clash[0]}' 과 같은 표기 — "
                                          "시트에 그 이름을 써도 이 별칭 대상으로 감"))
        register(line, alias, akey, dest)

    # ---- homonyms
    for knode, vnode in mapping("homonyms"):
        line = knode.start_mark.line + 1
        name = str(knode.value or "").strip()
        hkey = compact_name(name)[0]
        if not hkey:
            problems.append((line, "오류", "동명 이름이 비어 있음"))
            continue
        if not isinstance(vnode, yaml.MappingNode):
            problems.append((line, "오류", f"동명 '{name}': 아래에 법인/개인/기본을 적어야 함"))
            continue
        fields = {}
        for fk, fv in vnode.value:
            fline = fk.start_mark.line + 1
            if fk.value not in ("법인", "개인", "기본"):
                problems.append((fline, "오류", f"동명 '{name}': 모르는 항목 '{fk.value}' (법인/개인/기본)"))
                continue
            fields[fk.value] = (fline, str(fv.value or "").strip() if isinstance(fv, yaml.ScalarNode) else "")
        sides = {}
        for side in ("법인", "개인"):
            if side in fields and fields[side][1]:
                fline, reg = fields[side]
                if reg_ok(fline, f"동명 '{name}' {side}", reg):
                    sides[side] = reg
                    if compact_name(regs[reg])[0] != hkey:
                        problems.append((fline, "안내", f"동명 '{name}' {side} {reg} 의 등록명은 '{regs[reg]}'"))
        if not any(fields.get(s, (0, ""))[1] for s in ("법인", "개인")):
            problems.append((line, "오류", f"동명 '{name}': 법인/개인 번호가 하나도 없음"))
        if len(set(sides.values())) < len(sides):
            problems.append((line, "오류", f"동명 '{name}': 법인과 개인이 같은 번호"))
        dline, default = fields.get("기본", (line, ""))
        if default not in ("법인", "개인"):
            problems.append((dline, "오류", f"동명 '{name}': 기본은 법인 또는 개인 ({default!r})"))
        elif not fields.get(default, (0, ""))[1]:
            problems.append((dline, "오류", f"동명 '{name}': 기본 {default} 인데 {default} 번호가 없음"))
        register(line, name, hkey, "homonym:" + name)

    # ---- manual_clients
    for knode, vnode in mapping("manual_clients"):
        line = knode.start_mark.line + 1
        label = str(knode.value or "").strip()
        reason = str(vnode.value or "").strip() if isinstance(vnode, yaml.ScalarNode) else ""
        mkey = compact_name(label)[0]
        if not mkey:
            problems.append((line, "오류", "수동처리 표기가 비어 있음"))
            continue
        if not reason:
            problems.append((line, "경고", f"수동처리 '{label}': 사유가 비어 있음 (산출근거에 '수동처리'만 적힘)"))
        if by_key.get(mkey):
            problems.append((line, "안내", f"수동처리 '{label}' 가 등록명 '{by_key[mkey][0]}' 과 같은 표기 — 수동처리가 우선"))
        register(line, label, mkey, "manual:" + label)
    return sorted(problems)


# ---------------------------------------------------------------- 후보 제안
def _parts(key: str) -> list[str]:
    """'에스티엠(stm)' → ['에스티엠(stm)', '에스티엠', 'stm'] — 괄호 안 영문 표기도 비교 대상."""
    out = [key]
    out += [p for p in re.split(r"[()\[\]]", key) if p and p != key]
    return out


def similarity(query: str, name: str) -> float:
    """0~1. 같은 표기 1.0 > 한쪽이 다른 쪽에 포함 0.8~1.0 > 글자 유사도(difflib).
    '케이엔티/대명티에스'는 '/'로 나눠 각각, '엔젠텍 소관&호닝'은 첫 단어(엔젠텍)도 따로 비교."""
    pieces = [p.strip() for p in query.split("/") if p.strip()]
    qparts = list(dict.fromkeys(k for p in pieces for k in (compact_name(p)[0], compact_name(p.split()[0])[0]) if k))
    best = 0.0
    for q in qparts:
        for p in _parts(compact_name(name)[0]):
            if not q or not p:
                continue
            if q == p:
                s = 1.0
            elif len(min(q, p, key=len)) >= 2 and (q in p or p in q):
                s = 0.8 + 0.2 * min(len(q), len(p)) / max(len(q), len(p))
            else:
                s = difflib.SequenceMatcher(None, q, p).ratio()
            best = max(best, s)
        # 단어 순서만 다른 경우: '나까조코리아' ↔ '코리아 나까조(주)'
        words = [k for k in (compact_name(x)[0] for x in re.split(r"[\s()]+", name)) if len(k) >= 2]
        if len(words) >= 2 and all(x in q for x in words):
            best = max(best, 0.95)
    return best


def suggest(con: sqlite3.Connection, query: str, top: int = 3, threshold: float = 0.5) -> list[dict]:
    counts = _counts(con, SUGGEST_SINCE)
    cands = []
    for name, reg, last in _clients(con):
        s = similarity(query, name)
        if s >= threshold:
            cands.append({"name": name, "reg_no": reg, "last_date": last or "", "score": s,
                          "n": counts.get((name, reg), 0)})
    cands.sort(key=lambda d: (round(d["score"], 1), d["n"]), reverse=True)
    return cands[:top]


def suggestion_line(con: sqlite3.Connection, query: str) -> str:
    cands = suggest(con, query)
    if not cands:
        return f'  # "{query}": ""    # 후보없음'
    body = " | ".join(f"{c['name']} ({c['reg_no'] or '-'}, 25~26 {c['n']}건)" for c in cands)
    return f'  # "{query}": ""    # 제안: {body}'


def unresolved_from_sheets() -> list[str]:
    """두 시트 활성 탭에서 거래처를 못 찾은 묶음의 거래처 칸 원문 (dry-run 한 바퀴, 시트 쓰기 없음)."""
    import watcher
    w = watcher.Watcher(watcher.load_config(), dry_run=True)
    w._first_done.update(w.cfg["sheets"])  # 읽기만 — 재시작 보호 바퀴·대량 재견적 판정 없이
    w.run_once()
    out: list[str] = []
    for snap in w.snapshots.values():
        for r in snap["rows"]:
            if r["client_resolved"] is None and r["block_label"] not in out and r["block_label"] != "?":
                out.append(r["block_label"])
    return out


# ---------------------------------------------------------------- clients trend
def pick_client(con: sqlite3.Connection, query: str) -> tuple[dict | None, list[dict]]:
    """검색어 → (거래처 하나, 후보들). 사업자번호 → 시트와 같은 해석(별칭·동명규칙·'이름(대표명)')
    → 정규화 이름이 똑같은 것 하나 / 후보 하나 순으로 확정, 아니면 None."""
    from datetime import date

    import watcher

    rows = find_clients(con, re.sub(r"\(.*", "", query).strip() or query, str(date.today().year))
    q = query.strip()
    if REG_NO_RE.match(q):
        hit = next(({"name": n, "reg_no": r, "last_date": d or ""} for n, r, d in _clients(con) if r == q), None)
        return hit, [hit] if hit else []
    res = watcher.load_client_master(con, rules=watcher.load_client_rules(watcher.ALIASES_YAML)).resolve_full(q)
    if res.name and res.reg_no and res.hold is None:
        return {"name": res.name, "reg_no": res.reg_no, "last_date": "", "how": res.basis or res.how}, rows
    qk = compact_name(query)[0]
    exact = [r for r in rows if compact_name(r["name"])[0] == qk]
    if len(exact) == 1:
        return exact[0], rows
    return (rows[0] if len(rows) == 1 else None), rows


def trend_report(lines: list, name: str, reg_no: str, how: str = "") -> list[str]:
    """한 거래처 임가공 줄 → 재주문 쌍 표 + 동결형/인상형 판정 근거 + 12~24개월 동일품 인상률."""
    import inflation
    from pricer_job import pct

    t = inflation.client_trend(lines)
    how = f", {how}" if how else ""
    span = f"{min(ln.date for ln in lines)}~{max(ln.date for ln in lines)}" if lines else "-"
    out = [f"== {name} ({reg_no or '사업자번호 없음'}{how}) 임가공 {len(lines)}줄 {span}",
           f"재주문 쌍 {len(t.pairs)}개 — 같은 품목(구경 ±{inflation.ITEM_DIA_TOL:g}·기장 ±{inflation.ITEM_LEN_TOL:.0%}"
           "·공차 동일)이 연속한 두 해에 나온 것, 해마다 단가 중앙값, 변화율은 연환산"]
    if not t.pairs:
        return out + ["판정: 모름 — 재주문 쌍 없음 → 12~24개월 동일품은 기본 +5%"]
    recent = {id(p) for p in t.recent}
    for p in t.pairs:
        dims = f"Ø{p['dia']:g}" + (f" L{p['length']:g}" if p["length"] else "")
        if p["tol_hi"] is not None:
            dims += f" {p['tol_lo'] or 0:+g}~{p['tol_hi']:+g}"
        mark = "*" if id(p) in recent else " "
        out.append(f" {mark} {_pad((p['item'] or '-')[:18], 18)}  {_pad(dims, 22)}  "
                   f"{p['date_a']} {p['price_a']:>9,.0f}({p['n_a']}건) → {p['date_b']} {p['price_b']:>9,.0f}({p['n_b']}건)"
                   f"  {p['rate']:+.1%}")
    rates = ", ".join(f"{p['rate']:+.1%}" for p in t.recent)
    op = ">" if t.kind == "인상형" else "≤"
    out.append(f"판정: {t.kind} — 최근 {len(t.recent)}쌍(*) 평균 ({rates}) = {t.mean_recent:+.1%} "
               f"{op} {inflation.RAISE_THRESHOLD:.0%}")
    rate, why = t.raise_rate()
    out.append(f"12~24개월 동일품 인상률: {pct(rate)}" + (f" ({why})" if why else " (기본)"))
    return out


def print_trend(con: sqlite3.Connection, query: str) -> bool:
    from cases import LineIndex, load_job_lines

    hit, rows = pick_client(con, query)
    if hit is None:
        print(f"'{query}' " + ("일치 없음" if not rows else "후보가 여럿 — 정식명으로 다시:"))
        for r in rows[:15]:
            print(f"  {r['name']}  {r['reg_no'] or '-'}  최근 {r['last_date'] or '-'}")
        return False
    lines = LineIndex.build(load_job_lines(con)).client_lines(hit["reg_no"], hit["name"])
    for line in trend_report(lines, hit["name"], hit["reg_no"], hit.get("how", "")):
        print(line)
    return True
