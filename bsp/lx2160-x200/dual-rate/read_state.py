#!/usr/bin/env python3
"""Read-only dual-rate diagnostics; no MDIO or configuration writes.

Board binding: hardware-evidence-v1.json facts x200-dual-rate-native18-design
and x200-datapath-port-order. Register layouts follow generic LX2160A SerDes.
"""
import argparse
import ctypes as C
import json
import os
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys

PROFILE = 'x200-s1_12-s2_05-s3_02-v1'
PORTS = {'eth0': ('dpni.3', 4, 6, 10000), 'eth1': ('dpni.2', 3, 7, 10000),
         'eth2': ('dpni.1', 5, 5, 25000), 'eth3': ('dpni.0', 6, 4, 25000)}
DT = Path('/sys/firmware/devicetree/base')


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def command(*args):
    proc = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=10,
                          env={**os.environ, 'LC_ALL': 'C'})
    return {'returncode': proc.returncode, 'raw': proc.stdout}


def read_mmio(base, length, offsets):
    libc = C.CDLL(None, use_errno=True)
    libc.mmap.argtypes = [C.c_void_p, C.c_size_t, C.c_int, C.c_int, C.c_int, C.c_int64]
    libc.mmap.restype = C.c_void_p
    libc.munmap.argtypes = [C.c_void_p, C.c_size_t]
    fd = os.open('/dev/mem', os.O_RDONLY | os.O_SYNC)
    try:
        ptr = libc.mmap(None, length, 1, 1, fd, base)  # PROT_READ, MAP_SHARED
    finally:
        os.close(fd)
    require(ptr != C.c_void_p(-1).value, 'read-only mmap failed')
    try:
        return {f'{off:04x}': C.c_uint32.from_address(ptr + off).value for off in offsets}
    finally:
        require(libc.munmap(ptr, length) == 0, 'munmap failed')


def check_identity(state, boot, profile):
    require(state['boot_id'] == boot, 'unexpected boot ID')
    require(state['machine'] == 'aarch64' and state['kernel'] == '6.12.49', 'wrong target/kernel')
    require('rhinelab,lx2160a-x200' in state['compatible'], 'wrong board')
    require(state['profile'] == profile, 'wrong profile')
    require(state['serdes_reg'] == [0x1ea0000, 0x1e30], 'wrong SerDes aperture')


def expected_rates(rates=None):
    result = {port: data[3] for port, data in PORTS.items()}
    for port, rate in (rates or {}).items():
        require(port in PORTS, f'unknown port: {port}')
        require(type(rate) is int and rate in (10000, 25000), f'{port}: unsupported rate')
        result[port] = rate
    return result


def parse_rates(values):
    rates = {}
    for value in values:
        require(re.fullmatch(r'eth[0-3]=(10000|25000)', value), 'rate must be ethN=10000 or ethN=25000')
        port, rate = value.split('=')
        require(port not in rates, f'duplicate rate: {port}')
        rates[port] = int(rate)
    return expected_rates(rates)


def check_clocks(raw, rates=None):
    rates = expected_rates(rates)
    def rd(off):
        value = raw[f'{off:04x}']
        require(value != 0xffffffff, 'inaccessible SerDes register')
        return value
    require(rd(8) == 0, 'common test mode active')
    for base, clock in ((0x400, 0x16000000), (0x500, 0x06000000)):
        require(rd(base) & 0x01800000 == 0x00800000, 'PLL disabled/unlocked')
        require(rd(base+4) & 0x001f0000 == 0x00040000, 'wrong PLL reference')
        require(rd(base+8) & 0x1f000000 == clock, 'wrong PLL clock')
    for port, (_, mac, lane, _) in PORTS.items():
        base = 0x800 + lane * 0x100
        protocol, rate = (0x52, 0x10000000) if rates[port] == 10000 else (0xd4, 0x03000000)
        require(rd(base) & 0xff == protocol, f'lane{lane}: wrong protocol/width')
        for off in (0x24, 0x44):
            require(rd(base+off) & 0x17000000 == rate, f'lane{lane}: wrong PLL/rate')
        for off in (0x20, 0x40):
            require(rd(base+off) & 0xcd000000 == 0x40000000, f'lane{lane}: reset/disable')
        require(all(rd(base+off) == 0 for off in range(0xa0, 0xb4, 4)), 'lane test mode active')
        # Linux df24f942 phy-fsl-lynx-28g.c: LNaPSS_TYPE, get_pccr,
        # e25g_pcvt and lane_enable_pcvt. Board mappings: hardware evidence
        # x200-allports-rx-gain-scope / x200-dual-rate-native18-design.
        is25 = rates[port] == 25000
        converter = 7-lane if is25 else lane
        pss = (0x68000000 if is25 else 0x28000000) | ((mac-1) << 16) | (converter << 8)
        require(rd(0x1000+lane*4) == pss, f'lane{lane}: wrong PSS mapping')
        xfi = (rd(0x10b0) >> (28-lane*4)) & 0xf
        e25 = (rd(0x10b4) >> (lane*4)) & 0xf
        require((xfi & 1 == 0 and e25 == 1) if is25 else (xfi == 9 and e25 == 0),
                f'lane{lane}: wrong protocol converter')
        if is25:
            require(rd(0x1b08+(7-lane)*0x10) & 0x900000 == 0, f'lane{lane}: FEC active')


