## 技术方案

本方案提出一种可扩展的本地 Agent Runtime 适配架构，使 Mission Control 能够将 OpenCode 作为原生 agent runtime 进行识别、管理和会话继续，同时保持对现有 Claude、Codex、Hermes、OpenClaw 等运行时的兼容。核心思路是引入 Runtime 适配器合约（Runtime Adapter Contract），将发现、检测、会话扫描和能力声明抽象为标准化接口，OpenCode 作为实现该合约的新 runtime 接入系统。

### 整体架构

系统在现有 agent-runtimes 模块基础上，将 Runtime 接入抽象为三层：发现层（Discovery）、适配层（Adapter）和能力声明层（Capability Declaration）。

发现层负责定位本地已安装的 runtime 实例。对于 OpenCode，通过环境变量 OPENCODE_STATE_DIR 和 OPENCODE_BIN 的可配置路径，以及默认扫描 ~/.opencode/ 目录来发现安装。发现层输出的 RuntimeStatus 结构包含 installed、version、running、authenticated 等字段，与现有 Claude、Codex、Hermes、OpenClaw 的检测结果同构。

适配层通过 RuntimeSessionScanner 接口定义统一的会话扫描合约。每个 runtime 实现该接口，负责读取各自本地存储的会话记录，转换为系统统一的 SessionStats 格式（包含 sessionId、model、messageCount、tokenUsage、timestamps、isActive 等字段）。OpenCode 的适配器实现 opencodeSessionScanner，负责解析 OpenCode 本地会话文件。

能力声明层通过 RuntimeCapabilities 结构显式声明每个 runtime 支持的操作边界。例如，OpenCode 可能支持会话查看（listSessions）、会话继续（continueSession），但可能不支持直接通过 Mission Control 发起新会话（startSession）或实时流式输出（streamOutput）——这些由适配器在能力结构中如实声明，前端据此渲染对应的 UI 控件，避免向用户展示不可用的操作。

### Runtime 适配器合约

系统定义 RuntimeSessionScanner 接口作为所有本地 runtime 会话扫描的统一合约。

该接口包含以下方法：（1）scanSessions(limit?: number): Promise<SessionStats[]>——扫描本地会话文件/数据库，返回统一格式的会话统计信息；（2）getCapabilities(): RuntimeCapabilities——返回该 runtime 的能力声明，包括是否支持会话列表、会话继续、新会话发起、流式输出、实时心跳等；（3）detectInstallation(): RuntimeStatus——检测 runtime 是否已安装及版本信息；（4）resolveSessionPath(sessionId: string): string | null——将逻辑会话 ID 解析为本地文件路径或数据库 key，用于会话继续操作。

SessionStats 统一格式包含：sessionId（会话标识）、runtimeId（来源 runtime 类型）、projectPath（本地项目路径）、model（模型名称）、userMessages / assistantMessages / toolUses（消息和工具调用计数）、inputTokens / outputTokens（token 统计）、firstMessageAt / lastMessageAt（首末消息时间戳）、isActive（是否活跃）。该格式兼容现有 Claude（JSONL）、Codex（JSONL）、Hermes（SQLite）和 OpenClaw（JSON session store）的会话表示。

RuntimeCapabilities 结构声明每个 runtime 的操作边界，包含以下布尔字段：canListSessions（是否可列出历史会话）、canContinueSession（是否可继续已有会话）、canStartSession（是否可通过 Mission Control 发起新会话）、canStreamOutput（是否支持实时流式输出）、canHeartbeat（是否可上报实时心跳）、canSelfRegister（是否支持 agent 自注册）。对于 OpenCode，canContinueSession 的实现依赖于其本地 CLI 提供的 continue 命令；若该命令的接口格式暂不可确认，则通过 fixture 替身（使用预置测试数据模拟 continue 命令的输出）进行验证，并在能力声明中将对应字段标记为 experimental。

### OpenCode 发现与检测机制

OpenCode 的发现和检测遵循与现有 runtime 一致的可配置发现模式。系统通过环境变量 OPENCODE_STATE_DIR 指定 OpenCode 状态目录，通过 OPENCODE_BIN 指定可执行文件路径。默认情况下，系统扫描 ~/.opencode/ 目录。

检测逻辑实现 detectOpenCode() 函数，执行以下步骤：（1）检查 opencodeConfigPath（默认 ~/.opencode/config.json 或通过环境变量指定的路径）是否存在，若存在则 installed=true；（2）尝试执行 OPENCODE_BIN --version 获取版本号，若成功则同时确认 installed=true；（3）检查 OpenCode 是否有正在运行的服务进程——通过尝试连接其默认端口或检查 PID 文件来判断 running 状态；（4）检查 OpenCode 的认证状态——通过读取其本地凭证文件或尝试执行 opencode auth status 命令来判断 authenticated。检测结果纳入 RuntimeStatus 结构，通过 GET /api/agent-runtimes 接口返回。

