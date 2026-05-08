# Docs 索引

## 文档清单

`docs` 目录包含 14 份核心设计/状态文档：

1. [专利交底书结构方案](/Users/yangchaoqun/myProj/patent_creator/docs/patent-disclosure-structure.md)
2. [Agent 基本设计原则](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles.md)
3. [Agent Prompt 与上下文规范](/Users/yangchaoqun/myProj/patent_creator/docs/agent-prompt-context-spec.md)
4. [上下文管理规范](/Users/yangchaoqun/myProj/patent_creator/docs/context-management.md)
5. [子 Agent 定义](/Users/yangchaoqun/myProj/patent_creator/docs/subagents.md)
6. [Tools 设计](/Users/yangchaoqun/myProj/patent_creator/docs/tools.md)
7. [Session 事件日志 Schema](/Users/yangchaoqun/myProj/patent_creator/docs/session-log.md)
8. [工作区初始化规范](/Users/yangchaoqun/myProj/patent_creator/docs/workspace-init.md)
9. [技术栈规范](/Users/yangchaoqun/myProj/patent_creator/docs/tech-stack.md)
10. [API 设计规范](/Users/yangchaoqun/myProj/patent_creator/docs/api-design.md)
11. [render_ast Schema](/Users/yangchaoqun/myProj/patent_creator/docs/render-ast-schema.md)
12. [前端交互规范](/Users/yangchaoqun/myProj/patent_creator/docs/frontend-interaction.md)
13. [一轮内部时序](/Users/yangchaoqun/myProj/patent_creator/docs/round-lifecycle.md)
14. [后端实现概览](/Users/yangchaoqun/myProj/patent_creator/docs/backend-implementation-status.md)

## 是否需要合并

核心文档保持当前拆分粒度。

原因：

1. `专利交底书结构方案` 是文档 schema，本身就是独立主题
2. `Agent 基本设计原则` 是系统架构与职责边界，也应独立
3. `Agent Prompt 与上下文规范` 同时覆盖上下文装配、prompt 分层与 prefix cache
4. `上下文管理规范` 负责定义 session 恢复、上下文窗口、cursor、压缩和兜底裁剪策略
5. `子 Agent 定义` 负责收敛业务子 agent 清单与能力边界
6. `Tools 设计` 负责收敛工具集合与设计边界
7. `Session 事件日志 Schema` 负责收敛会话过程记录格式
8. `工作区初始化规范` 负责定义内部项目工作区的初始化方式
9. `技术栈规范` 负责定义前后端实现栈、运行时和包管理
10. `API 设计规范` 负责定义前端与后端之间的接口协议
11. `render_ast Schema` 负责定义后端生成、前端消费的统一展示模型
12. `前端交互规范` 负责定义三栏页面的具体交互方式
13. `一轮内部时序` 负责定义一次用户请求在系统内部如何运行和收束

它们虽然相关，但关注点不同；合并为一份总文档会降低可维护性。

## 文档拆分策略

文档拆分策略如下：

- 保留 `专利交底书结构方案` 独立
- 保留 `Agent 基本设计原则` 独立
- 将 `上下文说明` 与 `prompt 模板原则` 合并为一份 `Agent Prompt 与上下文规范`
- 保留 `上下文管理规范` 独立
- 保留 `子 Agent 定义` 独立
- 保留 `Tools 设计` 独立
- 保留 `Session 事件日志 Schema` 独立
- 保留 `工作区初始化规范` 独立
- 保留 `技术栈规范` 独立
- 保留 `API 设计规范` 独立
- 保留 `render_ast Schema` 独立
- 保留 `前端交互规范` 独立
- 保留 `一轮内部时序` 独立

这些核心文档保持独立。

## 文档依赖关系

阅读顺序如下：

