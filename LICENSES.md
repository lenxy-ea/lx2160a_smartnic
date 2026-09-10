# Licensing / 许可说明

This is a multi-license repository. The root MIT license applies to independent
original tools and documentation without another applicable notice. It does not
replace the license of an upstream component or its modifications.

本仓库采用多许可证。根目录 MIT 许可证适用于没有其他适用声明的原创独立工具
与文档，不替代上游组件及其修改部分适用的许可证。

| Component / 组件 | License handling / 许可处理 |
|---|---|
| Independent Python/shell tooling, product documentation / 独立工具与产品文档 | MIT, unless a file says otherwise / 文件另有声明的除外 |
| TF-A board source / TF-A 板级源码 | Preserve BSD-3-Clause notices / 保留 BSD-3-Clause 声明 |
| U-Boot board source and modifications / U-Boot 板级源码与修改 | Preserve per-file GPL-2.0-or-later and other applicable notices / 保留各文件 GPL-2.0-or-later 等声明 |
| Device trees / 设备树 | Per-file terms, including GPL-2.0-or-later OR X11 where stated / 遵循文件声明，部分提供双许可证选择 |
| Linux patches and kernel modules / Linux 补丁与内核模块 | GPL-2.0-only or the existing per-file upstream terms / GPL-2.0-only 或上游现有逐文件条款 |
| Downloaded SDK sources and Debian packages / 下载的 SDK 源码与 Debian 软件包 | Their own upstream licenses / 各自上游许可证 |
| MC and DDR training firmware / MC 与 DDR 训练固件 | Separate NXP binary licenses; fetched externally, not included here / 独立 NXP 二进制许可，外部下载，不随本仓库提供 |

The original copyright and SPDX notices are retained. `GPL-2.0+` in an existing
header means `GPL-2.0-or-later`; it is not converted to MIT. License texts for
BSD-3-Clause, GPL version 2 and X11 are in `LICENSES/`.

保留原有版权及 SPDX 声明。现有头部的 `GPL-2.0+` 表示 `GPL-2.0-or-later`，
不会改为 MIT。BSD-3-Clause、GPL 第 2 版与 X11 条款全文位于 `LICENSES/`。

Pinned binary dependency terms / 固定二进制依赖条款：

- [DDR PHY binary agreement](https://github.com/nxp-qoriq/ddr-phy-binary/blob/1dd5f83191c1d0de34df1dca18b93b40fdbe18f7/NXP-Binary-EULA.txt)
- [MC binary agreement](https://github.com/nxp-qoriq/qoriq-mc-binary/blob/258dd3a59457163fc21f6055f7b893ca94ce642f/LICENSE)

The download lock records the exact source and firmware versions and checksums.
Review the corresponding dependency terms before use or binary redistribution.
This repository distributes source and recipes, not a binary release.

下载锁定文件记录准确的源码、固件版本及校验和。使用依赖或重新分发二进制前，
应查阅对应依赖条款。本仓库交付源码和构建配方，不交付二进制发行包。
