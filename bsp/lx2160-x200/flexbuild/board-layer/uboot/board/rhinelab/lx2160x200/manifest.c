// SPDX-License-Identifier: GPL-2.0+
/* Read-only x200-json-sha256-v1 metadata and NOR composition verification.
 * Layout authority: flash-layout-v1.json; manifest fields: firmware-manifest-
 * contract-v1.json. SHA256 is integrity only under development-sb-en-0.
 */
#include <config.h>
#include <command.h>
#include <dm.h>
#include <malloc.h>
#include <spi_flash.h>
#include <stdio.h>
#include <linux/errno.h>
#include <linux/string.h>
#include <u-boot/sha256.h>
#include "manifest.h"

#define SLOT_SIZE 0x10000U
#define SLOT_A 0x9c0000U
#define SLOT_B 0x9d0000U
#define HASH_CHUNK 4096U
#define MAX_TOKENS 2048
#define MAX_DEPTH 24
#define COMPONENTS 9
#define CONTAINERS 2

struct token {
	u32 start, end;
	int next;
	char type;
};

struct json {
	const u8 *data;
	u32 len, pos;
	int count;
	struct token *tokens;
};

struct record {
	u32 offset, size;
	u8 digest[SHA256_SUM_LEN];
};

struct manifest {
	u64 sequence;
	u32 length;
	u8 digest[SHA256_SUM_LEN];
	char image[4], profile[96], variant[128], commit[65];
	u32 mc_api_major, mc_api_minor;
	struct record component[COMPONENTS], container[CONTAINERS];
};

static struct x200_flash_summary boot_summary;
static bool boot_summary_valid;

const struct x200_flash_summary *x200_boot_flash_summary(void)
{
	return boot_summary_valid ? &boot_summary : NULL;
}

static u32 le32(const u8 *p)
{
	return (u32)p[0] | (u32)p[1] << 8 | (u32)p[2] << 16 |
	       (u32)p[3] << 24;
}

static u64 le64(const u8 *p)
{
	return le32(p) | (u64)le32(p + 4) << 32;
}

static int hex_digit(u8 c)
{
	if (c >= '0' && c <= '9')
		return c - '0';
	if (c >= 'a' && c <= 'f')
		return c - 'a' + 10;
	return -1;
}

/* Compact ASCII, sorted unescaped keys are the on-media encoding contract.
 * Strict ordering rejects duplicate keys at every level, including unknown
 * provenance objects. Tokens retain structural scope; strings cannot supply
 * keys, and a nested key cannot satisfy a required top-level field.
 */
