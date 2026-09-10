/* SPDX-License-Identifier: GPL-2.0+ */
/* LX2160A X200 SmartNIC */

#ifndef __LX2160X200_H
#define __LX2160X200_H

#include "lx2160a_common.h"

/*
 * Retain the unused PCIe reservation outside all load addresses.
 * hardware-evidence-v1.json: x200-pcie-persistent-linux-publication-design.
 * U-Boot leaves both PFs without BAR mappings and CFG_READY clear. Linux
 * publishes the runtime layout; this 64 KiB remains excluded from boot loads
 * and Linux ordinary memory.
 */
#define CFG_SYS_PCI_EP_MEMORY_BASE	0x94000000ULL
#define X200_PCI_EP_MEMORY_SIZE		0x00010000ULL

/* X200 wiring: one SPD per DDR controller and M41T11 on I2C3. */
#undef SPD_EEPROM_ADDRESS1
#undef SPD_EEPROM_ADDRESS2
#undef SPD_EEPROM_ADDRESS3
#undef SPD_EEPROM_ADDRESS4
#undef SPD_EEPROM_ADDRESS5
#undef SPD_EEPROM_ADDRESS6
#undef SPD_EEPROM_ADDRESS
#define SPD_EEPROM_ADDRESS1		0x50
#define SPD_EEPROM_ADDRESS2		0x51
#define SPD_EEPROM_ADDRESS		SPD_EEPROM_ADDRESS1

#undef CFG_SYS_I2C_RTC_ADDR
#define CFG_SYS_I2C_RTC_ADDR		0x68

/*
 * X200 SATA boot policy.
 *
 * The selected NOR contains MC/DPC/DPL and the profile-bound board DTB. The
 * Linux Image and Debian live on SATA0 using GPT partition 1 for /boot and
 * partition 2 for rootfs.
 * Verify the selected NOR composition before loading the SATA boot script.
 * A failed verification or missing medium/file returns to the U-Boot shell.
 */
#define X200_BOARD_PROFILE_ID	"x200-s1_12-s2_05-s3_02-v1"
#define X200_FDT_FILE		"fsl-lx2160a-x200.dtb"
#define X200_CONSOLE_ARGS	"console=ttyAMA0,115200 " \
				"earlycon=pl011,mmio32,0x21c0000"
#define X200_MC_BOOT_COMMAND	"sf probe 0:0 && " \
	"sf read ${x200_mc_addr_r} 0xa00000 0x300000 && " \
	"sf read ${x200_dpc_addr_r} 0xe00000 0x100000 && " \
	"fsl_mc start mc ${x200_mc_addr_r} ${x200_dpc_addr_r}"
#define X200_DPL_STAGE_COMMAND	"sf read ${x200_dpl_addr_r} 0xd00000 " \
				"0x100000 && fsl_mc lazyapply dpl " \
				"${x200_dpl_addr_r}"
#define X200_FDT_STAGE_COMMAND	"sf read ${fdt_addr_r} 0xf00000 0x100000"
#define X200_BOOT_COMMAND	"if x200_flash_verify && scsi reset && load scsi " \
	"${x200_scsi_dev}:${x200_boot_part} ${scriptaddr} /boot.scr; " \
	"then source ${scriptaddr}; else " \
	"echo X200-BOOT: Flash verification or boot media failed - entering shell; false; fi"

#undef CFG_EXTRA_ENV_SETTINGS
#define CFG_EXTRA_ENV_SETTINGS					\
	"board=lx2160-x200\0"				\
	"BOARD=lx2160-x200\0"				\
	"fdtfile=" X200_FDT_FILE "\0"			\
	"board_profile_id=" X200_BOARD_PROFILE_ID "\0"	\
	"console=ttyAMA0,115200\0"				\
	"x200_console_args=" X200_CONSOLE_ARGS "\0"		\
	"fdt_addr_r=0x90000000\0"				\
	"kernel_addr_r=0x81000000\0"			\
	"scriptaddr=0x80000000\0"				\
	"load_addr=0xa0000000\0"				\
	"x200_mc_addr_r=0x80a00000\0"			\
	"x200_dpl_addr_r=0x80d00000\0"			\
	"x200_dpc_addr_r=0x80e00000\0"			\
	"x200_scsi_dev=0\0"					\
	"x200_boot_part=1\0"				\
	"x200_root_part=2\0"				\
	"x200_mc_boot=" X200_MC_BOOT_COMMAND "\0"		\
	"x200_dpl_stage=" X200_DPL_STAGE_COMMAND "\0"	\
	"x200_fdt_stage=" X200_FDT_STAGE_COMMAND "\0"	\
	"mcinitcmd=run x200_mc_boot\0"				\
	"x200_boot=" X200_BOOT_COMMAND "\0"			\
	"fsl_bootcmd_mcinitcmd_set=y\0"			\
	"x200_pcie_ep_mem=0x94000000\0"			\
	"x200_pcie_ep_mem_size=0x10000\0"

#include <asm/fsl_secure_boot.h>

#endif /* __LX2160X200_H */
