<p align="center"><img src="assets/official-logo.svg" width="128" height="128" alt="CloudSaver 官方图标"></p>

# CloudSaver For FNOS

[CloudSaver For FNOS](https://github.com/CyberShadowX/CloudSaver-For-FNOS) 是本 GitHub 项目的名称。安装到 NAS 的应用始终名为 **CloudSaver**，包括应用中心、桌面图标、网页左上角、浏览器标题和安装名称；使用官方蓝色 CS 图标。个人频道与其他设置保存在设备自己的数据目录。

已在 fnOS `1.2.1011` / Debian 12 / x86_64 完成原生安装、已有配置迁移、真实搜索、启停及应用中心原位升级验收。[验收范围与限制](docs/VALIDATION.md) · [v1.0.3 发行说明](RELEASE_NOTES.md)

| 项目 | 当前版本 |
| --- | --- |
| 封装版本 | `1.0.3` |
| 固定上游版本 | CloudSaver `0.9.3` |
| 平台 | fnOS `1.2.1010` 或以上，x86_64 / amd64（FPK 标识为 `x86`） |
| 网页入口 | HTTP `8008`，从飞牛桌面图标打开 |
| 内部后端 | `127.0.0.1:18009` |
| 应用标识 | `CloudSaver` |

应用显示名称、标识、系统账户和数据目录均为 `CloudSaver`；GitHub 项目名与仓库地址使用 `CloudSaver For FNOS` / `CloudSaver-For-FNOS`。1.0.3 修正了 1.0.2 将仓库名误用到应用界面的问题。已有安装通过原位升级保留配置，历史标签及附件保留原内容和摘要。

## 原生运行方式

首次安装时，安装器直接访问官方 Docker Hub，按 [upstream.json](upstream.json) 的固定摘要获取程序，核对镜像配置及每一层内容，再提取为应用私有的 Node、musl 依赖、CloudSaver 程序和静态网页。下载使用的匿名访问令牌仅保留在内存中。

运行时由 NAS 宿主直接启动低权限服务，不需要 Docker daemon、chroot 或容器命名空间。应用中心只在安装、目录初始化和生命周期管理时使用必要的管理员权限。

上游目前公开的源码停在 `0.2.5`，与较新官方镜像的实现存在差异。本仓库和 FPK **不再分发较新版本的上游程序**，安装设备从作者官方来源获取。这里的 [MIT 许可](LICENSE) 仅覆盖本仓库维护的封装代码，不扩展上游程序或其他项目的授权范围。[上游版本说明](https://github.com/jiangrui1994/CloudSaver#readme)

## 安装与使用

1. 从本项目的对应版本发行页面取得 FPK 与 `SHA256SUMS.txt`，核对安装包摘要。
2. 确认设备可以访问官方 Docker Hub，且 `8008`、`18009` 端口空闲。在飞牛应用中心手动上传 `CloudSaver-1.0.3-x86.fpk`。
3. 等待官方程序下载、校验和初始化结束，再从飞牛桌面打开 CloudSaver。
4. 新部署按上游页面完成注册、网络和网盘设置；已有 Docker 部署按下节迁移后再验收。

首次安装需要联网，不是完整离线安装包；设备缺少 `python3-aiohttp` 时，安装钩子会从设备已配置的软件源安装该依赖。默认入口使用 HTTP，适合受控的家庭网络或已有的安全远程入口；需要 HTTPS 时，可使用自己的受控反向代理。

日常使用、存储位置、备份与恢复详见 [使用与迁移指南](docs/USER_GUIDE.md)。

## 从 Docker 迁移

迁移按 **核验原版本和真实存储 → 一致性备份 → 通用空配置安装 → 只在安装设备导入 → 配置与功能验收 → 清理旧部署** 的顺序执行。

- 保留完整 `data`、`config` 和盘点发现的其他持久化内容，核对频道内容、顺序、开关，以及账号、插件和系统设置。
- 验收使用独立副本；原生版与旧容器不能同时写入同一数据库。
- 旧容器占用 `8008` 时，先完成备份和独立测试，再在切换窗口停止旧服务、释放端口。停止服务与删除旧部署分开安排。
- 原生版通过实际搜索、启停、升级与配置保留验收后，才精确清理 CloudSaver 专属 Docker 对象，并留下受限恢复备份。

具体步骤与核验工具边界见 [迁移流程](docs/USER_GUIDE.md#从-docker-迁移)。

## 搜索频道与 PanSou

首版保留 CloudSaver 现有搜索方式。PanSou 仅作为可选的公开频道资料来源，按需在频道管理中增量导入、去重，并保留已有频道的排序和启用状态。

本期不接入 PanSou 搜索引擎、API 或插件服务。频道导入方法参考 [CloudSaver 作者使用文档](https://www.yuque.com/xiaoruihenbangde/ggogn3/ga6gaaiy5fsyw62l) 和 [PanSou 官方项目](https://github.com/fish2018/pansou)。

## 配置与公开发布

FPK 从明确的程序文件白名单构建，个人配置在安装设备上另行迁入。安装包和公开仓库不携带个人频道、账号、网盘 Cookie、数据库、日志或私密备份；新设备的 JWT 密钥在本机随机生成。

全新安装使用生产模式及随机强密钥。迁移时保留原部署的配置、密钥和运行模式；模式记录由设备管理员单独保存，随配置检查点一起备份。

FPK 内层文件、源码附件和公开 Git 内容分别核对，源码与已安装 FPK 的程序文件一致。已迁移的生产目录不作为重新打包输入。

## 本地测试与构建

使用 Python `3.11+` 和已安装 `aiohttp` 的隔离开发环境，在仓库目录执行：

```powershell
python -m unittest discover -s tests -q
python tools/build_package.py --fnpack 'F:\path\to\verified\fnpack.exe' --output dist
```

将示例路径替换为**经过核验的官方 fnpack `1.2.3` Windows 可执行文件绝对路径**。构建器会再次检查其 SHA-256，固定值保存在 [build_package.py](tools/build_package.py) 中；不匹配时拒绝执行。

构建输出为 `dist/CloudSaver-1.0.3-x86.fpk`、`dist/build-inventory.json` 和 `dist/SHA256SUMS.txt`。构建不会把设备上的上游运行目录或个人数据装入 FPK。Windows 上的 Linux 专属测试会跳过，源码测试通过不能替代目标 NAS 实测。

## 更新与恢复

封装通过新版 FPK 升级；上游程序固定到已声明的版本和摘要，不跟随 `latest` 自动变化。未来上游版本需要单独做兼容性、配置迁移和回退验证。

升级前停止服务并保存配置快照。异常收尾尚未确认时，先检查并恢复现场，再继续重启或升级；恢复时使用配套的旧程序和配置备份。详见 [更新与恢复](docs/USER_GUIDE.md#更新与恢复)。

## 致谢

- [CloudSaver 上游](https://github.com/jiangrui1994/CloudSaver)：应用、使用文档和官方蓝色 CS 图标。
- [PanSou](https://github.com/fish2018/pansou)：可选公开频道资料来源。

本项目为非官方 fnOS 封装。官方图标的固定来源、原始摘要和 MIT 许可见 [第三方声明](THIRD_PARTY_NOTICES.md)。