static int parse_value(struct json *j, unsigned int depth)
{
	struct token *t;
	u32 start;
	int id, key, previous = -1, cmp;
	u8 c;

	if (depth > MAX_DEPTH || j->pos >= j->len || j->count == MAX_TOKENS)
		return -EBADMSG;
	id = j->count++;
	t = &j->tokens[id];
	t->start = j->pos;
	c = j->data[j->pos++];
	t->type = c;
	if (c == '{' || c == '[') {
		u8 close = c == '{' ? '}' : ']';

		if (j->pos < j->len && j->data[j->pos] == close) {
			j->pos++;
			goto done;
		}
		for (;;) {
			if (c == '{') {
				key = j->count;
				if (parse_value(j, depth + 1) < 0 ||
				    j->tokens[key].type != '"')
					return -EBADMSG;
				start = j->tokens[key].start + 1;
				if (memchr(j->data + start, '\\',
					   j->tokens[key].end - start - 1))
					return -EBADMSG;
				if (previous >= 0) {
					u32 a = j->tokens[previous].end -
						j->tokens[previous].start;
					u32 b = j->tokens[key].end - j->tokens[key].start;

					cmp = memcmp(j->data + j->tokens[previous].start + 1,
						     j->data + start, (a < b ? a : b) - 2);
					if (cmp > 0 || (!cmp && a >= b))
						return -EBADMSG;
				}
				previous = key;
				if (j->pos == j->len || j->data[j->pos++] != ':')
					return -EBADMSG;
			}
			if (parse_value(j, depth + 1) < 0 || j->pos == j->len)
				return -EBADMSG;
			if (j->data[j->pos++] == close)
				break;
			if (j->data[j->pos - 1] != ',')
				return -EBADMSG;
		}
	} else if (c == '"') {
		for (;;) {
			if (j->pos == j->len)
				return -EBADMSG;
			c = j->data[j->pos++];
			if (c == '"')
				break;
			if (c < 0x20 || c > 0x7e)
				return -EBADMSG;
			if (c == '\\') {
				if (j->pos == j->len)
					return -EBADMSG;
				c = j->data[j->pos++];
				if (c == 'u') {
					int i;

					for (i = 0; i < 4; i++)
						if (j->pos == j->len || hex_digit(j->data[j->pos++]) < 0)
							return -EBADMSG;
				} else if (!c || !strchr("\"\\/bfnrt", c)) {
					return -EBADMSG;
				}
			}
		}
	} else if (c == 't' || c == 'f' || c == 'n') {
		const char *word = c == 't' ? "true" : c == 'f' ? "false" : "null";
		u32 size = strlen(word);

		if (size > j->len - t->start || memcmp(j->data + t->start, word, size))
			return -EBADMSG;
		j->pos = t->start + size;
	} else {
		/* JSON number grammar; required unsigned integers are checked below. */
		j->pos = t->start;
		if (j->data[j->pos] == '-')
			j->pos++;
		if (j->pos == j->len || j->data[j->pos] < '0' || j->data[j->pos] > '9')
			return -EBADMSG;
		if (j->data[j->pos++] != '0')
			while (j->pos < j->len && j->data[j->pos] >= '0' && j->data[j->pos] <= '9')
				j->pos++;
		if (j->pos < j->len && j->data[j->pos] == '.') {
			start = ++j->pos;
			while (j->pos < j->len && j->data[j->pos] >= '0' && j->data[j->pos] <= '9')
				j->pos++;
			if (start == j->pos)
				return -EBADMSG;
		}
		if (j->pos < j->len && (j->data[j->pos] == 'e' || j->data[j->pos] == 'E')) {
			j->pos++;
			if (j->pos < j->len && (j->data[j->pos] == '+' || j->data[j->pos] == '-'))
				j->pos++;
			start = j->pos;
			while (j->pos < j->len && j->data[j->pos] >= '0' && j->data[j->pos] <= '9')
				j->pos++;
			if (start == j->pos)
				return -EBADMSG;
		}
		t->type = '0';
	}
 done:
	t->end = j->pos;
	t->next = j->count;
	return id;
}

static int equals(const struct json *j, int id, const char *s)
{
	return id >= 0 && j->tokens[id].type == '"' &&
	       j->tokens[id].end - j->tokens[id].start == strlen(s) + 2 &&
	       !memcmp(j->data + j->tokens[id].start + 1, s, strlen(s));
}

static int field(const struct json *j, int object, const char *name)
{
	int i;

	if (object < 0 || j->tokens[object].type != '{')
		return -1;
	for (i = object + 1; i < j->tokens[object].next; i = j->tokens[i + 1].next)
		if (equals(j, i, name))
			return i + 1;
	return -1;
}

static int members(const struct json *j, int object)
{
	int i, count = 0;

	if (object < 0 || j->tokens[object].type != '{')
		return -1;
	for (i = object + 1; i < j->tokens[object].next;
	     i = j->tokens[i + 1].next)
		count++;
	return count;
}

static int string(const struct json *j, int id, char *out, u32 capacity)
{
	u32 size;

	if (id < 0 || j->tokens[id].type != '"')
		return -EBADMSG;
	size = j->tokens[id].end - j->tokens[id].start - 2;
	if (!size || size >= capacity ||
	    memchr(j->data + j->tokens[id].start + 1, '\\', size))
		return -EBADMSG;
	memcpy(out, j->data + j->tokens[id].start + 1, size);
	out[size] = 0;
	return 0;
}

