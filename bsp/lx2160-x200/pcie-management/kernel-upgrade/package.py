#!/usr/bin/env python3
"""Package a current producer Image and matching card bundle for a SATA update."""
import argparse
import json
from pathlib import Path
import shutil
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runtime_contract import producer, find_artifact, sha, require


def package(manifest, root, card, expected, out):
    data, records = producer(manifest, root)
    require(sha(card / 'SHA256SUMS') == expected, 'card bundle digest mismatch')
    runtime = json.loads((card / 'runtime.json').read_text())
    require(all(runtime[key] == data[key] for key in ('build_id', 'kernel_release', 'image_sha256', 'vermagic')),
            'card bundle targets another kernel')
    # Verify package bytes before copying, and require a flat, exact inventory.
    inventory = {}
    for line in (card / 'SHA256SUMS').read_text().splitlines():
        digest, name = line.split('  ')
        require(Path(name).name == name and name not in inventory, 'unsafe/duplicate card inventory')
        require(sha(card / name) == digest and not (card / name).is_symlink(), 'card asset mismatch')
        inventory[name] = digest
    require('runtime.json' in inventory, 'missing card runtime identity')
    out.mkdir(parents=True, exist_ok=False)
    (out / 'card').mkdir()
    for name in [*inventory, 'SHA256SUMS']:
        shutil.copyfile(card / name, out / 'card' / name)
    shutil.copyfile(find_artifact(records, 'arch/arm64/boot/Image'), out / 'Image')
    shutil.copyfile(Path(__file__).with_name('install.py'), out / 'install.py')
    metadata = {'schema_version': 1, 'scope': 'SATA Image and next-boot card service',
                'image_sha256': data['image_sha256'], 'kernel_build_id': data['build_id'],
                'card_bundle_sha256': expected, 'producer_manifest_sha256': sha(manifest)}
    (out / 'upgrade.json').write_text(json.dumps(metadata, indent=2) + '\n')
    paths = [p for p in out.rglob('*') if p.is_file()]
    (out / 'SHA256SUMS').write_text(''.join(f'{sha(p)}  {p.relative_to(out)}\n' for p in sorted(paths)))
    return {'package': str(out), 'manifest_sha256': sha(out / 'SHA256SUMS')}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('kernel-manifest', 'artifact-root', 'card-bundle', 'out'):
        p.add_argument('--' + name, required=True, type=Path)
    p.add_argument('--card-sha256', required=True)
    a = p.parse_args(); print(json.dumps(package(a.kernel_manifest, a.artifact_root, a.card_bundle, a.card_sha256, a.out), indent=2))
