# FingerBrowser 自产 Chromium 内核

本仓库提供 Chromium 源码固定、原生补丁、Windows 编译、运行包打包与自动预发行流程。

## 自动发布

`build-kernel` 在 `main` 完成后，`auto-package-completed-build` 读取该次运行的状态产物：

- `built` 且 `browserCompiled=true`：自动触发 `package-candidate`，校验完整构建来源并提取运行文件。
- `checkpoint-ready`：只保存编译进度，不打包、不发布。
- 构建失败、来源不匹配或校验失败：不发布。

运行包打包及诊断完成后，工作流自动选择下一个未占用的 `Chromium版本-fb.N-pre.N` 标签，发布 GitHub Pre-release。当前所有自动发布均为实验候选，不代表完整浏览器验收或发行审查通过。

Release 包含：

- `browser-kernel-windows-x64.zip`：实际运行包，包含 DLL、locales、许可证和构建来源。
- `kernel.json`：管理器使用的版本、下载包大小、SHA-256、可执行文件摘要和源码提交。
- `package-report.json`：文件清单与打包证据。
- `SHA256SUMS.txt`：上述三个文件的校验值。

完整构建 checkpoint 不是供用户安装的内核包。当前 148 候选的运行 ZIP 约 413 MB，完整 checkpoint 约 9.38 GB。

## 版本与重试

同一 Chromium 版本已有 `-fb.1-pre.1` 时，下次自动发布使用 `-fb.1-pre.2`。若最高的 `fb.N` 已存在稳定标签，则从 `fb.(N+1)-pre.1` 开始。切换到新的 Chromium 基础版本时，使用该基础版本自己的候选序列。不会覆盖已有 Release 资产。

发布阶段失败时，无需重新编译：可手动运行 `auto-package-completed-build`，填入已完整编译成功的 `build-kernel` run ID。它会重新核验来源后继续打包发布。

也可手动运行 `package-candidate` 并勾选 `auto_publish`；已有经过核对的打包产物则可使用 `publish-candidate`，指定精确产物 ID、SHA-256 与候选版本号。

## 软件安装

支持此协议的 FingerBrowser 管理器中，进入“内核与设置 → 内核版本目录 → 刷新自产版本”。安装后可选择默认内核，或在环境编辑页固定版本。新旧内核独立安装，安装不会自动改变默认版本。

升级前关闭环境并完整备份应用数据目录。配置备份不包含浏览器 Cookie、保存密码和网页存储。当前实验候选仍需补验真实显卡 WebGL、强密码生成菜单及真实用户旧环境完整迁移。