static int integer(const struct json *j, int id, bool offset, u64 *out)
{
	u32 pos, end, base = 10;
	u64 value = 0;
	int digit;

	if (id < 0)
		return -EBADMSG;
	pos = j->tokens[id].start;
	end = j->tokens[id].end;
	if (offset) {
		if (j->tokens[id].type != '"' || end - pos < 5 ||
		    j->data[pos + 1] != '0' || j->data[pos + 2] != 'x')
			return -EBADMSG;
		pos += 3;
		end--;
		base = 16;
	} else if (j->tokens[id].type != '0') {
		return -EBADMSG;
	}
	for (; pos < end; pos++) {
		digit = hex_digit(j->data[pos]);
		if (digit < 0 || digit >= base || value > (~(u64)0 - digit) / base)
			return -EBADMSG;
		value = value * base + digit;
	}
	*out = value;
	return 0;
}

static int digest(const struct json *j, int id, u8 *out)
{
	char text[65];
	int i, a, b;

	if (string(j, id, text, sizeof(text)) || strlen(text) != 64)
		return -EBADMSG;
	for (i = 0; i < 32; i++) {
		a = hex_digit(text[i * 2]);
		b = hex_digit(text[i * 2 + 1]);
		if (a < 0 || b < 0)
			return -EBADMSG;
		out[i] = a * 16 + b;
	}
	return 0;
}

static int record(const struct json *j, int obj, struct record *r,
		  u32 offset, u32 limit, bool version)
{
	u64 off, size;
	int id;

	if (integer(j, field(j, obj, "offset"), true, &off) || off != offset ||
	    integer(j, field(j, obj, "size"), false, &size) || !size || size > limit ||
	    digest(j, field(j, obj, "sha256"), r->digest))
		return -EBADMSG;
	id = field(j, obj, "version");
	if (version && (id < 0 || j->tokens[id].type != '"' ||
			j->tokens[id].end - j->tokens[id].start <= 2))
		return -EBADMSG;
	r->offset = off;
	r->size = size;
	return 0;
}

