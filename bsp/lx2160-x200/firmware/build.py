#!/usr/bin/env python3
"""Build both native X200 NOR banks from verified sources and pinned firmware blobs."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tarfile

HERE = Path(__file__).resolve().parent
BOARD = HERE.parent
REPO = BOARD.parents[1]
INPUT_LOCK = HERE / 'inputs-v1.json'
CURRENT = HERE / 'current.json'
SDK_LOCK = BOARD / 'flexbuild/sdk-source-lock.json'
sys.path.insert(0, str(BOARD))
from build_time import build_environment, timestamp_utc
BUILDER = json.loads(CURRENT.read_text())['toolchain_container']


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read(path):
    return json.loads(path.read_text())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def record(path):
    return dict(path=str(path.resolve()), size=path.stat().st_size, sha256=sha(path))


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def verified_inputs(lock, directory):
    result = {}
    for item in lock['artifacts']:
        name = item['name']
        relative = Path(item['path'])
        path = (directory / relative).resolve()
        require(name not in result, 'duplicate input name: ' + name)
        require(not relative.is_absolute() and '..' not in relative.parts and
                path.is_relative_to(directory.resolve()), 'input path escapes directory: ' + str(relative))
        require(path.is_file(), 'missing pinned input: ' + str(path))
        require(sha(path) == item['sha256'], 'input checksum mismatch: ' + name)
        require('size' not in item or path.stat().st_size == item['size'], 'input size mismatch: ' + name)
        result[name] = dict(item, path=str(path))
    return result


def extract(item, destination):
    root = item['archive_root']
    require(Path(root).name == root and root not in ('', '.', '..'), 'invalid archive root')
    with tarfile.open(item['path']) as bundle:
        for member in bundle.getmembers():
            parts = Path(member.name).parts
            require(parts and parts[0] == root and '..' not in parts and not Path(member.name).is_absolute(),
                    'archive member escapes pinned root: ' + member.name)
        bundle.extractall(destination, filter='data')
    tree = destination / root
    require(tree.is_dir(), 'archive root missing after extraction')
    return tree


def compile_profile(output, inputs_dir):
    """Internal container stage; hash proof replaces Git metadata for archive sources."""
    current, sdk = read(CURRENT), read(SDK_LOCK)
    inputs = verified_inputs(read(INPUT_LOCK), inputs_dir)
    validator = load('firmware_profile', BOARD / 'profiles/validate_profile.py')
    profile_dir = BOARD / 'profiles' / current['board_profile_id']
    profile = read(profile_dir / 'profile.json')
    paths = validator.source_paths(profile_dir, profile)
    profile_id = validator.verify_profile_contract(profile, paths)
    evidence = validator.verify_evidence_contract(profile, read(BOARD / 'hardware-evidence-v1.json'))
    rcw = output / 'sources' / inputs['rcw']['archive_root']
    linux = output / 'sources' / inputs['linux']['archive_root']
    proof = dict(inputs['rcw'], commit=sdk['components']['rcw']['commit'])
    binary, audit, commit = validator.compile_rcw(paths['rcw'], rcw, output, None, archive_proof=proof)
    artifacts = dict(rcw_pbi=binary, rcw_reverse_audit=audit)
    for kind in ('dpc', 'dpl'):
        target = output / (kind + '.dtb')
        validator.compile_plain_dts(paths[kind], target)
        artifacts[kind] = target
    target = output / 'linux.dtb'
    validator.compile_linux_dts(paths['dtb'], linux, target)
    artifacts['dtb'] = target
    validator.verify_compiled_trees(profile_id, artifacts['dpc'], artifacts['dpl'], target)
    receipt = dict(schema_version=1, phase='profile', validation='PASS',
          board_profile_id=profile_id, profile_status=profile['status'], deployment_allowed=False,
          deployment_gates=profile['deployment_gates'], hardware_evidence=dict(
              registry=record(BOARD / 'hardware-evidence-v1.json'), bindings=profile['evidence_bindings'],
              source_references=evidence, contract_validation="PASS"), pinned_sources=dict(rcw_commit=commit,
              linux_commit=sdk['components']['linux']['commit']),
          archive_proofs={key: inputs[key] for key in ('rcw', 'linux')},
          sources={key: record(path) for key, path in paths.items()},
          artifacts={key: record(path) for key, path in artifacts.items()})
    write(output / 'profile-receipt.json', receipt)


def build(args):
    environment = build_environment(read(SDK_LOCK)['build_environment'])
    output, inputs_dir = args.output.resolve(), args.inputs_dir.resolve()
    require(output.is_relative_to(REPO / 'build') and not output.exists(), 'output must be a new directory under build/')
    require(args.jobs > 0, 'jobs must be positive')
    current, sdk = read(CURRENT), read(SDK_LOCK)
    require(current['toolchain_container'] == BUILDER, 'current builder pin mismatch')
    variant = current['firmware_variant']
    inputs = verified_inputs(read(INPUT_LOCK), inputs_dir)
    for key in ('atf', 'uboot', 'rcw', 'linux'):
        require(key in inputs, 'missing source archive: ' + key)
        require(sdk['components'][key]['commit'] in inputs[key]['url'], 'archive URL differs from SDK pin: ' + key)
    git = lambda *command: subprocess.check_output(['git', '-C', str(REPO), *command], text=True).strip()
    require(not git('status', '--porcelain', '--untracked-files=no'), 'commit tracked changes before building')
    commit = git('rev-parse', 'HEAD')
    tracked = [REPO / name for name in git('ls-files', 'bsp/lx2160-x200').splitlines()]
    inventory = [record(path) for path in tracked]
    inspected = subprocess.check_output(['podman', 'image', 'inspect', '--format', '{{.Id}}', BUILDER], text=True).strip()
    inspected = 'sha256:' + inspected.removeprefix('sha256:')
    require(len(inspected) == 71 and all(c in '0123456789abcdef' for c in inspected[7:]), 'builder image identity missing')
    output.mkdir(parents=True)
    write(output / 'build-time.json', dict(timestamp_utc=timestamp_utc(environment), build_environment=environment))
    sources = output / 'sources'
    sources.mkdir()
    trees = {key: extract(inputs[key], sources) for key in ('atf', 'uboot', 'rcw', 'linux')}
    installer = load('firmware_installer', BOARD / 'flexbuild/board-layer/install_profile.py')
    profile = read(BOARD / 'profiles' / current['board_profile_id'] / 'profile.json')
    banner = installer.BANNER.build_metadata(commit, environment['SOURCE_DATE_EPOCH'])
    installer.install_atf(trees['atf'], banner=banner)
    installer.install_uboot(trees['uboot'], profile, banner=banner)
    prefix = ['podman', 'run', '--rm', '--network=none', '--security-opt', 'label=disable',
              '-v', f'{REPO}:{REPO}:ro', '-v', f'{inputs_dir}:{inputs_dir}:ro',
              '-v', f'{output}:{output}:rw', '-w', str(REPO)]
    for key, value in environment.items():
        prefix += ['-e', f'{key}={value}']
    prefix += [inspected]
    commands = []
    def run(command, container=True):
        full = prefix + [str(part) for part in command] if container else [str(part) for part in command]
        commands.append(full)
        with (output / 'build.log').open('a') as log:
            result = subprocess.run(full, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            log.write(result.stdout)
        if result.returncode:
            raise RuntimeError('build command failed; inspect ' + str(output / 'build.log') + '\n' + result.stdout[-4000:])
        return result.stdout.strip()
    profile_command = ['python3', HERE / 'build.py', '_profile', '--output', output, '--inputs-dir', inputs_dir]
    run(profile_command)
    uout = output / 'uboot-output'
    umake = ['make', '-C', trees['uboot'], f'O={uout}', 'CROSS_COMPILE=aarch64-linux-gnu-']
    run(umake + ['lx2160x200_tfa_defconfig'])
    run(umake + [f'-j{args.jobs}'])
    make = ['make', '-C', trees['atf'], f'-j{args.jobs}', 'PLAT=lx2160x200',
            'CROSS_COMPILE=aarch64-linux-gnu-', 'BOOT_MODE=flexspi_nor', 'WARM_BOOT=no',
            'DEBUG=0', 'LOG_LEVEL=20', 'BUILD_STRING=' + variant,
            'BUILD_MESSAGE_TIMESTAMP=' + environment['BUILD_MESSAGE_TIMESTAMP']]
    profile_receipt = read(output / 'profile-receipt.json')
    rcw = Path(profile_receipt['artifacts']['rcw_pbi']['path'])
    ddr = inputs_dir / 'ddr-phy-binary/lx2160a'
    raw_ddr = [item for item in inputs.values() if Path(item['path']).parent == ddr]
    require(len(raw_ddr) == 8, 'DDR PHY requires eight checksum-pinned LX2160A training blobs')
    run(make + ['bl2', 'bl31'])
    run(make + ['pbl', f'RCW={rcw}'])
    run(make + ['fip', f'BL33={uout / "u-boot.bin"}'])
    run(make + ['fip_ddr', f'DDR_PHY_BIN_PATH={ddr}'])
    atf_out = trees['atf'] / 'build/lx2160x200/release'
    artifacts = dict(pbl=atf_out / 'bl2_flexspi_nor.pbl', bl2=atf_out / 'bl2.bin',
                     bl31=atf_out / 'bl31.bin', fip=atf_out / 'fip.bin',
                     ddr_phy_fip=atf_out / 'ddr_fip.bin', uboot=uout / 'u-boot.bin')
    ddr_check = output / 'ddr-fip-audit'
    ddr_check.mkdir()
    run([trees['atf'] / 'tools/fiptool/fiptool', 'unpack', '--out', ddr_check, artifacts['ddr_phy_fip']])
    require(sorted(sha(path) for path in ddr_check.iterdir()) == sorted(item['sha256'] for item in raw_ddr),
            'DDR FIP payloads differ from the eight pinned training blobs')
    audit = audit_boot(run, trees, uout, artifacts, profile)
    audit['boot_banner'] = audit_banner(
        {name: artifacts[name].read_bytes() for name in ('bl2', 'bl31', 'uboot')}, banner)
    run([uout / 'tools/mkimage', '-A', 'arm64', '-O', 'linux', '-T', 'script', '-C', 'none',
         '-n', variant, '-d', BOARD / 'os/x200-sata-boot.cmd', output / 'boot.scr'])
    write(output / 'boot-receipt.json', dict(schema_version=1, phase=current['boot_phase'],
          operation='build-native-boot-chain', validation='PASS', board_profile_id=current['board_profile_id'],
          firmware_variant=variant, native_uboot_config='lx2160x200_tfa_defconfig',
          deployment_allowed=False, source_git_commit=commit, builder_image_id=inspected, build_environment=environment,
          boot_banner=banner,
          inputs=inputs, audit=audit, artifacts={name: record(path) for name, path in artifacts.items()}))
    mc = [Path(item['path']) for item in inputs.values() if Path(item['path']).name == 'mc_lx2160a_10.40.0.itb']
    require(len(mc) == 1, 'one checksum-pinned MC 10.40.0 ITB is required')
    for bank in ('D11', 'D12'):
        command = ['python3', BOARD / 'flash-layout/pack_flash.py', '--bank', bank,
                   '--output-dir', output / 'images', '--profile-receipt', output / 'profile-receipt.json',
                   '--boot-receipt', output / 'boot-receipt.json', '--mc', mc[0]]
        for name, path in artifacts.items():
            command += ['--' + name.replace('_', '-'), path]
        for name in ('dpc', 'dpl', 'dtb'):
            command += ['--' + name, profile_receipt['artifacts'][name]['path']]
        # Packing uses Python byte operations plus host Git metadata; all
        # compiler/device-tree tools above run in the pinned container.
        run(command, container=False)
    require(commit == git('rev-parse', 'HEAD') and not git('status', '--porcelain', '--untracked-files=no'),
            'committed source changed during build')
    require(inventory == [record(path) for path in tracked], 'source inventory changed during build')
    require(inputs == verified_inputs(read(INPUT_LOCK), inputs_dir), 'binary/source inputs changed during build')
    toolchain = run(['aarch64-linux-gnu-gcc', '--version'])
    packages = run(['dpkg-query', '-W'])
    (output / 'builder-packages.txt').write_text(packages + '\n')
    write(output / 'manifest.json', dict(schema_version=1,
          operation='source-build-current-firmware',
          validation='PASS', board_profile_id=current['board_profile_id'], firmware_variant=variant,
          source_git_commit=commit, builder_image_id=inspected, toolchain=toolchain, hardware_access_performed=False,
          deployment_allowed=False, reliability_qualified=False, inherited_nor_components=[],
          inputs=inputs, source_inventory=inventory, commands=commands, build_environment=environment,
          artifacts=[record(path) for path in sorted((output / 'images').glob('*'))] +
                    [record(output / name) for name in ('boot.scr', 'profile-receipt.json', 'boot-receipt.json', 'builder-packages.txt', 'build-time.json')]))
    print(output / 'manifest.json')


def audit_banner(binaries, metadata):
    for component, stage in (('bl2', 'TF-A/BL2'), ('bl31', 'TF-A/BL31'), ('uboot', 'U-Boot')):
        data = binaries[component]
        for token in (stage, metadata['bsp_git'], metadata['built_utc']):
            require(token.encode('ascii') in data, component + ' missing banner input: ' + token)
        require(b'physical_mapping=' not in data, component + ' contains a removed mapping notice')
    require(b'[RhineLab X200] LX2160A SmartNIC' in binaries['uboot'], 'U-Boot identity table missing')
    for data in binaries.values():
        require(b'event=enter schema=1' not in data, 'obsolete event banner remains')
    return dict(validation='PASS', schema=1, presentation='human-readable', components=['bl2', 'bl31', 'uboot'],
                bsp_git=metadata['bsp_git'], built_utc=metadata['built_utc'],
                scope='compiled banner presence and exact build metadata; target output separately qualified')


def audit_boot(run, trees, uout, artifacts, profile):
    audit = load('firmware_uboot_audit', HERE / 'audit_uboot.py')
    registry = read(BOARD / 'hardware-evidence-v1.json')
    config = (uout / '.config').read_text().splitlines()
    binary = artifacts['uboot'].read_bytes()
    symbols = run(['aarch64-linux-gnu-nm', uout / 'u-boot'])
    decoded = run([uout / 'scripts/dtc/dtc', '-I', 'dtb', '-O', 'dts', uout / 'u-boot.dtb'])
    (uout.parent / 'u-boot.decoded.dts').write_text(decoded + '\n')
    for mac in range(3, 7):
        mode = run(['fdtget', uout / 'u-boot.dtb', f'/fsl-mc@80c000000/dpmacs/dpmac@{mac}', 'phy-connection-type'])
        require(mode == ('xgmii' if mac < 5 else '25g-aui'), 'compiled U-Boot native18 MAC mode mismatch')
    audit.audit_serdes2_rc(config, symbols, decoded)
    require('CONFIG_PCIE_LAYERSCAPE_GEN4=y' not in config and 'CONFIG_PCI_ENDPOINT=y' in config and
            'CONFIG_PCIE_LAYERSCAPE_EP=y' in config, 'U-Boot endpoint driver configuration mismatch')
    require(' x200_flash_verify' in symbols and b'if x200_flash_verify && scsi reset' in binary,
            'U-Boot must verify the Flash composition before SATA boot')
    require(all('CONFIG_' + name + '=y' in config for name in ('DM_RNG', 'FSL_CAAM_RNG')),
            'U-Boot must enable the direct CAAM hardware RNG provider')
    require(all(' ' + name in symbols for name in ('fdt_kaslrseed', 'caam_rng_read', 'dm_rng_read')),
            'U-Boot must link the CAAM RNG and Linux KASLR handoff')
    result = dict(flash_verify_before_sata='PASS', rx=audit.audit_rx_auto(profile, registry, symbols, binary),
        psci=audit.audit_psci(registry, config, symbols, decoded),
        identity=audit.audit_identity(registry, config, symbols, binary),
        environment=audit.audit_environment(registry, config, binary, sha(trees['uboot'] / 'env/sf.c')),
        publication=audit.audit_linux_publication(registry, symbols, binary,
            (trees['uboot'] / 'drivers/pci/pcie_layerscape_x200_ep.c').read_text()))
    bl2, bl31 = artifacts['bl2'].read_bytes(), artifacts['bl31'].read_bytes()
    require(artifacts['pbl'].read_bytes()[0x9000:] == bl2, 'PBL/BL2 placement mismatch')
    for marker in (b'DDR PHY %d %s training passed', b'DDR PHY %d %s training failed', b'DDR init failed: %lld'):
        require(marker in bl2, 'BL2 DDR training/failure marker absent')
    require(b'X200_RESET CPLD request complete' in bl31 and bytes.fromhex('20004c0099ff') in bl31,
            'BL31 native CPLD reset implementation absent')
    fiptool = trees['atf'] / 'tools/fiptool/fiptool'
    unpacked = uout.parent / 'fip-audit'
    unpacked.mkdir()
    run([fiptool, 'unpack', '--out', unpacked, artifacts['fip']])
    require((unpacked / 'soc-fw.bin').read_bytes() == bl31 and
            (unpacked / 'nt-fw.bin').read_bytes() == binary, 'FIP differs from newly built BL31/U-Boot')
    elf = artifacts['bl31'].parent / 'bl31/bl31.elf'
    assembly = run(['aarch64-linux-gnu-objdump', '-d', elf])
    psci = assembly.split('<_psci_system_reset>:', 1)[1].split('\n\n', 1)[0]
    require('<x200_system_reset>' in psci, 'BL31 PSCI hook does not call X200 CPLD reset')
    console = (trees['atf'] / 'drivers/nxp/console/console_pl011.c').read_text()
    require('CONSOLE_FLAG_RUNTIME | CONSOLE_FLAG_CRASH' in console, 'BL31 runtime console absent')
    (uout.parent / 'bl31.disassembly.txt').write_text(assembly + '\n')
    result['bl2_training'] = 'PASS'
    result['bl31_cpld_reset'] = 'PASS'
    result['fip_payloads'] = 'PASS'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', nargs='?', choices=('_profile',))
    parser.add_argument('--inputs-dir', type=Path, default=REPO / 'build/lx2160-x200/firmware-inputs')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args()
    if args.stage == '_profile':
        compile_profile(args.output.resolve(), args.inputs_dir.resolve())
    else:
        build(args)


if __name__ == '__main__':
    main()
