# Tools 设计

## 文档定位

本文档定义本项目提供的工具集合。

它建立在以下文档之上：

- [Agent 基本设计原则](agent-principles.md)
- [Agent Prompt 与上下文规范](agent-prompt-context-spec.md)
- [子 Agent 定义](subagents.md)

本文档定义：

- 工具清单
- 工具职责
- 文档读写工具协议
- 子 agent 调度工具协议
- 通用命令工具协议

本文档不定义每个工具的底层实现细节。

## 一、总体原则

工具设计遵循以下原则：

1. 工具数量保持收敛。
2. 交底书正文读写只能通过专用文档工具完成。
3. 文档定位统一使用 `section_id` 和 `block_id`。
4. 文档工具负责 id 索引、schema 校验、权限控制和变更追踪。
5. 主 agent 与子 agent 的工具权限不同。
6. 子 agent 通过 `execute_subagent` 调度工具启动。
7. 通用命令工具不用于直接修改 `disclosure.json`。

## 二、工具清单

系统保留以下核心工具：

1. `document_read`
2. `document_edit`
3. `execute_subagent`
4. `exec_command`

### 1. document_read

`document_read` 是交底书文档的只读入口。

它负责：

- 读取项目上下文
- 读取文档元信息
- 读取目录
- 按 `section_id` 读取章节
- 按 `block_id` 读取块级内容
- 搜索块级文本

`document_read` 不产生副作用。

### 2. document_edit

`document_edit` 是交底书文档的唯一写入入口。

它负责：

- 校验写入操作
- 自动生成新增 block id
- 原子写入 `disclosure.json`
- 返回变更的 section id 和 block id

`document_edit` 只能由主 agent 调用。

### 3. execute_subagent

`execute_subagent` 是子 agent 调度工具。

它负责：

- 启动指定子 agent
- 触发上下文管理器自动装配子 agent `messages`
- 返回子 agent 的统一结果结构

`execute_subagent` 只能由主 agent 调用。

### 4. exec_command

`exec_command` 用于执行通用命令行操作。

适合：

- 文件浏览
- 调试命令
- git 操作
- 非交底书真相源的辅助处理

不适合：

- 直接修改 `disclosure.json`
- 绕过 `document_edit` 执行文档写入

运行口径：

- `exec_command` 已暴露给主 agent 和子 agent。
- 以当前 project 工作区为 cwd 执行命令字符串。
- 不做命令白名单限制，支持 shell 能力，例如管道、重定向、命令拼接和外部访问。
- 工具层只校验调用方 scope 权限、`command` 是否为空和 timeout 执行结果。
- 命令自身失败时仍以工具结果返回，由 agent 根据 `exit_code`、`stdout`、`stderr` 继续判断。

## 三、工具权限

工具权限如下：

```text
main_agent:
  - document_read
  - document_edit
  - execute_subagent
  - exec_command

subagents:
  - document_read
  - exec_command
```

约束：

1. 子 agent 不允许调用 `document_edit`。
2. 子 agent 不允许调用 `execute_subagent`。
3. 执行器必须在工具层检查权限。
4. 权限失败返回 `permission_denied`。

## 四、文档 id 与章节语义体系

交底书文档使用系统生成 id 定位，并使用 `type` 表达章节语义。

### 1. section id

`section.id` 使用系统生成的稳定 id，全文唯一。

格式：

```text
sec_000001
sec_000002
sec_000003
```

规则：

1. 新增 section 时由 `document_edit` 自动生成。
2. 替换 section 时保留原 `section_id`。
3. agent 不为新增 section 手写 id。
4. `section.id` 不承载章节语义。

### 2. section type

`section.type` 表示章节在交底书中的语义角色。

标准 type：

```text
title
technical_field
background_technology
existing_solution
existing_solution_defects
technical_problem
technical_solution
key_innovations
embodiments
technical_effects
drawings
claim_suggestions
custom
```

规则：

1. 标准交底书章节使用对应标准 type。
2. 普通子章节使用 `custom`。
3. 系统需要识别“技术方案”等标准章节时，基于 `section.type` 判断，不基于 `section.id` 判断。
4. `section.title` 用于展示，可由用户或 agent 修改。

### 3. block id

