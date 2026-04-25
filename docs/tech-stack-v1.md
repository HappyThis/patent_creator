# 技术栈规范 v1

## 文档定位

本文档定义本项目 v1 阶段的实现技术栈。

它建立在以下文档之上：

- [Agent 基本设计原则 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles-v1.md)
- [API 设计规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/api-design-v1.md)
- [工作区初始化规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/workspace-init-v1.md)

本文档用于明确：

- 前端技术栈
- 后端技术栈
- 运行时与包管理
- 持久化策略
- 不采用的基础设施

## 一、总体原则

v1 是单机工具型项目。

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
3. v1 不额外引入其他 Python 包管理方案。

## 五、持久化与状态

v1 采用文件系统持久化。

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

v1 不引入数据库。

原因：

1. 当前是单机工具型项目。
2. 没有用户概念。
3. 没有多租户需求。
4. 当前核心真相源已经由文件系统表达清楚。

## 七、网络与外部读取

后端允许按需执行网络命令和外部文件读取。

规则：

1. 用户提供的 `url` 可在当前回合按需抓取。
2. 用户提供的 `file_path` 可在当前回合按需读取。
3. 外部引用不作为项目资源持久化。

## 八、当前结论

v1 技术栈固定为：

- 前端：`TypeScript + React + Vite`
- 后端：`Python 3.11 + FastAPI + Uvicorn`
- 包管理：`uv`
- 持久化：本地文件系统 + `git`
- 数据库：不使用
