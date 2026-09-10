/* SPDX-License-Identifier: GPL-2.0+ */
#ifndef X200_MANIFEST_H
#define X200_MANIFEST_H

struct x200_flash_summary {
	char image[4], profile[96], variant[128];
	unsigned int mc_api_major, mc_api_minor;
};

/* NULL until the existing late-init manifest report succeeds. */
const struct x200_flash_summary *x200_boot_flash_summary(void);
void x200_report_boot_flash_identity(void);
int x200_flash_verify(void);

#endif
