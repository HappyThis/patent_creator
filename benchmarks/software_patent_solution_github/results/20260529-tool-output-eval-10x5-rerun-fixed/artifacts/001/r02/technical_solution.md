## 技术方案

本技术方案提出一种可扩展的本地 agent runtime 接入框架，使 Mission Control 能够将 OpenCode 以原生 agent runtime 的方式纳入统一管理，在保持对已有 Claude Code、Codex CLI、Hermes Agent 等 runtime 兼容的前提下，实现 OpenCode 工作会话的发现、展示与条件式继续，同时通过能力边界声明机制避免用户对未完整支持的能力产生误解。

### 1. 技术问题

Mission Control 已有的 agent 管理体系支持 OpenClaw Gateway、Claude Code、Codex CLI、Hermes Agent 四种 runtime 的安装检测、会话发现与条件式继续。OpenCode 作为一种新兴的本地 agent runtime，用户已在本地使用其完成 agent 工作并产生会话记录。当前系统缺乏识别 OpenCode 本地工作记录的机制，用户只能在 Mission Control 外部手工管理这些会话，无法获得统一的 agent 编队视图。核心挑战在于：OpenCode 的本地状态存储目录、消息文件格式以及会话继续命令等细节并非已实现的已知契约，必须设计一套不依赖硬编码假设的通用接入机制。

### 2. 核心技术方案

本方案的核心思路是：在 Mission Control 已有 runtime 管理架构中，将 OpenCode 注册为第五种 RuntimeId，复用已有的运行时检测、会话发现、会话列表合并、会话继续分发等基础设施，同时为 OpenCode 特有的不确定性设计可配置的发现层、适配器合约与能力边界声明机制，使系统在不硬编码 OpenCode 内部实现细节的前提下完成接入。

### 3.1 Runtime 注册与检测

在 agent-runtimes 模块中扩展 RuntimeId 联合类型，新增 'opencode' 枚举值。同时扩展三个关键映射表：RUNTIME_META（元数据描述）、DETECTORS（检测函数映射）、INSTALL_FNS（安装函数映射）。新增的 detectOpenCode 检测函数负责判断 OpenCode CLI 是否已安装在当前系统中，返回包含 installed、version、running、authenticated 等字段的 RuntimeStatus 结构。检测逻辑与现有 Claude/Codex 的 detectBinary 模式一致：通过候选路径列表扫描 OpenCode 可执行文件，执行版本探测命令获取版本信息。

### 3.2 可配置的会话发现机制

由于 OpenCode 的本地状态目录和会话文件格式在不同版本中可能不同，本方案不硬编码具体路径和解析逻辑，而是通过以下可配置机制实现发现：(1) 在 config.ts 中新增 OPENCODE_STATE_DIR 环境变量配置项，允许用户显式指定 OpenCode 状态目录路径；若未配置，系统回退到多个常见候选路径（如 ~/.opencode、~/.config/opencode 等）进行探测。(2) 通过 OPENCODE_SESSION_GLOB 环境变量配置会话文件的匹配模式，默认可覆盖 .jsonl、.json 等常见格式。(3) 会话扫描函数采用路径遍历+最近修改时间排序的方式发现候选文件，对解析失败的文件静默跳过，不影响其他会话的呈现。

### 3.3 适配器合约与兼容式解析

定义 OpenCode 会话适配器合约接口 IOpenCodeSessionAdapter，声明以下方法：(1) listSessionFiles(stateDir)：返回候选会话文件路径及修改时间；(2) parseSessionFile(filePath)：尝试将单个会话文件解析为统一的 OpenCodeSessionStats 结构，包含 sessionId、projectPath、model、messageCount、inputTokens、outputTokens、firstMessageAt、lastMessageAt、isActive 等字段；(3) supportsContinue(session)：返回布尔值，声明该会话是否支持 continue 操作。系统提供默认实现 DefaultOpenCodeAdapter，该适配器基于 JSONL 行式解析策略：逐行读取文件，识别每条记录的 type 字段推断消息角色，从 usage 字段提取 token 用量，从 timestamp 字段提取时间信息。若用户环境中的 OpenCode 使用不同格式，可通过 OPENCODE_ADAPTER_MODULE 环境变量注册自定义适配器模块路径，系统在会话扫描时动态加载该模块替代默认实现。该适配器合约还要求每个解析结果附带 parseConfidence 置信度标记（high/medium/low），基于成功解析的字段比例计算，用于驱动前端能力边界的声明。

