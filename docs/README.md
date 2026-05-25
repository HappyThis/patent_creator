# 文档索引

## 目录结构

- `core/`：单主 agent、prompt、上下文和工具边界等核心机制。
- `specs/`：产品、协议、上下文、前端和工作区等专题规范。
- `status/`：阶段性实现状态。
- `benchmarks/`：评测基准分类和具体评测规范。
- `assets/`：文档图片资源。

## 核心文档

1. [Agent 基本设计原则](core/agent-principles.md)
2. [Agent Prompt 与上下文规范](core/agent-prompt-context-spec.md)
3. [Tools 设计](core/tools.md)

## 规范文档

1. [专利交底书结构方案](specs/patent-disclosure-structure.md)
2. [上下文管理规范](specs/context-management.md)
3. [Session 事件日志 Schema](specs/session-log.md)
4. [工作区初始化规范](specs/workspace-init.md)
5. [技术栈规范](specs/tech-stack.md)
6. [API 设计规范](specs/api-design.md)
7. [render_ast Schema](specs/render-ast-schema.md)
8. [前端交互规范](specs/frontend-interaction.md)
9. [一轮内部时序](specs/round-lifecycle.md)

## 状态文档

- [后端实现概览](status/backend-implementation-status.md)

## 评测规范

- [评测基准规范索引](benchmarks/README.md)
- [GitHub 中型项目软件专利技术方案评测基准](benchmarks/software-patent-solution-github.md)
- [GitHub 中型项目候选清单](benchmarks/github-project-candidates.md)

## 推荐阅读顺序

1. [专利交底书结构方案](specs/patent-disclosure-structure.md)
2. [Agent 基本设计原则](core/agent-principles.md)
3. [Agent Prompt 与上下文规范](core/agent-prompt-context-spec.md)
4. [Tools 设计](core/tools.md)
5. [上下文管理规范](specs/context-management.md)
6. [Session 事件日志 Schema](specs/session-log.md)
7. [一轮内部时序](specs/round-lifecycle.md)
8. [API 设计规范](specs/api-design.md)
9. [前端交互规范](specs/frontend-interaction.md)

## 产品形态

产品形态按 Web 前端理解。前端采用三栏结构：

1. 目录区域
2. 渲染区
3. Agent Chat 区

正文修改主要通过主 agent 驱动完成；用户如果需要手动编辑，可导出 Markdown 后在系统外处理。

## 当前实现口径

1. 系统采用单主 agent 架构。
2. 交底书文档定位使用 `section_id` 和 `block_id`。
3. 文档读取统一通过 `document_read`。
4. 文档写入统一通过专用文档写入工具。
5. 文档变更事件使用 `changed_section_ids` 和 `changed_block_ids`。
6. `render_ast` 使用 id 体系支撑前端定位、高亮和滚动。
