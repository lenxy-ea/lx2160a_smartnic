#!/usr/bin/env python3
"""Read-only inspection of the two X200 redundant U-Boot environments.

Input must be a complete 16 MiB NOR image. Slot layout is the explicit X200
BSP design: 0x500000/0x510000, each 64 KiB, LE CRC32, flags, 65531 data bytes.
Selection mirrors env_check_redund() in env/common.c at pinned U-Boot commit
4ddbad60eff308a5b356fb9ab8734ac382ddd692. Syntax checks are independent of
CRC/serial selection; a selected malformed slot is never silently replaced.

Default JSON contains no variable names or values. --get NAME exposes only
that selected variable (UTF-8 text when possible, and exact bytes as hex).
Exit 0 means inspection completed, even if neither slot is valid. Exit 1
means I/O/size failure or a requested variable cannot be returned. No writes.
"""
# Layout authority: hardware-evidence-v1.json / x200-redundant-environment-v1.
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import zlib

NOR_SIZE = 16 * 1024 * 1024
SLOT_SIZE = 64 * 1024
OFFSETS = (0x500000, 0x510000)
SOURCE_COMMIT = '4ddbad60eff308a5b356fb9ab8734ac382ddd692'


def parse_data(data):
    """Validate double-NUL termination and unique nonempty key=value entries."""
    data = bytes(data)
    end = data.find(b'\0\0')
    if end < 0:
        return None, 'missing double-NUL terminator'
    entries = [] if end == 0 else data[:end].split(b'\0')
    variables = {}
    for index, entry in enumerate(entries):
        if b'=' not in entry:
            return None, f'entry {index}: missing equals sign'
        name, value = entry.split(b'=', 1)
        if not name:
            return None, f'entry {index}: empty key'
        if name in variables:
            return None, f'entry {index}: duplicate key'
        variables[name] = value
    return variables, None


def inspect_slot(raw, index):
    if len(raw) != SLOT_SIZE:
        raise ValueError('environment slot must be exactly 65536 bytes')
    stored, = struct.unpack_from('<I', raw)
    computed = zlib.crc32(raw[5:]) & 0xffffffff
    crc_valid = stored == computed
    variables, error = parse_data(raw[5:])
    info = {
        'slot': index, 'offset': f'0x{OFFSETS[index]:06x}',
        'sha256': hashlib.sha256(raw).hexdigest(),
        'state': 'blank' if raw == b'\xff' * SLOT_SIZE else ('valid' if crc_valid else 'corrupt'),
        'crc_valid': crc_valid, 'crc_stored': f'0x{stored:08x}',
        'crc_computed': f'0x{computed:08x}', 'flags': raw[4],
        'syntax_valid': error is None, 'syntax_error': error,
        'key_count': len(variables) if variables is not None else None,
    }
    return info, variables


def select_slot(slots):
    """Pinned upstream serial comparison, including the explicit FF/00 wrap."""
    a, b = slots
    if not a['crc_valid']:
        return 1 if b['crc_valid'] else None
    if not b['crc_valid']:
        return 0
    first, second = a['flags'], b['flags']
    if first == 255 and second == 0:
        return 1
    if second == 255 and first == 0:
        return 0
    return 1 if second > first else 0


def inspect_image(image, get=None):
    if len(image) != NOR_SIZE:
        raise ValueError(f'expected exactly {NOR_SIZE} NOR bytes, got {len(image)}')
    pairs = [inspect_slot(image[offset:offset + SLOT_SIZE], i)
             for i, offset in enumerate(OFFSETS)]
    slots = [pair[0] for pair in pairs]
    selected = select_slot(slots)
    report = {'selection_source': f'u-boot:{SOURCE_COMMIT}:env/common.c:env_check_redund',
              'nor_bytes': len(image), 'slots': slots, 'selected_slot': selected,
              'selection_status': 'valid' if selected is not None else 'invalid',
              'selected_syntax_valid': slots[selected]['syntax_valid'] if selected is not None else None}
    if get is not None:
        key = get.encode('utf-8')
        if not key or b'\0' in key or b'=' in key:
            raise ValueError('--get must name one nonempty environment key without NUL or equals')
        result = {'name': get}
        report['get'] = result
        if selected is None:
            result['status'] = 'no_crc_valid_slot'
        elif pairs[selected][1] is None:
            result['status'] = 'selected_slot_syntax_invalid'
        elif key not in pairs[selected][1]:
            result['status'] = 'absent'
        else:
            value = pairs[selected][1][key]
            result.update(status='found', value_hex=value.hex())
            try:
                result['value'] = value.decode('utf-8')
            except UnicodeDecodeError:
                result['value'] = None
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('nor', type=Path)
    parser.add_argument('--get', metavar='NAME')
    args = parser.parse_args()
    try:
        # The bound detects oversized input without reading an arbitrary file.
        with args.nor.open('rb') as source:
            image = source.read(NOR_SIZE + 1)
        report = inspect_image(image, args.get)
    except (OSError, ValueError) as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 1 if args.get is not None and report['get']['status'] != 'found' else 0


if __name__ == '__main__':
    sys.exit(main())
