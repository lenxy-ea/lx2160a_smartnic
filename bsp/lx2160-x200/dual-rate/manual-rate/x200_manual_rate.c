// SPDX-License-Identifier: GPL-2.0
/* Board authority: hardware-evidence-v1.json,
 * x200-manual-rate-design. This changes only an existing OF property.
 * Userspace owns driver unbind/rebind and link verification; loading is inert.
 */
#include <linux/capability.h>
#include <linux/device.h>
#include <linux/fsl/mc.h>
#include <linux/kref.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/netdevice.h>
#include <linux/of.h>
#include <linux/proc_fs.h>
#include <linux/seq_file.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <net/net_namespace.h>

#define PROFILE "x200-s1_12-s2_05-s3_02-v1"

struct port {
	u32 mac, dpni, lane, initial_rate;
	struct device_node *node;
	struct property *original, *replacement;
	struct of_changeset change;
	bool active, ambiguous, revert_failed;
};

static struct port ports[] = {
	{ .mac = 3, .dpni = 2, .lane = 7, .initial_rate = 10000 },
	{ .mac = 4, .dpni = 3, .lane = 6, .initial_rate = 10000 },
	{ .mac = 5, .dpni = 1, .lane = 5, .initial_rate = 25000 },
	{ .mac = 6, .dpni = 0, .lane = 4, .initial_rate = 25000 },
};
static DEFINE_MUTEX(control_lock);
static struct proc_dir_entry *entry;
static unsigned int active_count;
static int last_error;

static u32 node_rate(struct device_node *node)
{
	const char *mode;

	if (!node || of_property_read_string(node, "phy-mode", &mode))
		return 0;
	if (!strcmp(mode, "10gbase-r"))
		return 10000;
	if (!strcmp(mode, "25gbase-r"))
		return 25000;
	return 0;
}

static struct device_node *port_node(const struct port *port)
{
	char path[80];

	snprintf(path, sizeof(path),
		 "/soc/fsl-mc@80c000000/dpmacs/ethernet@%u", port->mac);
	return of_find_node_by_path(path);
}

static int board_guard(void)
{
	struct device_node *root;
	const char *profile;
	int ret;

	if (!of_machine_is_compatible("rhinelab,lx2160a-x200"))
		return -ENODEV;
	root = of_find_node_by_path("/");
	ret = of_property_read_string(root, "rhinelab,board-profile-id", &profile);
	if (!ret && strcmp(profile, PROFILE))
		ret = -ENODEV;
	of_node_put(root);
	return ret;
}

static int topology_guard(struct port *port, struct device_node *node)
{
	struct of_phandle_args phy;
	struct device_node *pcs, *fixed, *expected;
	const char *managed;
	char path[80];
	u32 reg;
	int ret;

	if (!of_device_is_available(node) ||
	    !of_device_is_compatible(node, "fsl,qoriq-mc-dpmac") ||
	    of_property_count_u32_elems(node, "reg") != 1 ||
	    of_property_read_u32(node, "reg", &reg) || reg != port->mac ||
	    of_property_read_string(node, "managed", &managed) ||
	    strcmp(managed, "in-band-status") ||
	    of_find_property(node, "fixed-link", NULL) ||
	    of_find_property(node, "sfp", NULL) ||
	    of_find_property(node, "phy-handle", NULL) ||
	    of_find_property(node, "phy-connection-type", NULL))
		return -EINVAL;
	fixed = of_get_child_by_name(node, "fixed-link");
	if (fixed) {
		of_node_put(fixed);
		return -EINVAL;
	}
	if (of_count_phandle_with_args(node, "phys", "#phy-cells") != 1)
		return -EINVAL;
	ret = of_parse_phandle_with_args(node, "phys", "#phy-cells", 0, &phy);
	if (ret)
		return ret;
	/* full_name is a basename on flattened-tree nodes in this kernel. */
	expected = of_find_node_by_path("/soc/phy@1ea0000");
	ret = expected && phy.np == expected && phy.args_count == 1 &&
	      phy.args[0] == port->lane ? 0 : -EINVAL;
	of_node_put(expected);
	of_node_put(phy.np);
	if (ret)
		return ret;
	if (of_count_phandle_with_args(node, "pcs-handle", NULL) != 1)
		return -EINVAL;
	pcs = of_parse_phandle(node, "pcs-handle", 0);
	if (!pcs)
		return -EINVAL;
	snprintf(path, sizeof(path), "/soc/mdio@%x/ethernet-phy@0",
		 0x8c03000 + port->mac * 0x4000);
	expected = of_find_node_by_path(path);
	ret = expected && pcs == expected ? 0 : -EINVAL;
	of_node_put(expected);
	of_node_put(pcs);
	return ret;
}

