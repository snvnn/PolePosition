import argparse
from pathlib import Path
from .setup import backup, restore


def main():
    parser = argparse.ArgumentParser(description="ACC vehicle input setup and reports")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("backup")
    p.add_argument("--acc-root", type=Path, default=Path.home() / "Documents/Assetto Corsa Competizione")
    p.add_argument("--output", default="input_profiles")
    p = sub.add_parser("restore")
    p.add_argument("--backup", required=True)
    p.add_argument("--game-closed", action="store_true")
    p = sub.add_parser("report")
    p.add_argument("--run", required=True)
    p = sub.add_parser("calibration-draft")
    p.add_argument("--acc-root", type=Path, default=Path.home() / "Documents/Assetto Corsa Competizione")
    p.add_argument("--backup", required=True)
    p.add_argument("--prepared", required=True)
    p.add_argument("--driver", default="C:/Windows/System32/drivers/vjoy.sys")
    p = sub.add_parser("calibration-finalize")
    p.add_argument("--draft", required=True)
    args = parser.parse_args()
    try:
        if args.command == "backup":
            print(backup(args.acc_root, args.output))
        elif args.command == "restore":
            print(restore(args.backup, game_closed=args.game_closed))
        elif args.command == "report":
            from .report import report
            print(report(Path(args.run)))
        elif args.command == "calibration-draft":
            from .calibration import draft
            print(draft(args.acc_root, args.backup, args.prepared, args.driver))
        else:
            from .calibration import finalize
            print(finalize(args.draft))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Vehicle input: {error}\n")


if __name__ == "__main__":
    main()
