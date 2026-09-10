#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build Debian userspace and a SATA disk file, without accessing target devices."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE.parent / 'linux'))
from build import artifact, sha


def run(command, **kwargs):
    return subprocess.run([str(x) for x in command], check=True, **kwargs)


def capture(command):
    return subprocess.check_output([str(x) for x in command], text=True).strip()


def read_manifest(path):
    data = json.loads(path.read_text())
    if data.get('build_validation') != 'PASS':
        raise ValueError(f'producer did not pass: {path}')
    for item in data['artifacts']:
        file = ROOT / item['path']
        if not file.resolve().is_relative_to(ROOT / 'build'):
            raise ValueError('producer artifact escapes build directory')
        if file.stat().st_size != item['bytes'] or sha(file) != item['sha256']:
            raise ValueError(f'producer artifact changed: {file}')
    return data


def read_kernel_manifest(path):
    data = read_manifest(path)
    inventory = {item['path']: item for item in data['artifacts']}
    for key in ('image', 'vmlinux', 'module_symvers', 'config'):
        if data.get(key) not in inventory:
            raise ValueError(f'kernel selected {key} is absent from producer inventory')
    if inventory[data['image']]['sha256'] != data['image_sha256']:
        raise ValueError('selected kernel Image differs from producer image identity')
    if data.get('image_vmlinux_validation') != 'PASS':
        raise ValueError('kernel producer did not verify Image/vmlinux equivalence')
    return data


def prefix(args, network=False):
    result = ['podman', 'run', '--rm', '--security-opt', 'label=disable',
              '--cap-add', 'MKNOD',
              '-v', f'{ROOT}:{ROOT}:ro', '-v', f'{args.out_dir}:{args.out_dir}:rw',
              '-v', f'{ROOT / "build/downloads/debian"}:{ROOT / "build/downloads/debian"}:rw',
              '-w', str(ROOT)]
    if not network:
        result += ['--network=none']
    return result + [args.builder]


@contextmanager
def foreign_execution(args):
    """Temporarily register the pinned static QEMU interpreter on the build host."""
    registry = Path('/proc/sys/fs/binfmt_misc')
    if os.geteuid() != 0 or not (registry / 'register').exists():
        raise ValueError('rootful Linux with mounted binfmt_misc is required for Debian builds')
    emulator = args.out_dir / 'host-tools/aarch64-binfmt-P'
    emulator.parent.mkdir(parents=True, exist_ok=True)
    run(prefix(args) + ['cp', '-L', '/usr/libexec/qemu-binfmt/aarch64-binfmt-P', emulator])
    specification = capture(prefix(args) + ['cat', '/usr/lib/binfmt.d/qemu-aarch64.conf']).split(':')
    name = 'x200-aarch64-' + str(os.getpid())
    specification[1], specification[-2] = name, str(emulator)
    (registry / 'register').write_text(':'.join(specification) + '\n')
    try:
        yield
    finally:
        handler = registry / name
        if handler.exists():
            handler.write_text('-1\n')


def foreign(args, root, command, network=False):
    invocation = prefix(args, network=network)
    # Read-only proc provides normal package postinst semantics; no host device
    # tree is shared and package services are suppressed by policy-rc.d.
    invocation[-1:-1] = ['-v', f'/proc:{root / "proc"}:ro']
    return invocation + ['env', 'DEBOOTSTRAP_DIR=/debootstrap', 'DEBIAN_FRONTEND=noninteractive',
                         'chroot', str(root)] + command


def put(root, path, text, mode=0o644):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    target.chmod(mode)


def link(root, path, target):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.unlink(missing_ok=True)
    file.symlink_to(target)


def packages(status):
    records = []
    for paragraph in status.read_text().split('\n\n'):
        fields = dict(line.split(': ', 1) for line in paragraph.splitlines()
                      if ': ' in line and not line[0].isspace())
        if fields.get('Status') == 'install ok installed':
            records.append({key.lower(): fields[key] for key in ('Package', 'Version', 'Architecture')})
    return sorted(records, key=lambda item: item['package'])