/* Called with both device locks held. Driver removal unregisters the netdev
 * before dropping its DPNI lock; inspect all namespaces, independent of names.
 */
static bool netdev_present(struct device *dpni, struct device *mac)
{
	struct net_device *dev;
	struct net *net;
	bool present = false;

	rcu_read_lock();
	for_each_net_rcu(net) {
		for_each_netdev_rcu(net, dev) {
			if (dev->dev.parent == dpni || dev->dev.parent == mac) {
				present = true;
				goto out;
			}
		}
	}
out:
	rcu_read_unlock();
	return present;
}

static void free_unattached_property(struct property *prop)
{
	if (!prop)
		return;
	kfree(prop->name);
	kfree(prop->value);
	kfree(prop);
}

static void release_change(struct port *port)
{
	of_changeset_destroy(&port->change);
	/* After any apply attempt OF may retain replacement in deadprops. All
	 * three allocations are heap owned and never point into module memory.
	 * Do not free them or remove them from OF's lifetime bookkeeping.
	 */
	of_node_put(port->node);
	port->node = NULL;
	port->original = NULL;
	port->replacement = NULL;
	port->active = false;
	port->ambiguous = false;
	if (!--active_count)
		module_put(THIS_MODULE);
}

static int restore_change(struct port *port)
{
	struct property *active_prop;
	int ret;

	if (!port->active)
		return 0;
	/* A notifier failure on revert cannot be certified by property identity
	 * alone. Keep all state and the module reference for diagnosis.
	 */
	if (port->revert_failed)
		return -EUCLEAN;
	active_prop = of_find_property(port->node, "phy-mode", NULL);
	if (active_prop == port->replacement) {
		ret = of_changeset_revert(&port->change);
		if (ret) {
			port->revert_failed = true;
			port->ambiguous = true;
			return ret;
		}
	} else if (active_prop != port->original) {
		port->ambiguous = true;
		return -EUCLEAN;
	}
	if (of_find_property(port->node, "phy-mode", NULL) != port->original ||
	    node_rate(port->node) != port->initial_rate) {
		port->ambiguous = true;
		return -EUCLEAN;
	}
	release_change(port);
	return 0;
}

static int change_rate(struct port *port, struct device_node *node, u32 rate)
{
	struct property *prop;
	const char *mode = rate == 10000 ? "10gbase-r" : "25gbase-r";
	int ret;

	if (port->active) {
		if (rate == port->initial_rate)
			return restore_change(port);
		if (port->ambiguous ||
		    of_find_property(node, "phy-mode", NULL) != port->replacement ||
		    node_rate(node) != rate)
			return -EUCLEAN;
		return 0;
	}
	if (node_rate(node) != port->initial_rate)
		return -EUCLEAN;
	if (rate == port->initial_rate)
		return 0;
	prop = kzalloc(sizeof(*prop), GFP_KERNEL);
	if (!prop)
		return -ENOMEM;
	prop->name = kstrdup("phy-mode", GFP_KERNEL);
	prop->value = kstrdup(mode, GFP_KERNEL);
	prop->length = strlen(mode) + 1;
	of_property_set_flag(prop, OF_DYNAMIC);
	if (!prop->name || !prop->value) {
		free_unattached_property(prop);
		return -ENOMEM;
	}
	of_changeset_init(&port->change);
	ret = of_changeset_update_property(&port->change, node, prop);
	if (ret) {
		of_changeset_destroy(&port->change);
		free_unattached_property(prop);
		return ret;
	}
	port->node = of_node_get(node);
	port->original = of_find_property(node, "phy-mode", NULL);
	port->replacement = prop;
	port->active = true;
	port->revert_failed = false;
	if (!active_count++)
		__module_get(THIS_MODULE);
	ret = of_changeset_apply(&port->change);
	if (ret || of_find_property(node, "phy-mode", NULL) != prop ||
	    node_rate(node) != rate) {
		port->ambiguous = true;
		return ret ?: -EUCLEAN;
	}
	return 0;
}

