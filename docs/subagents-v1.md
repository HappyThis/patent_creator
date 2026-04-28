# 子 Agent 定义 v1

## 文档定位

本文档定义本项目 v1 阶段的 4 个核心子 agent。

它建立在以下文档之上：

- [专利交底书结构方案 v1](/Users/yangchaoqun/myProj/patent_creator/docs/patent-disclosure-structure-v1.md)
- [Agent 基本设计原则 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles-v1.md)
- [Agent Prompt 与上下文规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-prompt-context-spec-v1.md)
- [Tools 设计 v1](/Users/yangchaoqun/myProj/patent_creator/docs/tools-v1.md)

本文档定义：

- 子 agent 清单
- 子 agent 职责范围
- 子 agent 权限边界
- 子 agent 声明字段
- 子 agent 统一返回结构
- 不同 proposal 类型

## 一、总体原则

v1 阶段先收敛为 4 个业务子 agent：

1. `material_analyst`
2. `solution_refiner`
3. `section_writer`
4. `consistency_reviewer`

这 4 个子 agent 对应四类核心能力：

1. 从资料和对话中抽取事实
2. 将事实收敛为技术方案
3. 将方案写成交底书章节候选内容
4. 对已有内容做一致性审查

统一原则：

1. 用户始终只与主 agent 对话。
2. 子 agent 不直接面向用户。
3. 主 agent 负责决定何时调用哪个子 agent。
4. 子 agent 只提出建议或候选结果。
5. 主 agent 负责判断是否采纳子 agent 结果。
6. `document_edit` 只能由主 agent 调用。
7. 子 agent 可以读取文档，但不直接修改 `disclosure.json`。
8. 子 agent 支持多轮 tool use，但不允许继续调用其他子 agent。

当前实现口径：

- 4 个子 agent 均通过统一的子 agent loop 运行。
- 子 agent 可在 loop 中调用 `document_read` 和 `exec_command`。
- 子 agent 的内部工具调用会写入 session log，并通过 SSE 以 `scope=subagent:<agent_id>` 实时推送。
- 子 agent 最终仍必须输出统一 envelope，再由主 agent 决定是否采纳。

## 二、权限边界

子 agent 可以：

- 调用 `document_read`
- 调用 `exec_command`
- 分析用户资料和已有正文
- 生成候选正文 blocks
- 生成候选 `document_edit.operations`
- 提出问题和风险提示
- 返回结构化 proposal

子 agent 不可以：

- 调用 `document_edit`
- 调用 `execute_subagent`
- 直接修改 `disclosure.json`
- 决定是否采纳自己的候选内容
- 执行 git commit
- 面向用户直接收束回合

## 三、上下文类型

当前类型定义如下：

### 1. forked_context

定义：

- `100%` 继承当前调用方上下文
- 包括系统提示词

适合：

- 上下文压缩
- 历史整理
- 处理当前运行现场的 runtime agent

### 2. rich_context_specialist

定义：

- 继承当前调用方的非系统提示词部分
- 使用自己的 system prompt

适合：

- 带着当前任务现场执行专业任务
- 局部写作
- 局部审查

### 3. task_only_specialist

定义：

- 只知道 `goal`
- 不继承其他上下文

适合：

- 输入已经足够自包含的任务
- 可通过工具自行读取上下文的任务

## 四、子 agent 清单

### 1. material_analyst

`material_analyst` 用于把用户聊天内容、参考资料、已有草稿中的信息提炼成结构化技术事实。

它主要负责：

- 抽取技术主题、应用场景、现有方案、问题点、模块、流程、效果
- 识别信息缺口、歧义点和待澄清点
- 归纳候选术语
- 输出 `analysis_result`

它不直接负责：

- 发明完整技术方案
- 直接生成交底书正文
- 做最终一致性裁决
- 修改文档

默认类型：

- `rich_context_specialist`

允许类型：

- `rich_context_specialist`
- `task_only_specialist`

适合场景：

- 用户刚开始聊一个方向
- 用户提供参考资料、草稿、说明材料
- 主 agent 需要先把零散信息整理干净

### 2. solution_refiner

`solution_refiner` 用于把零散技术事实收敛成一个可写作、可继续讨论的技术方案。

