# wechat-export-send

在 Linux 服务器上用容器挂一个 Linux 版微信，把**你自己账号**本地的聊天记录只读解密、增量导出成开放的 JSONL 格式（`chat-export/v1`），并提供一个严格区分结果的命令 `wechat-send`，往会话里发文字和图片（`chat-send/v1`）。

从 [rmqg/wechat-digest](https://github.com/rmqg/wechat-digest) v1.2.4 拆分出来，全新历史。那边负责写总结，这边只负责「把消息拿出来」和「把消息发出去」。

## 它是什么

- **一个容器一个账号。** 想挂几个号就建几个实例，目录、端口、密钥、导出全部分开。
- **只读。** 客户端的数据库从不被修改：先复制数据库和 WAL，逐页校验 HMAC 再解密到私有目录，读完就删。
- **增量导出。** 消息、撤回、后来才下载到的图片都能跟上；格式见 [docs/CONTRACT.md](docs/CONTRACT.md)。
- **发送结果说实话。** `sent` 是在数据库里确认到了新消息；`failed` 是确定没发出去；**只要可能已经点过发送，就是 `unknown`，绝不报 `failed`**，也不会自动重发。
- **导出和发送解耦。** 只用其中一个也可以，两者只共用实例目录和密钥。
- **自带检查。** `demo` 不用账号、不联网跑出结果；`doctor` 检查依赖、权限、配置和连通性并给修法。

## 它不是什么

- 不是微信协议客户端，也不是机器人框架：它操作的是官方客户端的窗口，不模拟登录，不收发网络协议。
- 不是总结工具（那是 chat-digest），不接大模型，导出内容不会被发到任何地方。
- 不分发微信客户端。deb 由你自己从官方获取。
- 不支持别人的账号。密钥捕获要读客户端进程内存，只能对你自己的账号、在你自己的机器上做。
- 不保证永远能用。微信改版可能让捕获或界面坐标失效。使用自动化工具可能违反微信的服务条款，账号风险由你自己承担。
- Windows 和 macOS 版微信不在支持范围内。

## 支持矩阵摘要

完整版本和说明见 [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)。**请注意两栏的区别**：

| 项目 | 实测通过 | 未测试 / 不保证 |
|---|---|---|
| 这个仓库的离线 demo、doctor、单元测试和假微信端到端测试 | Debian 12、Ubuntu 24.04，arm64 和 amd64 容器（OrbStack 上，过程见 [docs/VALIDATION.md](docs/VALIDATION.md)） | 其他发行版 |
| 微信客户端 | **4.1.13.26**（x86_64）：导出在两个真实账号上核对过条数；`wechat-send` 往文件传输助手发过文字和图片并查库核对。拆分前的同类代码长期运行 | **经搜索选中会话后往真实群发送**（重构后没跑过，先 `--dry-run`）；真实的 `keys capture`；其他客户端版本 |
| 拆分前代码运行的宿主机 | iStoreOS 24.10.8，Linux 6.6，Docker 27.3.1，Compose 2.39.1 | 其他宿主机上的真实账号 |
| CPU | 真实客户端：x86_64 | arm64 上的真实客户端 |

## 快速开始

### 先试 demo：不用账号，不联网

```sh
tar -xzf wechat-export-send-1.0.0.tar.gz
cd wechat-export-send-1.0.0
./install.sh            # 装依赖；这一步要联网。跳过它 demo 也能跑，只是慢
./bin/wechat-export-send demo
./bin/wechat-export-send doctor
```

demo 把仓库带的虚构加密账号导出到 `demo-output/export/`，并检查引用、转发、图片、增量、错误密钥被拒绝等共 12 项。

### 接真实账号

```sh
./install.sh --images                                   # 构建桌面和工具镜像
./bin/wechat-export-send client install ~/WeChatLinux_x86_64.deb   # 你自己下载的官方 deb
./bin/wechat-export-send instance create main
./bin/wechat-export-send instance up main
# 在你的电脑上开隧道：ssh -L 17900:127.0.0.1:17900 服务器
# 浏览器打开 http://127.0.0.1:17900/vnc.html ，用手机扫码登录
./bin/wechat-export-send keys check --instance main
./bin/wechat-export-send keys capture --instance main --i-own-this-account
# 这时在 noVNC 里退出登录再重新登录一次（手机确认）
./bin/wechat-export-send export --instance main
./bin/wechat-export-send conversations --instance main
```

### 发一条消息

第一次往某个会话发之前，先校准它的标题模板，再用 `--dry-run` 验证：

```sh
./bin/wechat-send --instance main --to "项目群" --calibrate
./bin/wechat-send --instance main --to "项目群" --text "测试" --dry-run --json
./bin/wechat-send --instance main --to "项目群" --text "大家好" --request-id 20261108-1 --json
```

输出和退出码：

| status | 退出码 | 含义 | 你该怎么做 |
|---|---|---|---|
| `sent` | 0 | 已提交，并在数据库里确认到新消息 | 记为完成 |
| `failed` | 1 | 确定没发出去 | 可以重试 |
| `unknown` | 2 | 可能已经发出去了，没能确认 | **不要自动重试**，人工看一眼 |
| `invalid` | 3 | 参数错误 | 修配置 |
| `dry_run` | 0 | 只在 `--dry-run` 时出现：校验通过，没有提交 | |

## 文档

- [docs/INSTALL.md](docs/INSTALL.md)：一步一步安装，多账号，开机自启
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md)：每个参数
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)：按症状查
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：数据流和组件
- [docs/SECURITY.md](docs/SECURITY.md)：读什么、写什么、要什么权限、有什么风险
- [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)：版本支持
- [docs/INTEGRATION.md](docs/INTEGRATION.md)：下游怎么接：读导出、调发送，带 chat-digest 的端到端示例
- [docs/CONTRACT.md](docs/CONTRACT.md)：`chat-export/v1` 和 `chat-send/v1`
- [docs/DATABASE.md](docs/DATABASE.md)：数据库格式和解析规则
- [docs/VALIDATION.md](docs/VALIDATION.md)：干净环境实测记录

## 验证 release 包

```sh
sha256sum -c SHA256SUMS
gh attestation verify wechat-export-send-1.0.0.tar.gz --repo rmqg/wechat-export-send
```

## 许可证

MIT，见 [LICENSE](LICENSE)。借用的文件和外部依赖见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本项目与腾讯没有关联。
