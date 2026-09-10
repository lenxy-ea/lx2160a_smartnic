#!/usr/bin/env python3
"""Install the prepared X200 profile snapshot into FlexBuild component trees."""

from __future__ import annotations

import argparse
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys


LAYER_DIR = Path(__file__).resolve().parent
PROFILE_DIR = LAYER_DIR.parents[1] / "profiles/x200-s1_12-s2_05-s3_02-v1"
ATF_LAYER_DIR = LAYER_DIR / "atf"
UBOOT_LAYER_DIR = LAYER_DIR / "uboot"
PROFILE_FILE = PROFILE_DIR / "profile.json"
EXPECTED_PROFILE_ID = "x200-s1_12-s2_05-s3_02-v1"
RCW_VARIANT = "X200_S2_GEN3_PEX5X8_12_5_2"
RCW_NAME = "rcw_2000_600_3200_12_5_2.rcw"
FIRMWARE_VARIANT = "native18-s2-gen3"

_banner_spec = importlib.util.spec_from_file_location("x200_boot_banner", LAYER_DIR / "boot_banner.py")
BANNER = importlib.util.module_from_spec(_banner_spec)
_banner_spec.loader.exec_module(BANNER)


class InstallError(RuntimeError):
    pass


def load_profile() -> dict[str, object]:
    try:
        profile = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InstallError(f"cannot read prepared profile: {error}") from error
    if profile.get("board_profile_id") != EXPECTED_PROFILE_ID:
        raise InstallError("prepared profile has an unexpected board_profile_id")
    if profile.get("deployment_allowed") is not False:
        raise InstallError("prepared development profile must remain non-deployable")
    return profile


def source_for(profile: dict[str, object], component: str) -> Path:
    sources = profile.get("sources")
    if not isinstance(sources, dict) or not isinstance(sources.get(component), str):
        raise InstallError(f"profile source is missing: {component}")
    source = (PROFILE_DIR / sources[component]).resolve()
    if PROFILE_DIR.resolve() not in source.parents or not source.is_file():
        raise InstallError(f"invalid prepared profile source: {source}")
    if EXPECTED_PROFILE_ID not in source.read_text(encoding="utf-8"):
        raise InstallError(f"profile ID is missing from {source}")
    return source


