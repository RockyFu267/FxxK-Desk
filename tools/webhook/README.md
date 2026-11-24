# 群机器人消息发送工具

直接调用机器人 webhook，不需要打开企业微信、监控面板或模型，也不占用鼠标键盘。

在项目根目录执行：

```powershell
# 自定义消息，执行一次发送一次。
python .\tools\webhook\send_message.py '这是一条测试消息，无需处理'

# 不传消息，运行后输入。
python .\tools\webhook\send_message.py

# 从 UTF-8 文件读取，支持多行。
python .\tools\webhook\send_message.py --file .\message.txt

# 只预览，不发送，适合先检查中文。
python .\tools\webhook\send_message.py --preview '中文编码测试'

# 按需指定本机代理。
python .\tools\webhook\send_message.py --proxy http://127.0.0.1:1099 '代理发送测试'
```

也可以双击 `send.cmd`，输入一行消息后回车发送，结果会保留在窗口里。该入口自动使用项目虚拟环境，不需要启用 PowerShell 脚本。

包含双引号或复杂多行内容时，建议使用交互输入或 UTF-8 文本文件，避免旧版 PowerShell 的命令行引号处理改变消息。

尚未激活虚拟环境时，可以使用完整解释器路径。无需设置 `PYTHONPATH`：

```powershell
.\.venv\Scripts\python .\tools\webhook\send_message.py '自定义消息内容'
.\.venv\Scripts\python .\tools\webhook\send_message.py --file .\message.txt
```

配置优先读取工具旁的 `webhook.local.json`，格式如下：

```json
{"webhook_url": "你的完整机器人地址"}
```

该私密文件已被项目 Git 忽略规则排除，真实地址不写进代码或说明文档。若没有此文件，读取环境变量 `SOURCE_WEBHOOK_URL`，再读取项目根目录 `.env` 中的同名项。默认沿用系统/环境代理，`--proxy` 可显式指定。

发送内容使用 UTF-8，本工具限制为 2048 字节；中文按实际 UTF-8 字节计数。成功须收到 `errcode=0`。网络中断时不会自动重试，避免同一条消息重复发送，需先查看群确认。
