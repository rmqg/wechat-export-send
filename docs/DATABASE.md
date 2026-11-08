# 数据库和解析规则

这些是对微信 4.x 本地格式的观察，不是腾讯的公开接口，客户端升级时可能变化。程序认不出时会拒绝或标记，不会猜。

## 目录

```
xwechat_files/<账号目录>/
  db_storage/
    contact/contact.db         联系人、群
    session/session.db         会话列表
    message/message_N.db       消息分片
    message/message_resource.db 媒体索引（可能认不出，就退回消息 XML 里的 md5）
  msg/attach/<md5(会话账号)>/<年-月>/Img/<md5>[_t|_h|_W].dat   图片缓存
  msg/file/<年-月>/<文件名>     已下载的文件
all_users/config/global_config{,.crc}   算图片密钥用的本地配置
```

路径里的目录名是账号标识，不要贴到公开的地方。

## SQLCipher 4 页格式

- 页长 4096 字节，每页末尾保留 80 字节：16 字节 IV + 64 字节 HMAC-SHA512
- 第一页开头 16 字节是盐，解密后要补上 `SQLite format 3\0` 头
- AES-256-CBC
- MAC 密钥：盐的每个字节异或 `0x3A` 作为盐，用 enc key 做 PBKDF2-HMAC-SHA512，2 轮，32 字节
- HMAC 覆盖：密文 + IV + 小端的 4 字节页号（从 1 开始）
- enc key：32 字节 passphrase 配这个库的盐，PBKDF2-HMAC-SHA512，256000 轮，32 字节。每个库的盐不同，所以 key 也不同

每页都先验 HMAC，认证失败、错误密钥、截断页一律报错，不输出半截数据库。

**预分配的零页**：客户端会把消息库预分配到 30 MB，尾部的全零页还没用过，没有 HMAC。程序只放过文件尾部连续的零页；零页之后又出现内容算损坏。有人把用过的页抹零时，数据库头记的页数对不上，`quick_check` 会拒绝。

## WAL

客户端可能把最近的提交留在 `-wal` 里，只解主库会漏最新的消息。`patch_wal` 校验 WAL 头和帧的校验和、页认证，只把**完整提交事务**的帧应用到解密副本，未提交的尾帧不用。主库和 WAL 必须来自同一时刻：程序复制前后比较 (大小, mtime, inode)，变了就重来，最多三次。

## 手工解一个库

```sh
umask 077
python3 -m wechat_export_send.sqlcipher \
  instances/main/gui/xwechat_files/<账号目录>/db_storage/message/message_0.db \
  /一个私有目录/message_0.db \
  --keys instances/main/keys/all_keys.json --rel message/message_0.db
```

（在安装目录下运行，或者设 `PYTHONPATH`。）

`keys/all_keys.json` 的格式：

```json
{"_db_dir": "/config/...", "message/message_0.db": {"enc_key": "十六进制", "salt": "十六进制"}}
```

## 消息表

每个会话一张表 `Msg_<md5(会话账号)>`，可能分布在多个分片里。用到的列：

| 列 | 含义 |
|---|---|
| `local_id` | 表内自增 |
| `server_id` | 服务器消息 ID；刚发、还没到服务器时是 0 |
| `local_type` | 低 32 位是基础类型，高 32 位是应用消息子类型 |
| `real_sender_id` | `Name2Id` 表的 rowid |
| `create_time` | Unix 秒 |
| `origin_source` | 1 表示自己发的 |
| `source` | XML，含 `atuserlist` |
| `message_content` | 正文或 XML；可能压缩 |
| `compress_content` | 压缩的备用正文 |
| `WCDB_CT_message_content` | 压缩类型 |

缺列时导出报 `unsupported message schema` 并停止。

`local_id` 自增，但和 `create_time` 的顺序不一定一致（同步历史时会逆序）。不要假设按时间排序后 `local_id` 也递增。

压缩：有 zstd 魔数或类型标记就用 zstd，否则依次试 zlib、gzip、raw deflate，都不是就当明文。

