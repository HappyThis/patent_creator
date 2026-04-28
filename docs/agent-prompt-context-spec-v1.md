# Agent Prompt 与上下文规范 v1

## 文档定位

本文档统一定义本项目中 agent 的 prompt 组织方式、上下文组成方式以及 prefix cache 设计原则。

它建立在以下文档之上：

- [专利交底书结构方案 v1](/Users/yangchaoqun/myProj/patent_creator/docs/patent-disclosure-structure-v1.md)
- [Agent 基本设计原则 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles-v1.md)

本文档覆盖两类内容：

- agent 上下文组成与按需读取原则
- agent prompt 分层与 prefix cache 设计原则

## 目标

本文档重点回答四个问题：

1. agent 的 prompt 应如何按角色拆分
2. agent 的 prompt 应如何按稳定性拆分
3. agent 的上下文默认包含什么
4. 如何利用 prefix cache 降低成本并提升稳定性

本文档不讨论具体有哪些子 agent，也不提供具体 prompt 文案。

## 一、基本原则

1. 默认不注入完整交底书全文。
2. 默认只注入最小必要信息。
3. 正文内容按需读取。
4. 修改后如有必要，再回读目标章节确认。
5. 主 agent 与子 agent 的上下文都应尽量收敛。
6. prompt 既要按角色拆分，也要按稳定性拆分。
7. 主 agent 的控制输出必须结构化。

## 二、为什么要考虑 prefix cache

在模型调用中，如果 prompt 前缀部分长期保持稳定，服务端更容易复用前缀计算结果。

这会带来三个直接好处：

1. 降低调用成本
2. 降低调用延迟
3. 提高多轮调用时的 prompt 一致性

因此，本项目的 prompt 设计不应只追求“信息完整”，还应追求“前缀尽量稳定”。

## 三、按角色拆分

### 1. 通用 Agent 模板

通用模板适用于所有 agent。

建议包含：

- agent 的基本身份
- 总体目标
- 基本行为约束
- 输出要求
- 不越权、不编造、信息不足时先读取等基础规则

这些内容通常长期稳定，适合放在稳定前缀中。

### 2. 主 Agent 模板

主 agent 模板在通用模板之上增加主 agent 独有信息。

建议包含：

- 主 agent 是决策者而不是执行器
- 主 agent 负责决定读什么、调用什么、是否采纳结果
- 主 agent 不负责上下文管理
- 主 agent 不负责真正执行工具调用

主 agent 常见可见信息包括：

- 可用工具清单
- 可用子 agent 清单
- 当前工作区
- 交底书 meta
- 交底书目录结构

### 3. 子 Agent 模板

子 agent 模板在通用模板之上增加子 agent 独有信息。

建议包含：

- 该子 agent 的单一职责
- 该子 agent 的输入边界
- 该子 agent 的输出格式
- 该子 agent 的越权限制

子 agent 不应默认拥有：

- 其他子 agent 的信息
- 全局编排职责
- 全量项目背景

## 四、按稳定性拆分

### 1. 稳定前缀

稳定前缀是长期几乎不变的内容，应尽量固定。

建议包括：

- 通用 agent 身份和基本规则
- 当前 agent 的角色边界
- 当前 agent 的职责说明
- 输出格式要求
- 交底书结构原则

特点：

- 跨轮次稳定
- 跨调用稳定
- 最适合利用 prefix cache

### 2. 半稳定前缀

半稳定前缀是变化不频繁但可能随项目变化的内容。

建议包括：

- 当前工作区
- 可用工具清单
- 可用子 agent 清单
- 交底书 meta
- 交底书目录结构

特点：

- 同一项目内较稳定
- 不应频繁改格式
- 适合尽量保持序列和字段顺序稳定

### 3. 动态后缀

动态后缀是每轮都可能变化的内容。

建议包括：

- 当前时间
- 当前用户输入
- 本轮任务描述
- 本轮按需读取的章节内容
- 本轮工具返回结果
- 本轮子 agent 返回结果
- 最近少量 git 提交信息

特点：

- 高频变化
- 不适合放在 prompt 前部
- 应尽量后置，避免破坏 prefix cache

## 五、主 Agent 的提示词组成

主 agent 的提示词可理解为四部分：

