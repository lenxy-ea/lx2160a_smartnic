"""Generate component banner inputs from the enclosing BSP build invocation."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re

def build_metadata(bsp_git: str, epoch: str) -> dict[str, str]:
    if not isinstance(bsp_git, str) or not re.fullmatch(r'[0-9a-f]{40}', bsp_git):
        raise ValueError('banner requires the full BSP source Git commit')
    if not isinstance(epoch, str) or not re.fullmatch(r'[0-9]+', epoch) or not 0 <= int(epoch) <= 0xffffffff:
        raise ValueError('banner requires the build SOURCE_DATE_EPOCH')
    stamp = datetime.fromtimestamp(int(epoch), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return dict(bsp_git=bsp_git, built_utc=stamp)


def install(tree: Path, metadata: dict[str, str]) -> None:
    """One generated header for this component; never discover enclosing Git."""
    if set(metadata) != {'bsp_git', 'built_utc'}:
        raise ValueError('banner metadata must contain bsp_git and built_utc')
    stamp = datetime.strptime(metadata['built_utc'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    if build_metadata(metadata['bsp_git'], str(int(stamp.timestamp()))) != metadata:
        raise ValueError('banner build metadata is not canonical')
    include = tree / 'include'
    include.mkdir(parents=True, exist_ok=True)
    contents = ('/* Generated from this component build invocation. */\n'
                '#ifndef X200_BOOT_BANNER_BUILD_H\n#define X200_BOOT_BANNER_BUILD_H\n'
                '#define X200_BANNER_BSP_GIT ' + json.dumps(metadata['bsp_git']) + '\n'
                '#define X200_BANNER_BUILT_UTC ' + json.dumps(metadata['built_utc']) + '\n'
                '#endif\n')
    (include / 'x200_boot_banner_build.h').write_text(contents, encoding='ascii')
