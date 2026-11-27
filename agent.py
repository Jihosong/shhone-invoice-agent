"""shhone-invoice-agent 진입점.

사용:
    python agent.py run         # 시트 감시 → 단가 산출 → 거래명세표 발행 루프
    python agent.py import <거래명세표목록.xls> <품목별판매현황.xls>  # 경리나라 내려받기 → DB 적재
    python agent.py blocks      # 현재 월 탭의 묶음 목록 출력 (dry-run, 시트 수정 없음)
    python agent.py issue       # 한 바퀴 돌고 명세표발행대기 묶음을 data/outbox/YYYYMMDD_HHMM.xls 한 파일로
    python agent.py issue --dry-run  # 파일만 임시 폴더에 (시트·DB 안 건드림)
    python agent.py outbox      # outbox 파일별 포함 묶음·상태
    python agent.py aliases check     # config/client_aliases.yaml 검사 (틀린 줄 표시)
    python agent.py aliases suggest   # 시트의 미해석 거래처 → 마스터 후보 (yaml 주석 형식)
    python agent.py clients find <검색어>  # 마스터 이름 부분일치: 정식명/사업자번호/최근거래/올해 건수
    python agent.py clients add "<정식명>" <사업자번호>  # 경리나라 import에 없는 거래처 수동 등록
    python agent.py clients trend <거래처>  # 재주문 쌍(품목·날짜·단가 전후·변화율) + 동결형/인상형 판정 근거
    python agent.py backtest build [--year 2026]  # 임가공 월 탭 전부(읽기만) + DB 실제 단가 → data/backtest/ 정답지
    python agent.py backtest run --no-ai  # 정답지 match≠none 행에 사다리(사전단가·동일품) 채점 → results_noai.csv
    python agent.py backtest run --ai --limit 200 --seed 0  # 미결 행 표본 Claude 견적 채점 → results_ai.csv
    python agent.py backtest inflation  # DB 같은 거래처·같은 품목 연도별 단가 변화율 → data/backtest/inflation.csv
    python agent.py calibrate   # 화면 좌표/비전 보정
"""
import argparse


def _file_log() -> None:
    from pathlib import Path

    from loguru import logger

    # 콘솔만으로는 사고 뒤 원인을 못 찾음 → 파일에도 남김 (10MB 회전, 14개 보관)
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    logger.add(log_dir / "agent.log", rotation="10 MB", retention=14, encoding="utf-8", enqueue=True)


def _pipeline(dry_run: bool):
    import pricer
    import watcher
    from issuer.outbox import Issuer

    cfg = watcher.load_config()
    w = watcher.Watcher(cfg, dry_run=dry_run)
    w.defer_flush = True  # watcher+pricer 쓰기를 한 번에 (사람 확인 행은 같은 바퀴에 견적완료(사람확인/사람수정)까지)
    return cfg, w, pricer.Pricer(cfg, w.con, dry_run=dry_run), Issuer(cfg, w.con, dry_run=dry_run)


def _quote_cycle(w, p) -> None:
    """시트 감시 → 상태(입력중/확정/기존건 …) → 확정 행 단가 산출 → 둘의 시트 쓰기를 시트당 한 번에.
    켠 뒤 첫 바퀴는 watcher 보호 모드 (지문 그대로인 행은 상태·쓰기 없음). 첫 바퀴에 대량 재견적이 감지되면
    watcher가 RestartGuardError (DB 되돌림·쓰기 버림) → _guarded가 ERROR로 끝냄."""
    try:
        w.run_once()
        p.run_once(w.snapshots)
    finally:
        w.flush_writes()


def _guarded(fn):
    """재시작 후 대량 재견적 감지(watcher.RestartGuardError) → 종료 코드 2 (시트·DB 그대로)."""
    import watcher

    try:
        return fn()
    except watcher.RestartGuardError as e:
        raise SystemExit(f"중단: {e}") from None