1. `系统提示词`
2. `用户当前输入`
3. `默认上下文`
4. `按需读取结果`

### 1. 系统提示词

系统提示词负责规定主 agent 的角色与边界，例如：

- 主 agent 只负责决策
- 主 agent 不负责上下文管理
- 主 agent 不直接承担持久化职责
- 主 agent 可以决定是否读取正文、是否调用子 agent
- 主 agent 本身支持多轮 tool use 的 agent loop

### 2. 用户当前输入

这是本轮用户提出的目标、问题或修改要求。

例如：

- “帮我补写技术方案”
- “把实施例一改得更具体”
- “检查附图说明是否完整”

### 3. 默认上下文

默认上下文建议包含：

1. 当前交底书目录
2. 当前 session 中与本轮任务直接相邻的少量历史

其中“交底书目录”建议至少包含：

- 一级章节列表
- 某些章节下的二级子章节列表

默认不包含：

- 完整正文
- 大量历史事件
- 无关章节全文

说明：

- `当前用户任务`
- `最近少量修改摘要`
- `最近少量 session 摘要`

如果已经自然存在于当前上下文中，则不需要重复注入；只有在上下文被裁剪、压缩或切换调用环境时，才作为补充信息显式加入。

### 4. 按需读取结果

当主 agent 决定读取某些章节时，读取结果进入本轮上下文。

例如：

- 读取 `technical_solution`
- 读取 `embodiment_1`
- 读取附图说明对应的 `section_id` 或 `block_id`

这些结果不是默认常驻，而是根据当前任务动态加入。

## 六、子 Agent 的提示词组成

子 agent 提示词也可理解为四部分：

1. 子 agent 自己的系统提示词
2. 主 agent 下发的具体任务
3. 主 agent 传入的最小必要上下文
4. 必要的工具读取结果

### 1. 子 agent 系统提示词

每个子 agent 有自己独立的系统提示词。

它应明确：

- 自己负责什么任务
- 不负责什么任务
- 输出格式是什么
- 不能越权做什么
- 自己支持多轮 tool use
- 但不能继续调用其他 agent

### 2. 主 agent 下发的具体任务

主 agent 传给子 agent 的任务必须具体。

例如：

- “补写技术方案中的处理流程子章节”
- “根据现有技术缺陷，重写技术问题章节”
- “检查实施例一与技术方案是否一致”

### 3. 最小必要上下文

子 agent 默认只拿完成任务所必需的内容。

例如：

- 当前目标章节
- 相邻相关章节
- 必要的目录信息
- 必要的最近修改摘要

子 agent 默认不拿：

- 整篇交底书全文
- 全量 session 历史
- 无关章节正文

补充说明：

- 子 agent 虽然上下文更窄，但仍然支持 agent loop
- 当上下文不足时，子 agent 可以继续调用工具补充信息
- 但子 agent 不允许继续调用其他子 agent

### 4. 必要的工具读取结果

如果任务需要更多信息，子 agent 可以通过主流程触发进一步读取。

读取结果进入该子 agent 的本轮工作上下文。

## 七、默认注入内容与按需读取内容的边界

### 默认注入

默认注入内容应尽量稳定、紧凑。

建议包括：

- 当前交底书目录
- 当前 session 中与本轮任务直接相邻的少量上下文

### 按需读取

以下内容适合按需读取：

- 某个一级章节正文
- 某个二级子章节正文
- 某张图的说明
- 某个表格内容
- 某段刚修改后的正文

## 八、主 Agent 的结构化控制输出

主 agent 面向系统的控制输出必须使用结构化格式。

这里需要区分两件事：

1. 主 agent 面向用户的回复
2. 主 agent 面向系统的控制指令

其中：

- 面向用户的回复可以是自然语言
- 面向系统的控制指令必须结构化

v1 建议主 agent 只输出两类控制动作：

1. `respond`
2. `tool_call`

### 1. respond

当主 agent 选择直接回复用户时，输出 `respond`。

示例：

```json
{
  "kind": "respond",
  "message": "我先确认两个点：这个方案更偏方法专利还是系统专利？是否已经有明确实施例？"
}
```

### 2. tool_call

当主 agent 选择调用系统能力时，输出 `tool_call`。

普通工具和子 agent 调用都统一走 `tool_call`。

示例：