1. [专利交底书结构方案](/Users/yangchaoqun/myProj/patent_creator/docs/patent-disclosure-structure.md)
2. [Agent 基本设计原则](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles.md)
3. [Agent Prompt 与上下文规范](/Users/yangchaoqun/myProj/patent_creator/docs/agent-prompt-context-spec.md)
4. [上下文管理规范](/Users/yangchaoqun/myProj/patent_creator/docs/context-management.md)
5. [子 Agent 定义](/Users/yangchaoqun/myProj/patent_creator/docs/subagents.md)
6. [Tools 设计](/Users/yangchaoqun/myProj/patent_creator/docs/tools.md)
7. [Session 事件日志 Schema](/Users/yangchaoqun/myProj/patent_creator/docs/session-log.md)
8. [工作区初始化规范](/Users/yangchaoqun/myProj/patent_creator/docs/workspace-init.md)
9. [技术栈规范](/Users/yangchaoqun/myProj/patent_creator/docs/tech-stack.md)
10. [API 设计规范](/Users/yangchaoqun/myProj/patent_creator/docs/api-design.md)
11. [render_ast Schema](/Users/yangchaoqun/myProj/patent_creator/docs/render-ast-schema.md)
12. [前端交互规范](/Users/yangchaoqun/myProj/patent_creator/docs/frontend-interaction.md)
13. [一轮内部时序](/Users/yangchaoqun/myProj/patent_creator/docs/round-lifecycle.md)

依赖关系可以理解为：

- `专利交底书结构方案`
  - 最底层基础文档
- `Agent 基本设计原则`
  - 依赖交底书结构边界
- `Agent Prompt 与上下文规范`
  - 依赖交底书结构和 agent 基本原则
- `上下文管理规范`
  - 依赖 agent 基本原则、prompt/context 规范、session 日志规范和一轮内部时序
- `子 Agent 定义`
  - 依赖 agent 基本原则、prompt/context 规范和上下文管理规范
- `Tools 设计`
  - 依赖 agent 基本原则、prompt/context 规范和子 agent 定义
- `Session 事件日志 Schema`
  - 依赖 agent 基本原则、prompt/context 规范、子 agent 定义和 tools 设计
- `工作区初始化规范`
  - 依赖交底书结构、agent 基本原则、tools 设计和 session 日志规范
- `技术栈规范`
  - 依赖 agent 基本原则、API 设计规范和工作区初始化规范
- `API 设计规范`
  - 依赖交底书结构、agent 基本原则、tools 设计、session 日志规范和工作区初始化规范
- `render_ast Schema`
  - 依赖交底书结构和 API 设计规范
- `前端交互规范`
  - 依赖 API 设计规范和 render_ast Schema
- `一轮内部时序`
  - 依赖 agent 基本原则、tools 设计、session 日志规范、API 设计规范和前端交互规范

## 产品形态

产品形态按 Web 前端来理解，而不是 CLI 产品。

前端采用三栏结构：

1. `目录区域`
2. `渲染区`
3. `Agent Chat 区`

其中：

- `目录区域` 负责展示交底书目录并支持定位章节
- `渲染区` 负责实时预览当前交底书
- `Agent Chat 区` 负责与主 agent 对话并驱动文档修改

前端不提供正文手动编辑器。

也就是说：

- 正文修改主要通过 agent 驱动完成
- 用户如果需要手动编辑，可先导出 Markdown，再在系统外自行编辑

## 核心实现口径

文档体系采用以下统一口径：

1. 交底书文档定位使用 `section_id` 和 `block_id`
2. 文档读取统一通过 `document_read`
3. 文档写入统一通过 `document_edit`
4. 子 agent 只提出 proposal，主 agent 决定是否采纳
5. 子 agent 通过 `execute_subagent` 调度工具启动
6. 文档变更事件使用 `changed_section_ids` 和 `changed_block_ids`
7. `render_ast` 使用 id 体系支撑前端定位、高亮和滚动

## 文档维护规则

新增文档统一遵循两条规则：

1. 每份文档开头增加“文档定位”
2. 每份文档明确列出“依赖文档”和“相关文档”

这样文档数量增加时仍能保持结构清晰。
