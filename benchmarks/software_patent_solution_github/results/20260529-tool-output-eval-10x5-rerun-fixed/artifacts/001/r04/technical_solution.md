## 技术方案

本方案提出一种将 OpenCode 作为原生 agent runtime 接入 Mission Control 的软件技术方案。方案通过扩展 Mission Control 已有的 runtime 检测、会话扫描、适配器调度和会话继续体系，使 OpenCode 的本地工作记录能够被 Mission Control 统一发现、展示和管理，同时保持与 Claude Code、Codex CLI、Hermes Agent 等既有运行时的完全兼容。

### 1. 技术问题

Mission Control 当前已支持 OpenClaw、Claude Code、Codex CLI、Hermes Agent 四种 agent runtime 的检测与集成。每种 runtime 均通过独立的检测函数、会话扫描模块和继续命令实现与 Mission Control 的统一对接。OpenCode 作为一种新兴的本地 agent 终端工具，其用户同样需要将本地工作会话纳入 Mission Control 的统一管理视图。然而 OpenCode 的本地状态目录结构、会话文件格式和继续命令与现有 runtime 均不相同，如果简单将其视为外部工具或手工记录，无法实现会话自动发现、状态同步和条件继续等核心能力。

### 2. 整体架构

方案在 Mission Control 现有分层架构基础上，将 OpenCode 作为第五种原生 runtime 类型纳入以下四个层面：(1) Runtime 检测层：在 agent-runtimes 模块中新增 detectOpenCode 函数，遵循与 detectClaude、detectCodex 等一致的 RuntimeStatus 返回契约。(2) 会话扫描层：新增 opencode-sessions 模块，负责从可配置的 OpenCode 本地状态目录读取会话数据，并映射为统一的 GatewaySession 或本地会话格式。(3) 适配器调度层：在 adapters 注册表中新增 opencode 适配器入口，复用 FrameworkAdapter 接口的 register、heartbeat、reportTask、getAssignments、disconnect 五元操作。(4) 会话继续层：在 sessions/continue API 中增加 opencode 分支，同时通过能力边界标记机制向 UI 传递每种 runtime 实际可用的操作列表。

### 3. 可配置的 OpenCode 运行时发现与注册机制

由于 OpenCode 的本地状态目录、会话文件格式和继续命令在当前项目环境中尚未实现为标准化的已知路径，方案采用"可配置发现 + 适配器合约 + 兼容式解析"三层机制来解决运行时能力差异。

(1) 可配置的状态目录发现。OpenCode 的本地状态目录通过环境变量 OPENCODE_STATE_DIR 和 OPENCODE_PROJECTS_DIR 进行配置，支持 fallback 到用户主目录下的默认路径（如 ~/.opencode 和 ~/.opencode/projects）。检测函数 detectOpenCode 首先检查配置路径是否存在，然后尝试执行 opencode --version 获取版本信息，最后通过检查可执行文件和环境变量 OPENAI_API_KEY 或 OPENCODE_API_KEY 判断认证状态。整个检测逻辑与 detectClaude、detectCodex 保持一致的 RuntimeStatus 返回结构，确保 UI 层无需为 OpenCode 做特殊适配。

(2) 适配器合约的契约式集成。在 config.ts 中新增 opencodeStateDir 和 opencodeBin 配置项，其解析优先级为：环境变量显式指定 > 默认路径。在 detectAllRuntimes 的 DETECTORS 映射中增加 'opencode' 条目，将 RuntimeId 类型扩展为 'openclaw' | 'hermes' | 'claude' | 'codex' | 'opencode'。VALID_RUNTIMES 集合同步扩展，确保 agent-runtimes API 的 GET/POST 端点能正确识别和处理 opencode 类型。

### 4. OpenCode 会话扫描与统一模型映射

OpenCode 的本地会话通过新增的 opencode-sessions 模块进行扫描和映射。该模块遵循与 claude-sessions、codex-sessions、hermes-sessions 相同的设计模式，但增加了兼容式解析层以适应 OpenCode 会话格式的潜在变化。

(1) 会话文件发现。扫描函数 scanOpenCodeSessions 从配置的 opencodeStateDir 下的 projects 子目录中递归遍历，寻找符合 OpenCode 会话文件命名规范的文件（如 session-*.jsonl 或 conversation-*.json 等，由可配置的文件模式 glob 指定）。每次扫描的结果通过 TTL 缓存（默认 30 秒）减少重复文件 I/O。

