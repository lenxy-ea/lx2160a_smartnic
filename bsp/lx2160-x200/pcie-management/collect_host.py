#!/usr/bin/env python3
"""Read PCI sysfs metadata and at most 64 config bytes; never activate a device."""

import argparse
import json
import os
from pathlib import Path
import re
import struct


def optional_text(path):
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return None


def collect(bdf, sysfs=Path('/sys/bus/pci/devices'),
            boot_id=Path('/proc/sys/kernel/random/boot_id')):
    if not re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[01][0-9a-fA-F]\.[0-7]', bdf):
        raise ValueError('BDF must be an explicit domain:bus:slot.function address')
    bdf = bdf.lower()
    device = sysfs / bdf
    vendor = int((device / 'vendor').read_text(), 16)
    product = int((device / 'device').read_text(), 16)
    expected = {'0': 0x80c0, '1': 0x8d91}
    if vendor != 0x1957 or product != expected.get(bdf[-1]):
        raise ValueError('device is not an expected X200 PF0 1957:80c0 or PF1 1957:8d91')
    with (device / 'config').open('rb') as stream:
        header = stream.read(64)
    if len(header) != 64:
        raise ValueError('incomplete PCI config header (64 bytes required)')
    if struct.unpack_from('<HH', header) != (vendor, product):
        raise ValueError('config header identity differs from sysfs identity')
    if header[14] & 0x7f:
        raise ValueError('expected a type 0 PCI config header')
    command, status = struct.unpack_from('<HH', header, 4)
    lines = (device / 'resource').read_text().splitlines()
    if len(lines) < 6:
        raise ValueError('incomplete sysfs BAR resources')
    bars = []
    upper = False
    for index, line in enumerate(lines[:6]):
        start, end, flags = (int(value, 16) for value in line.split())
        if end < start:
            raise ValueError('invalid BAR resource range')
        raw = struct.unpack_from('<I', header, 16 + 4 * index)[0]
        continuation = upper
        upper = not continuation and not (raw & 1) and (raw & 6) == 4
        size = end - start + 1 if start or end else 0
        bars.append({'bar': index, 'start': start, 'end': end,
                     'size_bytes': size, 'resource_flags': hex(flags),
                     'assigned': bool(start), 'config_raw': hex(raw),
                     'upper_half_of_64bit_bar': continuation,
                     'io': None if continuation else bool(raw & 1),
                     'memory_64bit': None if continuation else upper,
                     'prefetchable': None if continuation or raw & 1 else bool(raw & 8)})
    driver_path = device / 'driver'
    iommu_path = device / 'iommu_group'
    driver = driver_path.resolve().name if driver_path.is_symlink() else None
    sriov = optional_text(device / 'sriov_numvfs')
    reasons = ['Read-only observation does not authorize driver activation.']
    if command & 2:
        reasons.append('Memory space decoding is already enabled in the observed config header.')
    if command & 4:
        reasons.append('Bus mastering is already enabled in the observed config header.')
    if driver:
        reasons.append('Device is already owned by host driver ' + driver + '.')
    if sriov is not None and int(sriov):
        reasons.append('SR-IOV virtual functions are already enabled.')
    return {'schema_version': 1, 'observed_only': True, 'activation_allowed': False,
            'activation_denial_reasons': reasons, 'bdf': bdf,
            'vendor': f'{vendor:04x}', 'device': f'{product:04x}',
            'host': {'uname': dict(zip(('sysname', 'nodename', 'release', 'version', 'machine'), os.uname())),
                     'boot_id': boot_id.read_text().strip()},
            'config_header_hex': header.hex(), 'command': hex(command), 'status': hex(status),
            'io_space_enabled': bool(command & 1), 'memory_space_enabled': bool(command & 2),
            'bus_master_enabled': bool(command & 4), 'driver': driver,
            'iommu_group': iommu_path.resolve().name if iommu_path.is_symlink() else None,
            'sriov_numvfs': None if sriov is None else int(sriov),
            'sriov_enabled': None if sriov is None else bool(int(sriov)),
            'current_link_speed': optional_text(device / 'current_link_speed'),
            'current_link_width': optional_text(device / 'current_link_width'),
            'bars': bars}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bdf', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = collect(args.bdf)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    except (OSError, ValueError) as error:
        parser.exit(1, f'collection failed: {error}\n')


if __name__ == '__main__':
    main()
