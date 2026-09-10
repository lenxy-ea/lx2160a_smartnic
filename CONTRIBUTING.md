# Contributing / 参与开发

Develop product changes in this repository. Keep source, configuration,
dependency pins, meaningful tests and product documentation together. Hardware
parameters must bind to the public board contract. Do not replace an unknown
X200 fact with a reference-board default.

在本仓库开发产品变更，将源码、配置、依赖版本、有效测试和产品文档一起维护。
硬件参数必须绑定公开板级契约，不能用参考板默认值填补未知 X200 事实。

## Review and validation / 审查与验证

`source-files.json` is the explicit source inventory. Add each reviewed new
file to it; do not generate a recursive inventory from an unreviewed workspace.
The index check reads actual staged blobs, so edits made after staging cannot
hide what will be committed.

`source-files.json` 是显式源码清单。逐个审核新文件后再加入，不要对未经审核的
工作区递归生成清单。暂存检查读取实际 Git 暂存对象，避免工作区与待提交内容
不一致。

```sh
make -C bsp/lx2160-x200 check test
git diff --cached --check
python3 tools/check_source.py --index
```

Before pushing, inspect the staged diff and commit messages and run:

推送前检查暂存差异、提交说明，并执行：

```sh
python3 tools/check_source.py --history
```

Automated checks reject excluded paths, non-source objects, archive references
and common credential forms. Review remains necessary for research narratives,
site-specific details and code ownership. Use synthetic test fixtures.

自动检查会拒绝禁止路径、非源码对象、归档引用和常见凭据格式。研究叙述、现场
专用信息和代码权属仍需人工审查。测试使用人工构造的夹具。

## Build contracts / 构建契约

- A producer records the actual artifact hashes and identities it creates.
  Consumers verify those bytes and reject mismatched kernels/modules.
- Source builds must work in a fresh checkout with public pinned dependencies.
  Keep generated files and operator configuration outside Git.
- Retain upstream copyright and license notices; consult `LICENSES.md` before
  choosing a license for new component modifications.
- Keep English and Chinese user guides synchronized. Describe actual validation
  scope; source tests do not establish target hardware qualification.

- 构建程序记录实际生成产物的哈希与身份；消费者验证这些字节，拒绝不匹配的
  内核或模块。
- 源码应能从干净检出和固定公开依赖完成构建；生成文件及现场配置不进入 Git。
- 保留上游版权与许可声明，修改组件时先查阅 `LICENSES.md`。
- 同步维护中英文指南，准确说明验证范围；源码测试不代表目标硬件验收。

## CI

Pushes and pull requests run source/history checks and source-only test groups.
The manually triggered full-build workflow also builds firmware, kernel,
runtime and a Debian SATA image using sample configuration. It does not access
hardware or upload build directories and binary artifacts.

推送和拉取请求运行源码／历史检查及离线测试。手动触发的完整构建流程还会用
示例配置构建固件、内核、运行服务与 Debian SATA 镜像，不访问硬件，也不上传
构建目录或二进制产物。