def cmd_run(args: argparse.Namespace) -> None:
    import time

    _file_log()
    cfg, w, p, iss = _pipeline(args.dry_run)
    interval = int(cfg["common"].get("poll_interval_sec", 15))
    bundle_every = float((cfg.get("issue") or {}).get("bundle_interval_min", 3)) * 60
    last_bundle = None
    cycles = 0
    while True:
        started = time.monotonic()
        _guarded(lambda: _quote_cycle(w, p))
        cycles += 1
        # 명세표발행대기 표시·명세표발행완료(AI) 마감, 주기마다 outbox 파일 묶음 (dry-run이면 파일 안 만듦 → agent.py issue --dry-run)
        due = not args.dry_run and (last_bundle is None or started - last_bundle >= bundle_every)
        iss.run_once(w.snapshots, bundle=due)
        if due:
            last_bundle = started
        w.api.report()  # 1분마다 Sheets API 호출 수 (한도 사용자당 60/분)
        if args.once and cycles >= 2:  # 첫 바퀴는 보호 모드라 --once는 보호 바퀴 + 평소 한 바퀴
            return
        time.sleep(max(0.0, interval - (time.monotonic() - started)))


def cmd_issue(args: argparse.Namespace) -> None:
    if not args.dry_run:
        _file_log()
    _, w, p, iss = _pipeline(args.dry_run)
    for _ in range(2):  # 첫 바퀴는 보호 모드 (지문 그대로인 행은 손대지 않음) → 평소 한 바퀴 더
        _guarded(lambda: _quote_cycle(w, p))
    path = iss.run_once(w.snapshots, bundle=True)
    print(path if path else "새로 담을 명세표발행대기 묶음 없음")


def cmd_outbox(args: argparse.Namespace) -> None:
    import watcher
    from issuer.outbox import format_outbox

    con = _db()
    watcher.ensure_columns(con)
    for line in format_outbox(con):
        print(line)


def cmd_import(args: argparse.Namespace) -> None:
    from pathlib import Path

    import feedback
    from config.settings import settings

    Path(settings.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    files = args.files
    if files[0] == "clients":  # python agent.py import clients <거래처목록.xls>
        if len(files) != 2:
            raise SystemExit("사용: python agent.py import clients <거래처목록.xls>")
        feedback.main_clients(files[1], settings.DB_PATH)
        return
    if len(files) != 2:
        raise SystemExit("사용: python agent.py import <매출거래명세표목록.xls> <품목별판매현황.xls>")
    feedback.main(files[0], files[1], settings.DB_PATH)


def format_blocks(snap_rows: list[dict]) -> list[str]:
    """watcher 스냅샷 행 → 묶음당 한 줄: 거래처 / 날짜 / 행번호 / 상태."""
    from collections import Counter

    blocks: dict[str, list[dict]] = {}
    for r in snap_rows:
        blocks.setdefault(r["block_id"], []).append(r)
    lines = []
    for rows in blocks.values():
        rownums = ",".join(str(r["rownum"]) for r in rows)
        st = Counter(r["status"] or "사람처리" for r in rows)
        status = next(iter(st)) if len(st) == 1 else ", ".join(f"{k}×{v}" for k, v in st.items())
        lines.append(f"{rows[0]['block_label']} / {rows[0]['block_date']} / 행 {rownums} / {status}")
    return lines


def cmd_blocks(args: argparse.Namespace) -> None:
    import watcher

    w = watcher.Watcher(watcher.load_config(), dry_run=True)
    w._first_done.update(w.cfg["sheets"])  # 목록만 (dry-run) — 재시작 보호 바퀴 없이 평소 판정으로 보여 줌
    w.run_once()  # dry-run: 시트 쓰기 없음, DB 롤백
    for key, snap in w.snapshots.items():
        print(f"== {snap['label']} / {snap['ws'].title} ({len({r['block_id'] for r in snap['rows']})}묶음)")
        for line in format_blocks(snap["rows"]):
            print("  " + line)


def _db():
    import sqlite3

    from config.settings import settings

    return sqlite3.connect(settings.DB_PATH)


def cmd_aliases_check(args: argparse.Namespace) -> None:
    import sys
    from pathlib import Path

    import clients_cli
    import watcher

    path = Path(args.file) if args.file else watcher.ALIASES_YAML
    if not path.exists():
        sys.exit(f"{path} 없음")
    problems = clients_cli.check_aliases(path, _db())
    for line, level, msg in problems:
        print(f"{path.name}:{line}  [{level}] {msg}")
    n_err = sum(1 for _, level, _ in problems if level == "오류")
    print(f"검사 끝: 오류 {n_err}, 경고·안내 {len(problems) - n_err}")
    sys.exit(1 if n_err else 0)


def cmd_aliases_suggest(args: argparse.Namespace) -> None:
    import clients_cli

    texts = args.text or clients_cli.unresolved_from_sheets()
    con = _db()
    for t in texts:
        print(clients_cli.suggestion_line(con, t))


def cmd_clients_find(args: argparse.Namespace) -> None:
    from datetime import date

    import clients_cli

    clients_cli.print_find(_db(), " ".join(args.query), args.year or str(date.today().year))


def cmd_clients_add(args: argparse.Namespace) -> None:
    import sys

    import feedback
    import watcher

    if not watcher.REG_NO_RE.match(args.reg_no):
        sys.exit(f"사업자번호 형식 아님: {args.reg_no!r} (000-00-00000)")
    msg, ok = feedback.add_manual_client(_db(), args.name, args.reg_no)
    print(msg)
    sys.exit(0 if ok else 1)


def cmd_clients_trend(args: argparse.Namespace) -> None:
    import sqlite3
    import sys
    from pathlib import Path

    import clients_cli
    from config.settings import settings

    db = Path(settings.DB_PATH)
    db = db if db.is_absolute() else Path(__file__).parent / db
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)  # DB 읽기 전용
    sys.exit(0 if clients_cli.print_trend(con, " ".join(args.query)) else 1)


