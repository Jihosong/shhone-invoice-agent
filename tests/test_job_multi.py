"""임가공 다품목: 블록 규칙(2번째 줄 이후 독립 견적) + E의 '+' 구간별 견적·AI 한 번 호출·사람 단가 개수 검사·발행 줄."""
import json
from datetime import date

import pytest

import pricer
from cases import LineIndex
from issuer.excel_upload import Line, job_lines
from job_parse import item_segments, item_texts
from pricer_ai import AiError, MULTI_SCHEMA, parse_multi_answer
from pricer_job import M_PENDING, JobInput, Quote, item_count, split_items
from tests.test_issuer_job import ctx, get
from tests.test_pricer_ai import COLS, REG, FakeWriter, fake_ai, job, ln, mk_pricer, sheet_row

CUT = date(2026, 6, 1)
EX = "SUS316 Ø63.5 +0~+0.02 / 432L / 2개 + 534L / 1개"
EX3 = "SUS316 Ø63.5 +0~+0.02 / 432L / 2개 + 534L / 1개 + 600L / 3개"


# ---------------------------------------------------------------- 구간 나누기 (job_parse)
@pytest.mark.parametrize("text,want", [
    (EX, [("SUS316", "Ø63.5 +0~+0.02 / 432L / 2개"), ("SUS316", "Ø63.5 +0~+0.02 / 534L / 1개")]),
    ("SUSØ35+0.0~+0.03 / 330 / 1개 + SUSØ40+0.0~+0.03 / 175 / 3개",
     [("SUS", "Ø35+0.0~+0.03 / 330 / 1개"), ("SUS", "Ø40+0.0~+0.03 / 175 / 3개")]),
    ("Ra0.4 Ø50 +0~0.02 / 300 / 2개 + 400 / 1개",
     [("Ra0.4", "Ø50 +0~0.02 / 300 / 2개"), ("Ra0.4", "Ø50 +0~0.02 / 400 / 1개")]),
    ("Ø63.5 +0~+0.02 432L 2개 + 534L 1개", [("", "Ø63.5 +0~+0.02 432L 2개"), ("", "Ø63.5 +0~+0.02 / 534L 1개")]),
    ("Ø50+0.0~+0.05 / 300 / 2개", [("", "Ø50+0.0~+0.05 / 300 / 2개")]),          # 공차 '+'는 안 나눔
    ("Ø50 / 6개긴급 +8개", [("", "Ø50 / 6개긴급 + 8개")]),                        # 수량만 → 같은 품목
    ("Ø50 / 2개 + 열처리", [("", "Ø50 / 2개 + 열처리")]),
    ("Ø50 / 2개 +0.02 추가", [("", "Ø50 / 2개 +0.02 추가")]),
    ("모델A 2개 + 모델B 1개", []),                                                 # 구경 없음 → 한 품목
    ("4HOLE Ø80-0.01~-0.03 / 2개+2개 (11월26일도착)", [("4HOLE", "Ø80-0.01~-0.03 / 2개 + 2개 (11월26일도착)")]),
    # 뒤 구간은 재질·조도만 물려받음 (막힘·hole·모델명은 아님)
    ("막힘 Ø63.5 +0~+0.01 / 1015 / 2개 + Ø46 -0.1~+0.1 / 110 / 2개",
     [("막힘", "Ø63.5 +0~+0.01 / 1015 / 2개"), ("", "Ø46 -0.1~+0.1 / 110 / 2개")]),
    ("SUS Ø35 / 1개 + 사각 Ø40 / 2개", [("SUS", "Ø35 / 1개"), ("SUS 사각", "Ø40 / 2개")]),
    ("각도 60도 Ra0.2~0.3 Ø90.07 +0~+0.05 / 4개 + Ø30.18 +0~+0.02 / 4개",
     [("각도 60도 Ra0.2~0.3", "Ø90.07 +0~+0.05 / 4개"), ("Ra0.2~0.3", "Ø30.18 +0~+0.02 / 4개")]),
])
def test_item_segments(text, want):
    assert item_segments(text) == want


