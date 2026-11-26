"""시트 확정 단가 즉시 학습 — lines(source='sheet').

사람이 임가공 시트에서 단가를 확정(가견적 행 상태 칸 비움 / 견적확인필요 행 단가 수정 → 견적완료)하거나
묶음이 명세표발행완료(AI)으로 마감되면, 그 행을 발행 줄과 같은 방식(issuer.excel_upload.job_lines)으로 줄로 만들어
lines에 source='sheet', row_id=시트 행 id로 넣는다. 다음 견적부터 동일품·유사품 검색(cases.load_job_lines)에 쓰인다.
  - 같은 row_id의 sheet 줄은 지우고 다시 넣는다 (사람 확정 뒤 명세표발행완료(AI) 마감 = 같은 줄 갱신, 중복 없음).
  - 월간 import(feedback.main) 뒤 replace_by_official: 같은 거래처(사업자번호, 없으면 이름)·같은 날짜·
    같은 구경(±0.3mm, 구경 없는 줄은 둘 다 없을 때)의 공식 줄(source='export')이 있으면 sheet 줄은 지운다.
  - 백테스트는 공식 줄만 본다 (load_job_lines(official_only=True)) — 시트에서 온 답이 섞이지 않게.
"""
from __future__ import annotations

import sqlite3
from datetime import date

SOURCE = "sheet"
DIA_TOL = 0.3   # pricer_job.SAME_DIA_TOL과 같음


def add_sheet_lines(con: sqlite3.Connection, row_id: str, client: str, reg_no: str, day: date,
                    lines: list, note: str = "") -> int:
    """시트 행 하나의 줄들(issuer.excel_upload.Line)을 lines(source='sheet')로. 같은 row_id 이전 줄은 교체.
    반환 넣은 줄 수."""
    import feedback

    feedback.ensure_columns(con)
    con.execute("DELETE FROM lines WHERE source=? AND row_id=?", (SOURCE, row_id))
    rows = []
    for ln in lines:
        spec = feedback.norm_text(ln.spec) or ""
        item = feedback.norm_text(ln.name) or ""
        p = feedback.parse_spec(spec, item)
        kind = p["kind"] or "job"
        rows.append((day.isoformat(), client, item, spec, float(ln.qty), int(ln.unit_price),
                     int(ln.unit_price) * int(ln.qty), note or None, kind, p["dia"], p["tol_lo"], p["tol_hi"],
                     p["length"], p["pipe_id"], p["pipe_od"], SOURCE, reg_no or None,
                     feedback.normalize_spec(spec), feedback.category_of(item, kind), row_id))
    con.executemany("INSERT INTO lines(date,client,item,spec,qty,unit_price,supply,note,kind,dia,tol_lo,tol_hi,length,"
                    "pipe_id,pipe_od,source,reg_no,spec_norm,category,row_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def replace_by_official(con: sqlite3.Connection) -> int:
    """공식 줄로 대체된 sheet 줄 삭제. 반환 지운 줄 수."""
    import feedback

    feedback.ensure_columns(con)
    sheet = con.execute("SELECT id, date, client, reg_no, dia FROM lines WHERE source=?", (SOURCE,)).fetchall()
    if not sheet:
        return 0
    official: dict[str, list[tuple]] = {}
    dates = sorted({d for _, d, *_ in sheet})
    q = ",".join("?" * len(dates))
    for d, client, reg, dia in con.execute(
            f"SELECT date, client, reg_no, dia FROM lines WHERE source='export' AND date IN ({q})", dates):
        official.setdefault(d, []).append((client, reg or "", dia))

    def same_client(reg, client, o_client, o_reg):
        if reg and o_reg and not o_reg.startswith("동명:"):
            return reg == o_reg
        return client == o_client

    gone = [lid for lid, d, client, reg, dia in sheet
            if any(same_client(reg, client, oc, oreg)
                   and ((dia is None and odia is None)
                        or (dia is not None and odia is not None and abs(dia - odia) <= DIA_TOL + 1e-9))
                   for oc, oreg, odia in official.get(d, []))]
    con.executemany("DELETE FROM lines WHERE id=?", [(i,) for i in gone])
    return len(gone)
