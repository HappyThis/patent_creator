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
2. `document_replace_section_blocks`
3. `document_append_block`
4. `document_replace_block`
5. `document_append_child_section`
6. `document_clear_section_blocks`
7. `execute_subagent`
8. `exec_command`

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

### 2. document_replace_section_blocks

`document_replace_section_blocks` 用于替换指定章节的正文 blocks，不改变章节标题和子章节。

适合：

- 写入根章节总述
- 重写某个章节的短正文 blocks
- 清晰覆盖当前章节正文

### 3. document_append_block

`document_append_block` 用于向指定章节末尾追加一个 block。

适合：

- 小步追加段落
- 小步追加列表、图片或表格
- 将较长正文拆成多次写入

### 4. document_replace_block

`document_replace_block` 用于替换指定 block 的内容，并保留原 `block_id`。

适合：

- 小范围改写已有段落
- 修正已有列表、图片或表格
- 保持文档定位稳定

### 5. document_append_child_section

`document_append_child_section` 用于在指定父章节下追加一个 `custom` 子章节。

适合：

- 在“技术方案”下追加短子章节
- 写入“整体架构”“处理流程”“关键模块”等局部结构
- 先建立短标题和正文，再通过 `document_append_block` 继续补充

### 6. document_clear_section_blocks

`document_clear_section_blocks` 用于清空指定章节的正文 blocks。

它只清空 blocks，不删除章节节点，也不删除子章节。

### 7. execute_subagent

`execute_subagent` 是子 agent 调度工具。

它负责：

- 启动指定子 agent
- 触发上下文管理器自动装配子 agent `messages`
- 返回子 agent 的统一结果结构

`execute_subagent` 只能由主 agent 调用。

### 8. exec_command

`exec_command` 用于执行通用命令行操作。

适合：

- 文件浏览
- 调试命令
- git 操作
- 非交底书真相源的辅助处理

不适合：

- 直接修改 `disclosure.json`
- 绕过文档写入工具执行交底书正文写入

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
  - document_replace_section_blocks
  - document_append_block
  - document_replace_block
  - document_append_child_section
  - document_clear_section_blocks
  - execute_subagent
  - exec_command

subagents:
  - document_read
  - exec_command
