# 排障

第一步永远是：

```sh
./bin/wechat-export-send doctor --instance main
```

## 安装和 demo

**`install.sh` 说没有 venv 也没有 pip**
Debian/Ubuntu：`apt-get install -y python3-venv`，再运行一次。

**demo 报「输出目录不是空的」**
`rm -rf demo-output`，或者 `demo --output 新目录`。

**demo 很慢，提示纯 Python AES**
没装依赖。`./install.sh`。

**`docker compose` 报 `unknown shorthand flag` 或 `compose is not a docker command`**
没装 Compose 插件。Debian/Ubuntu：`apt-get install -y docker-compose-plugin`（来自 Docker 官方源）。

**`./install.sh` 没有提问，只打印「不提问……只装了依赖」**
你是在脚本或自动化工具里运行的（没有终端）。用答案文件：`cp install.env.example install.env`，改好后 `./install.sh --config install.env`；或者装完再运行 `./bin/wechat-export-send setup --answers install.env`。

**向导说「接真实账号之前，先处理这些」**
它列出的每一条后面都有要运行的命令（装 Docker、装 python3-venv、换 x86_64 机器）。处理完重新运行 `./bin/wechat-export-send setup`，做完的步骤会跳过。

**命令报「错误：……」，后面让你先运行 doctor**
程序遇到了没预料到的情况。照提示先跑 `./bin/wechat-export-send doctor`；要看完整的出错位置（提 issue 时附上，注意先删掉路径里的私人信息）：在命令前加 `WES_DEBUG=1`。

## 真实部署时踩过的坑（在 iStoreOS 上用这个 release 接真实账号时遇到的）

**窗口标题变成了「微信」，看门狗一直报 offline、发送找不到窗口**
桌面镜像的语言环境被改成了 `zh_CN`：客户端的窗口标题会从 `Weixin` 变成「微信」，而界面校准和窗口识别都是按 `Weixin` 做的。不要改 `docker/desktop/Dockerfile` 里的 `LANG=C.UTF-8`；改过的话改回来，`./install.sh --images` 重新构建，再 `instance down`、`instance up`。对接别人的容器（`instance attach`）而它用的是中文环境时，把实例的 `ui-calibration.json` 里 `window_title_substring` 改成 `微信`。

**Debian 12 上 `client install` 崩溃，报 `TypeError` 和 `filter`**
Debian 12 自带的 Python 3.11.2 的 `tarfile` 还没有 `filter` 参数。1.0.0 已经改成自己检查路径和链接，不再用这个参数；还遇到的话说明你在用更早的代码，换成 1.0.0 的 release 包。

**`doctor` 报 keys 目录「属于 uid …，当前用户读写不了」，或者 `export` 报没有权限**
用 root 建的实例（或 `instance attach`），实例目录会交给容器里的用户（默认是客户端数据目录的属主），这样容器才写得进去。之后要一直用 root 运行（命令前加 `sudo`）。不要把 `keys/` 改成别人能读的权限：doctor 要求 `keys/` 是 700、密钥文件是 600，`chmod 700 instances/<实例>/keys && chmod 600 instances/<实例>/keys/*.json` 可以改回来。

**往微信窗口按了 Esc 以后主窗口不见了**
客户端收到 Esc 会把主窗口收起来，这时 noVNC 里只剩桌面。不要按 Esc；要找回来，在容器里再启动一次客户端（它是单实例，会把已有的窗口叫出来）：`docker exec wes-main-desktop timeout 25 /opt/wechat/wechat --no-sandbox`。

**内存很小的设备（4～8 GB）上导出时卡死**
桌面容器上限 2 GB，导出还要解密数据库。导出前先看 `grep MemAvailable /proc/meminfo`，低于约 1.2 GB 就先别跑；定时导出用 `flock -n /tmp/wes-export-main.lock ./bin/wechat-export-send export --instance main` 防止两次导出叠在一起。BusyBox 的 `free -m` 会忽略 `-m`，输出单位其实是 KB，别看错。

## 桌面容器

**`instance up` 报「先运行 wechat-export-send client install」**
还没有安装客户端，或者 `instance.env` 里 `CLIENT_ROOT` 是空的。装好后再运行 `instance up`。

