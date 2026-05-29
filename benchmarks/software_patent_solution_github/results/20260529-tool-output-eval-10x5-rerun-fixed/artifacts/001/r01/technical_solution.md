## 技术方案

本方案提出一种将 OpenCode 作为原生 agent runtime 接入 Mission Control（MC）的软件技术方案。方案在 MC 现有的多运行时管理架构（Claude Code、Codex CLI、Hermes Agent、OpenClaw）基础上，引入一组可配置发现、适配器合约和兼容式解析机制，使 OpenCode 的本地会话能够被 MC 识别、展示和管理，同时明确声明能力边界，避免向用户展示未经确认的完整支持。

### 待解决的技术问题

现有 MC 系统的 agent runtime 管理基于固定的运行时类型（RuntimeId = openclaw | hermes | claude | codex），每种运行时对应一组硬编码的检测函数（DETECTORS）、安装策略（INSTALL_FNS）和会话扫描器（claude-sessions.ts、codex-sessions.ts、hermes-sessions.ts）。当需要接入 OpenCode 这一新的本地 agent runtime 时，面临以下技术问题：

1. OpenCode 的本地状态目录、消息文件格式和继续命令在系统中尚未实现，不能假设其存储结构与现有运行时一致；
2. 现有 DETECTORS 注册表（agent-runtimes.ts 第 467-472 行）和会话列表合并逻辑（sessions/route.ts 的 mergeLocalSessions 函数）均以硬编码方式逐运行时展开，新增运行时需修改多处核心路径；
3. 现有会话继续接口（sessions/continue/route.ts）仅支持 claude-code 和 codex-cli 两种 kind，无通用扩展点；
4. OpenCode 的某些能力（如会话继续、任务报告、心跳上报）可能无法完整对外暴露，系统需要在不伪造能力的前提下，仍能正确展示已发现的信息。

### 整体架构

本方案在 MC 现有架构基础上，新增一组可拔插的运行时扩展层，核心包含三个组件：(1) 可配置发现器（Discovery Provider），统一管理各运行时的路径发现逻辑；(2) 适配器合约（Runtime Adapter Contract），定义检测、扫描、继续、管理四类操作的接口；(3) 能力边界声明（Capability Boundary），在 UI 和 API 层显式标记每个运行时的操作可用性。

扩展层位于现有 agent-runtimes.ts 和 sessions/route.ts 之间，不改变现有 Claude、Codex、Hermes、OpenClaw 的硬编码逻辑路径——这些已有运行时通过适配器合约的一个默认实现保持行为不变。OpenCode 作为第一类扩展运行时，通过实现适配器合约接入。

### 可配置发现机制

为解决 OpenCode 本地状态路径不可假设的问题，系统在 config.ts 中引入一组可配置的环境变量，由运营者在部署时指定或在首次检测时通过交互式引导填入：

- OPENCODE_STATE_DIR：OpenCode 本地状态根目录（默认 ~/.opencode），用于定位会话存储和代理定义；
- OPENCODE_SESSION_GLOB：会话文件的 glob 模式（默认 {sessions,history}/**/*.jsonl），支持 JSONL 或嵌套 JSON 目录结构；
- OPENCODE_BIN：OpenCode CLI 可执行文件路径或命令名（默认 opencode），用于版本检测和会话继续；
- OPENCODE_CONTINUE_ARGS_TEMPLATE：会话继续命令的 argparse 模板（默认 "--resume {sessionId} {prompt}"），允许适配不同 CLI 参数约定。

这些配置项均提供默认值，使系统在 OpenCode 遵循常规安装路径时无需额外配置即可工作。当 OpenCode 的存储格式或命令接口发生变更时，仅需调整环境变量而无需修改代码。检测函数 detectOpenCode() 通过读取 OPENCODE_STATE_DIR 下的标记文件（如 config.json 或 version.txt）判断安装状态，通过执行 OPENCODE_BIN --version 获取版本号，通过检查 OPENCODE_STATE_DIR 下是否存在符合 OPENCODE_SESSION_GLOB 的文件判断是否存在历史会话。