(2) 兼容式解析。对于每个发现的会话文件，解析器首先尝试按 JSONL 逐行解析（每行对应一条消息记录），然后尝试按单 JSON 对象或数组解析。解析过程中使用宽松 JSON 解析器（复用项目已有的 parseJsonRelaxed），尽可能从非标准格式中提取关键字段：sessionId、model、timestamp、role、content、tokenUsage。无法识别的字段被静默跳过而非触发错误，确保即使 OpenCode 升级改变了会话格式，Mission Control 也能最大限度地展示已有数据。

(3) 统一会话模型映射。解析出的会话结构被映射为与 Claude/Codex/Hermes 会话一致的 opencodeSessionStats 接口，包含 sessionId、projectSlug、model、messageCount、inputTokens、outputTokens、firstMessageAt、lastMessageAt、isActive 等字段。在 sessions API 的 GET 端点中，scanOpenCodeSessions 的返回结果与现有 Claude、Codex、Hermes 会话一起通过 mergeLocalSessions 和 dedupeAndSortSessions 进行合并去重和排序，确保来自不同 runtime 的会话在 UI 中统一展示。

### 5. 会话继续的能力边界管理与渐进验证

与 Claude Code 和 Codex CLI 不同，OpenCode 的继续命令在当前项目环境中尚未被确认为稳定接口。方案通过"能力边界标记 + fixture/命令替身验证"两层机制，确保系统不会将未确认的能力展示为已完整支持。

(1) 能力边界标记机制。在 RuntimeStatus 接口中新增 capabilities 字段，描述每种 runtime 当前实际可用的操作列表。对于 OpenCode，初始 capabilities 至少包含 'detect'（检测）、'sessions_read'（读取会话）、'register'（注册为 agent），而 'sessions_continue'（继续会话）、'sessions_send'（发送消息）等在未经验证前标记为 unavailable。GET /api/agent-runtimes 的返回结果中包含 capabilities 字段，前端 agent-runtimes-section 组件根据 capabilities 决定是否展示"继续会话"按钮及相关操作入口，从而避免用户在 OpenCode 会话卡片上看到无法使用的操作。

(2) fixture/命令替身验证。在开发与测试阶段，通过 e2e-openclaw 测试框架中已有的 mock-gateway 模式，为 OpenCode 的 continue 命令提供 fixture 替身。测试用例首先通过 fixture 验证命令调用的参数格式和会话 ID 传递正确性，然后逐步替换为真实 opencode continue 命令进行集成验证。只有在真实命令执行成功并返回预期响应后，才将 'sessions_continue' 能力标记为 available。这种渐进式验证方式借鉴了项目中已有的 openclaw-gateway mock 测试模式。

(3) 会话继续适配层。在 sessions/continue API 的 POST 端点中，ContinueKind 类型扩展为 'claude-code' | 'codex-cli' | 'opencode'。当 kind 为 'opencode' 且 capabilities 已确认时，调用 opencode continue --session <id> <prompt> 命令。该调用封装在执行超时（默认 180 秒）和错误处理中，与 claude-code 和 codex-cli 分支保持一致的错误处理和审计日志记录。当 capabilities 未确认时，API 返回明确的错误信息，告知用户该能力尚不可用。

### 6. 与现有运行时体系的兼容性保障

方案的设计确保新增 OpenCode 支持不会破坏或改变 Claude Code、Codex CLI、Hermes Agent 和 OpenClaw 的现有行为。

(1) 运行时不变量。RuntimeId 的扩展遵循联合类型的增量扩展方式，现有的 DETECTORS 映射、INSTALL_FNS 映射、VALID_RUNTIMES 集合均以新增条目而非修改已有条目的方式进行扩展。各 runtime 的检测函数独立运行，互不依赖，OpenCode 检测失败不会影响其他 runtime 的检测结果。

(2) 会话聚合不变量。sessions API 的 GET 端点通过 mergeLocalSessions 和 dedupeAndSortSessions 逐层合并，新增的 opencode 会话来源以独立参数传入合并函数，不会改变现有 Claude/Codex/Hermes 会话的合并逻辑。会话去重仍以 (sessionId, source) 为联合键，确保不同 runtime 同 ID 会话不会互相覆盖。

(3) 适配器调度不变量。adapters 注册表以字符串键索引，新增 'opencode' 条目不影响现有 'openclaw'、'generic'、'crewai' 等条目的查询和调用。OpenCodeAdapter 的实现可复用 queryPendingAssignments 等共享工具函数，无需修改 adapter.ts 中定义的 FrameworkAdapter 接口契约。

(4) 配置不变量。opencodeStateDir 和 opencodeBin 配置项以与 claudeHome 相同的模式添加到 config.ts，使用环境变量优先、默认路径 fallback 的解析策略。OpenCode 配置的缺失不会导致系统启动失败，仅在调用 OpenCode 相关功能时返回明确的状态提示。