def test_split_items_inherits_head_and_splits_d():
    inp = JobInput.from_raw({"spec_qty": EX3, "main_msg": "6/5.5/5", "pre_note": "급함"}, "가사", REG, CUT)
    segs = split_items(inp)
    assert [(s.dia, s.tol_lo, s.tol_hi, s.length, s.qty, s.material) for s in segs] == [
        (63.5, 0.0, 0.02, 432.0, 2, ["SUS"]), (63.5, 0.0, 0.02, 534.0, 1, ["SUS"]),
        (63.5, 0.0, 0.02, 600.0, 3, ["SUS"])]
    assert [s.raw["main_msg"] for s in segs] == ["6", "5.5", "5"]
    assert all(s.raw["pre_note"] == "급함" and s.client == "가사" for s in segs)
    assert item_count(inp.raw) == 3 and item_count({"spec_qty": "Ø50 / 2개"}) == 1
    assert split_items(job("Ø50 / 2개")) == [job("Ø50 / 2개")]
    assert item_texts("모델A 2개 + 모델B 1개") == []


# ---------------------------------------------------------------- 운영 견적 (pricer.quote_job_row)
def test_multi_rules_only_capped_mid():
    hist = [ln(1, "2026-05-01", 60000, dia=63.5, length=432, lo=0, hi=0.02),
            ln(2, "2026-04-01", 55000, dia=63.5, length=534, lo=0, hi=0.02)]
    p = mk_pricer(False)
    price, basis, st, method, conf = p.quote_job_row(job(EX), LineIndex.build(hist), CUT)
    assert (price, st, method, conf) == ("60000/55000", pricer.ST_CHECK, "다품목", "mid")  # 둘 다 high여도 mid
    assert basis.startswith("[가견적-mid] 다품목 2구간")
    assert "구간1 동일품 | 2026-05-01 60,000원" in basis and "; 구간2 동일품 | 2026-04-01 55,000원" in basis


def test_multi_preset_per_segment_low_wins():
    hist = [ln(1, "2025-01-01", 10000, dia=10)]
    price, basis, st, _, conf = mk_pricer(False).quote_job_row(job(EX, d="6/50"), LineIndex.build(hist), CUT)
    assert (price, st, conf) == ("60000/500000", pricer.ST_CHECK, "low")   # 50만 원은 과거 중앙값 대비 low
    assert basis.startswith("[가견적-low] 다품목 2구간")


def test_multi_ai_one_call_for_pending_segments(tmp_path):
    hist = [ln(1, "2026-05-01", 60000, dia=63.5, length=432, lo=0, hi=0.02)]
    ai = fake_ai(tmp_path, [{"items": [
        {"segment": 2, "unit_price": 55049, "method": "유사품", "basis": "534L 비례", "confidence": "high"},
        {"segment": 3, "unit_price": 50000, "method": "유사품", "basis": "600L", "confidence": "mid"}]}])
    p = mk_pricer(ai)
    price, basis, st, method, conf = p.quote_job_row(job(EX3), LineIndex.build(hist), CUT)
    assert len(ai.client.messages.calls) == 1
    kw = ai.client.messages.calls[0]
    assert kw["output_config"]["format"]["schema"] == MULTI_SCHEMA and kw["max_tokens"] == 100 + 200 * 2
    msg = kw["messages"][0]["content"]
    for part in ("구간 1: SUS316 Ø63.5 +0~+0.02 / 432L / 2개", "이미 정함: 60,000원 (동일품",
                 "구간 2: SUS316 Ø63.5 +0~+0.02 / 534L / 1개", "구간 3: SUS316 Ø63.5 +0~+0.02 / 600L / 3개",
                 "견적할 구간: 2, 3 (2개)"):
        assert part in msg
    assert (price, st, method, conf) == ("60000/55000/50000", pricer.ST_CHECK, "다품목", "mid")
    assert "구간2 AI-유사품 | 534L 비례 | high; 구간3 AI-유사품 | 600L | mid" in basis


