#!/usr/bin/env python3
"""Read-only check of the X200 public UID, MC endpoints and Linux physical MACs.

Run on X200 Linux. Reads exactly the two public FUID words, never other fuses.
The Python hash calculation is independent of the U-Boot C implementation.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys


def main():
    fd = os.open('/sys/bus/nvmem/devices/fsl-sfp0/nvmem', os.O_RDONLY)
    try:
        first = os.pread(fd, 8, 0x1c)
        second = os.pread(fd, 8, 0x1c)
    finally:
        os.close(fd)
    if len(first) != 8 or first != second or first in (bytes(8), b'\xff' * 8):
        raise ValueError('invalid or unstable public UID')
    canonical = struct.pack('>II', *struct.unpack('<II', first))
    base = bytearray(hashlib.sha256(b'x200-mac-v1\0' + canonical).digest()[:6])
    base[0] = (base[0] & 0xfc) | 2
    base[5] &= 0xf8
    expected = {}
    for dpmac in range(3, 7):
        mac = base.copy()
        mac[5] |= dpmac - 3
        expected[dpmac] = ':'.join(f'{n:02x}' for n in mac)
    ports = []
    for path in sorted(Path('/sys/class/net').iterdir()):
        device = (path / 'device').resolve()
        if not re.fullmatch(r'dpni\.\d+', device.name):
            continue
        info = subprocess.check_output(['restool', 'dpni', 'info', device.name], text=True)
        endpoint = re.search(r'^endpoint: dpmac\.(\d+),', info, re.M)
        mc_mac = re.search(r'^mac address: ([0-9a-f:]+)$', info, re.M)
        if not endpoint or not mc_mac:
            raise ValueError('MC endpoint/MAC missing for ' + device.name)
        dpmac = int(endpoint[1])
        linux_mac = (path / 'address').read_text().strip()
        if dpmac not in expected or linux_mac != expected[dpmac] or mc_mac[1] != expected[dpmac]:
            raise ValueError(f'identity mismatch: {path.name} DPMAC{dpmac} Linux={linux_mac} MC={mc_mac[1]}')
        ports.append(dict(interface=path.name, dpni=device.name, dpmac=dpmac,
                          mac=linux_mac, mc_mac=mc_mac[1], carrier=(path / 'carrier').read_text().strip()))
    if sorted(p['dpmac'] for p in ports) != [3, 4, 5, 6]:
        raise ValueError('expected exactly one interface for each DPMAC3–6')
    print(json.dumps(dict(status='PASS', contract='x200-soc-derived-mac-v1',
                          boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                          uid=canonical.hex(), uid_repeat_equal=True, ports=ports), indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
