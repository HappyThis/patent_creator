# 技术栈规范

## 文档定位

本文档定义本项目的实现技术栈。

它建立在以下文档之上：

- [Agent 基本设计原则](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles.md)
- [API 设计规范](/Users/yangchaoqun/myProj/patent_creator/docs/api-design.md)
- [工作区初始化规范](/Users/yangchaoqun/myProj/patent_creator/docs/workspace-init.md)

本文档用于明确：

- 前端技术栈
- 后端技术栈
- 运行时与包管理
- 持久化策略
- 不采用的基础设施

## 一、总体原则

本项目是单机工具型项目。

因此技术栈遵循以下原则：

1. 优先使用本地文件系统，不引入数据库。
2. 前后端分离，但都保持轻量。
3. 持久化对象以文档文件和日志文件为主。
4. 不引入用户系统、鉴权系统和多租户设计。

## 二、前端技术栈

前端栈固定为：

- `TypeScript`
- `React`
- `Vite`

说明：

- `TypeScript` 作为前端实现语言。
- `React` 用于三栏交互界面实现。
- `Vite` 用于本地开发与构建。

## 三、后端技术栈

后端栈固定为：

- `Python 3.11`
- `FastAPI`
- `Uvicorn`

说明：

- `Python 3.11` 作为后端运行时版本。
- `FastAPI` 负责 HTTP API 和 SSE 接口。
- `Uvicorn` 作为 ASGI 运行服务。

## 四、包管理

项目统一使用：

- `uv`

约定：

1. Python 依赖通过 `uv` 管理。
2. Python 运行、安装和锁定流程围绕 `uv` 组织。
3. 不额外引入其他 Python 包管理方案。

## 五、持久化与状态

系统采用文件系统持久化。

核心对象：

- `disclosure.json`
- `project.json`
- `sessions/*.jsonl`
- `exports/`
- `assets/`

说明：

- 文档真相源是 `disclosure.json`。
- 会话过程记录是 `jsonl` 格式的 session log。
- 版本历史通过 `git` 管理。

## 六、数据库策略

系统不引入数据库。

原因：

1. 系统是单机工具型项目。
2. 没有用户概念。
3. 没有多租户需求。
4. 核心真相源由文件系统表达清楚。

## 七、网络与外部读取

后端不单独设计面向前端暴露的外部引用输入字段。

agent 在回合内只接收用户的自然语言 `message`，并自行从文本中提取可用信息。

规则：

1. 用户输入的有效信息统一包含在 `message` 中。
2. 是否执行额外信息阅读，属于 agent 内部能力，而不是独立 API 契约。
3. 外部引用信息即便被读取，也不作为项目资源持久化。

## 八、设计结论

技术栈固定为：

- 前端：`TypeScript + React + Vite`
- 后端：`Python 3.11 + FastAPI + Uvicorn`
- 包管理：`uv`
- 持久化：本地文件系统 + `git`
- 数据库：不使用