### 3.4 统一会话发现流水线扩展

在现有 /api/sessions 的 GET 处理流程中增加 getLocalOpenCodeSessions() 调用。该函数调用 scanOpenCodeSessions() 扫描 OpenCode 状态目录下的会话文件，通过适配器合约解析为统一格式，映射为与 Claude/Codex/Hermes 会话结构一致的返回对象（含 id、key、agent、kind='opencode'、model、tokens、channel、flags、active、source='local' 等字段）。扫描结果通过已有的 mergeLocalSessions 和 dedupeAndSortSessions 函数与 gateway 会话及其他本地会话合并排序，最终按 lastActivity 降序返回前 100 条。新增的会话扫描函数遵循与 codex-sessions.ts 和 hermes-sessions.ts 相同的防御式设计：对不存在的目录静默跳过、对大文件设置尺寸上限（50MB）、对解析失败的记录跳过该行继续处理、对时间戳异常值使用 clampTimestamp 矫正。

### 3.5 会话继续分发与命令替身验证

在 /api/sessions/continue 的 POST 处理中扩展 ContinueKind 联合类型，新增 'opencode' 分支。OpenCode 的继续逻辑不假定具体命令格式，而是通过以下两层机制实现：(1) 命令模板配置：通过 OPENCODE_CONTINUE_COMMAND 环境变量配置继续命令模板，支持 {sessionId} 和 {prompt} 占位符替换，默认为 opencode resume {sessionId} {prompt}。(2) 命令替身验证（Fixture Verification）：在首次使用 continue 前，系统自动执行一次干运行验证——向 OpenCode CLI 发送探测命令（如 --help 或 resume --help），检查退出码和输出中是否包含预期关键词；验证通过后缓存结果，验证失败则在会话的 continueSupported 字段标记为 false。

### 3.6 能力边界声明机制

为防止用户在 Mission Control 中误以为 OpenCode 的所有能力已得到完整支持，本方案设计了三级能力边界声明机制：(1) 会话级别：每条 OpenCode 会话记录携带 capabilities 字段，列出已确认支持的操作（如 view_transcript、continue）和明确不支持的操作（如 set_thinking_level、delete_session），基于适配器解析置信度和命令替身验证结果动态确定。(2) runtime 级别：在 RuntimeStatus 结构中新增 capabilities 子对象，声明该 runtime 在当前部署中支持的操作集合，供前端 Agent Runtimes 面板消费。(3) UI 呈现：前端会话列表和会话详情面板对 OpenCode 类型的会话展示能力标签，对不支持的操作按钮置灰并附带 tooltip 说明原因。例如，当 OpenCode 会话适配器返回 parseConfidence=low 时，UI 仅展示基本会话元数据而不提供 continue 入口。

### 3.7 处理流程

系统处理 OpenCode 会话的完整流程如下：(1) 启动时或按请求触发 detectAllRuntimes()，其中 detectOpenCode() 扫描候选二进制路径，探测 OpenCode CLI 是否可用并获取版本，返回 RuntimeStatus。(2) 当用户访问会话列表页面时，GET /api/sessions 依次调用 syncClaudeSessions()、getLocalClaudeSessions()、getLocalCodexSessions()、getLocalHermesSessions()，以及新增的 getLocalOpenCodeSessions()。(3) getLocalOpenCodeSessions() 读取 OPENCODE_STATE_DIR 配置（或回退候选路径），通过 listRecentOpenCodeSessionFiles() 发现最近修改的候选会话文件。(4) 对每个候选文件调用适配器的 parseSessionFile() 方法，提取统一格式的会话统计信息。(5) 解析结果与 gateway 会话及其他本地会话合并去重，按最后活动时间排序返回。(6) 前端接收会话列表后，对 kind='opencode' 的会话根据其 capabilities 字段动态控制可用操作按钮。

