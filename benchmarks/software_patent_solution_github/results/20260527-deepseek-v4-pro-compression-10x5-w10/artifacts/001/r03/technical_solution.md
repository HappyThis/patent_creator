## 技术方案

本技术方案提出一种将 OpenCode 作为原生 agent runtime 接入 Mission Control 系统的方法。OpenCode 是用户在本地使用的一种 agent 工作环境，产生会话记录、代理定义和任务执行日志。系统需要在不假设 OpenCode 内部实现细节的前提下，将其纳入与 Claude Code、Codex CLI、Hermes Agent、OpenClaw 等已有运行时一致的统一管理框架。

### 可配置的 OpenCode 运行时发现机制

Mission Control 已有运行时检测体系对每种 agent runtime 提供独立检测函数（detectOpenClaw、detectClaude、detectCodex、detectHermes），返回统一的 RuntimeStatus 结构（包含 installed、version、running、authenticated 等字段）。为接入 OpenCode，系统引入一种可配置的发现机制，而非硬编码 OpenCode 的安装路径和版本检测方式。

具体而言，系统在 RuntimeId 联合类型中新增 'opencode' 枚举值，并在 RUNTIME_META 表中注册其名称、描述和认证提示。发现逻辑不硬编码 OpenCode 的二进制路径或状态目录，而是通过环境变量 OPENCODE_BIN、OPENCODE_HOME 和 OPENCODE_STATE_DIR 进行可配置寻址。若用户未设置这些变量，系统按约定搜索常见路径（如 ~/.opencode/），并将搜索结果纳入统一的 detectRuntime 调用链。

### OpenCode 适配器合约

Mission Control 已定义 FrameworkAdapter 接口，为每种 agent 框架提供统一适配能力，包括 register（代理注册）、heartbeat（心跳上报）、reportTask（任务报告）、getAssignments（待处理任务查询）和 disconnect（断连处理）。现有实现包括 OpenClawAdapter、GenericAdapter、CrewAIAdapter 等。

为接入 OpenCode，系统新增 OpenCodeAdapter 类，实现 FrameworkAdapter 接口。该适配器将 OpenCode 的代理生命周期事件（注册、心跳、任务报告）通过 Mission Control 的事件总线（eventBus）广播为标准事件。当 OpenCode 代理启动时，适配器将代理元数据（名称、框架标识等）写入系统数据库，并通过 eventBus.broadcast('agent.created', ...) 通知其他模块；心跳更新和任务进度报告同样通过 eventBus 广播。

由于 OpenCode 的任务分配和查询机制可能与现有运行时不同，适配器合约将 OpenCode 特有的交互封装在标准接口之后。对于 getAssignments，适配器复用系统已有的 queryPendingAssignments 查询逻辑，从统一的 tasks 表中按 agentId 和优先级返回待处理任务列表。这种设计使 OpenCode 代理可以接收与其他运行时相同的任务分配，而无需了解 Mission Control 内部任务调度细节。

### OpenCode 本地会话扫描器

Mission Control 现有三种本地会话扫描模式，对应不同运行时的数据存储方式：Claude Code 扫描 ~/.claude/projects/ 下的 JSONL 会话文件；Codex CLI 递归扫描 ~/.codex/sessions/ 下的 JSONL 文件；Hermes Agent 以只读模式打开 ~/.hermes/state.db SQLite 数据库。这些扫描器各自提取会话 ID、模型、消息数、token 用量、活跃状态等信息，并统一映射为 Session 结构。

针对 OpenCode，系统不假设其会话存储格式（可能是 JSONL 文件、SQLite 数据库、JSON 对象文件或其他自定义格式），而是设计一种兼容式解析机制。核心思路是：定义一个 OpenCodeSessionParser 接口，包含两个方法——discoverSessionFiles() 返回会话文件路径列表，parseSessionFile(path) 返回标准化的 SessionStats 结构。接口的实现通过配置选择：系统优先检查是否存在 OpenCode 特定的解析器（如 opencode-jsonl 或 opencode-sqlite），若均不可用则回退到基于 fixture 文件的替身验证模式（harness mode），使用预置的测试数据确保 UI 的非破坏性展示。

具体而言，scanOpenCodeSessions() 函数遵循以下流程：（1）通过配置项 opencodeStateDir 定位 OpenCode 状态目录；（2）遍历该目录下的会话存储位置（由 opencodeSessionGlob 配置模式指定）；（3）对每个匹配文件，尝试按注册的解析器顺序解析，首个成功解析的解析器产生结果；（4）无论原始格式如何，均统一输出包含 sessionId、projectPath、model、userMessages、assistantMessages、inputTokens、outputTokens、firstMessageAt、lastMessageAt、isActive 的标准字段。其中 isActive 的判定复用现有 90 分钟活跃窗口阈值。

### 多源会话聚合与统一展示

