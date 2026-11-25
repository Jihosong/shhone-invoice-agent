"""거래처 별칭표 도구 (agent.py aliases check / aliases suggest / clients find).

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
from watcher import REG_NO_RE, compact_name

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
SECTIONS = ("aliases", "homonyms", "manual_clients")
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
    w.run_once()
    out: list[str] = []
    for snap in w.snapshots.values():
        for r in snap["rows"]:
            if r["client_resolved"] is None and r["block_label"] not in out and r["block_label"] != "?":
                out.append(r["block_label"])
    return out