### 7. 关键处理流程

以下描述 OpenCode 接入后的关键处理流程，体现各模块之间的协作关系。

流程一：运行时检测与状态展示。用户打开 Mission Control 的 Agent Runtimes 设置面板 → 前端调用 GET /api/agent-runtimes → 服务端执行 detectAllRuntimes()，遍历 DETECTORS 映射中所有检测函数（包括新增的 detectOpenCode）→ detectOpenCode 检查 OPENCODE_STATE_DIR 或 ~/.opencode 目录存在性、执行 opencode --version、检查认证状态 → 返回统一的 RuntimeStatus 数组（含 capabilities 字段）→ 前端根据 capabilities 决定每个 runtime 卡片展示的操作按钮。

流程二：会话发现与聚合。用户打开 Sessions 面板 → 前端调用 GET /api/sessions → 服务端依次执行 getAllGatewaySessions()（OpenClaw 网关会话）、syncClaudeSessions() + getLocalClaudeSessions()、getLocalCodexSessions()、getLocalHermesSessions()、scanOpenCodeSessions()（新增）→ 各来源会话通过 mergeLocalSessions 合并，再通过 dedupeAndSortSessions 以 lastMessageAt 倒序去重 → 返回统一会话列表，每个会话包含 source 字段标识来源 runtime（如 'opencode'）。

流程三：条件会话继续。用户在 UI 中对某个 OpenCode 会话点击"继续"按钮 → 前端检查该 runtime 的 capabilities 中是否包含 'sessions_continue' → 若可用，调用 POST /api/sessions/continue，body 中 kind='opencode', id=<sessionId>, prompt=<用户输入> → 服务端验证 capabilities 后，执行 opencode continue --session <id> <prompt> → 返回结果。若不可用，前端展示提示信息，不发起 API 调用。

### 8. 技术效果

相比将 OpenCode 视为外部工具或手工记录的方式，本方案具有以下技术效果：

(1) 统一管理视图。OpenCode 会话与 Claude Code、Codex CLI、Hermes Agent 和 OpenClaw 网关会话在同一个会话列表中呈现，按最后活跃时间统一排序。用户在一个界面中即可浏览所有 agent 的工作历史，无需在多个工具之间切换或手工维护记录。

(2) 自动发现与零配置同步。通过可配置的状态目录发现和定时会话扫描，OpenCode 的本地工作记录会自动出现在 Mission Control 中，无需用户手工导入或配置。新的 OpenCode 会话在创建后，在下次扫描周期（受 TTL 缓存控制）内即可被 Mission Control 发现。

(3) 可演进的能力边界。通过 capabilities 字段的能力标记机制，系统可以在 OpenCode 新版本稳定后逐步开放高级功能（如继续会话、发送消息），而不会在能力未确认时给用户造成误导。这种渐进式集成模式降低了因外部工具接口不稳定导致的系统风险。

(4) 架构一致性。OpenCode 的集成完全复用 Mission Control 现有的 runtime 检测框架、会话扫描模式、适配器调度机制和会话继续 API，新增代码集中在独立的 opencode-sessions 模块和扩展点中，不修改现有运行时模块的核心逻辑，降低了引入新运行时对系统稳定性的影响。

### 9. 风险与待确认问题

(1) OpenCode 本地状态目录和文件格式的稳定性。本方案假设 OpenCode 的本地状态存储在 ~/.opencode 目录下，会话采用 JSON/JSONL 格式。如果 OpenCode 的实际实现使用完全不同的存储后端（如 SQLite 或 LevelDB），scanOpenCodeSessions 需要实现对应的读取适配器。当前的兼容式解析设计已考虑此风险，支持通过配置切换文件模式和后端类型。

(2) opencode continue 命令的接口稳定性。该命令的存在性和参数格式在当前项目环境中未被确认。方案通过 capabilities 标记机制和 fixture 验证流程来管理此风险：在确认前不暴露给终端用户，确认后通过测试验证确保接口兼容。

(3) 会话 ID 稳定性。如果 OpenCode 的会话 ID 在应用重启或版本升级后发生变化，可能导致 Mission Control 中已有的会话记录无法与新扫描的会话正确关联。应在实际集成时确认 OpenCode 会话 ID 的生成和持久化策略。

(4) 认证状态检测的准确性。当前方案通过检查环境变量或配置文件判断 OpenCode 的认证状态，但 OpenCode 可能采用 OAuth 或 keychain 存储等非文件认证方式。应参考 claude-sessions 中对 Claude Code OAuth 认证的多层次检测策略，为 OpenCode 实现类似的 fallback 检测链。
