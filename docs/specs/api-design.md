# API Design

## 文档定位

本文档概述当前后端 API 和事件形态。系统采用单主 agent 架构，SSE 事件只展示主流程工具调用和文档变更。

## 主要资源

- Project：一个交底书工作区。
- Session：一次持续对话。
- Round：一次用户消息触发的 agent 执行。
- Disclosure：交底书结构化文档。

## 核心接口

- `POST /api/projects` 创建项目。
- `GET /api/projects` 列出项目。
- `GET /api/projects/{project_id}/outline` 获取目录。
- `GET /api/projects/{project_id}/document` 获取结构化文档。
- `GET /api/projects/{project_id}/render` 获取渲染 AST。
- `POST /api/projects/{project_id}/chat/messages` 发送消息并通过 SSE 返回 round 事件。
- `GET /api/projects/{project_id}/sessions` 列出会话。
- `GET /api/projects/{project_id}/sessions/{session_id}/events` 读取 session log。
- `POST /api/projects/{project_id}/export/markdown` 导出 Markdown。

## SSE 事件

常见事件：

- `round_started`
- `assistant_delta`
- `tool_call_started`
- `tool_call_finished`
- `document_changed`
- `context_compression_started`
- `context_compression_completed`
- `context_compression_failed`
- `round_finished`
- `round_failed`

工具事件只代表主 agent 调用工具，不再表示子 agent 启动或结束。

## 工具调用事件示例

```json
{
  "event": "tool_call_started",
  "data": {
    "scope": "main",
    "tool": "disclosure_read_section",
    "summary": "开始读取交底书章节",
    "round_id": "round_001",
    "message_id": "msg_001"
  }
}
```

```json
{
  "event": "tool_call_finished",
  "data": {
    "scope": "main",
    "tool": "disclosure_read_section",
    "summary": "交底书章节已读取",
    "result": {
      "status": "success",
      "output": {}
    }
  }
}
```

## 文档变更

`disclosure_edit` 成功后，服务层广播 `document_changed`，并在 round 结束时提交工作区 git commit。
