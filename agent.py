"""shhone-invoice-agent 진입점.

사용:
    python agent.py run         # 시트 감시 → 단가 산출 → 거래명세표 발행 루프
    python agent.py import <거래명세표목록.xls> <품목별판매현황.xls>  # 경리나라 내려받기 → DB 적재
    python agent.py blocks      # 현재 월 탭의 묶음 목록 출력 (dry-run, 시트 수정 없음)
    python agent.py aliases check     # config/client_aliases.yaml 검사 (틀린 줄 표시)
    python agent.py aliases suggest   # 시트의 미해석 거래처 → 마스터 후보 (yaml 주석 형식)
    python agent.py clients find <검색어>  # 마스터 이름 부분일치: 정식명/사업자번호/최근거래/올해 건수
    python agent.py clients add "<정식명>" <사업자번호>  # 경리나라 import에 없는 거래처 수동 등록
    python agent.py calibrate   # 화면 좌표/비전 보정
"""
import argparse


def cmd_run(args: argparse.Namespace) -> None:
    import time

    import pricer
    import watcher

    from pathlib import Path

    from loguru import logger

    # 콘솔만으로는 사고 뒤 원인을 못 찾음 → 파일에도 남김 (10MB 회전, 14개 보관)
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)
    logger.add(log_dir / "agent.log", rotation="10 MB", retention=14, encoding="utf-8", enqueue=True)

    cfg = watcher.load_config()
    w = watcher.Watcher(cfg, dry_run=args.dry_run)
    p = pricer.Pricer(cfg, w.con, dry_run=args.dry_run)
    interval = int(cfg["common"].get("poll_interval_sec", 30))
    while True:
        started = time.monotonic()
        w.run_once()             # 시트 감시 → 상태(입력중/확정/기존건 …)
        p.run_once(w.snapshots)  # 확정 행 단가 산출
        if args.once:
            return
        time.sleep(max(0.0, interval - (time.monotonic() - started)))


def cmd_import(args: argparse.Namespace) -> None:
    from pathlib import Path

    import feedback
    from config.settings import settings

    Path(settings.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    feedback.main(args.statements, args.lines, settings.DB_PATH)


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


def cmd_calibrate(args: argparse.Namespace) -> None:
    print("calibrate: TODO")


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent", description="shhone-invoice-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="시트 감시 및 발행 루프 실행")
    p_run.add_argument("--once", action="store_true", help="한 바퀴만 돌고 종료")
    p_run.add_argument("--dry-run", action="store_true", help="시트/DB에 쓰지 않고 요약만 출력")
    p_run.set_defaults(func=cmd_run)
    p_imp = sub.add_parser("import", help="경리나라 내려받기 DB 적재")
    p_imp.add_argument("statements", help="매출거래명세표 목록 (.xls)")
    p_imp.add_argument("lines", help="품목별판매현황 (.xls)")
    p_imp.set_defaults(func=cmd_import)
    sub.add_parser("blocks", help="현재 월 탭 묶음 목록 출력 (dry-run)").set_defaults(func=cmd_blocks)
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
    sub.add_parser("calibrate", help="화면 좌표/비전 보정").set_defaults(func=cmd_calibrate)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
