"""임가공 견적 B단계: 사다리(pricer_job)에서 미결인 행을 Claude로 견적한다. 운영(pricer)과 백테스트가 같이 쓴다.

- 모델 claude-sonnet-5-5, 시스템 프롬프트 = config/pricing_rules.md 전문 (prompt caching).
- 사용자 메시지 = 견적할 행(원문·파싱값) + 참고 사례:
    같은 거래처 유사 8건(모자라면 같은 거래처 최근 건으로 채움, 사다리 hint는 맨 위)
    · 타 거래처 유사 5건 · 거래처 인상 성향(같은 품목 연도쌍 최근 3쌍).
  참고 사례는 모두 cutoff_date 이전, exclude_ids(백테스트 정답 줄) 제외.
- 견적할 행은 D·E·F·H·S·T·U 원문 전부(빈 칸 생략) + 파싱값(구경·공차·기장·수량·재질·특이사항·치대).
- 출력은 부록 B.5 JSON 하나 (structured outputs로 형식 강제). max_tokens 300.
  사다리가 max_conf를 주면(24개월 초과 동일품) 결과 confidence를 그 아래로 깎는다.
- temperature: Sonnet 5.5는 기본값이 아닌 temperature를 400으로 거절 → 보내지 않는다.
  thinking: max_tokens 300 안에서 답만 내도록 between_tools(사고 없음).
- 호출마다 토큰·비용을 logs/ai_calls.jsonl에 한 줄씩 남긴다. 운영 호출은 하루 DAILY_LIMIT건까지,
  운영 첫날(로그에 운영 호출이 오늘 것뿐)은 FIRST_DAY_LIMIT건까지.
"""
from __future__ import annotations

import json
import random
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from loguru import logger

from cases import CaseQuery, DbLine, LineIndex, find_similar_cases
from job_parse import is_stock_removal
from pricer_job import JobInput, Quote

ROOT = Path(__file__).parent
RULES_PATH = ROOT / "config" / "pricing_rules.md"
CALL_LOG = ROOT / "logs" / "ai_calls.jsonl"

MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 300
DAILY_LIMIT = 300            # 운영(source=ops) 하루 API 호출 상한
FIRST_DAY_LIMIT = 60         # 운영 첫날 상한 (밀린 확정 행이 한꺼번에 AI로 가는 날)
SAME_CLIENT_CASES = 8
OTHER_CLIENT_CASES = 5
# USD / 1M tokens (claude-sonnet-5-5): 입력, 출력, 캐시 쓰기(5분), 캐시 읽기
PRICE = {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.10}

AI_METHODS = ["동일품", "유사품", "규칙계산", "사전단가", "특약"]
CONFIDENCES = ["high", "mid", "low"]
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "unit_price": {"type": "integer"},
        "method": {"type": "string", "enum": AI_METHODS},
        "basis": {"type": "string"},
        "confidence": {"type": "string", "enum": CONFIDENCES},
    },
    "required": ["unit_price", "method", "basis", "confidence"],
    "additionalProperties": False,
}

FIELD_LABELS = {"main_msg": "D 주요 메시지", "spec_qty": "E 공차x갯수·조도", "difficulty": "F 난이도",
                "pre_size": "H 연마량/치대", "pre_note": "S 호닝 전 매니저 노트",
                "post_size": "T 호닝 후 치수", "post_note": "U 호닝 후 매니저 노트"}
CONF_RANK = {"low": 0, "mid": 1, "high": 2}

_log_lock = threading.Lock()


class AiError(RuntimeError):
    """Claude 견적 실패 (API 오류·형식 오류·상한 초과) — 견적확인필요 사유."""


# ---------------------------------------------------------------- 참고 사례
def _fmt_line(ln: DbLine, cutoff: date) -> str:
    dims = [f"Ø{ln.dia:g}" if ln.dia is not None else "", f"L{ln.length:g}" if ln.length else ""]
    if ln.tol_hi is not None:
        dims.append(f"공차 {ln.tol_lo or 0:+g}~{ln.tol_hi:+g}")
    months = (cutoff - ln.date).days // 30
    return (f"{ln.date.isoformat()} ({months}개월 전) {ln.client} | {ln.item} {ln.spec}".rstrip()
            + f" | {' '.join(d for d in dims if d)}" + (f" | {ln.qty:g}개" if ln.qty is not None else "")
            + f" | 단가 {ln.unit_price:,}원")