def test_multi_ai_failure_or_missing():
    class Boom:
        def quote_multi(self, *a, **k):
            raise AiError("AI 다품목 구간 불일치 (요청 [1, 2], 응답 [1])")
    price, basis, st, method, _ = mk_pricer(Boom()).quote_job_row(job(EX), LineIndex.build([]), CUT)
    assert (price, st, method) == ("", pricer.ST_CHECK, M_PENDING) and "구간 불일치" in basis
    price, basis, st, _, _ = mk_pricer(False).quote_job_row(job(EX), LineIndex.build([]), CUT)
    assert (price, st) == ("", pricer.ST_CHECK) and basis.startswith("미결 | 다품목 2구간") and "AI 없음" in basis
    _, basis, _, _, _ = mk_pricer(Boom(), dry_run=True).quote_job_row(job(EX), LineIndex.build([]), CUT)
    assert "dry-run" in basis


def test_parse_multi_answer_checks_segments():
    ok = json.dumps({"items": [{"segment": 2, "unit_price": 1, "method": "유사품", "basis": "", "confidence": "low"}]})
    assert parse_multi_answer(ok, [1])[0]["unit_price"] == 100
    with pytest.raises(AiError):
        parse_multi_answer(ok, [0, 1])
    with pytest.raises(AiError):
        parse_multi_answer('{"unit_price": 1}', [0])


def test_multi_stale_segment_cap_passed(tmp_path):
    hist = [ln(1, "2023-01-01", 45000, dia=63.5, length=432, lo=0, hi=0.02)]   # 24개월 초과 → AI, 최대 mid
    ai = fake_ai(tmp_path, [{"items": [
        {"segment": 1, "unit_price": 50000, "method": "동일품", "basis": "인상", "confidence": "high"},
        {"segment": 2, "unit_price": 52000, "method": "유사품", "basis": "x", "confidence": "high"}]}])
    segs = split_items(job(EX))
    from pricer_job import quote_ladder
    qs = [quote_ladder(s, LineIndex.build(hist), CUT) for s in segs]
    got = ai.quote_multi(job(EX), segs, qs, LineIndex.build(hist), CUT)
    assert [q.confidence for q in got] == ["mid", "high"] and "24개월 초과" in got[0].basis


# ---------------------------------------------------------------- 운영 process_job: 블록·사람 단가 개수
class RecAi:
    """입력을 기록하는 가짜 AI (단품·다품목)."""
    def __init__(self):
        self.inputs = []

    def quote(self, inp, index, cutoff, hint="", tag="", max_conf=None):
        self.inputs.append(inp)
        return Quote("AI-유사품", 10000 * len(self.inputs), "mid", f"근거{len(self.inputs)}")

    def quote_multi(self, inp, segs, qs, index, cutoff, exclude_ids=(), tag=""):
        self.inputs.append(inp)
        return [Quote("AI-유사품", 60000 - 5000 * k, "mid", "x") for k, q in enumerate(qs) if q.method == M_PENDING]


@pytest.fixture
def env(monkeypatch):
    import sqlite3

    monkeypatch.setattr(pricer, "SheetWriter", FakeWriter)
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE rows (row_id TEXT, status TEXT)")
    con.execute("CREATE TABLE lines (id INTEGER, kind TEXT)")
    ai = RecAi()
    p = mk_pricer(ai, con=con)
    monkeypatch.setattr(p, "job_index", lambda: LineIndex.build([]))
    scfg = {"columns": COLS, "data_start_row": 3, "hash_columns": ["client", "main_msg", "spec_qty"]}
    return p, ai, scfg, con


