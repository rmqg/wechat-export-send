# 下游怎么接

写给第一次对接 `wechat-export-send` 的人：怎么读它导出的聊天记录，怎么调它发消息。这里只讲外部接口，字段的完整定义见 [CONTRACT.md](CONTRACT.md)。

下面的命令假设：

| 占位 | 含义 | 例子 |
|---|---|---|
| `/opt/wechat-export-send` | 安装目录 | |
| `/opt/wechat-export-send/instances` | 实例根目录，也就是环境变量 `WES_HOME` | |
| `main` | 实例名，一个实例对应一个微信账号 | |
| `g_1a2b3c` | 会话的 `conversation_id`，第 2 节教你查 | |

先把这三行贴进 shell，后面的命令就能直接复制：

```sh
export WES_HOME=/opt/wechat-export-send/instances
export EXPORT=$WES_HOME/main/export
export WES=/opt/wechat-export-send/bin
```

`WES_HOME` 默认就是安装目录下的 `instances`。如果安装目录是软链接，或者实例放在别处，就必须显式设置。

## 1. 导出在哪、谁能读

导出目录默认是 `<实例目录>/export`，配置项 `export.root` 可以改。它是一个 `chat-export/v1` 目录：

```
export/
  manifest.json                         会话清单，原子替换
  conversations/<conversation_id>.jsonl 每行一条消息，只追加
  members/<conversation_id>.json        群成员，可选
  media/<前两位>/<sha256>.<扩展名>       媒体，按内容寻址
  state/                                生产方自用，不要读
```

**目录权限是 700，属主是实例用户。** 读取方必须用同一个 UID，或者用 root。查属主：

```sh
ls -ldn "$EXPORT"
```

输出的第 3、4 列就是 UID 和 GID，比如 `drwx------ 6 1200 1201 ...`。

读取方跑在容器里时，容器用户也要是这个 UID，否则读不了。以 chat-digest 为例，`project.json` 的 `worker_uid`、`worker_gid` 要写成同样的值。

导出里是真实聊天内容，**不要放宽权限，不要放进任何代码仓库**。只读挂载就够了，读取方不需要写权限。

## 2. 查 conversation_id

用 Python 读 manifest，不需要额外依赖：

```sh
python3 - <<'EOF'
import json, os
manifest = json.load(open(os.path.join(os.environ["EXPORT"], "manifest.json"), encoding="utf-8"))
for item in manifest["conversations"]:
    print(item["id"], item["kind"], item["message_count"], item["name"], sep="\t")
EOF
```

如果用 chat-digest：

```sh
./bin/chat-digest list-conversations --export-root "$EXPORT"
```

- `kind` 是 `group`、`private`、`channel`、`other`。默认只导出群和私聊。
- **ID 由实例的盐决定**（实例目录下的 `state/id-salt`）。同一个实例里 ID 一直不变，升级也不变。**删掉实例重建，所有 ID 都会变**，下游配置里的 ID 要跟着改。备份和搬迁时要带上这个文件。
- 名字可能重复或改变，配置里请写 ID，不要写名字。

## 3. 导出的语义

每行一个 JSON 对象。下面这些是最容易接错的地方。

**同一个 id 可能出现多次，以最后一行为准。** 撤回、编辑，或者图片后来下载到了，都会用同一个 id 再写一行。这种行的 `ts` 是原来的时间，所以文件不保证严格按时间递增。读的时候先按 `id` 去重：

```sh
CONV=g_1a2b3c python3 - <<'EOF'
import json, os
path = os.path.join(os.environ["EXPORT"], "conversations", os.environ["CONV"] + ".jsonl")
latest = {}
with open(path, encoding="utf-8") as stream:
    for line in stream:
        try:
            message = json.loads(line)
        except ValueError:
            continue  # 正在写的最后一行可能不完整，下次再读
        latest[message["id"]] = message
messages = sorted(latest.values(), key=lambda item: (item["ts"], item["id"]))
others = [item for item in messages if not item.get("is_self")]
print(len(messages), "条，其中别人发的", len(others), "条")
EOF
```

