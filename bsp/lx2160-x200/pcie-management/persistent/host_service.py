#!/usr/bin/env python3
"""Qualified X200 host startup. Failures preserve peer mappings; never unload."""
import argparse
import hashlib
import ipaddress
import json
import lzma
import os
import re
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from check_host_caps import proc_config_path, walk
from check_vntb_host import validate_bars

RELEASE = PARENT = PF0 = PF1 = None
NETWORK = None
CONFIG = {}
SYS = Path('/sys/bus/pci/devices')
PROC = Path('/proc/bus/pci')
NET = Path('/sys/class/net')
MODULES = Path('/sys/module')
ORDER = ('ntb', 'ntb_transport', 'ntb_hw_epf', 'ntb_netdev')
TAG_REQUESTER = 0x1000  # PCI_EXP_DEVCTL2_10BIT_TAG_REQ_EN, pciutils lib/header.h.
AER_FILES = ('aer_dev_correctable', 'aer_dev_nonfatal', 'aer_dev_fatal',
             'aer_rootport_total_err_cor', 'aer_rootport_total_err_nonfatal',
             'aer_rootport_total_err_fatal')


class NotReadyError(ValueError):
    """A late peer can be retried without touching an existing mapping."""


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv, *, check=True, input=None):
    result = subprocess.run([str(x) for x in argv], check=False, text=True,
                            input=input, capture_output=True, timeout=30)
    if check:
        require(result.returncode == 0, f'{argv}: {result.stderr.strip()}')
    return result


def module_note(path):
    """Extract ELF64LE build-id note, also from the pinned native xz modules."""
    blob = path.read_bytes()
    if path.suffix == '.xz':
        blob = lzma.decompress(blob)
    require(blob[:6] == b'\x7fELF\x02\x01', 'expected ELF64 little endian module')
    shoff = struct.unpack_from('<Q', blob, 40)[0]
    entsize, count, names_index = struct.unpack_from('<HHH', blob, 58)
    require(entsize == 64 and 0 < names_index < count, 'invalid ELF section table')
    headers = [struct.unpack_from('<IIQQQQIIQQ', blob, shoff + i * entsize)
               for i in range(count)]
    names_header = headers[names_index]
    names = blob[names_header[4]:names_header[4] + names_header[5]]
    for header in headers:
        name = names[header[0]:].split(b'\0', 1)[0]
        if name == b'.note.gnu.build-id':
            note = blob[header[4]:header[4] + header[5]]
            require(len(note) >= 20, 'empty module build-id')
            return note
    raise ValueError('module has no build-id; cannot adopt or attest it')


def configure(bundle, config):
    global RELEASE, PARENT, PF0, PF1, CONFIG, NETWORK
    CONFIG = json.loads(config.read_text())
    receipt = json.loads((bundle / 'manifest.json').read_text())
    RELEASE = receipt['kernel_release']
    PARENT, PF0 = CONFIG['host']['parent_bdf'], CONFIG['host']['pf0_bdf']
    for bdf in (PARENT, PF0):
        require(re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-1][0-9a-f]\.[0-7]', bdf), 'invalid configured PCI address')
    require(PF0.endswith('.0'), 'PF0 must be function zero')
    PF1 = PF0[:-1] + '1'
    net = CONFIG['network']
    NETWORK = ipaddress.ip_network(net['network'])
    require(isinstance(NETWORK, ipaddress.IPv4Network) and NETWORK.prefixlen == 30, 'management network must be IPv4 /30')
    for field in ('host_address', 'card_address'):
        require(ipaddress.ip_address(net[field]) in NETWORK.hosts(), 'management address outside subnet')
    require(net['host_address'] != net['card_address'], 'management addresses must differ')
    for field in ('host_interface', 'card_interface', 'uplink_interface', 'firewall_zone'):
        require(re.fullmatch(r'[A-Za-z0-9_.-]{1,15}', net[field]), 'invalid network interface/zone')


