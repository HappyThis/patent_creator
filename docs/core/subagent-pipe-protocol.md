# 子 Agent 管道协议

## 文档定位

本文档定义子 agent 向主 agent 传递结果并结束工作的当前管道协议。

该协议只定义新增的结果传输能力，不定义子 agent 的完整工具集。子 agent 是否还能调用 `document_read`、`exec_command` 或其他工作工具，仍由子 agent 声明和执行器权限配置决定。

相关文档：

- [子 Agent 定义](subagents.md)
- [Agent 基本设计原则](agent-principles.md)
- [Agent Prompt 与上下文规范](agent-prompt-context-spec.md)
- [一轮内部时序](../specs/round-lifecycle.md)

## 一、为什么需要管道协议

此前的子 agent 完成方式要求模型一次性提交复杂结构化结果。

该方案在复杂任务中暴露出三个问题：

1. 模型心智负担过重，需要同时完成业务任务和复杂协议填充。
2. 大正文或长分析被塞入嵌套 JSON，容易出现非法 JSON、参数字符串化或字段缺失。
3. 不同模型提供商对 tool calling 的强制能力不同，不能稳定依赖强制工具调用策略。

当前协议的目标是：

1. 尽可能降低模型心智负担。
2. 尽可能减少大 JSON 和复杂 JSON 输出次数。
3. 将结果内容传输与任务结束信号拆开。
4. 保持 OpenAI-compatible tool call / tool result 上下文协议。
5. 让主 agent 继续负责解释、采纳和落盘。

## 二、当前方案特点

| 维度 | 当前方案 |
| --- | --- |
| 内容传输 | `write_pipe(content)` 少量多次写入字符串 |
| 结束信号 | `finish({})` 是唯一显式结束信号 |
| 大内容承载 | 作为普通字符串进入 pipe |
| 子 agent 是否生成 operations | 默认不生成，主 agent 解释和落盘 |
| 模型需要记忆的协议 | 只需记住“写内容用 write_pipe，结束用 finish” |
| provider 依赖 | 依赖标准 tool call / tool result |
| 主 agent 接收内容 | 合并后的字符串内容 |

因此，当前管道协议是子 agent 结果传输的正式目标协议。

## 三、协议边界

每次 `execute_subagent` 启动时，执行器为本次子 agent run 创建一个内存 pipe。

管道协议只新增两个工具：

```text
write_pipe(content)
finish({})
```

其他工具不属于本协议范围。

执行器必须保证：

1. pipe 只在本次 `execute_subagent` 调用内有效。
2. pipe 按 `write_pipe` 调用顺序追加内容。
3. 每次 `write_pipe` 都生成标准 tool result，回到子 agent 上下文。
4. 收到 `finish({})` 后结束子 agent loop。
5. 结束时将 pipe 内容按顺序用 `\n` 拼接，作为 `execute_subagent` 的结果返回给主 agent。

## 四、write_pipe

`write_pipe` 是唯一内容通道。所有需要展示给主 agent 的内容都必须写入 pipe。

参数：

```json
{
  "content": "一小段 markdown 或纯文本"
}
```

规则：

1. `content` 必须是字符串。
2. 内容可以是 Markdown 或纯文本。
3. 子 agent 可以在任何时候调用 `write_pipe`。
4. 鼓励少量多次，避免一次写入巨大内容。
5. 子 agent 不应通过普通 assistant 文本向主 agent 交付结果。
6. 子 agent 不应在 `write_pipe` 中输出复杂嵌套 JSON，除非任务本身就是生成 JSON 示例。

执行器内部处理：

```text
pipe.parts.append(content)
```

合并规则：

```text
content = "\n".join(pipe.parts)
```

## 五、write_pipe ack

每次 `write_pipe` 必须产生标准 tool result，并进入子 agent 上下文。

ack 的目标是确认写入、提供进度、提示下一步，而不是重复全部历史内容或承载语义总结。

暂定 ack 结构：

```json
{
  "status": "ok",
  "part_index": 2,
  "written_chars": 1240,
  "total_chars": 2860,
  "stored_preview": "本次写入内容的可控预览，短则完整，长则截断...",
  "next": "如果还有内容，继续调用 write_pipe；如果已经完成，调用 finish({})。"
}
```

