# AstrBot 姓名文件查找器

这是一个可直接放入 `data/plugins/astrbot_plugin_name_searcher` 的 AstrBot 插件。它会捕获当前消息和转发消息节点中的文件、图片，在本地保存并建立来源索引，然后提供姓名查找、OCR、图片转表格和可视化管理面板。

## 已实现功能

1. 递归读取当前聊天消息以及 Forward、Node 等转发节点中的图片、文档和常见附件字段（`url`、`path`、`file`、`resource`、`data`）。
2. 文件下载到插件数据目录，按 SHA-256 去重，索引记录发起人、消息编号、统一消息来源和捕获时间。
3. `/查找 姓名` 支持人员库精确/模糊查找，并从已捕获文件的 OCR 文本中查找。
4. `/图片转表格 [文件编号]` 将图片 OCR 为 CSV；不传编号时使用最新图片。
5. WebUI 提供文件、图片预览、下载、搜索、状态筛选、全选、多选删除、一键清理和下载失败记录清理。
6. OCR/下载失败或图片置信度低于阈值时，自动通知发起人；可选启用 LLM 审核，由 AstrBot 模型提供商自动校正识别结果。
7. 查找输出逐条附带来源文件、文件编号、发起人、消息和捕获时间；可配置为合并转发聊天记录，适配器支持时会在结果节点中附带命中的原图片或文件资源。
8. `_conf_schema.json` 提供图形化配置，其中 `clear_downloaded_files` 是保存配置后执行一次的清理开关。
9. 默认仅在指定聊天中启用；可在图形化配置中填写允许的群聊 ID 和私聊用户 ID，范围外不会捕获附件，也不会响应插件命令。
10. 图片识别支持 EXIF 方向校正、小图放大、自动对比度、锐化、二值化和多 PSM 版面识别，并记录清晰度与 OCR 置信度。
11. 可搜索解析 DOCX/DOCM、XLSX/XLSM/XLS/XLSB、CSV/TSV、PPTX、PDF、扫描 PDF、ODT/ODS/ODP、EPUB、RTF 及常见文本格式；其他文件仍会捕获并保存在面板中。
12. 所有成功下载的文件都会生成视觉审阅页；可在 Plugin Page 中多选多个 `provider::model`，逐页审核图片、PDF、DOCX、表格等文件，并可启用多模型共识输出最终文本。视觉服务不可用时自动保留本地解析结果。
13. 多文件捕获默认按 30 秒时间窗口聚合，窗口内收到的附件统一处理；任务进度会实时显示收集、下载、本地解析、视觉审核、共识和完成阶段，并同步到 Plugin Page 与 `/捕获进度`。
14. 可开启“仅捕获管理员发送的文件”，并在 Plugin Page 或 AstrBot 插件配置中填写管理员用户 ID；非管理员发送的附件会被忽略，命令仍按聊天范围配置正常工作。
15. 文本、CSV 等文件会自动探测 UTF-8、GB18030、Big5 和 UTF-16，并尝试修复常见乱码；可选择向查询发起人汇总通告发生转码的具体文件名与源编码。
16. 单个 XLSX/XLSM/XLS/XLSB/ODS/FODS 文件会遍历全部工作表，保留空单元格列位置、工作表名称和公式后备文本，避免名单分散在多个 Sheet 时漏查。
17. 姓名查找会联合检索本地解析、每个视觉模型原始输出、多模型共识及 LLM 审核前后文本；即使共识或审核遗漏了某个人，也可从保留的视觉证据中命中，并显示具体匹配依据。
18. 自动展开 ZIP、7Z、RAR、TAR、TGZ、GZ、BZ2、XZ 等压缩包，将内部图片、文档、表格及嵌套压缩包成员作为独立文件识别和查找；压缩包本体及包内来源路径会一并保留。
19. Plugin Page 提供本地批量上传，可一次选择多张图片、文档、表格或压缩包，并复用聊天附件的完整解析、视觉审核与姓名索引流程。

## 安装

