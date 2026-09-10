# Build

Use a Linux x86-64 host with Git, GNU Make, Python 3 (3.12 recommended), a C
compiler and OpenSSL development headers/library for source tests, `patch`,
`binutils` (`readelf`), Podman, and `kmod` tools (`modinfo`, `modprobe`).
Run the complete image pipeline from a root shell with a root-owned checkout
and rootful Podman. Debian ARM64 package setup requires writable, mounted
`/proc/sys/fs/binfmt_misc`. The builder registers a temporary, uniquely named
QEMU handler and removes only that handler when the operation finishes. Other
host handlers are not changed. The container does not need privileged mode.
Allow at least 80 GiB free disk space for source trees, kernel modules, rootfs
and the 32,017,047,552-byte sparse SATA image. Start with four compiler jobs
on a host with 16 GiB RAM; CPU count alone is not a suitable job limit.

The shared container recipe pins a public Debian base by digest and specifies
tool package versions. It includes the cross compiler, device-tree tools,
image utilities and ARM userspace emulation. Source and firmware downloads
are checked against their pinned hashes. Package retrieval needs network
access; firmware and kernel compiler containers run without network access.

## Source and toolchain

Run these commands from the repository root. Commit intentional source edits
before firmware builds: generated manifests identify the source commit.

```sh
make -C bsp/lx2160-x200 check test
make -C bsp/lx2160-x200 toolchain
make -C bsp/lx2160-x200 firmware JOBS=4
make -C bsp/lx2160-x200 kernel JOBS=4
```

Firmware outputs are under `build/firmware/`, including independently packed
D11 and D12 images and an input/output manifest. The kernel producer writes
`build/kernel/manifest.json`, Image, vmlinux, matching modules and ABI checks.
Do not interchange modules from another build with the same release string.

Output directories must be new. For another build select a new `FIRMWARE_OUT`
or `KERNEL_OUT`; keep that same `KERNEL_OUT` for subsequent packaging.
`KERNEL_MANIFEST` defaults to its `manifest.json`; if overriding the manifest,
also set `KERNEL_OUT` to the matching kernel build directory. Download caches can be retained because their content is
verified against pinned hashes.

## Runtime and Debian

Copy `bsp/lx2160-x200/pcie-management/config.example.json` to
`config.local.json` at the repository root and edit it for your host and card.
The example uses documentation addresses and illustrative PCI BDFs; it is not
a detected hardware configuration. Supply a public SSH key for the operator
account. No password or private key is embedded in source.

```sh
make -C bsp/lx2160-x200 manual-rate runtime CONFIG="$PWD/config.local.json"
make -C bsp/lx2160-x200 bootstrap
make -C bsp/lx2160-x200 rootfs AUTHORIZED_KEY="$HOME/.ssh/id_ed25519.pub" OPERATOR=x200
make -C bsp/lx2160-x200 sata
```

The bootstrap step downloads and configures Debian packages. The rootfs step
combines that verified userspace, the current kernel's modules and the checked
runtime overlay. The SATA step consumes the verified rootfs archive and kernel
and writes a GPT image with a 1 GiB boot partition and a root partition.
No loop device, physical disk or target connection is required.

The default geometry is 62,533,296 sectors of 512 bytes. Outputs include
`build/os/x200-debian13-rootfs.tar.zst`, `build/os/x200-debian13-sata.img.zst`,
package versions and producer manifests. A rootfs built without an authorized
key has no provisioned operator login; root remains locked. Such an image is
useful for build validation but needs offline account provisioning before use.

Debian package versions are recorded; the complete rootfs is not promised
byte reproducible. Build timestamps use the actual time by default. Set
`SOURCE_DATE_EPOCH` consistently when reproducing time-dependent boot/kernel
outputs, together with the same source and dependency/toolchain versions.

## Host driver package

Build the host modules on the intended x86-64 Linux host. Install its exact
running-kernel development headers and native NTB modules first. The builder
fetches the pinned public driver source and checks the replacement transport
against the host's symbol CRCs and native netdev module:

```sh
python3 bsp/lx2160-x200/pcie-management/build_vntb_host.py \
  --kernel-headers "/lib/modules/$(uname -r)/build" \
  --native-modules "/lib/modules/$(uname -r)/kernel" \
  --out "$PWD/build/host-vntb"
```

The resulting bundle and manifest are in `build/host-vntb/bundle/`. Host ABI
support depends on the supplied headers and native modules; a different kernel
requires a matching rebuild. Compilation never loads the modules or rescans PCI.
