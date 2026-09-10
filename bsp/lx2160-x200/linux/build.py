#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build the current X200 kernel from a pinned public archive and tracked inputs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE.parent))
from build_time import build_environment


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def artifact(path):
    path = Path(path)
    return {'path': str(path.relative_to(ROOT)), 'sha256': sha(path), 'bytes': path.stat().st_size}


def run(args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def capture(args):
    return subprocess.check_output([str(arg) for arg in args], text=True).strip()


def config(path):
    return dict(line.split('=', 1) for line in Path(path).read_text().splitlines()
                if line.startswith('CONFIG_') and '=' in line)


def container(image, out, environment):
    command = ['podman', 'run', '--rm', '--network=none', '--security-opt', 'label=disable',
               '-v', f'{ROOT}:{ROOT}:ro', '-v', f'{out}:{out}:rw', '-w', str(ROOT)]
    for name, value in environment.items():
        command += ['--env', f'{name}={value}']
    return command + [image]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, default=ROOT / 'build/kernel')
    parser.add_argument('--builder', default='localhost/x200-toolchain:1')
    parser.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args()
    out = args.out_dir.resolve()
    if not out.is_relative_to(ROOT / 'build') or out.exists() or args.jobs < 1:
        parser.error('output must be a new directory under build/ and jobs must be positive')
    builder_image_id = capture(['podman', 'image', 'inspect', '--format', '{{.Id}}', args.builder])
    lock = json.loads((HERE / 'source.lock.json').read_text())
    facts = json.loads((HERE.parent / 'hardware-evidence-v1.json').read_text())['facts']
    if any(name not in facts for name in lock['hardware_evidence']):
        raise ValueError('kernel source recipe cites missing hardware facts')
    input_paths = [HERE / 'build.py', HERE / 'source.lock.json', HERE / 'x200.config',
                   HERE / 'verify_abi.py', HERE.parent / 'build_time.py'] + [HERE / name for name in lock['patches']]
    initial_inputs = [artifact(path) for path in input_paths]
    out.mkdir(parents=True)
    cache = ROOT / 'build/downloads'
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / ('linux-' + lock['commit'] + '.tar.gz')
    if not archive.exists():
        temporary = archive.with_suffix('.part')
        urllib.request.urlretrieve(lock['archive_url'], temporary)
        temporary.rename(archive)
    if sha(archive) != lock['archive_sha256']:
        raise ValueError('public source archive checksum mismatch')
    source, kernel = out / 'linux', out / 'kernel-output'
    source.mkdir(); kernel.mkdir()
    run(['tar', '-xzf', archive, '--strip-components=1', '-C', source])
    for name in lock['patches']:
        run(['patch', '--batch', '--fuzz=0', '-p1', '-i', HERE / name], cwd=source)
    shutil.copyfile(HERE / 'x200.config', kernel / '.config')
    environment = build_environment({'KBUILD_BUILD_USER': 'builder', 'KBUILD_BUILD_HOST': 'x200',
                                     'KBUILD_BUILD_VERSION': '1'})
    prefix = container(builder_image_id, out, environment)
    make = ['make', '-C', source, f'O={kernel}', 'ARCH=arm64', 'CROSS_COMPILE=aarch64-linux-gnu-']
    with (out / 'build.log').open('w') as log:
        run(prefix + make + ['olddefconfig'], stdout=log, stderr=subprocess.STDOUT)
        if config(kernel / '.config') != config(HERE / 'x200.config'):
            raise ValueError('tracked final configuration changed after olddefconfig; inspect build output')
        run(prefix + make + [f'-j{args.jobs}', 'Image', 'modules'], stdout=log, stderr=subprocess.STDOUT)
        run(prefix + make + ['modules_install', f'INSTALL_MOD_PATH={out / "modules"}'],
            stdout=log, stderr=subprocess.STDOUT)
    release = (kernel / 'include/config/kernel.release').read_text().strip()
    version = re.search(r'#define UTS_VERSION "(.*)"',
                        (kernel / 'include/generated/utsversion.h').read_text()).group(1)
    module_paths = sorted(kernel.rglob('*.ko'))
    if not module_paths:
        raise ValueError('kernel did not produce modules')
    abi = json.loads(capture(prefix + ['python3', HERE / 'verify_abi.py', kernel]))
    vermagic, build_id = abi['vermagic'], abi['build_id']
    files = [kernel / name for name in ('.config', 'Module.symvers', 'vmlinux', 'arch/arm64/boot/Image')]
    if [artifact(path) for path in input_paths] != initial_inputs:
        raise ValueError('tracked kernel build inputs changed during compilation')
    manifest = {'schema_version': 1, 'build_validation': 'PASS', 'kernel_release': release,
                'kernel_version': version, 'source_commit': lock['commit'], 'build_id': build_id,
                'vermagic': vermagic, 'image_vmlinux_validation': abi['image_vmlinux_validation'], 'image_sha256': sha(files[-1]), 'build_environment': environment,
                'source_dir': str(source.relative_to(ROOT)), 'output_dir': str(kernel.relative_to(ROOT)),
                'modules_dir': str((out / 'modules').relative_to(ROOT)),
                'image': str(files[-1].relative_to(ROOT)),
                'vmlinux': str((kernel / 'vmlinux').relative_to(ROOT)),
                'module_symvers': str((kernel / 'Module.symvers').relative_to(ROOT)),
                'config': str((kernel / '.config').relative_to(ROOT)),
                'builder_image_id': builder_image_id,
                'inputs': initial_inputs,
                'build_commands': [[str(value) for value in prefix + make + targets] for targets in
                                   [['olddefconfig'], [f'-j{args.jobs}', 'Image', 'modules'],
                                    ['modules_install', f'INSTALL_MOD_PATH={out / "modules"}']]],
                'artifacts': [artifact(path) for path in files + module_paths +
                              sorted(path for path in (out / 'modules').rglob('*')
                                     if path.is_file() and not path.is_symlink())],
                'modules': {path.stem.replace('-', '_'): artifact(path) for path in module_paths}}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'PASS: {out / "manifest.json"}')


if __name__ == '__main__':
    main()