`block.id` 使用递增生成 id，全文唯一。

格式：

```text
blk_000001
blk_000002
blk_000003
```

规则：

1. 新增 block 时由 `document_edit` 自动生成。
2. 替换 block 时保留原 `block_id`。
3. agent 不为新增 block 手写 id。
4. `list` item 与 `table` cell 不单独建立 id。

## 五、工具统一返回结构

所有 tool 的输出统一采用外层结构：

```json
{
  "status": "success | failed",
  "output": {}
}
```

说明：

- `status` 表示工具调用层是否成功。
- `output` 表示工具返回结果。
- 命令本身、子 agent 任务本身可能有自己的内层状态。

## 六、document_read 协议

### 通用输入

```json
{
  "action": "get_section"
}
```

`action` 支持：

```text
get_meta
get_project_context
get_outline
get_section
get_block
search_blocks
```

### 1. get_meta

输入：

```json
{
  "action": "get_meta"
}
```

成功输出：

```json
{
  "status": "success",
  "output": {
    "meta": {
      "document_type": "patent_disclosure",
      "schema_version": "v2",
      "title": "一种图像检测方法"
    }
  }
}
```

### 2. get_project_context

`get_project_context` 返回主 agent 决策所需的轻量项目上下文。

输入：

```json
{
  "action": "get_project_context"
}
```

成功输出：

```json
{
  "status": "success",
  "output": {
    "context": {
      "kind": "project_context",
      "document": {
        "title": "一种图像检测方法",
        "outline": [
          {
            "id": "sec_000007",
            "type": "technical_solution",
            "title": "技术方案",
            "children": [
              {
                "id": "sec_000013",
                "type": "custom",
                "title": "处理流程",
                "children": []
              }
            ]
          }
        ]
      }
    }
  }
}
```

说明：

- `outline` 是完全展开的章节树。
- 章节节点只包含 `id`、`type`、`title` 和 `children`。
- 返回内容不包含章节正文、block、前端 anchor、UI 焦点或填充状态。

### 3. get_outline

输入：

```json
{
  "action": "get_outline"
}
```

成功输出：

```json
{
  "status": "success",
  "output": {
    "sections": [
      {
        "id": "sec_000007",
        "type": "technical_solution",
        "title": "技术方案",
        "children": [
          {
            "id": "sec_000013",
            "type": "custom",
            "title": "处理流程"
          }
        ]
      }
    ]
  }
}
```

### 4. get_section

输入：

```json
{
  "action": "get_section",
  "section_id": "sec_000007",
  "include_children": true
}
```

成功输出：

```json
{
  "status": "success",
  "output": {
    "section": {
      "id": "sec_000007",
      "type": "technical_solution",
      "title": "技术方案",
      "blocks": [],
      "children": []
    }
  }
}
```

失败输出：

```json
{
  "status": "failed",
  "output": {
    "code": "section_not_found",
    "message": "section_id 不存在：sec_000007"
  }
}
```

### 5. get_block

输入：

```json
{
  "action": "get_block",
  "block_id": "blk_000001"
}
```

成功输出：

```json
{
  "status": "success",
  "output": {
    "section_id": "sec_000007",
    "block": {
      "id": "blk_000001",
      "type": "paragraph",
      "text": "本发明提供一种图像检测方法。"
    }
  }
}
```

### 6. search_blocks

输入：

```json
{
  "action": "search_blocks",
  "query": "低算力",
  "section_id": "sec_000007"
}
```

说明：

- `query` 为文本查询。
- `section_id` 可选。
- 搜索采用简单文本包含匹配。

成功输出：

```json
{
  "status": "success",
  "output": {
    "matches": [
      {
        "section_id": "sec_000007",
        "block_id": "blk_000001",
        "text": "本发明提供一种适用于低算力设备的图像检测方法。"
      }
    ]
  }
}
```

## 七、document_edit 协议

`document_edit` 接收一组 `operations`，按顺序原子执行。

通用输入：

```json
{
  "operations": []
}
```

通用成功输出：

```json
{
  "status": "success",
  "output": {
    "changed_section_ids": ["sec_000007"],
    "changed_block_ids": ["blk_000014"],
    "operations_applied": 1,
    "primary_section_id": "sec_000007",
    "primary_block_id": "blk_000014",
    "change_scope": "block_appended"
  }
}
```