def verify_bundle(bundle):
    receipt = json.loads((bundle / 'manifest.json').read_text())
    require(receipt['kernel_release'] == RELEASE and receipt['build_validation'] == 'PASS',
            'host kernel/build mismatch')
    require(receipt.get('native_netdev_imports_match_replacement_transport') is True, 'missing native netdev ABI gate')
    contract = bundle / 'vntb-contract.json'
    require(receipt['contract_sha256'] == sha(contract), 'host contract integrity mismatch')
    require(receipt['contract'] == json.loads(contract.read_text()), 'host contract mismatch')
    for name, digest in receipt['assets'].items():
        path = bundle / name
        require(not Path(name).is_absolute() and '..' not in Path(name).parts and not path.is_symlink(), 'unsafe host asset')
        require(sha(path) == digest, 'host asset integrity mismatch: ' + name)
    paths = {}
    require(set(receipt['modules']) == set(ORDER), 'host module inventory mismatch')
    for name, record in receipt['modules'].items():
        path = bundle / 'modules' / record['filename']
        require(Path(record['filename']).name == record['filename'] and not path.is_symlink(), 'unsafe module path')
        require(sha(path) == record['sha256'], 'host module integrity mismatch: ' + name)
        module_note(path)  # Fail before any hardware action if exact live attestation is unavailable.
        paths[name] = path
    return paths


def attest_loaded(paths):
    for name, path in paths.items():
        note = MODULES / name / 'notes/.note.gnu.build-id'
        require(note.exists() and note.read_bytes() == module_note(path),
                f'loaded {name} build differs from qualified module; preserve state and quiesce explicitly')
    for name, value in {'use_dma': 'N', 'use_msi': 'N', 'max_num_clients': '1'}.items():
        require((MODULES / 'ntb_transport/parameters' / name).read_text().strip() == value,
                f'unqualified transport parameter {name}')


def topology():
    parent = SYS / PARENT
    require(parent.exists(), 'measured parent bridge absent')
    descendants = {p.name for p in SYS.iterdir()
                   if p != parent and p.resolve().is_relative_to(parent.resolve())}
    require(descendants <= {PF0, PF1}, f'foreign subordinate topology: {descendants}')
    for bdf, identity in ((PF0, '0xe200'), (PF1, '0x8d91')):
        path = SYS / bdf
        if not path.exists():
            continue
        require(path.resolve().parent == parent.resolve(), f'{bdf} unexpected parent')
        require((path / 'vendor').read_text().strip() == '0x1957' and
                (path / 'device').read_text().strip() == identity,
                f'{bdf} is not runtime vNTB; raw bootstrap PF removal is never automatic')
    return descendants == {PF0, PF1}


def parent_control(config):
    """Locate DEVCTL2 only after checking the measured root port and full chain."""
    # PCIe capability headers locate the generic 10-bit Tag Requester control.
    require(len(config) == 256, 'short parent conventional configuration read')
    require(config[10:12] == bytes([4, 6]) and config[14] & 0x7f == 1,
            'parent must be a PCI bridge with a type-one header')
    require(config[0x19] == int(PF0.split(':')[1], 16) and config[0x1a] == config[0x19],
            'configured parent must own exactly the endpoint bus')
    require(struct.unpack_from('<H', config, 6)[0] & 0x10,
            'parent conventional capability list absent')
    offset, seen, pcie = config[0x34], set(), []
    while offset:
        require(0x40 <= offset <= 0xfc and offset % 4 == 0 and offset not in seen,
                'malformed parent conventional capability chain')
        seen.add(offset)  # At most 48 aligned conventional headers are reachable.
        if config[offset] == 0x10:
            require(offset + 0x2a <= 256, 'parent PCIe capability exceeds conventional space')
            flags = struct.unpack_from('<H', config, offset + 2)[0]
            require(flags & 0xf == 2 and (flags >> 4) & 0xf == 4,
                    'parent must be PCIe v2 Root Port')
            pcie.append(offset)
        offset = config[offset + 1]
    require(len(pcie) == 1, 'expected exactly one parent PCIe capability')
    require(not any(pcie[0] < offset < pcie[0] + 0x2a for offset in seen),
            'overlapping parent PCIe capability')
    return pcie[0] + 0x28