将本目录完整复制到 AstrBot 的插件目录（通常是 `data/plugins/astrbot_plugin_name_searcher`），在 AstrBot 面板启用插件。建议安装 `requirements.txt` 中的可选依赖：

`requirements.txt` 包含图片、PDF、Office、OpenDocument 和 WebUI 所需的可选解析库，AstrBot 导入插件时应一并安装。

图片识别默认先尝试随 Python 安装的 RapidOCR ONNX，再与可用的 Tesseract 多阶段结果比较。启用视觉模型后优先使用 AstrBot 模型结果；LLM 审核可进一步校正姓名、班级和专业。Tesseract 属于可选增强；两个 OCR 引擎都不可用时，插件仍可以下载、管理文本/表格文件。

## 人员库

在配置中填写 `people_file`，支持 JSON、CSV、TSV、XLSX。JSON 可以是数组，也可以是 `{ "people": [...] }`；姓名列可以叫 `姓名` 或 `name`，其他列会作为附加信息输出。仓库内的 `people.example.json` 可作为模板。

## 命令

```text
/查找 张三
/图片转表格
/图片转表格 1750000000-ab12cd34
/文件面板
/捕获进度
```

所有命令回复均不生成 Markdown。`merged_forward_results` 关闭时，查找结果使用普通文本；开启时，Bot 会将查询摘要和全部匹配结果作为合并转发聊天记录发送，消息适配器不支持该组件时回退为普通文本。查找结果中的“来源”是可追溯引用，不代表人员信息一定来自配置人员库；启用 LLM 审核时会显示模型审核状态。

## WebUI

插件按 AstrBot 官方 Plugin Pages 协议提供 `pages/name-searcher/index.html`，页面通过 `window.AstrBotPluginPage` bridge 调用已认证的后端 API；同时保留旧版 ASGI WebUI 注册或挂载接口，默认兼容路径为 `/api/name-searcher/`。`/文件面板` 会返回可打开的管理地址和最新捕获进度。静态页面和接口约定见 [`webui/API.md`](webui/API.md)。核心接口同时提供短路径和 `/api/files` 别名：

```text
GET  /files
GET  /progress
GET  /progress/{id}
GET  /files/{id}/content
POST /files/bulk-delete   {"ids": ["..."]}
POST /files/upload        {"files": [{"name": "名单.zip", "mime": "application/zip", "data_base64": "..."}]}
POST /files/clear
POST /files/clear-failed
GET  /config
PATCH /config
```

插件不会开放本地绝对路径，删除接口也受 `allow_panel_delete` 配置保护。旧版直连接口（非 AstrBot 插件页）必须在插件配置中设置 `panel_access_token` 才会开放，访问时附带 `?token=令牌` 或 `Authorization: Bearer 令牌`；未设置时一律返回 403。`storage_dir`、`people_file`、`panel_mount_path`、`panel_public_url`、`panel_access_token` 只能在 AstrBot 插件配置中修改，面板提交这些键会被忽略，令牌也不会通过配置接口返回。Plugin Page 上传接口只执行校验和持久化，随后立即返回 `status: queued`；压缩包展开、OCR 和视觉模型审核在后台继续进行，避免长时间模型调用导致反向代理 502。插件重载后，打开面板、查询进度或收到新消息时会自动恢复未完成的上传任务。恢复使用逐文件检查点：已经完成识别的文件和压缩包成员会被跳过，从下一个未完成文件继续，不会从零重复识别。`GET /files` 只返回面板所需的摘要字段，完整模型结果仍保存在索引中供姓名查找使用。

## 配置重点

