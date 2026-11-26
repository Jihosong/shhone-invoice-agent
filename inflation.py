"""임가공 단가 물가상승률 측정 (python agent.py backtest inflation). DB 읽기 전용.

DB lines(kind='job') 전체에서 같은 거래처(사업자번호, 없으면 정규화 거래처명) + 같은 품목
(구경 ±0.5mm, 공차 동일, 기장은 양쪽 다 있으면 ±10% — 한쪽만 있으면 다른 품목)이 서로 다른 해에 나온 경우,
품목·연도별 단가 중앙값을 내고 **품목이 나온 연속한 두 해**마다 연환산 변화율 (p_b/p_a)^(1/(y_b-y_a)) - 1 을 쌍으로 만든다.
(같은 품목이 23·24·26에 나오면 23→24, 24→26 두 쌍. 수량 차이에 따른 단가 차이는 보정하지 않음.)
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from cases import DbLine

ROOT = Path(__file__).parent
OUT = ROOT / "data" / "backtest" / "inflation.csv"
MANUAL_ANNUAL_RATE = 0.09  # 매뉴얼의 '연 9%' 인상 가정
ITEM_DIA_TOL = 0.5
ITEM_LEN_TOL = 0.10


@dataclass
class Item:
    client_key: str
    dia: float
    length: float | None
    tol: tuple
    lines: list = field(default_factory=list)

    def accepts(self, ln: DbLine) -> bool:
        if abs(ln.dia - self.dia) > ITEM_DIA_TOL or (ln.length is None) != (self.length is None):
            return False
        return self.length is None or abs(ln.length - self.length) <= ITEM_LEN_TOL * self.length


def _tol_key(ln: DbLine) -> tuple:
    r = lambda v: None if v is None else round(float(v), 4)  # noqa: E731
    return r(ln.tol_lo), r(ln.tol_hi)


def group_items(lines: list[DbLine]) -> list[Item]:
    """거래처·공차가 같고 구경 ±0.5·기장 ±10%인 줄을 한 품목으로 (먼저 나온 줄이 기준)."""
    open_items: dict[tuple, list[Item]] = defaultdict(list)
    for ln in sorted(lines, key=lambda x: (x.date, x.id)):
        if ln.dia is None or not ln.unit_price or ln.unit_price <= 0:
            continue
        key = (ln.client_key, _tol_key(ln))
        item = next((it for it in open_items[key] if it.accepts(ln)), None)
        if item is None:
            item = Item(ln.client_key, ln.dia, ln.length, key[1])
            open_items[key].append(item)
        item.lines.append(ln)
    return [it for items in open_items.values() for it in items]


def dia_band(d: float) -> str:
    return "소(<20)" if d < 20 else "대(>300)" if d > 300 else "일반(20~300)"


def year_pairs(items: list[Item]) -> list[dict]:
    """품목마다 연도별 단가 중앙값 → 연속 해 쌍의 연환산 변화율."""
    out = []
    for it in items:
        by_year = defaultdict(list)
        for ln in it.lines:
            by_year[ln.date.year].append(ln)
        years = sorted(by_year)
        if len(years) < 2:
            continue
        client = Counter(ln.client for ln in it.lines).most_common(1)[0][0]
        item = Counter(ln.item for ln in it.lines if ln.item).most_common(1)
        for ya, yb in zip(years, years[1:]):
            pa = statistics.median(ln.unit_price for ln in by_year[ya])
            pb = statistics.median(ln.unit_price for ln in by_year[yb])
            out.append({
                "client": client, "client_key": it.client_key, "dia": it.dia, "length": it.length,
                "tol_lo": it.tol[0], "tol_hi": it.tol[1], "band": dia_band(it.dia),
                "item": item[0][0] if item else "",
                "date_a": max(ln.date for ln in by_year[ya]), "date_b": max(ln.date for ln in by_year[yb]),
                "year_a": ya, "year_b": yb, "pair": f"{ya % 100}→{yb % 100}",
                "price_a": pa, "price_b": pb, "n_a": len(by_year[ya]), "n_b": len(by_year[yb]),
                "rate": (pb / pa) ** (1 / (yb - ya)) - 1,
                "line_ids_a": " ".join(str(ln.id) for ln in by_year[ya]),
                "line_ids_b": " ".join(str(ln.id) for ln in by_year[yb]),
            })
    return out


# ---------------------------------------------------------------- 거래처 인상 성향 (동일품 12~24개월·AI 참고·clients trend)
TREND_PAIRS = 3          # 최근 몇 쌍으로 판정
RAISE_THRESHOLD = 0.05   # 최근 쌍 평균 인상률이 이보다 크면 인상형
RAISE_DEFAULT = 0.05     # 12~24개월 동일품 기본 인상률
RAISE_CAP = 0.15         # 인상형 평균 인상률 상한


@dataclass
class Trend:
    pairs: list            # 재주문 쌍 전부 (오래된 → 최근)
    recent: list           # 판정에 쓴 최근 TREND_PAIRS쌍
    mean_recent: float | None
    kind: str              # 인상형 / 동결형 / 모름

    def raise_rate(self) -> tuple[float, str]:
        """12~24개월 동일품 인상률과 그 사유: 인상형이면 최근 쌍 평균(상한 15%), 아니면 5%."""
        if self.kind == "인상형":
            r = min(round(self.mean_recent, 3), RAISE_CAP)
            cap = f", 상한 {RAISE_CAP:.0%}" if self.mean_recent > RAISE_CAP else ""
            return r, f"인상형: 최근 {len(self.recent)}쌍 평균 {self.mean_recent:+.1%}{cap}"
        return RAISE_DEFAULT, ""

    def line(self) -> str:
        if not self.pairs:
            return "같은 품목 여러 해 거래 없음 (성향 모름)"
        ups = sum(1 for p in self.recent if p["rate"] > 0.001)
        mean_all = statistics.mean(p["rate"] for p in self.pairs)
        return (f"{self.kind} — 최근 {len(self.recent)}쌍 평균 {self.mean_recent:+.1%} (인상 {ups}쌍), "
                f"전체 {len(self.pairs)}쌍 평균 연 {mean_all:+.1%} ("
                + ", ".join(f"{p['pair']} {p['rate']:+.1%}" for p in self.recent) + ")")


def client_trend(lines: list[DbLine]) -> Trend:
    """한 거래처 줄들 → 재주문 쌍(같은 품목, 연속한 두 해)과 성향. 최근 3쌍 평균 인상률 > 5%면 인상형."""
    pairs = sorted(year_pairs(group_items(lines)), key=lambda p: (p["date_b"], p["date_a"]))
    recent = pairs[-TREND_PAIRS:]
    if not recent:
        return Trend([], [], None, "모름")
    mean = statistics.mean(p["rate"] for p in recent)
    return Trend(pairs, recent, mean, "인상형" if mean > RAISE_THRESHOLD else "동결형")


def stats(rates: list[float]) -> str:
    if not rates:
        return "0쌍"
    s = sorted(rates)
    k = len(s) // 20
    trimmed = s[k:len(s) - k] if len(s) > 2 * k else s
    up = sum(1 for r in s if r > 0.001)
    same = sum(1 for r in s if abs(r) <= 0.001)
    return (f"{len(s)}쌍  중앙값 {statistics.median(s):+.1%}  평균 {statistics.mean(s):+.1%}"
            f"  (양끝 5% 뺀 평균 {statistics.mean(trimmed):+.1%})  인상 {up / len(s):.0%} / 동결 {same / len(s):.0%}"
            f" / 인하 {(len(s) - up - same) / len(s):.0%}")


def report(pairs: list[dict]) -> list[str]:
    out = ["전체: " + stats([p["rate"] for p in pairs])]
    out.append("연도쌍별:")
    for k in sorted({p["pair"] for p in pairs}, key=lambda x: tuple(map(int, x.split("→")))):
        out.append(f"  {k}: " + stats([p["rate"] for p in pairs if p["pair"] == k]))
    out.append("구경대별:")
    for band in ("소(<20)", "일반(20~300)", "대(>300)"):
        out.append(f"  {band}: " + stats([p["rate"] for p in pairs if p["band"] == band]))
    out.append("거래처별 상위 20 (쌍 많은 순):")
    by_client = defaultdict(list)
    for p in pairs:
        by_client[p["client_key"]].append(p)
    top = sorted(by_client.values(), key=lambda ps: -len(ps))[:20]
    for ps in top:
        rates = [p["rate"] for p in ps]
        out.append(f"  {ps[0]['client']}: {len(ps)}쌍  중앙값 {statistics.median(rates):+.1%}"
                   f"  평균 {statistics.mean(rates):+.1%}")
    out.append("결론: " + conclusion([p["rate"] for p in pairs]))
    return out


def conclusion(rates: list[float]) -> str:
    """매뉴얼 '연 9%' 가정과 비교한 한 줄."""
    if not rates:
        return "비교할 쌍 없음"
    med, mean = statistics.median(rates), statistics.mean(rates)
    same = sum(1 for r in rates if abs(r) <= 0.001) / len(rates)
    ups = [r for r in rates if r > 0.001]
    up_med = statistics.median(ups) if ups else 0.0

    def cmp(v):
        return "낮음" if v < MANUAL_ANNUAL_RATE - 0.02 else "높음" if v > MANUAL_ANNUAL_RATE + 0.02 else "비슷"

    return (f"같은 거래처·품목 연환산 단가 변화 중앙값 {med:+.1%}(동결 {same:.0%})·평균 {mean:+.1%} → "
            f"매뉴얼 '연 {MANUAL_ANNUAL_RATE:.0%}' 일괄 가정보다 {cmp(mean)}; 올린 건만 보면 중앙값 {up_med:+.1%}"
            f"({cmp(up_med)})")


def run(con) -> None:
    import pandas as pd

    from cases import load_job_lines

    lines = load_job_lines(con)
    items = group_items(lines)
    pairs = year_pairs(items)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(pairs).to_csv(OUT, index=False, encoding="utf-8-sig")
    multi = sum(1 for it in items if len({ln.date.year for ln in it.lines}) > 1)
    span = f"{lines[0].date}~{lines[-1].date}" if lines else "-"
    print(f"== 임가공 단가 변화 (DB lines job {len(lines)}줄 {span}, 품목 {len(items)}개 중 여러 해 {multi}개)"
          f" → {OUT.relative_to(ROOT)}")
    for line in report(pairs):
        print(("  " if not line.startswith("  ") else "") + line)
