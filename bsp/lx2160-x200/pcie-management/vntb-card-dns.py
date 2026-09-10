#!/usr/bin/env python3
"""Own card DNS during the session-scoped PCIe management connection."""
import argparse
import configparser
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import tempfile

TEMPLATE = Path(__file__).with_name('91-x200-vntb-dns.conf')
DEST = Path('/run/NetworkManager/conf.d') / TEMPLATE.name
IFACE = GATEWAY = None


def run(*args):
    return subprocess.check_output(args, text=True)


def check_owned_config(path, expected):
    if path.is_symlink() or (path.exists() and path.read_bytes() != expected):
        raise ValueError(f'refusing to replace/remove another configuration: {path}')


def check_owned_route(routes, server):
    if len(routes) > 1 or any(
        ipaddress.ip_network(r['dst'], strict=False) != ipaddress.ip_network(server + '/32')
        or r.get('dev') != IFACE or r.get('gateway') != GATEWAY
        or r.get('protocol') != 'static' or r.get('metric', 0) != 0
        for r in routes
    ):
        raise ValueError(f'refusing to replace/remove another DNS route: {routes}')


def configure(action):
    global IFACE, GATEWAY
    network = json.loads(Path(__file__).with_name('runtime.json').read_text())['network']
    IFACE, GATEWAY = network['card_interface'], network['host_address']
    expected = TEMPLATE.read_bytes()
    config = configparser.ConfigParser()
    config.read_string(expected.decode())
    server = str(ipaddress.IPv4Address(config['global-dns-domain-*']['servers']))
    subnet = server + '/32'
    run('systemctl', 'is-active', 'NetworkManager')
    check_owned_config(DEST, expected)
    routes = json.loads(run('ip', '-j', '-4', 'route', 'show', 'exact', subnet))
    check_owned_route(routes, server)
    if not DEST.exists() and routes:
        raise ValueError('DNS route exists without our configuration; ownership is unknown')

    if action == 'apply':
        if 'driver: ntb_netdev' not in run('ethtool', '-i', IFACE).splitlines():
            raise ValueError('expected qualified NTB network interface')
        DEST.parent.mkdir(parents=True, exist_ok=True)
        if not DEST.exists():
            # Publish the ownership record first so an interrupted apply is retryable.
            with tempfile.NamedTemporaryFile(dir=DEST.parent, delete=False) as f:
                tmp = Path(f.name)
                try:
                    f.write(expected)
                    f.flush()
                    os.fchmod(f.fileno(), 0o644)
                    tmp.replace(DEST)
                finally:
                    tmp.unlink(missing_ok=True)
        if not routes:
            run('ip', 'route', 'add', subnet, 'via', GATEWAY, 'dev', IFACE, 'proto', 'static')
    elif DEST.exists():
        # A removed netdev already retires its route. Never restore stale resolv.conf.
        if routes:
            run('ip', 'route', 'del', subnet, 'via', GATEWAY, 'dev', IFACE, 'proto', 'static')
        DEST.unlink()

    run('nmcli', 'general', 'reload', 'conf')
    run('nmcli', 'general', 'reload', 'dns-rc')
    resolv = Path('/etc/resolv.conf').read_text()
    if action == 'apply':
        nameservers = [x.split()[1] for x in resolv.splitlines() if x.startswith('nameserver ')]
        if nameservers != [server]:
            raise ValueError(f'NetworkManager did not publish the expected resolver: {resolv!r}')
        route = json.loads(run('ip', '-j', '-4', 'route', 'get', server))[0]
        if route.get('dev') != IFACE or route.get('gateway') != GATEWAY:
            raise ValueError(f'DNS traffic does not use the PCIe gateway: {route}')
    print(resolv, end='')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['apply', 'remove'])
    configure(parser.parse_args().action)