def verify_overlay(root, manifest_path):
    root = root.resolve()
    manifest = json.loads(manifest_path.read_text())
    files = {item['path']: item for item in manifest['artifacts']}
    links = {item['path']: item['target'] for item in manifest.get('symlinks', [])}
    actual_files = {str(path.relative_to(root)) for path in root.rglob('*')
                    if path.is_file() and not path.is_symlink()}
    actual_links = {str(path.relative_to(root)): os.readlink(path) for path in root.rglob('*')
                    if path.is_symlink()}
    if actual_files != set(files) or actual_links != links:
        raise ValueError('runtime overlay contains missing or unlisted files/symlinks')
    for name, item in files.items():
        file = root / name
        if not file.resolve().is_relative_to(root) or sha(file) != item['sha256'] or file.stat().st_size != item['bytes']:
            raise ValueError('runtime overlay integrity check failed')
    for name, target in links.items():
        link_path = root / name
        destination = root / target.lstrip('/') if target.startswith('/') else link_path.parent / target
        if not destination.resolve().is_relative_to(root):
            raise ValueError('runtime overlay symlink escapes its filesystem root')
    return manifest


def verify_installed_modules(kernel):
    directory = ROOT / kernel['modules_dir']
    recorded = {item['path']: item for item in kernel['artifacts']
                if Path(item['path']).is_relative_to(Path(kernel['modules_dir']))}
    actual = {str(path.relative_to(ROOT)) for path in directory.rglob('*')
              if path.is_file() and not path.is_symlink()}
    if actual != set(recorded):
        raise ValueError('installed kernel modules differ from producer file set')
    expected = {item['sha256'] for item in kernel['modules'].values()}
    if {sha(path) for path in directory.rglob('*.ko')} != expected:
        raise ValueError('installed modules differ from compiled kernel module bytes')


