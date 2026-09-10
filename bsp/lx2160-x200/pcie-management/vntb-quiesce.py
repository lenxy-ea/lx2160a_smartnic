#!/usr/bin/env python3
"""Stop both vNTB clients before freeing either peer memory window.

Runtime diagnostic sequence, not unattended peer-reset recovery. A failed card
acknowledgment stops this program before any host teardown. Do not bypass it.
"""
import argparse
import subprocess
from pathlib import Path
import json
import re
import sys
from check_vntb_host import collect

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CARD_QUIESCE = '''set -eu
ethtool -i pcie0 | grep -qx 'driver: ntb_netdev'
ip link set pcie0 down
rmmod ntb_netdev
test ! -e /sys/module/ntb_netdev
cat /proc/sys/kernel/random/boot_id
printf 'X200_CARD_CLIENT_QUIESCED\\n'
'''
CARD_UNBIND = '''set -eu
test ! -e /sys/module/ntb_netdev
printf 0 > /sys/kernel/config/pci_ep/controllers/3800000.pcie-ep/start
rmmod ntb_transport
rm /sys/kernel/config/pci_ep/controllers/3800000.pcie-ep/x200
rmdir /sys/kernel/config/pci_ep/functions/pci_epf_vntb/x200
rmmod pci_epf_vntb
rmmod ntb
cat /sys/kernel/config/pci_ep/controllers/3800000.pcie-ep/start
'''


def quiesce_clients(card, run=subprocess.run, host_interface="x200pcie", card_interface="pcie0"):
    # An exception/nonzero UART status must prevent every host teardown action.
    card('card-quiesce', CARD_QUIESCE.replace('pcie0', card_interface))
    run(['ip', 'link', 'set', host_interface, 'down'], check=True)
    for module in ('ntb_netdev', 'ntb_hw_epf', 'ntb_transport', 'ntb'):
        run(['rmmod', module], check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--evidence', required=True, type=Path)
    p.add_argument('--config', required=True, type=Path)
    p.add_argument('--uart-device', required=True)
    args = p.parse_args()
    config = json.loads(args.config.read_text())
    pf0 = config['host']['pf0_bdf']
    if not re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[01][0-9a-f]\.0', pf0):
        raise ValueError('invalid PF0 BDF')
    for field in ('host_interface', 'card_interface'):
        if not re.fullmatch('[A-Za-z0-9_.-]{1,15}', config['network'][field]):
            raise ValueError('invalid interface')
    out = args.evidence.resolve()
    if out.exists():
        raise ValueError('use a new journal directory')
    for pf, identity in [('0', '0xe200'), ('1', '0x8d91')]:
        d = Path('/sys/bus/pci/devices') / (pf0[:-1] + pf)
        if (d / 'vendor').read_text().strip() != '0x1957' or (d / 'device').read_text().strip() != identity:
            raise ValueError('unexpected target identity')
    if (Path('/sys/bus/pci/devices') / pf0 / 'driver').resolve().name != 'ntb_hw_epf':
        raise ValueError('expected vNTB host driver')
    out.mkdir(parents=True)

    def card(name, script):
        payload = out / (name + '.sh')
        payload.write_text(script)
        with (out / (name + '.log')).open('wb') as log:
            subprocess.run([sys.executable, str(HERE.parent / 'bringup/uart_run.py'), str(payload),
                            '--timeout', '30', '--device', args.uart_device], check=True, stdout=log, stderr=subprocess.STDOUT)

    # Card netdev removal joins its TX/RX work while host backing remains valid.
    quiesce_clients(card, host_interface=config['network']['host_interface'], card_interface=config['network']['card_interface'])
    observed = collect(pf0)
    (out / 'host-before-remove.json').write_text(json.dumps(observed, indent=2) + '\n')
    for pf in ('1', '0'):
        (Path('/sys/bus/pci/devices') / (pf0[:-1] + pf) / 'remove').write_text('1')
    card('card-unbind', CARD_UNBIND)
    print('Both clients quiesced and Endpoint unbound; routing cleanup remains explicit.')


if __name__ == '__main__':
    main()
