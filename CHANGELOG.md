# 更新日志

## v1.0.0

第一版。从 [rmqg/wechat-digest](https://github.com/rmqg/wechat-digest) v1.2.4 拆分出提取和发送，全新历史。

### 新增

- 多实例：一个容器一个账号，目录、端口、密钥、导出都分开
- `chat-export/v1` 增量导出：回看窗口跟上撤回和编辑，`pending` 的图片之后用同一个 id 重写，哈希 ID
- `chat-send/v1` 的 `wechat-send`：任意会话的文字和图片，`--request-id` 幂等，`--dry-run`，按会话校准标题模板
- 提交边界标记：worker 在按下发送之前落盘，宿主据此严格区分 `failed` 和 `unknown`；docker 命令中断时先确认容器已停
- 自己的桌面镜像（Debian 12）和工具镜像；`client install` 解开 deb，不执行里面的脚本
- `doctor` 自检，离线 `demo`（没装依赖也能跑），systemd unit 和 OpenWrt procd 脚本
- 可选的 `fetch-originals`（默认关）和 `serve-media`（默认关）

### 从生产版带来的修复

- 窗口尺寸漂移时自动调回模板
- 超过约 16.8MB 的 PNG 走 ICCCM INCR
- 发送确认的等待时间随文件大小增加
- 登录看门狗放宽尺寸判断，主窗口偏离模板不再算掉线

### 第一次连真实数据时发现并修好的

- 客户端把数据库预分配到 30 MB，尾部的全零页没有 HMAC，解密会失败。现在只放过文件尾部连续的零页，零页之后再出现内容算损坏；数据库头记的页数对不上时由 `quick_check` 拒绝
- `local_id` 和时间顺序不一致时，增量游标会漏行（`verify-export` 发现的）
- 图片缓存的文件名不是消息 XML 里的 md5，而是 `packed_info_data` 里的；带时间戳的缩略图 `<md5>_<时间>_t.dat` 也要认成缩略图
- 拍一拍（应用消息 62）归为系统消息
- 搜索下拉框刚创建时标题还没设置，逐键检查偶尔把它当成意外窗口。现在意外窗口要持续 1 秒才算数；查询途中窗口消失也不会让进程退出
- 没有选中会话时标题是空白，这样的模板什么都能匹配：现在拒绝保存，比较时也不算匹配
- 搜不到的会话（文件传输助手）支持 `--calibrate --current`；当前打开的已经是目标时不再搜索
- 失败时收掉搜索下拉框，不按 Esc（主窗口收到 Esc 会被收起来）

### 新增（对接已有环境）

- `instance attach`：对接已经在跑、已经登录的容器，只读挂载，不启停它
- `export --container`、`export.runner`：导出跑在工具容器里，无网络，内存有上限，适合宿主没有依赖的系统
- `verify-export`：拿解密后的数据库核对条数，只输出计数
- `./install.sh --tools`：只构建工具镜像
- 导出边读边写，不再把消息全留在内存里

### 文档

- `docs/INTEGRATION.md`：下游怎么接，文里的 Python 片段和状态表由测试核对

### 没有带过来的

总结、日报、网站、QQ（都在 chat-digest）。

### 已知的限制

- 导出已在真实账号上核对过条数；`wechat-send` 只在真实账号上往已打开的文件传输助手发送过，经搜索选中会话的路径重构后还没有在真实群上跑。详见 [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)
- 语音和视频只导出记录，不导出文件
- 群昵称是尽力解析
