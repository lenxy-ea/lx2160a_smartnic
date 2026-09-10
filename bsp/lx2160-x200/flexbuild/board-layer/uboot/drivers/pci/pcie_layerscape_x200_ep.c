// SPDX-License-Identifier: GPL-2.0+
/*
 * X200 cold-boot DWC Endpoint bootstrap, not a transport or live rebind API.
 * Board authority: hardware-evidence-v1.json board-lx2160a-rev2,
 * board-pcie5-x8-endpoint and
 * x200-pcie-persistent-linux-publication-design. Register interfaces: pinned NXP U-Boot
 * 4ddbad60eff308a5b356fb9ab8734ac382ddd692 pcie_layerscape{,_ep}.{c,h}.
 * x200-pcie-persistent-linux-publication-design: CFG_READY remains clear.
 * Both PFs have no BAR mappings until the Linux runtime service publishes.
 * The existing 64 KiB reservation is unused; U-Boot never accesses it.
 * MSI remains discoverable on PF0, disabled. MSI-X, SR-IOV and address
 * translation services are disabled and unlinked before host discovery.
 * No inbound or outbound mappings. Linux owns initial host publication;
 * this initializer rejects an already-ready boot or active prior owner.
 */
#include <config.h>
#include <dm.h>
#include <dm/read.h>
#include <dm/ofnode.h>
#include <asm/arch/fsl_serdes.h>
#include <asm/arch/clock.h>
#include <asm/global_data.h>
#include <asm/io.h>
#include <errno.h>
#include <pci_ep.h>
#include <linux/ioport.h>
#include <linux/string.h>
#include "pcie_layerscape.h"

#ifndef CONFIG_TARGET_LX2160X200
#error "The X200 endpoint bootstrap is only for the native X200 target"
#endif

#define X200_COMMAND_INTX_DISABLE BIT(10)
#define X200_MSIX_FLAGS 2
#define X200_MSIX_ENABLE BIT(15)
#define X200_MSIX_MASKALL BIT(14)
#define X200_EXT_NEXT_MASK 0xfff00000U
/* Generic PCIe control fields (Linux include/uapi/linux/pci_regs.h). */
#define X200_ATS_CTRL 0x06
#define X200_ATS_ENABLE BIT(15)
#define X200_PRI_CTRL 0x04
#define X200_PRI_ENABLE BIT(0)
#define X200_PASID_CTRL 0x06
#define X200_PASID_ENABLE BIT(0)

/* Readbacks below validate writable fields, never DBI2 write-only BAR masks. */
static int x200_writeb(u8 value, void *addr)
{
	writeb(value, addr);
	return readb(addr) == value ? 0 : -EIO;
}

static int x200_writew(u16 value, void *addr)
{
	writew(value, addr);
	return readw(addr) == value ? 0 : -EIO;
}

static int x200_writel(u32 value, void *addr)
{
	writel(value, addr);
	return readl(addr) == value ? 0 : -EIO;
}

static int x200_quiesce_caps(void *dbi, unsigned int pf)
{
	unsigned int pos, prev, next, ttl, offset;
	u16 disable;
	bool remove;
	u32 header;
	u16 value;
	u8 id;
	bool msi = false;

	/* A bounded walk rejects malformed/looping chains before ready. */
	prev = PCI_CAPABILITY_LIST;
	pos = readb(dbi + prev);
	for (ttl = 48; pos && ttl; ttl--) {
		if (pos < 0x40 || pos > 0xfc || (pos & 3))
			return -EINVAL;
		id = readb(dbi + pos);
		next = readb(dbi + pos + PCI_CAP_LIST_NEXT);
		if (id == PCI_CAP_ID_MSI) {
			value = readw(dbi + pos + PCI_MSI_FLAGS);
			if (x200_writew(value & ~PCI_MSI_FLAGS_ENABLE,
					dbi + pos + PCI_MSI_FLAGS))
				return -EIO;
			msi = true;
		}
		if (id == PCI_CAP_ID_MSIX) {
			value = readw(dbi + pos + X200_MSIX_FLAGS);
			value = (value & ~X200_MSIX_ENABLE) | X200_MSIX_MASKALL;
			if (x200_writew(value, dbi + pos + X200_MSIX_FLAGS))
				return -EIO;
		}
		if (id == PCI_CAP_ID_MSIX || (pf && id == PCI_CAP_ID_MSI)) {
			if (x200_writeb(next, dbi + prev))
				return -EIO;
		} else {
			prev = pos + PCI_CAP_LIST_NEXT;
		}
		pos = next;
	}
	if (pos || (!pf && !msi))
		return -EINVAL;

	prev = 0;
	pos = 0x100;
	for (ttl = 960; pos && ttl; ttl--) {
		if (pos < 0x100 || pos > 0xffc || (pos & 3))
			return -EINVAL;
		header = readl(dbi + pos);
		if (!header)
			break;
		if (header == 0xffffffff)
			return -EINVAL;
		next = PCI_EXT_CAP_NEXT(header);
		remove = true;
		switch (PCI_EXT_CAP_ID(header)) {
		case PCI_EXT_CAP_ID_SRIOV:
			offset = PCI_SRIOV_CTRL;
			disable = PCI_SRIOV_CTRL_VFE | PCI_SRIOV_CTRL_MSE;
			break;
		case PCI_EXT_CAP_ID_ATS:
			offset = X200_ATS_CTRL;
			disable = X200_ATS_ENABLE;
			break;
		case PCI_EXT_CAP_ID_PRI:
			offset = X200_PRI_CTRL;
			disable = X200_PRI_ENABLE;
			break;
		case PCI_EXT_CAP_ID_PASID:
			offset = X200_PASID_CTRL;
			disable = X200_PASID_ENABLE;
			break;
		default:
			remove = false;
			offset = disable = 0;
		}
		if (remove) {
			/* An inert function must not invite host device-TLB traffic.
			 * Disable first, then unlink: merely clearing Enable permits
			 * the IOMMU driver to enable ATS again during enumeration.
			 * The measured first capability is retained AER at 0x100.
			 */
			if (!prev || pos + offset + sizeof(value) > 0x1000)
				return -EINVAL;
			value = readw(dbi + pos + offset);
			if (x200_writew(value & ~disable, dbi + pos + offset))
				return -EIO;
			header = (readl(dbi + prev) & ~X200_EXT_NEXT_MASK) |
				 (next << 20);
			if (x200_writel(header, dbi + prev))
				return -EIO;
		} else {
			prev = pos;
		}
		pos = next;
	}
	return pos && !ttl ? -EINVAL : 0;
}