```

约束：

1. 子 agent 不允许调用文档写入工具。
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

1. 新增 section 时由文档写入工具自动生成。
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

1. 新增 block 时由文档写入工具自动生成。
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

## 七、文档写入工具协议

文档写入工具是交底书文档的唯一写入入口。所有写入工具都由主 agent 调用，执行器负责权限检查、参数校验、正文长度检查、文档 schema 校验和落盘。

通用成功输出：

```json
{
  "status": "success",
  "output": {
    "changed_section_ids": ["sec_000007"],
    "changed_block_ids": ["blk_000014"],
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

文档写入工具的执行流程为：

```text
读取 disclosure.json
-> 构建 id 索引
-> 校验工具参数
-> 在内存副本上应用写入
-> 校验修改后的整份文档
-> 保存 disclosure.json
-> 返回 changed ids 与主定位字段
```

约束：

1. 参数和文档 schema 校验通过后才能写入。
2. 任一校验失败时不修改文件。
3. 单次正文写入总量不得超过 1500 字。
4. 写入工具不直接执行 SSE 推送或 git commit。

返回字段说明：

- `changed_section_ids`：本次变更影响到的 section id 集合
- `changed_block_ids`：本次新增或替换的 block id 集合
- `primary_section_id`：本次变更的主要 section
- `primary_block_id`：本次变更的主要 block，没有则为 `null`
- `change_scope`：本次变更的主要语义范围

`change_scope` 支持：

```text
block_appended
block_replaced
section_blocks_replaced
child_section_appended
```

### 1. document_replace_section_blocks

输入：

```json
{
  "section_id": "sec_000007",
  "blocks": [
    {
      "type": "paragraph",
      "text": "本发明提供一种图像检测方法。"
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

### 2. document_append_block

输入：

```json
{
  "section_id": "sec_000007",
  "block": {
    "type": "paragraph",
    "text": "本发明的处理流程包括图像获取、特征提取和结果输出。"
  }
}
```

说明：

- 新 block 不携带 `id`。
- 工具生成 `block_id`。
- `primary_section_id` 等于目标 `section_id`。
- `primary_block_id` 等于新生成的 `block_id`。
- `change_scope` 为 `block_appended`。

### 3. document_replace_block

输入：

```json
{
  "block_id": "blk_000001",
  "block": {
    "type": "paragraph",
    "text": "本发明提供一种适用于低算力设备的图像检测方法。"
  }
}
```

说明：

- 替换后的 block 继续使用原 `block_id`。
- 输入 block 不携带 `id`。
- `primary_section_id` 等于该 block 所属 section id。
- `primary_block_id` 等于原 `block_id`。
- `change_scope` 为 `block_replaced`。

### 4. document_append_child_section

输入：

```json
{
  "parent_section_id": "sec_000007",
  "title": "处理流程",
  "blocks": [
    {
      "type": "paragraph",
      "text": "本发明的处理流程包括图像获取、特征提取和结果输出。"
    }
  ]
}
```

说明：

- 新 section 的 `type` 固定为 `custom`。
- 工具为新 section 生成 `section_id`，agent 不提供 id 或 children。
- 只支持两级章节。
- `changed_section_ids` 包含父 section 和新 section。
- `primary_section_id` 等于新 section id。
- `primary_block_id` 等于新 section 内第一个新生成的 block id，没有 block 时为 `null`。
- `change_scope` 为 `child_section_appended`。

### 5. document_clear_section_blocks

输入：

```json
{
  "section_id": "sec_000007"
}
```

说明：

- 只清空目标 section 的 blocks，不删除 section 节点或 children。
- `changed_section_ids` 包含目标 `section_id`。
- `changed_block_ids` 为空。
- `primary_section_id` 等于目标 `section_id`。
- `primary_block_id` 为 `null`。
- `change_scope` 为 `section_blocks_replaced`。

## 八、文档写入校验规则

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

## 九、文档写入错误码

固定错误码：

```text
section_not_found
block_not_found
duplicate_section_id
duplicate_block_id
schema_validation_failed
edit_too_large
invalid_tool_arguments
permission_denied
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
    "content": "## 局部候选正文\n\n本发明提供一种图像检测方法。"
  }
}
```

说明：

- 外层 `status=success` 表示调度工具成功启动并拿到子 agent pipe 内容。
- `output.content` 是子 agent 通过 `write_pipe(content)` 少量多次写入后合并得到的字符串。
- 如果子 agent 判断信息不足，也应把缺口或待确认问题写入 `content` 后调用 `finish({})`。
- 主 agent 读取 `content` 后自行决定是否采纳、追问、继续拆分任务或调用文档写入工具。

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

## 十一、子 agent pipe 工具

子 agent 结果传输只使用两个管道工具：

```text
write_pipe(content)
finish({})
```

规则：

1. 所有要展示给主 agent 的内容都必须写入 `write_pipe(content)`。
2. `content` 是字符串，可以是 Markdown 或纯文本。
3. 子 agent 可以多次写入，执行器按顺序用 `\n` 拼接。
4. `finish({})` 不接收任何业务参数，只表示本次子 agent run 结束。
5. 子 agent 不生成最终文档写入参数作为默认责任。

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
    "code": "invalid_tool_arguments",
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
4. 文档写入统一走五个专用写入工具：`document_replace_section_blocks`、`document_append_block`、`document_replace_block`、`document_append_child_section`、`document_clear_section_blocks`。
5. 文档写入工具是 `disclosure.json` 的唯一写入入口。
6. 子 agent 通过 `execute_subagent` 启动。
7. 子 agent 通过 pipe 返回分析、骨架或局部候选正文，主 agent 决定是否采纳。
8. 通用命令统一走 `exec_command`，但不直接修改交底书真相源。
9. 自动提交基于 changed section ids 和 changed block ids 生成 commit message。