def same_client_cases(inp: JobInput, index: LineIndex, cutoff: date, exclude_ids=(), n=SAME_CLIENT_CASES):
    """같은 거래처: 구경 유사 순, 모자라면 같은 거래처 최근 건(재질·형상 유추용)."""
    if not (inp.reg_no or inp.client):
        return []
    out: list[DbLine] = []
    if inp.dia is not None:
        q = CaseQuery(inp.dia, inp.reg_no, inp.client, inp.tol_lo, inp.tol_hi, inp.length, inp.qty)
        out = [c.line for c in find_similar_cases(index, q, cutoff, limit=n + len(exclude_ids),
                                                   same_client_only=True) if c.line.id not in exclude_ids][:n]
    if len(out) < n:
        ids = {ln.id for ln in out} | set(exclude_ids)
        recent = sorted((ln for ln in index.client_lines(inp.reg_no, inp.client)
                         if ln.date < cutoff and ln.id not in ids and ln.unit_price),
                        key=lambda ln: (ln.date, ln.id), reverse=True)
        out += recent[:n - len(out)]
    return out


def other_client_cases(inp: JobInput, index: LineIndex, cutoff: date, exclude_ids=(), n=OTHER_CLIENT_CASES):
    if inp.dia is None:
        return []
    q = CaseQuery(inp.dia, inp.reg_no, inp.client, inp.tol_lo, inp.tol_hi, inp.length, inp.qty)
    cands = find_similar_cases(index, q, cutoff, limit=200, dia_tol=max(1.0, inp.dia * 0.05))
    return [c.line for c in cands if not c.same_client and c.line.id not in exclude_ids
            and c.line.unit_price][:n]


def client_tendency(inp: JobInput, index: LineIndex, cutoff: date, exclude_ids=()) -> str:
    """같은 거래처 같은 품목의 연도쌍 단가 변화 (cutoff 이전) → 한 줄."""
    import inflation

    if not (inp.reg_no or inp.client):
        return "거래처 미상"
    lines = [ln for ln in index.client_lines(inp.reg_no, inp.client)
             if ln.date < cutoff and ln.id not in exclude_ids]
    pairs = sorted(inflation.year_pairs(inflation.group_items(lines)), key=lambda p: (p["year_b"], p["year_a"]))
    if not pairs:
        return "같은 품목 여러 해 거래 없음 (성향 모름)"
    last = pairs[-3:]
    ups = sum(1 for p in last if p["rate"] > 0.001)
    mean_all = sum(p["rate"] for p in pairs) / len(pairs)
    kind = "인상형" if len(last) >= 2 and ups >= 2 else "동결형"
    return (f"{kind} — 최근 {len(last)}쌍 중 인상 {ups}쌍, 전체 {len(pairs)}쌍 평균 연 {mean_all:+.1%} "
            f"(" + ", ".join(f"{p['pair']} {p['rate']:+.1%}" for p in last) + ")")


