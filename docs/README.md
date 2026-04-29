# Docs 索引

## 当前文档

当前 `docs` 目录包含 14 份核心设计/状态文档：

1. [专利交底书结构方案 v1](/Users/yangchaoqun/myProj/patent_creator/docs/patent-disclosure-structure-v1.md)
2. [Agent 基本设计原则 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles-v1.md)
3. [Agent Prompt 与上下文规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-prompt-context-spec-v1.md)
4. [上下文管理规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/context-management-v1.md)
5. [子 Agent 定义 v1](/Users/yangchaoqun/myProj/patent_creator/docs/subagents-v1.md)
6. [Tools 设计 v1](/Users/yangchaoqun/myProj/patent_creator/docs/tools-v1.md)
7. [Session 事件日志 Schema v1](/Users/yangchaoqun/myProj/patent_creator/docs/session-log-v1.md)
8. [工作区初始化规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/workspace-init-v1.md)
9. [技术栈规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/tech-stack-v1.md)
10. [API 设计规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/api-design-v1.md)
11. [render_ast Schema v1](/Users/yangchaoqun/myProj/patent_creator/docs/render-ast-schema-v1.md)
12. [前端交互规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/frontend-interaction-v1.md)
13. [一轮内部时序 v1](/Users/yangchaoqun/myProj/patent_creator/docs/round-lifecycle-v1.md)
14. [后端实现状态 v1](/Users/yangchaoqun/myProj/patent_creator/docs/backend-implementation-status-v1.md)

## 是否需要合并

当前阶段不建议把所有文档继续拆得更细。

原因：

1. `专利交底书结构方案` 是文档 schema，本身就是独立主题
2. `Agent 基本设计原则` 是系统架构与职责边界，也应独立
3. `Agent Prompt 与上下文规范` 同时覆盖上下文装配、prompt 分层与 prefix cache
4. `上下文管理规范 v1` 负责定义 session 恢复、上下文窗口、cursor、压缩和兜底裁剪策略
5. `子 Agent 定义 v1` 负责收敛业务子 agent 清单与能力边界
6. `Tools 设计 v1` 负责收敛工具集合与当前取舍
7. `Session 事件日志 Schema v1` 负责收敛会话过程记录格式
8. `工作区初始化规范 v1` 负责定义内部项目工作区的初始化方式
9. `技术栈规范 v1` 负责定义前后端实现栈、运行时和包管理
10. `API 设计规范 v1` 负责定义前端与后端之间的接口协议
11. `render_ast Schema v1` 负责定义后端生成、前端消费的统一展示模型
12. `前端交互规范 v1` 负责定义三栏页面的具体交互方式
13. `一轮内部时序 v1` 负责定义一次用户请求在系统内部如何运行和收束

它们虽然相关，但关注点仍然不同。全部继续合并成一份总文档后，后续修改会变得笨重。

## 建议的合并策略

当前建议是：

- 保留 `专利交底书结构方案 v1` 独立
- 保留 `Agent 基本设计原则 v1` 独立
- 将 `上下文说明` 与 `prompt 模板原则` 合并为一份 `Agent Prompt 与上下文规范 v1`
- 保留 `上下文管理规范 v1` 独立
- 保留 `子 Agent 定义 v1` 独立
- 保留 `Tools 设计 v1` 独立
- 保留 `Session 事件日志 Schema v1` 独立
- 保留 `工作区初始化规范 v1` 独立
- 保留 `技术栈规范 v1` 独立
- 保留 `API 设计规范 v1` 独立
- 保留 `render_ast Schema v1` 独立
- 保留 `前端交互规范 v1` 独立
- 保留 `一轮内部时序 v1` 独立

当前阶段保持这些核心文档独立更利于迭代。

## 文档依赖关系

建议按下面顺序阅读：

