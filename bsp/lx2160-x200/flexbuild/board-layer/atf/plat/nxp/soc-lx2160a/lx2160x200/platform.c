/*
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright 2026 RhineLab
 */

#include <plat_common.h>

/* X200 does not use the RDB POVDD control hook. */
bool board_enable_povdd(void)
{
	return false;
}

bool board_disable_povdd(void)
{
	return false;
}
