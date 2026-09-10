#!/usr/bin/env python3
"""Install the X200-only immutable SoC-MAC authority into pinned U-Boot."""
from pathlib import Path
import shutil

HERE = Path(__file__).resolve().parent


def replace_once(path, old, new):
    source = path.read_text()
    if new in source:
        return
    if source.count(old) != 1:
        raise ValueError(f'X200 identity anchor mismatch: {path}')
    path.write_text(source.replace(old, new, 1))


def install(tree):
    tree = Path(tree)
    shutil.copyfile(HERE / 'uboot/board/rhinelab/lx2160x200/identity.h',
                    tree / 'include/x200_identity.h')
    eth = tree / 'net/eth-uclass.c'
    include = ('#include <eth_phy.h>\n'
               '#ifdef CONFIG_TARGET_LX2160X200\n'
               '#include <x200_identity.h>\n#include <net/ldpaa_eth.h>\n#endif')
    replace_once(eth, '#include <eth_phy.h>', include)
    old = '\t/* seq is valid since the device is active */'
    new = '''#ifdef CONFIG_TARGET_LX2160X200
	/* Repair even a forced/imported environment before every hardware write. */
	if (!strcmp(dev->driver->name, LDPAA_ETH_DRIVER_NAME)) {
		ret = x200_identity_apply(dev);
		if (ret)
			return ret;
	}
#endif

''' + old
    replace_once(eth, old, new)
    old = '\t\tstruct eth_pdata *pdata = dev_get_plat(dev);\n\t\tswitch (op) {'
    new = '''		struct eth_pdata *pdata = dev_get_plat(dev);
#ifdef CONFIG_TARGET_LX2160X200
		/* Accept only the authority mirror. Returning here avoids recursion
		 * when x200_identity_apply() updates the environment itself.
		 */
		if (!strcmp(dev->driver->name, LDPAA_ETH_DRIVER_NAME))
			return x200_identity_env_change(dev,
				op == env_op_delete ? NULL : value) ? 1 : 0;
#endif
		switch (op) {'''
    replace_once(eth, old, new)
    old = '\tret = eth_get_ops(dev)->start(dev);'
    new = """#ifdef CONFIG_TARGET_LX2160X200
\tif (!strcmp(dev->driver->name, LDPAA_ETH_DRIVER_NAME)) {
\t\tret = x200_identity_apply(dev);
\t\tif (ret)
\t\t\treturn ret;
\t}
#endif

""" + old
    replace_once(eth, old, new)
    old = '\t/* Check if the device has a valid MAC address in device tree */'
    new = '''#ifdef CONFIG_TARGET_LX2160X200
	/* X200 never enters the environment/ROM/random identity selection. */
	if (!strcmp(dev->driver->name, LDPAA_ETH_DRIVER_NAME))
		return eth_write_hwaddr(dev);
#endif

''' + old
    replace_once(eth, old, new)
    mc = tree / 'drivers/net/fsl-mc/mc.c'
    old = '#include <net.h>'
    new = old + '\n#ifdef CONFIG_TARGET_LX2160X200\n#include <x200_identity.h>\n#endif'
    replace_once(mc, old, new)
    old = '\tvoid *val = NULL;\n\n\tswitch (type) {'
    new = '''	void *val = NULL;

#ifdef CONFIG_TARGET_LX2160X200
	/* DPC/DPL publish only the UID-derived value, including after env import. */
	err = x200_identity_apply(eth_dev);
	if (err)
		return err;
#endif

	switch (type) {'''
    replace_once(mc, old, new)
    for old in ('\tmc_ram_num_256mb_blocks = mc_ram_size / MC_RAM_SIZE_ALIGNMENT;',
                '\tif (!mc_dpl_addr)\n\t\treturn -1;'):
        new = """#ifdef CONFIG_TARGET_LX2160X200
\tif (!x200_identity_valid()) {
\t\tprintf("X200_IDENTITY FAILED: MC network publication denied\\n");
\t\treturn -EINVAL;
\t}
#endif

""" + old
        replace_once(mc, old, new)