### 适配器合约

系统定义统一的 RuntimeAdapter 接口（TypeScript interface），将每个 agent runtime 的四类操作标准化。该接口扩展自现有的 FrameworkAdapter（adapters/adapter.ts），增加检测与会话管理维度：

- detect(): RuntimeStatus —— 检测运行时是否安装、版本号、运行状态、认证状态，与现有 DETECTORS 注册表函数签名一致；
- scanSessions(limit: number): UnifiedSession[] —— 扫描本地会话并映射为 MC 统一会话格式（UnifiedSession），包含 sessionId、model、token 用量、活跃状态、来源标记（source: 'opencode'）；
- continue(sessionId: string, prompt: string): Promise<ContinueResult> —— 尝试继续指定会话，若运行时支持则返回响应文本，若不支持则返回能力未就绪的标记；
- getCapabilities(): RuntimeCapabilities —— 返回该运行时的能力位图，包含 canContinue、canHeartbeat、canReportTask、canDelete 等布尔标志。

每个运行时通过一个工厂函数注册到全局 RuntimeRegistry 中。Registry 的 detectAllRuntimes() 遍历所有已注册适配器调用 detect()，scanAllSessions() 聚合各适配器的 scanSessions() 结果，mergeSessions() 按来源和活跃时间去重排序。这替代了当前 sessions/route.ts 中硬编码的 getLocalClaudeSessions()、getLocalCodexSessions()、getLocalHermesSessions() 逐一调用模式。

### OpenCode 会话扫描与映射

针对 OpenCode 会话文件格式未知的问题，OpenCode 适配器的 scanSessions() 实现采用兼容式解析策略。解析器按以下顺序尝试解析每个匹配 OPENCODE_SESSION_GLOB 的文件：(1) 尝试按 JSONL（每行一个 JSON 对象）解析，提取 sessionId、model、timestamp、tokenUsage 等字段；(2) 若 JSONL 失败，尝试按顶层 JSON 对象/数组解析，按约定键名（如 "sessions"、"messages"、"history"）递归查找会话记录；(3) 若以上均失败，使用 fixture 替身（fixture/command stand-in）模式生成一条标注为 "unparseable" 的占位会话条目，附带文件名和修改时间，确保用户至少能看到会话存在。

解析出的每条会话映射为 UnifiedSession，其中 source 字段固定为 "opencode"、kind 字段为 "opencode"。会话的 model 字段从消息记录中的 "model" 键或文件元数据中提取；token 用量从 "usage" 对象中提取 input_tokens 和 output_tokens；活跃状态通过最后消息时间戳与 ACTIVE_THRESHOLD_MS 比较判定。

为提高解析正确率，系统支持在 OPENCODE_STATE_DIR 下放置一个 schema hint 文件（.mc-opencode-hint.json），由用户或 OpenCode 社区提供字段映射规则，例如定义 "sessionIdField": "id"、"timestampField": "created_at" 等映射关系。系统在扫描时优先使用 hint 文件中的映射规则，未提供时回退到自动推断。

### 会话继续与删除机制

会话继续功能通过 OPENCODE_CONTINUE_ARGS_TEMPLATE 配置项实现命令替身（command stand-in）机制。当用户在 MC 界面点击 OpenCode 会话的"继续"按钮时，系统执行模板替换后的 shell 命令，例如将模板 "--resume {sessionId} {prompt}" 中的 {sessionId} 替换为实际会话 ID、{prompt} 替换为用户的继续输入。

