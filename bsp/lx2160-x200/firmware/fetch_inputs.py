#!/usr/bin/env python3
"""Materialize checksum-pinned source archives and MC/DDR firmware inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import urllib.request

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def fetch(directory):
    directory = directory.resolve()
    lock = json.loads((HERE / 'inputs-v1.json').read_text())
    for item in lock['artifacts']:
        path = (directory / item['path']).resolve()
        if not path.is_relative_to(directory):
            raise ValueError('input path escapes cache')
        if path.exists():
            if digest(path) != item['sha256']:
                raise ValueError('cached input checksum mismatch: ' + str(path))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + '.part')
            try:
                with urllib.request.urlopen(item['url'], timeout=60) as source, temporary.open('xb') as out:
                    shutil.copyfileobj(source, out)
                if digest(temporary) != item['sha256']:
                    raise ValueError('download checksum mismatch: ' + item['name'])
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        print('INPUT_PASS', item['name'], item['sha256'])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs-dir', type=Path, default=REPO / 'build/lx2160-x200/firmware-inputs')
    fetch(parser.parse_args().inputs_dir)
