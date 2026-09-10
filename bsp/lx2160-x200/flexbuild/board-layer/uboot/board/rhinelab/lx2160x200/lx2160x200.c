// SPDX-License-Identifier: GPL-2.0+
/*
 * LX2160A X200 SmartNIC board support
 *
 * This port intentionally contains no LX2160ARDB/QIXIS fallback. TF-A owns
 * DDR initialization; X200 peripherals follow the board hardware contract.
 */

#include <config.h>
#include <display_options.h>
#include <env.h>
#include <dm.h>
#include <dm/uclass.h>
#include <init.h>
#include <malloc.h>
#include <errno.h>
#include <fdt_support.h>
#include <fsl_ddr.h>
#include <fsl-mc/fsl_mc.h>
#include <spi.h>
#include <spi_flash.h>
#include <version.h>
#include <x200_boot_banner_build.h>
#include <asm/io.h>
#include <asm/global_data.h>
#include <asm/arch/clock.h>
#include <asm/arch/soc.h>
#include <asm/arch-fsl-layerscape/fsl_icid.h>
#include <linux/libfdt.h>
#include <linux/string.h>
#include "rx_gain.h"
#include "identity.h"
#include "manifest.h"

DECLARE_GLOBAL_DATA_PTR;

#define X200_SYSCLK_HZ	100000000UL

#define X200_EPLD_SPI_BUS	1
#define X200_EPLD_SPI_CS	0
#define X200_EPLD_SPI_HZ	5000000
#define X200_EPLD_BOOT_ACK_REGISTER	0x0066
#define X200_EPLD_BOOT_ACK_VALUE	0x0001
#define X200_EPLD_BOOT_ACK_TRAILING_BYTE	0x66

static int x200_epld_xfer(struct spi_slave *slave, u32 address,
			  const void *data, size_t length)
{
	u8 command[3] = {
		(u8)((address >> 16) | 0x20),
		(u8)(address >> 8),
		(u8)address,
	};
	int ret;

	ret = spi_xfer(slave, sizeof(command) * 8, command, NULL,
		       SPI_XFER_BEGIN);
	if (ret) {
		spi_xfer(slave, 0, NULL, NULL, SPI_XFER_END);
		return ret;
	}

	return spi_xfer(slave, length * 8, data, NULL, SPI_XFER_END);
}

static int x200_epld_write(struct spi_slave *slave, u32 address, u16 value,
			   u8 trailing_byte)
{
	u8 data[3] = { (u8)(value >> 8), (u8)value, trailing_byte };

	return x200_epld_xfer(slave, address, data, sizeof(data));
}

static int x200_acknowledge_boot(void)
{
	struct spi_slave *slave;
	struct udevice *bus;
	int ret;

	ret = _spi_get_bus_and_cs(X200_EPLD_SPI_BUS, X200_EPLD_SPI_CS,
				  X200_EPLD_SPI_HZ, SPI_MODE_3,
				  "spi_generic_drv", "x200-epld@0",
				  &bus, &slave);
	if (ret)
		goto fail;

	ret = spi_claim_bus(slave);
	if (ret)
		goto fail;

	/* Board controller power-good handshake. */
	ret = x200_epld_write(slave, X200_EPLD_BOOT_ACK_REGISTER,
			       X200_EPLD_BOOT_ACK_VALUE,
			       X200_EPLD_BOOT_ACK_TRAILING_BYTE);
	spi_release_bus(slave);
	if (ret)
		goto fail;

	puts("X200-CPLD: boot acknowledgement=PASS\n");
	return 0;

fail:
	printf("X200-CPLD: boot acknowledgement=FAIL err=%d\n", ret);
	return ret;
}

static void x200_print_board_table(void);