字段说明：

- `status`：固定为 `ok`，表示本次写入成功。
- `part_index`：从 1 开始的写入序号。
- `written_chars`：本次写入字符数。
- `total_chars`：当前 pipe 累计字符数。
- `stored_preview`：本次写入内容的可控预览；短内容可完整返回，长内容应截断。
- `next`：固定提示下一步动作，降低模型跑偏概率。

ack 约束：

1. 不返回完整 pipe。
2. 不重复历史所有内容。
3. 不对内容做二次总结。
4. 不引入 `ack_mode` 等额外参数，先保持协议最小化。

## 六、finish

`finish` 是唯一显式结束信号。

参数固定为空对象：

```json
{}
```

规则：

1. `finish` 不接收任何内容参数。
2. `finish` 不包含 `summary`、`questions`、`warnings` 或其他业务字段。
3. `finish` 只表示当前子 agent 工作结束。
4. 执行器收到 `finish` 后不再让该子 agent 继续生成。
5. 如果 pipe 为空，执行器仍可结束，但返回给主 agent 的 `content` 为空字符串。

`finish` 的 tool result 可以很轻：

```json
{
  "status": "done",
  "parts": 5,
  "total_chars": 6840
}
```

该 tool result 主要用于日志完整性；执行器收到 `finish` 后立即收束子 agent run。

## 七、主 agent 接收结果

执行器将 pipe 合并后，作为 `execute_subagent` 的 tool result 返回给主 agent。

目标结构：

```json
{
  "status": "success",
  "output": {
    "agent_id": "solution_refiner",
    "content": "part 1\npart 2\npart 3"
  }
}
```

主 agent 的职责：

1. 阅读 `content`。
2. 判断是否采纳。
3. 自行决定是否继续追问、继续拆分任务或调用 `document_edit`。
4. 不假定子 agent 已经生成可直接落盘的 `document_edit.operations`。

## 八、上下文协议

管道协议必须遵守 OpenAI-compatible tool call 上下文链路。

示例：

```text
assistant tool_calls: write_pipe({"content":"第一段分析..."})
tool result: {"status":"ok","part_index":1,...}

assistant tool_calls: write_pipe({"content":"第二段分析..."})
tool result: {"status":"ok","part_index":2,...}

assistant tool_calls: finish({})
tool result: {"status":"done","parts":2,"total_chars":...}
```

每一次 assistant tool call 后都必须有对应 tool result。执行器不得只更新内部 pipe 而不回填 tool result。

## 九、provider 兼容性

管道协议的基础能力是标准 tool call / tool result，因此适用于 OpenAI-compatible provider。

不同 provider 的差异仍需要由运行时 profile 处理：

1. 支持强制 tool call 的 provider，可以在子 agent 阶段使用更强的 `tool_choice` 策略。
2. 只能可靠支持 `tool_choice=auto` 的 provider，仍可能直接返回 assistant 文本。
3. provider 差异不应改变主 agent 接收结果的结构。

对于直接 assistant 文本的 fallback，本文档只定义目标方向，不强制实现细节：

```text
如果 provider 无法强制工具调用，执行器可以选择将直接文本吸收为一次隐式 write_pipe 后结束，或给一次纠错机会。
```

具体策略应由执行器实现文档或 provider profile 另行定义。

## 十、实现原则

落实当前管道协议时遵守以下原则：

1. 子 agent 输出内容只通过 `write_pipe(content)`。
2. 子 agent prompt 不要求模型填写复杂结构化结果。
3. 子 agent 不承担生成 `document_edit.operations` 的默认责任。
4. 主 agent prompt 强调阅读子 agent `content` 后自行决策。
5. benchmark 评分仍只看最终 `disclosure.json` 中的技术方案章节，不评价 pipe 内部过程。

## 十一、设计结论

管道协议的核心结论是：

1. 子 agent 输出内容走 `write_pipe(content)`。
2. 子 agent 结束工作走 `finish({})`。
3. `finish` 不承载任何业务内容。
4. pipe 内容按字符串拼接后进入主 agent 上下文。
5. 主 agent 负责解释、筛选、结构化和落盘。
6. 协议只新增 `write_pipe` 和 `finish`，不定义子 agent 的完整工具权限。
