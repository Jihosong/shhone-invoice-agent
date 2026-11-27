"""
경리나라 내려받기 → SQLite 적재 (feedback.py)

입력 1: 매출거래명세표 목록 (.xls)   → clients, statements
        공통메모 열이 있으면 '(AI)'로 끝나는 건 → statements.issued_by='AI' (발행기 issue.memo_text)
입력 2: 품목별판매현황 (.xls)        → lines
입력 3: 거래처 목록 (.xls/.xlsx)     → clients.ceo_name (대표자명, 동명 구분용) — import_client_list
        python agent.py import clients <파일>  (사업자번호 기준 갱신, 없으면 추가 source='list')
출력  : agent.db (SQLite), data/거래내역_마스터.xlsx (거래내역/거래처마스터/동명거래처)

사용: python feedback.py <거래명세표목록.xls> <품목별판매현황.xls> <agent.db>
"""
import sys, re, sqlite3, unicodedata
from decimal import Decimal
from pathlib import Path
import pandas as pd

MASTER_XLSX_NAME = "거래내역_마스터.xlsx"

DIA = r"[ØΦ∅⌀φ]"

def norm_text(s):
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    s = unicodedata.normalize("NFKC", str(s))
    s = re.sub(r"[ØΦ∅⌀φ]", "Ø", s)
    s = re.sub(r"[\t\u3000]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None

_NUM = r"\d+(?:\.\d+)?"
# 공차 범위: 부호(선택) 숫자 ~ 부호(선택) 숫자.  앞이 글자/숫자/Ø/부호이면 공차가 아님
_RANGE_RE = re.compile(r"(?<![\w.Ø+\-±=])([+-]?)[ ]*(" + _NUM + r")[ ]*~[ ]*([+-]?)[ ]*(" + _NUM + r")(?![\d.])")
# 부호 없는 단일 값: 앞이 글자/숫자/Ø/부호/~/= 이면 제외, 뒤가 숫자/~ 이면 제외
_SINGLE_RE = re.compile(r"(?<![\w.Ø+\-±~=])(" + _NUM + r")(?![\d.]|[ ]*~)")


def _small(v):
    return Decimal(v) < 1


def normalize_spec(spec):
    """규격 표기 정규화.
    - 부호 없는 1 미만 공차에 '+' 부여: 'Ø38 0~ 0.04' → 'Ø38 +0~+0.04', 'Ø425 0.03' → 'Ø425 +0.03'
    - 음수·±·절대치수 범위('Ø117.52~117.54')는 유지
    - 지름 바로 뒤에 붙은 공차는 띄움: 'Ø120+0.0~+0.03' → 'Ø120 +0.0~+0.03'
    """
    s = norm_text(spec)
    if s is None:
        return None
    s = re.sub(r"(Ø" + _NUM + r")(?=[+\-±])", r"\1 ", s)

    def fix_range(m):
        s1, v1, s2, v2 = m.groups()
        if not (_small(v1) and _small(v2)):
            return m.group(0)  # 절대치수 범위
        return f"{s1 or '+'}{v1}~{s2 or '+'}{v2}"

    s = _RANGE_RE.sub(fix_range, s)
    src = s

    def fix_single(m):
        v = m.group(1)
        if not _small(v) or re.search(r"Ra\s*$", src[:m.start()], re.I):
            return v
        return "+" + v

    return _SINGLE_RE.sub(fix_single, s)


# 파이프 판매 줄(kind='pipe', 임가공 이력 검색에서 제외) 판정 — 품목명은 NFKC·공백 제거 후 비교.
#   ① 품목명이 호닝파이프/소관파이프/H.P로 시작 (그 단어뿐인 줄 포함)
#   ② 품목명에 호닝파이프·소관파이프·소관·H.P가 있고, 그 말을 뺀 나머지에 작업 단어가 없음 ('소관및호닝파이프')
#   ③ 규격이 파이프 규격(내경·외경·길이: '160-190-2340'·'160x190 2340'·'80x90 L760')이고 품목명(파이프 말 뺀 나머지)에
#      작업 단어가 없음
# 작업 단어: 호닝(재호닝)·용접·면작업·수리·연마·도금 ('외경쎈타레스연마 20/25/1000'·'내경도금비 159.7/180/560'은 작업) — '소관용접후호닝'·'후랜지용접소관호닝'·'시린더호닝(소관용접후)'은 임가공(job).
_PIPE_START_RE = re.compile(r"^(?:호닝파이프|소관파이프|H\.P(?![A-Za-z]))", re.I)
_PIPE_WORD_RE = re.compile(r"호닝파이프|소관파이프|소관|(?<![A-Za-z])H\.P(?![A-Za-z])", re.I)
_WORK_RE = re.compile(r"호닝|용접|면작업|수리|연마|도금")


def _compact_item(item) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", item)) if isinstance(item, str) else ""


def _has_work_word(compact: str) -> bool:
    """파이프 말(호닝파이프의 '호닝' 등)을 뺀 품목명에 작업 단어가 있는지."""
    return bool(_WORK_RE.search(_PIPE_WORD_RE.sub(" ", compact)))


def is_pipe_item(item) -> bool:
    """품목명만으로 파이프 판매 줄인지 (규칙 ①·②)."""
    c = _compact_item(item)
    return bool(_PIPE_START_RE.match(c) or (_PIPE_WORD_RE.search(c) and not _has_work_word(c)))


def is_pipe_spec(spec) -> bool:
    """규격이 파이프 규격(내경 < 외경 + 길이, Ø·공차 표기 없음)인지."""
    if not isinstance(spec, str) or re.search(r"[ØΦ∅⌀φ~±+]", spec):
        return False
    m = _PIPE_DIMS_RE.search(spec)
    return bool(m and float(m.group(1)) < float(m.group(2)))


def is_pipe_line(item, spec) -> bool:
    """파이프 판매 줄 (규칙 ①·②·③) — kind='pipe', 임가공 이력에서 제외."""
    return is_pipe_item(item) or (is_pipe_spec(spec) and not _has_work_word(_compact_item(item)))


def category_of(item, kind=None):
    """'기성품'(호닝파이프·소관 판매) / '임가공'."""
    if kind == "pipe" or is_pipe_item(item):
        return "기성품"
    return "임가공"


def reg_no_map(clients):
    """[(거래처명, 사업자번호)] → {거래처명: 번호 또는 '동명:번호1/번호2'}"""
    by_name = {}
    for name, reg in clients:
        if name and reg:
            regs = by_name.setdefault(name, [])
            if reg not in regs:
                regs.append(reg)
    return {n: (regs[0] if len(regs) == 1 else "동명:" + "/".join(sorted(regs))) for n, regs in by_name.items()}


def parse_tol(seg):
    """'0.0~0.03', '+0.02~+0.05', '-0.01~-0.03', '0.02', '±0.02' → (lo, hi)
    부호 없는 값은 + 로 해석 (경리나라 일부 내려받기에서 + 소실)."""
    seg = seg.replace(" ", "")
    m = re.search(r"±(\d+\.?\d*)", seg)
    if m:
        v = float(m.group(1)); return (-v, v)
    m = re.search(r"([+-]?\d+\.?\d*)~([+-]?\d+\.?\d*)", seg)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = re.search(r"([+-]?\d+\.\d+)", seg)
    if m:
        v = float(m.group(1)); return (0.0, v) if v >= 0 else (v, 0.0)
    return (None, None)

# 호닝파이프 규격 '내경/외경/길이': '40/50/1191' · '45x55 1228' · '160-190-2340' · '80x90 L760'
_PIPE_DIMS_RE = re.compile(r"(?<![\d.])(\d+\.?\d*)\s*[/xX×*\-]\s*(\d+\.?\d*)\s*(?:[/xX×*\-]|\s)\s*L?\s*(\d+\.?\d*)")


def reclassify_pipe_lines(con) -> tuple[int, int]:
    """기존 DB를 파이프 판매 규칙(is_pipe_line)으로 다시 분류 — 양방향. 규격을 다시 파싱해 kind·치수·category 갱신.
    반환 (job→pipe 줄 수, pipe→job 줄 수)."""
    ensure_columns(con)
    to_pipe, to_job = [], []
    for lid, item, spec, kind in con.execute("SELECT id, item, spec, kind FROM lines"):
        pipe = is_pipe_line(item, spec)
        if pipe and kind != "pipe":
            to_pipe.append((lid, item, spec))
        elif not pipe and kind == "pipe":
            to_job.append((lid, item, spec))
    for lid, item, spec in to_pipe + to_job:
        p = parse_spec(spec, item)
        con.execute("UPDATE lines SET kind=?, dia=?, tol_lo=?, tol_hi=?, length=?, pipe_id=?, pipe_od=?, category=?"
                    " WHERE id=?", (p["kind"], p["dia"], p["tol_lo"], p["tol_hi"], p["length"], p["pipe_id"],
                                    p["pipe_od"], category_of(item, p["kind"]), lid))
    return len(to_pipe), len(to_job)


def parse_spec(spec, item):
    """규격 문자열 → dict(kind, dia, tol_lo, tol_hi, length, pipe_id, pipe_od)"""
    out = dict(kind=None, dia=None, tol_lo=None, tol_hi=None, length=None, pipe_id=None, pipe_od=None)
    if is_pipe_line(item, spec):
        # 파이프 판매 줄은 규격 모양과 무관하게 pipe ('160-190-2340', '80x90 L760', 규격 없음도).
        # 예전엔 '/ x × *' 구분자만 pipe로 봐서 '-'·'L' 표기 줄이 job으로 떨어져 임가공 이력에 섞였다.
        out["kind"] = "pipe"
        m = isinstance(spec, str) and _PIPE_DIMS_RE.search(spec)
        if m:
            out.update(pipe_id=float(m.group(1)), pipe_od=float(m.group(2)), length=float(m.group(3)))
        return out
    if not isinstance(spec, str) or not spec:
        return out
    s = spec
    out["kind"] = "job"
    m = re.search(DIA + r"\s*(\d+\.?\d*)", s) or re.match(r"\s*(\d+\.?\d*)(?=[\s/+\-~=]|$)", s)
    if m:
        out["dia"] = float(m.group(1))
        rest = s[m.end():]
    else:
        rest = s
    m = re.search(r"/\s*(\d+\.?\d*)\s*L?\s*(?:\(|$)", rest) or re.search(r"(\d+\.?\d*)\s*L\b", rest)
    if m:
        out["length"] = float(m.group(1))
        rest = rest[:m.start()]
    lo, hi = parse_tol(rest)
    out["tol_lo"], out["tol_hi"] = lo, hi
    return out

def load_statements(path):
    df = pd.read_excel(path, header=1)
    df.columns = [str(c).strip() for c in df.columns]
    df = df[df["거래일"].notna()].copy()
    df["거래일"] = pd.to_datetime(df["거래일"], errors="coerce").dt.date.astype(str)
    pr = df["품목/규격"].astype(str)
    df["extra"] = pr.str.extract(r"외\s*(\d+)\s*건")[0].fillna(0).astype(int)
    body = pr.str.replace(r"\s*외\s*\d+\s*건\s*$", "", regex=True)
    sp = body.str.split(" / ", n=1, expand=True)
    df["item"] = sp[0].map(norm_text)
    df["spec"] = sp[1].map(norm_text) if sp.shape[1] > 1 else None
    return df

# 명세표 목록에서 공통메모 열 이름 후보, 앞쪽 우선 (발행기가 issue.memo_text를 넣는 칸).
# 경리나라 목록은 공통메모를 '메모'로 내보낼 수 있음 — '(AI)'로 끝나는 값만 표시하니 오인 위험 없음
COMMON_MEMO_COLUMNS = ("공통메모", "메모")
AI_MEMO_SUFFIX = "(AI)"


def common_memo_column(df):
    """명세표 목록 DataFrame에서 공통메모 열 이름 (없으면 None)."""
    return next((c for c in COMMON_MEMO_COLUMNS if c in df.columns), None)


def issued_by_of(memo):
    """공통메모가 '(AI)'로 끝나면 'AI' (발행기가 만든 파일로 올린 명세표), 아니면 None."""
    m = norm_text(memo)
    return "AI" if m and m.endswith(AI_MEMO_SUFFIX) else None


def load_lines(path):
    df = pd.read_excel(path, header=2)
    df.columns = [str(c).strip() for c in df.columns]
    df = df[df["작성일자"].notna()].copy()
    df["작성일자"] = pd.to_datetime(df["작성일자"], errors="coerce").dt.date.astype(str)
    for c in ["수량", "단가", "공급가", "부가세", "합계"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["item"] = df["품목"].map(norm_text)
    df["spec"] = df["규격"].map(norm_text)
    return df

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients(
  name TEXT, reg_no TEXT, n_statements INTEGER, first_date TEXT, last_date TEXT,
  PRIMARY KEY(name, reg_no));
CREATE TABLE IF NOT EXISTS statements(
  id INTEGER PRIMARY KEY, date TEXT, client TEXT, reg_no TEXT,
  item TEXT, spec TEXT, extra_lines INTEGER, supply INTEGER, tax INTEGER, total INTEGER,
  color TEXT, memo TEXT, source TEXT DEFAULT 'export', issued_by TEXT);
CREATE TABLE IF NOT EXISTS lines(
  id INTEGER PRIMARY KEY, date TEXT, client TEXT, item TEXT, spec TEXT,
  qty REAL, unit_price INTEGER, supply INTEGER, note TEXT,
  kind TEXT, dia REAL, tol_lo REAL, tol_hi REAL, length REAL, pipe_id REAL, pipe_od REAL,
  source TEXT DEFAULT 'export', reg_no TEXT, spec_norm TEXT, category TEXT);
CREATE INDEX IF NOT EXISTS ix_lines_client ON lines(client);
CREATE INDEX IF NOT EXISTS ix_lines_item ON lines(item);
CREATE INDEX IF NOT EXISTS ix_lines_pipe ON lines(pipe_id, pipe_od);
CREATE INDEX IF NOT EXISTS ix_stm_key ON statements(date, reg_no);
"""

# lines.source: 'export' = 품목별판매현황 import, 'sheet' = 시트 확정 단가 즉시 학습(sheet_lines.py, row_id=시트 행)
LINES_EXTRA = {"reg_no": "TEXT", "spec_norm": "TEXT", "category": "TEXT", "row_id": "TEXT"}
# source: 'import' = 명세표 목록, 'manual' = agent.py clients add, 'list' = 거래처 목록에만 있음(명세표 없음)
# ceo_name: 대표자명 (거래처 목록 import) — 시트 '거래처(대표명)'으로 동명 구분
CLIENTS_EXTRA = {"source": "TEXT", "ceo_name": "TEXT"}
# issued_by: 'AI' = 공통메모가 '(AI)'로 끝남(발행기 파일로 올린 명세표) — 중복·단가 대조용. 열이 없으면 NULL
STATEMENTS_EXTRA = {"issued_by": "TEXT"}
KEEP_SOURCES = ("manual", "list")  # 명세표 import가 지우지 않는 등록 (같은 번호가 import에 오면 import로 덮어씀)


def ensure_columns(con):
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(lines)")}
    for name, typ in LINES_EXTRA.items():
        if name not in have:
            con.execute(f"ALTER TABLE lines ADD COLUMN {name} {typ}")
    for table, extra in (("clients", CLIENTS_EXTRA), ("statements", STATEMENTS_EXTRA)):
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for name, typ in extra.items():
            if name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def replace_clients(con, imported):
    """import 거래처로 clients 교체. 수동 등록(source='manual')·거래처 목록 전용(source='list')은 남기되,
    같은 사업자번호가 import에 들어오면 import 정보로 덮어씀. 대표자명(ceo_name)은 사업자번호 기준으로 유지.
    imported = [(이름, 번호, 명세표수, 첫거래, 최근거래)] → (덮어쓴 수동 수, 남은 수동 수)"""
    ensure_columns(con)
    ceo = {reg: c for reg, c in con.execute("SELECT reg_no, ceo_name FROM clients WHERE ceo_name IS NOT NULL")}
    keep = ",".join("?" * len(KEEP_SOURCES))
    con.execute(f"DELETE FROM clients WHERE COALESCE(source, '') NOT IN ({keep})", KEEP_SOURCES)
    regs = {r[1] for r in imported}
    replaced = [reg for (reg,) in con.execute("SELECT reg_no FROM clients WHERE source='manual'") if reg in regs]
    con.executemany(f"DELETE FROM clients WHERE source IN ({keep}) AND reg_no=?",
                    [(*KEEP_SOURCES, r) for r in regs])
    con.executemany("INSERT INTO clients(name, reg_no, n_statements, first_date, last_date, source, ceo_name)"
                    " VALUES(?,?,?,?,?,'import',?)", [(*r[:5], ceo.get(r[1])) for r in imported])
    kept = con.execute("SELECT COUNT(*) FROM clients WHERE source='manual'").fetchone()[0]
    return len(replaced), kept


# 거래처 목록 엑셀 열 이름 (공백·괄호 무시 비교) — 경리나라 내려받기 표기가 바뀌어도 맞도록 후보 여럿
CLIENT_LIST_COLUMNS = {
    "name": ("거래처명", "상호", "상호명", "거래처", "회사명", "업체명"),
    "reg_no": ("사업자번호", "사업자등록번호", "등록번호", "사업자등록번호주민번호", "사업자주민번호"),
    "ceo": ("대표자명", "대표자", "대표명", "대표자성명", "성명대표자", "대표"),
}


def _header_key(v):
    return re.sub(r"[\s()（）/·.]", "", unicodedata.normalize("NFKC", str(v))) if v is not None else ""


def format_reg_no(v):
    """'1234567890' / '123-45-67890' / 1234567890.0 → '123-45-67890' (10자리가 아니면 정리만)."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    d = re.sub(r"\D", "", s)
    return f"{d[:3]}-{d[3:5]}-{d[5:]}" if len(d) == 10 else s


def load_client_list(path):
    """거래처 목록 → [(거래처명, 사업자번호, 대표자명)]. 머리글 행은 위 10줄 안에서 세 열이 모두 있는 행.
    거래처명·사업자번호·대표자명 열이 없으면 ValueError (찾은 머리글을 알려줌)."""
    raw = pd.read_excel(path, header=None, dtype=str)
    want = {k: {_header_key(c) for c in cands} for k, cands in CLIENT_LIST_COLUMNS.items()}
    for hi in range(min(10, len(raw))):
        heads = [_header_key(v) for v in raw.iloc[hi]]
        # 정확히 같은 머리글 우선, 없으면 4글자 이상 후보를 포함하는 머리글 ('상호(거래처명)' 등)
        where = {k: next((i for i, h in enumerate(heads) if h in keys), None)
                 if any(h in keys for h in heads) else
                 next((i for i, h in enumerate(heads) if any(len(c) >= 4 and c in h for c in keys)), None)
                 for k, keys in want.items()}
        if all(v is not None for v in where.values()):
            break
    else:
        first = [str(v) for v in raw.iloc[0].tolist()] if len(raw) else []
        raise ValueError("거래처 목록에서 거래처명·사업자번호·대표자명 열을 못 찾음 (첫 줄: %s)" % first)
    out = []
    for row in raw.iloc[hi + 1:].itertuples(index=False):
        name, reg, ceo = (row[where[k]] for k in ("name", "reg_no", "ceo"))
        name, reg, ceo = norm_text(name), format_reg_no(reg), norm_text(ceo)
        if name and reg:
            out.append((name, reg, ceo))
    return out


def import_client_list(con, entries):
    """거래처 목록 → clients. 사업자번호 기준: 있으면 대표자명 갱신(이름은 명세표 표기 유지), 없으면 추가
    (source='list', 명세표 0건). 반환 (갱신 행 수, 추가 수, 대표자명 빈 수)."""
    ensure_columns(con)
    updated = added = no_ceo = 0
    for name, reg, ceo in entries:
        no_ceo += not ceo
        cur = con.execute("UPDATE clients SET ceo_name=? WHERE reg_no=?", (ceo, reg))
        if cur.rowcount:
            updated += cur.rowcount
        else:
            con.execute("INSERT INTO clients(name, reg_no, n_statements, first_date, last_date, source, ceo_name)"
                        " VALUES(?,?,0,NULL,NULL,'list',?)", (name, reg, ceo))
            added += 1
    con.commit()
    return updated, added, no_ceo


def main_clients(path, db_path):
    con = sqlite3.connect(db_path)
    entries = load_client_list(path)
    updated, added, no_ceo = import_client_list(con, entries)
    print("거래처 목록 %d건: 대표자명 갱신=%d 추가(source=list)=%d 대표자명 빈칸=%d" % (len(entries), updated, added, no_ceo))


def add_manual_client(con, name, reg_no):
    """clients에 수동 등록 (source='manual'). 같은 번호가 이미 있으면: 수동이면 이름 갱신, import면 거절.
    반환 (결과 문구, 성공 여부)."""
    ensure_columns(con)
    name = norm_text(name)
    row = con.execute("SELECT name, COALESCE(source, '') FROM clients WHERE reg_no=?", (reg_no,)).fetchone()
    if row and row[1] != "manual":
        return f"이미 등록됨 (경리나라 import): {row[0]} {reg_no}", False
    if row:
        con.execute("UPDATE clients SET name=? WHERE reg_no=? AND source='manual'", (name, reg_no))
        con.commit()
        return f"수동 등록 이름 변경: {row[0]} → {name} {reg_no}", True
    con.execute("INSERT INTO clients(name, reg_no, n_statements, first_date, last_date, source)"
                " VALUES(?,?,0,NULL,NULL,'manual')", (name, reg_no))
    con.commit()
    return f"수동 등록: {name} {reg_no} (다음 월간 import에 같은 번호가 들어오면 import 정보로 덮어씀)", True


def write_master(con, out_path):
    """통합 마스터 엑셀: 거래내역 / 거래처마스터 / 동명거래처"""
    lines = pd.read_sql_query(
        "SELECT date AS 날짜, client AS 거래처, reg_no AS 사업자번호, category AS 구분, item AS 품목,"
        " spec AS 규격_원본, spec_norm AS 규격, qty AS 수량, unit_price AS 단가, supply AS 공급가, note AS 비고,"
        " dia AS 지름, tol_lo AS 공차_하, tol_hi AS 공차_상, length AS 길이, pipe_id AS 파이프_ID, pipe_od AS 파이프_OD"
        " FROM lines WHERE source='export' ORDER BY date, id", con)
    clients = pd.read_sql_query(
        "SELECT c.name AS 거래처명, c.reg_no AS 사업자번호, c.n_statements AS 명세표수,"
        " c.first_date AS 첫거래, c.last_date AS 최근거래, d.n AS 동명수"
        " FROM clients c LEFT JOIN (SELECT name, COUNT(*) n FROM clients GROUP BY name) d ON d.name=c.name"
        " ORDER BY c.name, c.reg_no", con)
    clients["동명여부"] = clients.pop("동명수").gt(1).map({True: "동명", False: ""})
    dup = clients[clients["동명여부"] == "동명"].drop(columns="동명여부")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
        for name, df in (("거래내역", lines), ("거래처마스터", clients), ("동명거래처", dup)):
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
    return len(lines), len(clients), len(dup)


def main(stm_path, line_path, db_path, master_path=None):
    st = load_statements(stm_path)
    ln = load_lines(line_path)
    con = sqlite3.connect(db_path)
    ensure_columns(con)
    con.execute("DELETE FROM statements WHERE source='export'"); con.execute("DELETE FROM lines WHERE source='export'")
    g = st.groupby(["거래처명", "등록번호"]).agg(n=("거래일", "size"), f=("거래일", "min"), l=("거래일", "max")).reset_index()
    n_rep, n_manual = replace_clients(con, [(norm_text(r.거래처명), str(r.등록번호), int(r.n), r.f, r.l)
                                           for r in g.itertuples()])
    print("수동 등록 거래처: import로 덮어씀=%d 남음=%d" % (n_rep, n_manual))
    memo_col = common_memo_column(st)
    if memo_col is None:
        print("명세표 목록에 공통메모 열 없음 → AI 발행 표시(issued_by) 건너뜀 (열: %s)" % ", ".join(st.columns))
    issued = [issued_by_of(v) for v in st[memo_col]] if memo_col else [None] * len(st)
    memos = [norm_text(v) for v in st["메모"]] if "메모" in st.columns else [None] * len(st)
    con.executemany("INSERT INTO statements(date,client,reg_no,item,spec,extra_lines,supply,tax,total,color,memo,issued_by)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r.거래일, norm_text(r.거래처명), str(r.등록번호), r.item, r.spec, int(r.extra),
          int(r.공급가액) if pd.notna(r.공급가액) else None, int(r.세액) if pd.notna(r.세액) else None,
          int(r.합계금액) if pd.notna(r.합계금액) else None, r.컬러 if pd.notna(r.컬러) else None,
          memo, by) for r, memo, by in zip(st.itertuples(), memos, issued)])
    if memo_col:
        print("AI 발행 명세표(공통메모 '%s'로 끝남)=%d / %d" % (AI_MEMO_SUFFIX, sum(b == "AI" for b in issued), len(st)))
    regs = reg_no_map(con.execute("SELECT name, reg_no FROM clients").fetchall())
    rows = []
    for r in ln.itertuples():
        p = parse_spec(r.spec, r.item)
        client = norm_text(r.회사명)
        rows.append((r.작성일자, client, r.item, r.spec,
                     float(r.수량) if pd.notna(r.수량) else None,
                     int(r.단가) if pd.notna(r.단가) else None,
                     int(r.공급가) if pd.notna(r.공급가) else None,
                     norm_text(r.비고), p["kind"], p["dia"], p["tol_lo"], p["tol_hi"], p["length"], p["pipe_id"], p["pipe_od"],
                     regs.get(client), normalize_spec(r.spec), category_of(r.item, p["kind"])))
    con.executemany("INSERT INTO lines(date,client,item,spec,qty,unit_price,supply,note,kind,dia,tol_lo,tol_hi,length,pipe_id,pipe_od,"
                    "reg_no,spec_norm,category) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    import sheet_lines

    n_pipe, n_job = reclassify_pipe_lines(con)  # 시트 학습 줄(source=sheet) 중 옛 규칙으로 분류된 줄
    if n_pipe or n_job:
        print("파이프 판매 규칙 재분류: job→pipe=%d pipe→job=%d" % (n_pipe, n_job))
    n_gone = sheet_lines.replace_by_official(con)
    con.commit()
    print("시트 확정 줄(source=sheet): 공식 줄로 대체되어 삭제=%d 남음=%d" % (
        n_gone, con.execute("SELECT COUNT(*) FROM lines WHERE source='sheet'").fetchone()[0]))
    def q(s):
        return con.execute(s).fetchone()[0]
    print("clients=%d statements=%d lines=%d" % (q("select count(*) from clients"), q("select count(*) from statements"), q("select count(*) from lines")))
    print("lines kind: pipe=%d job=%d none=%d" % (q("select count(*) from lines where kind='pipe'"), q("select count(*) from lines where kind='job'"), q("select count(*) from lines where kind is null")))
    print("job lines with dia=%d, with tol=%d, with length=%d" % (q("select count(*) from lines where kind='job' and dia is not null"), q("select count(*) from lines where kind='job' and tol_hi is not null"), q("select count(*) from lines where kind='job' and length is not null")))
    print("pipe lines parsed id/od/len=%d" % q("select count(*) from lines where kind='pipe' and pipe_id is not null"))
    print("lines reg_no: 단일=%d 동명=%d 없음=%d / category: 기성품=%d 임가공=%d" % (
        q("select count(*) from lines where reg_no is not null and reg_no not like '동명:%'"),
        q("select count(*) from lines where reg_no like '동명:%'"), q("select count(*) from lines where reg_no is null"),
        q("select count(*) from lines where category='기성품'"), q("select count(*) from lines where category='임가공'")))
    if master_path is None:
        master_path = Path(db_path).parent / MASTER_XLSX_NAME
    n_l, n_c, n_d = write_master(con, master_path)
    print("master=%s 거래내역=%d 거래처마스터=%d 동명거래처=%d" % (master_path, n_l, n_c, n_d))
    con.close()

if __name__ == "__main__":
    main(*sys.argv[1:4])