会话继续接口（sessions/continue/route.ts）从当前硬编码的 ContinueKind = 'claude-code' | 'codex-cli' 扩展为包含 'opencode'，处理逻辑从 if/else 分支重构为查找对应 RuntimeAdapter 并调用其 continue() 方法。当 OpenCode 适配器的 canContinue 能力标志为 false 时（例如 OPENCODE_CONTINUE_ARGS_TEMPLATE 未配置），API 返回 { ok: false, reason: 'capability_not_available' }，前端据此隐藏"继续"按钮或显示为禁用状态并附带说明文字。

对于 OpenCode 的会话删除操作，系统同样通过 RuntimeCapabilities 中的 canDelete 标志控制。若 OpenCode CLI 不支持 session delete 子命令，适配器标记 canDelete = false，前端仅允许从 MC 视图中隐藏会话记录（hide from view），而不尝试操作 OpenCode 本地存储。

### 与现有运行时的兼容性

方案在设计上保证对现有四种运行时（OpenClaw、Hermes、Claude Code、Codex CLI）的完全兼容。具体策略如下：

- 向后兼容：现有 DETECTORS、INSTALL_FNS、各会话扫描器（claude-sessions.ts、codex-sessions.ts、hermes-sessions.ts、sessions.ts）的代码路径不变。每个已有运行时通过一个 LegacyRuntimeAdapter 包装器实现 RuntimeAdapter 接口，其 detect() 委托到现有 detectXxx() 函数，scanSessions() 委托到现有扫描函数，continue() 委托到 sessions/continue 的现有逻辑。
- 渐进迁移：RuntimeRegistry 同时注册 LegacyRuntimeAdapter 和 OpenCodeAdapter。sessions/route.ts 的 GET 处理函数改为调用 RuntimeRegistry.scanAllSessions() 聚合所有会话，mergeLocalSessions 函数保留但改为从 Registry 获取各运行时会话列表后合并。
- 统一排序与会话去重：合并后的 UnifiedSession 列表按 lastActivity 降序排列、截断至 100 条。每条会话的 source 字段（'gateway' | 'local' | 'opencode'）在去重时作为复合键的一部分，确保不同来源的同名会话不会互相覆盖。
- UI 兼容：前端会话列表组件通过 source 或 kind 字段渲染不同的运行时图标和操作按钮。OpenCode 会话使用专门的品牌标识，与 Claude、Codex、Hermes 并列展示，不改变现有 UI 布局结构。

### 能力边界声明

为避免向用户展示 OpenCode 尚未确认支持的能力，系统在 API 响应和 UI 渲染中显式引入能力边界声明机制。

在 API 层，GET /api/agent-runtimes 返回的 RuntimeStatus 扩展一个 capabilities 字段，类型为 RuntimeCapabilities，逐项声明该运行时的操作可用性。对于 OpenCode，在未经充分验证的初始部署阶段，默认能力位图为：canContinue = false（除非 OPENCODE_CONTINUE_ARGS_TEMPLATE 已显式配置并通过 fixture/命令替身验证）、canHeartbeat = false、canReportTask = false、canDelete = false、canShowSessions = true、canShowTokens = true。每项能力在配置中开放前，需通过对应的 fixture 验证流程。

在 UI 层，前端根据 capabilities 标志决定是否渲染对应的操作按钮。对于标记为 false 的能力，按钮以禁用态展示并附带 tooltip，文案示例为"此功能需要 OpenCode 版本 ≥ x.y.z"或"此功能尚未经 Mission Control 验证"。这确保所有运行时在一个统一的管理界面中呈现一致的操作可达性，而不是让 OpenCode 缺少某些按钮而给用户造成困惑。

### Agent 注册与识别

OpenCode 代理通过两种路径进入 MC 的代理管理表（agents 表）：

- 本地磁盘扫描：在 local-agent-sync.ts 的 getLocalAgentRoots() 函数中增加 ~/.opencode/agents/ 目录。该目录下的子目录若包含 AGENT.md、agent.md、soul.md、identity.md 或 config.json 等标记文件，则被视为一个 OpenCode 代理定义。扫描器提取代理名称、角色、框架标记（framework = 'opencode'），按现有逻辑进行 SHA256 哈希去重和 upsert。
- 显式注册：用户可通过 POST /api/agents/register 接口显式注册 OpenCode 代理，在请求体中指定 framework = 'opencode'。服务器在 agents 表的 config JSON 字段中记录 framework 和 capabilities 快照，供前端区分运行时类型。

