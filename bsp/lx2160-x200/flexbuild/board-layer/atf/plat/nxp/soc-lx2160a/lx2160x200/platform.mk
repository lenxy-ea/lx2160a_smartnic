# SPDX-License-Identifier: BSD-3-Clause
# Copyright 2026 RhineLab

BOOT_MODE	?=	flexspi_nor
BOARD		?=	lx2160x200
POVDD_ENABLE	:=	no
NXP_COINED_BB	:=	no

# The board has two controllers and one SPD-backed DIMM per controller.
NUM_OF_DDRC	:=	2
DDRC_NUM_DIMM	:=	1
DDRC_NUM_CS	:=	4
DDR_ECC_EN	:=	yes
DDR_ADDR_DEC	:=	yes
APPLY_MAX_CDD	:=	yes

ERRATA_DDR_A011396	:= 1
ERRATA_DDR_A050450	:= 1

# hardware-evidence-v1.json: two-independent-16mib-flashes and
# board-w25q128-geometry. The selected device reports W25Q128, 16 MiB and
# 64 KiB erase units. W25Q128 selects the generic three-byte SPI-NOR read path.
FLASH_TYPE	:=	W25Q128
XSPI_FLASH_SZ	:=	0x01000000
NXP_XSPI_NOR_UNIT_SIZE	:=	0x10000
BL2_BIN_XSPI_NOR_END_ADDRESS	:=	0x100000
FSPI_ERASE_4K	:=	0

WARM_BOOT	:=	no

# Compact identity messages at each stage's boot console checkpoint.
$(eval $(call add_define,X200_BOOT_BANNER))
BL2_SOURCES += ${BOARD_PATH}/boot_banner.c
BL31_SOURCES += ${BOARD_PATH}/boot_banner.c

# X200 board-level PSCI reset gate; never applies to NXP reference boards.
$(eval $(call add_define,X200_SYSTEM_RESET))
BL31_SOURCES += ${BOARD_PATH}/reset.c

BL2_SOURCES	+=	${BOARD_PATH}/ddr_init.c \
			${BOARD_PATH}/platform.c

SUPPORTED_BOOT_MODE	:=	flexspi_nor

include plat/nxp/common/plat_make_helper/plat_common_def.mk
include plat/nxp/soc-lx2160a/soc.mk
