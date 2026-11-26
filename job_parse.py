"""임가공 행 공용 파서 — watcher가 넘기는 시트 원문(D/E/F/H/S…)과 백테스트 정답지가 같은 함수를 쓴다.

parse_row(raw) : RAW_FIELDS 원문 dict → 구경·공차·기장·수량·재질·특이사항 키워드.
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