1. [专利交底书结构方案 v1](/Users/yangchaoqun/myProj/patent_creator/docs/patent-disclosure-structure-v1.md)
2. [Agent 基本设计原则 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles-v1.md)
3. [Agent Prompt 与上下文规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-prompt-context-spec-v1.md)
4. [上下文管理规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/context-management-v1.md)
5. [子 Agent 定义 v1](/Users/yangchaoqun/myProj/patent_creator/docs/subagents-v1.md)
6. [Tools 设计 v1](/Users/yangchaoqun/myProj/patent_creator/docs/tools-v1.md)
7. [Session 事件日志 Schema v1](/Users/yangchaoqun/myProj/patent_creator/docs/session-log-v1.md)
8. [工作区初始化规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/workspace-init-v1.md)
9. [技术栈规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/tech-stack-v1.md)
10. [API 设计规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/api-design-v1.md)
11. [render_ast Schema v1](/Users/yangchaoqun/myProj/patent_creator/docs/render-ast-schema-v1.md)
12. [前端交互规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/frontend-interaction-v1.md)
13. [一轮内部时序 v1](/Users/yangchaoqun/myProj/patent_creator/docs/round-lifecycle-v1.md)

依赖关系可以理解为：

- `专利交底书结构方案 v1`
  - 最底层基础文档
- `Agent 基本设计原则 v1`
  - 依赖交底书结构边界
- `Agent Prompt 与上下文规范 v1`
  - 依赖交底书结构和 agent 基本原则
- `上下文管理规范 v1`
  - 依赖 agent 基本原则、prompt/context 规范、session 日志规范和一轮内部时序
- `子 Agent 定义 v1`
  - 依赖 agent 基本原则、prompt/context 规范和上下文管理规范
- `Tools 设计 v1`
  - 依赖 agent 基本原则、prompt/context 规范和子 agent 定义
- `Session 事件日志 Schema v1`
  - 依赖 agent 基本原则、prompt/context 规范、子 agent 定义和 tools 设计
- `工作区初始化规范 v1`
  - 依赖交底书结构、agent 基本原则、tools 设计和 session 日志规范
- `技术栈规范 v1`
  - 依赖 agent 基本原则、API 设计规范和工作区初始化规范
- `API 设计规范 v1`
  - 依赖交底书结构、agent 基本原则、tools 设计、session 日志规范和工作区初始化规范
- `render_ast Schema v1`
  - 依赖交底书结构和 API 设计规范
- `前端交互规范 v1`
  - 依赖 API 设计规范和 render_ast Schema
- `一轮内部时序 v1`
  - 依赖 agent 基本原则、tools 设计、session 日志规范、API 设计规范和前端交互规范

## 当前产品形态

当前阶段，产品形态按 Web 前端来理解，而不是 CLI 产品。

前端采用三栏结构：

1. `目录区域`
2. `渲染区`
3. `Agent Chat 区`

其中：

- `目录区域` 负责展示交底书目录并支持定位章节
- `渲染区` 负责实时预览当前交底书
- `Agent Chat 区` 负责与主 agent 对话并驱动文档修改

当前阶段暂不支持用户在前端手动编辑正文。

也就是说：

- 正文修改主要通过 agent 驱动完成
- 用户如果需要手动编辑，可先导出 Markdown，再在系统外自行编辑

## 核心实现口径

当前文档体系采用以下统一口径：

1. 交底书文档定位使用 `section_id` 和 `block_id`
2. 文档读取统一通过 `document_read`
3. 文档写入统一通过 `document_edit`
4. 子 agent 只提出 proposal，主 agent 决定是否采纳
5. 子 agent 通过 `execute_subagent` 调度工具启动
6. 文档变更事件使用 `changed_section_ids` 和 `changed_block_ids`
7. `render_ast` 使用 id 体系支撑前端定位、高亮和滚动

## 后续建议

后续如果继续新增文档，建议统一遵循两条规则：

1. 每份文档开头增加“文档定位”
2. 每份文档明确列出“依赖文档”和“相关文档”

这样即使文档继续增多，也不会失去结构。