def check_mixed(state, rates=None, allowed_modules=()):
    rates = expected_rates(rates)
    require(set(allowed_modules) <= {'x200_manual_rate'}, 'unsupported module allowance')
    require(state['profile'] == PROFILE, 'mixed check requires candidate profile')
    require(not state['errors'], 'incomplete diagnostic capture: '+repr(state['errors']))
    require(set(state['diagnostic_modules']) <= set(allowed_modules), 'diagnostic module loaded')
    check_clocks(state['serdes'], rates)
    require(any('lynx' in driver for driver in state['phy_provider_drivers'].values()), 'Lynx PHY provider not bound')
    require(len([p for p in state['phy_devices'].values() if '1ea0000' in p]) >= 8, 'missing SerDes lane PHY devices')
    require(state['identity']['returncode'] == 0, 'UID MAC identity check failed')
    for port, (dpni, mac, lane, rate) in PORTS.items():
        rate = rates[port]
        p = state['ports'][port]
        require(p['device'] == dpni, f'{port}: wrong DPNI')
        require(p['dpni']['returncode'] == 0 and re.search(rf'^endpoint: dpmac\.{mac},', p['dpni']['raw'], re.M), 'wrong MC endpoint')
        info = p['dpmac']
        require(info['returncode'] == 0 and re.search(r'4\.11\b', info['raw']), 'wrong DPMAC API')
        require(re.search(r'^DPMAC link type:\s*DPMAC_LINK_TYPE_PHY\s*$', info['raw'], re.M), 'MAC not PHY-owned')
        interface = 'XFI' if rate == 10000 else 'CAUI'
        require(re.search(rf'^DPMAC ethernet interface:\s*DPMAC_ETH_IF_{interface}\s*$', info['raw'], re.M), 'wrong MC interface')
        require(re.search(rf'^maximum supported rate {rate} Mbps\s*$', info['raw'], re.M), 'wrong MC rate')
        require(p['phy_mode'] == ('10gbase-r' if rate == 10000 else '25gbase-r'), 'wrong DT PHY mode')
        require(p['managed'] == 'in-band-status' and p['phys'] == p['expected_phys']
                and p['pcs_handle'] == p['expected_pcs_handle'], 'wrong PHY lane/PCS phandle ownership')
        require(p['mdio_hz'] == 2500000 and p['mdio_cfg'] != 0xffffffff and (p['mdio_cfg'] >> 7) & 511 == 119, 'wrong MDIO clock')
        require(p['ethtool']['returncode'] == 0, 'ethtool failed')
        if p['carrier'] == '1':
            require(p['speed'] == str(rate) and f'Speed: {rate}Mb/s' in p['ethtool']['raw'], 'wrong active port speed')


def port_dt_node(dt, mac):
    # Pinned Linux fsl-lx2160a.dtsi uses ethernet@N. U-Boot uses dpmac@N.
    node = dt/f'soc/fsl-mc@80c000000/dpmacs/ethernet@{mac:x}'
    require((node/'compatible').read_bytes().rstrip(b'\0') == b'fsl,qoriq-mc-dpmac',
            'wrong Linux DPMAC DT compatible')
    require((node/'reg').read_bytes() == struct.pack('>I', mac), 'wrong Linux DPMAC DT reg')
    return node


