"""
경리나라 내려받기 → SQLite 적재 (feedback.py)

입력 1: 매출거래명세표 목록 (.xls)   → clients, statements
입력 2: 품목별판매현황 (.xls)        → lines
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


def category_of(item, kind=None):
    """'기성품'(호닝파이프·소관 판매) / '임가공'."""
    it = item if isinstance(item, str) else ""
    if kind == "pipe" or "호닝파이프" in it or "소관" in it:
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

def parse_spec(spec, item):
    """규격 문자열 → dict(kind, dia, tol_lo, tol_hi, length, pipe_id, pipe_od)"""
    out = dict(kind=None, dia=None, tol_lo=None, tol_hi=None, length=None, pipe_id=None, pipe_od=None)
    if not isinstance(spec, str) or not spec:
        return out
    s = spec
    item = item if isinstance(item, str) else ""
    is_pipe = bool(item and ("호닝파이프" in item or "소관" in item))
    m = re.search(r"(\d+\.?\d*)\s*[/xX×*]\s*(\d+\.?\d*)\s*[/xX×* ]+\s*(\d+\.?\d*)", s)
    if is_pipe and m:
        out.update(kind="pipe", pipe_id=float(m.group(1)), pipe_od=float(m.group(2)), length=float(m.group(3)))
        return out
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
  color TEXT, memo TEXT, source TEXT DEFAULT 'export');
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

LINES_EXTRA = {"reg_no": "TEXT", "spec_norm": "TEXT", "category": "TEXT"}
CLIENTS_EXTRA = {"source": "TEXT"}  # 'manual' = agent.py clients add (경리나라 import에 아직 없는 거래처)


def ensure_columns(con):
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(lines)")}
    for name, typ in LINES_EXTRA.items():
        if name not in have:
            con.execute(f"ALTER TABLE lines ADD COLUMN {name} {typ}")
    have = {r[1] for r in con.execute("PRAGMA table_info(clients)")}
    for name, typ in CLIENTS_EXTRA.items():
        if name not in have:
            con.execute(f"ALTER TABLE clients ADD COLUMN {name} {typ}")


def replace_clients(con, imported):
    """import 거래처로 clients 교체. 수동 등록(source='manual')은 남기되, 같은 사업자번호가 import에
    들어오면 import 정보로 덮어씀. imported = [(이름, 번호, 명세표수, 첫거래, 최근거래)] → (덮어쓴 수, 남은 수동 수)"""
    con.execute("DELETE FROM clients WHERE COALESCE(source, '') != 'manual'")
    regs = {r[1] for r in imported}
    replaced = [reg for (reg,) in con.execute("SELECT reg_no FROM clients WHERE source='manual'") if reg in regs]
    con.executemany("DELETE FROM clients WHERE source='manual' AND reg_no=?", [(r,) for r in replaced])
    con.executemany("INSERT INTO clients(name, reg_no, n_statements, first_date, last_date, source)"
                    " VALUES(?,?,?,?,?,'import')", imported)
    kept = con.execute("SELECT COUNT(*) FROM clients WHERE source='manual'").fetchone()[0]
    return len(replaced), kept


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
    con.executemany("INSERT INTO statements(date,client,reg_no,item,spec,extra_lines,supply,tax,total,color,memo) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(r.거래일, norm_text(r.거래처명), str(r.등록번호), r.item, r.spec, int(r.extra),
          int(r.공급가액) if pd.notna(r.공급가액) else None, int(r.세액) if pd.notna(r.세액) else None,
          int(r.합계금액) if pd.notna(r.합계금액) else None, r.컬러 if pd.notna(r.컬러) else None,
          norm_text(r.메모)) for r in st.itertuples()])
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
    con.commit()
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
