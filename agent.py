"""shhone-invoice-agent 진입점.

사용:
    python agent.py run         # 시트 감시 → 단가 산출 → 거래명세표 발행 루프
    python agent.py import <거래명세표목록.xls> <품목별판매현황.xls>  # 경리나라 내려받기 → DB 적재
    python agent.py blocks      # 현재 월 탭의 묶음 목록 출력 (dry-run, 시트 수정 없음)
    python agent.py calibrate   # 화면 좌표/비전 보정
"""
import argparse


def cmd_run(args: argparse.Namespace) -> None:
    import time

    import pricer
    import watcher

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
    sub.add_parser("calibrate", help="화면 좌표/비전 보정").set_defaults(func=cmd_calibrate)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
