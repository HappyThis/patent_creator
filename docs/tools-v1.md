# Tools 设计 v1

## 文档定位

本文档定义本项目 v1 阶段建议提供的工具集合。

它建立在以下文档之上：

- [Agent 基本设计原则 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles-v1.md)
- [Agent Prompt 与上下文规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-prompt-context-spec-v1.md)
- [子 Agent 定义 v1](/Users/yangchaoqun/myProj/patent_creator/docs/subagents-v1.md)

本文档当前只定义：

- 工具清单
- 工具职责
- 当前阶段的取舍结论
- 工具输入输出协议方案

本文档暂不定义：

- 每个工具的底层实现细节

## 一、总体原则

v1 工具设计遵循以下原则：

1. 工具尽量少而清晰
2. 交底书中间结果使用专用工具
3. 其余通用能力尽量统一收敛
4. 底层实现可以通过命令行完成
5. 主 agent 与子 agent 都可以调用工具

## 二、当前建议工具清单

### 1. JSON 文档工具

- `get_json_tree`
- `read_json_path`
- `write_json_path`

说明：

- 这些工具围绕交底书 JSON 文档工作
- 路径语义基于 JSON Pointer
- `write_json_path` 在 v1 中先按整值替换理解

### 2. 子 agent 调用工具

- `execute_subagent`

说明：

- 子 agent 在概念层仍然是 agent
- 但在调用层统一通过工具方式触发

### 3. 通用命令工具

- `exec_command`

说明：

- `exec_command` 用于执行通用命令行操作
- 本地文件读取、目录浏览、git 操作等通用能力统一收敛到这个工具
- 不再拆成 `list_files`、`read_file`、`git_log` 等细粒度独立工具
- 交底书 JSON 中间结果读写仍然保留专用工具，不并入该工具

## 三、关于自动提交

当前结论：

- `exec_command` 可以承载 git 相关操作
- 自动提交策略已初步确定

当前策略：

1. 以“一轮会话”为提交粒度
2. 一轮会话指：`用户一次输入 -> 主 agent 完成一次对用户响应`
3. 如果这一轮会话中发生了中间文件变更，则在主 agent 完成响应后执行一次 `git commit`
4. 如果这一轮会话中没有发生文件变更，则不执行 `git commit`
5. `git commit` 的消息不做自由摘要生成，而是直接基于这一轮写入过的 pointer 路径生成

说明：

- 这里的“中间文件变更”主要指交底书文档或相关项目文件被实际写入
- 不要求每次单个工具写入后立刻提交
- 提交点在一轮会话结束时统一触发一次

提交消息格式：

```text
update disclosure

Time: YYYY-MM-DD HH:mm

Change pointers:
- /pointer1
- /pointer2
- /pointer3
```

说明：

- 第一行固定为 `update disclosure`
- 时间使用人类可读时间
- `Change pointers` 直接列出这一轮内所有实际写入过的 JSON Pointer 路径
- pointer 列表在写入 commit message 前必须先去重
- pointer 列表按修改量排序，修改量可按对应写入内容的字符长度近似衡量
- commit message 中最多保留 10 条 pointer
- 如果发生截断，需要在 commit message 中说明截断原因以及剩余未展示数量
- 不额外依赖自然语言摘要生成

截断示例：

```text
update disclosure

Time: 2026-04-23 18:10

Change pointers:
- /sections/6
- /sections/7/children/1
- /meta/title

Truncated: only top 10 pointers are shown, 4 more pointers omitted due to message length policy.
```

## 四、后续待讨论项

1. `execute_subagent` 的返回结果是否还需要进一步收敛
2. `exec_command` 的执行边界
3. commit message 中截断说明的最终固定文案是否需要进一步统一

## 五、工具输入输出协议方案

### 通用规则

所有 tool 的输入统一为 JSON object。

所有 tool 的输出统一采用外层结构：

```json
{
  "status": "success | failed",
  "output": {}
}
```

说明：

- `status` 表示工具调用层是否成功
- `output` 表示工具返回结果
- 对工具来说，`output` 可以是结构化对象
- 对子 agent 来说，最终工作结论仍然是自然语言字符串

当前工作区与当前交底书文档默认都是隐式对象，不需要在每次调用时额外传入。

### 1. get_json_tree

#### 输入

```json
{}
```

#### 成功输出

