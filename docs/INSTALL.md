# 安装

本文按顺序走。每一步的命令都在干净的容器里照着跑过的部分，记录在 [VALIDATION.md](VALIDATION.md)；真实微信那几步需要你自己的账号，那里会写清楚没有实测的范围。

## 1. 准备

**只想跑 demo**：Python 3.10 或更新版本就够了。

```sh
# Debian 12、Ubuntu 24.04
apt-get update && apt-get install -y python3 python3-venv
```

**接真实账号**还需要：

- x86_64 的机器（目前只测过这个架构的微信客户端）
- Docker Engine 和 Compose 插件（`docker compose version` 能输出版本）
- 至少 2 GB 空闲内存给每个实例（默认上限 2 GB），几 GB 磁盘
- 一部能扫码的手机，和你自己的微信账号
- 官方 Linux 版微信的 deb，`WeChatLinux_x86_64.deb`，从微信官网下载。本项目不分发它。[实测过的版本](COMPATIBILITY.md)是 4.1.13.26；官网通常只提供最新版，别的版本要自己评估

## 2. 解压并验证

```sh
sha256sum -c SHA256SUMS
gh attestation verify wechat-export-send-1.0.0.tar.gz --repo rmqg/wechat-export-send   # 可选，需要 gh
tar -xzf wechat-export-send-1.0.0.tar.gz
cd wechat-export-send-1.0.0
```

## 3. 装依赖

```sh
./install.sh
```

第一次接触的话，建议改看 [QUICKSTART.md](QUICKSTART.md)。

它只碰当前目录：先检查环境（发行版、内核、CPU、Python 和 venv、Docker 和 Compose、内存、磁盘），缺什么就打印要运行的命令；再装依赖：优先建 `.venv`，没有 venv 模块时退回 `pip --target .deps`，都没有时给出提示。不改系统，不需要 root。

在终端里运行时，最后进**安装向导**（`./bin/wechat-export-send setup`，以后也可以单独运行）：问你只跑演示还是接真实账号；接真实账号就依次问实例名、只导出哪些会话、要不要现在构建镜像、deb 在哪、要不要启动桌面，替你执行第 5～7 步，最后自检并打印「下一步」。已经做完的步骤会跳过，可以反复运行。

| 选项 | 作用 |
|---|---|
| `--config FILE` | 用答案文件回答向导，不提问。格式见 [install.env.example](../install.env.example) |
| `--non-interactive` | 不提问，只装依赖（没有终端时也是这样） |
| `--no-deps`、`--images`、`--tools`、`--link DIR` | 只做对应的那件事，不进向导（和旧版一样） |

想把命令放进 PATH：

```sh
./install.sh --link /usr/local/bin
```

下面的命令都写成 `./bin/...`。

## 4. 离线 demo 和自检

```sh
./bin/wechat-export-send demo
./bin/wechat-export-send doctor
```

demo 结果在 `demo-output/export/`，全部是虚构数据。再跑一次要先删掉 `demo-output`，或者用 `--output` 换个目录。这时 doctor 对 Docker 和实例的警告是正常的。

## 5. 构建镜像

```sh
./install.sh --images
```

构建工具镜像 `wechat-export-send-tools:local` 和桌面镜像 `wechat-export-send-desktop:<版本>`，要下载 Debian 和 Python 的基础镜像和 apt 包，约几百 MB 到 1 GB（中文字体占得比较多）。

## 6. 安装微信客户端

```sh
./bin/wechat-export-send client install ~/WeChatLinux_x86_64.deb
```

它用 Python 解开 deb，**不执行 deb 里的任何脚本**，放到 `client/<版本>/`，打印版本和哈希。`tested: false` 表示不是实测过的版本，能不能用要自己验证。

## 7. 创建并启动实例

```sh
./bin/wechat-export-send instance create main
./bin/wechat-export-send instance up main
./bin/wechat-export-send instance status main
```

`create` 会打印 noVNC 端口（默认从 17900 开始，自动避开占用）。实例的客户端哈希会写进 `instance.env`，之后客户端文件被换掉时容器拒绝启动。

## 8. 登录

noVNC 只绑服务器的 127.0.0.1。在你自己的电脑上开隧道：

```sh
ssh -L 17900:127.0.0.1:17900 你的服务器
```

浏览器打开 `http://127.0.0.1:17900/vnc.html`，点连接，用手机扫码登录。等聊天列表出现，消息同步一会儿。

```sh
./bin/wechat-export-send doctor --instance main
```

## 9. 捕获数据库密钥

```sh
./bin/wechat-export-send keys check --instance main
./bin/wechat-export-send keys capture --instance main --i-own-this-account
```

`keys check` 只定位捕获点，不附加调试器。`keys capture` 打印「等待重新登录」后，到 noVNC 里退出登录、重新登录一次（手机确认），密钥就在登录这一刻被捕获。成功时只打印计数：

```json
{"verified_databases": 12, "database_files": 12, "debugger_detached": true, "message_bodies_read": false}
```

默认的 gdb 硬件断点方式不支持时（报 `Hardware capture point was not supported`，有些虚拟化环境会这样），改用 native 方式：它会停掉客户端再由钩子重新启动，同样在 noVNC 里登录：

