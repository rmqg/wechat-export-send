# 版本支持

「实测通过」只写真正测过的；其余都在「未测试 / 不保证」。这里分三层，因为它们测过的范围不一样：

- **A 层：这个仓库的 release 包**，在干净容器里按文档安装，跑离线 demo、doctor、全部测试（假微信端到端）。过程见 [VALIDATION.md](VALIDATION.md)。
- **B 层：真实客户端，不登录**。桌面镜像和真实的官方 deb 一起启动到登录窗口。
- **C 层：拆分前的代码**，同一套捕获、解密和 X11 发送逻辑在真实账号上长期运行。
- **D 层：这个 release 对接真实账号。** 用 `instance attach` 对接一个已登录的容器，导出后用 `verify-export` 核对，数据库行数和导出条数完全一致。

**`wechat-send` 还没有在真实账号上完成过一次发送。** 真实客户端上校准和窗口操作跑过，发现并修了几个问题。如果你是第一个用它发真消息的人，先用 `--dry-run`，并用文件传输助手或一个自己的小群试。

## 实测通过

| 项目 | 版本 | 层 | 说明 |
|---|---|---|---|
| 微信 Linux 客户端 | 4.1.13.26，x86_64，deb SHA-256 `a0db8caeee8d1e6cfe0aa5ce58710270f8d74a90ff4920f53a3fc22aa0956fe9`，可执行文件 SHA-256 `2749535f299ba67feb8ef80cab898bb0531fa8f376b16f8a72db706811c84132` | C | 捕获、解密、发送都跑过；两个账号各一个容器 |
| 宿主（真实账号） | iStoreOS 24.10.8（OpenWrt），Linux 6.6.144，x86_64，Docker 27.3.1，Compose 2.39.1，Python 3.11.14 | C | 只有拆分前的代码在这里跑过 |
| 拆分前的容器环境 | 基于 Debian 的 selenium 桌面基础镜像，Python 3.12、3.14 | C | 这个仓库已改用自己的 Debian 12 桌面镜像 |
| 导出、核对（真实账号） | 微信 4.1.13.26，iStoreOS 24.10.8，Docker 27.3.1，工具容器（无网络，内存上限 1 GB） | D | `instance attach`、`export --container`、`verify-export` |
| 桌面镜像 + 真实客户端 | Debian 12.15，Xvfb 21.1.7，fluxbox 1.3.5，x11vnc 0.9.16，noVNC 1.3.0；客户端 4.1.13.26 | B | 经 Rosetta 转译。启动到登录窗口、看门狗识别 `offline`、`keys check` 定位到捕获点。没有登录，没有实际捕获 |
| 干净环境 | Debian 12（Python 3.11.2）、Ubuntu 24.04.5（Python 3.12.3），arm64 和 amd64 容器 | A | 宿主是 macOS 上的 OrbStack，内核 7.0.14，amd64 经 Rosetta 转译 |
| Docker | Engine 29.4.0，Compose 2.40.3（Ubuntu 24.04 的 `docker-compose-v2`） | B | OrbStack 的引擎 |
| Python | 3.10.12（只跑不含 X11 的测试）、3.11.2、3.12.3 | A | |
| 内部依赖 | PyCryptodome 3.23.0，zstandard 0.25.0，Pillow 11.3.0 | A | `requirements.txt` 固定 |

## 未测试 / 不保证

| 项目 | 状态 |
|---|---|
| `wechat-send` 在真实账号上完成发送 | **未完成** |
| `keys capture`、`fetch-originals` 连真实账号 | **未测试**（拆分前的同类代码跑过） |
| 其他微信版本（4.1.13.26 之外） | 未测试。捕获点会按二进制重新计算，可能能用；界面坐标、表结构可能要改 |
| 微信 4.0 及更早 | 未测试 |
| arm64 上的真实客户端 | 未测试。`compose.yaml` 固定用 amd64 镜像，转译环境下调试器和硬件断点大概率不可用 |
| `compose.selkies.yaml`（外部镜像） | 未测试，只检查过 compose 语法。显示号和窗口布局不同，发送器不能直接用 |
| `compose.proxy.yaml`（nginx） | 只检查过 compose 语法 |
| systemd unit、procd 脚本 | 未在真实系统上启用过；procd 脚本由拆分前的同类脚本改写 |
| 原生 x86_64 宿主上的这个 release 包 | 未测试：amd64 只在 Rosetta 下跑过 |
| GitHub Actions 工作流、`gh attestation verify` | 未运行：仓库还没有推送 |
| 其他发行版、内核 | 未测试。Linux 发行版不重要的部分在容器里；要注意 `kernel.yama.ptrace_scope`、硬件断点、虚拟化环境 |
| Windows、macOS 版微信 | 不支持 |
| Python 3.13 及以上做宿主 | 未测试 |
| 语音、视频文件的导出 | 不支持，只导出消息记录 |
| Wayland、真实显示器 | 不支持，只支持容器里的 Xvfb |

## 升级客户端之前

Linux 版微信升级可能带来：捕获点变化、表结构变化、界面坐标变化。容器里的客户端文件只读，不会自动更新，`WECHAT_BINARY_SHA256` 也会挡住被换掉的文件。想升级时：

1. 建一个新实例，不要动正在用的。
2. `client install` 新 deb，`doctor`、`keys check`。
3. `export` 一次，看 `errors`。
4. 量一下界面坐标，`wechat-send --calibrate`，`--dry-run`。
5. 在自己的小群或文件传输助手试发。
6. 没问题再切换，并把结果提个 issue，这张表就能多一行。