| 字段或类型 | 语义 | 下游要做什么 |
|---|---|---|
| `is_self` | 只有导出账号自己发的消息才有，值是 `true` | **导出不过滤自己的消息。** 不想要就自己过滤，比如机器人发的通知不该进总结 |
| `sender_name` | 优先用群昵称，其次备注，再次微信昵称。群昵称是尽力解析的 | 要稳定地认人就用 `sender_id`，名字会变 |
| `sender_id` | 哈希过的稳定 ID；系统消息是 `system` | |
| `type: quote` | 引用消息。`text` 只是新写的那句话，**被引用的内容在 `reply_to`**（`id`、`sender_name`、`text`） | 需要上下文时把 `reply_to.text` 拼上 |
| `type: forward` | 合并转发。`text` 是标题，条目在 `forward` 数组 | |
| `type: system` | 系统提示，包括入群提示和**拍一拍** | 拍一拍不是真人发言，不要算进发言统计 |
| `type: revoke` | 撤回提示，`sender_id` 是 `system` | 被撤回的那条原文可能还在更早的行里，由下游决定怎么处理 |
| `text` | **保留原始空白**：换行、连续空格、特殊空格都不动 | 需要归一化就自己做。图片、语音等没有正文时不写，不会出现占位文字 |
| `mentions` | 被 @ 的 `sender_id`；@所有人是 `"@all"` | |
| `raw_type` | 微信的原始类型，如 `49:57` | 只用来排查 |

### media

```json
{"kind": "image", "status": "ready", "path": "media/ab/ab12....jpg", "sha256": "ab12...",
 "mime": "image/jpeg", "bytes": 12345, "is_thumbnail": false, "source_quality": "client_full"}
```

| 字段 | 取值 | 含义 |
|---|---|---|
| `status` | `ready` | 文件已经在 `path`，路径相对导出目录 |
| | `pending` | 消息里有下载地址，但本地还没有完整图片。之后如果有了，会用同一个 id 重写一行，默认等 7 天 |
| | `missing` | 拿不到。**语音、视频、表情目前都是 `missing`**，只有消息记录，没有文件。文件消息要客户端已经下载过才会 `ready` |
| `is_thumbnail` | `false` / `true` | 缩略图绝不会冒充原图。默认不收缩略图，打开配置后它会单独成一项，并标 `true` |
| `source_quality` | `original` | 长度和 md5 都和消息记录对得上 |
| | `client_full` | 客户端缓存的完整图，但不一定和原图逐字节相同 |

`status` 不是 `ready` 时没有 `path`。消费方看到 `pending` 不要报错，也不要等，下次读到新行再处理。

微信私有的 `wxgf` 格式图片按原样保存，`mime` 是 `application/x-wxgf`，下游要自己决定是否解码。

## 4. 新鲜度

导出是增量的，要定时跑。推荐每 2 分钟一次，这样数据最多落后约 2 分钟（再加上一次导出本身的几秒）：

```cron
*/2 * * * * flock -n /tmp/wes-export-main.lock env WES_HOME=/opt/wechat-export-send/instances /opt/wechat-export-send/bin/wechat-export-send export --instance main >> /opt/wechat-export-send/instances/main/export-cron.log 2>&1
```

要最新的数据时，可以手动跑一次：

```sh
"$WES/wechat-export-send" export --instance main
```

| 退出码 | 含义 | 怎么办 |
|---|---|---|
| 0 | 完成 | 标准输出是计数 |
| 3 | **另一个导出正在运行**，这次没有写任何消息 | 不是错误。正在跑的那个结束后，数据就是新的，过几秒再试一次即可 |
| 其他 | 出错了 | 看标准错误，再跑 `doctor` |

和 cron 撞上时可以这样等：

```sh
for attempt in 1 2 3 4 5 6; do
    "$WES/wechat-export-send" export --instance main; code=$?
    [ "$code" = 3 ] || break
    sleep 10
done
echo "退出码 $code"
```

读取和写入可以同时进行，不用加锁：`manifest.json` 是原子替换的，JSONL 只追加。读到不完整的最后一行时跳过即可。`manifest.json` 比消息行晚更新，所以要最新的数据时读 JSONL，不要只看 `last_ts`。

核对条数（只输出计数，不输出内容）：

```sh
"$WES/wechat-export-send" verify-export --instance main
```

刚写入的消息可能让两边差几条，隔一会儿再跑。

## 5. 发送：wechat-send

接口是 `chat-send/v1`。结果总是一个 JSON 对象，打印到标准输出。

**先读这几条：**

- 它操作的是真实的微信窗口。**同一个微信不能有两个程序同时操作**：切换到它之前，要先停掉旧的发送器（比如 wechat-digest v1 的 `send-report.py`）；有人在 noVNC 里操作时也可能失败。同一个实例内部的多次调用会自动排队，默认最多等 60 秒，等不到就报 `failed`。
- `--dry-run`、`--calibrate` 不发消息，但**会操作窗口**：会搜索并切换到目标会话，还会点一下输入框。不要在别人正用这个桌面时跑。
- 不要手工往微信窗口按 Esc，主窗口会被收起来。

