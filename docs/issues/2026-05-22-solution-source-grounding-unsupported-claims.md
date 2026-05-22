# 技术方案源码依据与无支撑断言问题

> 状态：进行中
> 最后更新：2026-05-22
> 关闭条件：agent 能稳定区分“当前源码事实”和“拟新增设计”，不再把未实现 API、方法名、数据流或能力描述为当前项目已有事实。

## 背景

本 benchmark 要评估的是主 agent 在给定项目快照和粗粒度需求下生成软件专利技术方案的能力。对于基于开源项目的 case，方案必须能贴合当前源码，同时不能编造源码中不存在的接口或能力。

## 现象

`20260522-deepseek-v4-pro-reasoning-5x` 的 judge 结果多次指出 `unsupported_claims`：

- `002`：方案把 task-dispatch 与 spawn-history 的桥接描述为当前已存在，但源码中未见对应调用链。
- `007`：方案使用 agentTool、runAgentTool、getChatChunksForReplay 等方法名，但当前源码或 RFC 中依据不足。
- `008`：方案称 generateTypesFromJsonSchema 可直接生成多 namespace 类型声明，但当前源码更偏固定 `codemode` 声明。
- `010`：方案把 `@callable` 与内部 Durable Object RPC 边界混用，部分描述不符合当前 Agents 文档语义。

## 影响

- 评估分数下降，尤其在 `unsupported_claims` 和 feasibility 维度。
- 用户可能误以为项目已有某些接口，导致后续实现计划失真。
- 专利技术方案会混入“不真实的现有技术基础”，影响交底书可信度。

## 处理方向

- 主 agent 在写技术方案时，应明确区分三类内容：
  - 已有源码事实。
  - 基于源码扩展的新增模块。
  - 待确认或可选设计。
- 对方法名、表名、API 路径、工具名、框架能力，应优先引用真实源码或文档；无法确认时使用功能性命名，而不是断言已有具体符号。
- 在技术方案中可使用“新增”“扩展”“引入”等措辞，避免把拟新增机制写成现状。

## 验证方式

- 统计每次 judge 输出中的 `unsupported_claims` 数量和严重程度。
- 优先复测 `002`、`007`、`008`、`010`。
- 目标是 unsupported claims 明显减少，且不牺牲技术机制深度。
