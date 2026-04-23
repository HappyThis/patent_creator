# 子 Agent 定义 v1

## 文档定位

本文档定义本项目 v1 阶段的 4 个核心子 agent。

它建立在以下文档之上：

- [专利交底书结构方案 v1](/Users/yangchaoqun/myProj/patent_creator/docs/patent-disclosure-structure-v1.md)
- [Agent 基本设计原则 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles-v1.md)
- [Agent Prompt 与上下文规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-prompt-context-spec-v1.md)

本文档当前只定义：

- 子 agent 名称
- 子 agent 职责范围
- 默认类型
- 允许类型
- 声明字段示例
- 输出协议约束

本文档暂不定义：

- 输入协议细节
- 具体 prompt 文案
- 工具调用细节

## 一、总体原则

v1 阶段先收敛为 4 个业务子 agent：

1. `material_analyst`
2. `solution_refiner`
3. `section_writer`
4. `consistency_reviewer`

这 4 个子 agent 对应四类核心能力：

1. 从资料和对话中抽取事实
2. 将事实收敛为技术方案
3. 将方案写成交底书章节
4. 对已有内容做一致性审查

说明：

1. 用户始终只与主 agent 对话
2. 子 agent 不直接面向用户
3. 主 agent 负责决定何时调用哪个子 agent
4. 实际上下文类型由主 agent 决定
5. 上下文管理器按本次调用类型装配上下文
6. 主 agent 只在必要且信息足够时调用子 agent

补充说明：

- 子 agent 不是常驻参与者
- 子 agent 的目标是承担局部深加工任务
- 主 agent 应尽量避免在轻量对话阶段频繁调用子 agent
- 如果资料和上下文还不足以支撑有效产出，应优先继续收集信息

## 二、子 agent 清单

### 1. material_analyst

`material_analyst` 用于把用户聊天内容、参考资料、已有草稿中的信息提炼成结构化技术事实。

它主要负责：

- 抽取技术主题、应用场景、现有方案、问题点、模块、流程、效果
- 识别信息缺口、歧义点和待澄清点
- 归纳候选术语

它不直接负责：

- 发明完整技术方案
- 直接生成交底书正文
- 做最终一致性裁决

默认类型：

- `rich_context_specialist`

允许类型：

- `rich_context_specialist`
- `task_only_specialist`

适合场景：

- 用户刚开始聊一个方向
- 用户上传参考资料、草稿、说明材料
- 主 agent 需要先把零散信息整理干净

### 2. solution_refiner

`solution_refiner` 用于把零散技术事实收敛成一个可写作、可继续讨论的技术方案。

它主要负责：

- 组织整体方案结构
- 拆出模块、流程、关键约束、创新点
- 指出还需要用户拍板的设计点
- 在必要时给出方案候选

它不直接负责：

- 与用户直接对话
- 直接完成整份交底书成文
- 做最终落盘决定

默认类型：

- `rich_context_specialist`

允许类型：

- `rich_context_specialist`
- `task_only_specialist`

适合场景：

- 用户想法比较散，需要整理成方案
- 用户在反复讨论技术方案，需要逐步补完整
- 主 agent 判断当前已经有足够素材，适合先收敛方案再写

### 3. section_writer

`section_writer` 用于面向指定章节或子章节执行写作任务。

它主要负责：

- 起草章节
- 补写章节
- 改写章节
- 按反馈重写章节
- 生成段落、列表、图片说明、表格文字等正文内容

它不直接负责：

- 决定整篇方案方向
- 决定全局文档结构
- 做全文一致性裁决

默认类型：

- `rich_context_specialist`

允许类型：

- `rich_context_specialist`
- `task_only_specialist`

适合场景：

- 用户明确要求“写出来”
- 用户要求补写某一节
- 用户要求改写、重写某一章或某个子章节

### 4. consistency_reviewer

`consistency_reviewer` 用于检查当前内容在术语、逻辑、章节关系和技术闭环上的一致性。

它主要负责：

- 检查术语是否统一
- 检查技术问题、技术方案、技术效果是否闭环
- 检查实施例是否支撑方案
- 检查章节是否冲突、跳步、空泛

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
- 主 agent 判断当前文本已经值得做一次 review
- 某次写作或重写后需要局部审查

## 三、子 agent 声明示例

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
  "output_contract": "返回统一最小输出协议，其中 output 为自然语言工作结论。",
  "tool_permissions": []
}
```

### solution_refiner

```json
{
  "id": "solution_refiner",
  "description": "将零散技术事实整理为可写作的技术方案，输出方案结构、创新点和待确认决策，不直接完成整份交底书成文。",
  "default_type": "rich_context_specialist",
  "allowed_types": [
    "rich_context_specialist",
    "task_only_specialist"
  ],
  "input_expectation": "需要任务描述，通常需要已有事实、局部正文片段或参考方案说明。",
  "output_contract": "返回统一最小输出协议，其中 output 为自然语言工作结论。",
  "tool_permissions": []
}
```

### section_writer

```json
{
  "id": "section_writer",
  "description": "针对指定章节或子章节执行写作任务，支持起草、补写、改写和重写，输出候选正文内容或修改建议。",
  "default_type": "rich_context_specialist",
  "allowed_types": [
    "rich_context_specialist",
    "task_only_specialist"
  ],
  "input_expectation": "需要任务描述，通常需要目标章节信息、局部正文上下文和必要的方案材料。",
  "output_contract": "返回统一最小输出协议，其中 output 为自然语言工作结论。",
  "tool_permissions": []
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
  "output_contract": "返回统一最小输出协议，其中 output 为自然语言工作结论。",
  "tool_permissions": []
}
```

## 四、后续待讨论项

本文档之后还需要继续细化以下内容：

1. 4 个子 agent 的输入协议
2. 4 个子 agent 在 `status=success` 时的输出风格
3. 4 个子 agent 在 `status=failed` 时的输出风格
4. 主 agent 的调用判定规则
5. 哪些工具允许子 agent 直接调用

## 五、当前结论

本项目 v1 先采用以下 4 个业务子 agent：

1. `material_analyst`
2. `solution_refiner`
3. `section_writer`
4. `consistency_reviewer`

这是后续继续设计输入输出协议的基础。