它主要负责：

- 组织整体方案结构
- 拆出模块、流程、关键约束、创新点
- 指出还需要用户确认的设计点
- 输出 `analysis_result`
- 在信息足够时输出 `document_edit_proposal`

它不直接负责：

- 与用户直接对话
- 直接完成整份交底书成文
- 做最终落盘决定
- 修改文档

默认类型：

- `rich_context_specialist`

允许类型：

- `rich_context_specialist`
- `task_only_specialist`

适合场景：

- 用户想法比较散，需要整理成方案
- 用户在反复讨论技术方案，需要逐步补完整
- 主 agent 判断当前素材适合先收敛方案再写

### 3. section_writer

`section_writer` 用于面向指定章节或 block 执行写作任务。

它主要负责：

- 起草章节候选 blocks
- 补写章节候选 blocks
- 改写章节候选 blocks
- 按反馈重写候选内容
- 生成段落、列表、图片说明、表格文字等块级内容
- 输出 `document_edit_proposal`

它不直接负责：

- 决定整篇方案方向
- 决定全局文档结构
- 做全文一致性裁决
- 修改文档

默认类型：

- `rich_context_specialist`

允许类型：

- `rich_context_specialist`
- `task_only_specialist`

适合场景：

- 用户明确要求“写出来”
- 用户要求补写某一节
- 用户要求改写、重写某一章、某个子章节或某个 block

### 4. consistency_reviewer

`consistency_reviewer` 用于检查当前内容在术语、逻辑、章节关系和技术闭环上的一致性。

它主要负责：

- 检查术语是否统一
- 检查技术问题、技术方案、技术效果是否闭环
- 检查实施例是否支撑方案
- 检查章节是否冲突、跳步、空泛
- 输出 `review_report`

它不直接负责：

- 直接改正文
- 决定是否最终采纳修改
- 接管写作任务

默认类型：

- `rich_context_specialist`

允许类型：

- `rich_context_specialist`
- `task_only_specialist`

适合场景：

- 用户要求“检查一下”
- 主 agent 判断当前文本值得做一次 review
- 某次写作或重写后需要局部审查

## 五、子 agent 声明字段

每个子 agent 声明包含：

- `id`
- `description`
- `default_type`
- `allowed_types`
- `input_expectation`
- `output_contract`
- `tool_permissions`
- `default_proposal_type`

字段说明：

- `id`：子 agent 的稳定机器标识
- `description`：能力边界说明
- `default_type`：默认上下文类型
- `allowed_types`：允许采用的上下文类型范围
- `input_expectation`：通常需要的输入
- `output_contract`：返回结构约束
- `tool_permissions`：允许调用的工具
- `default_proposal_type`：默认 proposal 类型

## 六、子 agent 声明示例

### material_analyst

```json
{
  "id": "material_analyst",
  "description": "从用户对话、参考资料和已有草稿中提炼结构化技术事实，输出事实摘要、信息缺口和候选术语，不直接生成交底书正文。",
  "default_type": "rich_context_specialist",
  "allowed_types": [
    "rich_context_specialist",
    "task_only_specialist"
  ],
  "input_expectation": "需要任务描述，通常需要参考资料、已有文本片段或用户刚提供的说明内容。",
  "output_contract": "返回统一子 agent envelope，其中 proposal 默认为 analysis_result。",
  "tool_permissions": [
    "document_read",
    "exec_command"
  ],
  "default_proposal_type": "analysis_result"
}
```

### solution_refiner

```json
{
  "id": "solution_refiner",
  "description": "将零散技术事实整理为可写作的技术方案，输出方案结构、创新点和待确认决策，不直接修改交底书正文。",
  "default_type": "rich_context_specialist",
  "allowed_types": [
    "rich_context_specialist",
    "task_only_specialist"
  ],
  "input_expectation": "需要任务描述，通常需要已有事实、局部正文片段或参考方案说明。",
  "output_contract": "返回统一子 agent envelope，其中 proposal 默认为 analysis_result，必要时可返回 document_edit_proposal。",
  "tool_permissions": [
    "document_read",
    "exec_command"
  ],
  "default_proposal_type": "analysis_result"
}
```

