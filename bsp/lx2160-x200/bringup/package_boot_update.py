#!/usr/bin/env python3
"""Package a fresh firmware producer with an actual current NOR/boot snapshot.

The current environment is retained byte-for-byte. Every other protected byte
must already match the producer, or packaging fails. No hardware is accessed.
"""
import argparse
import json
from pathlib import Path
import shutil
from boot_update import SIZE, sha, require, composition, manifest, plan_sectors


def preserve_environment(original, candidate):
    require(len(original) == len(candidate) == SIZE, 'incorrect 16 MiB NOR image size')
    merged = bytearray(candidate)
    merged[0x500000:0x520000] = original[0x500000:0x520000]
    sectors, expected = plan_sectors(original, bytes(merged))
    require(expected == merged, 'candidate changes protected regions outside firmware update ranges')
    require(sectors, 'no firmware sectors changed')
    return bytes(merged), sectors


def package(snapshot, candidate, current_boot, candidate_boot, producer_manifest, config, out):
    producer = json.loads(producer_manifest.read_text())
    require(producer.get('validation') == 'PASS' and producer.get('operation') == 'pack-independent-bank', 'unvalidated firmware producer')
    candidate_sha256 = producer['image']['sha256']
    original, fresh = snapshot.read_bytes(), candidate.read_bytes()
    require(len(fresh) == producer['image']['size'] and sha(fresh) == candidate_sha256, 'candidate differs from fresh producer digest')
    new, sectors = preserve_environment(original, fresh)
    site = json.loads(config.read_text())['boot_update']
    contract = {'schema_version':1, 'device':site['mtd_device'], 'bank_label':site['bank_label'],
                'site':site, 'producer_manifest_sha256':sha(producer_manifest.read_bytes()), 'producer_image_sha256':candidate_sha256, 'changed_sectors':sectors,
                'environment_preserved':True, 'protected_regions_preserved':True}
    for stage, image, boot in [('old',original,current_boot),('new',new,candidate_boot)]:
        embedded = manifest(image, 0x9c0000)
        profile, commit = embedded['board_profile_id'], embedded['build']['git_commit']
        composition(image, profile, commit)
        require(embedded['image_target']['physical_designator'] == site['bank_label'], 'candidate/current independent bank label differs')
        contract[stage] = {'profile':profile, 'commit':commit, 'flash_sha256':sha(image),
                           'boot_sha256':sha(boot.read_bytes())}
    out.mkdir(parents=True, exist_ok=False)
    (out/'firmware.bin').write_bytes(new)
    shutil.copyfile(candidate_boot,out/'boot.scr')
    shutil.copyfile(Path(__file__).with_name('boot_update.py'),out/'boot_update.py')
    (out/'deployment.json').write_text(json.dumps(contract,indent=2)+'\n')
    names=['firmware.bin','boot.scr','deployment.json','boot_update.py']
    (out/'SHA256SUMS').write_text(''.join(f'{sha((out/name).read_bytes())}  {name}\n' for name in names))
    return {'package':str(out), 'manifest_sha256':sha((out/'SHA256SUMS').read_bytes()),
            'changed_sectors':sectors, 'protected_regions_preserved':True}


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('current-snapshot','candidate-image','current-boot','candidate-boot','producer-manifest','config','out'):
        p.add_argument('--'+name,required=True,type=Path)
    a=p.parse_args()
    print(json.dumps(package(a.current_snapshot,a.candidate_image,a.current_boot,a.candidate_boot,a.producer_manifest,a.config,a.out),indent=2))