```json
{
  "kind": "tool_call",
  "name": "document_read",
  "arguments": {
    "action": "get_section",
    "section_id": "technical_solution",
    "include_children": true
  }
}
```

## 九、execute_subagent 最小调用协议

v1 中，启动子 agent 统一使用一个工具：

- `execute_subagent`

最小参数固定为：

- `agent_id`
- `goal`
- `call_type`

可选参数：

- `target_section_id`
- `target_block_id`

示例：

```json
{
  "kind": "tool_call",
  "name": "execute_subagent",
  "arguments": {
    "agent_id": "solution_refiner",
    "goal": "将当前讨论内容整理成一个更完整的技术方案，并指出仍需用户确认的关键决策。",
    "call_type": "rich_context_specialist",
    "target_section_id": "technical_solution",
    "target_block_id": null
  }
}
```

字段说明：

- `agent_id`：调用哪个子 agent
- `goal`：对子 agent 的自然语言任务描述，也是最核心的任务语义输入
- `call_type`：本次调用采用哪种上下文装配策略
- `target_section_id`：目标章节，可选
- `target_block_id`：目标 block，可选

说明：

1. `goal` 负责描述“要做什么”
2. `call_type` 负责选择“按什么类型装配上下文”
3. `target_section_id` 和 `target_block_id` 负责提供结构化目标
4. 上下文管理器根据 `call_type` 和当前系统状态装配实际上下文

因此，v1 的核心原则是：

**主 agent 只负责做意图级决策，不负责显式搬运上下文。**

### 三种 `call_type` 的装配规则

#### 1. `forked_context`

- `100%` 继承当前调用方上下文
- 包括系统提示词

当前实现中，`forked_context` 会在专业子 agent 自己的 system prompt 下，额外注入最近 session 事件摘要，尽量保留调用现场；不会让子 agent 继承主 agent 的系统提示词，以避免职责边界混淆。

#### 2. `rich_context_specialist`

- 继承当前调用方的非系统提示词部分
- 使用自己的 system prompt

当前实现中，`rich_context_specialist` 会注入目录、最近用户输入，并在提供 `target_section_id` 时预读目标章节。

#### 3. `task_only_specialist`

- 只继承 `goal`
- 不继承其他上下文

当前实现中，`task_only_specialist` 只注入任务、用户原始输入和结构化目标 id；如需正文或目录，子 agent 必须通过 `document_read` 自行读取。

说明：

- `forked_context` 更像复制一个当前调用方的完整现场
- `rich_context_specialist` 更像带着当前现场，切换到一个专门子 agent 的脑子
- `task_only_specialist` 更像只给任务，不给背景

## 十、子 agent 的统一输出协议

既然子 agent 在调用层被视为一种特殊工具，那么它必须有统一的标准输出。

v1 的输出协议固定为：

- `status`
- `summary`
- `proposal`
- `questions`
- `warnings`

### 1. status

`status` 表示本次执行的最终状态。

v1 建议先固定两个值：

- `success`
- `failed`

### 2. summary

`summary` 是给主 agent 阅读的简短结论。

### 3. proposal

`proposal` 是结构化结果。

支持类型：

- `document_edit_proposal`
- `analysis_result`
- `review_report`

### 4. questions

`questions` 用于表达需要用户或主 agent 补充确认的信息。

### 5. warnings

`warnings` 用于表达风险、假设和不确定性。

成功示例：

```json
{
  "status": "success",
  "summary": "已生成技术方案章节候选正文。",
  "proposal": {
    "type": "document_edit_proposal",
    "target_section_id": "technical_solution",
    "intent": "replace_section_blocks",
    "confidence": 0.84,
    "rationale": "目标章节适合整体写入候选 blocks。",
    "operations": [
      {
        "op": "replace_section_blocks",
        "section_id": "technical_solution",
        "blocks": [
          {
            "type": "paragraph",
            "text": "本发明提供一种图像检测方法。"
          }
        ]
      }
    ]
  },
  "questions": [],
  "warnings": []
}
```

失败示例：

```json
{
  "status": "failed",
  "summary": "当前信息不足，无法完成技术方案收敛。",
  "proposal": null,
  "questions": [
    "请补充现有技术缺陷描述。"
  ],
  "warnings": []
}
```

说明：

