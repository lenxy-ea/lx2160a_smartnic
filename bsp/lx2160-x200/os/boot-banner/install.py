#!/usr/bin/env python3
"""Install/remove the canonical banner in an offline mounted root filesystem."""
import argparse
from pathlib import Path
import shutil

SOURCE = Path(__file__).resolve().parent
FILES = {'usr/local/sbin/x200-boot-banner': ('x200-boot-banner', 0o755),
         'etc/systemd/system/x200-boot-banner.service': ('x200-boot-banner.service', 0o644)}
WANT = 'etc/systemd/system/multi-user.target.wants/x200-boot-banner.service'


def destination(root, relative):
    path = root / relative
    # Refuse rootfs links escaping the staging tree, including absolute links.
    if not path.parent.resolve().is_relative_to(root):
        raise ValueError(f'path escapes staging root: {relative}')
    if path.is_symlink():
        raise ValueError(f'refusing symlink destination: {relative}')
    return path


def install(root, remove=False):
    root = root.resolve(strict=True)
    if root == Path('/'):
        raise ValueError('--root must be an offline staging directory, not /')
    paths = {name: destination(root, name) for name in FILES}
    want = root / WANT
    if not want.parent.resolve().is_relative_to(root):
        raise ValueError('enablement directory escapes staging root')
    if want.exists() or want.is_symlink():
        if not want.is_symlink() or want.readlink() != Path('../x200-boot-banner.service'):
            raise ValueError('refusing to replace different service enablement')
    for name, path in paths.items():
        source, mode = FILES[name]
        if path.exists() and (not path.is_file() or path.read_bytes() != (SOURCE / source).read_bytes()):
            raise ValueError(f'existing file differs; save it before staging: {path}')
    if remove:
        want.unlink(missing_ok=True)
        for path in paths.values():
            path.unlink(missing_ok=True)
        return
    for name, path in paths.items():
        source, mode = FILES[name]
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / source, path)
        path.chmod(mode)
    want.parent.mkdir(parents=True, exist_ok=True)
    if not want.is_symlink():
        want.symlink_to('../x200-boot-banner.service')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--remove', action='store_true')
    args = parser.parse_args()
    try:
        install(args.root, args.remove)
    except (OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')
