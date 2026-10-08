import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description='Explicit v2 training preparation tasks')
    sub = parser.add_subparsers(dest='command', required=True)
    teacher = sub.add_parser('prepare-teacher')
    teacher.add_argument('--sources', required=True)
    teacher.add_argument('--output', default='reinforcement_runs')
    args = parser.parse_args()
    try:
        from .teacher import prepare_actions
        print(prepare_actions(args.sources, args.output))
        return 0
    except (ValueError, KeyError, OSError) as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__': sys.exit(main())
