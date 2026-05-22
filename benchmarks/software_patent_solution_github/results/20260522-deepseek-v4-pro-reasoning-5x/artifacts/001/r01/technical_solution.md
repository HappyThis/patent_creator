## 技术方案

本方案描述如何在 Mission Control 的多 agent 编排体系中，将 OpenCode 作为一种原生 agent runtime 接入，使其具备与已有 OpenClaw、Hermes、Claude、Codex 等运行时同等的管理体验。方案围绕 OpenCode 的本地状态目录、消息格式、会话继续命令等关键细节尚不明确的前提，以可配置发现、适配器合约、兼容式解析和 fixture/命令替身验证为核心手段，在保持对现有运行时完全兼容的前提下，实现 OpenCode agent 的发现、注册、会话展示和会话继续等能力。

### 要解决的技术问题

Mission Control 现有 RuntimeId 体系涵盖 openclaw、hermes、claude、codex 四种运行时，分别对应内置网关、自学习 agent、Anthropic CLI 和 OpenAI CLI。每种运行时均通过硬编码的检测逻辑、已知的安装路径和固定的 CLI 命令完成集成。OpenCode 作为一种新兴的本地 agent 工具，其状态目录结构、agent 定义文件格式、会话存储方式和继续命令语法均未被 Mission Control 代码库引用。若沿用现有硬编码模式，需在 OpenCode 接口稳定后重新修改源码。因此需要一套不依赖 OpenCode 具体实现的接入机制，使 Mission Control 能够以配置驱动的方式纳入新的 runtime 类型，且在不具备完整运行时环境时仍可通过 fixture 独立验证。

### 整体架构与融合策略

本方案在 Mission Control 现有 RuntimeId + FrameworkAdapter 双层架构上扩展，不改变已有运行时的检测、同步和会话管理逻辑。新增的 OpenCode 接入路径沿袭以下既有流程：运行时检测（agent-runtimes.ts）→ 本地 agent 扫描与同步（local-agent-sync.ts）→ 适配器注册与事件广播（adapters/opencode.ts）→ 会话发现与展示（sessions.ts）→ 会话继续（sessions/continue）。方案的核心设计原则是：所有 OpenCode 相关的路径、命令、格式均通过环境变量或配置项注入，不硬编码；对未知格式采用兼容式解析，解析失败时优雅降级而非崩溃；在无真实 OpenCode 环境时，通过 fixture 和命令替身保证功能可测试、可演示。

### 运行时检测与可配置发现

扩展 RuntimeId 联合类型，新增 'opencode' 枚举值。检测函数 detectOpenCode 不假设 OpenCode 已安装，而是按以下可配置的优先级尝试：首先检查环境变量 OPENCODE_CLI_NAME 指定的命令名（默认为空，表示未配置），再尝试常见的候选命令名如 'opencode'。检测方式与现有 detectBinary 模式一致：以 spawnSync 执行 --version 命令，超时 3 秒，成功则记录版本信息。若全部候选均失败，返回 installed=false 并标记 authenticated=false，不影响其他运行时检测。

在 RuntimeMeta 中为 opencode 注册元信息：名称为 'OpenCode'，描述为 'Local AI coding agent with session history and tool use.'，authRequired 为 true，authHint 提示用户运行 'opencode auth'。同时在 RUNTIME_META 和 DETECTORS 映射中注册对应的检测函数。

本地 agent 发现方面，扩展 local-agent-sync.ts 的扫描根目录列表。在现有 ~/.agents/、~/.codex/agents/、~/.claude/agents/、~/.hermes/skills/ 的基础上，新增环境变量 OPENCODE_AGENTS_DIR 指定的目录；若该变量未设置，则默认扫描 ~/.opencode/agents/。扫描逻辑复用已有的目录枚举、标记文件检测（soul.md、AGENT.md、identity.md、config.json 等）和 SHA256 内容哈希变化检测机制。对于 OpenCode 自定义格式的 agent 定义文件，解析失败时跳过该 agent 并记录日志，不中断整体扫描。