def build_bootstrap(args):
    root = args.out_dir / 'debian-base'
    if root.exists():
        raise ValueError('Debian base output already exists; use a fresh --out-dir')
    policy = json.loads((HERE / 'rootfs.json').read_text())
    include = [line.strip() for line in (HERE / 'packages.txt').read_text().splitlines()
               if line.strip() and not line.startswith('#')]
    with (args.out_dir / 'bootstrap-build.log').open('w') as log:
        run(prefix(args, network=True) + ['debootstrap', '--foreign', '--arch=arm64',
            '--cache-dir=' + str(ROOT / 'build/downloads/debian'),
            '--variant=minbase', policy['suite'], root,
            args.mirror or policy['mirror']], stdout=log, stderr=subprocess.STDOUT)
        put(root, 'usr/sbin/policy-rc.d', '#!/bin/sh\nexit 101\n', 0o755)
        run(foreign(args, root, ['/bin/sh', '/debootstrap/debootstrap', '--second-stage']),
            stdout=log, stderr=subprocess.STDOUT)
        run(foreign(args, root, ['/usr/bin/apt-get', 'update'], network=True), stdout=log, stderr=subprocess.STDOUT)
        run(foreign(args, root, ['/usr/bin/apt-get', '--no-install-recommends', '-y', 'install', *include], network=True),
            stdout=log, stderr=subprocess.STDOUT)
    resolved = packages(root / 'var/lib/dpkg/status')
    if not resolved or any(not any(row['package'] == name for row in resolved) for name in include):
        raise ValueError('requested Debian packages missing')
    (args.out_dir / 'packages.json').write_text(json.dumps(resolved, indent=2) + '\n')
    shutil.rmtree(root / 'debootstrap', ignore_errors=True)
    for file in (root / 'var/cache/apt/archives').glob('*.deb'):
        file.unlink()
    archive = args.out_dir / 'debian-base.tar.zst'
    run(prefix(args) + ['tar', '--zstd', '--numeric-owner', '--xattrs', '--acls', '-cpf', archive, '-C', root, '.'])
    manifest = {'schema_version': 1, 'build_validation': 'PASS', 'policy': policy,
                'mirror': args.mirror or policy['mirror'],
                'inputs': [artifact(HERE / name) for name in ('packages.txt', 'rootfs.json', 'build.py')],
                'artifacts': [artifact(archive), artifact(args.out_dir / 'packages.json')]}
    (args.out_dir / 'bootstrap-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'PASS: {args.out_dir / "bootstrap-manifest.json"}')


def build_restool(args, root):
    sdk = json.loads((HERE.parent / 'flexbuild/sdk-source-lock.json').read_text())
    pin = sdk['components']['restool']
    source = args.out_dir / 'restool-source'
    run(['git', 'clone', '--depth=1', '--branch', pin['upstream_ref'], pin['url'], source])
    if capture(['git', '-C', source, 'rev-parse', 'HEAD']) != pin['commit']:
        raise ValueError('public restool tag differs from pinned commit')
    run(prefix(args) + ['make', '-C', source, '-j2', 'CROSS_COMPILE=aarch64-linux-gnu-',
                        'VERSION_COMMIT=' + pin['upstream_ref']])
    target = root / 'usr/local/sbin/restool'
    target.parent.mkdir(parents=True, exist_ok=True)
    run(prefix(args) + ['aarch64-linux-gnu-strip', '--strip-unneeded', '-o', target, source / 'restool'])
    header = capture(prefix(args) + ['aarch64-linux-gnu-readelf', '-h', target])
    if 'AArch64' not in header:
        raise ValueError('restool is not an AArch64 binary')
    version = capture(foreign(args, root, ['/usr/local/sbin/restool', '--version']))
    return {'source_commit': pin['commit'], 'version': version, 'sha256': sha(target)}


def build_rootfs(args):
    kernel = read_kernel_manifest(args.kernel_manifest)
    base = read_manifest(args.out_dir / 'bootstrap-manifest.json')
    root = args.out_dir / 'rootfs'
    if root.exists():
        raise ValueError('rootfs output already exists; use a fresh --out-dir')
    root.mkdir()
    archive = ROOT / base['artifacts'][0]['path']
    run(prefix(args) + ['tar', '--zstd', '--numeric-owner', '--xattrs', '--acls', '-xpf', archive, '-C', root])
    policy = json.loads((HERE / 'rootfs.json').read_text())
    with (args.out_dir / 'rootfs-build.log').open('w') as log:
        # Accounts and key material are site input, never embedded project secrets.
        run(foreign(args, root, ['/usr/sbin/usermod', '--lock', 'root']), stdout=log, stderr=subprocess.STDOUT)
        if args.authorized_key:
            key = args.authorized_key.read_text().strip()
            if not re.fullmatch(r'(ssh-ed25519|ssh-rsa|ecdsa-sha2-\S+) [A-Za-z0-9+/=]+(?: [^\n]*)?', key):
                raise ValueError('expected one OpenSSH public key')
            run(foreign(args, root, ['/usr/sbin/useradd', '--create-home', '--shell', '/bin/bash',
                                    '--password', '*', '--groups', 'sudo', args.user]),
                stdout=log, stderr=subprocess.STDOUT)
            put(root, f'home/{args.user}/.ssh/authorized_keys', key + '\n', 0o600)
            (root / f'home/{args.user}/.ssh').chmod(0o700)
            run(foreign(args, root, ['/bin/chown', '-R', args.user + ':' + args.user,
                                    f'/home/{args.user}/.ssh']), stdout=log, stderr=subprocess.STDOUT)
            put(root, f'etc/sudoers.d/{args.user}', f'{args.user} ALL=(ALL:ALL) NOPASSWD: ALL\n', 0o440)
    put(root, 'etc/hostname', policy['hostname'] + '\n')
    link(root, 'etc/resolv.conf', '/run/NetworkManager/resolv.conf')
    put(root, 'etc/hosts', '127.0.0.1 localhost\n127.0.1.1 x200\n::1 localhost ip6-localhost\n')
    put(root, 'etc/fstab', 'LABEL=X200-ROOT / ext4 defaults,noatime 0 1\nLABEL=X200-BOOT /boot ext4 defaults,noatime 0 2\n')
    put(root, 'etc/apt/sources.list', f'deb {args.mirror or policy["mirror"]} trixie main\n'
        'deb https://security.debian.org/debian-security trixie-security main\n')
    put(root, 'etc/ssh/sshd_config.d/10-x200.conf', 'PermitRootLogin no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitEmptyPasswords no\n')
    put(root, 'etc/NetworkManager/conf.d/10-x200-explicit-configuration.conf', '[main]\nno-auto-default=*\n')
    put(root, 'etc/machine-id', '')
    for key in (root / 'etc/ssh').glob('ssh_host_*'):
        key.unlink()
    dbus_id = root / 'var/lib/dbus/machine-id'
    dbus_id.unlink(missing_ok=True)
    link(root, 'var/lib/dbus/machine-id', '/etc/machine-id')
    shutil.copyfile(HERE / 'sshd-keygen.service', root / 'etc/systemd/system/sshd-keygen.service')
    link(root, 'etc/systemd/system/ssh.service.wants/sshd-keygen.service', '../sshd-keygen.service')
    link(root, 'etc/systemd/system/multi-user.target.wants/ssh.service', '/usr/lib/systemd/system/ssh.service')
    link(root, 'etc/systemd/system/multi-user.target.wants/NetworkManager.service', '/usr/lib/systemd/system/NetworkManager.service')
    link(root, 'etc/systemd/system/default.target', '/usr/lib/systemd/system/multi-user.target')
    verify_installed_modules(kernel)
    shutil.copytree(ROOT / kernel['modules_dir'] / 'lib/modules', root / 'usr/lib/modules', dirs_exist_ok=True)
    for module_link in ('build', 'source'):
        (root / 'usr/lib/modules' / kernel['kernel_release'] / module_link).unlink(missing_ok=True)
    run([sys.executable, HERE / 'boot-banner/install.py', '--root', root])
    diagnostics = root / 'usr/local/sbin/x200-network-diagnostics'
    diagnostics.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / 'x200-network-diagnostics', diagnostics)
    diagnostics.chmod(0o755)
    if args.runtime_root:
        overlay = verify_overlay(args.runtime_root, args.runtime_manifest)
        expected_identity = {'kernel_build_id': kernel['build_id'], 'kernel_release': kernel['kernel_release'],
                             'image_sha256': kernel['image_sha256']}
        if any(overlay.get(name) != value for name, value in expected_identity.items()):
            raise ValueError('runtime overlay was built for a different kernel producer')
        shutil.copytree(args.runtime_root, root, dirs_exist_ok=True, symlinks=True)
    run(prefix(args) + ['depmod', '-b', root, kernel['kernel_release']])
    restool = build_restool(args, root)
    (root / 'usr/sbin/policy-rc.d').unlink(missing_ok=True)
    for file in (root / 'var/cache/apt/archives').glob('*.deb'):
        file.unlink()
    for file in (root / 'var/log').rglob('*'):
        if file.is_file() and not file.is_symlink():
            file.write_bytes(b'')
    archive = args.out_dir / 'x200-debian13-rootfs.tar.zst'
    run(prefix(args) + ['tar', '--zstd', '--numeric-owner', '--xattrs', '--acls', '-cpf', archive, '-C', root, '.'])
    manifest = {'schema_version': 1, 'build_validation': 'PASS', 'policy': policy,
                'restool': restool, 'kernel_build_id': kernel['build_id'], 'kernel_release': kernel['kernel_release'],
                'kernel_manifest_sha256': sha(args.kernel_manifest), 'rootfs_dir': str(root.relative_to(ROOT)),
                'operator_account_provisioned': bool(args.authorized_key),
                'artifacts': [artifact(archive), artifact(args.out_dir / 'packages.json')]}
    (args.out_dir / 'rootfs-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'PASS: {args.out_dir / "rootfs-manifest.json"}')


def build_sata(args):
    geometry = json.loads((HERE.parent / 'hardware-evidence-v1.json').read_text())['facts']['x200-sata-image-geometry']['value']
    if geometry != {'sector_bytes': 512, 'disk_sectors': 62533296, 'boot_partition_bytes': 1073741824, 'partition_table': 'GPT'}:
        raise ValueError('SATA image recipe differs from current hardware contract')
    kernel = read_kernel_manifest(args.kernel_manifest)
    rootfs_manifest = args.out_dir / 'rootfs-manifest.json'
    rootfs = read_manifest(rootfs_manifest)
    if rootfs['kernel_manifest_sha256'] != sha(args.kernel_manifest):
        raise ValueError('rootfs and SATA kernel producer differ')
    image = args.out_dir / 'x200-debian13-sata.img'
    if image.exists() or args.sectors < 4194304:
        raise ValueError('SATA output must be new and size at least 2 GiB')
    sata_root = args.out_dir / 'sata-rootfs'
    sata_root.mkdir()
    archive = ROOT / rootfs['artifacts'][0]['path']
    run(prefix(args) + ['tar', '--zstd', '--numeric-owner', '--xattrs', '--acls', '-xpf', archive, '-C', sata_root])
    boot = args.out_dir / 'boot'
    boot.mkdir()
    shutil.copyfile(ROOT / kernel['image'], boot / 'Image')
    shutil.copyfile(args.kernel_manifest, boot / 'x200-kernel-manifest.json')
    run(prefix(args) + ['mkimage', '-A', 'arm64', '-O', 'linux', '-T', 'script', '-C', 'none',
                        '-n', 'X200 SATA boot', '-d', HERE / 'x200-sata-boot.cmd', boot / 'boot.scr'])
    boot_uuid, root_uuid = str(uuid.uuid4()), str(uuid.uuid4())
    with image.open('wb') as stream:
        stream.truncate(args.sectors * 512)
    run(prefix(args) + ['sgdisk', '--clear', '--new=1:2048:+1G', '--typecode=1:8300',
        '--change-name=1:X200-BOOT', '--partition-guid=1:' + boot_uuid,
        '--new=2:0:0', '--typecode=2:8300', '--change-name=2:X200-ROOT',
        '--partition-guid=2:' + root_uuid, image])
    # Assemble filesystems into a regular sparse file; no loop device or mounts.
    part_records = []
    for number, directory, label in [(1, boot, 'X200-BOOT'), (2, sata_root, 'X200-ROOT')]:
        info = capture(prefix(args) + ['sgdisk', f'--info={number}', image])
        start = int(re.search(r'First sector: (\d+)', info).group(1))
        end = int(re.search(r'Last sector: (\d+)', info).group(1))
        partition = args.out_dir / f'partition-{number}.ext4'
        with partition.open('wb') as stream:
            stream.truncate((end - start + 1) * 512)
        run(prefix(args) + ['mkfs.ext4', '-F', '-L', label, '-d', directory, partition])
        run(prefix(args) + ['e2fsck', '-fn', partition])
        if start % 2048:
            raise ValueError('partition start not MiB aligned')
        run(prefix(args) + ['dd', f'if={partition}', f'of={image}', 'bs=1M',
                            f'seek={start // 2048}', 'conv=notrunc,sparse', 'status=none'])
        partition.unlink()
        part_records.append({'number': number, 'label': label, 'first_sector': start,
                             'last_sector': end, 'partuuid': boot_uuid if number == 1 else root_uuid})
    run(prefix(args) + ['sgdisk', '--verify', image])
    compressed = image.with_suffix('.img.zst')
    run(prefix(args) + ['zstd', '-T2', '-3', '-f', image, '-o', compressed])
    run(prefix(args) + ['zstd', '--test', compressed])
    manifest = {'schema_version': 1, 'build_validation': 'PASS', 'kernel_build_id': kernel['build_id'],
                'kernel_manifest_sha256': sha(args.kernel_manifest), 'rootfs_manifest_sha256': sha(rootfs_manifest),
                'logical_sector_bytes': 512, 'logical_sectors': args.sectors, 'hardware_evidence': ['x200-sata-image-geometry'], 'partitions': part_records,
                'artifacts': [artifact(image), artifact(compressed)], 'hardware_writes': False}
    (args.out_dir / 'sata-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'PASS: {args.out_dir / "sata-manifest.json"}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['bootstrap', 'rootfs', 'sata'])
    parser.add_argument('--kernel-manifest', type=Path, default=ROOT / 'build/kernel/manifest.json')
    parser.add_argument('--out-dir', type=Path, default=ROOT / 'build/os')
    parser.add_argument('--builder', default='localhost/x200-toolchain:1')
    parser.add_argument('--mirror', help='public Debian mirror URL')
    parser.add_argument('--authorized-key', type=Path, help='one operator SSH public key; absent means all accounts remain locked')
    parser.add_argument('--user', default='operator')
    parser.add_argument('--runtime-root', type=Path)
    parser.add_argument('--runtime-manifest', type=Path)
    parser.add_argument('--sectors', type=int, default=62533296, help='512-byte sectors in disk file; default is the X200 SATA contract')
    args = parser.parse_args()
    args.builder = capture(['podman', 'image', 'inspect', '--format', '{{.Id}}', args.builder])
    args.out_dir = args.out_dir.resolve()
    if not args.out_dir.is_relative_to(ROOT / 'build') or not re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', args.user):
        parser.error('output must be under build/ and operator name must be valid')
    if bool(args.runtime_root) != bool(args.runtime_manifest):
        parser.error('--runtime-root and --runtime-manifest must be supplied together')
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (ROOT / 'build/downloads/debian').mkdir(parents=True, exist_ok=True)
    if args.operation == 'sata':
        build_sata(args)
    else:
        with foreign_execution(args):
            if args.operation == 'bootstrap':
                build_bootstrap(args)
            else:
                build_rootfs(args)



if __name__ == '__main__':
    main()
