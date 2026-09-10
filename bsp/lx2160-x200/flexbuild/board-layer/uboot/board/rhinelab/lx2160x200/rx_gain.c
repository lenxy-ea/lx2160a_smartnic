/* Native18 authority: x200-dual-rate-native18-design; 25G lanes4/5 only. */
// SPDX-License-Identifier: GPL-2.0+
/*
 * X200 RX automatic K2 initialization after MC/DPL initialization.
 *
 * Board authority: hardware-evidence-v1.json, x200-rx-auto-startup-default.
 * This uses the measured runtime
 * RX halt/config/reset sequence, applied before Linux owns the datapath.
 * No TX, PLL, FEC, MDIO or PCS writes are issued here.
 */
#include <asm/io.h>
#include <linux/bitops.h>
#include <linux/errno.h>
#include <linux/iopoll.h>
#include <stdio.h>

#include "rx_gain.h"

#define SERDES1_BASE 0x01ea0000UL
#define RX_RESET BIT(31)
#define RX_DONE BIT(30)
#define RX_HALT BIT(27)
#define K2_MASK 0x9f000000U
#define AUTO_GAIN 0x00000085U

static void __iomem *reg(unsigned int offset)
{
	return (void __iomem *)(SERDES1_BASE + offset);
}

static void __iomem *lane_reg(unsigned int lane, unsigned int offset)
{
	return reg(0x800 + lane * 0x100 + offset);
}

static int check_initial_state(void)
{
	static const u32 pss[2] = {
		0x68050300, 0x68040200,
	};
	unsigned int lane, off;

	if (readl(reg(0x400)) != 0x40800030 ||
	    readl(reg(0x404)) != 0x00040000 ||
	    readl(reg(0x408)) != 0x96100008)
		return -EINVAL;

	/* Check every lane before the first write. Empty cages need no CDR lock. */
	for (lane = 4; lane < 6; lane++) {
		if (readl(reg(0x1000 + lane * 4)) != pss[lane - 4] ||
		    readl(lane_reg(lane, 0)) != 0xd4 ||
		    readl(lane_reg(lane, 0x24)) != 0x03000000 ||
		    readl(lane_reg(lane, 0x44)) != 0x03000330 ||
		    readl(lane_reg(lane, 0x48)) != 0x10000000 ||
		    readl(lane_reg(lane, 0x30)) != 0x20828720 ||
		    readl(lane_reg(lane, 0x34)) != 0x30000000 ||
		    readl(lane_reg(lane, 0x50)) != AUTO_GAIN ||
		    readl(lane_reg(lane, 0x68)) != 0x80000000 ||
		    readl(lane_reg(lane, 0x80)) != 0x8000 ||
		    (readl(lane_reg(lane, 0x5c)) & (RX_RESET | RX_DONE)) ||
		    (readl(reg(0x1b08 + (7 - lane) * 0x10)) & 0x00900000))
			return -EINVAL;
		for (off = 0x20; off <= 0x40; off += 0x20)
			if ((readl(lane_reg(lane, off)) & 0xcd000000) != RX_DONE)
				return -EINVAL;
		for (off = 0xa0; off <= 0xb0; off += 4)
			if (readl(lane_reg(lane, off)))
				return -EINVAL;
	}
	return 0;
}

static int reset_rx_auto(unsigned int lane)
{
	void __iomem *reset = lane_reg(lane, 0x40);
	void __iomem *eq = lane_reg(lane, 0x50);
	u32 value;
	int ret, reset_ret;

	setbits_le32(reset, RX_HALT);
	ret = readl_poll_sleep_timeout(reset, value, !(value & RX_HALT), 10, 1000000);
	if (!ret) {
		clrsetbits_le32(eq, K2_MASK, 0);
		if (readl(eq) != AUTO_GAIN)
			ret = -EIO;
	}
	/* Also release RX on a failed halt or readback. */
	setbits_le32(reset, RX_RESET);
	reset_ret = readl_poll_sleep_timeout(reset, value,
		(value & (RX_RESET | RX_DONE | RX_HALT)) == RX_DONE, 10, 1000000);
	return ret ? ret : reset_ret;
}

int x200_apply_rx_auto(void)
{
	int ret, restore_ret;
	unsigned int lane, restore, changed = 0;

	ret = check_initial_state();
	if (ret) {
		printf("X200_RX_AUTO_FAILED preflight=%d, no writes\n", ret);
		return ret;
	}
	for (lane = 4; lane < 6; lane++) {
		changed = lane;
		ret = reset_rx_auto(lane);
		if (ret)
			goto restore;
		printf("X200_RX_AUTO_APPLIED lane=%u recr0=%08x\n",
		       lane, AUTO_GAIN);
	}
	/* Verify the complete set again after the last lane has been reset. */
	for (lane = 4; lane < 6; lane++) {
		if (readl(lane_reg(lane, 0x50)) != AUTO_GAIN ||
		    (readl(lane_reg(lane, 0x40)) & 0xcd000000) != RX_DONE) {
			ret = -EIO;
			goto restore;
		}
	}
	printf("X200_RX_AUTO_PASS lanes=4,5\n");
	return 0;

restore:
	/* Include the failed lane: a write may precede a readback error. */
	for (restore = changed; restore >= 4; restore--) {
		restore_ret = reset_rx_auto(restore);
		printf("X200_RX_AUTO_RESTORE lane=%u status=%d\n", restore, restore_ret);
	}
	printf("X200_RX_AUTO_FAILED lane=%u status=%d\n", lane, ret);
	return ret;
}
