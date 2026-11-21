# 本地开发

刷屏、滚动补采、队列与并发设计见[刷屏补采方案](backfill-design.md)。持续模式中模型总结已移入独立线程；桌面采集与发送仍由主线程串行操作。

人工使用鼠标键盘时的暂停、恢复和发送中断策略见[人工接管方案](human-takeover.md)。默认空闲 5 秒且用户回到目标应用前台后，重新定位和补采，不抢其他应用焦点。

Windows、Python 3.11+。在项目目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\fxxk-desk windows
.\.venv\Scripts\fxxk-desk focus --title "目标窗口标题"
.\.venv\Scripts\fxxk-desk launch --exe "D:\Apps\Example\Example.exe"
.\.venv\Scripts\fxxk-desk observe --title "目标窗口标题"
```

`launch` 仅报告启动请求，使用 `windows` 和 `focus` 核对实际窗口。标题必须唯一；完全匹配优先，其次子串匹配。

通过项目 `.env` 或环境变量设置 `OPENAI_BASE_URL`、`OPENAI_API_KEY`、`VISION_MODEL`，值参考被 Git 忽略的本地网关说明。存在 `.env` 时，项目文件中的同名配置优先于机器级环境变量，以免误用其他服务的全局 Key。不要把凭据写进源码。配置完成后：

```powershell
.\.venv\Scripts\fxxk-desk observe --title "目标窗口标题" --analyze
```

仅 `--analyze` 会将截图发送到配置的网关。内网请求绕过系统代理，90 秒超时，不自动重试。截图默认存入被忽略的 `artifacts/`，不自动上传 GitHub。

当前观察框架提供 Win32 窗口发现、前台切换、Pillow 可见屏幕截图、模型描述。截图最长边缩至 1024，保留原始窗口坐标和图片尺寸。可见屏幕截图可能包含遮挡窗口；使用前移开弹窗，保持桌面解锁。暂不支持后台截图、通用自动点击规划或持续监控；模型输出只供阅读，不直接执行。

## 配置驱动的群聊导航

```powershell
.\.venv\Scripts\python -m pip install -e '.[automation]'
# 将 config.example.toml 复制为 config.local.toml，填写本机路径、窗口标题和群名。
.\.venv\Scripts\fxxk-desk open example --config config.local.toml
```

配置中每个 `[targets.名称]` 定义一个软件和目标群。更换目标使用不同配置项；本机配置 `*.local.toml` 不进入 Git。OCR 模型首次使用可能需要下载。

当前 `desktop_chat` 适配流程：核对程序路径和窗口 → 启动/恢复窗口 → Ctrl+F → 核对焦点是左上方搜索编辑框 → 填入群名 → 本机 OCR 查找唯一精确结果并点击 → 核对右侧顶部群标题。若目标已经打开则不再搜索。不会发送消息。

此适配器是实验实现，尚未完成真实客户端端到端验收。要求目标客户端支持 Ctrl+F 并暴露搜索框 UIA Edit；搜索框不可访问、同名结果、OCR 低置信度、窗口移动、焦点丢失或标题验证失败时停止。当前搜索结果和群标题的区域规则依赖常见左右分栏布局，需要用实际界面校准。仅同名标题不能区分同名群；配置唯一名称并确认结果。登录和权限弹窗由用户手动处理。

## 标准聊天流程

```powershell
# 获取当前可见消息，保存到 SQLite；首次运行只建立基线。
fxxk-desk chat collect ops
# 查看本地已采集记录，不需要模型配置或桌面连接。
fxxk-desk chat history ops
# 再采集并为新增的完整入站消息生成一条草稿，不发送。
fxxk-desk chat draft ops
# 核对输出的草稿后，使用其 id 发送。
fxxk-desk chat send ops --reply-id 1
# 持续观察和生成草稿；Ctrl+C 停止。
fxxk-desk chat watch ops --interval 5
# 只有目标配置 auto_reply=true 时，才允许以下自动发送模式。
fxxk-desk chat watch ops --interval 5 --send
```

流程：核对应用和群标题 → 截取配置中的消息区域 → 内网视觉模型结构化提取 → SQLite 存储和相邻观察去重 → 文本模型生成回复 → 本机 OCR 校验输入内容 → 点击发送 → 检查输入框清空及消息区域出现正文。优先直接点击侧栏中唯一的目标名称，找不到才调用搜索流程。

`message_rect`、`editor_rect`、`send_point` 均为可见窗口内物理像素，示例配置对应 1300×1160 的已观察布局。大小不匹配会停止，布局或 DPI 改变后必须重新校准；成员面板开关也可能改变布局。窗口截图使用 Pillow 的窗口模式，绕过已观察到的整屏截图失败。

记录包含观察时间、原始截图引用、方向、可见正文及识别来源；视觉模型输出标记为未验证，不把观察时间当消息发送时间。当前仅采集可见区域，不滚动，不代表完整聊天历史，也不能恢复不可见或错过的消息。按有序重叠识别新增消息；无重叠时视为基线/缺口，不自动回复。完全相同的消息序列或识别波动仍可能产生漏采或重复记录，这是实验实现的限制。

自动回复只处理完整入站消息，不处理自己的出站消息；每轮只回复最新合格消息，较旧消息作为已处理上下文。首次启动不回复历史消息。自动发送须同时设置目标 `auto_reply=true` 并传 `--send`，默认关闭。正文最多 500 字、单行；可通过 `--instruction` 定义回复规则。聊天内容作为不可信数据送入模型，不执行其中的指令。

发送箱状态为 `draft → sending → sent/unknown`。进程崩溃后的 `sending` 以及 `unknown` 不会自动重发，需人工查看群记录。`sent` 表示本机观察到了发送后的界面证据，不保证服务端投递或对方已读。任何输入/验证异常都会停止监控。请只运行一个桌面工作流，运行期间避免手动切换群或控制鼠标键盘。

这些标准流程已经过模拟后端测试；独立 Python 执行器的真实群聊提取、模型回复与自动发送闭环尚未验收。此前成功的点击与测试消息发送来自交互工具，不能等同于独立程序已验收。不会在开发测试时自行开启自动回复。

## 单向群消息转述

在本地配置中定义来源目标、接收目标以及 `[relays.路由名]`。转述场景不在来源群回复：每条新出现的完整可见消息均独立排队，包括来源群里的出站消息。发送者由气泡上方可见名称识别，未显示时写“发送者未识别”，不猜测。

```powershell
# 只观察、生成转述草稿，不发送。
fxxk-desk relay once example_relay
# 配置路由 auto_send=true 后，持续单向转述；Ctrl+C 停止。
fxxk-desk relay watch example_relay --send --interval 5
# 建立来源群观察基线，再经 SOURCE_WEBHOOK_URL 注入一条模拟告警并生成草稿。
# 此命令会实际向 Webhook 所属群发测试消息；不自动重试。
fxxk-desk relay simulate example_relay
```

Webhook 只能用于向它所属的群发消息，不能读取聊天记录，也不能直接发到另一个群。本程序通过来源群截图读取实际到达的消息，经接收群的桌面界面发送转述。Webhook 值放在被忽略的 `.env` 中的 `SOURCE_WEBHOOK_URL`，不写进公开配置。

消息采集走本机 OCR，并按当前浅色布局的白色/灰色入站气泡、蓝色出站气泡分组。它不依赖内网视觉模型。摘要优先调用配置的文本模型；模型不可用时，已知模拟告警格式生成明确标注的规则摘要，其余消息退化为标注“原文摘录”的有限摘录，不假装完成了 AI 总结。改变客户端主题、布局、DPI、消息卡片颜色或富文本形式，需要重新校准，当前实现不能保证捕获所有类型或不可见消息。

转述格式为 `【来源：来源群】【发送者：可见名称】摘要`。数据库保存来源观察、原始截图、来源消息编号和独立转述状态；按顺序转述，每条消息单独去重。首次运行只建基线，不批量转发旧消息；观察断档时不猜测哪些是新消息，可能漏转，不能承诺“任何消息”零遗漏。长消息或被裁切消息需要人工核对。

发生不确定发送时，发送任务会停止；不能直接重发。人工查看目标群确认未发送后，才能显式恢复，并记录恢复审计：

```powershell
fxxk-desk relay recover example_relay --relay-id 1 --confirm-not-sent
```

恢复只将该条记录设为待生成摘要，不发送消息。确认后可另行执行 `relay once example_relay --send`。测试时指定不同 `--database` 可以隔离测试状态，后续命令必须使用同一个数据库。

2026-11-21 实机验证：独立 Python 程序已完整跑通 Webhook 注入模拟告警 → 本机 OCR 识别发送者和正文 → 文本模型总结 → 切换接收群并输入 → 校验后发送 → 确认消息出现在接收群并记录 sent。只发送了一条转述，测试编号保留在消息中；最终截图保存在本地 `artifacts/relay-success.png`。单次闭环成功，不代表已完成持续运行或所有消息类型的验收。

实测修复了两个问题：机器级 Key 覆盖项目 Key 导致的 401，以及远程会话的常亮光标被 OCR 识别为数字导致空输入框误判。输入框空白检查结合像素与光标位置，真实草稿仍会阻止覆盖。当前依赖使用支持本机 Python 3.13 的 RapidOCR 3.x；其结构化输出接口见[官方使用说明](https://rapidai.github.io/RapidOCRDocs/main/en/install_usage/rapidocr/usage/)。

测试：

```powershell
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests -v
```
