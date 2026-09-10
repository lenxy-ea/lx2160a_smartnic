// SPDX-License-Identifier: GPL-2.0+
/* Authority: hardware-evidence-v1.json / x200-soc-derived-mac-v1.
 * Read only the public NXP FUID mirror, never keys or fuse instructions.
 */
#include <dm.h>
#include <env.h>
#include <errno.h>
#include <net.h>
#include <net/ldpaa_eth.h>
#include <asm/io.h>
#include <linux/string.h>
#include <u-boot/sha256.h>
#include "identity.h"

#define X200_FUID0 0x01e8021cUL
#define X200_FUID1 0x01e80220UL

static unsigned char identity_mac[4][6];
static int identity_ready;

int x200_identity_valid(void)
{
	return identity_ready;
}

int x200_identity_init(void)
{
	/* sizeof(domain) intentionally includes the separating NUL byte. */
	static const unsigned char domain[] = "x200-mac-v1";
	unsigned char input[sizeof(domain) + 8];
	unsigned char digest[SHA256_SUM_LEN];
	u32 first[2], second[2];
	unsigned int i, j;

	identity_ready = 0;
	memset(identity_mac, 0, sizeof(identity_mac));
	first[0] = in_le32((const void *)X200_FUID0);
	first[1] = in_le32((const void *)X200_FUID1);
	second[0] = in_le32((const void *)X200_FUID0);
	second[1] = in_le32((const void *)X200_FUID1);
	if (first[0] != second[0] || first[1] != second[1] ||
	    (first[0] == 0 && first[1] == 0) ||
	    (first[0] == 0xffffffffU && first[1] == 0xffffffffU)) {
		printf("X200_IDENTITY FAILED: invalid or unstable FUID; Ethernet disabled\n");
		return -EINVAL;
	}
	memcpy(input, domain, sizeof(domain));
	for (i = 0; i < 2; ++i)
		for (j = 0; j < 4; ++j)
			input[sizeof(domain) + i * 4 + j] = first[i] >> (24 - j * 8);
	sha256_csum_wd(input, sizeof(input), digest, CHUNKSZ_SHA256);
	for (i = 0; i < 4; ++i) {
		memcpy(identity_mac[i], digest, 6);
		identity_mac[i][0] = (identity_mac[i][0] & 0xfc) | 0x02;
		identity_mac[i][5] = (identity_mac[i][5] & 0xf8) | i;
	}
	identity_ready = 1;
	printf("X200_IDENTITY READY: SoC FUID derived MAC v1 DPMAC3-6\n");
	return 0;
}

int x200_identity_get_mac(unsigned int dpmac_id, unsigned char mac[6])
{
	memset(mac, 0, 6);
	if (!identity_ready || dpmac_id < 3 || dpmac_id > 6)
		return -EINVAL;
	memcpy(mac, identity_mac[dpmac_id - 3], 6);
	return 0;
}

int x200_identity_env_change(struct udevice *dev, const char *value)
{
	unsigned char expected[6], requested[6];

	if (!value || x200_identity_get_mac(ldpaa_eth_get_dpmac_id(dev), expected))
		return -EPERM;
	string_to_enetaddr(value, requested);
	return memcmp(expected, requested, 6) ? -EPERM : 0;
}

int x200_identity_apply(struct udevice *dev)
{
	struct eth_pdata *pdata = dev_get_plat(dev);
	unsigned int dpmac = ldpaa_eth_get_dpmac_id(dev);
	unsigned char expected[6];
	char name[32], value[18];
	int ret = x200_identity_get_mac(dpmac, expected);

	if (ret) {
		memset(pdata->enetaddr, 0, 6);
		return ret;
	}
	memcpy(pdata->enetaddr, expected, 6);
	/* Environment is a display mirror, never a source of identity. */
	if (dev_seq(dev) == 0)
		strcpy(name, "ethaddr");
	else
		snprintf(name, sizeof(name), "eth%daddr", dev_seq(dev));
	snprintf(value, sizeof(value), "%02x:%02x:%02x:%02x:%02x:%02x",
		 expected[0], expected[1], expected[2],
		 expected[3], expected[4], expected[5]);
	/* eth_env_set_enetaddr only fills a missing MAC and returns -EEXIST
	 * for an existing valid value. This authority mirror must replace it.
	 */
	ret = env_set(name, value);
	if (ret) {
		memset(pdata->enetaddr, 0, 6);
		printf("X200_IDENTITY FAILED: env mirror for DPMAC%u err=%d\n", dpmac, ret);
		return ret;
	}
	/*
	 * eth_initialize() owns an unfinished Net: line while invoking us.
	 * Successful identity details belong to the board table, not this hook.
	 */
	return 0;
}