```json
{
  "status": "success",
  "output": {
    "tree": {
      "path": "/",
      "type": "object",
      "children": [
        {
          "key": "meta",
          "path": "/meta",
          "type": "object"
        },
        {
          "key": "sections",
          "path": "/sections",
          "type": "array"
        }
      ]
    }
  }
}
```

#### 失败输出

```json
{
  "status": "failed",
  "output": {
    "message": "当前交底书文档不存在或无法解析为 JSON。"
  }
}
```

### 2. read_json_path

#### 输入

```json
{
  "path": "/sections/0/title"
}
```

说明：

- `path` 使用 JSON Pointer

#### 成功输出

```json
{
  "status": "success",
  "output": {
    "path": "/sections/0/title",
    "value": "技术方案"
  }
}
```

#### 失败输出

```json
{
  "status": "failed",
  "output": {
    "path": "/sections/0/title",
    "message": "指定路径不存在。"
  }
}
```

### 3. write_json_path

#### 输入

```json
{
  "path": "/sections/0/title",
  "value": "改进后的技术方案"
}
```

v1 规则：

- 只做整值替换
- 不做 merge
- 不做 append
- 路径必须已存在

#### 成功输出

```json
{
  "status": "success",
  "output": {
    "path": "/sections/0/title",
    "written": true
  }
}
```

#### 失败输出

```json
{
  "status": "failed",
  "output": {
    "path": "/sections/0/title",
    "message": "指定路径不存在，无法写入。"
  }
}
```

### 4. execute_subagent

#### 输入

```json
{
  "agent_id": "solution_refiner",
  "goal": "将当前讨论内容整理成一个更完整的技术方案，并指出仍需用户确认的关键决策。",
  "call_type": "rich_context_specialist"
}
```

字段说明：

- `agent_id`：调用哪个子 agent
- `goal`：对子 agent 的自然语言任务描述
- `call_type`：本次调用采用哪种上下文装配策略

#### 成功输出（子 agent 成功）

```json
{
  "status": "success",
  "output": {
    "agent_id": "solution_refiner",
    "result": {
      "status": "success",
      "output": "我已将当前讨论内容整理为一个可写作的技术方案，当前仍需确认是否强调轻量化网络结构。"
    }
  }
}
```

#### 成功输出（子 agent 失败）

```json
{
  "status": "success",
  "output": {
    "agent_id": "solution_refiner",
    "result": {
      "status": "failed",
      "output": "当前信息不足，无法稳定收敛技术方案。缺少现有技术缺陷描述。"
    }
  }
}
```

#### 失败输出（工具层失败）

```json
{
  "status": "failed",
  "output": {
    "agent_id": "solution_refiner",
    "message": "子 agent 调用失败或未能启动。"
  }
}
```

### 5. exec_command

#### 输入

```json
{
  "command": "git log --oneline -5"
}
```

说明：

- `command` 是要执行的命令字符串

#### 成功输出（命令成功）

```json
{
  "status": "success",
  "output": {
    "command": "git log --oneline -5",
    "exit_code": 0,
    "stdout": "abc123 feat: update disclosure\n...",
    "stderr": ""
  }
}
```

#### 成功输出（命令失败）

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

#### 失败输出（工具层失败）

```json
{
  "status": "failed",
  "output": {
    "message": "command 字段缺失。"
  }
}
```

#### 语义约定

`exec_command.status` 表示的是：

- 工具调用层是否成功返回结果

它不直接表示命令本身是否执行成功。

命令本身是否执行成功，应通过以下字段判断：

- `output.exit_code`

也就是说：

- `status=success` 代表命令已经被执行，并且工具成功返回了执行结果
- `exit_code=0` 代表命令执行成功
- `exit_code!=0` 代表命令执行失败
- `status=failed` 只用于工具层失败，例如参数错误、执行器异常、拒绝执行等场景

## 六、当前结论

v1 当前建议保留以下核心工具：

- `get_json_tree`
- `read_json_path`
- `write_json_path`
- `execute_subagent`
- `exec_command`

其中：

- 交底书 JSON 中间结果由专用工具负责
- 文件、目录、git 等通用能力统一走 `exec_command`
- `write_json_path` 在当前阶段只做整值替换
- `exec_command` 当前阶段不再细化
- 如果一轮会话中发生了文件变更，则在主 agent 完成响应后提交一次 `git commit`