- 外层协议固定
- 内层 `proposal.type` 区分具体结果类型
- `document_edit_proposal.operations` 只是候选修改
- 主 agent 负责决定是否采纳候选修改
## 十一、主 Agent 的推荐 prompt 结构

主 agent 的 prompt 可按以下结构组织：

1. 通用稳定前缀
2. 主 agent 稳定前缀
3. 主 agent 半稳定前缀
4. 当前轮次动态后缀

推荐内容：

### 稳定前缀

- 通用规则
- 主 agent 职责边界
- 主 agent 行为模式

### 半稳定前缀

- 当前工作区
- 工具列表
- 子 agent 列表
- 交底书 meta
- 交底书目录结构

### 动态后缀

- 当前时间
- 当前用户输入
- 当前任务
- 本轮读取结果
- 本轮执行结果
- 最近几次 git 提交信息

## 十二、子 Agent 的推荐 prompt 结构

子 agent 的 prompt 也应按同样的稳定性拆分，但总体更收敛。

推荐结构：

1. 通用稳定前缀
2. 子 agent 稳定前缀
3. 子 agent 半稳定前缀
4. 当前任务动态后缀

推荐内容：

### 稳定前缀

- 通用规则
- 当前子 agent 的职责说明
- 当前子 agent 的输出格式
- 当前子 agent 的越权限制

### 半稳定前缀

- 当前工作区
- 必要的少量 meta
- 必要的目录信息

### 动态后缀

- 主 agent 下发的具体任务
- 与任务相关的局部正文
- 与任务相关的工具读取结果
- 当前时间

子 agent 默认不应包含：

- 全量 git 提交历史
- 其他子 agent 列表
- 全文正文
- 大量 session 历史

## 十三、修改后的上下文策略

当发生正文修改后：

1. 修改动作进入当前上下文
2. 修改动作记入 session 日志
3. 如果主 agent 或子 agent 需要确认结果，可重新读取目标章节

因此：

- 不需要把整篇交底书重新塞回上下文
- 只需要对目标位置进行必要回读

## 十四、关于 git 提交信息的放置建议

最近几次 git 提交信息对主 agent 有价值，因为它能帮助理解近期工作方向。

但 git 提交信息通常变化较频繁，因此建议：

- 放在动态后缀中
- 控制数量，例如最近 3 到 5 条
- 使用稳定格式，例如 `id + message`

不建议默认注入：

- 大段 git diff
- 大量历史提交

这些内容应按需读取。

## 十五、关于当前时间的放置建议

当前时间应作为动态信息放在后段。

原因：

- 每次调用都可能不同
- 放在前缀会破坏 prefix cache

因此：

- 应提供当前时间
- 但不要将其放在稳定前缀中

## 十六、实现建议

实现时，建议将 prompt 拆成多个片段文件或模板层：

- `base_agent_prompt`
- `main_agent_prompt`
- `subagent_prompt_<type>`
- `project_context_fragment`
- `runtime_context_fragment`

再由系统在调用前按顺序拼装。

这样做的好处：

1. 便于复用稳定前缀
2. 便于控制 prefix cache 命中
3. 便于不同 agent 之间复用公共规则
4. 便于后续演化不同子 agent 模板

## 十七、最终原则总结

本项目的 prompt 与上下文设计原则如下：

1. 默认不注入完整交底书全文
2. 默认只注入目录、任务和少量必要摘要
3. 正文内容按需读取
4. 主 agent 面向系统的控制输出必须结构化
5. 子 agent 统一通过 `execute_subagent` 工具触发
6. `execute_subagent` 的最小参数为 `agent_id + goal + call_type`
7. `execute_subagent` 可携带 `target_section_id` 和 `target_block_id`
8. 子 agent 的统一输出协议为 `status + summary + proposal + questions + warnings`
9. prompt 同时按角色和稳定性两个维度拆分
10. 稳定规则尽量前置，以利用 prefix cache
11. 高频变化内容尽量后置
12. 主 agent 与子 agent 不共享同一套完整模板
13. 子 agent 使用更窄、更专注的模板
14. git 提交信息和当前时间属于动态后缀
15. 修改后如有必要，只回读目标章节或目标 block 确认

这套规则用于指导后续主 agent 与子 agent prompt 的实现。
