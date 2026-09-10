#!/usr/bin/env python3
"""Observe the development vNTB PFs before loading either transport driver."""
import argparse
import json
import os
from pathlib import Path
import struct
from check_host_caps import walk, proc_config_path


def validate_bars(text):
    """Validate the single tracked BAR contract before host driver activation."""
    contract = json.loads(Path(__file__).with_name('vntb-contract.json').read_text())
    rows = [tuple(int(x, 16) for x in line.split()) for line in text.splitlines()[:6]]
    def require(ok, message):
        if not ok:
            raise ValueError(message)
    require(len(rows) == 6 and all(len(row) == 3 for row in rows), 'invalid BAR resource table')
    for kind in ('config', 'doorbell', 'mw'):
        index = contract[kind + '_bar']
        start, end, flags = rows[index]
        require(start > 0 and end >= start and flags & 0x200, f'BAR{index} is not assigned memory')
        require(end - start + 1 == contract[kind + '_bytes'], f'BAR{index} size differs from contract')
        if contract.get(kind + '_bar_64bit'):
            require(flags & 0x100000, f'BAR{index} must be MEM64')
    require(all(rows[i][0] == rows[i][1] == 0 for i in contract['reserved_bars']),
            'unexpected additional BARs')
    return rows


def collect(pf0, check_resources=True):
    result = {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
              'protocol': 'x200-development-vntb-v2', 'functions': []}
    for bdf, expected in [(pf0, (0x1957, 0xe200)), (pf0[:-1] + '1', (0x1957, 0x8d91))]:
        device = Path('/sys/bus/pci/devices') / bdf
        assert not (device / 'driver').exists(), 'driver already bound'
        with proc_config_path(bdf, Path('/proc/bus/pci')).open('rb') as f:
            header = os.pread(f.fileno(), 64, 0)
            assert struct.unpack_from('<HH', header) == expected, 'unexpected PF identity'
            assert not (header[14] & 0x7f), 'not a type0 header'
            caps = walk(f.fileno())
        assert not any(c['unsafe_advertisement'] for c in caps), 'unsafe capability remains'
        resources = [[int(v, 16) for v in line.split()]
                     for line in (device / 'resource').read_text().splitlines()]
        if bdf == pf0 and check_resources:
            validate_bars((device / 'resource').read_text())
            assert header[11] == 5 and header[10] == 0, 'wrong class'
        result['functions'].append({'bdf': bdf, 'header': header.hex(),
                                    'capabilities': caps, 'resources': resources})
    result['gate'] = 'PASS'
    result['resources_checked'] = check_resources
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pf0', required=True)
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    args.output.write_text(json.dumps(collect(args.pf0), indent=2) + '\n')