### OpenCode 适配器合约

新建 src/lib/adapters/opencode.ts，实现 FrameworkAdapter 接口的全部五个方法。适配器的 framework 字段固定为 'opencode'。通信方式通过环境变量 OPENCODE_ADAPTER_MODE 配置：取值为 'cli' 时通过执行 CLI 子命令完成心跳和任务上报；取值为 'http' 时通过向 OpenCode 本地 HTTP 端点发送请求完成。在 adapters/index.ts 的 adapters 注册表中增加 'opencode' 条目。

register 方法：通过 eventBus 广播 agent.created 事件，携带 agentId、name、framework='opencode' 和元数据。heartbeat 方法：通过 eventBus 广播 agent.status_changed 事件。当配置为 CLI 模式时，额外尝试执行 OPENCODE_HEARTBEAT_CMD 或默认的 opencode status --json 命令获取运行时指标。reportTask 方法：通过 eventBus 广播 task.updated 事件。getAssignments 方法：复用 adapter.ts 中的 queryPendingAssignments 函数查询数据库中的待分配任务。disconnect 方法：广播 offline 状态事件。当适配器无法与 OpenCode 进程通信时（CLI 命令失败或 HTTP 端点不可达），不抛出异常，而是通过 eventBus 广播状态降级事件，由前端根据状态信息决定展示方式。

### 会话发现与兼容式解析

Mission Control 的 sessions.ts 当前从 {OPENCLAW_STATE_DIR}/agents/{name}/sessions/sessions.json 读取 OpenClaw 网关的会话记录。为支持 OpenCode，方案将 session store 路径解析从硬编码改为按 runtime 分派的策略模式。新增函数 resolveSessionStoreRoot(runtime: string, agentName: string) ，对 'openclaw' 保持原路径逻辑，对 'opencode' 使用环境变量 OPENCODE_STATE_DIR 拼接 {agentName}/sessions/ 子路径。若 OPENCODE_STATE_DIR 未配置，默认使用 ~/.opencode/ 作为根目录。

OpenCode 的会话文件格式可能与 OpenClaw 的 sessions.json 不同。方案引入兼容式解析层，在读取 OpenCode 会话文件时，依次尝试：JSON 对象格式（与 OpenClaw 相同结构）、JSON 数组格式（每条会话为一个数组元素）、NDJSON 格式（每行一个 JSON 对象）。对每种格式提取统一的 GatewaySession 字段：sessionId、updatedAt、chatType、channel、model、totalTokens、inputTokens、outputTokens。无法提取的字段置为默认值；整文件解析失败时返回空会话列表并记录警告日志，不影响其他 runtime 的会话读取。解析后的会话统一汇入 getAllGatewaySessions 的返回列表，保持 30 秒 TTL 缓存机制不变。

对于 transcript（会话消息内容）的读取，方案提供独立的 transcript 解析器接口。每种 runtime 可注册自己的 transcript 解析器，OpenCode 的解析器将原始消息文件（如 messages.jsonl 或类似格式）映射为统一的转录结构 { role, content, timestamp } 三元组。解析器以配置驱动：环境变量 OPENCODE_TRANSCRIPT_FORMAT 指定格式名（json/ndjson/custom），对应的解析逻辑在独立模块中实现。

### 会话继续机制与命令替身验证

sessions/continue 路由当前支持 claude-code 和 codex-cli 两种 ContinueKind。方案扩展 ContinueKind 联合类型，新增 'opencode-cli'。OpenCode 的继续命令通过环境变量 OPENCODE_CONTINUE_CMD 注入，格式为模板字符串，如 'opencode resume --session {sessionId} {prompt}'，其中 {sessionId} 和 {prompt} 在运行时替换为实际值。若该环境变量未设置，'/api/sessions/continue' 对 kind='opencode-cli' 的请求返回 501 Not Implemented 并携带明确消息 'OpenCode session continue is not configured. Set OPENCODE_CONTINUE_CMD to enable.'。