def build_user_message(inp: JobInput, index: LineIndex, cutoff: date, exclude_ids=(), hint: str = "") -> str:
    parts = ["# 견적 요청",
             f"- 접수일: {inp.on_date.isoformat() if inp.on_date else '-'}",
             f"- 거래처: {inp.client or '(미해석)'}"]
    parts += [f"- {FIELD_LABELS[k]}: {v}" for k, v in inp.raw.items() if v]
    if is_stock_removal(inp.raw.get("main_msg", "")):
        parts.append(f"  (D열 '{inp.raw['main_msg']}'은 부호·mm가 붙은 연마량(치대) 표기다. 단가가 아니다.)")
    parsed = [f"구경 {', '.join(f'Ø{d:g}' for d in inp.dia_all)}" if inp.dia_all else "구경 없음",
              f"공차 {inp.tol_lo or 0:+g}~{inp.tol_hi:+g}" if inp.tol_hi is not None else "공차 없음",
              f"기장 {inp.length:g}" if inp.length else "기장 없음",
              f"수량 {inp.qty}개" if inp.qty else "수량 없음",
              f"재질 {','.join(inp.material)}" if inp.material else "재질 없음",
              f"특이사항 {','.join(inp.specials)}" if inp.specials else "특이사항 없음",
              f"치대 {inp.chidae:g}mm" if inp.chidae is not None else "치대 없음"]
    parts.append("- 파싱: " + " / ".join(parsed))
    if len(inp.dia_all) > 1:
        parts.append(f"- 한 행에 품목이 {len(inp.dia_all)}개다. 첫 품목(Ø{inp.dia_all[0]:g})의 개당 단가를 답한다.")

    same = same_client_cases(inp, index, cutoff, exclude_ids)
    parts.append(f"\n# 참고: 같은 거래처 과거 거래 ({len(same)}건, 구경 유사 순 → 최근 순)")
    if hint:
        parts.append(f"※ {hint}")
        parts.append(f"※ 이 거래처 인상 성향: {client_tendency(inp, index, cutoff, exclude_ids)}")
    parts += [f"- {_fmt_line(ln, cutoff)}" for ln in same] or (["- 없음"] if not hint else [])
    other = other_client_cases(inp, index, cutoff, exclude_ids)
    parts.append(f"\n# 참고: 다른 거래처 유사 사례 ({len(other)}건)")
    parts += [f"- {_fmt_line(ln, cutoff)}" for ln in other] or ["- 없음"]
    parts.append(f"\n# 거래처 인상 성향\n- {client_tendency(inp, index, cutoff, exclude_ids)}")
    parts.append("\n부록 B.5 형식의 JSON 하나만 답하라. unit_price는 개당 원 단위 정수(100원 단위), basis는 한 줄.")
    return "\n".join(parts)


# ---------------------------------------------------------------- 호출
def call_cost(usage) -> float:
    g = lambda k: getattr(usage, k, 0) or 0  # noqa: E731
    return (g("input_tokens") * PRICE["input"] + g("output_tokens") * PRICE["output"]
            + g("cache_creation_input_tokens") * PRICE["cache_write"]
            + g("cache_read_input_tokens") * PRICE["cache_read"]) / 1_000_000


