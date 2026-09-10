/*
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright 2026 RhineLab
 */

#include <stdint.h>

#include <common/debug.h>
#include <ddr.h>
#include <lib/utils.h>
#include <load_img.h>

#include "plat_common.h"
#include <platform_def.h>

/* hardware-evidence-v1.json: x200-bl2-ddr-contract.
 * Two DDR controllers, one SPD DIMM per controller at addresses 0x50/0x51.
 */
static int x200_spd_addr[] = { 0x50, 0x51 };

int ddr_board_options(struct ddr_info *priv)
{
	struct memctl_opt *popts = &priv->opt;

	/* Board electrical parameters from x200-bl2-ddr-contract. */
	popts->vref_dimm = U(0x24);
	popts->rtt_override = 0;
	popts->rtt_park = U(240);
	popts->otf_burst_chop_en = 0;
	popts->burst_length = U(DDR_BL8);
	popts->trwt_override = U(1);
	popts->bstopre = U(0);
	popts->addr_hash = 1;
	popts->trwt = U(0x3);
	popts->twrt = U(0x3);
	popts->trrt = U(0x3);
	popts->twwt = U(0x3);
	popts->vref_phy = U(0x60);
	popts->odt = U(48);
	popts->phy_tx_impedance = U(48);

	return 0;
}

long long init_ddr(void)
{
	struct ddr_info info;
	struct sysinfo sys;
	long long dram_size;

	zeromem(&sys, sizeof(sys));
	if (get_clocks(&sys) != 0) {
		ERROR("System clocks are not set\n");
		panic();
	}
	debug("platform clock %lu\n", sys.freq_platform);
	debug("DDR PLL1 %lu\n", sys.freq_ddr_pll0);
	debug("DDR PLL2 %lu\n", sys.freq_ddr_pll1);

	zeromem(&info, sizeof(info));
	info.num_ctlrs = NUM_OF_DDRC;
	info.spd_addr = x200_spd_addr;
	info.ddr[0] = (void *)NXP_DDR_ADDR;
	info.ddr[1] = (void *)NXP_DDR2_ADDR;
	info.phy[0] = (void *)NXP_DDR_PHY1_ADDR;
	info.phy[1] = (void *)NXP_DDR_PHY2_ADDR;
	info.clk = get_ddr_freq(&sys, 0);
	info.img_loadr = load_img;
	info.phy_gen2_fw_img_buf = PHY_GEN2_FW_IMAGE_BUFFER;
	if (info.clk == 0)
		info.clk = get_ddr_freq(&sys, 1);
	info.dimm_on_ctlr = DDRC_NUM_DIMM;
	info.warm_boot_flag = DDR_WRM_BOOT_NT_SUPPORTED;

	dram_size = dram_init(&info
#if defined(NXP_HAS_CCN504) || defined(NXP_HAS_CCN508)
			      , NXP_CCN_HN_F_0_ADDR
#endif
			      );
	if (dram_size <= 0) {
		ERROR("DDR init failed: %lld\n", dram_size);
		panic();
	}

	return dram_size;
}
