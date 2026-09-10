#!/usr/bin/env python3
"""Run a checksum-verified shell payload on X200 through its development host UART.

Run on the development host, not the target. Uses only the Python standard
library. Raw received bytes go to stdout; preserve them as the evidence log.
"""
import argparse
import base64
import fcntl
import gzip
import hashlib
import os
import re
import select
import sys
import termios
import time
import tty
import uuid
from pathlib import Path


def make_paste(payload, done, end):
    encoded = base64.encodebytes(gzip.compress(payload, mtime=0)).decode("ascii")
    return (
        "set -euo pipefail\n"
        "work=$(mktemp -d /tmp/x200-uart.XXXXXXXX)\n"
        f"trap 'rc=$?; rm -rf \"$work\"; printf \"\\n{done} status=%s\\n\" \"$rc\"' EXIT\n"
        "base64 -d <<'X200_PAYLOAD_BASE64' | gzip -dc >\"$work/payload.sh\"\n"
        f"{encoded}X200_PAYLOAD_BASE64\n"
        f"printf '%s  %s\\n' '{hashlib.sha256(payload).hexdigest()}' \"$work/payload.sh\" | sha256sum -c -\n"
        'bash "$work/payload.sh"\n'
        f"{end}\n"
    ).encode("ascii")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path)
    parser.add_argument("--device", required=True)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    payload = args.payload.read_bytes()
    nonce = uuid.uuid4().hex
    ready = f"X200_READY_{nonce}"
    done = f"X200_DONE_{nonce}"
    end = f"# X200_SERIAL_END_{nonce}"
    paste = make_paste(payload, done, end)
    receiver = (
        "( saved=$(stty -g); trap 'stty \"$saved\"' EXIT; stty -echo; "
        f"printf '\\n{ready}\\n'; sed '/^{end}$/q' | bash -s )\n"
    ).encode("ascii")
    device = Path(args.device).resolve(strict=True)
    # TIOCEXCL blocks later opens; first refuse a device already open by anyone.
    for process in Path('/proc').glob('[0-9]*'):
        for handle in (process / 'fd').glob('*'):
            try:
                if handle.resolve() == device:
                    raise RuntimeError(f"UART already open: {handle}")
            except (FileNotFoundError, PermissionError):
                continue
    fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    saved = termios.tcgetattr(fd)
    received = bytearray()

    def drain(delay):
        if select.select([fd], [], [], delay)[0]:
            data = os.read(fd, 65536)
            if not data:
                raise RuntimeError("UART disconnected")
            received.extend(data)
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def send(data):
        for offset in range(0, len(data), 16):
            remaining = memoryview(data)[offset:offset + 16]
            while remaining:
                try:
                    remaining = remaining[os.write(fd, remaining):]
                except BlockingIOError:
                    drain(0.02)
            # Long console printk stalls caused measured PL011 RX overruns
            # with 128-byte bursts. Pace smaller bursts and consume output.
            until = time.monotonic() + 0.005
            while time.monotonic() < until:
                drain(max(0, min(0.01, until - time.monotonic())))

    def wait_for(pattern, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = re.search(pattern, received)
            if match:
                return match
            drain(0.1)
        raise TimeoutError("UART marker missing; inspect the log before another command")

    exclusive = False
    try:
        fcntl.ioctl(fd, termios.TIOCEXCL)
        exclusive = True
        tty.setraw(fd, termios.TCSANOW)
        attrs = termios.tcgetattr(fd)
        attrs[4] = attrs[5] = termios.B115200
        attrs[2] &= ~(termios.CRTSCTS | termios.HUPCL)
        attrs[2] |= termios.CLOCAL | termios.CREAD
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        send(b'\r')
        wait_for(rb'root@lx2160ax:[^\r\n]*# ', 5)
        received.clear()
        send(receiver)
        wait_for(rb'(?:^|\n)' + ready.encode() + rb'\r?\n', 5)
        received.clear()
        send(paste)
        result = wait_for(rb'(?:^|\n)' + done.encode() + rb' status=(\d+)\r?\n', args.timeout)
        status = int(result.group(1))
        wait_for(rb'root@lx2160ax:[^\r\n]*# ', 5)
        return status
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
        finally:
            if exclusive:
                fcntl.ioctl(fd, termios.TIOCNXCL)
            os.close(fd)


if __name__ == '__main__':
    sys.exit(main())
