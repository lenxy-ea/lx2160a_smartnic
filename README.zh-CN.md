# LX2160A X200 SmartNIC

[English](README.md)

本仓库维护 X200 板级支持、原生 TF-A/U-Boot 启动链、Linux 与 Debian SATA
镜像构建配方，以及主机和板卡管理工具。仓库交付源码和配方；MC 与 DDR 训练
固件通过外部固定版本下载、校验，适用独立许可证。

当前配置采用 DDR3200 和 SerDes2 protocol 5。已有 PCIe3 未连接时的 Linux
启动验证；实际 PCIe3 Gen3 链路与生产可靠性尚未完成验证，详见
[限制说明](docs/zh/limitations.md)。

## 从这里开始

1. [构建启动固件、内核、工具和 Debian SATA 镜像](docs/zh/build.md)
2. [配置和安装主机／板卡服务](docs/zh/install.md)
3. [架构与硬件契约](docs/zh/architecture.md)
4. [参与开发与源码验证](CONTRIBUTING.md)
5. [组件许可](LICENSES.md)

从干净检出开始，先检查源码，再下载构建依赖：

```sh
make -C bsp/lx2160-x200 check test
make -C bsp/lx2160-x200 toolchain
make -C bsp/lx2160-x200 firmware JOBS=4
```

所有构建产物都是 `build/` 下的普通文件，构建命令不访问目标硬件。网络配置
和 SSH 密钥由使用者单独提供，不应提交到仓库。

根目录 MIT 许可证适用于没有其他适用条款的原创独立工具与文档。TF-A、
U-Boot、Linux 等上游组件保留各自许可证与版权声明。

## 离线验证

首版源码交付通过了 92 项人工夹具／源码测试，以及两个 NOR 启动镜像、包含
1,205 个模块的 Linux、主机／板卡工具包、Debian rootfs 和完整 SATA 镜像的
本地构建。检查覆盖固件布局、保护区、硬件 fact 绑定、模块 ABI、产物完整性、
GPT 和文件系统。这些结果属于构建验证，不代表新增目标硬件验收。