**容器反复重启**

```sh
./bin/wechat-export-send instance logs main
docker exec wes-main-desktop cat /tmp/wechat.err
```

退出码 78 且提示「哈希不一致」：客户端被换过。确实要换就重新 `client install`，把 `WECHAT_BINARY_SHA256` 和 `CLIENT_ROOT` 改成新的。

**微信启动报缺库（`error while loading shared libraries`）**
不同版本依赖可能不同。查缺什么：

```sh
docker exec wes-main-desktop sh -c 'ldd /opt/wechat/wechat | grep "not found"'
```

在 `docker/desktop/Dockerfile` 的包列表里加上，重新构建。欢迎提 issue。

**ARM 机器上起不来或极慢**
客户端是 x86_64 的，只测过 x86_64，见 [COMPATIBILITY.md](COMPATIBILITY.md)。

**noVNC 打不开**
确认隧道：`ssh -L 17900:127.0.0.1:17900 服务器`。服务器上试 `curl -I http://127.0.0.1:17900/vnc.html`。端口见 `instances/main/instance.env`。

**中文显示成方块**
镜像里没有字体时才会这样，重新 `./install.sh --images`。

## 密钥

**`keys check` 报 `Runtime capture point does not match the official binary`**
这个客户端构建的捕获点定位不上。不要跳过检查。换实测过的版本，或者提 issue 附上版本号（不要附密钥）。

**`keys capture` 报 `Hardware capture point was not supported`**
虚拟化环境不支持硬件断点。改用 `--method native`。

**`keys capture` 报 `Login-time capture did not complete`**
超时前没有完成重新登录。默认 300 秒，可以 `--timeout 600`，开始后马上去 noVNC 退出并重新登录。

**报 `Captured candidates did not pass database HMAC verification`**
捕获到的东西对不上库。确认只登录了这一个账号；有多个账号目录时设 `account_dir`。

**报 `Client is already traced or stopped`**
上一次捕获没退干净。`docker rm -f wes-main-capture`，再 `docker restart wes-main-desktop`。

**`需要唯一确定一个账号目录，现在找到 2 个`**
这个客户端登录过两个账号。看 `ls instances/main/gui/xwechat_files/`，把要的那个写进 `config.json` 的 `account_dir`。

**`doctor` 说 HMAC 校验不过**
换过账号、重建过库，或者客户端升级了。重新 `keys capture`。

## 导出

**`stats.errors` 里有 `no_verified_key`**
新出现了分片（比如 `message_2.db`），密钥文件里没有。重新 `keys capture`。这些分片这一轮没有导出。

**`stats.errors` 里有 `unmapped_conversation`**
一张消息表对不上任何联系人，导出不猜它是谁，直接跳过。通常是已经退出的群。想确认的话，看同一条记录里的 `table`。

**报 `unsupported message schema: missing [...]`**
这个客户端版本改了表结构，导出拒绝继续。附上版本号和缺的列名提 issue。

**报 `source database changed during all snapshot attempts`**
复制时客户端一直在写。下一轮通常就好了。持续出现说明磁盘太慢，试试调大导出间隔。

**图片一直是 `pending`**
客户端还没下载原图。在 noVNC 里打开那张图，或者开 `originals.enabled` 用 `fetch-originals`。过了 `pending_days` 就不再检查。如果本地明明有缓存，试 `keys image-key`。

**图片是 `missing`**
消息里没有可下载的描述，或者 `media.enabled` 是 `false`。语音、视频目前都写 `missing`：只导出消息记录，不导出这两类文件。

**`sender_name` 不是群昵称**
群昵称是尽力解析的，格式不是公开规范。解析不了时退回联系人的备注或昵称。

**导出占满了磁盘**
`du -sh instances/main/*`。媒体按内容去重但不会自动清理。残留的解密临时目录在 `instances/main/state/export-*`：确认没有导出在运行后可以删。

**报「另一个导出正在运行」（退出码 3）**
上一次还没结束，下次定时再来就好。确认没有进程后不用手工删锁，锁随进程释放。

## 发送

看输出里的 `extra.code`。

