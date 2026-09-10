#!/usr/bin/env python3
"""Publish or adopt the X200 vNTB endpoint using the packaged producer identity."""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import time

KERNEL_RELEASE = KERNEL_VERSION = KERNEL_BUILD_ID = None
MODULES = {}
DNS_SERVER = None

ASSETS = {'card-runtime.py', 'card-install.py', 'card.service', 'card-modules.conf',
          'vntb-card-dns.py', '91-x200-vntb-dns.conf',
          '90-x200-vntb-unmanaged.conf', 'runtime.json'}
CONFIGS = {
    '90-x200-vntb-unmanaged.conf': '/etc/NetworkManager/conf.d/90-x200-vntb-unmanaged.conf',
    'card-modules.conf': '/etc/modprobe.d/x200-pcie.conf',
}
CONTROLLER = Path('/sys/kernel/config/pci_ep/controllers/3800000.pcie-ep')
FUNCTION = Path('/sys/kernel/config/pci_ep/functions/pci_epf_vntb/x200')
NET = Path('/sys/class/net')
SYS_MODULE = Path('/sys/module')
IFACE = 'pcie0'
ADDRESS = GATEWAY = NETWORK = None


def require(ok, message):
    if not ok:
        raise ValueError(message)


def run(*args):
    return subprocess.check_output(args, text=True, timeout=30).strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def verify_bundle(bundle, expected):
    manifest = bundle / 'SHA256SUMS'
    require(not manifest.is_symlink(), 'manifest must be a regular file')
    data = manifest.read_bytes()
    require(sha(data) == expected, 'bundle manifest digest mismatch')
    records = {}
    for line in data.decode().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9_.-]+)', line)
        require(match is not None, 'malformed or non-local SHA256SUMS entry')
        digest, name = match.groups()
        require(name not in records, 'duplicate manifest entry')
        records[name] = digest
    require('runtime.json' in records and sha((bundle / 'runtime.json').read_bytes()) == records['runtime.json'], 'runtime contract mismatch')
    configure(bundle)
    require(set(records) == ASSETS | {m[0] for m in MODULES.values()}, 'unexpected bundle inventory')
    for name, digest in records.items():
        path = bundle / name
        require(not path.is_symlink() and path.is_file(), f'not a regular asset: {name}')
        require(sha(path.read_bytes()) == digest, f'asset digest mismatch: {name}')
    for name, digest in MODULES.values():
        require(records[name] == digest, f'module differs from producer: {name}')


