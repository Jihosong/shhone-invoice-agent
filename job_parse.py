"""임가공 행 공용 파서 — watcher가 넘기는 시트 원문(D/E/F/H/S…)과 백테스트 정답지가 같은 함수를 쓴다.

parse_row(raw) : RAW_FIELDS 원문 dict → 구경·공차·기장·수량·재질·특이사항 키워드·치대.
"""
from __future__ import annotations

import re
import unicodedata

RAW_FIELDS = ("main_msg", "spec_qty", "difficulty", "pre_size", "pre_note", "post_size", "post_note")

# ---------------------------------------------------------------- 파서
_N = r"\d+(?:\.\d+)?"
_NUM_RE = re.compile(_N)
_DIA_RE = re.compile(r"(?:Ø|파이|내경)\s*(" + _N + r")")
_PAREN_RE = re.compile(r"\([^()]*\)")
_QTY_UNIT = r"(?:개|ea|pcs|본|set|조(?!도))"
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


_SIGNED_NUM = r"[-+]\s*(?:\d+(?:\.\d*)?|\.\d+)"
_PLAIN_NUM = r"(?:\d+(?:\.\d*)?|\.\d+)"
_STOCK_RE = re.compile(rf"(?:{_SIGNED_NUM})\s*(?:MM)?|{_PLAIN_NUM}\s*MM", re.I)
_CHIDAE_TXT_RE = re.compile(rf"(?:치대|연마량|호닝\s*전)\s*Ø?\s*({_SIGNED_NUM}|{_PLAIN_NUM})", re.I)
CHIDAE_MAX = 5.0   # 이보다 크면 치대가 아니라 치수(호닝 전 Ø270 등)


def is_stock_removal(text) -> bool:
    """'-0.5', '+0.3', '0.3mm'처럼 부호나 mm가 붙은 값 = 연마량(치대) 표기 (D열이면 단가가 아님)."""
    s = norm(text)
    return bool(s) and bool(_STOCK_RE.fullmatch(s))


def _chidae_value(tok: str) -> float | None:
    v = abs(float(re.sub(r"[\s+]", "", tok)))
    return v if v < CHIDAE_MAX else None


def parse_chidae(h: str = "", text: str = "") -> float | None:
    """치대(연마량, 절댓값 mm). H 칸이 숫자(부호·mm 허용)면 그 값, 아니면 H·text에서 '치대/연마량/호닝전 -0.2'."""
    s = norm(h)
    m = re.fullmatch(rf"({_SIGNED_NUM}|{_PLAIN_NUM})\s*(?:MM)?", s, re.I)
    if m:
        return _chidae_value(m.group(1))
    for t in (h, text):
        m = _CHIDAE_TXT_RE.search(norm(t))
        if m and (v := _chidae_value(m.group(1))) is not None:
            return v
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
        "chidae": row_chidae(raw),
    }


def row_chidae(raw: dict) -> float | None:
    """치대: H 칸 숫자 → D가 '-0.5'처럼 연마량 표기면 D → D·E·호닝 전 노트 글의 '치대/연마량/호닝전 -0.2'."""
    h, d = raw.get("pre_size", ""), raw.get("main_msg", "")
    v = parse_chidae(h)
    if v is None and is_stock_removal(d):
        v = parse_chidae(d)
    return v if v is not None else parse_chidae(
        h, " ".join(raw.get(k, "") for k in ("main_msg", "spec_qty", "pre_note")))


# ---------------------------------------------------------------- 품목 구간 (한 행에 품목 여럿)
_QTY_TAIL_RE = re.compile(r"\s*[/×xX*]?\s*\d+\s*" + _QTY_UNIT + r"(?![a-z]).*$", re.I | re.S)
# 품목 구분 '+' 뒤가 이런 모양이면 공차의 '+' ('+0.02', '+0~', '+-0.05')
_TOL_AFTER_PLUS_RE = re.compile(r"\s*[-+]?\s*(?:\d*\.\d|0(?![\d.]*\s*" + _QTY_UNIT + r")(?!\d))", re.I)
# 공통 머리(구경·공차·조도)가 끝나는 곳: '/', 기장('432L'·'L300'·'기장'), 수량
_HEAD_END_RE = re.compile(r"/|(?<![A-Za-z0-9])L\s*\d|\d+(?:\.\d+)?\s*L(?![a-z])|기장|길이|\d+\s*" + _QTY_UNIT,
                          re.I)


_NOTE_RE = re.compile(r"\([^()]*\)|\d+\s*[월일시]")   # 괄호 메모·날짜 ('(11월26일도착)')


