#!/usr/bin/env python3
"""Build host vNTB drivers using explicit host headers and pinned upstream files."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import urllib.request
from runtime_contract import sha, require, crcs, module_check

HERE = Path(__file__).resolve().parent
SOURCE_HASHES = {
    'drivers/ntb/hw/epf/ntb_hw_epf.c': '8ecd0970b5dfad52955473236bcc1bd50ef48cddad017355b6244a2fc6196c76',
    'drivers/ntb/ntb_transport.c': '07ceb1ad8ae8f3de20b2a249a646105c7bf5a9f76885715e6c0cb08ad6dbe6c4',
}
ALIAS = 'pci:v00001957d0000E200sv*sd*bc05sc00i*'


def fetch_source(directory):
    """Fetch only the two pinned public source files needed by the host modules."""
    lock = json.loads((HERE.parent / 'linux/source.lock.json').read_text())
    directory.mkdir(parents=True, exist_ok=False)
    for relative, digest in SOURCE_HASHES.items():
        path = directory / relative; path.parent.mkdir(parents=True, exist_ok=True)
        url = 'https://raw.githubusercontent.com/nxp-imx/linux-imx/' + lock['commit'] + '/' + relative
        with urllib.request.urlopen(url, timeout=60) as response:
            path.write_bytes(response.read())
        require(sha(path) == digest, 'downloaded upstream source checksum mismatch')
    return directory


def build(source, kernel, native, out, jobs):
    require(jobs > 0, 'jobs must be positive')
    for relative, digest in SOURCE_HASHES.items():
        require(sha(source / relative) == digest, 'upstream source differs: ' + relative)
    release = (kernel / 'include/config/kernel.release').read_text().strip()
    baseline = crcs((kernel / 'Module.symvers').read_text())
    native_paths = {}
    for name in ('ntb', 'ntb_netdev'):
        candidates = [p for p in native.rglob(name + '.ko*') if p.is_file()]
        require(len(candidates) == 1, 'native module missing or ambiguous: ' + name)
        native_paths[name] = candidates[0]
    expected = subprocess.check_output(['modinfo', '-F', 'vermagic', str(native_paths['ntb'])], text=True).strip()
    require(expected.startswith(release + ' ') and 'modversions' in expected, 'host headers/native module mismatch')
    out.mkdir(parents=True, exist_ok=False)
    work = out / 'source'; work.mkdir()
    shutil.copyfile(source / 'drivers/ntb/hw/epf/ntb_hw_epf.c', work / 'ntb_hw_epf.c')
    (work / 'drivers/ntb').mkdir(parents=True)
    shutil.copyfile(source / 'drivers/ntb/ntb_transport.c', work / 'drivers/ntb/ntb_transport.c')
    for patch in (HERE / 'vntb-host.patch', HERE.parent / 'linux/patches/0004-ntb-transport.patch'):
        subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(patch)], cwd=work, check=True)
    shutil.move(work / 'drivers/ntb/ntb_transport.c', work / 'ntb_transport.c')
    (work / 'Makefile').write_text('obj-m += ntb_hw_epf.o ntb_transport.o\n')
    subprocess.run(['make', '-C', str(kernel), f'M={work}', f'-j{jobs}', 'W=1', 'modules'], check=True)
    aliases = subprocess.check_output(['modinfo', '-F', 'alias', str(work / 'ntb_hw_epf.ko')], text=True).splitlines()
    require(aliases == [ALIAS], 'host driver must expose only X200 vNTB alias')
    # Replacement transport exports override the stock transport for native netdev imports.
    combined = dict(baseline)
    combined.update(crcs((work / 'Module.symvers').read_text()))
    modules = {}; bundle = out / 'bundle'; (bundle / 'modules').mkdir(parents=True)
    for name, path in {**native_paths, 'ntb_hw_epf': work / 'ntb_hw_epf.ko', 'ntb_transport': work / 'ntb_transport.ko'}.items():
        checked = module_check(path, expected, combined if name == 'ntb_netdev' else baseline)
        modules[name] = {'filename': path.name, **checked}
        shutil.copyfile(path, bundle / 'modules' / path.name)
    for name in ('host_service.py', 'host.service', 'host-network.nft', 'host-nm.conf', 'host-modprobe.conf', 'host-dracut.conf', 'host-sysctl.conf'):
        shutil.copyfile(HERE / 'persistent' / name, bundle / name)
    for name in ('collect_host.py', 'check_host_caps.py', 'check_vntb_host.py', 'vntb-contract.json'):
        shutil.copyfile(HERE / name, bundle / name)
    contract = HERE / 'vntb-contract.json'
    metadata = {'schema_version': 1, 'build_validation': 'PASS', 'target_validation': 'pending',
                'kernel_release': release, 'vermagic': expected, 'modules': modules,
                'native_netdev_imports_match_replacement_transport': True,
                'contract': json.loads(contract.read_text()), 'contract_sha256': sha(contract),
                'inputs': {'.config': sha(kernel / '.config'), 'Module.symvers': sha(kernel / 'Module.symvers')},
                'source_sha256': SOURCE_HASHES}
    metadata['assets'] = {str(p.relative_to(bundle)): sha(p) for p in sorted(bundle.rglob('*')) if p.is_file()}
    (bundle / 'manifest.json').write_text(json.dumps(metadata, indent=2) + '\n')
    return {'bundle': str(bundle), 'manifest_sha256': sha(bundle / 'manifest.json')}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, help='unpatched pinned upstream Linux tree')
    p.add_argument('--kernel-headers', type=Path, required=True)
    p.add_argument('--native-modules', type=Path, required=True, help='matching host /lib/modules/RELEASE/kernel tree')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--jobs', type=int, default=4)
    a = p.parse_args()
    if a.source is None:
        a.source = fetch_source(a.out.resolve().with_name(a.out.name + '-upstream'))
    print(json.dumps(build(a.source.resolve(), a.kernel_headers.resolve(), a.native_modules.resolve(), a.out.resolve(), a.jobs)))
