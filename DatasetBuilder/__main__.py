import argparse
import sys
from pathlib import Path

from .builder import build, load_config


def main():
    parser = argparse.ArgumentParser(description="Validate and extract ACC laps offline")
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.toml"))
    args = parser.parse_args()
    try:
        output = build(args.session, load_config(args.config))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Dataset build failed: {exc}", file=sys.stderr)
        return 1
    print(f"Dataset: {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
