# Docs 索引

## 当前文档

当前 `docs` 目录包含 6 份核心设计文档：

1. [专利交底书结构方案 v1](/Users/yangchaoqun/myProj/patent_creator/docs/patent-disclosure-structure-v1.md)
2. [Agent 基本设计原则 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles-v1.md)
3. [Agent Prompt 与上下文规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-prompt-context-spec-v1.md)
4. [子 Agent 定义 v1](/Users/yangchaoqun/myProj/patent_creator/docs/subagents-v1.md)
5. [Tools 设计 v1](/Users/yangchaoqun/myProj/patent_creator/docs/tools-v1.md)
6. [Session 事件日志 Schema v1](/Users/yangchaoqun/myProj/patent_creator/docs/session-log-v1.md)

## 是否需要合并

当前阶段不建议把所有文档继续拆得更细。

原因：

1. `专利交底书结构方案` 是文档 schema，本身就是独立主题
2. `Agent 基本设计原则` 是系统架构与职责边界，也应独立
3. `Agent Prompt 与上下文规范` 同时覆盖上下文装配、prompt 分层与 prefix cache
4. `子 Agent 定义 v1` 负责收敛业务子 agent 清单与能力边界
5. `Tools 设计 v1` 负责收敛工具集合与当前取舍
6. `Session 事件日志 Schema v1` 负责收敛会话过程记录格式

它们虽然相关，但关注点仍然不同。全部继续合并成一份总文档后，后续修改会变得笨重。

## 建议的合并策略

当前建议是：

- 保留 `专利交底书结构方案 v1` 独立
- 保留 `Agent 基本设计原则 v1` 独立
- 将 `上下文说明` 与 `prompt 模板原则` 合并为一份 `Agent Prompt 与上下文规范 v1`
- 保留 `子 Agent 定义 v1` 独立
- 保留 `Tools 设计 v1` 独立
- 保留 `Session 事件日志 Schema v1` 独立

当前阶段保持 6 份核心文档更利于迭代。

## 文档依赖关系

建议按下面顺序阅读：

1. [专利交底书结构方案 v1](/Users/yangchaoqun/myProj/patent_creator/docs/patent-disclosure-structure-v1.md)
2. [Agent 基本设计原则 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles-v1.md)
3. [Agent Prompt 与上下文规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-prompt-context-spec-v1.md)
4. [子 Agent 定义 v1](/Users/yangchaoqun/myProj/patent_creator/docs/subagents-v1.md)
5. [Tools 设计 v1](/Users/yangchaoqun/myProj/patent_creator/docs/tools-v1.md)
6. [Session 事件日志 Schema v1](/Users/yangchaoqun/myProj/patent_creator/docs/session-log-v1.md)

依赖关系可以理解为：

- `专利交底书结构方案 v1`
  - 最底层基础文档
- `Agent 基本设计原则 v1`
  - 依赖交底书结构边界
- `Agent Prompt 与上下文规范 v1`
  - 依赖交底书结构和 agent 基本原则
- `子 Agent 定义 v1`
  - 依赖 agent 基本原则和 prompt/context 规范
- `Tools 设计 v1`
  - 依赖 agent 基本原则、prompt/context 规范和子 agent 定义
- `Session 事件日志 Schema v1`
  - 依赖 agent 基本原则、prompt/context 规范、子 agent 定义和 tools 设计

## 后续建议

后续如果继续新增文档，建议统一遵循两条规则：

1. 每份文档开头增加“文档定位”
2. 每份文档明确列出“依赖文档”和“相关文档”

这样即使文档继续增多，也不会失去结构。
