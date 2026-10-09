# Name Searcher 管理面板接口

`webui/index.html` 是无构建依赖的静态页面。AstrBot 4.5.7 及以上使用 `pages/name-searcher/index.html`，页面通过 `window.AstrBotPluginPage` bridge 调用同源、已认证的插件 API；旧版插件仍可将页面挂载到 `/api/name-searcher`，页面默认从该路径发起请求，也可以通过 `?api=/your/prefix` 或设置 `window.NAME_SEARCHER_API` 覆盖前缀。

新版 AstrBot 扫描插件目录中的 `pages/<page_name>/index.html`，本插件页面为 `pages/name-searcher/index.html`；`plugin-page/index.html` 仅作为旧版兼容静态副本。

所有 JSON 请求和响应使用 UTF-8。删除接口应校验当前 AstrBot 管理员会话，路径中的文件 ID 必须经过 URL 解码后再使用。下载/预览 URL 应只允许指向插件下载目录中的文件，避免把本地路径直接暴露给浏览器。

## `GET /files`

返回已经捕获并下载的文件。响应可以直接是数组，也可以是 `{ "files": [...] }`（页面也兼容 `items`、`data`）。推荐字段：

```json
{
  "files": [
    {
      "id": "capture-20260913-001",
      "name": "名单.xlsx",
      "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "kind": "spreadsheet",
      "format": "xlsx",
      "size": 18240,
      "url": "/api/name-searcher/files/capture-20260913-001/content",
      "source_name": "群聊：软件工程 2 班",
      "captured_at": "2026-09-13T12:30:00+08:00",
      "download_status": "ok",
      "extraction_status": "extracted",
      "ocr_confidence": 0.94,
      "encoding_issue": false,
      "source_encoding": null,
      "sheet_count": 2,
      "sheet_names": ["一班", "二班"],
      "ocr_excerpt": "姓名 | 班级 | 专业"
    }
  ]
}
```

`url`（或 `preview_url`、`download_url`、`content_url`）用于图片预览和下载。没有 URL 时，页面仍会显示文件信息，但不会显示预览/下载按钮。列表包含成功下载和下载失败的全部捕获记录。图片会显示缩略图，PDF 会尝试内嵌预览；DOCX、XLSX、PPTX 等显示格式、识别状态和可搜索的文本摘要。文本发生自动转码时，`encoding_issue` 为 `true` 且 `source_encoding` 给出探测编码；表格文件通过 `sheet_count` 和 `sheet_names` 返回工作表数量与名称。

## `POST /files/bulk-delete`

批量删除下载的文件。请求体：

```json
{ "ids": ["capture-20260913-001", "capture-20260913-002"] }
```

服务端应返回 `200`（可带 `{ "deleted": 2 }`）或 `204`。当其中部分文件已经不存在时，建议按幂等方式返回成功并报告实际删除数量。

## `POST /files/clear`

一键清理插件下载目录。页面传递 `{ "scope": "downloads" }`，服务端应仅删除已下载的本地副本，不修改聊天原始消息或插件配置。建议响应 `{ "deleted": 12 }`。

## `POST /files/clear-failed`

仅删除 `download_status` 为 `unavailable` 或 `failed` 的捕获记录，不影响已成功下载的文件。响应包含删除 ID 和数量，例如 `{ "deleted": ["capture-id"], "count": 1 }`。

## `POST /files/upload`

Plugin Page 本地批量上传接口。为兼容 AstrBot Page bridge，请求使用 JSON/Base64：

```json
{
  "files": [
    {
      "name": "名单.zip",
      "mime": "application/zip",
      "data_base64": "UEsDB..."
    }
  ]
}
```

后端按 `max_file_size_mb` 校验每个上传文件，最多接受 100 个顶层文件。接口只完成校验和持久化，然后立即返回 `status: "queued"`、`count`、`records`、`errors` 和 `progress_id`；压缩包展开、OCR、视觉审核和索引在后台运行，页面通过 `GET /progress` 和 `GET /files` 轮询状态。插件重载时，未完成记录保持 `processing_status: "queued"`，并会在面板或消息入口重新触发处理。

`GET /files` 只返回列表及预览所需的摘要字段，不返回 `vision_results`、`vision_consensus_text`、`llm_review_text` 或 `ocr_text_local` 等大段模型原文；这些内容仍保存在服务端索引中供姓名查找使用。每个上传文件及压缩包成员都会持久化识别状态，插件崩溃或重启后会跳过 `completed` 记录，从下一个 `queued/running` 文件恢复。