def snap(*rows):
    return {"label": "임가공", "ws": None, "rows": [
        {"rownum": n, "row_id": f"R{n}", "row": row, "fp": fp, "status": st, "author": "", "legacy": False,
         "client_resolved": "가사", "client_reg_no": REG} for n, row, st, fp in rows]}


def test_human_price_count_must_match_items(env):
    p, ai, scfg, con = env
    p.process_job("imgagong", scfg, snap((5, sheet_row(e=EX3), "확정", "fp1")))
    w = FakeWriter.last.writes
    assert (w[(5, "unit_price")], w[(5, "status")]) == ("60000/55000/50000", pricer.ST_CHECK)
    basis = w[(5, "basis")]
    # 사람이 단가 2개만 적음 → 견적확인필요 유지 + '단가 개수 불일치'
    p.process_job("imgagong", scfg, snap((5, sheet_row(e=EX3, status="견적확인필요", price="60000/55000",
                                                       basis=basis), "견적확인필요", "fp1")))
    w = FakeWriter.last.writes
    assert w[(5, "status")] == pricer.ST_CHECK
    bad = w[(5, "basis")]
    assert bad.startswith("[가견적-mid] 단가 개수 불일치 (단가 2개 ≠ 품목 3개) | 다품목 3구간")
    # 다음 바퀴 그대로 → 쓰기 없음
    p.process_job("imgagong", scfg, snap((5, sheet_row(e=EX3, status="견적확인필요", price="60000/55000",
                                                       basis=bad), "견적확인필요", "fp1")))
    assert FakeWriter.last.writes == {}
    # 3개로 고침 → 견적완료(사람수정), 불일치 표시는 뗌
    p.process_job("imgagong", scfg, snap((5, sheet_row(e=EX3, status="견적확인필요", price="60,000/55,000/52,000",
                                                       basis=bad), "견적확인필요", "fp1")))
    w = FakeWriter.last.writes
    assert w[(5, "status")] == pricer.ST_QUOTED_EDIT
    assert w[(5, "basis")].startswith("사람 수정 60,000/55,000/52,000 (에이전트 60000/55000/50000) | 다품목 3구간")
    assert con.execute("SELECT price FROM job_quotes").fetchone() == ("60000/55000/52000",)


def test_human_clears_status_with_wrong_count_stays_check(env):
    p, ai, scfg, con = env
    p.process_job("imgagong", scfg, snap((5, sheet_row(e=EX3), "확정", "fp1")))
    basis = FakeWriter.last.writes[(5, "basis")]
    # 상태 칸을 비움(확정으로 돌아옴) + 단가 하나로 고침 → 품목 3개와 안 맞음
    p.process_job("imgagong", scfg, snap((5, sheet_row(e=EX3, price="60000", basis=basis), "확정", "fp1")))
    w = FakeWriter.last.writes
    assert w[(5, "status")] == pricer.ST_CHECK and "단가 개수 불일치 (단가 1개 ≠ 품목 3개)" in w[(5, "basis")]
    assert len(ai.inputs) == 1  # 다시 견적하지 않음


def test_single_item_row_single_price_still_quoted(env):
    p, ai, scfg, con = env
    p.process_job("imgagong", scfg, snap((5, sheet_row(), "확정", "fp1")))
    basis = FakeWriter.last.writes[(5, "basis")]
    p.process_job("imgagong", scfg, snap((5, sheet_row(status="견적확인필요", price="55,000", basis=basis),
                                          "견적확인필요", "fp1")))
    assert FakeWriter.last.writes[(5, "status")] == pricer.ST_QUOTED_EDIT


# ---------------------------------------------------------------- 블록 규칙: 임가공 2번째 줄 이후
JCOLS = {"date": 1, "client": 2, "main_msg": 3, "spec_qty": 4, "stock_removal": 5, "pre_note": 6, "work_end": 7,
         "shipped": 8, "author": 9, "status": 10, "unit_price": 11, "basis": 12, "row_id": 13}
