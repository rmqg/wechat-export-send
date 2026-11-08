# 参与贡献

欢迎提 issue 和 PR。

## 不能破坏的约定

1. **源数据只读。** 不修改、不 checkpoint 客户端的数据库。
2. **发送语义。** 凡是可能已经点过发送的情况，一律 `unknown`，不许报 `failed`，不许自动重发。改动发送流程时要跑 `tests/test_send_host.py` 和 `tests/test_send_x11.py`。
3. **目标明确。** 不猜会话，不加默认发送目标。不要用放宽校验的办法「修好」报错。
4. **媒体质量如实。** 缩略图不许冒充原图，占位文字不许写进正文。
5. **密钥认证。** 每页验 HMAC；不许跳过验证。
6. **接口不随便改。** `docs/CONTRACT.md` 的修改要同时改 chat-digest，并升 schema 版本。

## 许可证

只提交原创代码，或有明确再分发许可的代码；保留版权声明，写进 `THIRD_PARTY_NOTICES.md`（来源 URL、固定 commit、许可证）。公开的仓库不等于有授权，没有许可证的上游一律不收，也不要参考。贡献按 MIT 提交。

## 测试数据

只用合成数据，公开的测试密钥要标明。**不要**提交真实的数据库、成员列表、头像、截图或日志。

## 开发

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
python3 scripts/audit-release.py
```

`tests/test_send_x11.py` 用真的 Xvfb 和窗口管理器，配一个假微信（`tests/fake_weixin.py`）和合成加密库，走完整的发送流程。需要 `xvfb`、`fluxbox`、`python3-tk`、`libxtst6`，缺了会跳过。tkinter 不在当前 Python 里时用 `FAKE_PYTHON=/usr/bin/python3` 指定。

改了合成样本的生成逻辑后运行 `python3 scripts/generate-fixtures.py`。

## PR 里请写明

改了什么、行为有什么变化、实际跑了哪些验证、哪些平台没有测过。不要把别人的说法或没跑过的东西写成测试通过。
