#!/usr/bin/env python3
"""Collect routine interface/route/driver counters through standard Linux interfaces."""
import argparse
import json
from pathlib import Path
import subprocess


def collect(interfaces):
    commands = [['uname', '-a'], ['ip', '-j', 'address'], ['ip', '-j', 'route', 'show', 'table', 'all']]
    for name in interfaces:
        if not name or len(name) > 15 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for c in name):
            raise ValueError('invalid interface name')
        commands += [['ethtool', '-i', name], ['ethtool', name], ['ethtool', '-S', name]]
    records = []
    for command in commands:
        result = subprocess.run(command, text=True, capture_output=True, timeout=15)
        records.append({'command': command, 'returncode': result.returncode,
                        'stdout': result.stdout, 'stderr': result.stderr})
    return {'schema_version': 1, 'read_only': True, 'observations': records}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--interface', action='append', required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args(); a.output.write_text(json.dumps(collect(a.interface), indent=2) + '\n')
