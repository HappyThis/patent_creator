# 子 Agent 定义

## 文档定位

本文档定义本项目的业务子 agent 清单、职责范围、权限边界和声明字段。

子 agent 的结果传输与结束方式以 [子 Agent 管道协议](subagent-pipe-protocol.md) 为准。子 agent 不通过复杂 envelope 或 `document_edit.operations` 向主 agent 交付最终结果，而是将需要展示给主 agent 的内容写入 pipe，并通过 `finish({})` 结束。

依赖文档：

- [专利交底书结构方案](../specs/patent-disclosure-structure.md)
- [Agent 基本设计原则](agent-principles.md)
- [Agent Prompt 与上下文规范](agent-prompt-context-spec.md)
- [子 Agent 管道协议](subagent-pipe-protocol.md)
- [Tools 设计](tools.md)

## 一、总体原则

系统提供 3 个业务子 agent：

1. `material_analyst`
2. `solution_refiner`
3. `section_writer`

这 3 个子 agent 对应三类核心能力：

1. 从资料和对话中抽取事实
2. 将事实收敛为技术方案
3. 面向局部 section 或 block 生成候选正文

统一原则：

1. 用户始终只与主 agent 对话。
2. 子 agent 不直接面向用户。
3. 主 agent 负责决定何时调用哪个子 agent。
4. 子 agent 只提供分析、骨架或候选正文。
5. 主 agent 负责解释子 agent 内容，并决定是否采纳。
6. `document_edit` 只能由主 agent 调用。
7. 子 agent 不直接修改 `disclosure.json`。
8. 子 agent 支持多轮 tool use，但不允许继续调用其他子 agent。
9. 子 agent 的结果交付遵守管道协议：内容写入 `write_pipe(content)`，结束调用 `finish({})`。

## 二、权限边界

子 agent 可以：

- 分析用户资料和已有正文
- 读取自身权限范围内允许读取的上下文
- 使用自身权限范围内允许使用的工作工具
- 生成分析、方案骨架或局部候选正文
- 将需要展示给主 agent 的内容写入 pipe
- 调用 `finish({})` 结束本次子 agent run

子 agent 不可以：

- 调用 `document_edit`
- 调用 `execute_subagent`
- 直接修改 `disclosure.json`
- 决定是否采纳自己的候选内容
- 执行 git commit
- 面向用户直接收束回合
- 把最终结果绕过 pipe 直接交给主 agent

说明：

- 管道协议只新增 `write_pipe(content)` 和 `finish({})` 两个结果传输工具。
- 其他工作工具不属于管道协议，仍由子 agent 声明和执行器权限配置决定。

## 三、上下文装配

子 agent 采用统一上下文装配策略：

1. 主 agent 通过 `execute_subagent` 提供 `agent_id` 和 `goal`。
2. 上下文管理器自动装配子 agent 的 OpenAI-compatible `messages`。
3. 子 agent 使用自己的 system prompt。
4. 子 agent 不继承主 agent 的 system prompt。
5. 子 agent 继承调用方当前可见且已闭合的 `messages`，这里的 `messages` 不是 session raw events。
6. 上下文管理器在继承消息之后追加由 `agent_task` barrier 渲染出的任务说明 message。
7. 任务说明 message 只说明继承上下文的含义和本次执行目标。
8. 子 agent 内部工具调用与工具结果只服务于本次子 agent run。
9. 主 agent 只接收 `execute_subagent` 的最终工具返回结果。

如果子 agent 判断上下文不足，应在权限范围内调用读取或诊断工具；如果仍无法完成，应把缺口、风险或待确认问题写入 pipe 后调用 `finish({})`。

## 四、子 agent 清单

### 1. material_analyst

`material_analyst` 用于把用户聊天内容、参考资料、已有草稿中的信息提炼成技术事实。

它主要负责：

- 抽取技术主题、应用场景、现有方案、问题点、模块、流程、效果
- 识别信息缺口、歧义点和待澄清点
- 归纳候选术语
- 给主 agent 提供事实、术语、风险和待确认项

它不直接负责：

- 发明完整技术方案
- 直接生成交底书正文
- 做最终一致性裁决
- 修改文档

适合场景：

- 用户刚开始聊一个方向
- 用户提供参考资料、草稿、说明材料
- 主 agent 需要先把零散信息整理干净

### 2. solution_refiner

