# 实测记录

记录 v1.0.0 实际跑过什么、怎么跑的、结果是什么。**没有写在这里的就是没测过。** 版本支持的结论见 [COMPATIBILITY.md](COMPATIBILITY.md)。

## 环境

- 宿主：macOS（arm64）上的 OrbStack，Docker Engine 29.4.0，内核 `7.0.14-orbstack`。amd64 容器经 Rosetta 转译，不是原生 x86_64。
- 被测对象：用 `scripts/build-release.sh` 构建的 release 包。第一到四节在第一次打 tag 时跑了四个组合；之后为真实账号修过代码，见第六节的复核。

## 一、干净容器矩阵

每个组合都从官方基础镜像的全新容器开始，只装文档写的包。

| 镜像 | 架构 | 系统 | Python | 校验 | demo（联网安装后） | demo（无网络、未装依赖） | demo（无网络、已装依赖） | doctor | 测试 |
|---|---|---|---|---|---|---|---|---|---|
| `debian:12` | arm64 | Debian 12 | 3.11.2 | OK | 12/12 | 通过 | 12/12 | 退出码 0 | 133 passed |
| `debian:12` | amd64 | Debian 12 | 3.11.2 | OK | 12/12 | 通过 | 12/12 | 退出码 0 | 133 passed |
| `ubuntu:24.04` | arm64 | Ubuntu 24.04.5 | 3.12.3 | OK | 12/12 | 通过 | 12/12 | 退出码 0 | 133 passed |
| `ubuntu:24.04` | amd64 | Ubuntu 24.04.5 | 3.12.3 | OK | 12/12 | 通过 | 12/12 | 退出码 0 | 133 passed |

命令：

```sh
apt-get update && apt-get install -y python3 python3-venv
sha256sum -c SHA256SUMS
tar -xzf wechat-export-send-1.0.0.tar.gz && cd wechat-export-send-1.0.0
./install.sh
./bin/wechat-export-send demo
./bin/wechat-export-send doctor
```

然后把容器提交成镜像，用 `docker run --network none` 确认离线：在全新解压、没装依赖的目录跑 demo，在已安装的目录跑 `demo --json`（`fast_aes: true`，12 项）。

测试：

```sh
apt-get install -y python3-tk xvfb fluxbox libx11-6 libxtst6 libsqlite3-0
.venv/bin/pip install "pytest>=8.3,<10"
FAKE_PYTHON=/usr/bin/python3 .venv/bin/python -m pytest -q
```

这 133 项里包括：

- 12 项假微信端到端（`tests/test_send_x11.py`）：真 Xvfb、真 fluxbox、假微信窗口、合成加密库。发送并确认、同一个 request-id 不重发、dry-run 不点发送、点了但确认不到报 `unknown` 且之后不重发、已有草稿报 `failed` 且不动草稿、点错会话在点发送之前报 `failed`、未校准报 `failed`、窗口漂移恢复后发送、图片发送、空白标题不存模板、`--calibrate --current`、已打开时不再搜索。
- 3 项真 X11 剪贴板传输（`tests/test_clipboard_incr.py`）：2 MB 单次写入、约 17 MB 和 40 MB 走 INCR，字节完全一致。
- 其余是解密（含 WAL、错误密钥、篡改）、解析、媒体、增量导出（含撤回、等服务器 ID、待取图片、残缺行修复）、发送状态语义、看门狗、取原图协议、媒体服务、CLI 和 doctor。

另外在 2080 这台机器（Ubuntu 22.04，Python 3.10.12）上跑了不含 X11 的部分，全部通过。

测试过程中有两次 `test_image_sent` 在 Mac 负载较高时失败，当时没有留下具体原因；之后放宽了测试里的等待时间，并让矩阵打印完整的失败原因，再跑没有复现。怀疑是超时，没有证实。

## 二、默认的 docker 发送路径（假微信）

假微信放在名为 `wes-main-desktop` 的容器里（Xvfb + fluxbox + Tk），`wechat-send` 跑在另一个 Ubuntu 24.04 容器里，经 Docker socket 启动真正的 worker 容器（工具镜像，只读根文件系统，无 capability，`--network container:wes-main-desktop`）。

| 命令 | 结果 | 退出码 |
|---|---|---|
| `wechat-send --to <群> --calibrate` | `calibrated` | 0 |
| `wechat-send --to <群> --text ... --dry-run` | `dry_run` | 0 |
| `wechat-send --to <群> --text ... --request-id d1` | `sent`，带 `server_id` 回执 | 0 |
| 同一个 request-id 再来一次 | `sent`，「没有重发」 | 0 |
| 工具镜像不存在 | `failed`，`worker_not_started` | 1 |

假微信的日志显示发送按钮只被点了 1 次。看门狗探针同样经这条路连上 X，报 `online`。证据截图权限 600。

## 三、真实客户端（只到登录窗口，没有登录）

用官方 deb（4.1.13.26，SHA-256 与 `config/versions.json` 一致）和 release 包，在 Ubuntu 24.04 容器里以 uid 1000 照 [INSTALL.md](INSTALL.md) 走：

```sh
./install.sh
./bin/wechat-export-send demo
./install.sh --no-deps --images
./bin/wechat-export-send client install WeChatLinux_x86_64.deb   # tested: true
./bin/wechat-export-send instance create main
./bin/wechat-export-send instance up main
./bin/wechat-export-send doctor --instance main                  # 0 项失败，3 项警告
./bin/wechat-export-send watchdog --instance main                # offline
./bin/wechat-send --instance main --to 不存在 --text x --json     # failed，退出码 1
./bin/wechat-export-send instance down main
```