`storage_dir` 默认为 `files`，位于 AstrBot 的 `data/plugin_data/astrbot_plugin_name_searcher/` 下（1.7.1 起；AstrBot 更新插件时会删除插件目录，数据放在外面才不会丢）。旧默认值 `data/name_searcher/files` 自动改用新位置，旧目录里的数据不会自动搬迁。`people_file` 的相对路径也以该目录为基准。`ocr_preprocess` 控制图片增强，`ocr_psm_modes` 控制 Tesseract 版面尝试，`pdf_ocr_max_pages` 限制扫描 PDF 的 OCR 页数。`review_threshold` 控制低置信度图片审核提醒，`request_review_on_low_confidence` 可关闭主动提醒；`cleanup_on_start` 每次启动清空文件，`clear_downloaded_files` 保存后只执行一次清理并自动复位。

`restrict_chat_scope` 默认开启，请先填写 `allowed_group_ids` 和 `allowed_private_ids`。多个 ID 可用逗号、分号、空格或换行分隔；`*` 表示允许该类型的所有聊天。开启限制后，空的允许列表会禁用对应聊天类型。群聊会优先读取适配器提供的群号、频道 ID 或群会话 ID；私聊使用发送者用户 ID。视觉模型配置中的 `vision_provider` 和 LLM 审核配置中的 `llm_review_provider` 均应填写 AstrBot 聊天模型提供商 ID；留空时按当前会话选择，面板会提供可选提供商和模型。

`admin_only_capture` 开启后，插件只捕获 `admin_ids` 中用户发送的附件。多个管理员 ID 可用逗号、分号、空格或换行分隔；管理员列表为空时不会捕获任何文件。此限制只作用于文件捕获，不会额外限制 `/查找` 等命令。

`capture_window_enabled` 控制时间窗口捕获，`capture_interval_seconds` 默认 30 秒。`vision_model_targets` 可写多行 `provider::model`；`vision_review_max_pages` 限制每份文件送审页数，`vision_consensus_enabled` 控制多个模型结果的共识审核。

`notify_encoding_issues` 控制乱码文件通告。开启时，一次捕获批次中检测到的非 UTF-8 或乱码文件会汇总发送给查询发起人，列明文件名、源编码和修复方式；关闭仅停止通告，自动转码与识别仍会继续。

`archive_extraction_enabled` 控制压缩包内容捕获；`archive_max_depth`、`archive_max_members` 和 `archive_max_total_size_mb` 分别限制嵌套层级、成员数与累计解压大小。插件会拒绝绝对路径、`..` 路径穿越和符号链接，并在读取成员前检查声明大小。7Z 依赖 `py7zr`；RAR 依赖 `rarfile` 及系统可用的 UnRAR/bsdtar 后端，运行环境不具备时压缩包本体仍会保存并显示错误摘要。

`merged_forward_results` 控制 `/查找` 的输出样式。开启后，每个匹配结果会显示为一条合并转发聊天记录，并尽量携带对应文件或图片；目前 QQ/OneBot 等支持 `Nodes` 消息组件的适配器体验最完整。

查找视觉模型输出时会兼容 Markdown 包裹、HTML 实体、零宽字符、全半角差异、姓名间标点和 `\\uXXXX` 转义文本。上述处理只生成搜索键，面板及索引中仍保留模型原始输出，便于核对。

## 本地识别 + 导入识别包（1.7.0 起）

服务器配置低（例如 1 核 2GB 还跑着 NapCat）时，大批量文件不要直接让服务器识别。改为：

1. 在自己的电脑上准备 Python 3.10+，下载 `NameSearcher-local` 工具包并安装依赖：`pip install -r requirements-local.txt`
2. 双击 `本地识别GUI.bat` 打开图形界面，选文件夹点“识别并打包”（有进度条、停止、状态查看，缺依赖可一键安装）；或命令行运行 `python name_searcher_local.py run D:\要识别的文件夹`（也可以把文件夹拖到 `本地识别.bat` 上）。
   - 用的是和插件完全相同的识别代码（RapidOCR、PDF/Office/表格解析、压缩包展开）。
   - 可以随时 Ctrl+C 中断；重跑同一命令会跳过已识别的文件。