def configure(bundle):
    global KERNEL_RELEASE, KERNEL_VERSION, KERNEL_BUILD_ID, MODULES, IFACE, ADDRESS, GATEWAY, NETWORK, DNS_SERVER
    data = json.loads((bundle / 'runtime.json').read_text())
    KERNEL_RELEASE, KERNEL_VERSION, KERNEL_BUILD_ID = data['kernel_release'], data['kernel_version'], data['build_id']
    MODULES = {name: (item['filename'], item['sha256']) for name, item in data['modules'].items()}
    require(set(MODULES) == {'ntb', 'pci_epf_vntb', 'ntb_transport', 'ntb_netdev'}, 'incorrect NTB inventory')
    network = data['network']
    IFACE, ADDRESS, GATEWAY = network['card_interface'], network['card_address'], network['host_address']
    NETWORK = ipaddress.ip_network(network['network'])
    DNS_SERVER = str(ipaddress.IPv4Address(network['dns_server']))
    require(isinstance(NETWORK, ipaddress.IPv4Network) and NETWORK.prefixlen == 30 and ipaddress.ip_address(ADDRESS) in NETWORK.hosts() and
            ipaddress.ip_address(GATEWAY) in NETWORK.hosts() and ADDRESS != GATEWAY, 'invalid management subnet')
    require(re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', IFACE), 'invalid card interface')


def note_build_id(data):
    offset = 0
    while offset + 12 <= len(data):
        namesz, descsz, kind = struct.unpack_from('<III', data, offset)
        offset += 12
        name = data[offset:offset + namesz]
        offset += (namesz + 3) & ~3
        desc = data[offset:offset + descsz]
        offset += (descsz + 3) & ~3
        require(offset <= len(data), 'truncated ELF notes')
        if name == b'GNU\0' and kind == 3:
            require(descsz == 20, 'unexpected GNU build-id size')
            return desc.hex()
    raise ValueError('GNU build-id missing')


def module_build_id(path):
    data = path.read_bytes()
    require(data[:6] == b'\x7fELF\x02\x01', 'expected little-endian ELF64 module')
    shoff = struct.unpack_from('<Q', data, 40)[0]
    shsize, shnum, strindex = struct.unpack_from('<HHH', data, 58)
    require(shsize == 64 and shoff + shsize * shnum <= len(data), 'invalid ELF section table')
    sections = [struct.unpack_from('<IIQQQQIIQQ', data, shoff + i * shsize) for i in range(shnum)]
    strings = sections[strindex]
    names = data[strings[4]:strings[4] + strings[5]]
    for section in sections:
        if names[section[0]:].split(b'\0', 1)[0] == b'.note.gnu.build-id':
            return note_build_id(data[section[4]:section[4] + section[5]])
    raise ValueError(f'module build-id missing: {path}')


def kernel_gate():
    identity = os.uname()
    require((identity.release, identity.version, identity.machine) ==
            (KERNEL_RELEASE, KERNEL_VERSION, 'aarch64'), 'unqualified running kernel')
    require(note_build_id(Path('/sys/kernel/notes').read_bytes()) == KERNEL_BUILD_ID,
            'running kernel build-id mismatch')
    node = Path('/sys/bus/platform/devices/3800000.pcie-ep/of_node')
    require(b'rhinelab,x200-pcie-ep-diagnostic' in (node / 'compatible').read_bytes().split(b'\0'),
            'endpoint DT lacks the diagnostic handoff compatible')
    require((node / 'dma-coherent').exists(), 'qualified endpoint requires coherent DT')
    require(CONTROLLER.is_dir(), 'diagnostic endpoint controller missing')


def manager_gate(bundle, reload=True):
    for source, destination in CONFIGS.items():
        path = Path(destination)
        require(not path.is_symlink() and path.read_bytes() == (bundle / source).read_bytes(),
                f'persistent ownership configuration mismatch: {destination}')
    run('systemctl', 'is-active', 'NetworkManager')
    require(run('systemctl', 'show', 'systemd-networkd.service', '--property=ActiveState', '--value') == 'inactive',
            'systemd-networkd must be inactive; NetworkManager is the sole manager')
    if reload:
        run('nmcli', 'general', 'reload', 'conf')


def loaded_gate(bundle):
    for module, (filename, _) in MODULES.items():
        note = SYS_MODULE / module / 'notes/.note.gnu.build-id'
        require(note_build_id(note.read_bytes()) == module_build_id(bundle / filename),
                f'loaded module identity mismatch: {module}')
    params = SYS_MODULE / 'ntb_transport/parameters'
    require((params / 'use_dma').read_text().strip() in ('N', '0'), 'DMA transport is not qualified')
    require((params / 'max_num_clients').read_text().strip() == '1', 'unexpected transport clients')


def function_gate(timeout=5):
    require((FUNCTION / 'vendorid').read_text().strip() == '0x1957', 'incorrect EPF vendor')
    require((FUNCTION / 'deviceid').read_text().strip() == '0xe200', 'incorrect EPF device')
    links = [p for p in CONTROLLER.iterdir() if p.is_symlink()]
    require(len(links) == 1 and links[0].name == 'x200' and links[0].resolve() == FUNCTION.resolve(),
            'unexpected controller function binding')
    # The driver-specific configfs group may appear after the bind symlink.
    # Its mere existence is insufficient: observe a bound, advancing poller.
    deadline = time.monotonic() + timeout
    first_count = None
    while True:
        diagnostics = list(FUNCTION.glob('*/diagnostics'))
        require(len(diagnostics) <= 1, 'multiple vNTB diagnostics files')
        try:
            if diagnostics:
                attrs = diagnostics[0].parent
                for name, expected in {'num_mws': 1, 'spad_count': 64, 'db_count': 1, 'mw1': 1048576}.items():
                    require(int((attrs / name).read_text().strip(), 0) == expected, f'incorrect EPF {name}')
                state = dict(pair.split('=', 1) for pair in diagnostics[0].read_text().split())
                if state.get('bound') == '1':
                    count = int(state['poll_count'])
                    if first_count is not None and count > first_count:
                        return
                    first_count = count
                else:
                    first_count = None
        except FileNotFoundError:
            # Configfs is still publishing the group/attributes.
            first_count = None
        remaining = deadline - time.monotonic()
        require(remaining > 0, 'timed out waiting for vNTB diagnostics and advancing bound poller')
        time.sleep(min(0.05, remaining))


def ntb_interfaces():
    result = []
    for path in NET.iterdir():
        info = subprocess.run(['ethtool', '-i', path.name], text=True, capture_output=True, timeout=5)
        if 'driver: ntb_netdev' in info.stdout.splitlines():
            result.append(path.name)
    return result


def routes_gate(routes, active):
    defaults = []
    for route in routes:
        destination = route.get('dst')
        if destination == 'default':
            if route.get('table', 'main') not in ('main', 254):
                continue
            metric = route.get('metric', 0)
            if route.get('dev') == IFACE and route.get('gateway') == GATEWAY and metric == 0:
                defaults.append(route)
            else:
                # Preserve DHCP defaults; the owned metric-zero main default
                # wins. Another metric-zero default would make that ambiguous.
                require(metric > 0, f'competing main-table default route: {route}')
        elif destination and NETWORK.overlaps(ipaddress.ip_network(destination, strict=False)):
            require(active and route.get('dev') == IFACE and route.get('protocol') == 'kernel' and
                    route.get('prefsrc') == ADDRESS and
                    route.get('dst') in (str(NETWORK), str(NETWORK.network_address), ADDRESS, str(NETWORK.broadcast_address)),
                    f'overlapping management route: {route}')
    require(len(defaults) == int(active), 'unexpected management default route count')


def network_gate(active):
    routes_gate(json.loads(run('ip', '-j', '-4', 'route', 'show', 'table', 'all')), active)
    if active:
        require(ntb_interfaces() == [IFACE], 'unexpected NTB network interfaces')
        link = json.loads(run('ip', '-j', '-4', 'address', 'show', 'dev', IFACE))[0]
        addresses = [(a['local'], a['prefixlen']) for a in link['addr_info']]
        require(addresses == [(ADDRESS, 30)] and link['mtu'] == 1500 and 'UP' in link['flags'],
                'live pcie0 does not match qualified network state')
        require(run('nmcli', '-g', 'GENERAL.NM-MANAGED', 'device', 'show', IFACE) == 'no',
                'NetworkManager owns the NTB interface')
    else:
        require(not ntb_interfaces() and not (NET / IFACE).exists(), 'unexpected preexisting NTB netdev')
        for link in json.loads(run('ip', '-j', '-4', 'address', 'show')):
            require(not any(ipaddress.ip_network(f"{a['local']}/{a['prefixlen']}", strict=False).overlaps(NETWORK)
                            for a in link.get('addr_info', [])), 'management address overlap')


def cold_start(bundle):
    require((CONTROLLER / 'start').read_text().strip() == '0', 'endpoint is already published')
    require(not FUNCTION.exists(), 'unowned preexisting configfs function')
    require(not any(p.is_symlink() for p in CONTROLLER.iterdir()), 'controller already has a function')
    network_gate(False)
    for module in ('ntb', 'pci_epf_vntb'):
        run('insmod', str(bundle / MODULES[module][0]))
    FUNCTION.mkdir()
    # Defaults are part of the hash-pinned EPF contract, and bind validates them.
    (CONTROLLER / 'x200').symlink_to(FUNCTION)
    function_gate()
    run('insmod', str(bundle / MODULES['ntb_transport'][0]), 'use_dma=0', 'max_num_clients=1')
    run('insmod', str(bundle / MODULES['ntb_netdev'][0]))
    run('udevadm', 'settle', '--timeout=10')
    interfaces = ntb_interfaces()
    require(len(interfaces) == 1, 'expected exactly one NTB netdev')
    interface = interfaces[0]
    require(int((NET / interface / 'flags').read_text(), 0) & 1 == 0,
            'network manager raised NTB interface before preparation')
    require(run('nmcli', '-g', 'GENERAL.NM-MANAGED', 'device', 'show', interface) == 'no',
            'NetworkManager owns newly created NTB interface')
    if interface != IFACE:
        run('ip', 'link', 'set', 'dev', interface, 'name', IFACE)
    run('ip', 'link', 'set', 'dev', IFACE, 'mtu', '1500')
    run('ip', 'address', 'add', ADDRESS + '/30', 'dev', IFACE)
    run('ip', 'link', 'set', 'dev', IFACE, 'up')
    run('ip', 'route', 'add', 'default', 'via', GATEWAY, 'dev', IFACE)
    loaded_gate(bundle)
    network_gate(True)
    run('/usr/bin/python3', '-B', str(bundle / 'vntb-card-dns.py'), 'apply')
    # Publication is the last mutation: transport, addressing and DNS are ready.
    (CONTROLLER / 'start').write_text('1\n')
    require((CONTROLLER / 'start').read_text().strip() == '1', 'endpoint publication failed')


def start(bundle, expected):
    verify_bundle(bundle, expected)
    kernel_gate()
    require(not (SYS_MODULE / 'pci_epf_test').exists(), 'test endpoint module is loaded')
    present = [name for name in MODULES if (SYS_MODULE / name).exists()]
    require(not present or len(present) == len(MODULES),
            'partial NTB state; coordinated recovery required, refusing automatic teardown')
    if present:
        # Validate before touching even resolver/network-manager state on a live peer.
        loaded_gate(bundle)
        require((CONTROLLER / 'start').read_text().strip() == '1', 'loaded endpoint is not published')
        function_gate()
        network_gate(True)
    manager_gate(bundle)
    if present:
        run('/usr/bin/python3', '-B', str(bundle / 'vntb-card-dns.py'), 'apply')
        print('Adopted verified live X200 PCIe management network')
    else:
        cold_start(bundle)
        print('Published X200 PCIe management network; host discovery may proceed')


def check(bundle, expected):
    """Read-only verification; no module, configfs, route or manager mutations."""
    verify_bundle(bundle, expected)
    kernel_gate()
    require(not (SYS_MODULE / 'pci_epf_test').exists(), 'test endpoint module is loaded')
    loaded_gate(bundle)
    require((CONTROLLER / 'start').read_text().strip() == '1', 'endpoint is not published')
    function_gate()
    network_gate(True)
    manager_gate(bundle, reload=False)
    require(Path('/run/NetworkManager/conf.d/91-x200-vntb-dns.conf').read_bytes() ==
            (bundle / '91-x200-vntb-dns.conf').read_bytes(), 'DNS ownership configuration mismatch')
    nameservers = [line.split()[1] for line in Path('/etc/resolv.conf').read_text().splitlines()
                   if line.startswith('nameserver ')]
    require(nameservers == [DNS_SERVER], 'resolver does not use the PCIe DNS server')
    route = json.loads(run('ip', '-j', '-4', 'route', 'get', DNS_SERVER))[0]
    require(route.get('dev') == IFACE and route.get('gateway') == GATEWAY,
            'DNS traffic does not use the PCIe gateway')
    print('Verified live X200 PCIe management network (read-only)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('manifest_sha256')
    parser.add_argument('--check', action='store_true', help='verify live state without mutations')
    args = parser.parse_args()
    require(os.geteuid() == 0, 'root is required')
    with open('/run/x200-pcie-card.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (check if args.check else start)(args.bundle.resolve(), args.manifest_sha256)
