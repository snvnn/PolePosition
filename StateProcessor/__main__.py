import argparse
from pathlib import Path
import sys
import tomllib

from .prepare import prepare


def main():
    parser = argparse.ArgumentParser(description='StateProcessor input preparation and diagnostics report')
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('prepare')
    p.add_argument('--track-model',required=True)
    p.add_argument('--review',required=True)
    p.add_argument('--config',default='StateProcessor/config.toml')
    p = commands.add_parser('report')
    p.add_argument('--run',required=True)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            result = prepare(args.track_model,args.review,tomllib.loads(Path(args.config).read_text(encoding='utf-8')))
        else:
            from .report import report
            result = report(args.run)
        print(result)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