Mission Control 已有的会话 API（GET /api/sessions）采用多源聚合策略：首先读取 OpenClaw 网关会话（通过 getAllGatewaySessions 从 agents/{name}/sessions/sessions.json 加载），然后分别同步和扫描 Claude Code、Codex CLI、Hermes Agent 的本地会话，最后通过 dedupeAndSortSessions 按来源+ID 去重并按最后活跃时间降序排列，返回前 100 条统一格式的会话记录。

接入 OpenCode 后，会话 API 的聚合流程新增一个来源分支：getLocalOpenCodeSessions() 调用 scanOpenCodeSessions() 获取 OpenCode 本地会话，并将其映射为包含 source: 'local'、kind: 'opencode' 的统一 Session 结构。映射过程中，每条 OpenCode 会话被赋予与其他运行时一致的外观：id、key、agent、kind、age、model、tokens、flags、active、startTime、lastActivity 等字段。这使得 OpenCode 会话可以在 Mission Control 仪表板的会话列表中与 Claude、Codex、Hermes 的会话并列展示，用户看到统一的会话管理界面。

在去重排序阶段，OpenCode 会话使用 'local:{sessionId}' 作为去重键，与 Claude Code 的 'local:{session_id}'、Codex CLI 的 'local:{sessionId}' 采用相同命名空间，确保跨运行时的同 ID 会话以最近活跃的版本优先保留。这样当用户同时在 OpenCode 和 Claude Code 中操作同一项目时，系统展示最近活动的运行时会话。

### 会话继续能力与边界声明

Mission Control 已有的会话继续功能（POST /api/sessions/continue）支持对 Claude Code（通过 claude --print --resume {sessionId} {prompt}）和 Codex CLI（通过 codex exec resume {sessionId} {prompt}）的会话进行继续执行。这是一种让用户在 Mission Control 界面中直接向已存在的本地 agent 会话发送新提示并获取响应的能力。

对于 OpenCode 的会话继续能力，系统采用命令替身模式（command stub pattern）：在 Kind 联合类型中新增 'opencode'，在 POST /api/sessions/continue 的处理分支中添加 OpenCode 路径。具体的继续命令不硬编码为固定 CLI 参数，而是通过可配置的 opencodeContinueCommand 模板指定，模板中 {sessionId} 和 {prompt} 占位符在运行时替换。若 OpenCode 实际不支持会话恢复命令，则配置项可设为空值，API 返回明确的 'opencode session resume is not available' 错误信息及原因。

系统在 UI 层面对 OpenCode 会话的能力边界做显式声明。当 OpenCode 会话的 kind 为 'opencode' 时，UI 组件检查 opencodeContinueAvailable 标志：若该标志为 true，显示“继续会话”按钮并允许用户输入提示词；若为 false（如 OpenCode 不支持继续命令、命令模板未配置、或运行时未安装），按钮置灰并提示“OpenCode 会话暂不支持继续操作”。同理，OpenCode 会话的其他控制操作（如设置思考模式、设置详细程度、设置标签等，这些功能当前通过 OpenClaw 网关 RPC 实现）在会话详情面板中标注为“不可用于 OpenCode 会话”，避免给用户造成全功能支持的误解。

### OpenCode 代理定义同步扩展

Mission Control 已有的本地代理同步机制（local-agent-sync.ts）负责从多个磁盘目录发现代理定义文件（soul.md、AGENT.md、identity.md、config.json 等），并将其名称、角色、灵魂内容、内容哈希等信息同步到 agents 数据库表中。当前扫描的根目录包括 ~/.agents/、~/.codex/agents/、~/.claude/agents/、~/.hermes/skills/。

接入 OpenCode 后，getLocalAgentRoots() 函数新增返回 ~/.opencode/agents/ 路径（可通过 OPENCODE_AGENTS_DIR 环境变量覆盖），使 OpenCode 目录下的代理定义文件被纳入双向同步范围。当 OpenCode 代理目录中存在 soul.md 或 identity.md 等标识文件时，本地同步引擎将其识别为一个代理条目，计算内容 SHA-256 哈希，并与数据库中的记录比对：新增、更新或标记离线。这意味着用户在 OpenCode 中定义的代理角色会自动出现在 Mission Control 的代理管理面板中，保持源为 'local'。

### 安装管理与运行时检测集成

Mission Control 为每种运行时提供统一的安装管理接口。RuntimeId 联合类型中新增 'opencode' 后，系统自动获得对 OpenCode 的状态检测和安装作业支持。detectOpenCode() 函数通过可配置的 opencodeBin 搜索 OpenCode 二进制文件，并检查 OPENCODE_API_KEY 环境变量或 ~/.opencode/auth.json 等认证文件是否存在，返回 RuntimeStatus 结构。