static int decode(const u8 *payload, u32 length, struct manifest *m)
{
	static const char * const names[] = {
		"rcw", "bl2", "bl31", "uboot", "ddr_phy", "mc", "dpc", "dpl", "dtb"
	};
	static const u32 offsets[] = {
		0, 0x9000, 0x100000, 0x100000, 0x800000, 0xa00000, 0xe00000, 0xd00000, 0xf00000
	};
	static const u32 limits[] = {
		128, 0xf7000, 0x400000, 0x400000, 0x80000, 0x300000, 0x100000, 0x100000, 0x100000
	};
	static const char * const build_fields[] = {
		"git_commit", "timestamp_utc", "toolchain", "flexbuild_commit",
		"sdk_source_lock_sha256", "atf_commit", "uboot_commit", "linux_commit",
		"build_user", "build_host"
	};
	struct json j = { .data = payload, .len = length };
	u64 value;
	int board, components, containers, build, security, target, obj, id, i;
	int ret = -EBADMSG;

	j.tokens = calloc(MAX_TOKENS, sizeof(*j.tokens));
	if (!j.tokens)
		return -ENOMEM;
	if (parse_value(&j, 0) != 0 || j.pos != j.len || j.tokens[0].type != '{')
		goto out;
	for (i = 0; i < j.count; i++)
		if (equals(&j, i, "private_signing_key") ||
		    equals(&j, i, "private_provisioning_key") || equals(&j, i, "plaintext_secret"))
			goto out;
	board = field(&j, 0, "board");
	components = field(&j, 0, "components");
	containers = field(&j, 0, "containers");
	build = field(&j, 0, "build");
	security = field(&j, 0, "security");
	target = field(&j, 0, "image_target");
	/* No declared NOR object may silently escape verification. */
	if (members(&j, components) != COMPONENTS ||
	    members(&j, containers) != CONTAINERS)
		goto out;
	if (!equals(&j, field(&j, 0, "magic"), "X200FW1") ||
	    integer(&j, field(&j, 0, "format_version"), false, &value) || value != 1 ||
	    integer(&j, field(&j, 0, "sequence"), false, &m->sequence) ||
	    !equals(&j, field(&j, board, "model"), "RhineLab LX2160A X200 SmartNIC") ||
	    !equals(&j, field(&j, board, "compatible_mask"), "x200-development-only") ||
	    string(&j, field(&j, 0, "board_profile_id"), m->profile, sizeof(m->profile)) ||
	    strcmp(m->profile, X200_BOARD_PROFILE_ID) ||
	    string(&j, field(&j, 0, "firmware_variant"), m->variant, sizeof(m->variant)) ||
	    string(&j, field(&j, build, "git_commit"), m->commit, sizeof(m->commit)) ||
	    string(&j, field(&j, target, "physical_designator"), m->image, sizeof(m->image)) ||
	    (strcmp(m->image, "D11") && strcmp(m->image, "D12")) ||
	    !equals(&j, field(&j, target, "addressing"), "independent-bank-local") ||
	    integer(&j, field(&j, target, "capacity"), false, &value) || value != 0x1000000 ||
	    !equals(&j, field(&j, security, "secure_boot_policy"), "development-sb-en-0") ||
	    !equals(&j, field(&j, security, "signature_algorithm"), "none") ||
	    !equals(&j, field(&j, security, "signature"), ""))
		goto out;
	id = field(&j, board, "board_revision");
	if (id < 0 || j.tokens[id].type != '"' || j.tokens[id].end - j.tokens[id].start <= 2)
		goto out;
	for (i = 0; i < ARRAY_SIZE(build_fields); i++) {
		id = field(&j, build, build_fields[i]);
		if (id < 0 || j.tokens[id].type != '"' || j.tokens[id].end - j.tokens[id].start <= 2)
			goto out;
	}
	for (i = 0; i < COMPONENTS; i++) {
		obj = field(&j, components, names[i]);
		if (record(&j, obj, &m->component[i], offsets[i], limits[i], true))
			goto out;
		if ((i == 0 || i >= 6) &&
		    !equals(&j, field(&j, obj, "board_profile_id"), m->profile))
			goto out;
		if (i == 0 && (m->component[i].size != 128 ||
			       !equals(&j, field(&j, obj, "scope"), "on-media-rcw-128")))
			goto out;
		if (i >= 1 && i <= 3 &&
		    !equals(&j, field(&j, obj, "container"), i == 1 ? "boot-pbl" : "fip"))
			goto out;
		if (i == 5) {
			if (integer(&j, field(&j, obj, "api_major"), false, &value) ||
			    value > 0xffffffffULL)
				goto out;
			m->mc_api_major = value;
			if (integer(&j, field(&j, obj, "api_minor"), false, &value) ||
			    value > 0xffffffffULL)
				goto out;
			m->mc_api_minor = value;
		}
	}
	if (record(&j, field(&j, containers, "boot-pbl"), &m->container[0], 0, 0x100000, false) ||
	    record(&j, field(&j, containers, "fip"), &m->container[1], 0x100000, 0x400000, false) ||
	    m->container[0].size < 0x9000 + m->component[1].size)
		goto out;
	ret = 0;
 out:
	free(j.tokens);
	return ret;
}

static int read_slot(struct udevice *flash, u32 offset, struct manifest *m)
{
	u8 *slot, sum[SHA256_SUM_LEN];
	u32 length, i;
	int ret;

	slot = malloc(SLOT_SIZE);
	if (!slot)
		return -ENOMEM;
	ret = spi_flash_read_dm(flash, offset, SLOT_SIZE, slot);
	if (ret)
		goto out;
	ret = -EBADMSG;
	if (memcmp(slot, "X200FW1\0", 8) || le32(slot + 8) != 1)
		goto out;
	length = le32(slot + 12);
	if (!length || length > SLOT_SIZE - 16 - SHA256_SUM_LEN)
		goto out;
	for (i = 16 + length + SHA256_SUM_LEN; i < SLOT_SIZE; i++)
		if (slot[i] != 0xff)
			goto out;
	sha256_csum_wd(slot + 16, length, sum, CHUNKSZ_SHA256);
	if (memcmp(sum, slot + 16 + length, sizeof(sum)))
		goto out;
	ret = decode(slot + 16, length, m);
	if (!ret) {
		m->length = length;
		memcpy(m->digest, sum, sizeof(sum));
	}
 out:
	free(slot);
	return ret;
}

