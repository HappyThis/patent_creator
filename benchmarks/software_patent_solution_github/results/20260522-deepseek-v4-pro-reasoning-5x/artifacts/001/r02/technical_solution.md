## 技术方案

本方案描述一种多运行时（multi-runtime）Agent 管理系统中将本地 Agent 运行时（如 OpenCode/OpenClaw）作为一等运行时（native runtime）接入的方法。该系统通过统一的适配器合约、可配置的发现机制、兼容式会话解析以及能力边界声明，使得异构本地 Agent 运行时的会话记录能够被统一识别、聚合展示和在条件允许时继续使用，同时保持对已有 Claude Code、Codex CLI、Hermes 等运行时的兼容。

### 要解决的技术问题

现有的 Agent 管理系统通常将不同 Agent 运行时（如 Claude Code、Codex CLI、OpenCode/OpenClaw）视为独立的外部工具，缺少统一的本地运行时识别和管理能力。这导致以下问题：（1）用户在本地使用不同 Agent 工具完成的工作记录分散在不同位置，无法在统一界面中查看和管理；（2）不同运行时的数据格式和存储位置没有统一抽象，接入新运行时需要大量定制开发；（3）当新运行时（如 OpenCode）的某些能力与已有运行时不完全相同时，系统缺乏能力边界声明机制，容易给用户造成'所有操作都已完整支持'的误解。

### 适配器合约与统一管理

系统在管理层采用统一的适配器合约（FrameworkAdapter 接口规范），为每种 Agent 运行时提供一个标准化的适配器实现。每个适配器封装该运行时特有的注册、心跳、任务上报、任务分配查询和断连操作。已注册的适配器包括 openclaw、claude-sdk、crewai、langgraph、autogen 和 generic 等。适配器工厂根据运行时标识动态创建对应实例，新增运行时只需实现合约接口即可接入。

### 可配置的运行时发现机制

系统通过可配置的探测（detection）机制发现本地已安装的 Agent 运行时。对 OpenCode/OpenClaw 运行时，探测路径由环境变量 OPENCLAW_STATE_DIR、OPENCLAW_HOME、OPENCLAW_CONFIG_PATH 等联合确定，最终指向本地状态目录（如 ~/.openclaw）。探测过程检查：配置文件（openclaw.json）是否存在、二进制文件（openclaw --version）是否可执行、网关端口是否可达。探测结果包含 installed（已安装）、version（版本）、running（运行中）、authenticated（已认证）等状态字段。

### 兼容式会话状态解析

系统从 OpenCode/OpenClaw 本地状态目录读取会话记录，采用兼容式解析策略。会话数据按 agent 维度组织，存储在 {OPENCLAW_STATE_DIR}/agents/{agentName}/sessions/sessions.json 中。每个文件为 JSON 对象，key 为会话键（如 'agent:engineering-bot:main'），value 包含 sessionId、updatedAt、chatType、model、totalTokens、inputTokens、outputTokens、contextTokens 等字段。解析器不假定所有字段都存在——对缺失字段使用默认值（如 chatType 默认为 'unknown'、model 按 object.primary 回退），避免因格式演进导致读取失败。

### 多源会话聚合与继续

系统将来自不同运行时来源的会话数据统一聚合为标准化会话列表。每个会话记录携带 source 字段标识来源（'gateway' 表示 OpenClaw 网关会话、'local' 表示 Claude/Codex/Hermes 本地会话）。聚合时按 'source:id' 组合键去重，按 lastActivity 倒序排列。对于会话继续（continue）操作，系统当前支持 claude-code 和 codex-cli 两种类型，通过调用对应 CLI 命令（claude --resume 或 codex exec resume）执行。OpenCode 会话的继续能力因依赖其本地状态目录结构和继续命令的可用性，在能力边界中单独声明。

### 能力边界声明与 UI 自适应

系统引入能力边界（capability boundary）机制，在 UI 和 API 层面明确声明不同运行时当前支持的操作范围。通过 /api/status?action=capabilities 端点返回 openclawHome（OpenClaw 状态目录是否可访问）、gateway（网关是否可达）、claudeSessions（活跃 Claude 会话数）等能力标志。前端组件根据能力标志动态调整界面：例如，当 OpenClaw 状态目录不可访问时，对应的会话继续按钮不显示；当网关不可达时，网关控制面板显示受限状态。这一机制确保系统不会将未确认可用的能力展示为已完整支持，避免误导用户。

### 离线 Fixture 验证机制