OpenCode 的 RuntimeId 注册到系统的 VALID_RUNTIMES 集合中，与现有 openclaw、hermes、claude、codex 并列。系统在 DETECTORS 映射中注册 detectOpenCode，在 INSTALL_FNS 映射中注册 installOpenCodeLocal（若需要支持安装）。RUNTIME_META 中定义 OpenCode 的显示名称、描述和认证提示。

### OpenCode 会话扫描与映射

OpenCode 会话扫描通过实现 RuntimeSessionScanner 接口的 opencodeSessionScanner 完成。系统将 OpenCode 会话数据映射到统一的 SessionStats 结构，以便在 Mission Control 的会话列表中与其他 runtime 的会话并列展示。

会话存储路径通过 OPENCODE_STATE_DIR 配置，默认扫描其下的 sessions/ 子目录。系统遍历该目录中的会话文件，对每个文件执行兼容式解析：首先尝试按 JSON 格式解析，若失败则尝试 JSONL（逐行 JSON）格式。解析时采用宽松容错策略——对无法识别的字段静默跳过，只提取能映射到 SessionStats 的核心字段（会话 ID、模型名、时间戳、消息计数、token 统计）。解析失败的单个文件不影响其他文件的扫描结果。

会话继续（continue）机制通过 resolveSessionPath 将会话 ID 解析为 OpenCode 可识别的会话引用，然后调用 OPENCODE_BIN continue --session <id> --prompt <...> 命令将用户输入注入到已有会话中。命令执行采用 runCommand 工具函数（与现有 openclaw gateway call 模式一致），设置超时和输出捕获。若 OpenCode 的 continue 命令接口格式与预期不同，通过 fixture 替身验证机制——在测试环境中使用预置的会话文件和命令输出 JSON 来模拟继续操作，在不依赖真实 OpenCode 安装的情况下验证适配器的正确性。

### Agent 配置同步

OpenCode 的 agent 定义通过已有的 local-agent-sync 模块进行发现和同步。系统在 getLocalAgentRoots() 返回的扫描根目录中增加 ~/.opencode/agents/ 路径，使 OpenCode 本地定义的 agent 能够被自动发现并同步到 Mission Control 数据库。

扫描逻辑沿用现有的 IDENTITY_FILES 和 CONFIG_FILES 标记文件检测机制：若 ~/.opencode/agents/<name>/ 目录下存在 soul.md、AGENT.md、identity.md 等身份文件，或 config.json、agent.json 等配置文件，则将该目录识别为一个 agent。提取的信息包括 agent 名称、角色（role）、soul 内容、配置 JSON 和工作空间路径，写入 agents 表的 source='local' 记录。

双向同步方面，当用户在 Mission Control UI 中编辑 OpenCode agent 的 soul 内容时，系统通过 writeLocalAgentSoul 函数将修改写回 ~/.opencode/agents/<name>/soul.md 文件。同时更新数据库中的 content_hash，避免下次扫描时反向覆盖用户修改。此外，OpenCode agent 可通过 POST /api/agents/register 接口实现自注册，携带 framework='opencode' 标识来源。

### 能力边界声明与 UI/API 控制

为避免给用户造成“所有操作都已完整支持”的误解，系统为每个 runtime 维护 RuntimeCapabilities 结构。该结构通过 GET /api/agent-runtimes 接口随 RuntimeStatus 一同返回，前端根据能力标志决定哪些 UI 控件可见、哪些操作可触发。

对于 OpenCode，能力声明包括以下边界：canListSessions=true（会话列表可用）；canContinueSession=experimental（会话继续功能存在，但依赖 OpenCode continue 子命令的接口稳定性，标记为实验性）；canStartSession=false（OpenCode 的新会话由本地用户发起，Mission Control 不代为创建）；canStreamOutput=false（当前适配不假设 OpenCode 提供流式输出接口）；canHeartbeat=false（OpenCode 为本地 CLI 工具，不主动上报心跳）；canSelfRegister=true（支持 agent 自注册）。当 experimental 标记存在时，前端在对应按钮旁显示实验性标签，并通过 tooltip 说明当前功能的限制条件。

API 层面，POST /api/sessions/continue 接口在接收到 kind='opencode' 的请求时，先校验该 runtime 的 canContinueSession 能力标志；若为 false，直接返回 400 错误；若为 experimental，正常执行但响应中附加 warnings 字段提示用户该功能处于实验阶段。这种分层校验确保即使前端绕过了能力检查，API 也会拦截不被支持的操作。

