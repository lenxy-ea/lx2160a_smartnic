#!/usr/bin/env python3
"""Observe both X200 PF capability chains without authorizing activation/removal."""

import argparse
import json
import os
from pathlib import Path
import re
import struct

from collect_host import collect

# Generic PCIe capability IDs/control fields, Linux include/uapi/linux/pci_regs.h.
UNSAFE = {0x0f: ('ATS', 6, 0x8000), 0x10: ('SR-IOV', 8, 0x9),
          0x13: ('PRI', 4, 0x1), 0x1b: ('PASID', 6, 0x1)}


def read_exact(fd, offset, size):
    if offset < 0x100 or offset + size > 0x1000:
        raise ValueError('extended capability access outside config space')
    data = os.pread(fd, size, offset)
    if len(data) != size:
        raise ValueError(f'short config read at {offset:#x}: {len(data)}/{size}')
    return data


def walk(fd):
    """Read only reachable headers and unsafe capability control words."""
    entries, visited = [], set()
    offset = 0x100
    for _ in range(960):
        if not offset:
            return entries
        if offset < 0x100 or offset > 0xffc or offset & 3:
            raise ValueError(f'invalid capability offset {offset:#x}')
        if offset in visited:
            raise ValueError(f'capability cycle at {offset:#x}')
        visited.add(offset)
        header, = struct.unpack('<I', read_exact(fd, offset, 4))
        if header == 0:
            if offset == 0x100:
                return entries
            raise ValueError(f'linked empty capability at {offset:#x}')
        if header == 0xffffffff:
            raise ValueError(f'inaccessible capability at {offset:#x}')
        cap_id = header & 0xffff
        next_offset = header >> 20  # Preserve malformed low bits for validation.
        entry = {'offset': offset, 'id': cap_id, 'version': (header >> 16) & 15,
                 'next': next_offset, 'unsafe_advertisement': cap_id in UNSAFE}
        if cap_id in UNSAFE:
            name, control_offset, mask = UNSAFE[cap_id]
            control, = struct.unpack('<H', read_exact(fd, offset + control_offset, 2))
            entry.update(name=name, control=control, enabled=bool(control & mask))
        entries.append(entry)
        offset = next_offset
    if offset:
        raise ValueError('extended capability walk exceeded 960 headers')
    return entries


def proc_config_path(bdf, procfs):
    # Linux x86: drivers/pci/proc.c pci_proc_attach_device() and
    # arch/x86/include/asm/pci.h pci_proc_domain() == pci_domain_nr(bus).
    domain, bus, function = bdf.split(':')
    directory = bus if int(domain, 16) == 0 else domain + ':' + bus
    return procfs / directory / function


def check(pf0, sysfs=Path('/sys/bus/pci/devices'),
          boot_id=Path('/proc/sys/kernel/random/boot_id'),
          procfs=Path('/proc/bus/pci')):
    if not re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[01][0-9a-fA-F]\.0', pf0):
        raise ValueError('--pf0 must be an explicit domain:bus:slot.0 address')
    addresses = (pf0.lower(), pf0.lower()[:-1] + '1')
    # Validate BOTH identities/type-0 headers before any extended config access.
    identities = [collect(bdf, sysfs=sysfs, boot_id=boot_id) for bdf in addresses]
    results = []
    for identity in identities:
        result = {'identity': identity, 'capabilities': [], 'errors': []}
        config_path = proc_config_path(identity['bdf'], procfs)
        result['extended_config_source'] = str(config_path)
        try:
            # sysfs config inode size can lag cfg_size after rescan. Procfs is
            # the sole extended source, with its own identity check before walk.
            with config_path.open('rb') as stream:
                header = os.pread(stream.fileno(), 4, 0)
                if len(header) != 4:
                    raise ValueError('short proc config identity read (4 bytes required)')
                expected = (int(identity['vendor'], 16), int(identity['device'], 16))
                if struct.unpack('<HH', header) != expected:
                    raise ValueError('proc config identity differs from sysfs identity')
                result['capabilities'] = walk(stream.fileno())
        except (OSError, ValueError) as error:
            result['errors'].append(str(error))
        result['capability_gate_pass'] = not result['errors'] and not any(
            item['unsafe_advertisement'] for item in result['capabilities'])
        results.append(result)
    return {'schema_version': 1, 'observed_only': True,
            'scope': 'Capability gate only; not permission to remove or activate devices.',
            'capability_gate_pass': all(pf['capability_gate_pass'] for pf in results),
            'functions': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pf0', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = check(args.pf0)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    except (OSError, ValueError) as error:
        parser.exit(1, f'capability collection failed: {error}\n')
    if not result['capability_gate_pass']:
        parser.exit(1, 'capability gate failed; see observed JSON\n')


if __name__ == '__main__':
    main()