def copy_source(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def replace_once(path: Path, anchor: str, replacement: str) -> None:
    content = path.read_text(encoding="utf-8")
    if content.count(replacement) == 1:
        return
    if content.count(replacement) != 0 or content.count(anchor) != 1:
        raise InstallError(f"unexpected diagnostic anchor in {path}")
    path.write_text(content.replace(anchor, replacement, 1), encoding="utf-8")


def append_once(path: Path, addition: str) -> None:
    content = path.read_text(encoding="utf-8")
    if content.count(addition) == 1:
        return
    if content.count(addition) != 0:
        raise InstallError(f"duplicate X200 integration in {path}")
    path.write_text(content.rstrip() + "\n\n" + addition + "\n", encoding="utf-8")


def insert_before_last_once(path: Path, anchor: str, addition: str) -> None:
    content = path.read_text(encoding="utf-8")
    if content.count(addition) == 1:
        return
    if content.count(addition) != 0:
        raise InstallError(f"duplicate X200 integration in {path}")
    position = content.rfind(anchor)
    if position < 0:
        raise InstallError(f"structural anchor is missing from {path}: {anchor!r}")
    path.write_text(
        content[:position] + addition + "\n\n" + content[position:],
        encoding="utf-8",
    )


def install_rcw(profile: dict[str, object], tree: Path) -> None:
    if not (tree / "Makefile.inc").is_file() or not (tree / "lx2160asi").is_dir():
        raise InstallError(f"not an LX2160 RCW component tree: {tree}")
    board_dir = tree / "lx2160-x200"
    copy_source(source_for(profile, "rcw"), board_dir / RCW_VARIANT / RCW_NAME)
    (board_dir / "Makefile").write_text("include ../Makefile.inc\n", encoding="utf-8")
    (board_dir / "README").write_text(
        "X200 " + FIRMWARE_VARIANT + " profile: " + EXPECTED_PROFILE_ID + "\n",
        encoding="utf-8",
    )


def install_mc_utils(profile: dict[str, object], tree: Path) -> None:
    if not (tree / "config/Makefile").is_file():
        raise InstallError(f"not an mc-utils component tree: {tree}")
    destination = tree / "config/lx2160a/X200"
    copy_source(source_for(profile, "dpc"), destination / "x200.dpc.dts")
    copy_source(source_for(profile, "dpl"), destination / "x200.dpl.dts")


def install_linux(profile: dict[str, object], tree: Path) -> None:
    dts_dir = tree / "arch/arm64/boot/dts/freescale"
    makefile = dts_dir / "Makefile"
    if not makefile.is_file() or not (dts_dir / "fsl-lx2160a.dtsi").is_file():
        raise InstallError(f"not a Linux tree with LX2160 DTS support: {tree}")
    copy_source(source_for(profile, "dtb"), dts_dir / "fsl-lx2160a-x200.dts")

    append_once(
        makefile,
        "DTC_FLAGS_fsl-lx2160a-x200 := -Wno-interrupt_map "
        "-Wno-unique_unit_address\n"
        "dtb-$(CONFIG_ARCH_LAYERSCAPE) += fsl-lx2160a-x200.dtb",
    )



def install_atf_ddr_clock(tree: Path) -> None:
    """Account for LX2160A MEM PLL dividers without changing sysinfo units."""
    # Generic SoC clock semantics, not another board's clock configuration:
    # https://lists.buildroot.org/pipermail/buildroot/2025-February/772518.html
    # Keep the existing full-rate Hz contract used by get_ddr_freq and X200
    # diagnostics. Multiply before dividing to retain integer-Hz precision.
    header = tree / "include/drivers/nxp/dcfg/dcfg_lsch3.h"
    source = tree / "drivers/nxp/dcfg/dcfg.c"
    # Validate every edit before writing either file, including repeat installs.
    changes = []
    for channel, field in ((0, "MEM"), (1, "MEM2")):
        marker = f"RCWSR0_{field}_PLL_CFG_SHIFT"
        anchor = f"#define RCWSR0_{field}_PLL_RAT_SHIFT\t" + ("10" if channel == 0 else "18")
        replacement = (
            f"#define RCWSR0_{field}_PLL_CFG_SHIFT\t{8 if channel == 0 else 16}\n"
            f"#define RCWSR0_{field}_PLL_CFG_MASK\t0x3\n" + anchor
        )
        changes.append((header, anchor, replacement, marker))
        anchor = (
            f"\tsys->freq_ddr_pll{channel} *= (gur_in32(rcwsr0) >>\n"
            f"\t\t\t\tRCWSR0_{field}_PLL_RAT_SHIFT) &\n"
            f"\t\t\t\tRCWSR0_{field}_PLL_RAT_MASK;"
        )
        replacement = anchor + (
            "\n#if defined(CONFIG_CHASSIS_3_2)\n"
            f"\tsys->freq_ddr_pll{channel} = sys->freq_ddr_pll{channel} * 4ULL /\n"
            f"\t\t(((gur_in32(rcwsr0) >> RCWSR0_{field}_PLL_CFG_SHIFT) &\n"
            f"\t\t  RCWSR0_{field}_PLL_CFG_MASK) + 1U);\n"
            "#endif"
        )
        changes.append((source, anchor, replacement, marker))
    contents = {path: path.read_text(encoding="utf-8") for path in (header, source)}
    for path, anchor, replacement, marker in changes:
        content = contents[path]
        if (content.count(replacement) == 1 and content.count(anchor) == 1
                and content.count(marker) == 1):
            continue
        if content.count(marker) or content.count(replacement) or content.count(anchor) != 1:
            raise InstallError(f"unexpected TF-A DDR clock anchor in {path}")
        contents[path] = content.replace(anchor, replacement, 1)
    for path, content in contents.items():
        path.write_text(content, encoding="utf-8")


def install_atf_ddr_training(tree: Path) -> None:
    """Preserve every PHY failure through dram_init; expose cold-training results."""
    phy = tree / "drivers/nxp/ddr/phy-gen2/phy.c"
    replace_once(
        phy,
        '\t\tif (ret == -ETIMEDOUT) {\n'
        '\t\t\tERROR("Wait timed out: Firmware execution on PHY %d\\n",\n'
        '\t\t\t      i);\n\t\t}',
        '\t\tif (ret != 0) {\n'
        '\t\t\tERROR("DDR PHY %d %s training failed: %d\\n",\n'
        '\t\t\t      i, train2d ? "2D" : "1D", ret);\n'
        '\t\t\treturn ret;\n\t\t}\n'
        '\t\tNOTICE("DDR PHY %d %s training passed\\n",\n'
        '\t\t       i, train2d ? "2D" : "1D");',
    )
    replace_once(
        phy,
        '\t\t\tERROR("Execution FW failed (error code %d)\\n", ret);\n\t\t}',
        '\t\t\tERROR("Execution FW failed (error code %d)\\n", ret);\n'
        '\t\t\treturn ret;\n\t\t}',
    )
    # A completion mail with a failed acknowledgement is not a successful exchange.
    replace_once(
        phy,
        '\t\tERROR("Timeout ack PHY mail\\n");\n\t}',
        '\t\tERROR("Timeout ack PHY mail\\n");\n'
        '\t\treturn 0xFFFF;\n\t}',
    )
    replace_once(
        tree / "drivers/nxp/ddr/nxp-ddr/ddr.c",
        '\tret = compute_ddr_phy(priv);\n\tif (ret != 0)\n'
        '\t\tERROR("Calculating DDR PHY registers failed.\\n");',
        '\tret = compute_ddr_phy(priv);\n\tif (ret != 0) {\n'
        '\t\tERROR("Calculating DDR PHY registers failed.\\n");\n'
        '\t\treturn ret;\n\t}',
    )


def install_atf_boot_banner(tree: Path) -> None:
    """Hook the pinned SoC's boot-only console checkpoints for X200."""
    soc = tree / "plat/nxp/soc-lx2160a/soc.c"
    replace_once(
        soc,
        "#include <common/debug.h>",
        "#include <common/debug.h>\n"
        "#ifdef X200_BOOT_BANNER\n"
        "void x200_tfa_boot_banner(const char *stage);\n"
        "#endif",
    )
    # These are the distinct soc_early_init (BL2) and
    # soc_early_platform_setup2 (BL31) console blocks at the locked SDK pin.
    # Neither runs on secondary CPU entry or warm PSCI CPU_ON paths.
    for stage, indentation in (("BL2", "\t\t\t\t"), ("BL31", "\t\t\t  ")):
        anchor = (
            "\tplat_console_init(NXP_CONSOLE_ADDR,\n"
            + indentation + "NXP_UART_CLK_DIVIDER, NXP_CONSOLE_BAUDRATE);"
        )
        replace_once(
            soc, anchor,
            anchor + "\n#ifdef X200_BOOT_BANNER\n"
            f'\tx200_tfa_boot_banner("TF-A/{stage}");\n'
            "#endif",
        )


def install_atf(tree: Path, *, banner: dict[str, str]) -> None:
    BANNER.install(tree, banner)
    platform_root = tree / "plat/nxp/soc-lx2160a"
    soc_makefile = platform_root / "soc.mk"
    flash_info = tree / "include/drivers/nxp/flexspi/flash_info.h"
    if not (tree / "Makefile").is_file() or not soc_makefile.is_file():
        raise InstallError(f"not an LX2160A TF-A component tree: {tree}")
    if not flash_info.is_file():
        raise InstallError(f"TF-A FlexSPI geometry table is missing: {flash_info}")

    install_atf_ddr_clock(tree)
    install_atf_ddr_training(tree)
    install_atf_boot_banner(tree)

    source_platform = ATF_LAYER_DIR / "plat/nxp/soc-lx2160a/lx2160x200"
    destination_platform = platform_root / "lx2160x200"
    if not source_platform.is_dir():
        raise InstallError(f"prepared X200 TF-A platform is missing: {source_platform}")
    shutil.copytree(source_platform, destination_platform, dirs_exist_ok=True)

    fragment_path = (
        ATF_LAYER_DIR / "include/drivers/nxp/flexspi/flash_info.h.fragment"
    )
    fragment = fragment_path.read_text(encoding="utf-8").rstrip()
    content = flash_info.read_text(encoding="utf-8")
    if content.count("defined(CONFIG_W25Q128)") == 0:
        anchor = "#endif /* End of #elif defined(CONFIG_MT35XU02G) */"
        if content.count(anchor) != 1:
            raise InstallError(f"unexpected TF-A Flash geometry anchor: {flash_info}")
        content = content.replace(anchor, fragment + "\n\n" + anchor, 1)
        flash_info.write_text(content, encoding="utf-8")
    elif content.count("defined(CONFIG_W25Q128)") != 1:
        raise InstallError(f"duplicate W25Q128 geometry in {flash_info}")
    if fragment not in content:
        raise InstallError(f"non-canonical W25Q128 geometry in {flash_info}")

    # Route only X200 through its CPLD gate before the unchanged SoC sequence.
    psci = tree / "plat/nxp/common/psci/aarch64/psci_utils.S"
    old = "\tbl   _soc_sys_reset\nendfunc _psci_system_reset"
    new = ("#ifdef X200_SYSTEM_RESET\n\tbl   x200_system_reset\n"
           "#else\n\tbl   _soc_sys_reset\n#endif\nendfunc _psci_system_reset")
    data = psci.read_text()
    if new not in data:
        if data.count(old) != 1:
            raise InstallError(f"unexpected PSCI reset hook: {psci}")
        psci.write_text(data.replace(old, new, 1))

    # Keep the registered console available for terminal reset failures.
    console = tree / "drivers/nxp/console/console_pl011.c"
    old = "baud, &nxp_console);"
    new = (old + "\n#if defined(IMAGE_BL31) && defined(X200_SYSTEM_RESET)\n"
           "\tconsole_set_scope(&nxp_console, CONSOLE_FLAG_BOOT |\n"
           "\t\tCONSOLE_FLAG_RUNTIME | CONSOLE_FLAG_CRASH);\n#endif")
    data = console.read_text()
    if new not in data:
        if data.count(old) != 1:
            raise InstallError(f"unexpected PL011 console hook: {console}")
        console.write_text(data.replace(old, new, 1))


def install_uboot_platform_fixes(tree: Path) -> None:
    soc = tree / "arch/arm/cpu/armv8/fsl-layerscape/soc.c"
    if not soc.is_file():
        raise InstallError("U-Boot Layerscape SoC source is missing")

    replace_once(
        soc,
        """static void erratum_a050204(void)
{
#if defined(CONFIG_ARCH_LX2160A) || defined(CONFIG_ARCH_LX2162A)
	void __iomem *dcsr = (void __iomem *)DCSR_BASE;

	PROGRAM_USB_PHY_RX_OVRD_IN_HI(dcsr + DCSR_USB_PHY1);
	PROGRAM_USB_PHY_RX_OVRD_IN_HI(dcsr + DCSR_USB_PHY2);
#endif
}""",
        """static void erratum_a050204(void)
{
#if defined(CONFIG_ARCH_LX2160A) || defined(CONFIG_ARCH_LX2162A)
#if defined(CONFIG_TARGET_LX2160X200)
	/* The X200 has no routed SoC USB interface. */
#else
	void __iomem *dcsr = (void __iomem *)DCSR_BASE;

	PROGRAM_USB_PHY_RX_OVRD_IN_HI(dcsr + DCSR_USB_PHY1);
	PROGRAM_USB_PHY_RX_OVRD_IN_HI(dcsr + DCSR_USB_PHY2);
#endif
#endif
}""",
    )


    # x200-caam-rng-instantiated: direct CAAM RNG remains
    # available on this non-E part; TF-A's SiP RNG service does not.
    # ft_board_setup supplies the DM RNG seed and propagates failures.
    replace_once(
        tree / "arch/arm/cpu/armv8/fsl-layerscape/fdt.c",
        "\t\tfdt_fixup_kaslr(blob);",
        "#if !defined(CONFIG_TARGET_LX2160X200)\n"
        "\t\tfdt_fixup_kaslr(blob);\n#endif",
    )


    # Job-ring hardware status is not an errno: positive status is failure too.
    replace_once(
        tree / "drivers/crypto/fsl/rng.c",
        "\tret = run_descriptor_jr(priv->desc);\n\tif (ret < 0)\n\t\treturn -EIO;",
        "\tret = run_descriptor_jr(priv->desc);\n"
        "#if defined(CONFIG_TARGET_LX2160X200)\n"
        "\tif (ret)\n\t\treturn ret < 0 ? ret : -EIO;\n"
        "#else\n\tif (ret < 0)\n\t\treturn -EIO;\n#endif",
    )


def install_uboot_ddr_clock(tree: Path) -> None:
    """Use the same full-rate MEM PLL divider calculation as TF-A."""
    header = tree / "arch/arm/include/asm/arch-fsl-layerscape/immap_lsch3.h"
    source = tree / "arch/arm/cpu/armv8/fsl-layerscape/fsl_lsch3_speed.c"
    changes = []
    for channel, field in ((0, "MEM"), (1, "MEM2")):
        prefix = f"FSL_CHASSIS3_RCWSR0_{field}_PLL"
        marker = f"{prefix}_CFG_SHIFT"
        anchor = f"#define {prefix}_RAT_SHIFT\t{10 if channel == 0 else 18}"
        replacement = (
            f"#define {prefix}_CFG_SHIFT\t{8 if channel == 0 else 16}\n"
            f"#define {prefix}_CFG_MASK\t0x3\n" + anchor
        )
        changes.append((header, anchor, replacement, marker))
        member = "freq_ddrbus" if channel == 0 else "freq_ddrbus2"
        indent = "\t" if channel == 0 else "\t\t"
        anchor = (
            f"{indent}sys_info->{member} *= (gur_in32(&gur->rcwsr[0]) >>\n"
            f"\t\t\t{prefix}_RAT_SHIFT) &\n"
            f"\t\t\t{prefix}_RAT_MASK;"
        )
        replacement = anchor + (
            "\n#if defined(CONFIG_ARCH_LX2160A) || defined(CONFIG_ARCH_LX2162A)\n"
            f"{indent}sys_info->{member} = sys_info->{member} * 4ULL /\n"
            f"{indent}\t(((gur_in32(&gur->rcwsr[0]) >>\n"
            f"{indent}\t   {prefix}_CFG_SHIFT) &\n"
            f"{indent}\t  {prefix}_CFG_MASK) + 1U);\n"
            "#endif"
        )
        changes.append((source, anchor, replacement, marker))
    contents = {path: path.read_text(encoding="utf-8") for path in (header, source)}
    for path, anchor, replacement, marker in changes:
        content = contents[path]
        if (content.count(replacement) == 1 and content.count(anchor) == 1
                and content.count(marker) == 1):
            continue
        if content.count(marker) or content.count(replacement) or content.count(anchor) != 1:
            raise InstallError(f"unexpected U-Boot DDR clock anchor in {path}")
        contents[path] = content.replace(anchor, replacement, 1)
    for path, content in contents.items():
        path.write_text(content, encoding="utf-8")


def install_uboot_pcie_fixes(tree: Path) -> None:
    # The X200 is measured Rev2 DWC. Never patch or build the Gen4/Mobiveil
    # endpoint path. Select the bounded native implementation instead of the
    # generic DWC EP driver's large BAR/SR-IOV/outbound defaults.
    replace_once(
        tree / "drivers/pci/Makefile",
        "obj-$(CONFIG_PCIE_LAYERSCAPE_EP) += pcie_layerscape_ep.o",
        "obj-$(CONFIG_PCIE_LAYERSCAPE_EP) += pcie_layerscape_x200_ep.o",
    )


def insert_once(path: Path, anchor: str, addition: str) -> None:
    content = path.read_text(encoding="utf-8")
    if content.count(addition) == 0:
        if content.count(anchor) != 1:
            raise InstallError(f"unexpected U-Boot anchor in {path}: {anchor!r}")
        content = content.replace(anchor, addition + "\n\n" + anchor, 1)
        path.write_text(content, encoding="utf-8")
    elif content.count(addition) != 1:
        raise InstallError(f"duplicate X200 U-Boot integration in {path}")


def validate_rx_auto_profile(profile: dict[str, object]) -> None:
    s1 = profile.get("serdes", {}).get("s1", {})
    if (profile.get("board_profile_id") != EXPECTED_PROFILE_ID or
            s1.get("source_value") != 18 or s1.get("rx_gain_k2") != "automatic" or
            s1.get("rx_auto_lanes") != [4, 5] or profile.get("ddr_mt_s") != 3200):
        raise InstallError("prepared profile requires native18 DDR3200 and automatic RX K2 on lanes4/5")
    if profile.get("evidence_bindings", {}).get("serdes1_rx_gain") != [
            "x200-rx-auto-startup-default",
            "x200-dual-rate-native18-design"]:
        raise InstallError("automatic RX default lacks the startup and native18 evidence bindings")


def install_uboot(tree: Path, profile: dict[str, object], *, banner: dict[str, str]) -> None:
    BANNER.install(tree, banner)
    validate_rx_auto_profile(profile)
    arch_kconfig = tree / "arch/arm/Kconfig"
    layerscape_kconfig = tree / "arch/arm/cpu/armv8/fsl-layerscape/Kconfig"
    dts_makefile = tree / "arch/arm/dts/Makefile"
    if not (
        (tree / "Makefile").is_file()
        and arch_kconfig.is_file()
        and layerscape_kconfig.is_file()
        and dts_makefile.is_file()
    ):
        raise InstallError(f"not the expected LX2160A U-Boot component tree: {tree}")
    if not UBOOT_LAYER_DIR.is_dir():
        raise InstallError(f"prepared X200 U-Boot layer is missing: {UBOOT_LAYER_DIR}")

    for source in sorted(UBOOT_LAYER_DIR.rglob("*")):
        if source.is_file():
            copy_source(source, tree / source.relative_to(UBOOT_LAYER_DIR))

    (tree / "board/rhinelab/lx2160x200/rx_gain_profile.h").unlink(missing_ok=True)

    install_uboot_ddr_clock(tree)
    install_uboot_platform_fixes(tree)
    install_uboot_pcie_fixes(tree)
    identity_spec = importlib.util.spec_from_file_location(
        "x200_uboot_identity", LAYER_DIR / "uboot_identity.py")
    identity = importlib.util.module_from_spec(identity_spec)
    identity_spec.loader.exec_module(identity)
    identity.install(tree)
    env_spec = importlib.util.spec_from_file_location(
        "x200_uboot_env", LAYER_DIR / "uboot_env.py")
    env_adapter = importlib.util.module_from_spec(env_spec)
    env_spec.loader.exec_module(env_adapter)
    env_source = tree / "env/sf.c"
    if hashlib.sha256(env_source.read_bytes()).hexdigest() != env_adapter.ADAPTED_SHA256:
        env_source.write_text(env_adapter.adapt_sf(env_source.read_text()))

    target = """config TARGET_LX2160X200
\tbool \"Support LX2160A X200\"
\tselect ARCH_LX2160A
\tselect ARM64
\tselect ARMV8_MULTIENTRY
\tselect ARCH_SUPPORT_TFABOOT
\tselect BOARD_LATE_INIT
\tselect GPIO_EXTRA_HEADER
\thelp
\t  Support for the LX2160A X200 SmartNIC development board.
\t  This native target is defined only by the X200 hardware contract."""
    insert_before_last_once(arch_kconfig, "endchoice\n", target)
    insert_once(
        arch_kconfig,
        'source "arch/arm/Kconfig.debug"',
        'source "board/rhinelab/lx2160x200/Kconfig"',
    )
    append_once(
        dts_makefile,
        "dtb-$(CONFIG_TARGET_LX2160X200) += fsl-lx2160a-x200.dtb",
    )
