#!/usr/bin/env python3
"""Validate tracked build configuration and hardware bindings without downloads."""
import importlib.util
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
BOARD = HERE.parent
REPO = BOARD.parents[1]


def check():
    current = json.loads((HERE / 'current.json').read_text())
    lock = json.loads((HERE / 'inputs-v1.json').read_text())
    sdk = json.loads((BOARD / 'flexbuild/sdk-source-lock.json').read_text())
    artifacts = {item['name']: item for item in lock['artifacts']}
    if len(artifacts) != len(lock['artifacts']):
        raise ValueError('duplicate build inputs')
    for name, item in artifacts.items():
        if not re.fullmatch('[0-9a-f]{64}', item['sha256']) or item['size'] <= 0:
            raise ValueError('invalid pinned input: ' + name)
        if not item['url'].startswith('https://') or item['pin'] not in item['url']:
            raise ValueError('input URL must identify its public source pin: ' + name)
        path = Path(item['path'])
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('input path must be local to the cache')
    for name in ('atf', 'uboot', 'rcw', 'linux'):
        if artifacts[name]['pin'] != sdk['components'][name]['commit']:
            raise ValueError('SDK source pin mismatch: ' + name)
    mc = sdk['components']['mc_bin']
    if artifacts['mc']['pin'] != mc['commit'] or artifacts['mc']['sha256'] != mc['lx2160a_itb']['sha256']:
        raise ValueError('MC input pin mismatch')
    ddr = [item for name, item in artifacts.items() if name.startswith('ddr4_')]
    if len(ddr) != 8 or any(item['pin'] != sdk['components']['ddr_phy_bin']['commit'] for item in ddr):
        raise ValueError('DDR PHY pin mismatch')
    recipe = (HERE / 'Containerfile').read_text()
    if not re.search(r'^FROM docker.io/library/debian@sha256:[0-9a-f]{64}$', recipe, re.M):
        raise ValueError('toolchain base must use a registry digest')
    profile_dir = (REPO / current['profile']).resolve()
    if profile_dir.parent != BOARD / 'profiles':
        raise ValueError('profile must be directly under the board profile directory')
    spec = importlib.util.spec_from_file_location('profile_validator', BOARD / 'profiles/validate_profile.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    profile = json.loads((profile_dir / 'profile.json').read_text())
    paths = module.source_paths(profile_dir, profile)
    if module.verify_profile_contract(profile, paths) != current['board_profile_id']:
        raise ValueError('current profile identity mismatch')
    module.verify_evidence_contract(profile, json.loads((BOARD / 'hardware-evidence-v1.json').read_text()))
    module.verify_native18_rcw(paths['rcw'].read_text())
    print('current firmware source contract: PASS')


if __name__ == '__main__':
    check()
