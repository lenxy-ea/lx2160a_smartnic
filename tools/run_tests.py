#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run source-only test groups in separate processes to isolate module names."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    directories = set()
    for source in (ROOT / 'bsp', ROOT / 'tools'):
        directories.update(path.parent for path in source.rglob('test_*.py'))
    failed = []
    for directory in sorted(directories):
        print(f'Test group: {directory.relative_to(ROOT)}', flush=True)
        result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(directory),
                                 '-p', 'test_*.py', '-v'], cwd=ROOT)
        if result.returncode:
            failed.append(str(directory.relative_to(ROOT)))
    if failed:
        print('Failed groups: ' + ', '.join(failed), file=sys.stderr)
    else:
        print(f'PASS: {len(directories)} source-only test groups')
    return bool(failed)


if __name__ == '__main__':
    sys.exit(main())
