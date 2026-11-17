<p align="center"><img src="https://raw.githubusercontent.com/CyberShadowX/CloudSaver-For-FNOS/v1.0.2/assets/official-logo.svg" width="96" height="96" alt="CloudSaver 官方图标"></p>

# CloudSaver For FNOS v1.0.2

2026-11-18 · 飞牛 fnOS 原生封装的名称与官方图标更新。

项目、飞牛应用中心、桌面入口和网页固定标题统一使用 **CloudSaver For FNOS**，GitHub 仓库迁至 [CyberShadowX/CloudSaver-For-FNOS](https://github.com/CyberShadowX/CloudSaver-For-FNOS)。上游仍为 CloudSaver `0.9.3`。

应用中心与桌面的 64/256 图标、本地素材、README 和发行说明采用上游官方蓝色 CS 图标，原始 SVG 固定到已核验的官方提交。图标来源及完整 MIT 许可随包保留，详见 [第三方声明](THIRD_PARTY_NOTICES.md)。

## 安装与升级

- 新包：`CloudSaver-For-FNOS-1.0.2-x86.fpk`，94,060 字节。
- SHA-256：`8de18a25212f3815f5992b52a3258cbbbe77efacbfbd6c85c489518e82137ea4`，也可核对同版 `SHA256SUMS.txt`。
- 平台：x86_64 / amd64；FPK 平台标识 `x86`，最低声明 fnOS `1.2.1010`。
- 应用标识、账户、端口和数据目录保持 `CloudSaver`；从原有 1.0.1 安装原位升级，保留现有频道、账号、插件、其他设置及运行模式。
- 升级停止服务并生成设备私有配置快照；上游程序版本与固定摘要不变。
- 历史 `v1.0.1` 标签、提交和六份附件保留原始内容、名称和 SHA-256。

首次安装从作者官方 Docker Hub 下载固定摘要程序，逐层校验，再由宿主低权限账户直接运行。FPK 不包含较新上游程序，首次安装需要联网；缺少 aiohttp 时安装器使用设备已配置的软件源。

## 配置与使用范围

个人频道、数据库、账号、Cookie、环境配置、日志及恢复备份只在安装设备管理，不加入源码、FPK 或 GitHub 附件。全新安装使用随机强 JWT 密钥及生产模式；已有部署保留原配置、密钥和运行模式。

本版继续使用原搜索方式；PanSou 仅为可选公开频道资料来源，不启动额外搜索服务。默认网页 HTTP `8008`，内部后端 `127.0.0.1:18009`。

本轮 Windows 131 项（126 通过、5 项 POSIX 跳过），NAS root 与普通用户各 131/131 通过。应用中心 1.0.1 → 1.0.2 原位升级、18 个已安装负载文件与官方图标、配置完整字节保留、重启及真实搜索均通过。实际网页标题和浏览器安装清单名称已核验，原 37 频道及 3 插件保留。

本次验证结果和历史迁移范围见 [验收记录](docs/VALIDATION.md)。未执行真实网盘写入、整机重启、生产回滚或其他架构测试。

[使用与迁移指南](docs/USER_GUIDE.md) · [CloudSaver For FNOS](https://github.com/CyberShadowX/CloudSaver-For-FNOS) · [CloudSaver 上游](https://github.com/jiangrui1994/CloudSaver)