def cmd_backtest_build(args: argparse.Namespace) -> None:
    import backtest

    backtest.build(args.year, samples=args.samples, none_samples=args.none_samples, seed=args.seed)


def cmd_backtest_run(args: argparse.Namespace) -> None:
    import backtest

    backtest.run_scores(args.year, ai=args.ai, limit=args.limit, seed=args.seed, top=args.top)


def cmd_backtest_inflation(args: argparse.Namespace) -> None:
    import sqlite3
    from pathlib import Path

    import inflation
    from config.settings import settings

    db = Path(settings.DB_PATH)
    db = db if db.is_absolute() else Path(__file__).parent / db
    inflation.run(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True))  # DB 읽기 전용


def cmd_calibrate(args: argparse.Namespace) -> None:
    print("calibrate: TODO")


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent", description="shhone-invoice-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="시트 감시 및 발행 루프 실행")
    p_run.add_argument("--once", action="store_true", help="한 바퀴만 돌고 종료")
    p_run.add_argument("--dry-run", action="store_true", help="시트/DB에 쓰지 않고 요약만 출력")
    p_run.set_defaults(func=cmd_run)
    p_imp = sub.add_parser("import", help="경리나라 내려받기 DB 적재 (명세표목록 품목별판매현황 | clients 거래처목록)")
    p_imp.add_argument("files", nargs="+", metavar="FILE",
                       help="<매출거래명세표목록.xls> <품목별판매현황.xls>  또는  clients <거래처목록.xls>")
    p_imp.set_defaults(func=cmd_import)
    sub.add_parser("blocks", help="현재 월 탭 묶음 목록 출력 (dry-run)").set_defaults(func=cmd_blocks)
    p_iss = sub.add_parser("issue", help="명세표발행대기 묶음 → data/outbox/YYYYMMDD_HHMM.xls (경리나라 엑셀일괄등록)")
    p_iss.add_argument("--dry-run", action="store_true", help="파일만 임시 폴더에 만들고 시트·DB 안 건드림")
    p_iss.set_defaults(func=cmd_issue)
    sub.add_parser("outbox", help="outbox 파일별 포함 묶음·상태").set_defaults(func=cmd_outbox)
    p_al = sub.add_parser("aliases", help="거래처 별칭표").add_subparsers(dest="aliases_cmd", required=True)
    p_chk = p_al.add_parser("check", help="client_aliases.yaml 검사")
    p_chk.add_argument("--file", help="검사할 yaml (기본 config/client_aliases.yaml)")
    p_chk.set_defaults(func=cmd_aliases_check)
    p_sug = p_al.add_parser("suggest", help="미해석 거래처 후보 (기본: 두 시트 활성 탭에서 수집)")
    p_sug.add_argument("text", nargs="*", help="직접 줄 시트 표기 (없으면 시트에서 수집)")
    p_sug.set_defaults(func=cmd_aliases_suggest)
    p_cl = sub.add_parser("clients", help="거래처 마스터").add_subparsers(dest="clients_cmd", required=True)
    p_find = p_cl.add_parser("find", help="이름 부분일치 검색")
    p_find.add_argument("query", nargs="+")
    p_find.add_argument("--year", help="건수 집계 연도 (기본 올해)")
    p_find.set_defaults(func=cmd_clients_find)
    p_add = p_cl.add_parser("add", help="거래처 수동 등록 (source=manual)")
    p_add.add_argument("name", help="정식명")
    p_add.add_argument("reg_no", help="사업자번호 000-00-00000")
    p_add.set_defaults(func=cmd_clients_add)
    p_tr = p_cl.add_parser("trend", help="재주문 쌍과 동결형/인상형 판정 근거 (DB 읽기 전용)")
    p_tr.add_argument("query", nargs="+", help="거래처 이름 (정규화 일치 또는 부분일치 하나)")
    p_tr.set_defaults(func=cmd_clients_trend)
    p_bt = sub.add_parser("backtest", help="임가공 견적 백테스트").add_subparsers(dest="backtest_cmd", required=True)
    p_btb = p_bt.add_parser("build", help="임가공 시트 월 탭 전부 + DB 실제 단가 → data/backtest/imgagong_<연도>.csv/.xlsx")
    p_btb.add_argument("--year", type=int, default=2026)
    p_btb.add_argument("--samples", type=int, default=30, help="콘솔에 보일 시트 원문 샘플 수")
    p_btb.add_argument("--none-samples", type=int, default=20, help="콘솔에 보일 match=none 샘플 수")
    p_btb.add_argument("--seed", type=int, default=0)
    p_btb.set_defaults(func=cmd_backtest_build)
    p_btr = p_bt.add_parser("run", help="정답지 채점: --no-ai 사다리 a·b만 / --ai 미결 행 Claude 견적(B단계)")
    g = p_btr.add_mutually_exclusive_group()
    g.add_argument("--no-ai", dest="ai", action="store_false", help="사전단가·동일품만 (기본)")
    g.add_argument("--ai", dest="ai", action="store_true", help="미결 행 일부를 Claude로 채점")
    p_btr.set_defaults(ai=False, func=cmd_backtest_run)
    p_btr.add_argument("--year", type=int, default=2026)
    p_btr.add_argument("--limit", type=int, default=200, help="--ai: Claude로 채점할 미결 행 수")
    p_btr.add_argument("--seed", type=int, default=0)
    p_btr.add_argument("--top", type=int, default=20, help="틀린 것 상위 N건")
    p_bt.add_parser("inflation", help="DB 같은 거래처·같은 품목 연도별 단가 변화율 → data/backtest/inflation.csv"
                    ).set_defaults(func=cmd_backtest_inflation)
    sub.add_parser("calibrate", help="화면 좌표/비전 보정").set_defaults(func=cmd_calibrate)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