def capture(boot, profile):
    state = {'errors': [], 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
             'machine': platform.machine(), 'kernel': platform.release(),
             'compatible': (DT/'compatible').read_bytes().rstrip(b'\0').decode().split('\0'),
             'profile': (DT/'rhinelab,board-profile-id').read_bytes().rstrip(b'\0').decode(),
             'serdes_reg': list(struct.unpack('>QQ', (DT/'soc/phy@1ea0000/reg').read_bytes()))}
    # Identity must authorize the MMIO aperture before any register access.
    check_identity(state, boot, profile)
    def attempt(key, fn, target=state):
        try:
            target[key] = fn()
        except Exception as exc:
            state['errors'].append(f'{key}: {exc}')
    offsets = {0, 4, 8, 0x10b0, 0x10b4}
    for base in (0x400, 0x500):
        offsets.update(range(base, base+0x2c, 4))
    for lane in range(8):
        offsets.add(0x1000+lane*4)
        offsets.update(0x800+lane*0x100+off for off in (0, 0x20, 0x24, 0x40, 0x44, 0xa0, 0xa4, 0xa8, 0xac, 0xb0))
    offsets.update(range(0x1800, 0x1840, 4))
    offsets.update(range(0x1b00, 0x1b40, 4))
    attempt('serdes', lambda: read_mmio(0x1ea0000, 0x2000, sorted(offsets)))
    state['diagnostic_modules'] = sorted(p.name for p in Path('/sys/module').glob('x200_*'))
    attempt('identity', lambda: command(sys.executable, str(Path(__file__).resolve().parents[1]/'identity/verify_linux.py')))
    attempt('mc', lambda: command('restool', '--mc-version'))
    state['ports'] = {}
    def port_state(port, dpni, mac):
        root = Path('/sys/class/net')/port
        result = {'device': (root/'device').resolve().name, 'master': (root/'master').exists()}
        state['ports'][port] = result  # Retain partial raw capture if a later gate/read fails.
        for name in ('address', 'carrier', 'speed', 'flags', 'carrier_changes'):
            if name == 'speed' and result.get('carrier') == '0':
                try:
                    result[name] = (root/name).read_text().strip()
                except OSError:
                    result[name] = 'unavailable-link-down'
            else:
                attempt(name, lambda n=name: (root/n).read_text().strip(), result)
        result['stats'] = {p.name: int(p.read_text()) for p in (root/'statistics').iterdir()}
        for key, args in (('ethtool', ('ethtool', port)), ('ethtool_stats', ('ethtool', '-S', port)),
                          ('dpni', ('restool', 'dpni', 'info', dpni)), ('dpmac', ('restool', 'dpmac', 'info', f'dpmac.{mac}'))):
            attempt(key, lambda a=args: command(*a), result)
        mac_node = port_dt_node(DT, mac)
        for key, prop in (('phy_mode', 'phy-mode'), ('managed', 'managed')):
            result[key] = (mac_node/prop).read_bytes().rstrip(b'\0').decode()
        result['phys'] = (mac_node/'phys').read_bytes().hex()
        result['pcs_handle'] = (mac_node/'pcs-handle').read_bytes().hex()
        provider = DT/'soc/phy@1ea0000'
        result['expected_phys'] = ((provider/'phandle').read_bytes()+struct.pack('>I', PORTS[port][2])).hex()
        base = 0x8c03000+mac*0x4000
        node = DT/f'soc/mdio@{base:x}'
        require(struct.unpack('>QQ', (node/'reg').read_bytes()) == (base, 0x1000) and (node/'little-endian').exists(), 'wrong MDIO aperture')
        result['expected_pcs_handle'] = (node/'ethernet-phy@0/phandle').read_bytes().hex()
        result['mdio_hz'] = struct.unpack('>I', (node/'clock-frequency').read_bytes())[0]
        result['mdio_cfg'] = read_mmio(base, 4096, [0x30])['0030']
        return result
    for port, (dpni, mac, _, _) in PORTS.items():
        attempt(port, lambda p=port, d=dpni, m=mac: port_state(p, d, m), state['ports'])
    state['phy_devices'] = {p.name: str(p.resolve()) for p in Path('/sys/class/phy').glob('*')}
    provider_devices = [p for p in Path('/sys/bus/platform/devices').glob('*')
                        if (p/'of_node').resolve() == (DT/'soc/phy@1ea0000').resolve()]
    state['phy_provider_drivers'] = {p.name: (p/'driver').resolve().name
                                     for p in provider_devices if (p/'driver').exists()}
    state['pci_epc'] = {p.name: str(p.resolve()) for p in Path('/sys/class/pci_epc').glob('*')}
    state['pci_identity'] = {p.name: {n: (p/n).read_text().strip() for n in ('vendor', 'device', 'class')}
                             for p in Path('/sys/bus/pci/devices').glob('*')}
    state['aer'] = {str(p): p.read_text() for p in Path('/sys/bus/pci/devices').glob('*/aer_*')}
    state['aer_scope'] = 'board-visible only; host root-port capture required separately'
    state['boot_id_end'] = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    require(state['boot_id_end'] == boot, 'boot changed during capture')
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-boot-id', required=True)
    parser.add_argument('--expected-profile', required=True)
    parser.add_argument('--check-mixed', action='store_true')
    parser.add_argument('--rate', action='append', default=[])
    parser.add_argument('--allow-manual-rate-module', action='store_true')
    args = parser.parse_args()
    require(args.check_mixed or not (args.rate or args.allow_manual_rate_module), 'rate/module expectations require --check-mixed')
    rates = parse_rates(args.rate)
    state = capture(args.expected_boot_id, args.expected_profile)
    print(json.dumps(state, sort_keys=True), flush=True)
    if args.check_mixed:
        check_mixed(state, rates, ('x200_manual_rate',) if args.allow_manual_rate_module else ())
    require(not state['errors'], 'capture incomplete')


if __name__ == '__main__':
    main()
