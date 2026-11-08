# 配置

每个实例有两份配置，都在 `instances/<名字>/`：

- `config.json`：程序的配置。**只写你想改的项**，其余用默认值。写错键名会直接报错。完整默认值见 `config.example.json`，或运行 `wechat-export-send defaults`。
- `instance.env`：compose 用的环境变量，`instance create` 生成。

```json
{
  "instance": "main",
  "export": {"include": ["项目群"], "since": 1790000000},
  "watchdog": {"bark_url": "https://你的推送服务/设备密钥"}
}
```

## config.json

### 顶层

| 参数 | 类型 | 默认值 | 含义 | 什么时候改 |
|---|---|---|---|---|
| `instance` | 字符串 | 目录名 | 实例名，决定容器名 `wes-<名字>-desktop` | 不用改 |
| `account_dir` | 字符串 | `""` | `xwechat_files/` 下的账号目录名，如 `abc_1234` | 同一个客户端登录过多个账号时必须写，程序绝不猜 |

### export：导出

| 参数 | 类型 | 默认值 | 含义 | 什么时候改 |
|---|---|---|---|---|
| `export.root` | 字符串 | `"export"` | 导出目录，相对实例目录，也可以写绝对路径 | 想放到别的盘。不要放进任何 git 仓库或公开目录 |
| `export.timezone` | 字符串 | `"Asia/Shanghai"` | 写进 manifest 的显示时区，IANA 名字。消息时间本身总是 UTC 秒 | 你不在东八区 |
| `export.id_mode` | `"hash"` 或 `"raw"` | `"hash"` | `hash`：ID 用加盐哈希。`raw`：保留微信账号（把不允许的字符换成 `_`） | 消费方确实需要原始账号时。**建完不要再改**，否则会话 ID 全变 |
| `export.kinds` | 字符串数组 | `["group","private"]` | 导出哪些类型：`group`、`private`、`channel`（公众号）、`other`（文件传输助手等） | 想要公众号或别的 |
| `export.include` | 字符串数组 | `[]` | 白名单：会话名、账号或 `conversation_id`。空表示全部 | 只要几个群 |
| `export.exclude` | 字符串数组 | `[]` | 黑名单，写法同上，优先于白名单 | 排除某些会话 |
| `export.since` | 整数（Unix 秒） | `0` | 不导出比这更早的消息 | 不想导全部历史 |
| `export.lookback_seconds` | 整数 | `3600` | 回看窗口：这段时间内的旧行会比较指纹，发现撤回、编辑就重写一行 | 消息多到每次回看太慢时调小；想跟上更晚的改动时调大 |
| `export.interval_seconds` | 整数 | `300` | `export --watch` 的间隔 | 按需。用 systemd timer 时改 timer，不看这个 |
| `export.members` | 布尔 | `true` | 写不写 `members/*.json`（群成员的哈希 ID、昵称） | 不需要成员表 |
| `export.media.enabled` | 布尔 | `true` | 处理不处理媒体。`false` 时不解图，图片消息的 `status` 只会是 `pending` 或 `missing` | 只要文字 |
| `export.media.max_bytes` | 整数 | `104857600` | 超过这个大小的文件不复制（报 `missing`）。只管文件，不管图片 | 磁盘紧或要收大文件 |
| `export.media.thumbnails` | 布尔 | `false` | 没有原图时额外收一份缩略图，单独成项并标 `is_thumbnail: true`，不会顶替原图 | 消费方能接受缩略图 |
| `export.media.files` | 布尔 | `true` | 复制不复制本地已下载的文件（`msg/file/`） | 不想要文件 |
| `export.media.pending_days` | 整数 | `7` | `pending` 的图片继续等多少天。到了就不再检查。`0` 表示不等 | 按需 |

### send：发送

