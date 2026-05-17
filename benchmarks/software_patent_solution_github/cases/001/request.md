# 技术方案需求

当前项目希望支持将 OpenCode 作为一种原生 agent runtime 接入 Mission Control。

用户已经在本地使用 OpenCode 完成了一些 agent 工作，希望 Mission Control 能像管理其他 agent runtime 一样，识别并管理这些 OpenCode 工作记录。用户期望在 Mission Control 中看到真实的 OpenCode 会话，并在条件允许时继续使用这些会话，而不是只能把 OpenCode 当作一个外部工具或手工记录。

该能力应尽量融入 Mission Control 已有的 agent 管理体验，同时保持对现有 Claude、Codex、Hermes 等运行方式的兼容。如果 OpenCode 的某些能力与现有 runtime 不完全相同，系统也应避免给用户造成“所有操作都已经完整支持”的误解。

请生成一个可用于专利交底书的软件技术方案草稿。方案应说明系统如何支持这种新的本地 agent runtime 能力，以及相比现有方式能带来什么技术效果。

## 补充约束

请不要假设 OpenCode 的本地状态目录、消息文件格式或继续命令在当前项目中已经被实现。若方案需要读取 OpenCode 本地状态，应说明可配置发现、适配器合约、兼容式解析和 fixture/命令替身验证方式；若某些能力暂不可确认，应在 UI/API 能力边界中明确声明，避免把未知能力展示为已完整支持。