def qualify_parent(*, check_only=False):
    """Adopt a cleared port, or clear only its 10-bit requester before discovery."""
    topology()  # Reject foreign descendants before even opening writable config.
    path = proc_config_path(PARENT, PROC)
    # Proc config is authoritative; a sysfs config inode can remain only 256 bytes.
    with path.open('rb') as stream:
        config = os.pread(stream.fileno(), 256, 0)
    offset = parent_control(config)
    before = struct.unpack_from('<H', config, offset)[0]
    if not before & TAG_REQUESTER:
        return
    require(not check_only, 'parent 10-bit Tag Requester remains enabled')
    require(not any((SYS / bdf).exists() for bdf in (PF0, PF1)) and
            not any((MODULES / name).exists() for name in ORDER),
            'parent tag change requires empty peer/module state; preserve active mappings')
    # Repeat all gates immediately before the only write; never alter an active peer.
    topology()
    with path.open('r+b', buffering=0) as stream:
        current = os.pread(stream.fileno(), 256, 0)
        require(parent_control(current) == offset and current == config,
                'parent configuration changed before tag update')
        require(not any((SYS / bdf).exists() for bdf in (PF0, PF1)) and
                not any((MODULES / name).exists() for name in ORDER),
                'peer/module state changed before tag update')
        wanted = before & ~TAG_REQUESTER
        require(os.pwrite(stream.fileno(), struct.pack('<H', wanted), offset) == 2,
                'short parent DEVCTL2 write; state preserved')
        require(os.pread(stream.fileno(), 2, offset) == struct.pack('<H', wanted),
                'parent DEVCTL2 readback failed; state preserved')


def aer_snapshot():
    """Read mandatory monotonic counters; sticky register bits remain untouched."""
    counters = {}
    for name in AER_FILES:
        rows = (SYS / PARENT / name).read_text().splitlines()
        require(bool(rows), f'empty parent AER counters: {name}')
        labels = set()
        for row in rows:
            fields = row.split()
            if name.startswith('aer_rootport_'):
                require(len(rows) == 1 and len(fields) == 1, f'invalid AER counter: {name}')
                label, value = 'total', fields[0]
            else:
                require(len(fields) == 2, f'invalid AER counter row: {name}')
                label, value = fields
            require(label not in labels and value.isascii() and value.isdecimal(),
                    f'invalid AER counter value: {name}/{label}')
            labels.add(label)
            counters[name + '/' + label] = int(value)
        required = {'aer_dev_correctable': {'TOTAL_ERR_COR'},
                    'aer_dev_nonfatal': {'TOTAL_ERR_NONFATAL', 'CmpltTO', 'UnxCmplt'},
                    'aer_dev_fatal': {'TOTAL_ERR_FATAL', 'CmpltTO', 'UnxCmplt'}}
        require(required.get(name, {'total'}) <= labels, f'missing required AER counters: {name}')
    return counters


def require_aer_unchanged(before, after):
    require(before.keys() == after.keys(), 'parent AER counter set changed; state preserved')
    delta = {name: after[name] - value for name, value in before.items() if after[name] != value}
    require(not delta, f'parent AER counters changed during discovery: {delta}; state preserved')


def discovery_rescan(baseline):
    topology()
    qualify_parent(check_only=True)
    require_aer_unchanged(baseline, aer_snapshot())
    # Device rescan does not resize the bridge; measured subordinate bus scan does.
    (SYS / PARENT / ('pci_bus/' + PF0.rsplit(':', 1)[0] + '/rescan')).write_text('1\n')
    # Measured firmware-first AER reporting can lag completion of the sysfs write.
    time.sleep(3)
    after = aer_snapshot()
    require_aer_unchanged(baseline, after)
    return after


def conventional_caps(config, pf0):
    require(len(config) == 256, 'short conventional configuration read')
    offset, seen, ids = config[0x34], set(), []
    while offset:
        require(0x40 <= offset <= 0xfc and offset % 4 == 0 and offset not in seen,
                'malformed conventional capability chain')
        seen.add(offset)
        cap = config[offset]
        ids.append(cap)
        if cap == 5:
            control = struct.unpack_from('<H', config, offset + 2)[0]
            require(1 << ((control >> 1) & 7) == 2, 'runtime MSI must expose two vectors')
        offset = config[offset + 1]
    require(sorted(ids) == ([1, 5, 0x10] if pf0 else [1, 0x10]),
            'conventional capability contract differs; MSI-X must remain unlinked')
    return ids