| 参数 | 类型 | 默认值 | 含义 | 什么时候改 |
|---|---|---|---|---|
| `send.mode` | `"docker"` 或 `"direct"` | `"docker"` | `docker`：worker 跑在工具容器里。`direct`：直接在当前进程环境跑，要求自己能连到 X 显示 | 已经在容器里，或者测试 |
| `send.tools_image` | 字符串 | `"wechat-export-send-tools:local"` | 工具镜像，捕获和看门狗也用它 | 自己打了别的 tag |
| `send.calibration` | 字符串 | `""` | 界面校准文件，相对实例目录。空时先找 `calibration/ui-calibration.json`，再用 `config/ui-calibration.json` | 一般不用；放到默认位置更简单 |
| `send.timeout_seconds` | 整数（≥30） | `180` | 一次调用的总时限，可被 `--timeout` 覆盖 | 经常发大图 |
| `send.lock_wait_seconds` | 整数 | `60` | 同一实例正在发送时等多久，超时报 `failed` | 调用方并发高 |
| `send.confirm_base_seconds` | 整数 | `35` | 点击后等回执的基础时间（校准文件的 `confirmation_timeout_seconds` 优先） | 经常误报 `unknown`，见排障 |
| `send.max_image_bytes` | 整数 | `26214383` | 图片上限，超过报 `failed` | 一般不改 |
| `send.max_text_bytes` | 整数 | `16384` | 文字上限，超过报 `failed` | 一般不改 |
| `send.evidence` | 布尔 | `true` | 存发送前后的截图（标题以下涂黑），只留最近 40 张 | 不想留截图 |
| `send.memory_mb` | 整数 | `768` | worker 容器的内存上限 | 数据库大时可能要调大 |
| `send.tmpfs_mb` | 整数 | `384` | worker 容器放解密副本的 tmpfs 大小。要装得下最大的一个 `message_N.db` | 报 `failed` 且阶段是 `baseline` 时调大 |

### originals：取原图（默认关）

打开后 `fetch-originals` 会访问腾讯 CDN。

| 参数 | 类型 | 默认值 | 含义 | 什么时候改 |
|---|---|---|---|---|
| `originals.enabled` | 布尔 | `false` | 总开关 | 确实需要原图 |
| `originals.client_version_code` | 整数 | `-227271398` | 协议里的客户端版本号，这是 4.1.13.26 的值 | 换客户端版本时 |
| `originals.max_per_run` | 整数 | `50` | 一次最多下载几张 | 按需 |
| `originals.timeout_seconds` | 整数 | `600` | 一次运行的总时限 | 按需 |

### media_origin：媒体服务（默认关）

| 参数 | 类型 | 默认值 | 含义 | 什么时候改 |
|---|---|---|---|---|
| `media_origin.enabled` | 布尔 | `false` | 总开关 | 另一个容器要通过 HTTP 取媒体 |
| `media_origin.port` | 整数 | `8099` | 端口，只绑 127.0.0.1 | 冲突时 |
| `media_origin.token` | 字符串 | `""` | 访问用的 Bearer token，打开时必填 | 用长随机串 |

### watchdog：看门狗

| 参数 | 类型 | 默认值 | 含义 | 什么时候改 |
|---|---|---|---|---|
| `watchdog.bark_url` | 字符串 | `""` | Bark 推送地址。空时不推送，状态照常记录 | 想收到掉线提醒 |
| `watchdog.group` | 字符串 | `"wechat"` | Bark 分组 | 按需 |
| `watchdog.fail_level` | 字符串 | `"timeSensitive"` | 故障提醒的级别 | 按需 |
| `watchdog.recovery_level` | 字符串 | `"active"` | 恢复提醒的级别 | 按需 |

## instance.env

| 变量 | 默认值 | 含义 | 什么时候改 |
|---|---|---|---|
| `INSTANCE` | 实例名 | compose 项目和容器名 | 不改 |
| `INSTANCE_DIR` | 实例目录 | 挂载 `gui/` 用 | 搬目录后要同步改 |
| `VNC_WEB_PORT` | 17900 起 | noVNC 在宿主上的端口 | 冲突时 |
| `VNC_BIND` | `127.0.0.1` | noVNC 绑定的地址。**不要改成公网地址**，它没有密码 | 基本不改 |
| `PUID`、`PGID` | 当前用户 | 桌面容器和 worker 的用户。要和读 `gui/` 的用户一致 | 用别的用户运行 |
| `TZ` | `Asia/Shanghai` | 桌面容器的时区 | 按需 |
| `CLIENT_ROOT` | `client install` 的结果 | 客户端目录 | 换客户端版本后重新生成或手改 |
| `WECHAT_BINARY_SHA256` | 客户端哈希 | 只启动这个构建。留空表示不检查 | 主动换客户端时 |
| `DESKTOP_IMAGE` | `wechat-export-send-desktop:<版本>` | 桌面镜像 | 自己打 tag 时 |
| `DESKTOP_PLATFORM` | `linux/amd64` | 桌面镜像的架构 | 基本不改 |
| `DESKTOP_MEM_LIMIT` | `2g` | 内存上限 | 内存紧或群很多 |
| `DESKTOP_CPUS` | `1.5` | CPU 上限 | 按需 |
| `VNC_PROXY_BIND`、`VNC_PROXY_PORT`、`VNC_PROXY_HTPASSWD` | 无 | 只有 `compose.proxy.yaml` 用：内网地址、端口、htpasswd 文件 | 要把 noVNC 开给内网时 |

