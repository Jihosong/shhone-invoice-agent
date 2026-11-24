"""
경리나라 내려받기 → SQLite 적재 (feedback.py)

입력 1: 매출거래명세표 목록 (.xls)   → clients, statements
입력 2: 품목별판매현황 (.xls)        → lines
출력  : agent.db (SQLite)

사용: python feedback.py <거래명세표목록.xls> <품목별판매현황.xls> <agent.db>
"""
import sys, re, sqlite3, unicodedata
import pandas as pd

DIA = r"[ØΦ∅⌀φ]"

def norm_text(s):
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    s = unicodedata.normalize("NFKC", str(s))
    s = re.sub(r"[ØΦ∅⌀φ]", "Ø", s)
    s = re.sub(r"[\t\u3000]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None

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
  source TEXT DEFAULT 'export');
CREATE INDEX IF NOT EXISTS ix_lines_client ON lines(client);
CREATE INDEX IF NOT EXISTS ix_lines_item ON lines(item);
CREATE INDEX IF NOT EXISTS ix_lines_pipe ON lines(pipe_id, pipe_od);
CREATE INDEX IF NOT EXISTS ix_stm_key ON statements(date, reg_no);
"""

def main(stm_path, line_path, db_path):
    st = load_statements(stm_path)
    ln = load_lines(line_path)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    con.execute("DELETE FROM clients"); con.execute("DELETE FROM statements WHERE source='export'"); con.execute("DELETE FROM lines WHERE source='export'")
    g = st.groupby(["거래처명", "등록번호"]).agg(n=("거래일", "size"), f=("거래일", "min"), l=("거래일", "max")).reset_index()
    con.executemany("INSERT INTO clients VALUES(?,?,?,?,?)",
                    [(norm_text(r.거래처명), str(r.등록번호), int(r.n), r.f, r.l) for r in g.itertuples()])
    con.executemany("INSERT INTO statements(date,client,reg_no,item,spec,extra_lines,supply,tax,total,color,memo) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [(r.거래일, norm_text(r.거래처명), str(r.등록번호), r.item, r.spec, int(r.extra),
          int(r.공급가액) if pd.notna(r.공급가액) else None, int(r.세액) if pd.notna(r.세액) else None,
          int(r.합계금액) if pd.notna(r.합계금액) else None, r.컬러 if pd.notna(r.컬러) else None,
          norm_text(r.메모)) for r in st.itertuples()])
    rows = []
    for r in ln.itertuples():
        p = parse_spec(r.spec, r.item)
        rows.append((r.작성일자, norm_text(r.회사명), r.item, r.spec,
                     float(r.수량) if pd.notna(r.수량) else None,
                     int(r.단가) if pd.notna(r.단가) else None,
                     int(r.공급가) if pd.notna(r.공급가) else None,
                     norm_text(r.비고), p["kind"], p["dia"], p["tol_lo"], p["tol_hi"], p["length"], p["pipe_id"], p["pipe_od"]))
    con.executemany("INSERT INTO lines(date,client,item,spec,qty,unit_price,supply,note,kind,dia,tol_lo,tol_hi,length,pipe_id,pipe_od) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    def q(s):
        return con.execute(s).fetchone()[0]
    print("clients=%d statements=%d lines=%d" % (q("select count(*) from clients"), q("select count(*) from statements"), q("select count(*) from lines")))
    print("lines kind: pipe=%d job=%d none=%d" % (q("select count(*) from lines where kind='pipe'"), q("select count(*) from lines where kind='job'"), q("select count(*) from lines where kind is null")))
    print("job lines with dia=%d, with tol=%d, with length=%d" % (q("select count(*) from lines where kind='job' and dia is not null"), q("select count(*) from lines where kind='job' and tol_hi is not null"), q("select count(*) from lines where kind='job' and length is not null")))
    print("pipe lines parsed id/od/len=%d" % q("select count(*) from lines where kind='pipe' and pipe_id is not null"))
    con.close()

if __name__ == "__main__":
    main(*sys.argv[1:4])