def _plus_parts(s: str) -> list[str]:
    """수량 표기 뒤의 '+'에서 나눔 (공차 '+'는 안 나눔). 수량만 있는 꼬리('6개긴급 +8개')는 앞 구간에 붙임."""
    parts, start = [], 0
    for m in re.finditer(r"\+", s):
        if _QTY_RE.search(s[start:m.start()]) and not _TOL_AFTER_PLUS_RE.match(s, m.end()):
            parts.append(s[start:m.start()])
            start = m.end()
    parts.append(s[start:])
    out: list[str] = []
    for p in (x.strip(" ,/+") for x in parts):
        if not p:
            continue
        if out and not re.search(r"\d", _NOTE_RE.sub(" ", _QTY_RE.sub("", p))):  # 수량·메모만 → 같은 품목 추가 수량
            out[-1] = f"{out[-1]} + {p}"
        else:
            out.append(p)
    return out


def _dia_segments(s: str) -> list[str]:
    """구경(Ø)마다 한 구간. '~Ø'로 이어진 범위는 한 구간, 수량 없는 앞 구간은 다음에 붙임. 구간은 Ø부터."""
    spans = [st for _, st, _ in _dia_spans(s)]
    starts = [st for i, st in enumerate(spans) if i == 0 or not s[max(0, st - 2):st].strip().endswith("~")]
    if not starts:
        return []
    segs = [s[a:b].strip(" +,") for a, b in zip(starts, starts[1:] + [len(s)])]
    out: list[str] = []
    carry = ""
    for i, seg in enumerate(segs):
        seg = f"{carry} {seg}".strip() if carry else seg
        if i < len(segs) - 1 and not _QTY_TAIL_RE.search(seg):
            carry = seg
            continue
        carry = ""
        out.append(seg)
    return out


def _head_core(seg: str) -> str:
    """구간(Ø부터)의 공통 머리: 구경·공차·조도 ('Ø63.5 +0~+0.02 / 432L / 2개' → 'Ø63.5 +0~+0.02')."""
    spans = _dia_spans(seg)
    pos = spans[0][2] if spans else 0
    m = _HEAD_END_RE.search(seg, pos)
    return (seg[:m.start()] if m else seg).strip(" /+,")


def item_segments(text) -> list[tuple[str, str]]:
    """품목 구간들 [(머리말, 구간)]. 구간은 Ø부터 (명세표 규격용), 머리말은 그 앞 글(재질·조도 등).
    1) 수량 뒤 '+'로 나눔  2) 각 부분을 구경마다 나눔  3) 구경 없는 부분은 첫 품목의 공통 머리
    (재질·구경·공차·조도)를 물려받음: 'SUS316 Ø63.5 +0~+0.02 / 432L / 2개 + 534L / 1개'
    → [('SUS316', 'Ø63.5 +0~+0.02 / 432L / 2개'), ('SUS316', 'Ø63.5 +0~+0.02 / 534L / 1개')].
    구경 표기가 없는 글은 [] (모델명·품번 행 — 한 품목으로 본다)."""
    s = norm(text)
    if not _dia_spans(s):
        return []
    parts = _plus_parts(s)
    if not _dia_spans(parts[0]):  # 첫 부분에 구경이 없으면 나누지 않음 (구경 기준만)
        parts = [s]
    out: list[tuple[str, str]] = []
    head_prefix = head_core = ""
    for i, p in enumerate(parts):
        segs = _dia_segments(p)
        if segs:
            prefix = p[:_dia_spans(p)[0][1]].strip(" ,/+")
            if i == 0:
                head_prefix, head_core = prefix, _head_core(segs[0])
                out.append((prefix, segs[0]))
            else:
                out.append((_with_head(prefix, head_prefix), segs[0]))
            out += [(_with_head("", head_prefix), seg) for seg in segs[1:]]
        else:
            out.append((_with_head("", head_prefix), f"{head_core} / {p}"))
    return out


_RA_TXT_RE = re.compile(r"Ra\s*" + _N + r"(?:\s*~\s*" + _N + r")?", re.I)


def _with_head(own: str, head: str) -> str:
    """뒤 구간 머리말: 제 머리말 + 첫 품목 머리말 중 재질·조도(제 것에 없을 때만).
    막힘·hole·모델명 같은 나머지는 물려주지 않는다 ('막힘 Ø63.5 … + Ø46 …'의 Ø46은 막힘 아님)."""
    add = []
    if not parse_materials(own):
        add += [t for t in head.split() if parse_materials(t)]
    if not _RA_TXT_RE.search(own):
        add += [m.group(0) for m in _RA_TXT_RE.finditer(head)]
    return " ".join(add + ([own] if own else []))


def item_texts(text) -> list[str]:
    """품목 구간마다 견적용 글 (머리말 + 구간). 구간이 하나 이하면 []."""
    segs = item_segments(text)
    return [f"{pre} {seg}".strip() for pre, seg in segs] if len(segs) > 1 else []
