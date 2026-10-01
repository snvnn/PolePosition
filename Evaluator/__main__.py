import argparse
import sys
from .evaluate import replay


def main():
    p=argparse.ArgumentParser(description='Frozen-checkpoint recorded-observation evaluation')
    sub=p.add_subparsers(dest='command',required=True);q=sub.add_parser('replay')
    q.add_argument('--model',required=True);q.add_argument('--dataset',required=True);q.add_argument('--split',choices=['validation','test'],required=True)
    a=p.parse_args()
    try:print(replay(a.model,a.dataset,a.split));return 0
    except (ValueError,OSError,KeyError,RuntimeError) as e:print(f'{type(e).__name__}: {e}',file=sys.stderr);return 1


if __name__=='__main__':sys.exit(main())