`compose.selkies.yaml` 还用 `WECHAT_DESKTOP_IMAGE`、`DESKTOP_USER`、`DESKTOP_PASSWORD`。

## 环境变量

| 变量 | 含义 |
|---|---|
| `WES_HOME` | 实例根目录，默认 `<安装目录>/instances` |
| `WES_CLIENT_DIR` | 客户端根目录，默认 `<安装目录>/client` |
| `WES_INSTANCE` | `wechat-send` 默认用哪个实例 |

## 界面校准 ui-calibration.json

要改的话复制到 `instances/<名字>/calibration/ui-calibration.json`，覆盖默认的 `config/ui-calibration.json`。所有坐标都相对微信窗口左上角。

| 字段 | 默认值 | 含义 |
|---|---|---|
| `version` | `1` | 必须是 1 |
| `client_version` | `4.1.13.26` | 只是说明这份坐标是哪个版本量的 |
| `display` | `:99` | X 显示 |
| `window_title_substring` | `Weixin` | 用来找主窗口的标题片段 |
| `window` | 917×667 | 主窗口的模板尺寸。发送时窗口偏离它会被调回来。`x`、`y` 只是参考 |
| `login_window` | 292×396 | 登录窗口的尺寸，用来判断掉线 |
| `search_box` | `[155,60]` | 搜索框 |
| `search_result` | `[170,140]` | 第一条搜索结果 |
| `compose` | `[500,560]` | 输入框 |
| `send_button` | `[870,633]` | 发送按钮 |
| `send_button_region` | `[840,618,62,30]` | 判断按钮变绿（图片就绪）的区域 |
| `header_region` | `[327,43,75,32]` | 和模板比较的标题区域。改了要重新校准所有会话。名字前几个字相同的会话可以调宽，但标题带人数时人数变化会导致比较失败（这是安全的失败） |
| `header_pixel_threshold` | `8` | 单个像素差多少算不同 |
| `header_max_bad_fraction` | `0.001` | 不同像素的比例上限 |
| `allowed_visible_window_titles` | 见文件 | 这些标题的窗口不算意外弹窗 |
| `image_ready_timeout_seconds` | `45` | 图片粘贴后等按钮就绪的时间 |
| `search_delay_seconds` | `1.2` | 输入搜索词后等多久 |
| `switch_delay_seconds` | `1.2` | 点结果后等多久 |
| `clipboard_wait_seconds` | `8` | 等微信来取剪贴板图片的时间 |
| `paste_settle_seconds` | `1.5` | 粘贴文字后等多久 |
| `send_settle_seconds` | `2` | 点发送后等多久才开始查回执 |
| `confirmation_timeout_seconds` | `35` | 等回执的基础时间 |
| `window_restore_timeout_seconds` | `5` | 调整窗口尺寸的等待时间（可选字段） |
| `window_restore_settle_seconds` | `1` | 调整后再等多久（可选字段） |

## wechat-send 参数

| 参数 | 含义 |
|---|---|
| `--to` | 完整的会话名（备注、昵称、群名都可以，必须完全相等且唯一），或 `conversation_id`，或原始账号 |
| `--text` / `--text-file` / `--image` | 三选一。图片：PNG 原样发送，JPEG、WebP 先转 PNG，动图不支持 |
| `--request-id` | 幂等键。已 `sent` 的直接返回上次结果；`unknown` 或上次中断的报 `unknown`，不发；`failed` 的可以重试；同一个键换了内容或目标报 `invalid` |
| `--timeout` | 总时限（秒，≥30） |
| `--dry-run` | 定位会话、校验标题和空草稿，不提交，返回 `dry_run`。可以不带内容 |
| `--json` | 总是输出 JSON，留着是为了和接口约定一致 |
| `--instance` | 实例名 |
| `--calibrate` | 选中会话，保存标题模板，返回 `calibrated` |

标准输出里除了约定的字段，还可能有 `extra`：`code`（机器可读的失败原因）、`stage`（走到哪一步）、`receipt`（`server_id`、`local_id`、`create_time`、`source_db`）。
