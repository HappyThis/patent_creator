# API 设计规范 v1

## 文档定位

本文档定义前端 `React` 与后端 `FastAPI` 之间的 V1 API 协议。

本规范重点覆盖：

1. 项目初始化
2. 目录读取
3. 文档渲染
4. Agent Chat
5. Session 日志读取
6. Markdown 导出

相关文档：

- [专利交底书结构方案 v1](/Users/yangchaoqun/myProj/patent_creator/docs/patent-disclosure-structure-v1.md)
- [Agent 基本设计原则 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles-v1.md)
- [Tools 设计 v1](/Users/yangchaoqun/myProj/patent_creator/docs/tools-v1.md)
- [Session 事件日志 Schema v1](/Users/yangchaoqun/myProj/patent_creator/docs/session-log-v1.md)
- [工作区初始化规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/workspace-init-v1.md)

## 目标

V1 的 API 设计围绕三栏前端展开：

1. `目录区域`
2. `渲染区`
3. `Agent Chat 区`

因此 API 只围绕以下核心能力设计：

- 新建项目
- 获取目录
- 获取渲染数据
- 发送 chat 消息
- 接收流式 chat 结果
- 导出 Markdown

## 一、基本原则

### 1. 项目是前端核心资源

前端以 `project` 作为主要资源对象。

每个 `project` 在系统内部对应一个独立工作区。

### 2. 后端维护真相源

后端维护：

- `disclosure.json`
- session 事件日志
- 工作区目录

前端不直接理解底层工作区结构。

### 3. 渲染区消费 render_ast

渲染区消费的是后端生成的 `render_ast`。

说明：

- `Markdown` 主要用于导出
- 前端渲染主要基于结构化展示模型

### 4. Chat 过程采用 SSE

普通请求用 HTTP。

chat 过程中的持续输出、状态变化和文档更新通知，统一通过 `SSE` 传递。

## 二、核心资源模型

一个 `project` 对外主要暴露以下对象：

- 基本信息
- 交底书目录
- 当前渲染结果
- chat 会话
- session 日志
- Markdown 导出结果

## 三、项目接口

### 1. `POST /api/projects`

创建一个新的交底书项目，并自动初始化内部工作区。

请求：

```json
{
  "title": "一种图像检测方法"
}
```

响应：

```json
{
  "project_id": "proj_001",
  "title": "一种图像检测方法",
  "created_at": "2026-04-23T21:00:00+08:00"
}
```

### 2. `GET /api/projects/{project_id}`

获取项目基础信息。

响应：

```json
{
  "project_id": "proj_001",
  "title": "一种图像检测方法",
  "created_at": "2026-04-23T21:00:00+08:00",
  "updated_at": "2026-04-23T21:10:00+08:00"
}
```

## 四、目录接口

### `GET /api/projects/{project_id}/outline`

返回当前交底书目录树。

该接口直接服务于左侧目录区域。

响应：

```json
{
  "sections": [
    {
      "id": "technical_field",
      "title": "技术领域",
      "children": []
    },
    {
      "id": "technical_solution",
      "title": "技术方案",
      "children": [
        {
          "id": "processing_flow",
          "title": "处理流程"
        }
      ]
    }
  ]
}
```

## 五、渲染接口

### `GET /api/projects/{project_id}/render`

返回当前交底书的 `render_ast`。

这个接口直接服务于渲染区。

可选查询参数：

- `focus_section_id`
- `focus_pointer`

说明：

- 不传时，返回整篇渲染数据
- 传入时，返回整篇渲染数据，并附带当前聚焦位置

响应：

```json
{
  "render_ast": {
    "type": "document",
    "title": "一种图像检测方法",
    "children": [
      {
        "type": "section",
        "id": "technical_solution",
        "title": "技术方案",
        "level": 2,
        "anchor": "technical_solution",
        "children": [
          {
            "type": "paragraph",
            "text": "本发明提供一种图像检测方法。"
          }
        ]
      }
    ]
  },
  "active_section_id": "technical_solution",
  "updated_at": "2026-04-23T21:15:00+08:00"
}
```

## 六、原始文档接口

### `GET /api/projects/{project_id}/document`

返回当前 `disclosure.json`。

该接口主要用于调试、排查和内部管理，不作为前端渲染主依赖。

响应：

```json
{
  "meta": {
    "document_type": "patent_disclosure",
    "schema_version": "v1",
    "title": "一种图像检测方法"
  },
  "sections": []
}
```

## 七、Chat 接口

### 1. `POST /api/projects/{project_id}/chat/messages`

发送一条用户消息。

请求：

