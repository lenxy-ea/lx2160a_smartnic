/* SPDX-License-Identifier: GPL-2.0+ */
/* Host adapter only: production manifest.c supplies parsing and verification. */
#ifndef X200_MANIFEST_HOST_STUBS_H
#define X200_MANIFEST_HOST_STUBS_H
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <openssl/sha.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define SHA256_SUM_LEN 32
#define CHUNKSZ_SHA256 65536
#define X200_BOARD_PROFILE_ID "x200-s1_12-s2_05-s3_02-v1"
typedef SHA256_CTX sha256_context;
static inline void sha256_starts(sha256_context *ctx) { SHA256_Init(ctx); }
static inline void sha256_update(sha256_context *ctx, const u8 *data, u32 len) { SHA256_Update(ctx, data, len); }
static inline void sha256_finish(sha256_context *ctx, u8 *sum) { SHA256_Final(sum, ctx); }
static inline void sha256_csum_wd(const u8 *data, unsigned int len, u8 *sum, unsigned int chunk) { SHA256(data, len, sum); }
struct udevice { int unused; };
struct cmd_tbl { int unused; };
int spi_flash_probe_bus_cs(unsigned int bus, unsigned int cs, struct udevice **dev);
int spi_flash_read_dm(struct udevice *dev, u32 offset, size_t len, void *data);
#define CMD_RET_FAILURE 1
#define CMD_RET_SUCCESS 0
#define U_BOOT_CMD(name, maxargs, repeatable, fn, usage, help) \
 int host_command(void) { return fn(NULL, 0, 1, NULL); }
#endif
