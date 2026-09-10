#!/usr/bin/env python3
"""Execute one explicitly supplied U-Boot command with fresh bounded UART evidence.

Requires an existing prompt. Does not retry, reboot or boot Linux implicitly.
The supplied command may write hardware; this tool does not authorize its use.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import select
import termios
import time
import tty
import uuid

PROMPT = rb'(?:^|[\r\n])=> *$'


def reject_existing_users(device):
    for process in Path('/proc').glob('[0-9]*'):
        for handle in (process / 'fd').glob('*'):
            try:
                if handle.resolve() == device:
                    raise RuntimeError(f'UART already open: {handle}')
            except (FileNotFoundError, PermissionError):
                continue


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--device', required=True)
    ap.add_argument('--command', required=True)
    ap.add_argument('--evidence', required=True, type=Path)
    ap.add_argument('--timeout', default=30, type=int)
    args = ap.parse_args()
    if not 1 <= args.timeout <= 60 or '\n' in args.command or '\r' in args.command:
        ap.error('one line only; timeout must be 1..60 seconds')
    nonce = 'X200_CMD_' + uuid.uuid4().hex
    wire = f'echo {nonce}_BEGIN; if {args.command}; then echo {nonce}_OK; else echo {nonce}_FAIL; fi\r'.encode()
    if len(wire) >= 512:
        ap.error('wrapped command exceeds X200 CONFIG_SYS_CBSIZE=512')
    device = Path(args.device).resolve(strict=True)
    reject_existing_users(device)
    with args.evidence.open('xb') as log:
        fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        saved = termios.tcgetattr(fd)
        exclusive = False
        data = bytearray()
        try:
            fcntl.ioctl(fd, termios.TIOCEXCL)
            exclusive = True
            tty.setraw(fd, termios.TCSANOW)
            attrs = termios.tcgetattr(fd)
            attrs[4] = attrs[5] = termios.B115200
            attrs[2] &= ~(termios.CRTSCTS | termios.HUPCL)
            attrs[2] |= termios.CLOCAL | termios.CREAD
            termios.tcsetattr(fd, termios.TCSANOW, attrs)

            def drain(delay):
                if not select.select([fd], [], [], delay)[0]:
                    return False
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise RuntimeError('UART disconnected')
                log.write(chunk); log.flush(); data.extend(chunk)
                return True

            def send(payload):
                for offset in range(0, len(payload), 16):
                    pending = memoryview(payload[offset:offset + 16])
                    while pending:
                        if time.monotonic() >= deadline:
                            raise TimeoutError('UART send timeout')
                        try:
                            pending = pending[os.write(fd, pending):]
                        except BlockingIOError:
                            drain(0.01)
                    drain(0.01)

            deadline = time.monotonic() + 5
            while drain(0.05):
                if time.monotonic() >= deadline:
                    raise RuntimeError('UART not quiet')
            data.clear()
            # An empty Enter repeats U-Boot's previous command (including writes).
            # Cancel the current line instead, then require a fresh prompt.
            send(b'\x03')
            while not re.search(PROMPT, data):
                if time.monotonic() >= deadline:
                    raise TimeoutError('no existing U-Boot prompt')
                drain(0.05)
            data.clear()
            deadline = time.monotonic() + args.timeout
            send(wire)
            begin = rb'(?:^|[\r\n])' + nonce.encode() + rb'_BEGIN\r?\n'
            end = rb'(?:^|[\r\n])' + nonce.encode() + rb'_(OK|FAIL)\r?\n'
            while time.monotonic() < deadline:
                start = re.search(begin, data)
                finish = re.search(end, data)
                if start and finish and finish.start() > start.start() and re.search(PROMPT, data[finish.end():]):
                    if finish[1] != b'OK':
                        raise RuntimeError('command returned failure; evidence retained')
                    report = dict(status='PASS', command=args.command, nonce=nonce,
                                  scope='fresh command completion; inspect command-specific output')
                    args.evidence.with_suffix(args.evidence.suffix + '.json').write_text(json.dumps(report, indent=2) + '\n')
                    print(json.dumps(report)); return
                drain(0.05)
            raise TimeoutError('command completion missing; no retry')
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
            if exclusive:
                fcntl.ioctl(fd, termios.TIOCNXCL)
            os.close(fd)


if __name__ == '__main__':
    main()