/* Compare payload bytes, not labels, sequences or just metadata subsets. */
static int same_payload(struct udevice *flash, const struct manifest *a,
			const struct manifest *b)
{
	u8 data[2][HASH_CHUNK];
	u32 pos, size;
	int ret;

	if (a->length != b->length || memcmp(a->digest, b->digest, SHA256_SUM_LEN))
		return 0;
	for (pos = 0; pos < a->length; pos += size) {
		size = a->length - pos;
		if (size > HASH_CHUNK)
			size = HASH_CHUNK;
		ret = spi_flash_read_dm(flash, SLOT_A + 16 + pos, size, data[0]);
		if (!ret)
			ret = spi_flash_read_dm(flash, SLOT_B + 16 + pos, size, data[1]);
		if (ret)
			return ret;
		if (memcmp(data[0], data[1], size))
			return 0;
	}
	return 1;
}

static int select_manifest(struct udevice **flash, struct manifest *selected)
{
	struct manifest a, b;
	int ret, ra, rb, slot;

	ret = spi_flash_probe_bus_cs(0, 0, flash);
	if (ret) {
		printf("X200-FLASH-ID: image=UNKNOWN sha256=FAIL probe_err=%d\n", ret);
		return ret;
	}
	ra = read_slot(*flash, SLOT_A, &a);
	rb = read_slot(*flash, SLOT_B, &b);
	if (ra && rb) {
		printf("X200-FLASH-ID: image=UNKNOWN sha256=FAIL redundancy=FAIL err_a=%d err_b=%d\n", ra, rb);
		return ra;
	}
	slot = !ra && (rb || a.sequence >= b.sequence) ? 0 : 1;
	*selected = slot ? b : a;
	if (!ra && !rb) {
		ret = same_payload(*flash, &a, &b);
		if (ret < 0)
			return ret;
		if (!ret)
			printf("X200-FLASH-ID: WARN manifests differ; using slot %c (sequence %llu)\n",
			       'A' + slot, (unsigned long long)selected->sequence);
	} else {
		printf("X200-FLASH-ID: WARN only slot %c is valid (A err=%d, B err=%d)\n",
		       'A' + slot, ra, rb);
	}
	return 0;
}

static int hash_region(struct udevice *flash, const struct record *r, u32 offset)
{
	sha256_context ctx;
	u8 buffer[HASH_CHUNK], sum[SHA256_SUM_LEN];
	u32 pos, size;
	int ret;

	sha256_starts(&ctx);
	for (pos = 0; pos < r->size; pos += size) {
		size = r->size - pos;
		if (size > sizeof(buffer))
			size = sizeof(buffer);
		ret = spi_flash_read_dm(flash, offset + pos, size, buffer);
		if (ret)
			return ret;
		sha256_update(&ctx, buffer, size);
	}
	sha256_finish(&ctx, sum);
	return memcmp(sum, r->digest, sizeof(sum)) ? -EBADMSG : 0;
}