JSCFG = {"spreadsheet_id": "x", "label": "임가공", "data_start_row": 2, "columns": JCOLS,
         "hash_columns": ["client", "main_msg", "spec_qty", "stock_removal", "pre_note"],
         "item_columns": ["main_msg", "spec_qty", "stock_removal", "pre_note"]}
JHEADER = ["날짜", "거래처", "주요메시지", "공차x갯수", "치대", "매니저노트", "작업완료", "출고완료", "작성자", "상태",
           "단가", "산출근거", "행ID"]


def jr(date_, client, d="", e="", h="", s=""):
    return [date_, client, d, e, h, s, "FALSE", "FALSE", "", "", "", "", ""]


def test_block_continuation_rows_quoted_independently(monkeypatch):
    import sqlite3

    from tests.test_watcher_legacy import make_watcher

    rows = [jr("11/24", "가사", d="긴급", e="Ø80 +0~0.03 / 300 / 1개"),
            jr("", "", e="Ø50 / 2개", h="-0.3"),
            jr("", "", d="5", e="SUSØ35 / 175 / 3개", s="막힘"),
            jr("11/25", "나라", e="Ø40 / 1개")]
    w = make_watcher(rows, clients=[("가사", REG, 5), ("나라", "222", 3)], scfg=JSCFG, header=JHEADER)
    w.process_sheet("t", JSCFG)
    srows = w.snapshots["t"]["rows"]
    assert [(r["client_resolved"], r["client_reg_no"]) for r in srows[:3]] == [("가사", REG)] * 3
    assert srows[0]["block_id"] == srows[1]["block_id"] == srows[2]["block_id"] != srows[3]["block_id"]

    # 묶음이 확정됐다고 보고 pricer에 넘김 → 행마다 따로 견적 (거래처는 묶음 첫 줄 것)
    monkeypatch.setattr(pricer, "SheetWriter", FakeWriter)
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE rows (row_id TEXT, status TEXT)")
    con.execute("CREATE TABLE lines (id INTEGER, kind TEXT)")
    ai = RecAi()
    p = mk_pricer(ai, con=con)
    monkeypatch.setattr(p, "job_index", lambda: LineIndex.build([]))
    for r in srows:
        r["status"] = "확정"
    p.process_job("t", {**JSCFG, "data_start_row": 2}, {"label": "임가공", "ws": None, "rows": srows[:3]})
    got = [(i.client, i.reg_no, i.raw["main_msg"], i.raw["spec_qty"], i.raw["pre_size"], i.raw["pre_note"])
           for i in ai.inputs]
    assert got == [("가사", REG, "긴급", "Ø80 +0~0.03 / 300 / 1개", "", ""),
                   ("가사", REG, "", "Ø50 / 2개", "-0.3", "")]
    assert ai.inputs[1].chidae == 0.3
    wr = FakeWriter.last.writes
    rn = [r["rownum"] for r in srows[:3]]
    assert [wr[(n, "unit_price")] for n in rn] == [10000, 20000, 50000]   # 3번째 줄은 D '5' → 사전단가
    assert all(wr[(n, "status")] == pricer.ST_CHECK for n in rn)


# ---------------------------------------------------------------- 발행 줄
def test_issuer_lines_for_plus_items():
    assert job_lines(get(EX, price="60000/55,000"), ctx()) == [
        Line("SUS 시린더호닝", "Ø63.5 +0~+0.02 / 432L", 2, 60000),
        Line("SUS 시린더호닝", "Ø63.5 +0~+0.02 / 534L", 1, 55000)]
    got = job_lines(get("Ra0.4 Ø50 +0~0.02 / 300 / 2개 + 400 / 1개", price="40000/38000"), ctx())
    assert [(x.spec, x.qty) for x in got] == [("Ra0.4 Ø50 +0~0.02 / 300", 2), ("Ra0.4 Ø50 +0~0.02 / 400", 1)]
