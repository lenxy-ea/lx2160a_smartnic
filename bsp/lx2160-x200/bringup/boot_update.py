#!/usr/bin/env python3
"""Guarded native NOR update using a current snapshot and explicit site settings.

prepare snapshots current state. apply requires the same boot and exact original
bytes, journals every erase, verifies each sector and the entire NOR, then stages
the matching SATA boot script. No reboot or bank switching is performed. Writes
are not power-loss atomic; retain the current transaction journal for diagnosis.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import struct
import subprocess

SIZE, ERASE = 0x1000000, 0x10000
ROOT = BACKUP = None
DEVICE = None
CONFIG = {}
BOOT_PATH = None
OLD_PROFILE = NEW_PROFILE = None
OLD_COMMIT = NEW_COMMIT = None
IMAGE_SHA = BOOT_SHA = OLD_BOOT_SHA = OLD_FLASH_SHA = None
CONTRACT_SHA = None


def configure(bundle):
    global ROOT, BACKUP, OLD_PROFILE, NEW_PROFILE, OLD_COMMIT, NEW_COMMIT, DEVICE, CONFIG, BOOT_PATH
    global IMAGE_SHA, BOOT_SHA, OLD_BOOT_SHA, OLD_FLASH_SHA, CONTRACT_SHA
    ROOT = bundle.resolve(strict=True)
    BACKUP = ROOT / 'before'
    contract = (ROOT / 'deployment.json').read_bytes()
    value = json.loads(contract)
    CONFIG = value['site']
    DEVICE = CONFIG['mtd_device']
    BOOT_PATH = Path(CONFIG['boot_script'])
    require(re.fullmatch(r'/dev/mtd[0-9]+', DEVICE), 'invalid MTD character device')
    require(value['schema_version'] == 1 and value['device'] == DEVICE,
            'unexpected deployment contract/device')
    require(value['bank_label'] in ('D11', 'D12'), 'unknown independent NOR bank label')
    for stage in ('old', 'new'):
        item = value[stage]
        require(re.fullmatch(r'x200-[A-Za-z0-9_-]+', item['profile']), 'invalid native board profile')
        require(re.fullmatch(r'[0-9a-f]{40}', item['commit']), 'invalid source commit')
        for key in ('flash_sha256', 'boot_sha256'):
            require(re.fullmatch(r'[0-9a-f]{64}', item[key]), 'invalid artifact digest')
    OLD_PROFILE, NEW_PROFILE = value['old']['profile'], value['new']['profile']
    OLD_COMMIT, NEW_COMMIT = value['old']['commit'], value['new']['commit']
    OLD_FLASH_SHA, IMAGE_SHA = value['old']['flash_sha256'], value['new']['flash_sha256']
    OLD_BOOT_SHA, BOOT_SHA = value['old']['boot_sha256'], value['new']['boot_sha256']
    CONTRACT_SHA = sha(contract)


# flash-layout-v1.json; metadata is committed after all other payloads.
RANGES = ((0, 0x500000), (0xd00000, SIZE), (0x9c0000, 0x9e0000))


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def emit(event, **fields):
    print(json.dumps(dict(event=event, **fields), sort_keys=True), flush=True)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save_new(path, data, mode=0o600):
    with open(path, 'xb') as out:
        os.fchmod(out.fileno(), mode)
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    sync_dir(path.parent)
    require(path.read_bytes() == data, f'durable file verification failed: {path}')


def replace_file(path, data, mode):
    temporary = path.with_name(path.name + '.x200-native-txn')
    save_new(temporary, data, mode)
    os.replace(temporary, path)
    sync_dir(path.parent)
    require(path.read_bytes() == data, f'atomic replacement verification failed: {path}')


def manifest(image, offset):
    block = image[offset:offset + ERASE]
    magic, version, length = struct.unpack_from('<8sII', block)
    require(magic == b'X200FW1\0' and version == 1 and 0 < length < ERASE - 48,
            'invalid manifest header')
    payload = block[16:16 + length]
    require(hashlib.sha256(payload).digest() == block[16 + length:48 + length],
            'invalid manifest digest')
    return json.loads(payload)


def composition(image, profile, commit):
    require(len(image) == SIZE, 'wrong image length')
    value = manifest(image, 0x9c0000)
    require(value == manifest(image, 0x9d0000), 'manifest copies differ')
    require(value['board_profile_id'] == profile and value['build']['git_commit'] == commit,
            'unexpected firmware identity')
    require(value['image_target']['physical_designator'] in ('D11', 'D12'), 'unknown manifest bank label')
    for group in ('containers', 'components'):
        for name, item in value[group].items():
            if group == 'components' and 'container' in item:
                continue  # The complete parent container is hash-checked.
            offset = int(item['offset'], 16)
            if name == 'rcw' and item.get('scope') == 'on-media-rcw-128':
                # pack_flash.py hashes RCW words after the 8-byte PBL preamble.
                offset += 8
            require(0 <= offset < offset + item['size'] <= SIZE, 'payload outside NOR')
            require(sha(image[offset:offset + item['size']]) == item['sha256'],
                    f'payload digest mismatch: {name}')
    return value


def identity(expected_profile=None):
    dt = Path('/sys/firmware/devicetree/base')
    require(platform.machine() == 'aarch64', 'wrong target')
    require(b'rhinelab,lx2160a-x200' in (dt / 'compatible').read_bytes().split(b'\0'), 'wrong board')
    require((dt / 'rhinelab,board-profile-id').read_bytes().rstrip(b'\0').decode() == (OLD_PROFILE if expected_profile is None else expected_profile),
            'not the expected transaction profile')
    root = Path('/sys/class/mtd') / Path(DEVICE).name
    geometry = {name: (root / name).read_text().strip() for name in ('name', 'type', 'size', 'erasesize')}
    require(geometry == dict(name='20c0000.spi-0', type='nor', size=str(SIZE), erasesize=str(ERASE)),
            'MTD geometry/name changed')
    require(stat.S_ISCHR(os.stat(DEVICE).st_mode), 'not an MTD character device')
    nor = Path(CONFIG['spi_nor_sysfs'])
    chip = {name: (nor / name).read_text().strip() for name in ('jedec_id', 'partname', 'manufacturer')}
    require(chip == dict(jedec_id='ef4018', partname='w25q128', manufacturer='winbond'), 'NOR identity changed')
    mounts = {path: subprocess.check_output(['findmnt', '-n', '-o', 'SOURCE,FSTYPE', '--target', path], text=True).split()
              for path in (str(ROOT), str(BOOT_PATH.parent))}
    for path, source in ((str(ROOT), CONFIG['journal_source']), (str(BOOT_PATH.parent), CONFIG['boot_source'])):
        require(len(mounts[path]) == 2 and mounts[path][1] == 'ext4' and
                Path(mounts[path][0]).resolve() == Path(source).resolve(), 'configured persistent storage mounts changed')
    return dict(geometry=geometry, chip=chip, mounts=mounts,
                boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())


def read_flash():
    fd = os.open(DEVICE, os.O_RDONLY | os.O_SYNC)
    try:
        data = os.pread(fd, SIZE, 0)
        require(len(data) == SIZE, 'short NOR read')
        return data
    finally:
        os.close(fd)


def plan_sectors(original, image):
    require(len(original) == len(image) == SIZE, 'wrong transaction image size')
    expected = bytearray(original)
    sectors = []
    for start, end in RANGES:
        for off in range(start, end, ERASE):
            if original[off:off + ERASE] != image[off:off + ERASE]:
                sectors.append(off)
                expected[off:off + ERASE] = image[off:off + ERASE]
    return sectors, bytes(expected)


def inputs():
    image, boot = (ROOT / 'firmware.bin').read_bytes(), (ROOT / 'boot.scr').read_bytes()
    require(sha(image) == IMAGE_SHA and sha(boot) == BOOT_SHA, 'staged artifact digest changed')
    composition(image, NEW_PROFILE, NEW_COMMIT)
    return image, boot


def validated_plan(original, image):
    sectors, expected = plan_sectors(original, image)
    # Packagers must merge the live environment before pinning the image.
    # Check every protected byte, including bytes outside manifest payloads.
    require(expected == image, 'new image changes protected regions')
    composition(expected, NEW_PROFILE, NEW_COMMIT)
    return sectors, expected


def prepare():
    who = identity()
    image, _ = inputs()
    original, old_boot = read_flash(), BOOT_PATH.read_bytes()
    kernel_sha256 = sha((BOOT_PATH.parent / 'Image').read_bytes())
    composition(original, OLD_PROFILE, OLD_COMMIT)
    require(sha(original) == OLD_FLASH_SHA, 'starting NOR digest changed')
    require(sha(old_boot) == OLD_BOOT_SHA, 'starting boot script changed')
    sectors, expected = validated_plan(original, image)
    require(sectors, 'no changed firmware sectors')
    plan = dict(identity=who, contract_sha256=CONTRACT_SHA, original_sha256=sha(original), image_sha256=IMAGE_SHA,
                expected_flash_sha256=sha(expected), boot_sha256=BOOT_SHA,
                kernel_sha256=kernel_sha256,
                old_boot_sha256=sha(old_boot), boot_mode=stat.S_IMODE(os.stat('/boot/boot.scr').st_mode),
                sectors=sectors, protected_regions_preserved=True)
    BACKUP.mkdir(mode=0o700)
    sync_dir(ROOT)
    save_new(BACKUP / 'mtd0.bin', original)
    save_new(BACKUP / 'boot.scr', old_boot)
    save_new(BACKUP / 'plan.json', (json.dumps(plan, indent=2) + '\n').encode())
    require(read_flash() == original, 'NOR changed during snapshot')
    emit('PREPARED', backup=str(BACKUP), **plan)


def write_sector(fd, off, data):
    require(off % ERASE == 0 and len(data) == ERASE, 'unaligned sector')
    require(any(start <= off < end for start, end in RANGES), 'write outside approved firmware ranges')
    subprocess.run(['/usr/sbin/flash_erase', DEVICE, hex(off), '1'], check=True, timeout=30)
    require(os.pread(fd, ERASE, off) == b'\xff' * ERASE, 'erase readback failed')
    written = 0
    while written < ERASE:
        count = os.pwrite(fd, data[written:], off + written)
        require(count > 0, 'zero-length NOR write')
        written += count
    require(os.pread(fd, ERASE, off) == data, f'sector readback failed at {off:#x}')


def apply():
    plan = json.loads((BACKUP / 'plan.json').read_text())
    require(plan['contract_sha256'] == CONTRACT_SHA, 'deployment contract changed after prepare')
    require(identity() == plan['identity'], 'target identity changed after prepare')
    require(sha((BOOT_PATH.parent / 'Image').read_bytes()) == plan['kernel_sha256'],
            'kernel changed after prepare')
    image, boot = inputs()
    original, old_boot = (BACKUP / 'mtd0.bin').read_bytes(), (BACKUP / 'boot.scr').read_bytes()
    require(sha(original) == plan['original_sha256'] and sha(old_boot) == OLD_BOOT_SHA, 'snapshot digest mismatch')
    require(read_flash() == original and BOOT_PATH.read_bytes() == old_boot, 'starting state changed')
    sectors, expected = validated_plan(original, image)
    require(sectors == plan['sectors'] and sha(expected) == plan['expected_flash_sha256'], 'write plan changed')
    # Exclusive creation prevents blindly replaying an interrupted transaction.
    journal = open(BACKUP / 'apply-journal.jsonl', 'x')
    sync_dir(BACKUP)
    def record(event, **fields):
        journal.write(json.dumps(dict(event=event, **fields), sort_keys=True) + '\n')
        journal.flush()
        os.fsync(journal.fileno())
        emit(event, **fields)
    started = []
    fd = os.open(DEVICE, os.O_RDWR | os.O_SYNC)
    try:
        record('APPLY_BEGIN', sectors=[hex(off) for off in sectors])
        for off in sectors:
            record('ERASE_BEGIN', offset=hex(off))
            started.append(off)
            write_sector(fd, off, image[off:off + ERASE])
            record('SECTOR_VERIFIED', offset=hex(off), sha256=sha(image[off:off + ERASE]))
        require(read_flash() == expected, 'full NOR readback mismatch (including protected regions)')
        composition(expected, NEW_PROFILE, NEW_COMMIT)
        replace_file(BOOT_PATH, boot, plan['boot_mode'])
        os.sync()
        record('DEPLOY_PASS', flash_sha256=sha(expected), boot_sha256=sha(boot),
               sectors_written=len(sectors), protected_regions_unchanged=True, reboot_performed=False)
    except Exception as error:
        record('APPLY_FAILED', error=str(error))
        # Best-effort repair of this transaction, never a factory image.
        failures = []
        for off in started:
            try:
                write_sector(fd, off, original[off:off + ERASE])
                record('RESTORED_SECTOR', offset=hex(off))
            except Exception as failure:
                failures.append(f'{off:#x}: {failure}')
        try:
            if BOOT_PATH.read_bytes() != old_boot:
                replace_file(BOOT_PATH, old_boot, plan['boot_mode'])
            require(read_flash() == original, 'original NOR full readback mismatch')
        except Exception as failure:
            failures.append(str(failure))
        os.sync()
        record('TRANSACTION_RESTORE', passed=not failures, failures=failures)
        raise
    finally:
        os.close(fd)
        journal.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'apply'))
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args()
    manifest = (args.bundle / 'SHA256SUMS').read_bytes()
    require(sha(manifest) == args.manifest_sha256, 'boot update package digest mismatch')
    names = set()
    for line in manifest.decode().splitlines():
        digest, name = line.split('  ')
        require(Path(name).name == name and name not in names, 'invalid package inventory')
        path = args.bundle / name
        require(not path.is_symlink() and sha(path.read_bytes()) == digest, 'package asset mismatch')
        names.add(name)
    require(names == {'firmware.bin', 'boot.scr', 'deployment.json', 'boot_update.py'}, 'incomplete package inventory')
    configure(args.bundle)
    with open('/run/lock/x200-boot-update.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (prepare if args.action == 'prepare' else apply)()


if __name__ == '__main__':
    main()