命令执行复用现有的 runCommand 工具函数，超时设为 180 秒。命令的标准输出作为回复内容返回。同时保留现有的参数校验逻辑：sessionId 必须匹配安全正则、prompt 不为空且不超过 6000 字符。

对于不具备真实 OpenCode 环境的测试场景，方案提供 fixture 验证方式。在 tests/fixtures/opencode/ 目录下放置模拟的 agent 定义和 session 文件，以及在 playwright 测试中注入 mock 命令替身（如将 opencode 指向 echo 脚本），使 E2E 测试可以在无 OpenCode 安装的情况下验证检测、发现、同步和 continue 全流程。

### UI/API 能力边界声明

为避免给用户造成“所有操作均已完整支持”的误解，方案要求在 UI 和 API 层面对 OpenCode 的能力边界进行显式声明。具体包括：(1) 在 Agent 详情面板中，若 agent 的 source 为 'local' 且 runtime 为 'opencode'，在状态区域显示信息标签“本地 OpenCode agent —— 部分操作可能受限”；(2) 在 Session 详情中，若 session 来源于 OpenCode runtime，且 transcript 解析器未成功解析消息内容，展示“会话内容暂不可读 —— OpenCode 消息格式未识别”而非空白页面；(3) 在 /api/sessions/continue 的响应中，对 opencode-cli 增加 capabilities 字段，列出当前支持的操作（如 resume）和不支持的操作（如 fork、merge）。

能力边界声明的元数据由后端统一管理。在 RuntimeMeta 结构中新增 capabilities 字段，描述该 runtime 支持的操作列表。前端渲染时，根据 capabilities 动态显示或隐藏操作按钮。对于 opencode，初始 capabilities 设为 ['discover', 'sync', 'list_sessions', 'view_agent_config']，不包含 'live_heartbeat' 和 'task_execution' 等需要确认 OpenCode 实际支持后再启用的能力。随着 OpenCode 接口的演进，仅需更新 RuntimeMeta 中的 capabilities 配置即可扩展支持范围，无需修改前端代码。

### 技术效果

本方案通过配置驱动的 runtime 接入架构，取得以下技术效果：(1) 无需修改 Mission Control 核心代码即可纳入新的 agent runtime 类型，仅需通过环境变量注入路径、命令和格式配置；(2) 保持对已有 Claude、Codex、Hermes、OpenClaw 等运行时的完全兼容，OpenCode 的接入不影响现有功能；(3) 在无真实 OpenCode 环境时，通过 fixture 和命令替身体系仍可完成全链路功能验证；(4) 兼容式解析层确保未知或非标准格式的会话文件不会导致系统崩溃，而是优雅降级并给出明确的状态提示；(5) 能力边界声明机制让用户始终清楚 OpenCode 当前支持和不支持哪些操作，避免误导。整体方案不依赖 OpenCode 的具体实现细节，使其可在 OpenCode 接口稳定后通过配置调整快速适配，降低了系统与特定 runtime 之间的耦合度。

### 风险与待确认问题

以下事项依赖 OpenCode 的实际实现，当前无法在方案中确定：(1) OpenCode CLI 命令的确切名称与安装方式——可通过 OPENCODE_CLI_NAME 环境变量承载默认值，待确认后调整；(2) OpenCode 本地状态目录的默认约定——当前假定为 ~/.opencode/，需与 OpenCode 实际目录结构对齐；(3) OpenCode 会话文件的准确格式——当前方案已覆盖 JSON 对象、JSON 数组、NDJSON 三种常见格式，但若 OpenCode 使用二进制或自定义编码格式则需新增解析器；(4) OpenCode 是否原生支持 session resume——若不支持，openCode-cli 的 continue 将返回 501；(5) 适配器通信协议——若 OpenCode 不提供 HTTP API，CLI 模式的 heartbeat 延迟可能高于其他运行时，需在实际集成后评估是否需要引入轮询或文件监控机制。