通用失败输出：

```json
{
  "status": "failed",
  "output": {
    "code": "schema_validation_failed",
    "message": "paragraph block 缺少 text 字段。"
  }
}
```

### 原子写入规则

`document_edit` 的执行流程为：

```text
读取 disclosure.json
-> 构建 id 索引
-> 校验全部 operations
-> 在内存副本上应用全部 operations
-> 校验修改后的整份文档
-> 写入临时文件
-> rename 覆盖 disclosure.json
-> 返回 changed ids 与主定位字段
```

约束：

1. 全部 operation 校验通过后才能写入。
2. 任一 operation 失败时不修改文件。
3. 同一 project 同时只能有一个 `document_edit` 写入。
4. 写入工具不直接执行 SSE 推送或 git commit。

返回字段说明：

- `changed_section_ids`：本次变更影响到的 section id 集合
- `changed_block_ids`：本次新增或替换的 block id 集合
- `operations_applied`：实际成功应用的 operation 数量
- `primary_section_id`：本次变更的主要 section
- `primary_block_id`：本次变更的主要 block，没有则为 `null`
- `change_scope`：本次变更的主要语义范围

`change_scope` 支持：

```text
meta_updated
block_appended
block_replaced
section_blocks_replaced
child_section_appended
section_replaced
```

### 支持的 edit op

支持：

```text
update_meta
replace_section_blocks
append_block
replace_block
append_child_section
replace_section
```

不支持：

```text
delete_block
move_block
delete_section
move_section
insert_block_at
json_patch
merge_json_path
```

### 1. update_meta

输入：

```json
{
  "operations": [
    {
      "op": "update_meta",
      "fields": {
        "title": "一种图像检测方法"
      }
    }
  ]
}
```

说明：

- 只能更新允许的 meta 字段。
- `id_counters` 由系统维护，不由 agent 直接修改。
- `primary_section_id` 为 `null`。
- `primary_block_id` 为 `null`。
- `change_scope` 为 `meta_updated`。

### 2. replace_section_blocks

输入：