static int execute(struct port *port, u32 rate)
{
	struct device *dpni, *mac;
	struct device_node *node;
	char name[24];
	int ret;

	ret = board_guard();
	if (ret)
		return ret;
	node = port_node(port);
	if (!node)
		return -ENODEV;
	if (port->active && port->node != node) {
		ret = -EUCLEAN;
		goto put_node;
	}
	snprintf(name, sizeof(name), "dpni.%u", port->dpni);
	dpni = bus_find_device_by_name(&fsl_mc_bus_type, NULL, name);
	snprintf(name, sizeof(name), "dpmac.%u", port->mac);
	mac = bus_find_device_by_name(&fsl_mc_bus_type, NULL, name);
	if (!dpni || !mac) {
		ret = -ENODEV;
		goto put_devices;
	}
	/* Match DPAA2 removal's DPNI -> DPMAC ordering. The locks prevent
	 * concurrent sysfs/deferred driver binding throughout the OF operation.
	 */
	device_lock(dpni);
	device_lock(mac);
	if (!device_is_registered(dpni) || !device_is_registered(mac) ||
	    !is_fsl_mc_bus_dpni(to_fsl_mc_device(dpni)) ||
	    !is_fsl_mc_bus_dpmac(to_fsl_mc_device(mac)) ||
	    to_fsl_mc_device(dpni)->obj_desc.id != port->dpni ||
	    to_fsl_mc_device(mac)->obj_desc.id != port->mac ||
	    dpni->parent != mac->parent) {
		ret = -ENODEV;
		goto unlock;
	}
	if (dpni->driver || mac->driver || netdev_present(dpni, mac)) {
		ret = -EBUSY;
		goto unlock;
	}
	ret = topology_guard(port, node);
	if (!ret)
		ret = change_rate(port, node, rate);
unlock:
	device_unlock(mac);
	device_unlock(dpni);
put_devices:
	put_device(mac);
	put_device(dpni);
put_node:
	of_node_put(node);
	return ret;
}

/* Count only public list links, never inspect private devres allocations.
 * A count is a lifetime diagnostic, not a PHY reference count. Compare only
 * quiescent snapshots at the same bound/unbound phase of repeated cycles.
 */
static unsigned int devres_count(struct device *dev)
{
	struct list_head *item;
	unsigned long flags;
	unsigned int count = 0;

	spin_lock_irqsave(&dev->devres_lock, flags);
	list_for_each(item, &dev->devres_head)
		count++;
	spin_unlock_irqrestore(&dev->devres_lock, flags);
	return count;
}

static void show_devres(struct seq_file *m, struct port *port,
			struct device_node *node)
{
	struct device *dpni, *mac;
	unsigned int dpni_count = 0, mac_count = 0;
	unsigned int dpni_refs = 0, mac_refs = 0;
	char name[24];
	int ret;

	ret = board_guard();
	if (ret)
		goto report;
	if (!node) {
		ret = -ENODEV;
		goto report;
	}
	ret = topology_guard(port, node);
	if (ret)
		goto report;
	snprintf(name, sizeof(name), "dpni.%u", port->dpni);
	dpni = bus_find_device_by_name(&fsl_mc_bus_type, NULL, name);
	snprintf(name, sizeof(name), "dpmac.%u", port->mac);
	mac = bus_find_device_by_name(&fsl_mc_bus_type, NULL, name);
	if (!dpni || !mac) {
		ret = -ENODEV;
		goto put_devices;
	}
	/* Same lock order as execute() and DPAA2 driver removal. */
	device_lock(dpni);
	device_lock(mac);
	if (!device_is_registered(dpni) || !device_is_registered(mac) ||
	    !is_fsl_mc_bus_dpni(to_fsl_mc_device(dpni)) ||
	    !is_fsl_mc_bus_dpmac(to_fsl_mc_device(mac)) ||
	    to_fsl_mc_device(dpni)->obj_desc.id != port->dpni ||
	    to_fsl_mc_device(mac)->obj_desc.id != port->mac ||
	    dpni->parent != mac->parent) {
		ret = -ENODEV;
	} else {
		dpni_count = devres_count(dpni);
		mac_count = devres_count(mac);
		/* Each count includes our one bus lookup reference. Other users
		 * may hold transient references; compare settled lifecycle phases.
		 */
		dpni_refs = kref_read(&dpni->kobj.kref);
		mac_refs = kref_read(&mac->kobj.kref);
	}
	device_unlock(mac);
	device_unlock(dpni);
put_devices:
	put_device(mac);
	put_device(dpni);
report:
	seq_printf(m, "mac%u_devres_error=%d\n", port->mac, ret);
	if (!ret)
		seq_printf(m, "mac%u_dpni_devres_entries=%u\n"
			   "mac%u_dpmac_devres_entries=%u\n"
			   "mac%u_dpni_device_kref=%u\n"
			   "mac%u_dpmac_device_kref=%u\n",
			   port->mac, dpni_count, port->mac, mac_count,
			   port->mac, dpni_refs, port->mac, mac_refs);
}

