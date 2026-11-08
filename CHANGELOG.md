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

### 没有带过来的

总结、日报、网站、QQ（都在 chat-digest）。

### 已知的限制

- **导出和 `wechat-send` 的新代码只在合成数据和假微信上测过，还没有连真实账号跑。** 拆分前的代码在 4.1.13.26 上长期运行；详见 [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)
- 语音和视频只导出记录，不导出文件
- 群昵称是尽力解析
