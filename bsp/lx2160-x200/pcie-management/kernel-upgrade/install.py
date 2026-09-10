#!/usr/bin/env python3
"""Journal a SATA Image and next-boot service update. Never reboot or touch NOR.

stage requires the expected current Image SHA256 and boot ID. rollback restores
only the files in this transaction after exact current-byte checks. Retain the
journal and old bundle until the new boot has been independently validated.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def regular(path, optional=False):
    require(not path.is_symlink(), 'symlink refused: ' + str(path))
    if optional and not path.exists():
        return None
    require(path.is_file(), 'regular file required: ' + str(path))
    return path.read_bytes()


def replace(path, data):
    regular(path, optional=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    if data is None:
        path.unlink(missing_ok=True)
    else:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            tmp = Path(stream.name)
            try:
                stream.write(data); stream.flush(); os.fsync(stream.fileno()); os.fchmod(stream.fileno(), 0o644)
                tmp.replace(path)
            finally:
                tmp.unlink(missing_ok=True)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    require(regular(path, optional=True) == data, 'write readback mismatch')


def verify(package, expected):
    manifest = regular(package / 'SHA256SUMS')
    require(sha(manifest) == expected, 'package manifest digest mismatch')
    records = {}
    for line in manifest.decode().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9_./-]+)', line)
        require(match is not None, 'malformed package inventory')
        digest, name = match.groups(); path = Path(name)
        require(not path.is_absolute() and '..' not in path.parts and name not in records, 'unsafe/duplicate package path')
        require(sha(regular(package / path)) == digest, 'package artifact mismatch: ' + name)
        records[name] = digest
    require({'Image', 'upgrade.json', 'install.py', 'card/runtime.json', 'card/SHA256SUMS', 'card/card.service'} <= records.keys(), 'incomplete package')
    metadata = json.loads(regular(package / 'upgrade.json'))
    require(records['Image'] == metadata['image_sha256'], 'Image contract mismatch')
    require(records['card/SHA256SUMS'] == metadata['card_bundle_sha256'], 'card bundle contract mismatch')
    card_names = set()
    for line in regular(package / 'card/SHA256SUMS').decode().splitlines():
        digest, name = line.split('  ')
        require(Path(name).name == name and name not in card_names and records.get('card/' + name) == digest, 'card inventory mismatch')
        card_names.add(name)
    require({name[5:] for name in records if name.startswith('card/')} == card_names | {'SHA256SUMS'}, 'unexpected card files')
    runtime = json.loads(regular(package / 'card/runtime.json'))
    require(runtime['image_sha256'] == metadata['image_sha256'] and runtime['build_id'] == metadata['kernel_build_id'], 'runtime kernel mismatch')
    return metadata, card_names


def save(journal, data):
    replace(journal / 'transaction.json', (json.dumps(data, indent=2) + '\n').encode())


def target(root, name):
    path = root / name
    require(not Path(name).is_absolute() and '..' not in Path(name).parts and path.resolve().is_relative_to(root.resolve()), 'target path escapes root')
    return path


def old_bytes(journal, record):
    if record['old'] is None:
        return None
    require(re.fullmatch('[0-9a-f]{64}', record['old']), 'invalid transaction snapshot digest')
    data = regular(journal / 'snapshots' / record['old'])
    require(sha(data) == record['old'], 'transaction snapshot changed')
    return data


def stage(package, expected, root, journal, current_image_sha256, boot_id):
    metadata, names = verify(package, expected)
    require(regular(root / 'proc/sys/kernel/random/boot_id').decode().strip() == boot_id, 'boot identity changed')
    require(sha(regular(root / 'boot/Image')) == current_image_sha256, 'current Image changed')
    require(not journal.exists(), 'journal must be new')
    digest = metadata['card_bundle_sha256']
    base = 'usr/lib/x200-pcie/' + digest
    changes = {base + '/' + name: regular(package / 'card' / name) for name in names | {'SHA256SUMS'}}
    changes['boot/Image'] = regular(package / 'Image')
    unit = regular(package / 'card/card.service').decode().replace('@BUNDLE@', '/' + base).replace('@SHA256@', digest)
    changes['etc/systemd/system/x200-pcie-card.service'] = unit.encode()
    # Service ownership configs are stable; refuse arbitrary foreign replacements.
    for name, destination in [('90-x200-vntb-unmanaged.conf', 'etc/NetworkManager/conf.d/90-x200-vntb-unmanaged.conf'),
                              ('card-modules.conf', 'etc/modprobe.d/x200-pcie.conf')]:
        data = regular(package / 'card' / name)
        require(regular(target(root, destination), optional=True) in (None, data), 'foreign service configuration')
        changes[destination] = data
    snapshots = {}
    backup_content = {}
    for name, new in changes.items():
        old = regular(target(root, name), optional=True)
        if name.startswith(base + '/'):
            require(old in (None, new), 'immutable bundle collision')
        snapshots[name] = {'old': None if old is None else sha(old), 'new_sha256': sha(new)}
        if old is not None:
            backup_content[sha(old)] = old
    journal.mkdir(parents=True)
    for digest, old in backup_content.items():
        replace(journal / 'snapshots' / digest, old)
    backup_content.clear()
    data = {'schema_version': 1, 'status': 'PREPARED', 'boot_id': boot_id, 'root': str(root.resolve()),
            'package_sha256': expected, 'files': snapshots, 'completed': []}
    save(journal, data)
    try:
        for name, new in changes.items():
            require(regular(root / 'proc/sys/kernel/random/boot_id').decode().strip() == boot_id, 'boot changed during staging')
            old = old_bytes(journal, snapshots[name])
            require(regular(target(root, name), optional=True) == old, 'target changed during transaction')
            replace(target(root, name), new); data['completed'].append(name); save(journal, data)
        data['status'] = 'STAGED'; save(journal, data)
    except BaseException:
        data['status'] = 'INTERRUPTED'; save(journal, data); raise
    return data


def rollback(root, journal):
    data = json.loads(regular(journal / 'transaction.json'))
    require(data['root'] == str(root.resolve()), 'transaction root mismatch')
    # Audit every destination before the first restoration, including a write whose journal update was interrupted.
    for name, record in data['files'].items():
        current = regular(target(root, name), optional=True)
        old = old_bytes(journal, record)
        require(current == old or (current is not None and sha(current) == record['new_sha256']), 'foreign change prevents rollback: ' + name)
    for name, record in reversed(list(data['files'].items())):
        replace(target(root, name), old_bytes(journal, record))
    data['status'] = 'ROLLED_BACK'; save(journal, data); return data


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('stage', 'rollback', 'status'))
    p.add_argument('--root', type=Path, default=Path('/'))
    p.add_argument('--journal', type=Path, required=True)
    p.add_argument('--package', type=Path)
    p.add_argument('--manifest-sha256')
    p.add_argument('--current-image-sha256')
    p.add_argument('--expected-boot-id')
    a = p.parse_args()
    if a.action == 'status':
        print(regular(a.journal / 'transaction.json').decode(), end='')
    else:
        lock = a.root / 'run/lock/x200-kernel-upgrade.lock'; lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if a.action == 'stage':
                require(all((a.package, a.manifest_sha256, a.current_image_sha256, a.expected_boot_id)), 'stage requires package, manifest digest, current Image digest and boot ID')
                result = stage(a.package, a.manifest_sha256, a.root, a.journal, a.current_image_sha256, a.expected_boot_id)
            else:
                result = rollback(a.root, a.journal)
            print(json.dumps({'status': result['status'], 'journal': str(a.journal), 'rebooted': False}))