static int x200_disable_windows(struct ls_pcie *pcie, u32 direction)
{
	unsigned int index, maximum;

	/* Viewport discovery follows pinned Linux dw_pcie_iatu_detect(). */
	dbi_writel(pcie, 0xff, PCIE_ATU_VIEWPORT);
	maximum = dbi_readl(pcie, PCIE_ATU_VIEWPORT) + 1;
	if (!maximum || maximum > 256)
		return -ENODEV; /* unrolled or unknown register model */
	for (index = 0; index < maximum; index++) {
		dbi_writel(pcie, direction | index, PCIE_ATU_VIEWPORT);
		if (dbi_readl(pcie, PCIE_ATU_VIEWPORT) != (direction | index))
			return -EIO;
		dbi_writel(pcie, 0, PCIE_ATU_CR2);
		if (dbi_readl(pcie, PCIE_ATU_CR2) & PCIE_ATU_ENABLE)
			return -EIO;
		/* No window is enabled while probing target-register presence. */
		dbi_writel(pcie, 0x11110000, PCIE_ATU_LOWER_TARGET);
		if (dbi_readl(pcie, PCIE_ATU_LOWER_TARGET) != 0x11110000)
			break;
		dbi_writel(pcie, 0, PCIE_ATU_LOWER_TARGET);
		dbi_writel(pcie, 0, PCIE_ATU_UPPER_TARGET);
	}
	return index;
}