```sh
./bin/wechat-export-send keys capture --instance main --method native --i-own-this-account
```

## 10. 导出

```sh
./bin/wechat-export-send export --instance main
./bin/wechat-export-send conversations --instance main
```

第一次会处理全部历史，消息多时要等一会儿，也要几倍于数据库大小的临时磁盘。不想要全部历史就先设置 `export.since`；只要某些群就设 `export.include`，见 [CONFIGURATION.md](CONFIGURATION.md)。

图片：导出时优先用客户端配置算出 V2 图片密钥。如果 `media_pending` 一直很多，而你确定图片已经缓存了，可以试：

```sh
./bin/wechat-export-send keys image-key --instance main --i-own-this-account
```

## 11. 发送

每个目标会话要先校准一次：

```sh
./bin/wechat-send --instance main --to "项目群" --calibrate
```

打开 `instances/main/calibration/headers/` 下新生成的 `.check.png`（或者看 noVNC），**确认选中的确实是这个群**。这张模板决定了以后能不能往里发。然后：

```sh
./bin/wechat-send --instance main --to "项目群" --text "测试" --dry-run --json
./bin/wechat-send --instance main --to "项目群" --text "大家好" --request-id demo-1 --json
```

默认的界面坐标是 4.1.13.26、窗口 917×667 时量的。别的版本要自己调 [`ui-calibration.json`](CONFIGURATION.md#界面校准-ui-calibrationjson)。

## 12. 长期运行

### systemd

把目录放到 `/opt/wechat-export-send`（放在别处就改 unit 里的路径）：

```sh
cp service/systemd/* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now wechat-export-send-desktop@main.service
systemctl enable --now wechat-export-send-export@main.timer
systemctl enable --now wechat-export-send-watchdog@main.timer
```

### OpenWrt / iStoreOS（procd）

```sh
cp service/openwrt/wechat-export-send-desktop.init /etc/init.d/wechat-export-send-desktop-main
cp service/openwrt/wechat-export-send-export.init /etc/init.d/wechat-export-send-export-main
chmod +x /etc/init.d/wechat-export-send-*-main
# 编辑这两个文件顶部的 APP 和 INSTANCE
/etc/init.d/wechat-export-send-desktop-main enable && /etc/init.d/wechat-export-send-desktop-main start
/etc/init.d/wechat-export-send-export-main enable && /etc/init.d/wechat-export-send-export-main start
# 看门狗：
echo '* * * * * /opt/wechat-export-send/bin/wechat-export-send watchdog --instance main >/dev/null 2>&1' >> /etc/crontabs/root
```

内存小的设备注意：桌面容器上限 2 GB，导出和发送时还要解密数据库。

## 多账号

```sh
./bin/wechat-export-send instance create work
./bin/wechat-export-send instance up work
```

每个实例独立登录、捕获、导出。两个实例不要登录同一个账号。

`wechat-send` 有多个实例时要指定：`--instance work`，或者环境变量 `WES_INSTANCE=work`。

把实例放到别的磁盘：设环境变量 `WES_HOME=/data/wes`。客户端目录用 `WES_CLIENT_DIR`。

## 对接已经在跑的微信容器

已经有一个登录好的容器，不想动它时：

```sh
./install.sh --no-deps --tools     # 只要工具镜像；宿主没有 Python 依赖也行，导出跑在容器里
./bin/wechat-export-send instance attach main \
  --container 已有容器的名字 \
  --gui 容器的 HOME 在宿主上的目录 \
  --keys 已有的 all_keys.json \
  --runner container
./bin/wechat-export-send export --instance main
./bin/wechat-export-send verify-export --instance main
```

- 客户端目录只读挂载，容器不会被启动或停止，`instance up/down` 会拒绝。
- 密钥复制一份进实例目录。导出、状态都在实例目录。
- 容器用户默认取 `--gui` 目录的属主。
- 容器在另一个 dockerd 里：加 `--docker-host unix:///路径`。这时 worker 进不了它的网络命名空间，容器要用 host 网络，并加 `--network host`。
- X 显示号不是 `:99`：加 `--display :100`。
- `verify-export` 只输出计数，拿解密后的数据库核对条数。刚写入的消息可能让两边差几条，隔一会儿再跑。

## 文件传输助手这类搜不到的会话

有些账号的搜索不返回文件传输助手。先在 noVNC 里点开它，然后：

```sh
./bin/wechat-send --instance main --to 文件传输助手 --calibrate --current
```

当前打开的会话标题和模板一致时，发送不会再去搜索。所以发之前要保证它是打开的，不一致时会走搜索，搜不到就报 `failed`。

## 接下游

导出怎么读、发送怎么调，见 [INTEGRATION.md](INTEGRATION.md)。

## 升级

解压新版本，把 `instances/` 和 `client/` 搬过去（或者一直用 `WES_HOME`、`WES_CLIENT_DIR` 放在外面），重新 `./install.sh --images`，再 `instance up`。`conversation_id` 由实例的盐决定，不会因为升级改变。

## 卸载

```sh
./bin/wechat-export-send instance down main
rm -rf instances/main    # 这会删除登录态、密钥和导出，确认不需要了再删
docker image rm wechat-export-send-tools:local wechat-export-send-desktop:1.0.0
```

看 systemd 或 procd 服务是否也要停用。