### section_writer

```json
{
  "id": "section_writer",
  "description": "针对指定章节或 block 执行写作任务，输出可由主 agent 审查采纳的候选 document_edit operations，不直接修改文档。",
  "default_type": "rich_context_specialist",
  "allowed_types": [
    "rich_context_specialist",
    "task_only_specialist"
  ],
  "input_expectation": "需要任务描述、目标 section_id 或 block_id、局部正文上下文和必要的方案材料。",
  "output_contract": "返回统一子 agent envelope，其中 proposal 默认为 document_edit_proposal。",
  "tool_permissions": [
    "document_read",
    "exec_command"
  ],
  "default_proposal_type": "document_edit_proposal"
}
```

### consistency_reviewer

```json
{
  "id": "consistency_reviewer",
  "description": "检查术语、逻辑、章节关系和技术闭环是否一致，输出问题清单和修改建议，不直接改写正文。",
  "default_type": "rich_context_specialist",
  "allowed_types": [
    "rich_context_specialist",
    "task_only_specialist"
  ],
  "input_expectation": "需要任务描述，通常需要目标章节和必要的关联章节内容。",
  "output_contract": "返回统一子 agent envelope，其中 proposal 默认为 review_report。",
  "tool_permissions": [
    "document_read",
    "exec_command"
  ],
  "default_proposal_type": "review_report"
}
```

## 七、统一返回结构

所有子 agent 固定返回统一 envelope：

```json
{
  "status": "success",
  "summary": "一句话说明本次结果。",
  "proposal": {},
  "questions": [],
  "warnings": []
}
```

字段说明：

- `status`：`success` 或 `failed`
- `summary`：给主 agent 的简短结论
- `proposal`：结构化结果，失败时可以为 `null`
- `questions`：需要用户或主 agent 补充的信息
- `warnings`：风险、假设和不确定性

失败示例：

```json
{
  "status": "failed",
  "summary": "当前信息不足，无法完成任务。",
  "proposal": null,
  "questions": [
    "请补充目标应用场景。"
  ],
  "warnings": []
}
```

## 八、proposal 类型

`proposal.type` 支持：

```text
document_edit_proposal
analysis_result
review_report
```

### 1. document_edit_proposal

用于提出可采纳的文档修改候选。

示例：

```json
{
  "type": "document_edit_proposal",
  "target_section_id": "technical_solution",
  "target_block_id": null,
  "intent": "replace_section_blocks",
  "confidence": 0.82,
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
}
```

规则：

1. `operations` 结构贴近 `document_edit.operations`。
2. 新增 block 不携带 `id`。
3. 主 agent 必须审查后决定是否采纳。
4. 子 agent 返回 proposal 不等于文档已经修改。

### 2. analysis_result

用于返回事实提炼、方案收敛或下一步建议。

示例：

```json
{
  "type": "analysis_result",
  "facts": [
    {
      "kind": "technical_problem",
      "text": "现有图像检测方法在低算力设备上实时性不足。"
    }
  ],
  "candidate_terms": [
    "低算力设备",
    "特征提取模块"
  ],
  "recommended_next_actions": [
    {
      "action": "write_section",
      "section_id": "technical_problem"
    }
  ]
}
```

### 3. review_report

用于返回一致性审查结果。

示例：

```json
{
  "type": "review_report",
  "issues": [
    {
      "severity": "medium",
      "section_id": "technical_effects",
      "block_id": null,
      "message": "技术效果未对应技术问题中的实时性目标。",
      "suggested_fix": "补充低延迟或低算力相关效果。"
    }
  ]
}
```

## 九、当前结论

v1 采用以下子 agent 设计：

1. 子 agent 清单为 `material_analyst`、`solution_refiner`、`section_writer`、`consistency_reviewer`。
2. 子 agent 只提出建议，不直接修改文档。
3. 子 agent 可使用 `document_read`，不可使用 `document_edit`。
4. 子 agent 不可调用其他子 agent。
5. 所有子 agent 使用统一返回 envelope。
6. `proposal.type` 区分 `document_edit_proposal`、`analysis_result`、`review_report`。
7. 主 agent 决定是否采纳 `document_edit_proposal.operations`。