```json
{
  "session_id": "sess_001",
  "message": "请把技术方案这一章写得更具体一点。",
  "references": [
    {
      "type": "text",
      "content": "现有方案主要包括特征提取和检测模块。"
    },
    {
      "type": "url",
      "content": "https://example.com/ref"
    },
    {
      "type": "file_path",
      "content": "/absolute/path/to/file.txt"
    }
  ],
  "active_section_id": "technical_solution"
}
```

字段说明：

- `session_id`：当前 chat 会话标识
- `message`：用户本轮输入
- `references`：可选的参考资料
- `active_section_id`：可选，用于表示当前焦点章节

说明：

- 参考资料通过 chat 附带进入系统
- 不单独设计“材料管理区”或“材料上传区”API

响应：

```json
{
  "accepted": true,
  "session_id": "sess_001",
  "message_id": "msg_001"
}
```

### 2. `GET /api/projects/{project_id}/chat/stream?session_id=sess_001`

建立 SSE 流，接收主 agent 的流式过程输出。

建议事件类型如下：

#### `agent_output`

```text
event: agent_output
data: {"text":"我先补全技术方案章节中的处理流程。"}
```

#### `tool_call_started`

```text
event: tool_call_started
data: {
  "call_id": "call_001",
  "scope": "main",
  "tool": "execute_subagent",
  "summary": "已启动 section_writer"
}
```

#### `tool_call_finished`

```text
event: tool_call_finished
data: {
  "call_id": "call_001",
  "scope": "main",
  "tool": "execute_subagent",
  "summary": "section_writer 已完成",
  "result": {
    "status": "success",
    "output": "我已完成技术方案章节的补写。"
  }
}
```

#### `document_changed`

```text
event: document_changed
data: {
  "changed": true,
  "changed_pointers": ["/sections/6"],
  "active_section_id": "technical_solution"
}
```

#### `round_finished`

```text
event: round_finished
data: {
  "reply": "我已经补全了技术方案章节，重点补充了处理流程。",
  "changed": true,
  "changed_pointers": ["/sections/6"],
  "active_section_id": "technical_solution",
  "committed": true
}
```

前端处理建议：

- 收到 `tool_call_started` 后，在 Chat 区展示进行中的执行节点
- 收到 `tool_call_finished` 后，将执行节点切换为已完成，并默认折叠结果详情
- 收到 `document_changed` 后，刷新目录区与渲染区
- 收到 `round_finished` 后，更新 chat 区回合状态

## 八、Session 日志接口

### `GET /api/projects/{project_id}/sessions/{session_id}/events`

返回指定 session 的事件日志。

该接口主要用于：

- 调试
- 排查
- 回放

响应：

```json
{
  "events": [
    {
      "id": "evt_001",
      "type": "user_input",
      "seq": 1,
      "scope": "main",
      "payload": {
        "text": "请补写技术方案。"
      }
    }
  ]
}
```

## 九、导出接口

### `POST /api/projects/{project_id}/export/markdown`

导出当前交底书为 Markdown。

响应：

```json
{
  "path": "/absolute/path/to/export.md"
}
```

说明：

- 当前阶段 Markdown 主要作为导出格式
- 用户如果需要手动编辑，可导出后在系统外自行处理

## 十、前端实际依赖的最小接口集

V1 前端最小依赖如下接口：

1. `POST /api/projects`
2. `GET /api/projects/{project_id}`
3. `GET /api/projects/{project_id}/outline`
4. `GET /api/projects/{project_id}/render`
5. `POST /api/projects/{project_id}/chat/messages`
6. `GET /api/projects/{project_id}/chat/stream`
7. `POST /api/projects/{project_id}/export/markdown`

以下接口可作为调试或增强接口：

- `GET /api/projects/{project_id}/document`
- `GET /api/projects/{project_id}/sessions/{session_id}/events`

## 十一、一轮交互的最小链路

### 1. 用户发送消息

前端调用：

- `POST /api/projects/{project_id}/chat/messages`

### 2. 前端监听流式结果

前端建立：

- `GET /api/projects/{project_id}/chat/stream`

### 3. 文档发生修改

服务端推送：

- `document_changed`

前端收到后刷新：

- `outline`
- `render`

### 4. 回合结束

服务端推送：

- `round_finished`

前端更新：

- chat 区回合状态
- 最近修改章节定位

## 十二、当前结论

V1 API 设计采用如下原则：

1. 前端以 `project` 为核心资源
2. 渲染区消费 `render_ast`
3. Markdown 只作为导出格式
4. chat 过程采用 SSE
5. 参考资料通过 chat 附带，不单独做材料管理 API
6. 目录区、渲染区、chat 区分别由独立接口支撑
