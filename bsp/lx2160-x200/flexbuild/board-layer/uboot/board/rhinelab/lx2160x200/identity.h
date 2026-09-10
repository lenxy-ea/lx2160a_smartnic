/* SPDX-License-Identifier: GPL-2.0+ */
#ifndef X200_IDENTITY_H
#define X200_IDENTITY_H

struct udevice;

int x200_identity_init(void);
int x200_identity_valid(void);
int x200_identity_get_mac(unsigned int dpmac_id, unsigned char mac[6]);
int x200_identity_apply(struct udevice *dev);
int x200_identity_env_change(struct udevice *dev, const char *value);

#endif
