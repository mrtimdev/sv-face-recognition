#!/usr/bin/env python3
"""Run test modules in fresh processes to isolate Qt application/global state."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=1)
    parser.add_argument('--output', type=Path, default=Path('test-results'))
    parser.add_argument('--timeout', type=int, default=120)
    args = parser.parse_args()
    if args.jobs < 1 or args.timeout < 1:
        parser.error('Jobs and timeout must be positive')
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONDONTWRITEBYTECODE='1')

    def run(path):
        with (output / (path.stem + '.log')).open('w') as log:
            try:
                result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                    str(path), '--tb=short', '-o', 'faulthandler_timeout=60',
                    '--junitxml=' + str(output / (path.stem + '.xml'))],
                    cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout)
                success = result.returncode == 0
            except subprocess.TimeoutExpired:
                log.write('\nTest module exceeded its time limit\n')
                success = False
        print(f'{"PASS" if success else "FAIL"} {path.name}', flush=True)
        return success

    paths = sorted((root / 'tests').glob('test_*.py'))
    with ThreadPoolExecutor(max_workers=args.jobs) as workers:
        results = list(workers.map(run, paths))
    print(f'{sum(results)}/{len(results)} test modules passed; reports: {output}')
    return 0 if all(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
