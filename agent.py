"""shhone-invoice-agent 진입점.

사용:
    python agent.py run         # 시트 감시 → 단가 산출 → 거래명세표 발행 루프
    python agent.py import      # 경리나라 내려받기 → DB 적재
    python agent.py calibrate   # 화면 좌표/비전 보정
"""
import argparse


def cmd_run(args: argparse.Namespace) -> None:
    print("run: TODO")


def cmd_import(args: argparse.Namespace) -> None:
    print("import: TODO")


def cmd_calibrate(args: argparse.Namespace) -> None:
    print("calibrate: TODO")


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent", description="shhone-invoice-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="시트 감시 및 발행 루프 실행").set_defaults(func=cmd_run)
    sub.add_parser("import", help="경리나라 내려받기 DB 적재").set_defaults(func=cmd_import)
    sub.add_parser("calibrate", help="화면 좌표/비전 보정").set_defaults(func=cmd_calibrate)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