为验证 OpenCode 适配器在真实 OpenCode 不可用时的正确性，系统提供 fixture 替身验证机制：在 tests/fixtures/opencode/ 目录下预置模拟会话文件和命令输出 JSON。测试用例（opencode-harness.spec.ts）使用这些 fixture 数据模拟 OpenCode 的 --version、会话列表、continue 等命令输出，通过设置 OPENCODE_STATE_DIR 环境变量指向 fixture 目录来触发适配器的 fixture 模式。这样在 CI 环境和开发环境中无需安装真实 OpenCode 即可验证适配器逻辑。

### 技术效果

本方案带来以下技术效果。

第一，可扩展性。RuntimeSessionScanner 合约将 runtime 特定逻辑与系统公共逻辑解耦，新增 runtime 只需实现合约接口并注册到 DETECTORS / SCANNERS 映射中，无需修改会话列表 API、前端渲染逻辑或其他 runtime 的代码。这避免了每增加一个 runtime 就需要在核心模块中添加大量条件分支的问题。

第二，统一体验。所有 runtime 的会话通过统一的 SessionStats 结构在 Mission Control 中并列展示，用户可以跨 runtime 查看和管理所有 agent 工作记录，无需切换到各自的 CLI 工具。会话继续操作通过统一的 POST /api/sessions/continue 接口，前端只需传递 kind 和 sessionId 即可，后端根据 kind 路由到对应的适配器。

第三，能力透明。RuntimeCapabilities 结构使系统能够精确表达每个 runtime 的能力边界，前端据此动态调整 UI 展示。当 OpenCode 的某些功能（如 continue 命令）接口尚未稳定时，系统将其标记为 experimental 而非完全隐藏或完全开放，在提供功能的同时明确告知用户当前限制。

第四，可验证性。fixture 替身机制使 OpenCode 适配器在没有真实 OpenCode 安装的环境中也能被充分测试。测试通过设置环境变量指向预置的 fixture 数据，模拟完整的发现、扫描、会话继续流程。这种方式与现有 openclaw-harness.spec.ts 的模式一致，确保了 CI 可回归、开发可独立验证。

### 风险与待确认问题

以下为当前方案中需要后续确认的风险点和技术边界。

- OpenCode 本地会话文件的精确格式（JSON vs JSONL、字段命名、嵌套结构）尚未在本方案中完全确定。当前方案采用兼容式解析策略（先 JSON 后 JSONL，容错跳过未知字段），但字段映射表需要在获取真实 OpenCode 会话样例后校准。
- OpenCode continue 子命令的接口（参数名、返回格式、退出码语义）需要确认。当前方案假设其接口类似 opencode continue --session <id> --prompt <text>，但实际命令可能不同。fixture 替身机制可在接口确认前验证适配器逻辑。
- OpenCode 的认证模型（是否需要 API key、凭证存储位置和格式）需要确认，以完善 detectOpenCode() 中的 authenticated 检测逻辑。
- 若 OpenCode 未来版本改变会话存储格式或 CLI 接口，适配器需要相应更新。方案中的容错解析策略可缓解格式变化的影响，但接口级变化仍需代码适配。

### 关键处理流程

以下描述 OpenCode session 从发现到展示的完整处理流程。

步骤一：Runtime 发现。系统启动时或用户访问 Agent Runtimes 页面时，GET /api/agent-runtimes 调用 detectAllRuntimes()，其中 detectOpenCode() 检查 OpenCode 的安装状态、版本和认证信息，返回 RuntimeStatus。

步骤二：会话扫描。系统定时或用户触发会话刷新时，调用 opencodeSessionScanner.scanSessions()，遍历 OPENCODE_STATE_DIR/sessions/ 目录中的会话文件，对每个文件执行兼容式解析。解析结果映射为 SessionStats 结构，按 lastMessageAt 降序排列，标记 isActive（最近一条消息在 90 分钟内的会话为活跃）。

步骤三：统一展示。会话数据通过 GET /api/sessions 接口返回，kind 字段为 opencode。前端根据 kind 渲染对应的 runtime 图标和标签，根据 RuntimeCapabilities 中的能力标志决定是否显示「继续会话」按钮及其 experimental 标记。

步骤四：会话继续。用户点击继续按钮并输入 prompt 后，POST /api/sessions/continue 接收 kind=opencode、sessionId 和 prompt。API 先校验 RuntimeCapabilities.canContinueSession，通过后调用 resolveSessionPath 获取本地会话引用，执行 OPENCODE_BIN continue --session <ref> --prompt <prompt> 命令，将输出返回前端。

步骤五：Agent 同步。系统定时调用 syncLocalAgents()，扫描 ~/.opencode/agents/ 目录中的 agent 定义，与 agents 表中 source='local' 的记录比对。新增 agent 写入数据库，变更的 agent 更新记录，从磁盘消失的 agent 标记为 offline。用户通过 UI 修改 soul 内容时，修改写回磁盘并更新 content_hash。