3. 结束后生成 `bundle-时间.zip`。打开插件页，点“导入识别包”选择它。
   - 识别包按 2MB 分片上传，服务器逐个文件校验 sha256 后写入，索引只写一次，不做 OCR。
   - 重复文件不会重复导入；服务器上没识别完的同一文件会直接用识别包里的结果补全。
   - 很大的识别包也可以用 SFTP 放进存储目录下的 `import_inbox/`，再点“扫描导入目录”。导入完成的包会移到 `import_inbox/done/`。
4. 来源筛选里选“本地识别导入”可以只看这些文件。

本地工具不会调用 AstrBot 的视觉模型和 LLM 审核（那些依赖 AstrBot 的模型提供商）。

服务器端同时加了三层保护：
- RapidOCR 只用 1 个线程；
- 可用内存低于 `min_free_memory_mb`（默认 200MB）时，在两个文件之间暂停等待；
- 插件页上传的文件如果连续两次在识别途中被中断（例如服务器崩溃重启），第三次启动时直接标记为失败并跳过，不再反复拖垮服务器。

## 文件列表筛选与折叠（1.6.2 起）

- 筛选结果超过 5 个时只显示前 5 个，底部“展开其余 N 个文件 / 收起”切换；改变筛选条件会重新折叠。
- 按类型筛选：图片、文档、表格、演示文稿、压缩文件、其他。
- 按来源筛选：聊天捕获、插件页上传、最近一次捕获（即进度卡里“已捕获内容”的那一批）。
- 按状态筛选：已识别、待识别 / 识别中、下载或识别失败。
- “全选”作用于全部筛选结果，包括折叠起来的文件。

## 插件页（1.6.1 起）

Plugin Page 改为 Hark pro 风格（深色标题卡、圆角卡片、胶囊按钮、iOS 风格开关，自动跟随系统深浅色），配置按“聊天范围与权限 / 识别与审核 / 捕获与回复 / 压缩包”分组。重复或无效的控件已合并或移除：

- 视觉模型：原“提供商 + 模型名 + 多模型目标”三处合并为一个多选列表，旧的单模型设置会自动并入。
- LLM 审核：原“开关 + 提供商 + 模型”合并为一个下拉框（关闭 / 当前会话模型 / 具体模型）。
- 批量捕获：原“开关 + 时长”合并为一个时长输入，填 0 即关闭。
- 移除配置区的“清理已下载文件”（与列表上方“一键清理”重复）、无效的“文件保留天数”以及两个只读状态格。
- AstrBot 插件配置中隐藏 `vision_provider`、`vision_model`、`capture_window_enabled`、`clear_downloaded_files`（仍兼容旧值），`panel_access_token` 以密码框显示。

## 姓名匹配规则（1.6.0 起）

- 人员库：姓名完全一致优先；只有在没有完全一致结果时，才显示长度相同、相似度不低于 `name_match_threshold`（默认 0.85）的近似姓名，并标注“近似姓名”。三字姓名错一个字（王小明/王小红）不再命中。
- 已捕获文件：姓名需作为完整词出现（前后不是其他汉字，或前面是“姓名”等标签），“张三”不会再命中“张三丰”。只有找不到完整姓名时才显示包含匹配，并提示可能是其他人；有完整结果时会注明另有几条包含匹配未显示。
- `review_threshold` 只用于 OCR 低置信度提醒，不再影响姓名匹配。

## 兼容性说明

插件按 AstrBot 官方插件、事件和消息组件约定实现：

- https://docs.astrbot.app/dev/star/plugin-new.html
- https://docs.astrbot.app/dev/star/guides/listen-message-event.html
- https://docs.astrbot.app/dev/star/guides/plugin-pages.html
- https://docs.astrbot.app/dev/star/guides/ai.html

消息适配器对转发节点和附件字段的结构可能不同，插件采用字典/对象递归探测。QQ/OneBot 合并转发会通过 `get_forward_msg` 展开节点，NapCat 转发文件只有 `file_id` 时会继续调用群聊或私聊文件 URL 接口。平台最终仍未提供本地路径或可下载 URL 时，面板会保留一条无法下载记录并通知发起人。