doctor 的 3 项警告都符合预期：架构是 aarch64；还没有账号目录；登录状态 `offline`。

在桌面容器里确认（amd64 镜像，Rosetta 转译，Debian 12.15）：

- `ldd /opt/wechat/wechat` 没有缺库
- 容器以 uid 1000、只读根文件系统、无 capability 运行
- 窗口 `Weixin`，292×396，和校准文件里的登录窗口一致
- `curl http://127.0.0.1:17900/vnc.html` 返回 200，页面是 noVNC；端口只绑 127.0.0.1
- 组件版本：Xvfb 21.1.7，fluxbox 1.3.5，x11vnc 0.9.16，websockify 0.10.0，noVNC 1.3.0，supervisor 4.2.5

`keys check`（放了一个占位的账号目录让它通过前置检查）：

```json
{"capture_point_located": true, "debugger_attached": false, "binary_matches": true}
```

这一轮实测当场发现并修好的问题：

- 桌面镜像用 `LANG=zh_CN` 时窗口标题变成「微信」，看门狗和发送器认不出。改回 `C.UTF-8`，和拆分前一致。
- Debian 12 的 Python 3.11.2 的 `tarfile` 没有 `filter` 参数，`client install` 会崩溃。改成自己检查路径和链接。

## 四、隐私扫描

| 检查 | 结果 |
|---|---|
| `scripts/audit-release.py` | 102 个文件，0 处问题 |
| gitleaks（`zricethezav/gitleaks`，扫 git 历史） | 没有发现泄漏；`examples/synthetic/` 的公开测试向量已在 `.gitleaks.toml` 里说明 |
| 发布前的私有词表 grep | 命中两处都不是真实数据：检查脚本自己的规则，`vendor/wcdb_key_tool` 的一句注释（`wxid_xxx`，第三方文件原样收录） |

## 五、真实账号（第二账号，已登录）

宿主：iStoreOS 24.10.8，Linux 6.6，Docker 27.3.1。用 release 包在宿主上安装，工具镜像本地构建。下面只记计数，真实数据没有离开这台机器。

```sh
./install.sh --no-deps --tools
./bin/wechat-export-send instance attach verify2 --container <已有容器> --docker-host unix:///<另一个 dockerd> \
  --network host --display :100 --gui <容器的 HOME> --keys <all_keys.json> --runner container
./bin/wechat-export-send export --instance verify2           # --network none，--memory 1g
./bin/wechat-export-send verify-export --instance verify2
```

结果：`verify-export` 的 `ok: true`，数据库行数和导出条数完全一致，0 个不一致的会话，0 行坏数据，没有未知发送者，导出的 `ready` 媒体都在磁盘上。源目录只读挂载。

这一轮靠真实数据发现并修好（都有回归测试）：

| 现象 | 原因 | 修法 |
|---|---|---|
| 解密报第 170 页 HMAC 失败 | 客户端把库预分配到 30 MB，尾部是全零页 | 只放过尾部连续的零页 |
| 核对发现少了一百多条 | `local_id` 和时间顺序不一致，游标在扫描中前进 | 一轮内游标下限不变 |
| 图片全部对不上文件 | 文件名的 md5 在 `packed_info_data` 里，不是 XML 的 | 解析 protobuf 3.4 |
| 有发送者为 `unknown` 的消息 | 拍一拍没有真正的发送者 | 归为系统消息 |
| 校准存下空白模板 | 第一条搜索结果可能是网页搜索，没打开会话 | 拒绝空白模板 |
| 探针按 Esc 后主窗口消失 | 客户端把主窗口收起来 | 程序不按 Esc，文档写明唤醒方法 |

发送：这个账号的搜索不返回文件传输助手，先在 noVNC 里把它打开，然后往文件传输助手发送。发送前后用独立脚本直接读解密快照核对：

```sh
./bin/wechat-send --instance verify2 --to 文件传输助手 --calibrate --current --json   # calibrated；标题裁图人工确认
./bin/wechat-send ... --text ... --dry-run --json                                      # dry_run，数据库没有新行
./bin/wechat-send ... --text ... --request-id r1 --json                                # sent，退出码 0
./bin/wechat-send ... --text ... --request-id r1 --json                                # sent，「没有重发」，数据库没有新行
./bin/wechat-send ... --text 别的 --request-id r1 --json                               # invalid，退出码 3
./bin/wechat-send ... --image test.png --request-id r2 --json                          # sent，退出码 0
```

数据库里恰好多了两行：都是自己发的，服务器 ID 和回执一致，文字的 SHA-256 和要发的完全相等。

另一个账号（主账号）只导出、不发送：数据库行数和导出条数一致；和日报系统的来源快照比对同一个 24 小时窗口，快照的消息全部在导出里，时间全部一致，导出多出的只有快照本来就排除的自己发的消息；文字消息的差异全部只在空白（导出保留原样）。

## 六、没有测的

**这些都没有做，不要当成支持：**

- 真实的 `keys capture`；经搜索选中会话后往真实群发送。拆分前的代码在这些环节跑过，重构后的代码还没有
- 原生 x86_64 宿主上跑这个 release 包（amd64 只经过 Rosetta）
- `fetch-originals` 连真实 CDN（只用假的 HTTP 响应测过协议和校验）
- systemd unit 和 procd 脚本实际启用
- `compose.selkies.yaml`、`compose.proxy.yaml` 实际运行（CI 里检查 compose 语法）
- GitHub Actions 工作流实际运行（仓库还没有推送），`gh attestation verify` 同理