## 类型映射

| `local_type` 低 32 位 | 子类型 | `type` | 说明 |
|---|---|---|---|
| 1 | | `text` | |
| 3 | | `image` | 不写占位文字 |
| 34 | | `voice` | `media.status` 是 `missing` |
| 43 | | `video` | 同上 |
| 47 | | `sticker` | 同上 |
| 49 | XML `type` 57 | `quote` | `reply_to` 来自 `refermsg` |
| 49 | 19 | `forward` | `forward` 来自 `recorditem` |
| 49 | 6 | `file` | |
| 49 | 8 | `sticker` | |
| 49 | 1、3、4、5、7、33、36、51、53、63，或者有 url | `link` | |
| 49 | 其他 | `other` | 有标题就写进 `text` |
| 10000 | | `system` | 去掉 XML 标签 |
| 10002，或 `sysmsg type="revokemsg"` | | `revoke` | |
| 其他 ≥10000 | | `system` | |
| 其他 | | `other` | |

`raw_type` 保留原始类型，形如 `49:57`。

丢弃的内容：XML 里 `aeskey`、`fullmd5`、`md5`、`filekey`、`ticket`、`token` 等字段；域名含 `cdn`、`qpic` 或参数含 `aeskey`、`ticket`、`token`、`key` 的链接。

## 发送者

群消息正文可能以 `账号:\n` 开头，优先用它；否则查 `Name2Id`。`sender_name` 按这个顺序取：群昵称（`chat_room.ext_buffer`，尽力解析，字段 1 是成员，其中 1=账号、2=群昵称；这是观察到的格式）→ 联系人备注 → 昵称。

## 图片

1. 缓存文件名用的 md5 在消息行的 `packed_info_data` 里（protobuf 字段 3 的字段 4），**不是** XML 里的 `md5`。XML 的 `<img md5= hdlength= length= cdnbigimgurl= cdnmidimgurl=>` 用来校验是不是原图。
2. 在 `msg/attach/<md5(会话)>/<年-月>/Img/` 下按文件名找 `<md5>.dat`、`_h`、`_W`（全尺寸）和 `_t`、`_<时间>_t`（缩略图）。
3. `.dat` 有三种：
   - `07 08 V2 08 07` 开头：V2，15 字节头（4 字节 AES 段长度、4 字节 XOR 段长度），前段 AES-ECB，中段明文，尾段按字节异或
   - `07 08 V1 08 07` 开头：同样的版式，用固定公开密钥
   - 其他：整个文件按字节异或，密钥由文件头反推
4. 解出来必须是完整的图片（Pillow 能读完）才算数。
5. 长度和 md5 都和消息对上是 `source_quality: original`，否则 `client_full`。
6. 只有缩略图时 `status` 是 `pending`（消息描述里有 CDN 地址）或 `missing`。开了 `thumbnails` 才额外收一项，并标 `is_thumbnail: true`。

V2 的 AES 和 XOR 密钥：从 `all_users/config/global_config` 解出账号和 uin，AES 是 `md5(uin + 账号)` 的前 16 个十六进制字符，XOR 是 uin 的最低非零字节。账号对不上时报错。算不出来时可以用 `keys image-key` 从内存里找，并用几张本地图片校验。

微信自有的 `wxgf`（HEVC）容器按原样保存，扩展名 `.wxgf`，`mime` 是 `application/x-wxgf`，本项目不解码。

## 数据库 key 的捕获

- **gdb（默认）**：在客户端的 WCDB 配置函数上设硬件断点，登录时读到 32 字节候选值，逐库做 HMAC 校验。不改客户端指令；不支持硬件断点时明确失败，不退回软件断点。捕获点由 `vendor/wcdb_key_tool` 从 ELF 里的锚点字符串算出来。
- **native**：`vendor/wxextract` 用 ptrace，需要由它重新启动客户端。失败时程序从只读文件重启客户端。

两种都在最后逐库验证，至少要有消息库通过才写文件。不能跨平台照搬：Windows 和 macOS 版微信不在支持范围内。
