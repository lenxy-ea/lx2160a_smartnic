#!/usr/bin/env python3
"""Read X200 Linux Endpoint ownership and boot identity; never activate PCIe."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import platform


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--read-nor', action='store_true',
                        help='also hash the measured 16 MiB mtd0, read-only')
    args = parser.parse_args()
    dt = Path('/sys/firmware/devicetree/base')
    compatible = (dt / 'compatible').read_bytes().rstrip(b'\0').decode().split('\0')
    if platform.machine() != 'aarch64' or 'rhinelab,lx2160a-x200' not in compatible:
        raise SystemExit('Refusing a non-X200 target')
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    nodes = {}
    # Discover nodes through compatibility, without copying another board's addresses.
    for prop in (dt / 'soc').glob('*/compatible'):
        values = prop.read_bytes().rstrip(b'\0').decode().split('\0')
        if 'fsl,lx2160ar2-pcie-ep' not in values:
            continue
        node = prop.parent
        nodes[str(node.relative_to(dt))] = {
            name: (node / name).read_bytes().hex()
            for name in ('compatible', 'status', 'reg', 'reg-names', 'interrupts',
                         'interrupt-names', 'num-lanes', 'num-ib-windows',
                         'num-ob-windows', 'max-functions') if (node / name).exists()
        }
    platform_devices = {}
    for dev in Path('/sys/bus/platform/devices').iterdir():
        of_node = dev / 'of_node'
        if not of_node.exists() or str(of_node.resolve().relative_to(dt)) not in nodes:
            continue
        platform_devices[dev.name] = {
            'driver': (dev / 'driver').resolve().name if (dev / 'driver').exists() else None,
            'modalias': (dev / 'modalias').read_text().strip(),
        }
    config = gzip.decompress(Path('/proc/config.gz').read_bytes()).decode()
    symbols = ('PCI_LAYERSCAPE_EP', 'PCIE_DW_EP', 'PCI_ENDPOINT',
               'PCI_ENDPOINT_CONFIGFS', 'PCI_EPF_TEST', 'PCI_EPF_VNTB',
               'NTB', 'NTB_TRANSPORT', 'NTB_NETDEV')
    settings = {}
    for symbol in symbols:
        key = 'CONFIG_' + symbol
        settings[key] = next((line.split('=', 1)[1] for line in config.splitlines()
                              if line.startswith(key + '=')), 'n')
    result = {
        'schema_version': 1, 'observed_only': True, 'activation_allowed': False,
        'boot_id': boot_id, 'uname': platform.uname()._asdict(),
        'compatible': compatible, 'cmdline': Path('/proc/cmdline').read_text().strip(),
        'config_sha256': hashlib.sha256(config.encode()).hexdigest(),
        'config': settings, 'endpoint_nodes': nodes, 'platform_devices': platform_devices,
        'epc_devices': sorted(p.name for p in Path('/sys/class/pci_epc').glob('*')),
        'boot_files': {},
    }
    for name in ('/boot/Image', '/sys/firmware/fdt'):
        path = Path(name)
        if path.exists():
            result['boot_files'][name] = {'sha256': digest(path)}
    manifest = Path('/boot/x200-boot-manifest.json')
    if manifest.exists():
        result['boot_manifest'] = json.loads(manifest.read_text())
    if args.read_nor:
        mtd = Path('/sys/class/mtd/mtd0')
        # X200 NOR geometry and chip identity must match the board hardware registry.
        if ((mtd / 'size').read_text().strip() != '16777216'
                or (mtd / 'name').read_text().strip() != '20c0000.spi-0'):
            raise SystemExit('Unexpected NOR identity; refusing NOR read')
        result['nor_sha256'] = digest(Path('/dev/mtd0'))
    if Path('/proc/sys/kernel/random/boot_id').read_text().strip() != boot_id:
        raise SystemExit('Boot changed during collection')
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