### 4. 技术效果

(1) 统一管理视图：OpenCode 会话与 Claude Code、Codex CLI、Hermes Agent 会话在同一面板中展示，用户获得一致的 agent 编队管理体验，不再需要切换工具或手工跟踪 OpenCode 工作记录。(2) 兼容性保障：通过可配置发现路径、适配器合约和命令替身验证三层解耦，新增 OpenCode 支持不改变已有 runtime 的检测与会话管理逻辑，不影响 Claude、Codex、Hermes 的运行方式。(3) 可扩展性：适配器合约机制使得未来接入其他本地 agent runtime（如 Aider、Cline 等）时只需提供符合 IOpenCodeSessionAdapter 接口的新适配器实现，无需修改核心会话流水线代码。(4) 用户认知安全：能力边界声明机制确保系统不会向用户展示未经验证的操作入口，避免用户误以为 OpenCode 的能力已被 Mission Control 完整支持，从而防止因能力差异导致的操作失败或数据丢失。

### 5. 与项目环境的对应关系

本方案与 Mission Control 现有项目结构的对应关系如下：(1) agent-runtimes.ts：新增 detectOpenCode 检测函数，扩展 RuntimeId、RUNTIME_META、DETECTORS、INSTALL_FNS。(2) config.ts：新增 opencodeStateDir、opencodeSessionGlob、opencodeContinueCommand、opencodeAdapterModule 等配置项。(3) 新增 opencode-sessions.ts：实现 scanOpenCodeSessions、parseOpenCodeSessionFile、getLocalOpenCodeSessions 等核心函数，结构与 codex-sessions.ts 和 hermes-sessions.ts 对齐。(4) sessions/route.ts：在 GET 处理中引入 getLocalOpenCodeSessions()，在 mergeLocalSessions 中添加 opencode 参数。(5) sessions/continue/route.ts：在 ContinueKind 中新增 'opencode'，添加对应的命令执行分支。(6) agent-runtimes/route.ts：VALID_RUNTIMES 集合新增 'opencode'。(7) 前端组件：sessions 面板和 agent-runtimes 面板根据 capabilities 字段动态渲染 OpenCode 会话的操作按钮与能力标签。

### 6. 风险与待确认问题

(1) OpenCode 会话文件格式：本方案默认适配器假设 JSONL 格式（每行一个 JSON 对象，含 type/timestamp/usage 等字段），若实际 OpenCode 使用截然不同的格式（如 SQLite 数据库或 MessagePack），默认适配器的 parseConfidence 将为 low，此时系统仅展示基本元数据而无法提供详细 token 统计，用户需通过 OPENCODE_ADAPTER_MODULE 注册自定义适配器。(2) 继续命令兼容性：命令替身验证依赖 OpenCode CLI 对 --help 或 resume --help 的标准响应，若其 CLI 实现不使用标准帮助标志（例如使用 -h 或完全不支持子命令帮助），自动验证将失败，continue 能力将被标记为不可用。(3) 多版本共存：若系统同时存在多个 OpenCode 版本或安装路径，detectBinary 的候选路径扫描策略可能需要用户通过 OPENCODE_BIN 环境变量显式指定首选二进制。(4) 性能边界：OpenCode 会话文件数量和大小不可预知，系统通过文件尺寸上限（50MB）和扫描数量上限（100 个文件）控制 I/O 开销，在极端场景下可能遗漏历史会话。
