"""Offline adapter for the pinned X200 redundant SPI environment backend.

Only CONFIG_TARGET_LX2160X200 changes behavior. No build integration or hardware
access is performed here; the caller supplies the pinned env/sf.c text.
"""
import hashlib

COMMIT = '4ddbad60eff308a5b356fb9ab8734ac382ddd692'
SOURCE_SHA256 = '1b75fce3515c6a35bddcf0c909bce2a2f4d89813177e2696625af151e699f32f'
ADAPTED_SHA256 = '2ee688f9a9cbef7cd1d46a58c9e63a63beb155eda606a82fbbf083784479d672'

LOAD_HELPERS = r'''
#if defined(CONFIG_TARGET_LX2160X200)
/* Inspect only successfully read buffers; erased NOR is not CRC corruption. */
static bool x200_env_erased(const env_t *env)
{
	const unsigned char *bytes = (const unsigned char *)env;
	size_t i;

	for (i = 0; i < CONFIG_ENV_SIZE; i++)
		if (bytes[i] != 0xff)
			return false;
	return true;
}

/* Verify every byte before advancing the redundant-slot transaction. */
static int x200_env_verify(struct spi_flash *flash, u32 offset,
			   const void *expected, size_t length)
{
	void *actual;
	int ret;

	actual = memalign(ARCH_DMA_MINALIGN, length);
	if (!actual)
		return -ENOMEM;
	ret = spi_flash_read(flash, offset, length, actual);
	if (!ret && memcmp(actual, expected, length))
		ret = -EIO;
	free(actual);
	if (ret)
		printf("X200-ENV: status=verify-error offset=0x%x err=%d\n",
		       offset, ret);
	return ret;
}

static int x200_env_import(env_t *primary, int primary_error,
			   env_t *redundant, int redundant_error)
{
	int ret;

	if (primary_error || redundant_error) {
		printf("X200-ENV: status=io-error primary=%d redundant=%d\n",
		       primary_error, redundant_error);
	}
	if (!primary_error && !redundant_error &&
	    x200_env_erased(primary) && x200_env_erased(redundant)) {
		gd->env_valid = ENV_INVALID;
		puts("X200-ENV: status=blank; using defaults (not saved)\n");
		env_set_default(NULL, 0);
		return -ENOMSG;
	}
	/* Retain upstream recovery from the other readable, valid slot. */
	ret = env_import_redund((char *)primary, primary_error,
				(char *)redundant, redundant_error, H_EXTERNAL);
	if (!ret)
		printf("X200-ENV: status=valid selected=%s\n",
		       gd->env_valid == ENV_VALID ? "primary" : "redundant");
	else if (ret == -ENOMSG && !primary_error && !redundant_error)
		puts("X200-ENV: status=corrupt\n");
	return ret;
}
#endif

'''


def adapt_sf(source):
    """Return patched source, rejecting drift or accidental double application."""
    if hashlib.sha256(source.encode()).hexdigest() != SOURCE_SHA256:
        raise ValueError('X200 env adapter requires pristine pinned env/sf.c')
    old = '\tgd->env_valid = gd->env_valid == ENV_REDUND ? ENV_VALID : ENV_REDUND;'
    new = '''#if defined(CONFIG_TARGET_LX2160X200)
	/* Initial ENV_INVALID also writes primary: record the actual destination. */
	gd->env_valid = env_new_offset == CONFIG_ENV_OFFSET ? ENV_VALID : ENV_REDUND;
#else
''' + old + '\n#endif'
    assert source.count(old) == 1
    source = source.replace(old, new)
    marker = '#if defined(CONFIG_ENV_OFFSET_REDUND)\nstatic int env_sf_save(void)'
    assert source.count(marker) == 1
    source = source.replace(marker, '#if defined(CONFIG_ENV_OFFSET_REDUND)\n' +
                            LOAD_HELPERS + 'static int env_sf_save(void)')
    old = '''	ret = env_import_redund((char *)tmp_env1, read1_fail, (char *)tmp_env2,
				read2_fail, H_EXTERNAL);'''
    new = '''#if defined(CONFIG_TARGET_LX2160X200)
	ret = x200_env_import(tmp_env1, read1_fail, tmp_env2, read2_fail);
#else
''' + old + '\n#endif'
    assert source.count(old) == 1
    source = source.replace(old, new)
    # Verify new contents before invalidating the old copy, and verify the
    # obsolete flag before reporting success. The helper releases its buffer
    # on read/compare errors; all verification failures propagate to the caller.
    old = "\tret = spi_flash_write(env_flash, env_offset + offsetof(env_t, flags),"
    new = """#if defined(CONFIG_TARGET_LX2160X200)
	ret = x200_env_verify(env_flash, env_new_offset, &env_new, CONFIG_ENV_SIZE);
	if (ret)
		goto done;
#endif

""" + old
    assert source.count(old) == 1
    source = source.replace(old, new)
    old = """				sizeof(env_new.flags), &flag);
	if (ret)
		goto done;

	puts("done\\n");"""
    new = """				sizeof(env_new.flags), &flag);
	if (ret)
		goto done;
#if defined(CONFIG_TARGET_LX2160X200)
	ret = x200_env_verify(env_flash, env_offset + offsetof(env_t, flags),
			      &flag, sizeof(flag));
	if (ret)
		goto done;
#endif

	puts("done\\n");"""
    assert source.count(old) == 1
    source = source.replace(old, new)
    # Diagnose probe failure in the redundant loader without changing its error
    # handling or any non-X200 build. The other setup call sites are untouched.
    start = source.index('static int env_sf_load(void)')
    end = source.index('\n#else\nstatic int env_sf_save', start)
    load = source[start:end]
    old = '''	ret = setup_flash_device(&env_flash);
	if (ret)
		goto out;'''
    new = '''	ret = setup_flash_device(&env_flash);
	if (ret) {
#if defined(CONFIG_TARGET_LX2160X200)
		printf("X200-ENV: status=io-error probe=%d\\n", ret);
#endif
		goto out;
	}'''
    assert load.count(old) == 1
    source = source[:start] + load.replace(old, new) + source[end:]
    return source
