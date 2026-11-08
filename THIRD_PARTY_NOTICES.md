# 第三方声明

仓库根目录的 MIT 许可证只覆盖本项目的原创代码，不替代其他作者的版权和许可证。再分发下列文件时请保留对应声明。

## 收录的源码

| 路径 | 来源和固定版本 | 许可证 | 保留的声明 |
|---|---|---|---|
| `vendor/wcdb_key_tool/wcdb_key_tool.py` | [TANGandXUE/wcdb-key-tool](https://github.com/TANGandXUE/wcdb-key-tool)，commit `79f1b5b92e12c66aa281b4a60a3c478b5f547dfa` | MIT，Copyright (c) 2025 CloudDreamAI / TANGandXUE | `vendor/wcdb_key_tool/LICENSE` |
| `vendor/wxextract/keys.py`、`passphrase.py`、`__init__.py` | [boujuan/extract-wechat-messages-linux](https://github.com/boujuan/extract-wechat-messages-linux)，commit `74fa2ce1004411fae353ddd35c6426d60fab8a62` | MIT，Copyright (c) 2026 Juan Boullosa | `vendor/wxextract/LICENSE`、`NOTICE.md` |
| `wechat_export_send/media.py` 里 V1/V2 `.dat` 解码的部分 | 同一 commit 的 [`wxextract/images.py`](https://github.com/boujuan/extract-wechat-messages-linux/blob/74fa2ce1004411fae353ddd35c6426d60fab8a62/src/wxextract/images.py)，按其格式改写 | MIT，同上 | 模块头部说明和 `vendor/wxextract/LICENSE` |

`vendor/` 下的文件原样收录，没有改动（在 v1.2.4 时已与上述 commit 逐字节核对）。

## 参考过格式、没有收录代码的项目

| 项目 | 用在哪里 | 许可证 |
|---|---|---|
| [thisnick/agent-wechat](https://github.com/thisnick/agent-wechat)（`wechat-protocol/media.rs`、`large_cdn_config.rs`，核对许可证时的 commit `4bfc492675cb8228abe1cf45a71fb624f63d7bff`） | `wechat_export_send/originals.py` 的 CDN 协议格式；是独立实现 | MIT |

## 容器和依赖：不随本项目分发，各自保留各自的许可证

本项目只提供 Dockerfile 和 compose 文件，不发布镜像。你在本地构建或拉取镜像时，下面这些由你从各自的来源获取。

| 组件 | 用途 | 许可证 |
|---|---|---|
| 官方 Linux 版微信 | 由你自己下载 deb。腾讯的专有软件，受其服务条款约束；本项目不分发，也不隶属于腾讯 | 专有 |
| [nickrunning/wechat-selkies](https://github.com/nickrunning/wechat-selkies)（外部镜像，只在可选的 `compose.selkies.yaml` 里引用） | 备选桌面 | MIT；镜像里还有各组件自己的许可证和专有的微信客户端 |
| `debian:12-slim` 及 apt 包：Xvfb（MIT/X11）、fluxbox（MIT）、x11vnc（GPL-2.0-or-later）、websockify（LGPL-3.0）、noVNC（MPL-2.0 加各文件声明，见镜像内 `/usr/share/doc/novnc/copyright`）、supervisor（BSD 风格）、fonts-noto-cjk（OFL-1.1） | 桌面镜像 | 见左；构建时从 Debian 获取 |
| `python:3.12-slim-bookworm`、GDB（GPL-3.0-or-later）、binutils（GPL） | 工具镜像 | 见左；以独立进程调用，不和本项目代码链接 |
| `nginx:stable-alpine`（只在可选的 `compose.proxy.yaml` 里引用） | noVNC 反向代理 | BSD-2-Clause |
| [PyCryptodome](https://github.com/Legrandin/pycryptodome) | AES | BSD-2-Clause、公有领域部分 |
| [python-zstandard](https://github.com/indygreg/python-zstandard) | 解压 | BSD-3-Clause |
| [Pillow](https://github.com/python-pillow/Pillow) | 图片校验、标题比对 | MIT-CMU（HPND） |
| [pytest](https://github.com/pytest-dev/pytest)（只在开发时） | 测试 | MIT |

如果你把构建好的镜像再分发给别人，要自己履行里面 GPL、LGPL 等组件的源码义务，并遵守微信客户端的条款。

## 明确没有收录的东西

[xiaoguiwucan/linux-wechat-agent](https://github.com/xiaoguiwucan/linux-wechat-agent) 以及它带的 `tools/wechat-decrypt` 没有可确认的再分发许可，公开的 fork 或 Docker 镜像不等于授权。本项目没有收录、也没有参考它们的代码、文档或截图。数据库解密、消息解析和发送都是按公开的 SQLCipher 格式和上表列出的有许可证的实现独立写的。

## 合成样本

`examples/synthetic/` 和生成它的 `wechat_export_send/synthetic.py` 是原创的虚构数据。里面的密钥、盐都是公开的测试向量，不是真实凭据；不要把真实导出放进这个目录。

微信、WeChat、WCDB 是腾讯的商标或项目。本项目与腾讯没有关联。
