#!/usr/bin/env python3
"""Generate bounded UART steps and verify captures for two same-boot env saves.

This helper never opens UART or executes commands. The operator/controller must
start at the U-Boot prompt, enforce each timeout, capture each step separately,
and stop on failure. Saves write the selected bank's environment; run only in an
authorized environment acceptance window. No reset, CRC corruption, raw NOR
write, identity change, or automatic cleanup is included.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import uuid

VARIABLE = 'x200_env_accept'
PROMPT = rb'(?:^|[\r\n])=> *$'


def plan(nonce=None):
    nonce = nonce or uuid.uuid4().hex
    if not re.fullmatch(r'[0-9a-f]{32}', nonce):
        raise ValueError('nonce must contain exactly 32 lowercase hexadecimal digits')
    steps = []
    for name, body in (
        ('preflight', 'env info && env info -p && env print x200_pcie_boot_policy'),
        ('save1', f'env set {VARIABLE} {nonce}_1 && env print {VARIABLE} && env save'),
        ('save2', f'env set {VARIABLE} {nonce}_2 && env print {VARIABLE} && env save'),
    ):
        token = f'X200_ENV_{nonce}_{name}'
        command = (f'echo {token}_BEGIN; if {body}; then echo {token}_OK; '
                   f'else echo {token}_FAIL; fi')
        steps.append({'name': name, 'command': command, 'timeout_seconds': 30,
                      'capture': name + '.raw'})
    return {'format': 1, 'nonce': nonce, 'variable': VARIABLE, 'steps': steps,
            'requires': 'Fresh U-Boot prompt, exclusive UART owner; execute sequentially in one boot',
            'writes': 'Two explicit env save commands; final persistent marker remains for reboot readback',
            'next_read_only_command': f'env print {VARIABLE}'}


def verify(spec, captures):
    expected = plan(spec['nonce'])
    if spec != expected:
        raise ValueError('plan differs from the fixed acceptance sequence')
    slots = []
    hashes = {}
    for step in expected['steps']:
        name = step['name']
        data = captures[name]
        token = f"X200_ENV_{expected['nonce']}_{name}".encode()
        def marker(suffix):
            return re.search(rb'(?:^|[\r\n])' + re.escape(token + suffix) + rb'\r?\n', data)
        begin, success = marker(b'_BEGIN'), marker(b'_OK')
        if not begin or not success or begin.start() >= success.start() or marker(b'_FAIL'):
            raise ValueError(f'{name}: missing fresh ordered success markers')
        outside = data[:begin.start()] + data[success.end():]
        if any(message in outside for message in
               (b'Saving Environment to', b'Erasing SPI flash...', b'Writing to SPI flash...')):
            raise ValueError(f'{name}: unexpected environment write outside explicit command')
        if not re.search(PROMPT, data[success.end():]):
            raise ValueError(f'{name}: final U-Boot prompt missing')
        body = data[begin.end():success.start() + 1]
        if re.search(rb'(?:^|[\r\n])(?:NOTICE: +BL2:|U-Boot |X200_RESET|Linux version)', body):
            raise ValueError(f'{name}: reboot occurred during same-boot acceptance')
        if name == 'preflight':
            for line in (b'Environment can be persisted', b'x200_pcie_boot_policy=linux-publish-only'):
                if not re.search(rb'(?:^|[\r\n])' + re.escape(line) + rb'\r?\n', body):
                    raise ValueError(f'{name}: precondition missing: {line!r}')
        else:
            number = name[-1]
            value = f"{VARIABLE}={spec['nonce']}_{number}".encode()
            if not re.search(rb'(?:^|[\r\n])' + re.escape(value) + rb'\r?\n', body):
                raise ValueError(f'{name}: exact variable print missing')
            matches = re.findall(rb'(?:^|[\r\n])Valid environment: ([12])\r?\n', body)
            if len(matches) != 1:
                raise ValueError(f'{name}: exactly one successful backend slot report required')
            for message in (b'Erasing SPI flash...', b'Writing to SPI flash...'):
                if message not in body:
                    raise ValueError(f'{name}: backend save evidence missing')
            slots.append(int(matches[0]))
        hashes[name] = hashlib.sha256(data).hexdigest()
    if slots[0] == slots[1]:
        raise ValueError('consecutive saves selected the same slot')
    return {'passed': True, 'nonce': spec['nonce'], 'slots': slots,
            'capture_sha256': hashes, 'scope': 'same-boot command/save evidence; reboot persistence and NOR isolation not checked'}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='action', required=True)
    create = sub.add_parser('plan')
    create.add_argument('--output', required=True, type=Path)
    check = sub.add_parser('verify')
    check.add_argument('--plan', required=True, type=Path)
    check.add_argument('--captures', required=True, type=Path)
    args = ap.parse_args()
    if args.action == 'plan':
        with args.output.open('x') as out:
            json.dump(plan(), out, indent=2)
            out.write('\n')
    else:
        spec = json.loads(args.plan.read_text())
        captures = {s['name']: (args.captures / (s['name'] + '.raw')).read_bytes()
                    for s in plan(spec['nonce'])['steps']}
        print(json.dumps(verify(spec, captures), indent=2))


if __name__ == '__main__':
    main()