static int x200_pcie_ep_probe(struct udevice *dev)
{
	struct ls_pcie *pcie = dev_get_priv(dev);
	struct resource regs, ctrl, outbound, memory;
	ofnode backing;
	u32 ready;
	unsigned int pf, bar;
	int ret, inbound, outbound_count;
	void *dbi;

	if (get_svr() != 0x87361120)
		return -ENODEV;
	if (dev_read_resource(dev, 0, &regs) ||
	    dev_read_resource(dev, 1, &ctrl) ||
	    dev_read_resource_byname(dev, "addr_space", &outbound))
		return -EINVAL;
	/* Protect against the old Gen4 resource order (index1 was LUT). */
	if (regs.start != 0x03800000 || resource_size(&regs) != 0x80000 ||
	    ctrl.start != 0x038c0000 || resource_size(&ctrl) != 0x40000 ||
	    outbound.start != 0xa000000000ULL ||
	    resource_size(&outbound) != 0x800000000ULL ||
	    dev_read_bool(dev, "big-endian") ||
	    dev_read_u32_default(dev, "max-functions", 0) != 2)
		return -EINVAL;
	backing = ofnode_parse_phandle(dev_ofnode(dev), "memory-region", 0);
	if (!ofnode_valid(backing) || ofnode_read_resource(backing, 0, &memory) ||
	    !ofnode_read_bool(backing, "no-map") ||
	    memory.start != CFG_SYS_PCI_EP_MEMORY_BASE ||
	    resource_size(&memory) != X200_PCI_EP_MEMORY_SIZE)
		return -EINVAL;
	pcie->dbi = (void *)(uintptr_t)regs.start;
	pcie->ctrl = (void *)(uintptr_t)ctrl.start;
	pcie->idx = 4; /* measured PCIe5 */
	if (!is_serdes_configured(PCIE_SRDS_PRTCL(pcie->idx)))
		return -ENODEV;

	ready = ctrl_readl(pcie, PCIE_PF_CONFIG);
	if (ready & PCIE_CONFIG_READY)
		return -EBUSY; /* cold boot only; do not resize a live host mapping */

	for (pf = 0; pf < 2; pf++) {
		dbi = pcie->dbi + pf * LX2160_PCIE_PF1_OFFSET;
		if ((readb(dbi + PCI_HEADER_TYPE) & 0x7f) != PCI_HEADER_TYPE_NORMAL ||
		    readw(dbi + PCI_VENDOR_ID) != 0x1957 ||
		    (readw(dbi + PCI_COMMAND) & (PCI_COMMAND_MEMORY | PCI_COMMAND_MASTER)))
			return -EBUSY;
	}
	/* Prior ownership was rejected without any MMIO writes. */
	ctrl_writel(pcie, ready & ~PCIE_CONFIG_READY, PCIE_PF_CONFIG);
	if (ctrl_readl(pcie, PCIE_PF_CONFIG) & PCIE_CONFIG_READY)
		return -EIO;
	inbound = x200_disable_windows(pcie, PCIE_ATU_REGION_INBOUND);
	if (inbound < 1)
		return inbound < 0 ? inbound : -ENOSPC;
	outbound_count = x200_disable_windows(pcie, PCIE_ATU_REGION_OUTBOUND);
	if (outbound_count < 0)
		return outbound_count;

	for (pf = 0; pf < 2; pf++) {
		dbi = pcie->dbi + pf * LX2160_PCIE_PF1_OFFSET;
		/* DBI2 masks are write-only: validate by host sizing at cold test. */
		ls_pcie_dbi_ro_wr_dis(pcie);
		for (bar = 0; bar < 6; bar++)
			writel(0,
			       dbi + PCIE_NO_SRIOV_BAR_BASE + PCI_BASE_ADDRESS_0 + 4 * bar);
		writel(0, dbi + PCIE_NO_SRIOV_BAR_BASE + PCI_ROM_ADDRESS);
		ls_pcie_dbi_ro_wr_en(pcie);
		if (!(dbi_readl(pcie, PCIE_MISC_CONTROL_1_OFF) & PCIE_DBI_RO_WR_EN))
			return -EIO;
		for (bar = 0; bar < 6; bar++)
			writel(0, dbi + PCI_BASE_ADDRESS_0 + 4 * bar);
		writel(0, dbi + PCI_ROM_ADDRESS);
		if (!pf && readl(dbi + PCI_BASE_ADDRESS_0)) {
			ls_pcie_dbi_ro_wr_dis(pcie);
			return -EIO;
		}
		ret = x200_writew(X200_COMMAND_INTX_DISABLE, dbi + PCI_COMMAND);
		if (!ret)
			ret = x200_writeb(0, dbi + PCI_INTERRUPT_PIN);
		if (!ret)
			ret = x200_quiesce_caps(dbi, pf);
		ls_pcie_dbi_ro_wr_dis(pcie);
		if (ret)
			return ret;
	}

	/* Linux alone publishes the runtime BAR layout after EPF initialization. */
	mb();
	if (ctrl_readl(pcie, PCIE_PF_CONFIG) & PCIE_CONFIG_READY)
		return -EIO;
	printf("X200-PCIE: Linux publication deferred; CFG_READY=0 PF0/PF1=no-bars "
	       "MSI=off MSI-X=absent VF=absent ATS/PRI/PASID=absent "
	       "iATU=%d/%d inbound=off outbound=off\n",
	       inbound, outbound_count);
	return 0;
}

static int x200_pcie_ep_set_bar(struct udevice *dev, uint fn, struct pci_bar *bar)
{
	/* Initial publication belongs exclusively to the Linux runtime service. */
	return -EPERM;
}

static const struct pci_ep_ops x200_pcie_ep_ops = {
	.set_bar = x200_pcie_ep_set_bar,
};

static const struct udevice_id x200_pcie_ep_ids[] = {
	{ .compatible = "fsl,lx2160ar2-pcie-ep" },
	{ }
};

U_BOOT_DRIVER(pci_layerscape_x200_ep) = {
	.name = "pci_layerscape_x200_ep",
	.id = UCLASS_PCI_EP,
	.of_match = x200_pcie_ep_ids,
	.ops = &x200_pcie_ep_ops,
	.probe = x200_pcie_ep_probe,
	.priv_auto = sizeof(struct ls_pcie),
};
