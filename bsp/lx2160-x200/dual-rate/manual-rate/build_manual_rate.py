#!/usr/bin/env python3
"""Build manual rate module against actual fresh kernel outputs; no target access."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'pcie-management'))
from runtime_contract import producer, find_artifact, sha, crcs, module_check, require


def build(manifest, root, source, output, out, cross_compile, config, builder):
    data, records = producer(manifest, root)
    builder = builder or data['builder_image_id']
    actual_builder = subprocess.check_output(['podman', 'image', 'inspect', '--format', '{{.Id}}', builder], text=True).strip()
    require(actual_builder == data['builder_image_id'], 'manual module compiler differs from kernel producer')
    exports = find_artifact(records, 'Module.symvers')
    require(exports.resolve() == (output / 'Module.symvers').resolve(), 'kernel output differs from producer')
    require((source / 'Makefile').is_file(), 'kernel source missing')
    out.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(HERE / 'x200_manual_rate.c', out / 'x200_manual_rate.c')
    (out / 'Makefile').write_text('obj-m += x200_manual_rate.o\n')
    project = HERE.parents[3]
    prefix = ['podman', 'run', '--rm', '--network=none', '--security-opt', 'label=disable',
              '-v', f'{project}:{project}:ro', '-v', f'{out}:{out}:rw', builder]
    subprocess.run(prefix + ['make', '-C', str(source), f'O={output}', f'M={out}', 'ARCH=arm64',
                    'CROSS_COMPILE=' + cross_compile, 'W=1', 'modules'], check=True)
    module = out / 'x200_manual_rate.ko'
    checks = module_check(module, data['vermagic'], crcs(exports.read_text()))
    producer(manifest, root)
    receipt = {'schema_version': 1, 'build_validation': 'PASS', 'target_validation': 'pending',
               'kernel_image_sha256': data['image_sha256'], 'kernel_build_id': data['build_id'],
               'kernel_release': data['kernel_release'], 'producer_manifest_sha256': sha(manifest),
               'module_sha256': sha(module), 'module_compatibility': checks,
               'builder_image_id': actual_builder, 'source_sha256': sha(HERE / 'x200_manual_rate.c'),
               'management_interface': json.loads(config.read_text())['network']['card_interface']}
    (out / 'build-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    for name in ('port_rate.py',):
        shutil.copyfile(HERE / name, out / name)
    shutil.copyfile(HERE.parent / 'read_state.py', out / 'read_state.py')
    return receipt


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('kernel-manifest', 'artifact-root', 'source', 'kernel-output', 'out', 'config'):
        p.add_argument('--' + name, type=Path, required=True)
    p.add_argument('--cross-compile', default='aarch64-linux-gnu-')
    p.add_argument('--builder', help='defaults to exact producer container image identity')
    a = p.parse_args()
    print(json.dumps(build(a.kernel_manifest, a.artifact_root, a.source.resolve(), a.kernel_output.resolve(), a.out.resolve(), a.cross_compile, a.config, a.builder), indent=2))