### 第一次往某个会话发

每个会话先校准一次，保存标题模板。以后发送时拿它核对点开的会话对不对：

```sh
"$WES/wechat-send" --instance main --to g_1a2b3c --calibrate --json
```

确认一下存下来的模板确实是目标会话：看实例目录 `calibration/headers/g_1a2b3c.check.png`，或者打开 noVNC。这份模板决定了以后能不能往里发，**一定要亲眼确认**。

搜索找不到的会话（比如有些账号的文件传输助手）：先在 noVNC 里点开它，再加 `--current`，这样不搜索，直接用当前打开的会话：

```sh
"$WES/wechat-send" --instance main --to 文件传输助手 --calibrate --current --json
```

然后验证一次，不会提交：

```sh
"$WES/wechat-send" --instance main --to g_1a2b3c --text "测试" --dry-run --json
```

### 发文字和图片

```sh
# 文字
"$WES/wechat-send" --instance main --to g_1a2b3c --text "大家好" --request-id report-20261108-a --json

# 长文字从文件读（UTF-8，不超过 16384 字节）
"$WES/wechat-send" --instance main --to g_1a2b3c --text-file ./message.txt --request-id report-20261108-b --json

# 图片：PNG 原样发送，JPEG、WebP 先转成 PNG，不支持动图，不超过 26214383 字节
"$WES/wechat-send" --instance main --to g_1a2b3c --image ./report.png --request-id report-20261108-c --json
```

`--to` 可以写 `conversation_id`、完整的会话名或原始账号，**推荐写 `conversation_id`**。名字重复，或者搜索词还能匹配到别的联系人时，它会报 `failed`，不会猜。

`--timeout` 是总时限，单位秒，最小 30，默认 180。发大图时可以调大。

### 结果

```json
{"schema": "chat-send/v1", "status": "sent", "request_id": "report-20261108-a", "to": "g_1a2b3c",
 "conversation_id": "g_1a2b3c", "submitted_at": 1794043483, "detail": "...", "extra": {"stage": "done"}}
```

| status | 退出码 | 含义 | 调用方该怎么做 |
|---|---|---|---|
| `sent` | 0 | 已提交，并在数据库里确认到这条新消息 | 记为完成 |
| `failed` | 1 | 在提交**之前**失败，确定没发出去 | 可以重试 |
| `unknown` | 2 | 可能已经发出去了，但没能确认 | **不许自动重试**，交给人工确认 |
| `invalid` | 3 | 参数错误 | 修配置，不重试 |
| `dry_run` | 0 | 只在 `--dry-run` 时出现：校验通过，没有提交 | 不算发送 |
| `calibrated` | 0 | 只在 `--calibrate` 时出现 | 不算发送 |

判断是否发送成功时，**看 `status`，不要只看退出码**：退出码 0 也可能是 `dry_run`。`extra` 里有 `code`（失败原因）、`stage`（走到哪一步）、`receipt`（服务器消息 ID），不认识的字段请忽略。

### 幂等：--request-id

**所有会自动重试的调用都应该带 `--request-id`。**

| 同一个键上次的结果 | 这次会怎样 |
|---|---|
| `sent` | 不再发送，直接返回上次的结果 |
| `failed` | 正常重试 |
| `unknown`，或者进程中途崩溃 | 不发送，返回 `unknown` |
| 这次换了内容或目标 | 返回 `invalid` |

收到 `unknown` 时打开 noVNC 看一眼。确认消息没发出去后，换一个新的 `--request-id` 再发。旧键会一直报 `unknown`，这是故意的。

### 调用方的超时

调用方自己也设了超时时，要比 `--timeout` 长一些，因为排队和收尾还要时间。超时把 `wechat-send` 杀掉后，**只能当成 `unknown`**：它可能已经点了发送。

### 下游跑在容器里

`wechat-send` 是**宿主上的命令**，它会用 `docker run` 启动一个短命的 worker，所以调用方需要：

1. 能执行宿主上的安装目录和实例目录，而且**容器内外路径要相同**：bind mount 的路径是宿主 Docker 解析的。
2. 有 Docker 权限，比如挂载 `/var/run/docker.sock`。这基本等于宿主的 root，要评估风险。
3. 容器里有 `python3` 和 `docker` 命令。

