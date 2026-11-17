<p align="center"><img src="https://raw.githubusercontent.com/CyberShadowX/CloudSaver-For-FNOS/v1.0.3/assets/official-logo.svg" width="96" height="96" alt="CloudSaver 官方图标"></p>

# CloudSaver For FNOS v1.0.3

2026-11-18 · 恢复应用名称。

**只有 GitHub 项目叫 CloudSaver For FNOS，安装到 NAS 的应用始终叫 CloudSaver。**

修复 1.0.2 将仓库名称误用到应用界面的问题：应用中心、桌面图标、网页左上角、浏览器标题、浏览器安装名称均恢复为 **CloudSaver**。FPK 文件名为 `CloudSaver-1.0.3-x86.fpk`，官方蓝色 CS 图标保留。

GitHub 仓库继续使用 [CyberShadowX/CloudSaver-For-FNOS](https://github.com/CyberShadowX/CloudSaver-For-FNOS)，README 项目标题和项目链接保持该名称。应用 ID、账户、UID/GID、目录、端口、上游 CloudSaver 0.9.3 和固定来源保持。

## 安装与配置

通过应用中心从 1.0.2 原位升级，升级前停止服务并自动保存私有配置快照。原频道、账号、插件、其他设置、密钥和运行模式保留。个人配置不作为源码或安装包输入，不加入 GitHub。

旧版本标签和附件保持原始内容。新包与对应源码、构建清单、来源、中文说明及 SHA256SUMS.txt 一起发布；安装前核对同版摘要。目标为 fnOS 1.2.1010+、x86_64，日常运行无需 Docker；首次安装需要联网获取固定摘要官方程序。

## 验证范围

主会话 Windows 131 项（126 通过、5 POSIX 跳过），NAS root 与普通用户独立目录各 131/131。实际 1.0.2 → 1.0.3 原位升级及重启后，数据、配置与运行模式字节相同；37 频道、3 插件保留，真实搜索 31 组/35 资源。网页左上角、标题与安装清单名称已实测恢复 CloudSaver，18 个负载和官方图标匹配本次 FPK。

详细结果见[验收记录](docs/VALIDATION.md)。未执行真实网盘写入、整机重启或生产回滚。

[使用指南](docs/USER_GUIDE.md) · [第三方图标许可](THIRD_PARTY_NOTICES.md) · [CloudSaver For FNOS](https://github.com/CyberShadowX/CloudSaver-For-FNOS)
