# 配置、安装与升级

先按[构建指南](build.md)生成工具包，板卡打包与主机安装使用同一份现场配置。
必须将 `config.example.json` 中的地址和 PCI 位置替换为实际值，不复制其他
系统的密钥或设备身份。板卡管理命令须以 root 或通过 sudo 执行。

## 主机

当前主机服务需要 systemd、NetworkManager、firewalld、nftables、Python 3、
kmod，以及匹配的内核头文件和原生 NTB 模块。提供的早期加载配置使用 dracut。
不具备这些组件的主机，需要单独完成服务集成。

以下命令将经过校验的主机包部署到**离线主机根目录**：

```sh
python3 bsp/lx2160-x200/pcie-management/persistent/host_install.py \
  --bundle "$PWD/build/host-vntb/bundle" \
  --manifest-sha256 VERIFIED_HOST_MANIFEST_SHA256 \
  --config "$PWD/config.local.json" --root /mnt/host-root
```

使用主机构建程序输出的 `manifest_sha256`。安装程序验证工具包，部署模块和
配置，拒绝覆盖冲突文件，不启动服务或加载驱动。审核文件后，按主机维护流程
重建 initramfs，并显式启用服务。对端仍持有活动映射时不要卸载驱动；计划停止
时使用协调静默工具。

## 板卡与 SATA 镜像

rootfs 构建集成配套板卡服务及模块，并可配置操作员 SSH 公钥。root 登录和
SSH 密码认证保持禁用。板卡首次启动时生成缺失的 SSH 主机密钥，镜像不分发
这些密钥。

NetworkManager 通过预装的 `x200-l2-eth0` 至 `x200-l2-eth3` 配置管理四个物理
数据端口。配置禁用 IPv4 和 IPv6，保留 SoC 派生 MAC，并提供手动速率保护检查
要求的配置身份；这些端口配置不提供管理登录地址。

SATA 输出是完整的普通 GPT 磁盘镜像文件。在单独进行物理安装前，确认目标
SATA 设备，并核对容量与镜像清单，再使用适当的镜像写入流程。镜像构建独立于
这项会覆盖磁盘的安装操作。

启动文件、内核和板卡服务必须配套。NOR 配置身份须与 SATA 启动脚本一致，
身份不符时脚本停止启动。不要用其他构建的独立内核覆盖已打包模块和运行身份。

## 内核与板卡服务升级

从构建清单和配套板卡包生成升级包：

```sh
python3 bsp/lx2160-x200/pcie-management/kernel-upgrade/package.py \
  --kernel-manifest "$PWD/build/kernel/manifest.json" --artifact-root "$PWD" \
  --card-bundle "$PWD/build/runtime/card" --card-sha256 VERIFIED_CARD_BUNDLE_SHA256 \
  --out "$PWD/build/kernel-upgrade"
```

升级包包含安装程序和带校验和的文件清单。仅在明确安排升级时，在板卡上执行：

```sh
python3 /path/to/kernel-upgrade/install.py stage \
  --root / --package /path/to/kernel-upgrade \
  --manifest-sha256 VERIFIED_UPGRADE_MANIFEST_SHA256 \
  --current-image-sha256 VERIFIED_CURRENT_IMAGE_SHA256 \
  --expected-boot-id CURRENT_CARD_BOOT_ID \
  --journal /var/lib/x200/updates/UNIQUE_UPDATE_ID
```

安装程序检查包身份、当前 Image 字节及启动身份，然后记录并更新 SATA 文件。
它不重启，也不写 NOR。保留事务日志，直到下次启动验收通过。`status` 查看状态；
`rollback` 检查是否有外部修改后，仅恢复本次事务。该事务不保证断电时原子恢复。

## 启动固件升级

`bringup/package_boot_update.py` 将本次生成的 NOR 镜像及其打包回执，与实际当前
NOR 快照和 SATA 启动脚本组合：

```sh
python3 bsp/lx2160-x200/bringup/package_boot_update.py \
  --current-snapshot /path/to/current-nor.bin \
  --candidate-image /path/to/new-bank-image.bin \
  --current-boot /path/to/current-boot.scr \
  --candidate-boot "$PWD/build/firmware/boot.scr" \
  --producer-manifest /path/to/new-bank-pack-receipt.json \
  --config "$PWD/config.local.json" --out "$PWD/build/boot-update"
```

打包程序逐字节保留两个环境区，拒绝其他保护区变化。在 `boot_update` 中显式
配置设备、NOR 标识、sysfs 和文件系统身份，并审核变化扇区及工具包摘要。

在板卡上，`boot_update.py prepare --bundle PATH --manifest-sha256 DIGEST`
检查设备并保存事务快照。使用相同参数执行 `boot_update.py apply`，会在同次
启动及当前字节检查后执行实际写入。每个扇区均读回校验，清单扇区最后写入，
随后校验完整镜像并部署配套 SATA `boot.scr`。程序不重启、不切换 NOR。事务
文件保存在本地；该流程不保证断电原子性，尽力恢复仅限同一事务。

## 日常观察

rootfs 包含 `x200-network-diagnostics`。也可以使用源码采集工具显式选择网卡，
读取标准 Linux 接口：

```sh
python3 bsp/lx2160-x200/network-probe/collect.py \
  --interface eth0 --output /tmp/x200-network.json
```

采集结果可能包含现场网络和设备身份，应留在本地；如需提交支持请求，先审查
拟提交的内容。端口速率切换等运行时变更须显式执行，与只读采集分开。

运行覆盖层还安装配套手动速率模块和 `x200-port-rate` 命令。`x200-port-rate status`
查看端口；`x200-port-rate set eth0 10000` 为该数据端口请求运行时 10G。支持
10000 和 25000 Mbit/s，具体切换与线缆组合仍需验证。此操作不改变初始启动配置。