def validate_pfs():
    require(topology(), 'both runtime PFs required')
    observations = []
    for bdf, expected in ((PF0, (0x1957, 0xe200)), (PF1, (0x1957, 0x8d91))):
        path = SYS / bdf
        with proc_config_path(bdf, PROC).open('rb') as stream:
            header = os.pread(stream.fileno(), 64, 0)
            require(len(header) == 64 and struct.unpack_from('<HH', header) == expected,
                    f'{bdf} raw identity differs')
            require(header[14] & 0x7f == 0, 'not a type0 header')
            standard = conventional_caps(os.pread(stream.fileno(), 256, 0), bdf == PF0)
            caps = walk(stream.fileno())
            require(not any(c['unsafe_advertisement'] for c in caps),
                    f'{bdf} advertises unsafe extended capabilities')
        resources = (path / 'resource').read_text()
        if bdf == PF0:
            require(header[11] == 5 and header[10] == 0, 'wrong runtime PF0 class')
            validate_bars(resources)
        else:
            rows = resources.splitlines()[:6]
            require(len(rows) == 6 and all(int(v, 16) == 0 for row in rows for v in row.split()[:2]),
                    'PF1 unexpectedly exposes BARs')
            require(not (path / 'driver').exists(), 'PF1 unexpectedly bound')
        observations.append({'bdf': bdf, 'header': header.hex(),
                             'conventional_capabilities': standard, 'capabilities': caps})
    return observations


def await_pfs(timeout):
    deadline = time.monotonic() + timeout
    parent = SYS / PARENT
    topology()
    baseline = aer_snapshot()  # Missing counters fail closed before any mutation.
    # Gate the measured parent before changing its runtime power policy.
    with proc_config_path(PARENT, PROC).open('rb') as stream:
        parent_control(os.pread(stream.fileno(), 256, 0))
    # Runtime resume restores saved PCIe DEVCTL2, including the old tag bit.
    # Resume first; only then modify the live configuration and keep it awake.
    (parent / 'power/control').write_text('on\n')
    require((parent / 'power/control').read_text().strip() == 'on', 'parent PM pin failed')
    require((parent / 'power/runtime_status').read_text().strip() == 'active',
            'parent did not resume; state preserved')
    require_aer_unchanged(baseline, aer_snapshot())
    qualify_parent()
    while not topology():
        if time.monotonic() >= deadline:
            raise NotReadyError('runtime endpoint publication timed out; state preserved')
        baseline = discovery_rescan(baseline)
    return validate_pfs()


def endpoint_netdev():
    matches = [p for p in NET.iterdir() if (p / 'device').exists() and
               (p / 'device').resolve().is_relative_to((SYS / PF0).resolve())]
    require(len(matches) == 1, 'expected exactly one PF0 network interface')
    interface = matches[0].name
    require('driver: ntb_netdev' in run(['ethtool', '-i', interface]).stdout.splitlines(),
            'PF0 netdev driver mismatch')
    return interface


def bind(paths):
    loaded = {name for name in ORDER if (MODULES / name).exists()}
    if loaded:
        require(loaded == set(ORDER), 'partial NTB module state; explicit peer quiescence required')
        attest_loaded(paths)
        require((SYS / PF0 / 'driver').resolve().name == 'ntb_hw_epf', 'loaded PF0 driver mismatch')
        require(endpoint_netdev() == CONFIG['network']['host_interface'], 'existing interface identity mismatch')
        return
    require(not (SYS / PF0 / 'driver').exists(), 'PF0 already has a foreign driver')
    require(not (NET / CONFIG['network']['host_interface']).exists(), 'x200pcie already owned elsewhere')
    for name in ORDER:
        params = ['use_dma=0', 'use_msi=0', 'max_num_clients=1'] if name == 'ntb_transport' else []
        run(['insmod', paths[name], *params])
    attest_loaded(paths)
    require((SYS / PF0 / 'driver').resolve().name == 'ntb_hw_epf', 'PF0 bind failed')
    run(['udevadm', 'settle', '--timeout=10'])
    interface = endpoint_netdev()
    require(not int((NET / interface / 'flags').read_text(), 16) & 1,
            'new netdev unexpectedly UP before network configuration')
    require(run(['nmcli', '-g', 'GENERAL.NM-MANAGED', 'device', 'show', interface]).stdout.strip() == 'no',
            'NetworkManager must exclude NTB netdev before activation')
    run(['ip', 'link', 'set', 'dev', interface, 'name', CONFIG['network']['host_interface']])


def validate_routes(routes):
    wanted = NETWORK
    for route in routes:
        if route.get('dst') in (None, 'default'):
            continue
        if wanted.overlaps(ipaddress.ip_network(route['dst'], strict=False)):
            require(route.get('dev') == CONFIG['network']['host_interface'] and route.get('protocol') == 'kernel',
                    f'conflicting management route: {route}')