```json
{
  "operations": [
    {
      "op": "replace_section_blocks",
      "section_id": "sec_000007",
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

说明：

- 新 blocks 不携带 `id`。
- 工具为每个新 block 生成 id。
- 返回的 `changed_section_ids` 包含目标 section。
- 返回的 `changed_block_ids` 包含全部新生成的 block id。
- `primary_section_id` 等于目标 `section_id`。
- `primary_block_id` 等于第一个新生成的 block id，没有 block 时为 `null`。
- `change_scope` 为 `section_blocks_replaced`。

### 3. append_block

输入：

```json
{
  "operations": [
    {
      "op": "append_block",
      "section_id": "sec_000007",
      "block": {
        "type": "paragraph",
        "text": "本发明的处理流程包括图像获取、特征提取和结果输出。"
      }
    }
  ]
}
```

说明：

- 新 block 不携带 `id`。
- 工具生成 `block_id`。
- `primary_section_id` 等于目标 `section_id`。
- `primary_block_id` 等于新生成的 `block_id`。
- `change_scope` 为 `block_appended`。

### 4. replace_block

输入：

```json
{
  "operations": [
    {
      "op": "replace_block",
      "block_id": "blk_000001",
      "block": {
        "type": "paragraph",
        "text": "本发明提供一种适用于低算力设备的图像检测方法。"
      }
    }
  ]
}
```

说明：

- 替换后的 block 继续使用原 `block_id`。
- 输入 block 不携带 `id`。
- `primary_section_id` 等于该 block 所属 section id。
- `primary_block_id` 等于原 `block_id`。
- `change_scope` 为 `block_replaced`。

### 5. append_child_section

输入：

```json
{
  "operations": [
    {
      "op": "append_child_section",
      "parent_section_id": "sec_000007",
      "section": {
        "type": "custom",
        "title": "处理流程",
        "blocks": [],
        "children": []
      }
    }
  ]
}
```

说明：

- 新 section 不携带 `id`。
- 工具为新 section 生成 `section_id`。
- 只支持两级章节。
- `changed_section_ids` 包含父 section 和新 section。
- `primary_section_id` 等于新 section id。
- `primary_block_id` 等于新 section 内第一个新生成的 block id，没有 block 时为 `null`。
- `change_scope` 为 `child_section_appended`。

### 6. replace_section

输入：

```json
{
  "operations": [
    {
      "op": "replace_section",
      "section_id": "sec_000007",
      "section": {
        "type": "technical_solution",
        "title": "技术方案",
        "blocks": [],
        "children": []
      }
    }
  ]
}
```

说明：

- 替换后的 section 不携带 `id`，工具保留原 `section_id`。
- 标准章节的 `type` 应保持原语义角色。
- section 内新增 block 由工具补齐 id。
- `changed_section_ids` 包含目标 `section_id`。
- `changed_block_ids` 包含替换后 section 树中的全部 block id。
- `primary_section_id` 等于目标 `section_id`。
- `primary_block_id` 等于替换后第一个 block id，没有 block 时为 `null`。
- `change_scope` 为 `section_replaced`。

## 八、document_edit 校验规则

写入前至少校验：

1. 顶层只包含 `meta` 和 `sections`。
2. `section.id` 全文唯一。
3. `block.id` 全文唯一。
4. 落库后的 section 必须包含 `id`、`type`、`title`、`blocks`、`children`。
5. block 必须包含 `id` 和 `type`。
6. block `type` 只能是 `paragraph`、`list`、`image`、`table`。
7. `paragraph` 必须包含 `text`。
8. `list` 必须包含 `ordered` 和 `items`。
9. `image` 必须包含 `src`，可选 `caption` 和 `alt`。
10. `table` 必须包含 `columns` 和 `rows`。
11. 不允许超过两级章节。

## 九、document_edit 错误码

固定错误码：

```text
invalid_action
invalid_operation
section_not_found
block_not_found
duplicate_section_id
duplicate_block_id
schema_validation_failed
permission_denied
write_conflict
io_error
```

## 十、execute_subagent 协议

### 输入

```json
{
  "agent_id": "section_writer",
  "goal": "为“技术方案”章节生成候选正文 blocks。"
}
```

字段说明：

- `agent_id`：调用哪个子 agent
- `goal`：对子 agent 的自然语言任务描述

上下文装配规则：

1. 主 agent 只提供 `agent_id` 和自然语言 `goal`。
2. 上下文管理器自动装配子 agent 的 OpenAI-compatible `messages`。
3. 子 agent 使用自己的 system prompt。
4. 子 agent 继承调用方当前可见且已闭合的 `messages`，不继承调用方 system prompt。
5. 上下文管理器在继承消息之后追加由 `agent_task` barrier 渲染出的任务说明 message。
6. 子 agent 内部工具调用结果只进入本次子 agent run。

### 成功输出：子 agent 成功

```json
{
  "status": "success",
  "output": {
    "agent_id": "section_writer",
    "result": {
      "status": "success",
      "summary": "已生成技术方案章节候选正文。",
      "proposal": {
        "type": "document_edit_proposal",
        "target_section_id": "sec_000007",
        "intent": "replace_section_blocks",
        "confidence": 0.84,
        "rationale": "当前章节适合整体写入候选 blocks。",
        "operations": [
          {
            "op": "replace_section_blocks",
            "section_id": "sec_000007",
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
  }
}
```

### 成功输出：子 agent 任务失败

```json
{
  "status": "success",
  "output": {
    "agent_id": "section_writer",
    "result": {
      "status": "failed",
      "summary": "当前信息不足，无法生成稳定候选正文。",
      "proposal": null,
      "questions": [
        "该方案主要强调检测精度，还是低算力实时性？"
      ],
      "warnings": []
    }
  }
}
```

说明：

- 外层 `status=success` 表示调度工具成功启动并拿到子 agent 结构化结果。
- 内层 `result.status=failed` 表示子 agent 判断本次任务无法完成。

### 失败输出：调度工具失败

```json
{
  "status": "failed",
  "output": {
    "code": "subagent_not_found",
    "message": "不存在的子 agent：foo_writer",
    "agent_id": "foo_writer"
  }
}
```

工具层失败错误码：

```text
subagent_not_found
permission_denied
subagent_timeout
subagent_runtime_error
invalid_subagent_result
```

## 十一、子 agent 统一返回结构

所有子 agent 返回统一外层结构：

```json
{
  "status": "success | failed",
  "summary": "string",
  "proposal": {},
  "questions": [],
  "warnings": []
}
```

字段说明：

- `status`：子 agent 对本次任务的完成状态
- `summary`：给主 agent 的简短结论
- `proposal`：结构化结果
- `questions`：需要补充确认的问题
- `warnings`：风险、假设、不确定性

`proposal.type` 支持：

```text
document_edit_proposal
analysis_result
review_report
```

只有 `document_edit_proposal.operations` 可以作为 `document_edit.operations` 的候选输入，且必须由主 agent 决定是否采纳。

## 十二、exec_command 协议

输入：

```json
{
  "command": "git log --oneline -5"
}
```

成功输出：命令成功

```json
{
  "status": "success",
  "output": {
    "command": "git log --oneline -5",
    "exit_code": 0,
    "stdout": "abc123 update disclosure\n",
    "stderr": ""
  }
}
```

成功输出：命令失败

```json
{
  "status": "success",
  "output": {
    "command": "git log --oneline -5",
    "exit_code": 128,
    "stdout": "",
    "stderr": "fatal: not a git repository"
  }
}
```

失败输出：工具层失败

```json
{
  "status": "failed",
  "output": {
    "code": "invalid_operation",
    "message": "command 字段缺失。"
  }
}
```

语义约定：

- `exec_command.status` 表示工具调用层是否成功返回结果。
- `output.exit_code` 表示命令本身是否执行成功。
- `status=failed` 会作为工具结果返回给 agent 继续处理，不会自动让整个 round 失败。
- `exec_command` 不用于直接修改 `disclosure.json`。

执行约定：

- 默认工作目录为当前 project 工作区根目录。
- 命令工具不做命令白名单限制。
- 命令字符串按 shell 执行，支持管道、重定向、命令拼接等 shell 能力。
- 命令本身执行失败仍返回 `status=success`，并通过 `output.exit_code` 表示。
- 只有调用方无权限、`command` 缺失、运行时异常等工具层问题才返回 `status=failed`。
- 超时时间由 agent 在调用时按任务给出。
- `stdout` 和 `stderr` 的截断策略由 agent 按本轮任务需要决定。

## 十三、自动提交

正文历史版本通过 git 管理。

提交策略：

1. 以一轮会话为提交粒度。
2. 一轮会话指：`用户一次输入 -> 主 agent 完成一次对用户响应`。
3. 如果这一轮会话中发生了文档变更，则在主 agent 完成响应后执行一次 `git commit`。
4. 如果这一轮会话中没有文档变更，则不执行 `git commit`。
5. commit message 基于本轮变更的 section id 和 block id 生成。

提交消息格式：

```text
update disclosure

Time: YYYY-MM-DD HH:mm

Changed sections:
- technical_solution

Changed blocks:
- blk_000014
- blk_000015
```

说明：

- `Changed sections` 列出本轮变更涉及的 section id。
- `Changed blocks` 列出本轮新增或替换的 block id。
- id 列表在写入 commit message 前必须去重。
- 每组 id 最多保留 10 条。
- 如果发生截断，需要说明剩余未展示数量。

截断示例：

```text
update disclosure

Time: 2026-04-23 18:10

Changed sections:
- technical_solution
- embodiments

Changed blocks:
- blk_000014
- blk_000015

Truncated: only top 10 changed block ids are shown, 4 more block ids omitted.
```

## 十四、设计结论

工具体系采用以下原则：

1. 文档定位基于 `section_id` 和 `block_id`。
2. 文档读取统一走 `document_read`。
3. 当前项目标题和完整目录树通过 `document_read(action=get_project_context)` 获取。
4. 文档写入统一走 `document_edit`。
5. `document_edit` 是 `disclosure.json` 的唯一写入入口。
6. 子 agent 通过 `execute_subagent` 启动。
7. 子 agent 返回 proposal，主 agent 决定是否采纳。
8. 通用命令统一走 `exec_command`，但不直接修改交底书真相源。
9. 自动提交基于 changed section ids 和 changed block ids 生成 commit message。