| `extra.code` | 意思 | 怎么办 |
|---|---|---|
| `target_not_found` | 联系人库里没有这个名字 | `--to` 要写完整的名字，或者用 `conversations` 列出的 ID。新建的群要等客户端写进数据库 |
| `target_ambiguous` | 有多个同名会话 | 用 `conversation_id` |
| `search_ambiguous` | 搜索词还能匹配别的联系人 | 改一下群名或备注，让它不是别人名字的一部分 |
| `header_not_calibrated` | 没有标题模板 | `wechat-send --to X --calibrate` |
| `header_blank` | 校准时标题是空白，没有选中会话 | 第一条搜索结果可能是网页搜索。在 noVNC 里打开目标会话，再 `--calibrate --current` |
| `header_mismatch` | 选中的会话和模板不一致 | 看 noVNC；群改名、改了头像或窗口布局变了就重新校准 |
| `draft_not_empty` | 输入框里有草稿 | 在 noVNC 里清空。上一次 `failed` 在粘贴之后也可能留下草稿 |
| `draft_mismatch` | 粘贴后读回的文字不一样 | 含有微信会替换的字符（比如某些表情）时可能发生；在 noVNC 里清空草稿，换个写法 |
| `paste_not_requested` / `paste_not_ready` | 图片没粘上 | 看 noVNC；坐标可能不对 |
| `focus_lost` | 拿不到焦点 | 有人在 noVNC 里操作？ |
| `desktop_unavailable` | 桌面容器没运行 | `instance up` |
| `worker_not_started` | worker 没起来 | `./install.sh --images`，确认工具镜像存在 |
| `receipt_unconfirmed` | 点了发送，数据库里没确认到 | 见下 |
| `worker_not_stopped` | 超时后确认不了 worker 停没停 | `docker ps` 看 `wes-send-*`，手工 `docker kill`；再人工确认发没发 |
| 其他（Python 异常类型名） | 意外错误 | 看 `extra.stage` 走到哪一步 |

**收到 `unknown` 怎么办**
打开 noVNC 看目标会话，消息到底发出去没有。

- 发出去了：不用做什么。
- 确实没发出去，要补发：**换一个新的 `--request-id`**。旧键会一直报 `unknown`，这是故意的。

**经常误报 `unknown`（消息其实发出去了）**
回执等太短。调大 `confirmation_timeout_seconds` 或 `--timeout`。也看看是不是磁盘慢，每轮查回执都要解密一遍消息库。

**`Weixin client window not found`、`ambiguous`、`unexpected visible window/dialog`**
看 noVNC：微信掉线了（登录窗口）？弹了对话框？关掉对话框再试。如果那是正常窗口，把它的标题加进 `allowed_visible_window_titles`。

**主窗口不见了**
Esc 会把微信主窗口收起来，程序不会按它，但你在 noVNC 里可能按到。容器里再启动一次客户端就能唤醒（单实例，不会多开）：`docker exec -d <容器> sh -c "cd /config; timeout 25 /opt/wechat/wechat --no-sandbox"`。

**出现「Search」或 922×802 的网页窗口**
点到了搜索下拉框里的网页搜索。关掉那个窗口，再调整 `search_result` 或者用 `--calibrate --current`。

**校准的时候选中了错的会话**
删掉 `calibration/headers/<id>.png`，确认搜索结果的第一条就是目标后重来；或者调 `search_result` 坐标。

**点错位置**
坐标是 4.1.13.26 量的。在 noVNC 里量出新位置，复制并修改 `ui-calibration.json`。改完先 `--dry-run`。

## 看门狗

**一直是 `unavailable`**
工具镜像没构建，或者窗口标题不是 `Weixin`、`微信`。标题随语言环境变：桌面镜像固定 `LANG=C.UTF-8` 时是 `Weixin`。用 `docker exec wes-main-desktop xwininfo -display :99 -root -tree | grep -i -E "weixin|微信"` 看实际的标题和尺寸。发送器按 `window_title_substring` 找窗口，标题变了要一起改。

**没收到推送**
`watchdog --instance main --test-notify`。没配 `bark_url` 时状态会记录但不推送。

**掉线了**
到 noVNC 扫码。重新登录后数据库盐一般不变，密钥继续可用；`doctor` 能确认。