static int x200_install_boot_policy(void)
{
	static const struct {
		const char *name;
		const char *value;
	} policy[] = {
		{ "bootcmd", "run x200_boot" },
		{ "fdtfile", X200_FDT_FILE },
		{ "board_profile_id", X200_BOARD_PROFILE_ID },
		{ "x200_console_args", X200_CONSOLE_ARGS },
		{ "fdt_addr_r", "0x90000000" },
		{ "kernel_addr_r", "0x81000000" },
		{ "scriptaddr", "0x80000000" },
		{ "x200_mc_addr_r", "0x80a00000" },
		{ "x200_dpl_addr_r", "0x80d00000" },
		{ "x200_dpc_addr_r", "0x80e00000" },
		{ "x200_scsi_dev", "0" },
		{ "x200_boot_part", "1" },
		{ "x200_root_part", "2" },
		{ "x200_mc_boot", X200_MC_BOOT_COMMAND },
		{ "x200_dpl_stage", X200_DPL_STAGE_COMMAND },
		{ "x200_fdt_stage", X200_FDT_STAGE_COMMAND },
		{ "mcinitcmd", "run x200_mc_boot" },
		{ "x200_boot", X200_BOOT_COMMAND },
		{ "fsl_bootcmd_mcinitcmd_set", "y" },
	};
	size_t i;
	int ret;

	/*
	 * Layerscape common late init may synthesize bootcmd/mcinitcmd from the
	 * boot source. The X200 owns both commands and restores them on every
	 * boot so a stale mutable environment cannot select the RDB policy.
	 */
	for (i = 0; i < sizeof(policy) / sizeof(policy[0]); i++) {
		ret = env_set(policy[i].name, policy[i].value);
		if (ret)
			goto fail;
	}

	/* User preference persists; only a missing value uses the compiled default. */
	if (!env_get("bootdelay")) {
		char delay[16];

		snprintf(delay, sizeof(delay), "%d", CONFIG_BOOTDELAY);
		ret = env_set("bootdelay", delay);
		if (ret)
			goto fail;
	}
	x200_print_board_table();
	return 0;

fail:
	printf("X200-BOOT: policy install=FAIL err=%d\n", ret);
	return ret;
}

int board_early_init_f(void)
{
	fsl_lsch3_early_init_f();
	return 0;
}

int fsl_board_late_init(void)
{
	struct udevice *ep;
	int ret;

	/* Never trust a marker inherited from a mutable saved environment. */
	ret = env_set("x200_pcie_boot_policy", NULL);
	if (ret)
		return ret;

	ret = x200_acknowledge_boot();
	if (ret)
		return ret;

	/* PCI_INIT_R probes UCLASS_PCI only; explicitly initialize the Endpoint. */
	ret = uclass_get_device_by_name(UCLASS_PCI_EP, "pcie@3800000", &ep);
	if (ret) {
		printf("X200-PCIE: cold bootstrap failed err=%d; boot stopped\n", ret);
		return ret;
	}
	ret = env_set("x200_pcie_boot_policy", "linux-publish-only");
	if (ret)
		return ret;
	x200_report_boot_flash_identity();
	return x200_install_boot_policy();
}

/* Presentation reads the same runtime authority as native diagnostics. */
static const char *x200_dt_string(int node, const char *property)
{
	int length;
	const char *value = fdt_getprop(gd->fdt_blob, node, property, &length);

	return value && length > 0 && value[length - 1] == '\0' ? value : "unknown";
}

static void x200_table_row(const char *name, const char *value)
{
	/* Bound the complete line (including newline), never truncate identity. */
	char line[161];
	unsigned int i, used, prefix, width;
	unsigned char c;
	bool overflow = false;

	prefix = snprintf(line, sizeof(line), "  %-14s : ", name);
	used = prefix;
	for (i = 0; value[i]; i++) {
		c = value[i];
		width = c >= 32 && c <= 126 && c != '\\' ? 1 : 4;
		if (used + width > 159) {
			overflow = true;
			break;
		}
		if (width == 1)
			line[used++] = c;
		else {
			snprintf(line + used, sizeof(line) - used, "\\x%02x", c);
			used += 4;
		}
	}
	if (overflow) {
		memcpy(line + prefix, "unknown", 7);
		used = prefix + 7;
	}
	line[used++] = '\n';
	line[used] = '\0';
	puts(line);
	if (overflow)
		printf("  WARN           : %s exceeds display limit; shown as unknown\n", name);
}