注册后，OpenCode 代理与其他运行时的代理并列出现在 GET /api/agents 的返回列表中。代理卡片通过 framework 字段展示对应的品牌标识，任务分配统计（taskStats）适用于所有代理类型，不区分运行时。

### Fixture 验证与命令替身

鉴于 OpenCode 的本地状态格式和 CLI 接口在当前项目环境中未实现，方案引入 fixture 验证与命令替身（command stand-in）机制，用于在将能力标志翻转为 true 之前进行自动化验证。

验证流程为：(1) 系统在 OPENCODE_STATE_DIR 下创建一个 fixture 会话，包含已知结构的消息记录；(2) 运行 scanSessions() 并断言解析结果与 fixture 定义一致；(3) 若 OPENCODE_CONTINUE_ARGS_TEMPLATE 已配置，构造模版命令并在 fixture 会话上执行，验证 CLI 返回码为 0 且输出可解析；(4) 所有验证通过后，该 OpenCode 实例对应的能力位图中的 canContinue 等标志才被翻转为 true。验证脚本作为 MC 启动检查（类似 openclaw-doctor.ts）的一部分运行，或通过 POST /api/agent-runtimes { action: 'detect', runtime: 'opencode' } 时附带 verifyCapabilities 参数触发。

### 技术效果

本方案通过上述技术手段，在 MC 现有多运行时管理架构上实现了 OpenCode 的原生接入，带来以下技术效果：

- 运行时扩展不再需要修改核心路径：通过 RuntimeAdapter 接口和 RuntimeRegistry 注册机制，新增 agent runtime 仅需实现一个适配器类并注册即可，无需修改 sessions/route.ts、agent-runtimes.ts 等核心文件的硬编码分支；
- OpenCode 本地会话自动被发现：可配置的路径发现和兼容式解析器使 MC 能够在不预设 OpenCode 存储格式的前提下，自动扫描并展示其历史工作记录，用户无需手工导入或记录；
- 能力差异透明化：能力边界声明机制确保用户在 UI 中清楚看到每个运行时可执行的操作范围，避免因缺失按钮或操作失败而产生"已完整支持"的误解；
- 存量运行时零影响：已有 Claude Code、Codex CLI、Hermes、OpenClaw 的管理逻辑通过 LegacyRuntimeAdapter 保持不变的代码路径，OpenCode 接入不引入回归风险；
- 渐进式验证：fixture 验证与命令替身机制使运维人员可以在不依赖 OpenCode 官方文档完整性的前提下，逐步验证并开放更多能力，降低因接口不匹配导致的运行错误。

### 风险与待确认事项

以下事项在当前项目环境条件下无法确认，需要在 OpenCode 实体可用后进一步验证：

- OpenCode 会话文件的准确 JSON/JSONL schema 和字段命名——当前方案通过 schema hint 文件和兼容式解析器降低依赖，但首次映射仍需人工确认或利用 OpenCode 官方文档；
- OpenCode CLI 的 --resume 子命令是否存在及其参数格式——OPENCODE_CONTINUE_ARGS_TEMPLATE 提供了配置弹性，但如果 OpenCode 完全不支持会话继续，该能力将永久标记为 false；
- OpenCode 是否支持多代理定义（类似 OpenClaw 的 agents 配置）——如果 OpenCode 仅支持单代理模式，~/.opencode/agents/ 扫描路径将最多发现一个代理；
- OpenCode 网关/守护进程模式是否存在——若 OpenCode 仅作为一次性 CLI 工具运行，则 running 状态检测将仅依赖会话文件时间戳的活跃度推断，而非进程存活检测。
