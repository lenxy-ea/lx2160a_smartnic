# LX2160A X200 SmartNIC

[中文](README.zh-CN.md)

Maintained X200 board support, a native TF-A/U-Boot boot chain, Linux and Debian
SATA image recipes, and host/card management tools. This repository contains
source and build recipes; MC and DDR training firmware are externally fetched,
checksum-pinned dependencies with separate licenses.

The current profile uses DDR3200 and SerDes2 protocol 5. Linux startup has been
validated with PCIe3 disconnected; actual PCIe3 Gen3 link operation and production
reliability are not qualified. See [limitations](docs/en/limitations.md).

## Start here

1. [Build firmware, kernel, tools and a Debian SATA image](docs/en/build.md)
2. [Configure and install host/card services](docs/en/install.md)
3. [Architecture and hardware contracts](docs/en/architecture.md)
4. [Contribute and validate source](CONTRIBUTING.md)
5. [Component licenses](LICENSES.md)

From a clean checkout, inspect source before downloading build dependencies:

```sh
make -C bsp/lx2160-x200 check test
make -C bsp/lx2160-x200 toolchain
make -C bsp/lx2160-x200 firmware JOBS=4
```

All build products are regular files under `build/`. Build commands do not
access target hardware. Operator-specific network configuration and SSH keys
are supplied separately and must not be committed.

The root MIT license covers independent original tools and documentation where
no other terms apply. TF-A, U-Boot, Linux and other upstream components retain
their own licenses and copyright notices.