static void x200_print_board_table(void)
{
	const struct x200_flash_summary *m = x200_boot_flash_summary();
	struct ccsr_gur *gur = (void *)CFG_SYS_FSL_GUTS_ADDR;
	unsigned long long bytes = 0;
	const fdt32_t *lanes;
	const char *mode;
	char value[160], path[80], name[20];
	unsigned char mac[6];
	int i, node, length;
	u32 s1, s2, s3;

	s1 = (gur_in32(&gur->rcwsr[FSL_CHASSIS3_SRDS1_REGSR - 1]) &
	      FSL_CHASSIS3_SRDS1_PRTCL_MASK) >> FSL_CHASSIS3_SRDS1_PRTCL_SHIFT;
	s2 = (gur_in32(&gur->rcwsr[FSL_CHASSIS3_SRDS2_REGSR - 1]) &
	      FSL_CHASSIS3_SRDS2_PRTCL_MASK) >> FSL_CHASSIS3_SRDS2_PRTCL_SHIFT;
	s3 = (gur_in32(&gur->rcwsr[FSL_CHASSIS3_SRDS3_REGSR - 1]) &
	      FSL_CHASSIS3_SRDS3_PRTCL_MASK) >> FSL_CHASSIS3_SRDS3_PRTCL_SHIFT;
	puts("\n============================================================\n");
	puts("[RhineLab X200] LX2160A SmartNIC\n");
	x200_table_row("U-Boot", PLAIN_VERSION);
	x200_table_row("BSP", X200_BANNER_BSP_GIT);
	x200_table_row("Built UTC", X200_BANNER_BUILT_UTC);
	x200_table_row("Board", "X200");
	x200_table_row("Boot policy", "SATA0: boot partition 1, root partition 2");
	snprintf(value, sizeof(value), "%s s", env_get("bootdelay"));
	x200_table_row("Autoboot", value);
	x200_table_row("MAC authority", x200_identity_valid() ? "SoC FUID (v1)" : "unknown");
	x200_table_row("Firmware", m ? m->variant : "unknown");
	x200_table_row("Bank", m ? m->image : "unknown");
	x200_table_row("Profile", m ? m->profile : "unknown");
	if (!m)
		x200_table_row("WARN", "NOR manifest identity unavailable");
	snprintf(value, sizeof(value), "S1=0x%02x S2=0x%02x S3=0x%02x (RCW protocols)", s1, s2, s3);
	x200_table_row("SerDes", value);
	for (i = 0; i < CONFIG_NR_DRAM_BANKS; i++)
		bytes += gd->bd->bi_dram[i].size;
	snprintf(value, sizeof(value), "%llu.%llu GiB usable, %lu MT/s", bytes >> 30,
		 ((bytes & ((1ULL << 30) - 1)) * 10) >> 30,
		 get_ddr_freq(0) / 1000000);
	x200_table_row("DDR", value);
	if (m)
		snprintf(value, sizeof(value), "%u.%u", m->mc_api_major, m->mc_api_minor);
	x200_table_row("MC API", m ? value : "unknown");
	snprintf(value, sizeof(value), "RCW SB_EN=%u",
		 !!(gur_in32(&gur->rcwsr[RCW_SB_EN_REG_INDEX - 1]) & RCW_SB_EN_MASK));
	x200_table_row("Secure Boot", value);
	node = fdt_path_offset(gd->fdt_blob, "/pcie@3800000");
	lanes = fdt_getprop(gd->fdt_blob, node, "num-lanes", &length);
	if (lanes && length == (int)sizeof(*lanes))
		snprintf(value, sizeof(value), "Endpoint, configured x%u; Linux publication", fdt32_to_cpu(*lanes));
	else
		snprintf(value, sizeof(value), "Endpoint; Linux publication");
	x200_table_row("PCIe5 policy", value);
	for (i = 3; i <= 6; i++) {
		snprintf(path, sizeof(path), "/fsl-mc@80c000000/dpmacs/dpmac@%x", i);
		node = fdt_path_offset(gd->fdt_blob, path);
		snprintf(name, sizeof(name), "DPMAC%d", i);
		mode = x200_dt_string(node, "phy-connection-type");
		/* Native18 DT binding authority: x200-dual-rate-native18-design. */
		if (m && !strcmp(m->profile, "x200-s1_12-s2_05-s3_02-v1")) {
			if (!strcmp(mode, "xgmii"))
				mode = "10G (configured)";
			else if (!strcmp(mode, "25g-aui"))
				mode = "25G (configured)";
		}
		if (!x200_identity_get_mac(i, mac))
			snprintf(value, sizeof(value), "%s, MAC %02x:%02x:%02x:%02x:%02x:%02x",
				 mode, mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
		else
			snprintf(value, sizeof(value), "%s, MAC unknown", mode);
		x200_table_row(name, value);
	}
	puts("============================================================\n\n");
}

int checkboard(void)
{
	enum boot_src src = get_boot_src();

	puts("Board: LX2160A X200 SmartNIC, boot from ");
	if (src == BOOT_SOURCE_XSPI_NOR)
		puts("FlexSPI NOR\n");
	else if (src == BOOT_SOURCE_SD_MMC)
		puts("SD\n");
	else if (src == BOOT_SOURCE_SD_MMC2)
		puts("eMMC\n");
	else
		printf("source %d\n", src);

	return 0;
}

unsigned long get_board_sys_clk(void)
{
	return X200_SYSCLK_HZ;
}

unsigned long get_board_ddr_clk(void)
{
	return X200_SYSCLK_HZ;
}

int board_init(void)
{
	/*
	 * Relocated board_init runs before initr_env. The environment import
	 * callback already sees bound Ethernet devices, so its immutable MAC
	 * authority must exist before it validates the saved address mirrors.
	 * Invalid UID still disables Ethernet; it must not stop console init.
	 */
	x200_identity_init();
	return 0;
}

int fsl_initdram(void)
{
	gd->ram_size = tfa_get_dram_size();
	if (!gd->ram_size)
		panic("X200: TF-A did not provide initialized DRAM\n");

	return 0;
}

void detail_board_ddr_info(void)
{
	int i;
	u64 ddr_size = 0;

	puts("\nDDR    ");
	for (i = 0; i < CONFIG_NR_DRAM_BANKS; i++)
		ddr_size += gd->bd->bi_dram[i].size;
	print_size(ddr_size, "");
	print_ddr_info(0);
}

#ifdef CONFIG_FSL_MC_ENET
void fdt_fixup_board_enet(void *fdt)
{
	int offset;

	offset = fdt_path_offset(fdt, "/soc/fsl-mc");
	if (offset < 0)
		offset = fdt_path_offset(fdt, "/fsl-mc");
	if (offset < 0) {
		printf("%s: fsl-mc node not found (error %d)\n",
		       __func__, offset);
		return;
	}

	if (x200_identity_valid() && get_mc_boot_status() == 0 &&
	    (is_lazy_dpl_addr_valid() || get_dpl_apply_status() == 0))
		fdt_status_okay(fdt, offset);
	else
		fdt_status_fail(fdt, offset);
}

void board_quiesce_devices(void)
{
	int ret = fsl_mc_ldpaa_exit(gd->bd);

	/* The pinned exit path applies the lazy DPL before this point. */
	if (ret || get_mc_boot_status() || get_dpl_apply_status()) {
		printf("X200_RX_AUTO_FAILED MC/DPL unavailable status=%d\n", ret);
		return;
	}
	/* On failure retain the Linux console for diagnosis; helper logs/restores. */
	x200_apply_rx_auto();
}
#endif

#ifdef CONFIG_OF_BOARD_SETUP
int ft_board_setup(void *blob, struct bd_info *bd)
{
	u64 mc_memory_base = 0;
	u64 mc_memory_size = 0;
	u16 mc_memory_bank = 0;
	u16 total_memory_banks;
	u64 *base;
	u64 *size;
	int i;
	int err;

	err = fdt_increase_size(blob, 512);
	if (err)
		return err;

	ft_cpu_setup(blob, bd);
	fdt_fixup_mc_ddr(&mc_memory_base, &mc_memory_size);
	if (mc_memory_base)
		mc_memory_bank = 1;

	total_memory_banks = CONFIG_NR_DRAM_BANKS + mc_memory_bank;
	base = calloc(total_memory_banks, sizeof(*base));
	size = calloc(total_memory_banks, sizeof(*size));
	if (!base || !size) {
		free(base);
		free(size);
		return -ENOMEM;
	}

	for (i = 0; i < CONFIG_NR_DRAM_BANKS; i++) {
		base[i] = gd->bd->bi_dram[i].start;
		size[i] = gd->bd->bi_dram[i].size;
	}
	if (mc_memory_base) {
		base[CONFIG_NR_DRAM_BANKS] = mc_memory_base;
		size[CONFIG_NR_DRAM_BANKS] = mc_memory_size;
	}

	fdt_fixup_memory_banks(blob, base, size, total_memory_banks);
	free(base);
	free(size);

#ifdef CONFIG_FSL_MC_ENET
	fdt_fsl_mc_fixup_iommu_map_entry(blob);
	fdt_fixup_board_enet(blob);
	fdt_reserve_mc_mem(blob, 0x4000);
#endif
	fdt_fixup_icid(blob);

	/* Use the local CAAM RNG and propagate failures to the boot command. */
	return fdt_kaslrseed(blob, true);
}
#endif
