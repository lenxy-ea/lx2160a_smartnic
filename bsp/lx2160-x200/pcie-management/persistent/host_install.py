#!/usr/bin/env python3
"""Stage a verified host bundle and explicit site config; never start services."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runtime_contract import sha, require


def install(bundle, expected, config, root):
    require(sha(bundle / 'manifest.json') == expected, 'host manifest digest mismatch')
    # Authenticate helper bytes via a locally built bundle; installation never imports bundle code.
    metadata = json.loads((bundle / 'manifest.json').read_text())
    require(metadata['build_validation'] == 'PASS' and metadata['native_netdev_imports_match_replacement_transport'], 'host ABI gate missing')
    for name, digest in metadata['assets'].items():
        path = bundle / name
        require(not Path(name).is_absolute() and '..' not in Path(name).parts and not path.is_symlink(), 'unsafe host asset')
        require(sha(path) == digest, 'host asset integrity mismatch: ' + name)
    for name, record in metadata['modules'].items():
        require(Path(record['filename']).name == record['filename'], 'unsafe module filename')
        require(sha(bundle / 'modules' / record['filename']) == record['sha256'], 'host module hash mismatch')
    target = root / 'usr/local/lib/x200-pcie-management'
    require(not target.exists(), 'host bundle destination must be new')
    # Explicit bundle inventory: no recursive transfer of incidental producer files.
    names = ['host_service.py', 'host.service', 'host-network.nft', 'host-nm.conf', 'host-modprobe.conf',
             'host-dracut.conf', 'host-sysctl.conf', 'collect_host.py', 'check_host_caps.py',
             'check_vntb_host.py', 'vntb-contract.json', 'manifest.json']
    target.mkdir(parents=True)
    for name in names:
        shutil.copyfile(bundle / name, target / name)
    (target / 'modules').mkdir()
    for record in metadata['modules'].values():
        shutil.copyfile(bundle / 'modules' / record['filename'], target / 'modules' / record['filename'])
    files = {'host.service': 'etc/systemd/system/x200-pcie-management-host.service',
             'host-nm.conf': 'etc/NetworkManager/conf.d/90-x200-pcie-management.conf',
             'host-modprobe.conf': 'etc/modprobe.d/90-x200-pcie-management.conf',
             'host-dracut.conf': 'etc/dracut.conf.d/90-x200-pcie-management.conf',
             'host-sysctl.conf': 'etc/sysctl.d/90-x200-pcie-management.conf'}
    for name, relative in files.items():
        destination = root / relative
        require(not destination.exists() and not destination.is_symlink(), 'refusing existing configuration: ' + relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundle / name, destination)
    cfg = root / 'etc/x200/management.json'; cfg.parent.mkdir(parents=True, exist_ok=True)
    require(not cfg.exists(), 'site configuration already exists'); shutil.copyfile(config, cfg)
    print('Staged host bundle. Review config, reload systemd and rebuild initramfs using your host distribution before explicitly enabling/starting the service.')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle', required=True, type=Path)
    p.add_argument('--manifest-sha256', required=True)
    p.add_argument('--config', required=True, type=Path)
    p.add_argument('--root', required=True, type=Path)
    a = p.parse_args(); install(a.bundle, a.manifest_sha256, a.config, a.root)