def log_call(rec: dict, path: Path = CALL_LOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _log_lock, open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def first_call_day(source: str = "ops", path: Path = CALL_LOG) -> date | None:
    """로그에서 그 source의 첫 호출 날짜 (없으면 None)."""
    if not path.exists():
        return None
    first = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                d = date.fromisoformat(str(r.get("ts", ""))[:10]) if r.get("source") == source else None
            except ValueError:
                continue
            if d and (first is None or d < first):
                first = d
    return first


def calls_today(source: str = "ops", path: Path = CALL_LOG, today: date | None = None) -> int:
    day = (today or date.today()).isoformat()
    if not path.exists():
        return 0
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            n += r.get("source") == source and str(r.get("ts", "")).startswith(day)
    return n


def parse_answer(text: str) -> dict:
    try:
        d = json.loads(text)
    except ValueError as e:
        raise AiError(f"AI 응답 JSON 아님: {text[:80]!r}") from e
    price = d.get("unit_price")
    if not isinstance(price, int) or price <= 0:
        raise AiError(f"AI 단가 이상: {price!r}")
    if d.get("confidence") not in CONFIDENCES:
        raise AiError(f"AI confidence 이상: {d.get('confidence')!r}")
    d["unit_price"] = int(round(price / 100) * 100) or 100
    d["method"] = d.get("method") if d.get("method") in AI_METHODS else "규칙계산"
    d["basis"] = " ".join(str(d.get("basis", "")).split())
    return d


@dataclass
class AiPricer:
    """Claude 견적기. client는 anthropic.Anthropic (테스트에서는 가짜)."""
    client: object = None
    rules: str = ""
    source: str = "ops"          # 비용 로그 구분 (ops / backtest)
    daily_limit: int | None = DAILY_LIMIT
    first_day_limit: int | None = FIRST_DAY_LIMIT
    log_path: Path = CALL_LOG

    def __post_init__(self):
        if not self.rules:
            self.rules = RULES_PATH.read_text(encoding="utf-8")
        if self.client is None:
            import anthropic

            from config.settings import settings

            self.client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY or None)
        self._count_lock = threading.Lock()
        self._today = (date.today(), calls_today(self.source, self.log_path))
        self._first_day = first_call_day(self.source, self.log_path)

    def limit_for(self, day: date) -> int | None:
        """그날 상한: 첫 호출 날(또는 아직 호출 없음)이면 first_day_limit, 아니면 daily_limit."""
        if self.daily_limit is None:
            return None
        if self.first_day_limit is not None and (self._first_day is None or self._first_day >= day):
            return min(self.first_day_limit, self.daily_limit)
        return self.daily_limit

    def _reserve(self) -> None:
        """하루 상한 검사 + 1건 예약 (스레드 안전)."""
        if self.daily_limit is None:
            return
        with self._count_lock:
            day, n = self._today
            if day != date.today():
                day, n = date.today(), calls_today(self.source, self.log_path)
                self._first_day = first_call_day(self.source, self.log_path)
            limit = self.limit_for(day)
            if n >= limit:
                self._today = (day, n)
                raise AiError(f"AI 일일 상한 {limit}건 도달")
            self._today = (day, n + 1)

    def request(self, user_msg: str):
        return self.client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            thinking={"type": "between_tools"},
            system=[{"type": "text", "text": self.rules, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
            output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        )

    def quote(self, inp: JobInput, index: LineIndex, cutoff: date, exclude_ids=(), hint: str = "",
              tag: str = "", max_conf: str | None = None) -> Quote:
        """미결 행 → Quote(method='AI-<방법>'). max_conf가 있으면 confidence를 그 아래로. 실패하면 AiError."""
        import anthropic

        user_msg = build_user_message(inp, index, cutoff, exclude_ids, hint)
        self._reserve()
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "source": self.source, "model": MODEL,
               "tag": tag, "client": inp.client}
        try:
            resp = self.request(user_msg)
        except anthropic.APIStatusError as e:
            rec.update(error=f"{e.status_code} {e.message}"[:200], cost_usd=0.0)
            log_call(rec, self.log_path)
            raise AiError(f"AI API 오류 {e.status_code}") from e
        except anthropic.APIConnectionError as e:
            rec.update(error="connection", cost_usd=0.0)
            log_call(rec, self.log_path)
            raise AiError("AI 연결 실패") from e
        u = resp.usage
        cost = call_cost(u)
        rec.update(request_id=getattr(resp, "_request_id", None), stop_reason=resp.stop_reason,
                   input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                   cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
                   cache_read=getattr(u, "cache_read_input_tokens", 0) or 0, cost_usd=round(cost, 6))
        try:
            if resp.stop_reason == "refusal":
                raise AiError("AI 거절(refusal)")
            if resp.stop_reason == "max_tokens":
                raise AiError(f"AI 응답 잘림(max_tokens {MAX_TOKENS})")
            text = next((b.text for b in resp.content if b.type == "text"), "")
            ans = parse_answer(text)
        except AiError as e:
            rec["error"] = str(e)
            log_call(rec, self.log_path)
            raise
        rec["answer"] = ans
        log_call(rec, self.log_path)
        logger.debug("AI 견적 {} {} → {:,} ({}/{}) ${:.4f}", tag, inp.client, ans["unit_price"], ans["method"],
                     ans["confidence"], cost)
        conf, basis = ans["confidence"], ans["basis"]
        if max_conf and CONF_RANK[conf] > CONF_RANK[max_conf]:
            conf, basis = max_conf, f"{basis} (24개월 초과 동일품 → 최대 {max_conf})"
        return Quote("AI-" + ans["method"], ans["unit_price"], conf, basis, cost_usd=cost)


def sample_pending(rows: list, n: int, seed: int) -> list:
    """미결 행에서 무작위 n개 (seed 고정 → 재현 가능)."""
    return random.Random(seed).sample(rows, min(n, len(rows)))
