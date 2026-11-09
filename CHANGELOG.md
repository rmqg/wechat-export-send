# 更新日志

## 未发布

- 导出头像：`export.avatars`（默认开）从客户端本地的 `head_image.db` 取头像，写进 `media/`，填 `members/*.json` 的 `avatar` 和 manifest 里会话的可选字段 `avatar`（群头像）。不联网；客户端缓存被淘汰或读库失败时沿用上次导出的路径。之前 `avatar` 一直没有写，下游报告因此没有头像。

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

### 新手可用性（本地 tag 前移，版本号不变）

- `install.sh` 改成向导：先检查发行版、内核、CPU、Python 和 venv、Docker 和 Compose、内存、磁盘，缺什么就打印这台机器上该运行的命令；装完依赖后在终端里问「只跑演示」还是「接真实账号」，接真实账号就替你建实例、写 `export.include`（600）、构建镜像、装客户端、启动桌面，最后自检并打印「下一步」。新增 `--config <答案文件>`、`--non-interactive`；`--no-deps`、`--images`、`--tools`、`--link` 的行为不变。
- 新命令 `wechat-export-send setup`：同一个向导，可以反复运行，做完的步骤自动跳过；`setup --check-only` 只检查环境。
- 实例比客户端先建时，向导装完客户端会把客户端路径和哈希补进 `instance.env`。
- 新文档 `docs/QUICKSTART.md`：从全新服务器到 demo、再到第一份真实导出，每一步写明应该看到什么，扫码和点网页的地方配示意图（`scripts/make-quickstart-figures.py` 生成，合成，不含真实账号）；README 开头标明难度。
- `doctor` 的每个失败和警告都带可以直接复制的修复命令（按 Ubuntu / 其他发行版给出 Docker 和 Compose 的装法），并检查实例目录的属主。
- 命令出意外时打印中文说明和下一步，不再只给英文异常栈（`WES_DEBUG=1` 看完整信息）。
- `docs/TROUBLESHOOTING.md` 收录真实部署时踩过的坑：`LANG=zh_CN` 时窗口标题变化、Debian 12 的 Python 3.11.2 让 `client install` 崩溃、keys 目录的属主和权限、Esc 收起主窗口、小内存设备。

### 已知的限制

- 导出已在真实账号上核对过条数；`wechat-send` 只在真实账号上往已打开的文件传输助手发送过，经搜索选中会话的路径重构后还没有在真实群上跑。详见 [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)
- 语音和视频只导出记录，不导出文件
- 群昵称是尽力解析
