#!/usr/bin/env python3
"""Validate current producer outputs and exact module ABI, without target access."""
import hashlib
import json
from pathlib import Path
import re
import subprocess


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def producer(manifest, root):
    root = Path(root).resolve()
    manifest = Path(manifest)
    data = json.loads(manifest.read_text())
    require(data.get('build_validation') == 'PASS', 'producer build is incomplete or failed')
    records = {}
    for item in data['artifacts']:
        relative = Path(item['path'])
        path = (root / relative).resolve()
        require(not relative.is_absolute() and path.is_relative_to(root), 'producer path escapes artifact root')
        require(item['path'] not in records, 'duplicate producer artifact')
        require(path.is_file() and sha(path) == item['sha256'] and path.stat().st_size == item['bytes'],
                'producer artifact integrity mismatch: ' + str(relative))
        records[item['path']] = path
    require(records, 'empty producer inventory')
    for field in ('kernel_release', 'build_id', 'vermagic'):
        require(bool(data.get(field)), 'producer identity missing: ' + field)
    require(re.fullmatch('[0-9a-f]{40}', data['build_id']), 'expected SHA1 GNU kernel build-id')
    vmlinux = find_artifact(records, 'vmlinux')
    notes = subprocess.check_output(['readelf', '-n', str(vmlinux)], text=True)
    require(re.findall(r'Build ID: ([0-9a-f]+)', notes) == [data['build_id']], 'producer build-id differs from vmlinux')
    require(data.get('image_vmlinux_validation') == 'PASS', 'producer lacks Image/vmlinux equivalence validation')
    return data, records


def find_artifact(records, suffix):
    matches = [p for name, p in records.items() if name.endswith('/' + suffix) or name == suffix]
    require(len(matches) == 1, 'expected exactly one producer artifact: ' + suffix)
    return matches[0]


def crcs(text):
    result = {}
    for line in text.splitlines():
        fields = line.split()
        require(len(fields) >= 2, 'malformed symbol CRC table')
        crc, name = int(fields[0], 16), fields[1]
        require(name not in result or result[name] == crc, 'conflicting symbol CRC: ' + name)
        result[name] = crc
    return result


def validate_abi(vermagic, versions, exports, expected):
    require(vermagic.strip() == expected.strip(), 'module vermagic mismatch')
    imports = crcs(versions)
    require(imports and all(exports.get(name) == crc for name, crc in imports.items()),
            'module symbol CRC mismatch')
    return imports


def module_check(path, expected, exports):
    info = subprocess.check_output(['modinfo', '-F', 'vermagic', str(path)], text=True).strip()
    versions = subprocess.check_output(['modprobe', '--show-modversions', str(path)], text=True)
    imports = validate_abi(info, versions, exports, expected)
    return {'sha256': sha(path), 'vermagic': info, 'imported_symbol_count': len(imports), 'crc_validation': 'PASS'}