static int verify_fip(struct udevice *flash, const struct manifest *m)
{
	static const u8 uuids[2][16] = {
		{ 0x47, 0xd4, 0x08, 0x6d, 0x4c, 0xfe, 0x98, 0x46, 0x9b, 0x95, 0x29, 0x50, 0xcb, 0xbd, 0x5a, 0x00 },
		{ 0xd6, 0xd0, 0xee, 0xa7, 0xfc, 0xea, 0xd5, 0x4b, 0x97, 0x82, 0x99, 0x34, 0xf2, 0x34, 0xb6, 0xe4 }
	};
	const struct record *fip = &m->container[1];
	u8 toc[16 + 3 * 40], zero[16] = { 0 };
	u64 offsets[2], sizes[2];
	int ids[2], i, ret;

	if (fip->size < sizeof(toc))
		return -EBADMSG;
	ret = spi_flash_read_dm(flash, fip->offset, sizeof(toc), toc);
	if (ret)
		return ret;
	if (le32(toc) != 0xaa640001)
		return -EBADMSG;
	for (i = 0; i < 2; i++) {
		const u8 *entry = toc + 16 + i * 40;

		ids[i] = !memcmp(entry, uuids[0], 16) ? 0 :
			 !memcmp(entry, uuids[1], 16) ? 1 : -1;
		offsets[i] = le64(entry + 16);
		sizes[i] = le64(entry + 24);
		if (ids[i] < 0 || offsets[i] < sizeof(toc) || offsets[i] > fip->size ||
		    !sizes[i] || sizes[i] > fip->size - offsets[i] ||
		    sizes[i] != m->component[ids[i] + 2].size)
			return -EBADMSG;
	}
	if (ids[0] == ids[1] ||
	    (offsets[0] < offsets[1] + sizes[1] && offsets[1] < offsets[0] + sizes[0]) ||
	    memcmp(toc + 96, zero, 16) || le64(toc + 112) != fip->size ||
	    le64(toc + 120) || le64(toc + 128))
		return -EBADMSG;
	for (i = 0; i < 2; i++) {
		ret = hash_region(flash, &m->component[ids[i] + 2], fip->offset + offsets[i]);
		if (ret)
			return ret;
	}
	return 0;
}

void x200_report_boot_flash_identity(void)
{
	struct udevice *flash;
	struct manifest selected;

	/* Diagnostics cannot make board late-init fail. Boot policy uses command. */
	boot_summary_valid = false;
	if (!select_manifest(&flash, &selected)) {
		memcpy(boot_summary.image, selected.image, sizeof(boot_summary.image));
		memcpy(boot_summary.profile, selected.profile, sizeof(boot_summary.profile));
		memcpy(boot_summary.variant, selected.variant, sizeof(boot_summary.variant));
		boot_summary.mc_api_major = selected.mc_api_major;
		boot_summary.mc_api_minor = selected.mc_api_minor;
		boot_summary_valid = true;
		printf("X200-FLASH-ID: %s manifest verified\n", selected.image);
	}
}

int x200_flash_verify(void)
{
	struct udevice *flash;
	struct manifest selected;
	int i, ret = select_manifest(&flash, &selected);

	if (ret)
		goto out;
	for (i = 0; i < CONTAINERS; i++) {
		ret = hash_region(flash, &selected.container[i], selected.container[i].offset);
		if (ret) {
			printf("X200-FLASH-VERIFY: container=%s hash=FAIL\n", i ? "fip" : "boot-pbl");
			goto out;
		}
	}
	for (i = 0; i < COMPONENTS; i++) {
		if (i == 2 || i == 3)
			continue;
		ret = hash_region(flash, &selected.component[i], selected.component[i].offset + (i == 0 ? 8 : 0));
		if (ret) {
			printf("X200-FLASH-VERIFY: component_index=%d hash=FAIL\n", i);
			goto out;
		}
	}
	ret = verify_fip(flash, &selected);
 out:
	printf("X200-FLASH-VERIFY: composition=%s components=9 containers=2 err=%d\n",
	       ret ? "FAIL" : "PASS", ret);
	return ret;
}

static int do_x200_flash_verify(struct cmd_tbl *cmdtp, int flag, int argc,
				char *const argv[])
{
	return x200_flash_verify() ? CMD_RET_FAILURE : CMD_RET_SUCCESS;
}

U_BOOT_CMD(x200_flash_verify, 1, 0, do_x200_flash_verify,
	   "verify selected X200 NOR manifest and every component (read-only)", "");
