#!/usr/bin/env python3
"""Package a card service from a current kernel producer manifest and site config."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from runtime_contract import producer, find_artifact, crcs, module_check, sha, require


def package(manifest, root, config, out):
    data, records = producer(manifest, root)
    image = find_artifact(records, 'arch/arm64/boot/Image')
    exports = crcs(find_artifact(records, 'Module.symvers').read_text())
    require(data['image_sha256'] == sha(image), 'producer Image identity mismatch')
    network = json.loads(config.read_text())['network']
    sources = {name: (HERE if name in {'card-runtime.py', 'card-install.py', 'card.service', 'card-modules.conf'} else HERE.parent) / name
               for name in ('card-runtime.py', 'card-install.py', 'card.service', 'card-modules.conf',
                            'vntb-card-dns.py', '91-x200-vntb-dns.conf', '90-x200-vntb-unmanaged.conf')}
    modules = {}
    for module, filename in [('ntb', 'ntb.ko'), ('ntb_transport', 'ntb_transport.ko'),
                             ('ntb_netdev', 'ntb_netdev.ko'), ('pci_epf_vntb', 'pci-epf-vntb.ko')]:
        record = data['modules'][module]
        require(record['path'] in records, 'producer module absent from verified inventory')
        path = records[record['path']]
        require(path.name == filename and sha(path) == record['sha256'], 'producer module record mismatch')
        modules[module] = {'filename': filename, **module_check(path, data['vermagic'], exports)}
        sources[filename] = path
    identity = {k: data[k] for k in ('kernel_release', 'kernel_version', 'build_id', 'vermagic', 'image_sha256')}
    identity.update(modules=modules, network=network, producer_manifest_sha256=sha(manifest))
    out.mkdir(parents=True, exist_ok=False)
    card = out / 'card'; card.mkdir()
    for name, source in sources.items():
        shutil.copyfile(source, card / name)
    (card / 'runtime.json').write_text(json.dumps(identity, indent=2) + '\n')
    dns = card / '91-x200-vntb-dns.conf'
    import ipaddress
    dns.write_text(dns.read_text().replace('@DNS_SERVER@', str(ipaddress.IPv4Address(network['dns_server']))))
    unmanaged = card / '90-x200-vntb-unmanaged.conf'
    unmanaged.write_text('[device-x200-vntb]\nmatch-device=driver:ntb_netdev\nmanaged=0\n')
    names = sorted([*sources, 'runtime.json'])
    (card / 'SHA256SUMS').write_text(''.join(f'{sha(card / name)}  {name}\n' for name in names))
    digest = sha(card / 'SHA256SUMS')
    spec = importlib.util.spec_from_file_location('card_runtime', card / 'card-runtime.py')
    runtime = importlib.util.module_from_spec(spec); spec.loader.exec_module(runtime)
    runtime.verify_bundle(card, digest)
    receipt = {'schema_version': 1, 'card_bundle_sha256': digest, 'kernel_compatibility': identity,
               'producer_manifest_sha256': sha(manifest)}
    (out / 'package-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return {'package': str(out), 'card_bundle_sha256': digest}


def stage_rootfs(package, rootfs, manual_rate=None):
    """Write a new offline overlay. The caller owns rootfs integration."""
    require(not rootfs.exists() or not any(rootfs.iterdir()), 'rootfs overlay must be empty')
    card = package / 'card'; digest = sha(card / 'SHA256SUMS')
    relative = Path('usr/lib/x200-pcie') / digest
    target = rootfs / relative; target.mkdir(parents=True)
    for line in (card / 'SHA256SUMS').read_text().splitlines():
        _, name = line.split('  '); shutil.copyfile(card / name, target / name)
    shutil.copyfile(card / 'SHA256SUMS', target / 'SHA256SUMS')
    unit = rootfs / 'etc/systemd/system/x200-pcie-card.service'; unit.parent.mkdir(parents=True)
    unit.write_text((card / 'card.service').read_text().replace('@BUNDLE@', '/' + str(relative)).replace('@SHA256@', digest))
    wants = unit.parent / 'multi-user.target.wants'; wants.mkdir()
    (wants / unit.name).symlink_to('../' + unit.name)
    for name, relative in [('90-x200-vntb-unmanaged.conf', 'etc/NetworkManager/conf.d/90-x200-vntb-unmanaged.conf'),
                           ('card-modules.conf', 'etc/modprobe.d/x200-pcie.conf')]:
        path = rootfs / relative; path.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(card / name, path)
    diagnostics = rootfs / 'usr/local/lib/x200-network'
    diagnostics.mkdir(parents=True)
    shutil.copyfile(HERE.parents[1] / 'network-probe/collect.py', diagnostics / 'collect.py')
    commands = rootfs / 'usr/local/sbin'; commands.mkdir(parents=True, exist_ok=True)
    (commands / 'x200-network-collect').write_text('#!/bin/sh\nexec /usr/bin/python3 /usr/local/lib/x200-network/collect.py "$@"\n')
    (commands / 'x200-network-collect').chmod(0o755)
    if manual_rate is not None:
        receipt = json.loads((manual_rate / 'build-receipt.json').read_text())
        runtime = json.loads((card / 'runtime.json').read_text())
        require(receipt['build_validation'] == 'PASS' and receipt['kernel_image_sha256'] == runtime['image_sha256'] and
                receipt['kernel_build_id'] == runtime['build_id'], 'manual rate kernel identity mismatch')
        require(receipt['module_sha256'] == sha(manual_rate / 'x200_manual_rate.ko'), 'manual rate module changed')
        directory = rootfs / 'usr/lib/x200-manual-rate'; directory.mkdir(parents=True)
        for name in ('x200_manual_rate.ko', 'build-receipt.json', 'port_rate.py', 'read_state.py'):
            shutil.copyfile(manual_rate / name, directory / name)
        (commands / 'x200-port-rate').write_text('#!/bin/sh\nexec /usr/bin/python3 /usr/lib/x200-manual-rate/port_rate.py "$@"\n')
        (commands / 'x200-port-rate').chmod(0o755)
    artifacts = [{'path': str(p.relative_to(rootfs)), 'sha256': sha(p), 'bytes': p.stat().st_size}
                 for p in sorted(rootfs.rglob('*')) if p.is_file() and not p.is_symlink()]
    links = [{'path': str(p.relative_to(rootfs)), 'target': str(p.readlink())}
             for p in sorted(rootfs.rglob('*')) if p.is_symlink()]
    runtime = json.loads((card / 'runtime.json').read_text())
    metadata = {'schema_version': 1, 'kernel_build_id': runtime['build_id'],
                'kernel_release': runtime['kernel_release'], 'image_sha256': runtime['image_sha256'],
                'artifacts': artifacts, 'symlinks': links,
                'card_bundle_sha256': digest}
    (package / 'overlay-manifest.json').write_text(json.dumps(metadata, indent=2) + '\n')
    return metadata


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kernel-manifest', type=Path, required=True)
    p.add_argument('--artifact-root', type=Path, required=True)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--rootfs', type=Path, help='optional new root filesystem overlay')
    p.add_argument('--manual-rate', type=Path, help='ABI-verified manual-rate build to include in rootfs overlay')
    a = p.parse_args()
    print(json.dumps(package(a.kernel_manifest, a.artifact_root, a.config, a.out), indent=2))

    if a.rootfs:
        stage_rootfs(a.out, a.rootfs, a.manual_rate)