`solution_refiner` 用于把零散技术事实收敛成一个可写作、可继续讨论的技术方案骨架。

它主要负责：

- 组织整体方案结构
- 拆出模块、流程、关键约束、创新点
- 指出还需要用户确认的设计点
- 给主 agent 提供可继续写作的方案骨架

它不直接负责：

- 与用户直接对话
- 直接完成整份交底书成文
- 做最终落盘决定
- 修改文档

适合场景：

- 用户想法比较散，需要整理成方案
- 用户在反复讨论技术方案，需要逐步补完整
- 主 agent 判断当前素材适合先收敛方案再写

### 3. section_writer

`section_writer` 用于面向指定 section 或 block 生成轻量局部候选正文。

它主要负责：

- 起草一个短段落、短列表或单个局部子章节
- 补写或改写局部正文
- 按主 agent 给出的局部目标生成候选文本
- 给主 agent 提供可审查的正文片段

它不直接负责：

- 决定整篇方案方向
- 决定全局文档结构
- 一次生成完整技术方案、完整实施例或多子章节整章内容
- 生成最终 `document_edit.operations`
- 修改文档

适合场景：

- 用户或主 agent 明确要求补写一个局部段落
- 目标 section 或 block 已经明确
- 主 agent 需要一个轻量正文候选，而不是完整章节

## 五、子 agent 声明字段

每个子 agent 声明包含：

- `id`
- `description`
- `input_expectation`
- `output_contract`
- `usage_guidance`
- `tool_permissions`

字段说明：

- `id`：子 agent 的稳定机器标识。
- `description`：能力边界说明。
- `input_expectation`：通常需要的输入。
- `output_contract`：面向主 agent 的结果口径；当前协议下应表达为 pipe 内容类型，而不是复杂 envelope。
- `usage_guidance`：主 agent 调用该子 agent 时应遵守的使用边界。
- `tool_permissions`：允许调用的工作工具；不包含管道协议新增的 `write_pipe` 和 `finish`。

## 六、子 agent 声明示例

### material_analyst

```json
{
  "id": "material_analyst",
  "description": "从当前聊天材料和已有正文中抽取技术事实、术语和待确认问题。",
  "input_expectation": "提供用户输入、必要的参考章节和分析目标。",
  "output_contract": "通过 pipe 返回事实、术语、风险和待确认项。",
  "usage_guidance": "适合在写作前提炼事实、术语、约束和待确认问题；不要要求它直接生成完整正文或落盘编辑。",
  "tool_permissions": [
    "document_read",
    "exec_command"
  ]
}
```

### solution_refiner

```json
{
  "id": "solution_refiner",
  "description": "将零散事实收敛成可继续讨论或继续写作的技术方案骨架。",
  "input_expectation": "提供事实摘要、目标方向以及必要章节上下文。",
  "output_contract": "通过 pipe 返回方案骨架、模块关系、关键流程和待确认点。",
  "usage_guidance": "适合收敛技术问题、核心手段、模块关系和流程骨架；当需要完整章节正文时，由主 agent 基于骨架继续规划和落盘。",
  "tool_permissions": [
    "document_read",
    "exec_command"
  ]
}
```

### section_writer

```json
{
  "id": "section_writer",
  "description": "面向指定 section 或 block 生成轻量局部候选正文。",
  "input_expectation": "提供明确的局部目标，例如一个子章节、一个短段落或一个短列表，以及必要的当前章节内容。",
  "output_contract": "通过 pipe 返回局部候选正文。",
  "usage_guidance": "轻量局部写作工具；不要用于完整技术方案、完整实施例或多子章节整章生成。单次正文一般不超过 800 个中文字符。",
  "tool_permissions": [
    "document_read",
    "exec_command"
  ]
}
```

## 七、设计结论

子 agent 设计如下：

1. 子 agent 清单为 `material_analyst`、`solution_refiner`、`section_writer`。
2. 子 agent 只提供分析、骨架或候选正文，不直接修改文档。
3. 子 agent 不生成最终 `document_edit.operations` 作为默认责任。
4. 子 agent 的结果内容通过 `write_pipe(content)` 进入 pipe。
5. 子 agent 通过 `finish({})` 显式结束本次 run。
6. 主 agent 读取 pipe content 后，决定是否采纳、如何结构化和是否调用 `document_edit`。
