#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Validate all produced module import CRCs in one builder invocation."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import hashlib


def imported_versions(text):
    return {name: int(crc, 16) for crc, name in (line.split() for line in text.splitlines())}


def verify_imports(imports, exports):
    for name, crc in imports.items():
        if exports.get(name) != crc:
            raise ValueError(f'module ABI mismatch: {name}')


def output(*command):
    return subprocess.check_output([str(x) for x in command], text=True).strip()


def main():
    kernel = Path(sys.argv[1])
    exports = {line.split()[1]: int(line.split()[0], 16)
               for line in (kernel / 'Module.symvers').read_text().splitlines()}
    modules = sorted(kernel.rglob('*.ko'))
    if not modules:
        raise ValueError('no modules produced')
    vermagic = output('modinfo', '-F', 'vermagic', modules[0])
    for module in modules:
        if output('modinfo', '-F', 'vermagic', module) != vermagic:
            raise ValueError(f'module vermagic mismatch: {module}')
        verify_imports(imported_versions(output('modprobe', '--show-modversions', module)), exports)
    notes = output('aarch64-linux-gnu-readelf', '-n', kernel / 'vmlinux')
    build_id = re.search(r'Build ID: ([0-9a-f]+)', notes).group(1)
    with tempfile.TemporaryDirectory() as temporary:
        binary = Path(temporary) / 'Image'
        subprocess.run(['aarch64-linux-gnu-objcopy', '-O', 'binary', '-R', '.note',
                        '-R', '.note.gnu.build-id', '-R', '.comment', '-S',
                        str(kernel / 'vmlinux'), str(binary)], check=True)
        if binary.read_bytes() != (kernel / 'arch/arm64/boot/Image').read_bytes():
            raise ValueError('Image differs from the binary generated from vmlinux')
    print(json.dumps({'vermagic': vermagic, 'build_id': build_id, 'modules_checked': len(modules),
                      'image_vmlinux_validation': 'PASS'}))


if __name__ == '__main__':
    main()
