#!/usr/bin/env python3
"""Select 10G/25G on a standalone X200 L2 port; recreates the selected netdev.

Usage: python3 dual-rate/manual-rate/port_rate.py status
       python3 dual-rate/manual-rate/port_rate.py set eth1 25000 [--require-carrier]
Requires the packaged producer kernel; repeated mixed-rate carrier recovery is not qualified.
Runtime only; exact ethN names, UID MAC, MTU 1500 and queue length 1000 only.
Ports with addresses, routes, masters or virtual children are
rejected. PCIe management remains available while the selected port restarts.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import struct
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from read_state import capture, check_mixed, require, PORTS, PROFILE

PROC = Path('/proc/x200_manual_rate')
BUS = Path('/sys/bus/fsl-mc/devices')
DRIVER = Path('/sys/bus/fsl-mc/drivers/fsl_dpaa2_eth')
PENDING = Path('/run/x200-manual-rate.pending.json')
IMAGE_SHA = KERNEL_BUILD_ID = None
MODULE = 'x200_manual_rate'
MANAGEMENT_INTERFACE = 'pcie0'
QUALIFICATION = {'scope': 'bounded manual runtime use', 'target_validation': 'pending',
                 'repeated_cycle_qualified': False,
                 'mixed_rate_acceptance': 'not qualified; carrier recovery may fail'}


def configure(manifest):
    global IMAGE_SHA, KERNEL_BUILD_ID, MANAGEMENT_INTERFACE
    data = json.loads(manifest.read_text())
    require(data['build_validation'] == 'PASS', 'manual module build is not validated')
    IMAGE_SHA, KERNEL_BUILD_ID = data['kernel_image_sha256'], data['kernel_build_id']
    MANAGEMENT_INTERFACE = data['management_interface']


class Ambiguous(RuntimeError):
    pass


def run(*args, timeout=10):
    return subprocess.check_output(args, text=True, timeout=timeout)


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
    raise RuntimeError('GNU build-id missing')


def kernel_guard():
    require(hashlib.sha256(Path('/boot/Image').read_bytes()).hexdigest() == IMAGE_SHA,
            'unexpected Image')
    require(note_build_id(Path('/sys/kernel/notes').read_bytes()) == KERNEL_BUILD_ID,
            'unexpected running kernel build ID; staged Image is insufficient')


def module_state():
    if not PROC.exists():
        return {}
    return dict(line.split('=', 1) for line in PROC.read_text().splitlines() if '=' in line)


def rates_from_module(state):
    rates = {}
    for name, (dpni, mac, lane, initial) in PORTS.items():
        if state:
            require(state.get(f'mac{mac}_state') in ('original', 'changed'), 'ambiguous module state')
            require(state.get(f'mac{mac}_dpni') == dpni and state.get(f'mac{mac}_lane') == str(lane), 'wrong module port map')
            rate = int(state[f'mac{mac}_rate'])
        else:
            rate = initial
        require(rate in (10000, 25000), 'invalid active mode')
        rates[name] = rate
    return rates


def validate(state, rates):
    check_mixed(state, rates=rates, allowed_modules=(MODULE,) if PROC.exists() else ())


def network_snapshot(port):
    data = json.loads(run('ip', '-j', 'addr', 'show', 'dev', port))[0]
    require(not data.get('addr_info'), 'port has L3 addresses; use a standalone L2 test port')
    require(not data.get('master'), 'port belongs to a master')
    require('UP' in data['flags'], 'bring the standalone test port administratively up first')
    for family in ('-4', '-6'):
        require(not json.loads(run('ip', '-j', family, 'route', 'show', 'table', 'all', 'dev', port)), 'port has routes')
    for other in json.loads(run('ip', '-j', 'link', 'show')):
        require(other.get('link_index') != data['ifindex'], 'port has dependent virtual interfaces')
    require(not list((Path('/sys/class/net')/port).glob('upper_*')), 'port has upper devices')
    require((Path('/proc/sys/net/ipv6/conf')/port/'disable_ipv6').read_text().strip() == '1',
            'standalone L2 profile must disable IPv6')
    return {k: data[k] for k in ('ifindex', 'ifname', 'address', 'mtu', 'txqlen', 'flags')}


def profile_uuid(port):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'rhinelab:x200:l2:' + port))


def manager_state(port, *, timeout=10):
    return dict(line.split(':', 1) for line in run('nmcli', '-t', '--escape', 'no', '-f',
        'GENERAL.NM-MANAGED,GENERAL.AUTOCONNECT,GENERAL.STATE,GENERAL.CONNECTION,GENERAL.CON-UUID',
        'device', 'show', port, timeout=timeout).splitlines())


def profile_active(port, state):
    return state.get('GENERAL.NM-MANAGED') == 'yes' and \
        state.get('GENERAL.AUTOCONNECT') == 'yes' and \
        state.get('GENERAL.STATE', '').split(' ', 1)[0] == '100' and \
        state.get('GENERAL.CONNECTION') == 'x200-l2-' + port and \
        state.get('GENERAL.CON-UUID') == profile_uuid(port)


def management_guard(port):
    require(run('systemctl', 'show', 'systemd-networkd.service',
                '--property=ActiveState', '--value').strip() == 'inactive',
            'networkd must be inactive; NetworkManager is the sole network manager')
    require(profile_active(port, manager_state(port)),
            'the exact NetworkManager L2 profile must already be active')
    properties = dict(line.split(':', 1) for line in run('nmcli', '-t', '--escape', 'no', '-f',
        'connection.id,connection.uuid,connection.interface-name,connection.autoconnect,ipv4.method,ipv6.method,'
        '802-3-ethernet.mtu,802-3-ethernet.cloned-mac-address',
        'connection', 'show', profile_uuid(port)).splitlines())
    require(properties == {'connection.id': 'x200-l2-' + port,
                           'connection.uuid': profile_uuid(port),
                           'connection.interface-name': port, 'connection.autoconnect': 'yes',
                           'ipv4.method': 'disabled', 'ipv6.method': 'disabled',
                           '802-3-ethernet.mtu': 'auto', '802-3-ethernet.cloned-mac-address': ''},
            'explicit autoconnect NetworkManager standalone L2 profile is required')


def default_l2_guard(port, saved):
    require(saved['ifname'] == port and saved['mtu'] == 1500 and saved['txqlen'] == 1000,
            'manual rate trials require the exact ethN name, MTU 1500 and queue length 1000')


def wait_network_manager(port):
    started = time.monotonic()
    deadline = started + 10
    state = {}
    error = None
    status = 'TIMEOUT'
    while time.monotonic() < deadline:
        try:
            state = manager_state(port, timeout=max(.001, min(1, deadline-time.monotonic())))
            error = None
        except (RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            error = str(exc)
        if not error and profile_active(port, state) and time.monotonic() <= deadline:
            status = 'ACTIVATED'
            break
        if not error and state.get('GENERAL.STATE', '').split(' ', 1)[0] == '100' and not profile_active(port, state):
            status = 'WRONG_PROFILE'
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(.1, remaining))
    return {'status': status, 'elapsed_seconds': time.monotonic() - started,
            'timeout_seconds': 10, 'state': state, 'read_error': error}


def neighbor_guard(before, after, selected):
    for key in ('boot_id', 'pci_identity', 'pci_epc', 'aer'):
        require(before[key] == after[key], key+' changed')
    for base in (0x400, 0x500):
        for off in (0, 4, 8):
            key = f'{base+off:04x}'
            require(before['serdes'][key] == after['serdes'][key], 'shared PLL changed')
    for name, (_, _, lane, _) in PORTS.items():
        if name == selected:
            continue
        a, b = before['ports'][name], after['ports'][name]
        for key in ('device', 'address', 'carrier', 'speed', 'flags', 'master', 'carrier_changes',
                    'phy_mode', 'managed', 'phys', 'pcs_handle', 'mdio_hz'):
            require(a[key] == b[key], f'{name}: {key} changed')
        for off in (0, 0x24, 0x44):
            key = f'{0x800+lane*0x100+off:04x}'
            require(before['serdes'][key] == after['serdes'][key], f'{name}: lane mode changed')
        key = f'{0x1000+lane*4:04x}'
        require(before['serdes'][key] == after['serdes'][key], f'{name}: PSS changed')


def selected_guard(port, saved):
    current = network_snapshot(port)
    for key in ('ifname', 'address', 'mtu', 'txqlen'):
        require(current[key] == saved[key], f'{port}: {key} was not restored')


def wait_link_state(port, rate, *, timeout=5, stable_for=0.125):
    # Carrier publication and reported speed are separate updates. Require
    # matching samples over the requested stability period, and report timeouts.
    started = time.monotonic()
    deadline = started + timeout
    stable = samples = 0
    stable_started = None
    stable_seconds = 0.0
    carrier = speed = error = None
    status = 'TIMEOUT'
    root = Path('/sys/class/net') / port
    while time.monotonic() < deadline:
        carrier = speed = error = None
        try:
            carrier = root.joinpath('carrier').read_text().strip()
            if carrier == '1':
                speed = root.joinpath('speed').read_text().strip()
        except OSError as exc:
            error = str(exc)
        samples += 1
        ready = not error and carrier == '1' and speed == str(rate)
        now = time.monotonic()
        stable = stable+1 if ready else 0
        stable_started = (now if stable_started is None else stable_started) if ready else None
        stable_seconds = now - stable_started if ready else 0.0
        remaining = deadline - now
        if stable >= 2 and stable_seconds >= stable_for and remaining >= 0:
            status = 'READY'
            break
        if remaining <= 0:
            break
        time.sleep(min(0.125, remaining))
    return {'status': status, 'timeout_seconds': timeout,
            'elapsed_seconds': time.monotonic() - started, 'samples': samples,
            'consecutive_ready_samples': stable, 'stable_seconds': stable_seconds,
            'required_stable_seconds': stable_for, 'carrier': carrier,
            'speed': speed, 'read_error': error}


class Transaction:
    def __init__(self, out, boot, port, desired):
        self.out, self.boot, self.port, self.desired = out, boot, port, desired
        self.dpni, self.mac, _, _ = PORTS[port]
        self.report = {'status': 'PREPARING', 'boot_id': boot, 'port': port,
                       'dpni': self.dpni, 'mac': self.mac, 'desired_mbps': desired,
                       'events': [], 'runtime_only': True, 'qualification': dict(QUALIFICATION)}

    def save(self):
        (self.out/'receipt.json').write_text(json.dumps(self.report, indent=2)+'\n')

    def process(self, argv):
        require(Path('/proc/sys/kernel/random/boot_id').read_text().strip() == self.boot, 'boot changed')
        index = len(self.report['events'])
        event = {'argv': argv, 'phase': 'STARTING'}
        self.report['events'].append(event)
        self.save()
        # A sysfs write can enter a driver with an unbounded hardware wait.
        # Do not start rollback while such a worker might still be mutating.
        with (self.out/f'worker-{index}.log').open('w') as log:
            worker = subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=True)
            event['pid'] = worker.pid
            self.save()
            try:
                result = worker.wait(timeout=15)
            except subprocess.TimeoutExpired:
                event['phase'] = 'AMBIGUOUS_WORKER'
                self.save()
                raise Ambiguous(f'worker {worker.pid} still pending; preserve module/journal')
        event.update(phase='DONE', exit_code=result)
        self.save()
        require(result == 0, f'command failed; inspect worker-{index}.log')

    def write(self, path, text):
        self.process([sys.executable, '-c',
            'import sys; from pathlib import Path; Path(sys.argv[1]).write_text(sys.argv[2]+"\\n")',
            str(path), text])

    def ip(self, *args):
        self.process(['ip', *args])

    def unbind(self):
        driver = BUS/self.dpni/'driver'
        if driver.exists():
            require(driver.resolve() == DRIVER.resolve(), 'unexpected DPNI owner')
            names = [p.name for p in (BUS/self.dpni/'net').glob('*')]
            require(len(names) == 1, 'unexpected DPNI netdev count')
            self.ip('link', 'set', 'dev', names[0], 'down')
            self.write(DRIVER/'unbind', self.dpni)
        require(not driver.exists(), 'DPNI still bound')
        require(not (BUS/f'dpmac.{self.mac}'/'driver').exists(), 'DPMAC has another owner')

    def bind(self, saved):
        require(not (BUS/self.dpni/'driver').exists(), 'DPNI unexpectedly bound')
        self.write(DRIVER/'bind', self.dpni)
        require((BUS/self.dpni/'driver').resolve() == DRIVER.resolve(), 'DPNI bind failed')
        names = [p.name for p in (BUS/self.dpni/'net').glob('*')]
        require(len(names) == 1, 'netdev was not recreated')
        require(names[0] == saved['ifname'], 'recreated netdev has an unexpected name')
        # NM autoconnect is the sole activation path. Another explicit up or
        # down here can restart a connection NM has already activated.
        adoption = wait_network_manager(self.port)
        self.report.setdefault('network_manager_adoptions', []).append(adoption)
        self.save()
        require(adoption['status'] == 'ACTIVATED', 'NetworkManager profile adoption failed')
        management_guard(self.port)
        selected_guard(self.port, saved)

    def change(self, rate, saved):
        self.unbind()
        self.report.setdefault('unbound_resource_states', []).append(module_state())
        self.save()
        self.write(PROC, f'set {self.mac} {rate}')
        require(int(module_state()[f'mac{self.mac}_rate']) == rate, 'DT mode update failed')
        self.bind(saved)

    def observe_link(self, rate, required, phase):
        result = wait_link_state(self.port, rate, timeout=30 if required else 5,
                                 stable_for=2 if required else 0.125)
        self.report.setdefault('link_waits', []).append(
            {'phase': phase, 'required': required, 'rate_mbps': rate, **result})
        self.save()
        return result


def apply(port, desired, out, boot, require_carrier):
    require(not PENDING.exists(), 'unfinished transaction: '+str(PENDING))
    kernel_guard()
    require((Path('/sys/class/net')/MANAGEMENT_INTERFACE/'carrier').read_text().strip() == '1', 'PCIe management link is down')
    previous = rates_from_module(module_state())
    before = capture(boot, PROFILE)
    validate(before, previous)
    saved = network_snapshot(port)
    default_l2_guard(port, saved)
    require(saved['address'] == before['ports'][port]['address'], 'live MAC differs from verified UID identity')
    management_guard(port)
    out.mkdir(parents=True, exist_ok=False)
    (out/'before.json').write_text(json.dumps(before, indent=2)+'\n')
    tx = Transaction(out, boot, port, desired)
    tx.report.update(previous_mbps=previous[port], network_before=saved)
    if previous[port] == desired:
        if require_carrier:
            observed = tx.observe_link(desired, True, 'unchanged')
            if observed['status'] != 'READY':
                tx.report.update(status='FAILED', error='selected mode link wait timed out')
                tx.save()
                raise RuntimeError(tx.report['error'])
        tx.report['status'] = 'UNCHANGED'
        tx.save()
        return tx.report
    PENDING.write_text(json.dumps({'boot': boot, 'receipt': str(out/'receipt.json')})+'\n')
    changed = False
    safe = False
    try:
        if not PROC.exists():
            receipt = json.loads((HERE/'build-receipt.json').read_text())
            module = HERE/(MODULE+'.ko')
            require(receipt.get('kernel_image_sha256') == IMAGE_SHA, 'module targets another Image')
            require(receipt.get('build_validation') == 'PASS', 'unvalidated module build')
            require(hashlib.sha256(module.read_bytes()).hexdigest() == receipt['module_sha256'], 'module hash mismatch')
            run('insmod', str(module))
        initial_module = module_state()
        require(rates_from_module(initial_module) == previous, 'initial module state mismatch')
        tx.report['module_state_before'] = initial_module
        changed = True  # Includes a failed unbind or partially applied property.
        tx.change(desired, saved)
        expected = {**previous, port: desired}
        observed = tx.observe_link(desired, require_carrier, 'apply')
        after = capture(boot, PROFILE)
        (out/'after.json').write_text(json.dumps(after, indent=2)+'\n')
        validate(after, expected)
        neighbor_guard(before, after, port)
        selected_guard(port, saved)
        require(after['ports'][port]['address'] == saved['address'], 'MAC changed')
        if require_carrier:
            require(observed['status'] == 'READY', 'requested mode link wait timed out')
            require(after['ports'][port]['carrier'] == '1' and
                    after['ports'][port]['speed'] == str(desired),
                    'requested mode did not retain carrier and correct speed')
        tx.report.update(status='APPLIED', selected_mbps=desired,
                         carrier=after['ports'][port]['carrier'], neighbors_unchanged=True)
        safe = True
    except Ambiguous as exc:
        tx.report.update(status='AMBIGUOUS', error=str(exc))
    except Exception as exc:
        tx.report.update(status='FAILED', error=str(exc))
        try:
            if changed:
                tx.change(previous[port], saved)
                restore_carrier = before['ports'][port]['carrier'] == '1'
                restored_link = tx.observe_link(previous[port], restore_carrier, 'restore')
                restored = capture(boot, PROFILE)
                (out/'restored.json').write_text(json.dumps(restored, indent=2)+'\n')
                validate(restored, previous)
                neighbor_guard(before, restored, port)
                selected_guard(port, saved)
                require(restored['ports'][port]['address'] == saved['address'], 'restore MAC mismatch')
                if restore_carrier:
                    require(restored_link['status'] == 'READY', 'original link recovery timed out')
                    require(restored['ports'][port]['carrier'] == '1' and
                            restored['ports'][port]['speed'] == str(previous[port]),
                            'original carrier and speed were not restored')
                tx.report['restored'] = True
            safe = True
        except Exception as restore_error:
            tx.report.update(status='RESTORATION_INCOMPLETE', restore_error=str(restore_error))
    finally:
        if safe:
            try:
                tx.report['module_state'] = module_state()
                if PROC.exists() and module_state().get('pinned') == '0':
                    run('rmmod', MODULE)
                PENDING.unlink()
            except Exception as exc:
                tx.report.update(status='CLEANUP_INCOMPLETE', cleanup_error=str(exc))
        tx.save()
    return tx.report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, default=HERE/'build-receipt.json')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    change = sub.add_parser('set')
    change.add_argument('port', choices=PORTS)
    change.add_argument('mbps', type=int, choices=(10000, 25000))
    change.add_argument('--require-carrier', action='store_true')
    change.add_argument('--expected-boot-id')
    change.add_argument('--output', type=Path)
    args = p.parse_args()
    configure(args.manifest)
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    with open('/run/lock/x200-serdes-trial.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'status':
            kernel_guard()
            require(not PENDING.exists(), 'unfinished transaction: '+str(PENDING))
            state = capture(boot, PROFILE)
            rates = rates_from_module(module_state())
            validate(state, rates)
            result = {'ports': {n: {'selected_mbps': rates[n], 'link': d['carrier'],
                       'reported_mbps': d['speed']} for n, d in state['ports'].items()},
                      'runtime_only': True, 'pending': PENDING.exists(),
                      'qualification': dict(QUALIFICATION), 'module_state': module_state()}
        else:
            require(not args.expected_boot_id or args.expected_boot_id == boot, 'unexpected boot ID')
            out = args.output or Path('/var/log/x200-manual-rate')/f'{time.time_ns()}-{os.getpid()}'
            result = apply(args.port, args.mbps, out.resolve(), boot, args.require_carrier)
    print(json.dumps(result, indent=2))
    return 0 if result.get('status', 'UNCHANGED') in ('APPLIED', 'UNCHANGED') else 1


if __name__ == '__main__':
    raise SystemExit(main())
