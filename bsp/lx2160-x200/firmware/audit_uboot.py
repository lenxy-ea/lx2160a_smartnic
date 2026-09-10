"""Validate the newly compiled U-Boot artifacts against the board contract."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
HERE = Path(__file__).resolve().parent
LAYER = HERE.parent / 'flexbuild/board-layer'
PERSISTENT_FACT = 'x200-pcie-persistent-linux-publication-design'
RX_FACT = 'x200-rx-auto-startup-default'
NATIVE18_FACT = 'x200-dual-rate-native18-design'
def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def audit_rx_auto(profile, registry, symbols, binary, stale_header=False):
    if (profile.get('serdes', {}).get('s1', {}).get('rx_gain_k2') != 'automatic' or
            profile.get('evidence_bindings', {}).get('serdes1_rx_gain') != [RX_FACT, NATIVE18_FACT] or
            profile.get('serdes', {}).get('s1', {}).get('rx_auto_lanes') != [4, 5] or
            profile.get('serdes', {}).get('s1', {}).get('source_value') != 18 or
            profile.get('ddr_mt_s') != 3200):
        raise ValueError('build requires the automatic RX profile and startup evidence binding')
    fact = registry.get('facts', {}).get(RX_FACT, {})
    value = fact.get('value', {})
    if (fact.get('authority') != 'explicit-smartnic-design-delta' or
            fact.get('implementation_use') != 'allowed-with-target-gate' or
            value.get('k2_mode') != 'automatic' or
            value.get('k2_lanes4_to7') != ['automatic'] * 4 or
            value.get('expected_recr0_lanes4_to7') != ['0x00000085'] * 4):
        raise ValueError('registry does not authorize the four-lane automatic RX startup default')
    design = registry.get('facts', {}).get(NATIVE18_FACT, {})
    clock = design.get('value', {}).get('clock_selection', {})
    if (design.get('authority') != 'explicit-smartnic-design-delta' or
            design.get('implementation_use') != 'allowed-with-target-gate' or
            clock.get('SRDS_PRTCL_S1') != 18 or clock.get('MEM_PLL_RAT') != 32 or
            clock.get('MEM2_PLL_RAT') != 32):
        raise ValueError('registry does not authorize native18 DDR3200 RX lane selection')
    names = {line.split()[-1] for line in symbols.splitlines() if line.split()}
    if 'x200_apply_rx_auto' not in names or 'x200_apply_rx_gains' in names:
        raise ValueError('ELF automatic RX helper missing or superseded forced helper linked')
    for marker in (b'X200_RX_AUTO_PASS lanes=4,5\n\0', b'X200_RX_AUTO_APPLIED',
                   b'X200_RX_AUTO_FAILED'):
        if marker not in binary:
            raise ValueError('U-Boot binary misses automatic RX marker ' + marker.decode())
    if stale_header or b'X200_RX_GAIN_' in binary:
        raise ValueError('superseded forced RX header or binary markers remain')
    return dict(mode='automatic', lanes=[4, 5], evidence_facts=[RX_FACT, NATIVE18_FACT],
                helper='x200_apply_rx_auto', recr0=['0x00000085'] * 2,
                forced_helper_absent=True, forced_header_absent=True)


def audit_serdes2_rc(config, symbols, decoded):
    """Check the compiled RC driver and resource map without touching the EP."""
    if ('CONFIG_PCIE_LAYERSCAPE_RC=y' not in config or
            'CONFIG_FSL_PCIE_COMPAT="fsl,ls2088a-pcie"' not in config or
            ' ls_pcie_probe' not in symbols or ' ft_pci_setup_ls' not in symbols):
        raise ValueError('S2 configuration requires the DWC RC driver and Linux fixups')
    match = re.search(r'pcie@3600000\s*\{([^}]+)\}', decoded)
    if not match:
        raise ValueError('S2 configuration compiled PCIe3 node missing')
    node = match.group(1)
    # dtc versions render identical string lists and integers differently.
    for name, expected in {'compatible': ['fsl,ls-pcie'],
                           'reg-names': ['regs', 'lut', 'ctrl', 'config'],
                           'status': ['okay']}.items():
        prop = re.search(r'\b' + re.escape(name) + r'\s*=\s*([^;]+);', node)
        values = [part for text in re.findall(r'"([^"]*)"', prop.group(1) if prop else '')
                  for part in text.split(r'\0')]
        if values != expected:
            raise ValueError(f'S2 configuration compiled PCIe3 {name} mismatch')
    for name, expected in (('num-lanes', 4), ('max-link-speed', 3)):
        prop = re.search(r'\b' + name + r'\s*=\s*<([^>]+)>;', node)
        if not prop or [int(cell, 0) for cell in prop.group(1).split()] != [expected]:
            raise ValueError(f'S2 configuration compiled PCIe3 {name} mismatch')
    cells = re.search(r'\breg = <([^>]+)>;', node)
    expected = [0, 0x3600000, 0, 0x80000, 0, 0x3680000, 0, 0x40000,
                0, 0x36c0000, 0, 0x40000, 0x90, 0, 0, 0x2000]
    if not cells or [int(x, 0) for x in cells.group(1).split()] != expected:
        raise ValueError('S2 configuration compiled PCIe3 DWC resource map mismatch')
    return {'validation': 'PASS', 'scope': 'PCIe3 DWC RC resources and Linux fixups; target rate unmeasured'}


def audit_linux_publication(registry, symbols, binary, source):
    fact = registry.get('facts', {}).get(PERSISTENT_FACT, {})
    value = fact.get('value', {})
    if (fact.get('authority') != 'explicit-smartnic-design-delta' or
            fact.get('implementation_use') != 'allowed-with-target-gate' or
            value.get('ready_policy') != 'linux-publish-only' or
            value.get('enabled_inbound_windows') != 0 or
            value.get('enabled_outbound_windows') != 0 or
            value.get('pf_bar_masks') != 'all-zero'):
        raise ValueError('registry does not authorize persistent Linux publication')
    names = {line.split()[-1] for line in symbols.splitlines() if line.split()}
    if ('x200_pcie_ep_probe' not in names or 'x200_setup_bar0' in names or
            'ls_pcie_ep_probe' in names or any('ls_pcie_g4' in n for n in names)):
        raise ValueError('unexpected endpoint publication helper')
    for marker in (b'X200-PCIE: Linux publication deferred; CFG_READY=0',
                   b'inbound=off outbound=off', b'linux-publish-only',
                   b'x200_pcie_boot_policy'):
        if marker not in binary:
            raise ValueError('U-Boot binary misses publication marker ' + marker.decode())
    if b'DWC cold bootstrap ready' in binary or b'PF0 BAR0=8KiB' in binary:
        raise ValueError('superseded boot PIO publication remains')
    writes = re.findall(r'ctrl_writel\(pcie, ([^,]+), PCIE_PF_CONFIG\)', source)
    if writes != ['ready & ~PCIE_CONFIG_READY']:
        raise ValueError('unexpected CFG_READY write policy')
    if ('x200_setup_bar0' in source or 'memset(' in source or
            'flush_dcache_range' in source or
            re.search(r'dbi_writel\(pcie, (?!0,)[^;]*PCIE_ATU_CR2\)', source)):
        raise ValueError('boot PIO backing or enabled iATU remains')
    if not re.search(r'writel\(0,\s*dbi \+ PCIE_NO_SRIOV_BAR_BASE \+ PCI_BASE_ADDRESS_0', source):
        raise ValueError('all-zero PF BAR mask policy missing')
    return dict(evidence_fact=PERSISTENT_FACT, ready_policy='linux-publish-only',
                pf_bar_masks='all-zero', enabled_inbound_windows=0,
                enabled_outbound_windows=0, boot_memory_access=False)


def audit_psci(registry, config, symbols, decoded_dtb):
    """Audit dtc-decoded compiled control DT and the linked PSCI implementation."""
    fact_id = 'x200-psci-smc-reset-contract'
    fact = registry.get('facts', {}).get(fact_id, {})
    expected = {'method': 'smc', 'dt_node': '/psci', 'firmware_owner': 'TF-A BL31',
                'supported_reset': 'PSCI_0_2_FN_SYSTEM_RESET',
                'compatible': ['arm,psci-1.0', 'arm,psci-0.2']}
    if (fact.get('authority') != 'explicit-smartnic-design-delta' or
            any(fact.get('value', {}).get(key) != value for key, value in expected.items())):
        raise ValueError('PSCI registry contract mismatch')
    required = ('CONFIG_PSCI_RESET=y', 'CONFIG_ARM_PSCI_FW=y', 'CONFIG_ARM_SMCCC=y',
                'CONFIG_OF_CONTROL=y', 'CONFIG_SEC_FIRMWARE_ARMV8_PSCI=y')
    if any(value not in config for value in required):
        raise ValueError('generated config misses PSCI reset/firmware/SMC/DT support')
    linked = {line.split()[-1]: line.split()[-2] for line in symbols.splitlines()
              if len(line.split()) >= 3}
    for name in ('reset_misc', 'invoke_psci_fn', '__arm_smccc_smc'):
        if linked.get(name) != 'T':
            raise ValueError('ELF misses strong PSCI function: ' + name)
    for name in ('do_psci_probe', 'psci_probe', 'psci_method', '_u_boot_list_2_driver_2_psci'):
        if linked.get(name, 'U').upper() in ('U', 'W', 'V'):
            raise ValueError('ELF misses linked PSCI driver state: ' + name)
    # dtc renders root children with exactly one tab. Reject nested /soc/psci,
    # duplicates, disabled nodes and wrong transports in the compiled DT.
    nodes = re.findall(r'^\tpsci \{\n(.*?)^\t\};', decoded_dtb, re.M | re.S)
    if len(nodes) != 1:
        raise ValueError('compiled control DT requires one root /psci node')
    node = nodes[0]

    def strings(name):
        properties = re.findall(r'^\t\t' + name + r' = ([^;]+);$', node, re.M)
        if len(properties) != 1:
            return []
        return [part for quoted in re.findall(r'"([^"\n]*)"', properties[0])
                for part in quoted.split(r'\0')]
    if strings('compatible') != ['arm,psci-1.0', 'arm,psci-0.2']:
        raise ValueError('compiled /psci compatible does not match pinned driver')
    if strings('method') != ['smc']:
        raise ValueError('compiled /psci method must be smc')
    if 'status =' in node and strings('status') != ['okay']:
        raise ValueError('compiled /psci is disabled')
    return dict(evidence_fact=fact_id, dt_node='/psci', method='smc',
                compatible=['arm,psci-1.0', 'arm,psci-0.2'],
                reset='PSCI_0_2_FN_SYSTEM_RESET', firmware_owner='TF-A BL31',
                linked_reset_misc='strong', target_qualified=False)


def audit_identity(registry, config, symbols, binary):
    contract = json.loads((HERE.parent / 'identity/mac-v1.json').read_text())
    fact_id = 'x200-soc-derived-mac-v1'
    fact = registry.get('facts', {}).get(fact_id, {})
    if (fact.get('authority') != 'explicit-smartnic-design-delta' or
            any(fact.get('value', {}).get(k) != v for k, v in contract.items())):
        raise ValueError('SoC MAC authority contract mismatch')
    if 'CONFIG_NET_RANDOM_ETHADDR=y' in config or 'CONFIG_SHA256=y' not in config:
        raise ValueError('identity requires SHA256 and disabled random Ethernet')
    linked = {line.split()[-1]: line.split()[-2] for line in symbols.splitlines()
              if len(line.split()) >= 3}
    for name in ('x200_identity_init', 'x200_identity_valid', 'x200_identity_apply',
                 'x200_identity_get_mac', 'x200_identity_env_change'):
        if linked.get(name) != 'T':
            raise ValueError('ELF misses identity authority function: ' + name)
    for marker in (b'X200_IDENTITY READY:', b'X200_IDENTITY FAILED:',
                   b'MAC authority', b'SoC FUID (v1)'):
        if marker not in binary:
            raise ValueError('missing identity marker: ' + marker.decode())
    return dict(evidence_fact=fact_id, contract_sha256=sha(HERE.parent / 'identity/mac-v1.json'),
                authority='soc-fuid-v1', dpmac_ids=[3, 4, 5, 6],
                random_ethernet=False, environment_override=False, target_qualified=False)


def audit_environment(registry, config, binary, source_sha):
    contract = json.loads((HERE.parent / 'environment/contract-v1.json').read_text())
    fact_id = 'x200-redundant-environment-v1'
    fact = registry.get('facts', {}).get(fact_id, {})
    if (fact.get('authority') != 'explicit-smartnic-design-delta' or
            any(fact.get('value', {}).get(k) != v for k, v in contract.items())):
        raise ValueError('environment authority contract mismatch')
    expected = ('CONFIG_ENV_SIZE=0x10000', 'CONFIG_ENV_OFFSET=0x500000',
                'CONFIG_ENV_OFFSET_REDUND=0x510000', 'CONFIG_ENV_SECT_SIZE=0x10000',
                'CONFIG_ENV_IS_IN_SPI_FLASH=y', 'CONFIG_ENV_SPI_BUS=0',
                'CONFIG_ENV_SPI_CS=0', 'CONFIG_SYS_REDUNDAND_ENVIRONMENT=y',
                'CONFIG_CMD_NVEDIT_INFO=y', 'CONFIG_CMD_SAVEENV=y')
    if any(item not in config for item in expected):
        raise ValueError('compiled environment backend/layout mismatch')
    spec = importlib.util.spec_from_file_location('x200_env_audit', LAYER / 'uboot_env.py')
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    if source_sha != adapter.ADAPTED_SHA256:
        raise ValueError('compiled environment source does not match pinned adaptation')
    for status in ('blank', 'valid selected=', 'corrupt', 'io-error', 'verify-error'):
        if ('X200-ENV: status=' + status).encode() not in binary:
            raise ValueError('missing environment diagnostic: ' + status)
    return dict(evidence_fact=fact_id, source_sha256=source_sha,
                automatic_save=False, save_readback=True, target_qualified=False)
