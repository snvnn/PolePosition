import argparse
import csv
from pathlib import Path
import sys
import tomllib

from .model import build


def main():
    parser = argparse.ArgumentParser(description='Build a reference line from the fastest reviewed train lap')
    parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--config',type=Path,default=Path(__file__).with_name('config.toml'))
    args = parser.parse_args()
    try:
        with args.config.open('rb') as stream:
            config = tomllib.load(stream)
        output = build(args.review,config)
    except (OSError,ValueError,KeyError,TypeError,OverflowError,csv.Error) as exc:
        print(f'TrackModel failed: {exc}',file=sys.stderr)
        return 1
    print(f'TrackModel: {output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
