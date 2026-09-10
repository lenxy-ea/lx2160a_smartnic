/* SPDX-License-Identifier: BSD-3-Clause */
/* Copyright 2026 RhineLab */

#include <common/debug.h>
#include <x200_boot_banner_build.h>

void x200_tfa_boot_banner(const char *stage);

void x200_tfa_boot_banner(const char *stage)
{
	/* Stage is a fixed call-site literal; SHA and UTC are validated at build time. */
	NOTICE("%s: X200 SmartNIC\n", stage);
	NOTICE("%s: BSP %s\n", stage, X200_BANNER_BSP_GIT);
	NOTICE("%s: Built %s\n", stage, X200_BANNER_BUILT_UTC);
}
