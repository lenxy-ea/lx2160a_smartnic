#!/usr/bin/env python3
"""Install a vetted card bundle without starting services or rebooting.

Usage: python3 -B card-install.py BUNDLE EXPECTED_SHA256SUMS_DIGEST
The caller must verify that digest through the trusted packaging receipt.
"""
import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def runtime_from(bundle):
    spec = importlib.util.spec_from_file_location('card_runtime', bundle / 'card-runtime.py')
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    return runtime


def preflight_owned(path, data, previous=None):
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.read_bytes() not in (data, previous))):
        raise ValueError(f'refusing to replace a different installed configuration: {path}')


def install_owned(path, data, previous=None):
    preflight_owned(path, data, previous)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o644)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def render_unit(bundle, digest):
    return (bundle / 'card.service').read_text().replace('@BUNDLE@', str(bundle)).replace('@SHA256@', digest).encode()


def previous_owned_unit(path, desired, base, runtime):
    """Authorize replacement only of a unit exactly rendered from its old bundle."""
    if not path.exists() or path.is_symlink():
        preflight_owned(path, desired)
        return None
    previous = path.read_bytes()
    if previous == desired:
        return None
    pattern = (r'ExecStart=/usr/bin/python3 -B ' + re.escape(str(base)) +
               r'/([0-9a-f]{64})/card-runtime\.py ' + re.escape(str(base)) +
               r'/\1 \1')
    matches = [re.fullmatch(pattern, line) for line in previous.decode().splitlines()]
    matches = [match for match in matches if match]
    if len(matches) != 1:
        raise ValueError('installed unit does not reference one owned content-addressed bundle')
    digest = matches[0].group(1)
    prior_bundle = base / digest
    if prior_bundle.is_symlink():
        raise ValueError('prior immutable bundle path is a symlink')
    # The prior package is immutable evidence, not the new runtime inventory.
    manifest = prior_bundle / 'SHA256SUMS'
    if manifest.is_symlink() or hashlib.sha256(manifest.read_bytes()).hexdigest() != digest:
        raise ValueError('prior bundle manifest digest mismatch')
    records = {}
    for line in manifest.read_text().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9_.-]+)', line)
        if not match or match[2] in records:
            raise ValueError('invalid prior bundle manifest')
        checksum, name = match.groups()
        asset = prior_bundle / name
        if asset.is_symlink() or not asset.is_file() or hashlib.sha256(asset.read_bytes()).hexdigest() != checksum:
            raise ValueError('prior bundle asset mismatch')
        records[name] = checksum
    if 'card.service' not in records:
        raise ValueError('prior bundle has no verified unit')
    if render_unit(prior_bundle, digest) != previous:
        raise ValueError('installed unit differs from its verified prior bundle template')
    return previous


def verified_runtime(bundle, expected):
    # Verify the digest before importing the staged runtime helper. This is an
    # integrity gate, not a signature: the digest comes from the review receipt.
    import hashlib
    manifest = (bundle / 'SHA256SUMS').read_bytes()
    if hashlib.sha256(manifest).hexdigest() != expected:
        raise ValueError('staged SHA256SUMS digest mismatch')
    lines = manifest.decode().splitlines()
    runtime_hash = hashlib.sha256((bundle / 'card-runtime.py').read_bytes()).hexdigest()
    if f'{runtime_hash}  card-runtime.py' not in lines:
        raise ValueError('staged runtime hash mismatch')
    runtime = runtime_from(bundle)
    runtime.verify_bundle(bundle, expected)
    return runtime


def publish_verified_bundle(bundle, expected):
    """Publish owned files for boot; never load modules or start the service.

    A kernel-upgrade transaction verifies the old running kernel and the new
    boot Image before calling this file-only publication step. The ordinary
    service installer below always requires the bundle's running kernel.
    """
    runtime = verified_runtime(bundle, expected)
    base = Path('/usr/lib/x200-pcie')
    destination = base / expected
    unit = (bundle / 'card.service').read_text().replace('@BUNDLE@', str(destination)).replace('@SHA256@', expected)
    unit_path = Path('/etc/systemd/system/x200-pcie-card.service')
    previous_unit = previous_owned_unit(unit_path, unit.encode(), base, runtime)
    configuration = {Path(target): (bundle / name).read_bytes() for name, target in runtime.CONFIGS.items()}
    configuration[unit_path] = unit.encode()
    # Refuse foreign files before publishing any installation state.
    for path, data in configuration.items():
        preflight_owned(path, data, previous_unit if path == unit_path else None)
    base.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.is_symlink():
            raise ValueError('immutable bundle path is a symlink')
        runtime.verify_bundle(destination, expected)
    else:
        temporary = Path(tempfile.mkdtemp(prefix='.card-', dir=base))
        try:
            for name in runtime.ASSETS | {m[0] for m in runtime.MODULES.values()} | {'SHA256SUMS'}:
                shutil.copyfile(bundle / name, temporary / name)
                (temporary / name).chmod(0o444)
            runtime.verify_bundle(temporary, expected)
            temporary.chmod(0o555)
            temporary.rename(destination)
        finally:
            if temporary.exists():
                temporary.chmod(0o700)
                shutil.rmtree(temporary)
    for path, data in configuration.items():
        install_owned(path, data, previous_unit if path == unit_path else None)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', 'x200-pcie-card.service'], check=True)
    print(f'Installed {destination}; service enabled for boot, not started')
    print('Activate/adopt explicitly with: systemctl start x200-pcie-card.service')


def install(bundle, expected):
    verified_runtime(bundle, expected).kernel_gate()
    publish_verified_bundle(bundle, expected)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bundle', type=Path)
    parser.add_argument('manifest_sha256')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('root is required')
    install(args.bundle.resolve(), args.manifest_sha256)
