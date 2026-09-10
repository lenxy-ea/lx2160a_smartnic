#!/usr/bin/env python3
"""One UTC timestamp per build invocation, inherited by compiler/packer children."""
import argparse
import datetime as dt
import json
import os
import re
import shlex
import time

MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


def build_environment(base=None):
    """Use wall clock by default; an explicit epoch can reproduce an archived build.

    Set the process epoch once so subprocesses and subsequent components use
    the same instant. Lock-file timestamps are never an input to this choice.
    Other environment identity/configuration fields remain as supplied.
    """
    value = os.environ.get('SOURCE_DATE_EPOCH')
    if value is None:
        value = str(int(time.time()))
    if not re.fullmatch(r'[0-9]+', value) or not 0 <= int(value) <= 0xffffffff:
        raise ValueError('SOURCE_DATE_EPOCH must be an unsigned 32-bit Unix timestamp')
    epoch = int(value)
    stamp = dt.datetime.fromtimestamp(epoch, dt.timezone.utc)
    clock = f'{stamp.hour:02}:{stamp.minute:02}:{stamp.second:02}'
    month = MONTHS[stamp.month - 1]
    result = dict(base or {})
    result.update(SOURCE_DATE_EPOCH=str(epoch), TZ='UTC',
                  KBUILD_BUILD_TIMESTAMP=f'{WEEKDAYS[stamp.weekday()]} {month} {stamp.day:02} {clock} UTC {stamp.year}',
                  BUILD_MESSAGE_TIMESTAMP=f'"{clock}, {month} {stamp.day:02} {stamp.year}"')
    os.environ['SOURCE_DATE_EPOCH'] = str(epoch)
    return result


def timestamp_utc(environment):
    return dt.datetime.fromtimestamp(int(environment['SOURCE_DATE_EPOCH']), dt.timezone.utc).isoformat().replace('+00:00', 'Z')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shell', action='store_true', help='emit safely quoted exports for shell build wrappers')
    args = parser.parse_args()
    environment = build_environment()
    if args.shell:
        print('\n'.join(f'export {key}={shlex.quote(value)}' for key, value in environment.items()))
    else:
        print(json.dumps(environment, indent=2))