系统支持离线测试模式（offline harness），通过 fixture 文件和命令替身（command stand-in）在未安装 OpenCode 的环境中验证集成逻辑。测试夹具（fixtures/openclaw/）包含模拟的 openclaw.json 配置文件、agent 目录结构和 sessions.json 会话数据。能力探测端点（/api/status?action=capabilities）在检测到 fixture 目录存在时返回 openclawHome=true，后续的会话 API、网关配置 API 和 cron API 均可基于 fixture 数据正常工作。这种设计使得：新增运行时的适配器和解析逻辑可以在 CI 环境中独立验证，而不依赖该运行时的实际安装；同时确保本地模式下即使运行时未安装，系统仍能基于已有状态文件提供只读会话浏览能力。

### 本地 Agent 双向同步

系统支持从本地磁盘目录主动发现 Agent 定义（本地 Agent 同步）。扫描范围包括 ~/.agents/、~/.codex/agents/、~/.claude/agents/、~/.hermes/skills/ 等目录。一个目录被识别为 Agent 的条件是包含 soul.md、AGENT.md、agent.md、identity.md、SKILL.md 或 config.json、agent.json 等标记文件。发现后提取 role（从文件首行或 'role:'/'theme:' 字段）、计算内容哈希（SHA-256），与数据库中的现有记录比较：新增的插入、内容变更的更新、磁盘上消失的标记为离线。同时支持反向写入——用户在 MC 界面中编辑 Agent 的 soul 内容后，系统将内容写回磁盘对应的 soul.md 或 AGENT.md 文件，保持双向同步。

### 运行时健康诊断

系统集成了 OpenClaw Doctor 诊断工具的输出解析能力。执行 openclaw doctor 命令后，解析器以去 ANSI 转义码、分类提取的方式识别诊断级别（healthy/warning/error）和类别（config/state/security/general）。解析器过滤掉装饰性输出（边框字符、banner）、会话老化提示行（不影响健康的正常状态）和外部状态目录警告行（当目录路径与当前配置不一致时自动剥离），仅保留可操作的诊断问题。诊断结果支持展示可修复（canFix）标志，前端据此决定是否显示'一键修复'按钮。

### 技术效果

本方案带来的技术效果包括：（1）扩展性——通过统一适配器合约，新增 Agent 运行时只需实现标准接口，无需修改核心会话管理、状态聚合或 UI 逻辑；（2）健壮性——兼容式解析和默认值回退策略使系统能容忍不同运行时数据格式的差异和演进，不会因单个字段缺失而中断整个会话列表的展示；（3）用户透明度——能力边界声明机制确保 UI 展示的能力与实际可用能力一致，避免功能误展示；（4）可测试性——离线 fixture + 命令替身体系使新运行时的接入验证可脱离实际安装环境，在 CI 中独立完成回归测试；（5）数据一致性——基于内容哈希的变更检测和双向同步机制保证 MC 数据库与本地磁盘状态的一致性。

### 关键处理流程

系统处理新运行时接入的典型流程为：（1）探测阶段——系统启动时或通过 API 触发，依次检查各运行时的二进制可达性、配置文件存在性和端口监听状态，生成 RuntimeStatus 列表；（2）会话发现阶段——系统扫描 OpenClaw 状态目录下的 agents/*/sessions/sessions.json 文件，同时对 Claude Code、Codex CLI、Hermes 等运行时执行各自的会话扫描逻辑；（3）聚合阶段——将来自 gateway 源和 local 源的会话按 source:id 组合键去重，按最后活动时间排序，合并为统一会话列表返回给前端；（4）能力声明阶段——单独调用 /api/status?action=capabilities 端点，返回各能力标志供前端自适应渲染；（5）同步阶段——支持从 openclaw.json 配置文件和本地磁盘目录两个来源同步 Agent 定义到 MC 数据库，并支持反向写回。

### 风险与待确认问题

当前方案存在以下待确认风险点：（1）OpenCode 会话继续——当前 sessions/continue 接口仅支持 claude-code 和 codex-cli 两种 kind，OpenCode 会话的继续能力取决于其本地 CLI 是否提供 --resume 或等价命令，以及其会话状态是否可被外部进程安全读取和修改；在确认前，UI 应将 OpenCode 会话的继续按钮标记为受限或隐藏；（2）消息格式兼容性——OpenCode 的消息存储格式若与当前 sessions.json 的 JSON 结构不完全一致，兼容式解析器需要通过字段映射层进行适配，适配规则需随 OpenCode 版本演进持续维护；（3）并发访问安全——多个进程（MC 和 OpenCode CLI）同时读写 sessions.json 时可能存在竞争条件，需评估是否引入文件锁或原子写入机制；（4）认证集成——OpenCode 若使用与已有运行时不同的认证机制（如 OAuth、API Key、设备码），其 authenticated 状态检测逻辑需单独实现。