对于安装流程，startInstall('opencode', mode) 复用已有的安装作业模型（InstallJob），支持 local 和 docker 两种部署模式。本地安装时，系统通过可配置的安装脚本 URL（opencodeInstallScriptUrl）下载并经过安全审查（注入扫描 + AI 审查）后执行。Docker 模式下，generateDockerSidecar('opencode') 生成对应的 Docker Compose 侧车模板。安装作业的进度、输出和错误信息通过 getInstallJob 查询接口暴露给前端。

### 技术效果

本方案通过将 OpenCode 作为原生 agent runtime 接入 Mission Control，带来以下技术效果：

1. 统一管理体验：OpenCode 会话与 Claude Code、Codex CLI、Hermes Agent 的会话在同一个仪表板中并列展示，用户无需切换工具即可查看所有 agent 运行时的工作记录。统一的 Session 结构和去重排序逻辑确保不同运行时的会话之间具有可比性和一致的交互体验。
2. 可扩展的适配器架构：FrameworkAdapter 接口和会话解析器注册机制使新增运行时无需改动核心聚合逻辑。新增运行时只需实现适配器接口和会话解析接口，即可自动获得代理注册、心跳、任务分配和会话展示能力。这一架构同样适用于未来出现的其他 agent 运行时。
3. 不假设实现的兼容式设计：通过环境变量配置路径、解析器链回退和 fixture 替身验证，系统不依赖 OpenCode 的内部文件格式或 CLI 命令约定。当 OpenCode 的实际能力不确定时，系统通过 UI 能力边界声明明确告知用户哪些操作可用、哪些暂不支持，避免误导。
4. 双向代理同步：通过扩展本地代理同步机制的扫描根目录，OpenCode 中定义的代理自动出现在 Mission Control 的代理管理面板中。用户对代理灵魂内容的编辑也可回写到磁盘，实现 UI 与磁盘的双向同步。
5. 安全可控的安装流程：复用已有的安装脚本安全审查流水线（注入扫描 + AI 审查），确保 OpenCode 安装过程与其他运行时受到同等安全保护。

### 处理流程

接入 OpenCode 的整体处理流程如下：

1. 系统启动时，detectAllRuntimes() 调用 detectOpenCode()，通过环境变量 OPENCODE_BIN 和 OPENCODE_STATE_DIR 检测 OpenCode 安装状态，返回 RuntimeStatus。
2. 定时调度器（scheduler）触发会话同步：syncOpenCodeSessions() 扫描 OpenCode 状态目录下的会话文件，通过注册的解析器链解析为标准 SessionStats 结构，写入数据库。
3. 同时，syncLocalAgents() 扫描 ~/.opencode/agents/ 目录，发现 OpenCode 代理定义文件，与数据库中的本地代理记录进行双向同步。
4. 当前端请求 GET /api/sessions 时，getLocalOpenCodeSessions() 从数据库读取 OpenCode 会话并映射为统一 Session 格式，与网关会话、Claude/Codex/Hermes 本地会话合并后返回。
5. 用户在前端界面中看到 OpenCode 会话与其他运行时会话并列展示。对于 kind 为 'opencode' 的会话，UI 根据 opencodeContinueAvailable 配置决定是否显示“继续会话”按钮。
6. 当用户对 OpenCode 会话发起继续操作时，POST /api/sessions/continue 使用可配置的命令模板执行，返回执行结果。

### 风险与待确认问题

本方案存在以下需要后续确认的风险和边界：

- OpenCode 会话文件格式不确定性：当前方案通过解析器链回退和 fixture 替身验证来应对，但若 OpenCode 的实际会话格式与所有已注册解析器均不兼容，则在没有 fixture 数据时 UI 将显示空列表。需要在实际接入时根据 OpenCode 的真实文件格式实现对应解析器。
- 会话继续命令兼容性：OpenCode 的会话恢复命令（如 opencode continue）的具体参数和退出码行为需要在接入时验证。当前方案通过可配置命令模板和 UI 能力边界声明来降低风险。
- OpenCode 代理的心跳和任务报告路径：FrameworkAdapter 的 heartbeat 和 reportTask 方法依赖 OpenCode 代理主动调用 Mission Control 的 API。如果 OpenCode 代理不具备这种主动上报机制，则需要通过轮询 OpenCode 状态文件来推断代理状态。
- 多运行时会话的去重边界：当前去重键为 'local:{sessionId}'，如果 OpenCode 和 Codex 恰好使用相同的会话 ID 格式，可能导致误去重。需要在 OpenCode 实际接入时评估是否需要为不同运行时使用独立命名空间。
- OpenCode 网关能力差异：现有会话控制操作（设置思考模式、详细程度、推理级别、标签等）通过 OpenClaw 网关 RPC（callOpenClawGateway）实现。OpenCode 会话不属于 OpenClaw 网关管理范围，这些操作无法对 OpenCode 会话执行，UI 已通过能力边界声明处理此差异。
