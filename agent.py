"""shhone-invoice-agent 진입점.

사용:
    python agent.py run         # 시트 감시 → 단가 산출 → 거래명세표 발행 루프
    python agent.py import <거래명세표목록.xls> <품목별판매현황.xls>  # 경리나라 내려받기 → DB 적재
    python agent.py calibrate   # 화면 좌표/비전 보정
"""
import argparse


def cmd_run(args: argparse.Namespace) -> None:
    import watcher

    watcher.run(once=args.once, dry_run=args.dry_run)


def cmd_import(args: argparse.Namespace) -> None:
    from pathlib import Path

    import feedback
    from config.settings import settings

    Path(settings.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    feedback.main(args.statements, args.lines, settings.DB_PATH)


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
    sub.add_parser("calibrate", help="화면 좌표/비전 보정").set_defaults(func=cmd_calibrate)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
