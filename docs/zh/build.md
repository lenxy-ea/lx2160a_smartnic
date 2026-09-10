# 构建

使用 Linux x86-64 主机，安装 Git、GNU Make、Python 3（推荐 3.12）、运行源码
测试所需的 C 编译器、Podman 和 `kmod` 工具（`modinfo`、`modprobe`）。建议预留
至少 80 GiB 空间，用于源码、内核模块、rootfs 和 32,017,047,552 字节的稀疏
SATA 镜像。16 GiB 内存主机先使用 4 个编译任务，不要仅按 CPU 数量设置并发。

共享容器配方按摘要固定公开 Debian 基础镜像，并指定工具包版本，包含交叉
编译器、设备树工具、镜像工具和 ARM 用户态模拟器。源码及固件下载会验证固定
哈希。获取依赖需要网络；固件和内核编译容器不使用网络。

## 源码与工具链

以下命令在仓库根目录执行。构建固件前先提交有意修改的源码，生成清单会记录
源码提交身份。

```sh
make -C bsp/lx2160-x200 check test
make -C bsp/lx2160-x200 toolchain
make -C bsp/lx2160-x200 firmware JOBS=4
make -C bsp/lx2160-x200 kernel JOBS=4
```

固件输出位于 `build/firmware/`，包括分别打包的 D11、D12 镜像及输入／输出
清单。内核构建生成 `build/kernel/manifest.json`、Image、vmlinux、配套模块和
ABI 检查结果。即使 release 字符串相同，也不要混用其他构建的模块。

输出目录必须是新目录。再次构建时设置新的 `FIRMWARE_OUT` 或 `KERNEL_OUT`；
后续打包通过 `KERNEL_MANIFEST` 选择对应内核。下载缓存可以保留，使用时仍会
校验固定哈希。

## 运行服务与 Debian

将 `bsp/lx2160-x200/pcie-management/config.example.json` 复制到仓库根目录的
`config.local.json`，按实际主机和板卡修改。示例中的地址用于文档示意，PCI BDF
也仅是示例，不代表已检测出的硬件配置。为操作员账号提供 SSH 公钥；源码不嵌入
密码或私钥。

```sh
make -C bsp/lx2160-x200 manual-rate runtime CONFIG="$PWD/config.local.json"
make -C bsp/lx2160-x200 bootstrap
make -C bsp/lx2160-x200 rootfs AUTHORIZED_KEY="$HOME/.ssh/id_ed25519.pub" OPERATOR=operator
make -C bsp/lx2160-x200 sata
```

bootstrap 下载并配置 Debian 软件包。rootfs 步骤组合经过校验的用户空间、当前
内核模块及运行服务覆盖层。SATA 步骤消费经过校验的 rootfs 归档和内核，生成
带有 1 GiB 启动分区及根分区的 GPT 镜像，不需要 loop 设备、物理磁盘或目标连接。

默认几何为 62,533,296 个 512 字节扇区。产物包括
`build/os/x200-debian13-rootfs.tar.zst`、`build/os/x200-debian13-sata.img.zst`、
软件包版本和构建清单。不提供公钥时不会创建可登录的操作员账号，root 保持锁定；
这样的镜像可用于构建验证，实际使用前须离线配置账号。

构建会记录 Debian 软件包版本，不承诺完整 rootfs 字节一致。默认使用实际构建
时间；复现含时间信息的启动／内核产物时，需要一致的 `SOURCE_DATE_EPOCH`、
源码、依赖及工具链版本。

## 主机驱动包

在预期使用的 x86-64 Linux 主机上构建，先安装与当前内核精确匹配的开发头文件
和原生 NTB 模块。构建程序下载固定版本的公开驱动源码，并检查替代 transport
与主机符号 CRC、原生 netdev 模块的匹配关系：

```sh
python3 bsp/lx2160-x200/pcie-management/build_vntb_host.py \
  --kernel-headers "/lib/modules/$(uname -r)/build" \
  --native-modules "/lib/modules/$(uname -r)/kernel" \
  --out "$PWD/build/host-vntb"
```

工具包及清单位于 `build/host-vntb/bundle/`。主机 ABI 支持取决于提供的头文件和
原生模块；更换内核需要配套重建。编译不会加载模块或重新扫描 PCI。
