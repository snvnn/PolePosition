import argparse
import sys
import subprocess


def main():
    p=argparse.ArgumentParser(description='Offline low-speed teacher imitation')
    sub=p.add_subparsers(dest='command',required=True)
    q=sub.add_parser('prepare');q.add_argument('--sources',required=True);q.add_argument('--config',default='ModelTrainer/config.toml')
    q=sub.add_parser('train');q.add_argument('--dataset');q.add_argument('--config',default='ModelTrainer/config.toml');q.add_argument('--resume');q.add_argument('--smoke',action='store_true')
    q=sub.add_parser('export');q.add_argument('--run',required=True)
    a=p.parse_args()
    try:
        if a.command=='prepare':
            from .dataset import prepare
            result=prepare(a.sources,a.config)
        elif a.command=='train':
            from .training import train
            if bool(a.dataset)==bool(a.resume):raise ValueError('Specify exactly one of --dataset or --resume')
            result=train(a.dataset,a.config,a.resume,a.smoke)
        else:
            from .training import export
            result=export(a.run)
        print(result)
        from .common import read
        return 0 if read(result/'manifest.json').get('complete') is True else 1
    except (ValueError,OSError,KeyError,RuntimeError,subprocess.CalledProcessError) as e:
        print(f'{type(e).__name__}: {e}',file=sys.stderr);return 1


if __name__=='__main__':sys.exit(main())