## `GET /config`

返回管理面板需要展示的配置。页面识别以下字段（缺省时显示“未提供”）：

```json
{
  "retention_days": 30,
  "ocr_enabled": true,
  "review_enabled": true,
  "restrict_chat_scope": true,
  "allowed_group_ids": "123456, 987654",
  "allowed_private_ids": "10001, 10002",
  "admin_only_capture": true,
  "admin_ids": "10001, 10002",
  "merged_forward_results": true,
  "vision_enabled": true,
  "vision_model_targets": "provider-a::vision-a\nprovider-b::vision-b",
  "vision_review_max_pages": 4,
  "vision_consensus_enabled": true,
  "capture_window_enabled": true,
  "capture_interval_seconds": 30,
  "notify_encoding_issues": true,
  "archive_extraction_enabled": true,
  "archive_max_depth": 3,
  "archive_max_members": 300,
  "archive_max_total_size_mb": 200
}
```

也兼容 `file_retention_days`、`image_to_table_enabled`、`ask_sender_on_unknown` 字段。`retention_days: null` 表示不自动清理。

## `GET /progress`

返回最近的捕获任务和已完成文件。`percent` 是 0 到 100 的整数，`stage` 会标识 `collecting`、`downloading`、`extracting`、`vision_review`、`consensus` 或 `completed`，并额外返回 `stage_percent`、`current_file`、`current_model`、`window_ends_at`；也可以请求 `/progress/{id}` 查询指定任务。

```json
{
  "progress": {
    "id": "capture-1750000000-ab12cd34",
    "status": "running",
    "total": 4,
    "completed": 2,
    "percent": 50,
    "records": [{"id": "...", "name": "名单.xlsx", "kind": "spreadsheet"}]
  }
}
```

## `PATCH /config`（也可实现 `PUT`）

保存配置。页面会提交文件保留天数、聊天范围限制、管理员捕获、合并转发、视觉模型多选、视觉共识、时间窗口和乱码文件通告配置。`vision_model_targets` 是换行分隔的 `provider::model` 字符串，后端也兼容字符串数组；`notify_encoding_issues` 只控制是否向发起人通告，关闭后仍会自动转码。服务端可以只更新传入字段并返回完整配置。

## `GET /files/{id}/content`（推荐）

以文件原始 MIME 类型返回内容，并设置合适的 `Content-Disposition`。该路径可作为 `/files` 中 `url` 的值。若文件需要鉴权，使用 AstrBot 面板的同源会话，不要在 URL 中放置长期 token。

## 与后端路由的兼容约定

页面依赖上述管理接口；批量删除也兼容旧版的 `/files/delete` 路径。插件可以将路径映射到现有命名（例如 `/captures`、`/downloaded`），然后在 AstrBot 的 WebUI 注册函数中做一层转发。接口失败时页面会显示错误，不会静默清空服务端文件。

## 鉴权（1.6.0）

旧版直连接口除 `GET /` 与 `GET /index.html` 外都需要访问令牌：在插件配置中设置 `panel_access_token`，请求时附带 `Authorization: Bearer <令牌>`、`X-NameSearcher-Token: <令牌>` 或查询参数 `?token=<令牌>`。未配置令牌返回 403，令牌错误返回 401。`GET /config` 不再返回令牌，改为 `panel_access_token_set`；`PATCH /config` 会忽略锁定键并在 `ignored_keys` 中列出。


## 识别包导入（1.7.0，Plugin Page 接口）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/import/chunk` | `{upload_id, offset, data_base64}`，按顺序追加分片（单片 ≤ 8MB）。`offset` 与已收到的大小不一致时返回 409 和 `received`。 |
| POST | `/import/finish` | `{upload_id, name}`，导入已上传的识别包，返回 `{created, updated, exists, skipped, errors, total}`。 |
| POST | `/import/scan` | 导入存储目录 `import_inbox/*.zip`，完成后移到 `import_inbox/done/`。 |

识别包是 zip：`manifest.json`（`format: "name-searcher-bundle"`, `version: 1`, `records: [...]`，每条记录带 `bundle_file` 与 `sha256`）+ `files/` 下的原文件。