static int show(struct seq_file *m, void *unused)
{
	unsigned int i;

	mutex_lock(&control_lock);
	seq_printf(m, "schema_version=1\npinned=%u\nlast_error=%d\n"
		   "device_kref_includes_observer_held_ref=1\n",
		   !!active_count, last_error);
	for (i = 0; i < ARRAY_SIZE(ports); i++) {
		struct port *port = &ports[i];
		struct device_node *node = port_node(port);
		u32 rate = node_rate(node);
		bool ambiguous = port->ambiguous ||
			(port->active ?
			 (node != port->node ||
			  of_find_property(node, "phy-mode", NULL) != port->replacement) :
			 rate != port->initial_rate);

		seq_printf(m, "mac%u_dpni=dpni.%u\nmac%u_lane=%u\n"
			   "mac%u_initial_rate=%u\nmac%u_rate=%u\n"
			   "mac%u_active=%u\nmac%u_state=%s\n",
			   port->mac, port->dpni, port->mac, port->lane,
			   port->mac, port->initial_rate, port->mac, rate,
			   port->mac, port->active, port->mac,
			   ambiguous ? "ambiguous" : port->active ? "changed" : "original");
		show_devres(m, port, node);
		of_node_put(node);
	}
	mutex_unlock(&control_lock);
	return 0;
}

static int open_state(struct inode *inode, struct file *file)
{
	int ret;

	if (!try_module_get(THIS_MODULE))
		return -ENODEV;
	ret = single_open(file, show, NULL);
	if (ret)
		module_put(THIS_MODULE);
	return ret;
}

static int release_state(struct inode *inode, struct file *file)
{
	int ret = single_release(inode, file);

	module_put(THIS_MODULE);
	return ret;
}

static ssize_t command(struct file *file, const char __user *buf,
		       size_t count, loff_t *pos)
{
	char text[48], verb[8], mac_text[12], rate_text[12], extra;
	u32 mac = 0, rate = 0;
	unsigned int i;
	int ret = -EINVAL, fields;
	bool restore = false;

	if (!capable(CAP_SYS_ADMIN))
		return -EPERM;
	if (!count || count >= sizeof(text))
		return -EINVAL;
	if (copy_from_user(text, buf, count))
		return -EFAULT;
	if (memchr(text, '\0', count))
		return -EINVAL;
	text[count] = '\0';
	fields = sscanf(text, "%7s %11s %11s %c", verb, mac_text,
			rate_text, &extra);
	if (fields == 2 && !strcmp(verb, "restore")) {
		restore = true;
	} else if (fields != 3 || strcmp(verb, "set") ||
		   kstrtou32(rate_text, 10, &rate))
		return -EINVAL;
	if (kstrtou32(mac_text, 10, &mac))
		return -EINVAL;
	if (!restore && rate != 10000 && rate != 25000)
		return -EINVAL;
	mutex_lock(&control_lock);
	for (i = 0; i < ARRAY_SIZE(ports); i++) {
		if (ports[i].mac != mac)
			continue;
		ret = execute(&ports[i], restore ? ports[i].initial_rate : rate);
		break;
	}
	last_error = ret;
	mutex_unlock(&control_lock);
	return ret ? ret : count;
}

static const struct proc_ops ops = {
	.proc_open = open_state, .proc_read = seq_read, .proc_lseek = seq_lseek,
	.proc_release = release_state, .proc_write = command,
};

static int __init manual_rate_init(void)
{
	entry = proc_create("x200_manual_rate", 0600, NULL, &ops);
	return entry ? 0 : -ENOMEM;
}

static void __exit manual_rate_exit(void)
{
	/* Active or ambiguous changes hold a global reference: normal rmmod
	 * cannot reach here until every original property is restored.
	 */
	proc_remove(entry);
}

module_init(manual_rate_init);
module_exit(manual_rate_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Explicit X200 per-port 10G/25G OF mode control");
