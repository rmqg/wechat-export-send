# 安全策略

这个工具处理能解开聊天数据的秘密。**不要**在公开的 issue、PR、日志或截图里提交数据库、passphrase、`all_keys.json`、登录态、二维码、真实的导出内容或头像。

发现安全问题时，优先用 GitHub 的私密漏洞报告（Security → Report a vulnerability），或发送邮件至 `ruomuqiuguang@icloud.com`。报告里写清受影响的版本和平台、入口、风险，用虚构的数据复现。

凭据已经泄露时：重新登录并重新捕获，废弃旧密钥；从 git 里删掉文件不能消除历史里的泄漏。

设计上的安全边界见 [docs/SECURITY.md](docs/SECURITY.md)。本项目不是腾讯官方软件，不保证客户端兼容或账号零风险，只用于你自己或明确授权的账号。