**更简单的办法**是让调度脚本跑在宿主上，只把读导出、生成内容这些活放进容器。chat-digest 就是这样做的。

## 6. 端到端示例：chat-digest

导出 → 配置 → 生成 → 发送。下面的命令在 chat-digest 的安装目录里运行。

**第 1 步：确认导出是新的，查到 ID 和属主。**

```sh
"$WES/wechat-export-send" export --instance main
./bin/chat-digest list-conversations --export-root "$EXPORT"
./bin/chat-digest check-export --export-root "$EXPORT" check --conversation-id g_1a2b3c
ls -ldn "$EXPORT"
```

**第 2 步：准备 chat-digest。**

```sh
./bin/chat-digest init
docker build -t chat-digest-runtime:2.0.0 .
./bin/chat-digest pin-images
```

**第 3 步：编辑 `runtime/project.json`。** 其余配置项见 chat-digest 的 `docs/CONFIGURATION.md`。

```json
{
  "export": {"root": "/opt/wechat-export-send/instances/main/export"},
  "group": {"conversation_id": "g_1a2b3c", "name": "项目群"},
  "send": {
    "command": ["env", "WES_HOME=/opt/wechat-export-send/instances",
                "/opt/wechat-export-send/bin/wechat-send", "--instance", "main"],
    "to": "g_1a2b3c",
    "timeout": 180
  },
  "worker_uid": 1200,
  "worker_gid": 1201
}
```

- `export.root` 是绝对路径。
- **改了 `worker_uid`、`worker_gid` 之后，要再运行一次 `./bin/chat-digest init`**，它会按新的用户重新设置 `runtime/` 的属主。不运行的话，worker 写不进 `runtime/`。
- `send.command` 是**参数列表**，不是一条字符串。chat-digest 会在后面追加 `--to`、内容、`--request-id`、`--timeout` 和 `--json`。
- `worker_uid`、`worker_gid` 换成第 1 步查到的值。

**第 4 步：先校准目标会话。** 发送前必须有标题模板：

```sh
"$WES/wechat-send" --instance main --to g_1a2b3c --calibrate --json
```

**第 5 步：自检和生成，都不发送。**

```sh
./bin/chat-digest doctor --probe-send   # 发送只做 dry run，但会操作窗口
./bin/chat-digest prepare-only          # 生成当天的报告，不发送
```

**第 6 步：发送。** 先停掉其他操作这个微信的发送器，然后：

```sh
./bin/chat-digest daily-tick --force    # 立刻跑一次日报流程，这一步会真的发到群里
```

结果记在 chat-digest 的 `runtime/digest/delivery-state.json`。`unknown` 不会自动重发，要人工确认。

**第 7 步：定时。** 导出的定时任务见第 4 节，chat-digest 的调度：

```cron
* * * * * cd /opt/chat-digest && /usr/bin/python3 deployment/daily-job.py --tick >> runtime/digest/daily-job.log 2>&1
```

## 出了问题

### 报 `unexpected visible window/dialog`

`--dry-run` 或发送返回：

```json
{"schema": "chat-send/v1", "status": "failed", "detail": "提交前出错，确定没有发出去",
 "extra": {"code": "RuntimeError", "stage": "select_conversation", "reason": "unexpected visible window/dialog: 1 window(s)"}}
```

意思是微信窗口之外，还有一个标题不在允许名单里的窗口持续显示了 1 秒以上。这是提交之前的检查，**确定没有发出去**，可以放心重试。

处理步骤：

1. 只读看一眼有哪些窗口（容器名、显示号换成你的）：

   ```sh
   docker exec <桌面容器> xwininfo -display :99 -root -tree | grep -E '^ +0x' | head -20
   ```

2. 打开 noVNC 确认它是什么。图片查看器、更新提示、搜一搜网页窗口这些，用**它自己的关闭按钮**关掉。**不要按 Esc**：焦点在微信主窗口时，主窗口会被收起来。
3. 再跑一次 `--dry-run`，返回 `dry_run` 就可以了。
4. 那个窗口是正常的、以后也会一直在：把它的标题加进 `ui-calibration.json` 的 `allowed_visible_window_titles`。不认识的窗口不要加。

这条输出的标题可能带会话名，所以只报数量，不报名字。

- 导出和发送的排障：`wechat-export-send` 的 `docs/TROUBLESHOOTING.md`。先跑 `wechat-export-send doctor --instance main`。
- 支持哪些版本、哪些路径实测过：`wechat-export-send` 的 `docs/COMPATIBILITY.md`。