def configure_network(bundle):
    validate_routes(json.loads(run(['ip', '-j', '-4', 'route', 'show', 'table', 'all']).stdout))
    addresses = json.loads(run(['ip', '-j', '-4', 'address', 'show', 'dev', CONFIG['network']['host_interface']]).stdout)
    require(all(a['local'] == CONFIG['network']['host_address'] and a['prefixlen'] == 30
                for dev in addresses for a in dev['addr_info']), 'foreign x200pcie IPv4 address')
    require(run(['firewall-cmd', ('--get-zone-of-interface=' + CONFIG['network']['uplink_interface'])]).stdout.strip() == CONFIG['network']['firewall_zone'],
            'uplink zone differs from measured public zone')
    for permanent in ([], ['--permanent']):
        run(['firewall-cmd', *permanent, ('--zone=' + CONFIG['network']['firewall_zone']), '--query-forward'])
        zone = run(['firewall-cmd', *permanent, ('--get-zone-of-interface=' + CONFIG['network']['host_interface'])], check=False)
        require(zone.stdout.strip() in ('', 'no zone', CONFIG['network']['firewall_zone']), 'foreign x200pcie firewall zone')
    for permanent in ([], ['--permanent']):
        run(['firewall-cmd', *permanent, ('--zone=' + CONFIG['network']['firewall_zone']), ('--add-interface=' + CONFIG['network']['host_interface'])])
    run(['nmcli', 'device', 'set', CONFIG['network']['host_interface'], 'managed', 'no'])
    nft = (bundle / 'host-network.nft').read_text()
    for key in ('host_interface', 'card_address', 'uplink_interface'):
        nft = nft.replace('@' + key.upper() + '@', CONFIG['network'][key])
    tables = json.loads(run(['nft', '-j', 'list', 'tables']).stdout)
    exists = any(row.get('table', {}).get('family') == 'ip' and
                 row.get('table', {}).get('name') == 'x200_pcie_management'
                 for row in tables['nftables'])
    # Atomic transaction affects only our named table, including existing session rules.
    transaction = ('delete table ip x200_pcie_management\n' if exists else '') + nft
    run(['nft', '-c', '-f', '-'], input=transaction)
    run(['nft', '-f', '-'], input=transaction)
    run(['sysctl', '-w', 'net.ipv4.ip_forward=1'])
    run(['ip', 'link', 'set', 'dev', CONFIG['network']['host_interface'], 'mtu', '1500'])
    run(['ip', 'address', 'replace', (CONFIG['network']['host_address'] + '/30'), 'dev', CONFIG['network']['host_interface']])
    run(['ip', 'link', 'set', 'dev', CONFIG['network']['host_interface'], 'up'])


def await_ssh(timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if (NET / (CONFIG['network']['host_interface'] + '/carrier')).read_text().strip() == '1':
                with socket.create_connection((CONFIG['network']['card_address'], 22), timeout=2) as sock:
                    sock.settimeout(2)
                    if sock.recv(255).startswith(b'SSH-'):
                        return
        except OSError:
            pass
        time.sleep(1)
    raise NotReadyError('card carrier/SSH readiness timed out; state preserved')


def notify_ready():
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        return
    if address.startswith('@'):
        address = '\0' + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(address)
        sock.sendall(b'READY=1\nSTATUS=Qualified X200 carrier and SSH are ready')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=HERE)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--check', action='store_true', help='verify bundle and existing bound host only; no changes')
    args = parser.parse_args()
    configure(args.bundle, args.config)
    require(os.geteuid() == 0 and os.uname().release == RELEASE, 'root on qualified host kernel required')
    require(1 <= args.timeout <= 600, 'timeout must be between 1 and 600 seconds')
    paths = verify_bundle(args.bundle)
    if args.check:
        qualify_parent(check_only=True)
        observations = validate_pfs()
        attest_loaded(paths)
        require((SYS / PF0 / 'driver').resolve().name == 'ntb_hw_epf' and
                endpoint_netdev() == CONFIG['network']['host_interface'], 'existing binding mismatch')
    else:
        observations = await_pfs(args.timeout)
        bind(paths)
        configure_network(args.bundle)
        await_ssh(args.timeout)
        notify_ready()
    print(json.dumps({'gate': 'PASS', 'check_only': args.check,
                      'host_boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                      'functions': observations}))


if __name__ == '__main__':
    try:
        main()
    except NotReadyError as error:
        print(f'PEER_NOT_READY: {error}', file=sys.stderr)
        sys.exit(75)
    except Exception as error:
        print(f'FAILED_STATE_PRESERVED: {error}. Peer quiescence is required before teardown.', file=sys.stderr)
        sys.exit(1)
