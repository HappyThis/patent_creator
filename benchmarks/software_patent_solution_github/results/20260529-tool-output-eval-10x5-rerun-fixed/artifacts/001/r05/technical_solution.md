## 技术方案

本方案提出一种可扩展的本地 agent runtime 接入机制，使 Mission Control（以下简称 MC）能够将 OpenCode 作为原生 agent runtime 进行识别、会话发现与生命周期管理，同时保持对现有 Claude Code、Codex CLI、Hermes Agent 及 OpenClaw Gateway 等运行方式的完全兼容。

### 技术问题说明

当前 MC 已通过 RuntimeStatus 接口和 DETECTORS 注册表管理四种 agent runtime：OpenClaw、Hermes、Claude Code 和 Codex CLI。每种 runtime 通过硬编码的检测函数发现二进制、配置文件和认证状态，通过专属会话扫描器读取本地会话数据。当需要接入 OpenCode 这种新的本地 agent runtime 时，现有架构面临三个技术挑战：一是 OpenCode 的本地状态目录、消息文件格式和继续命令在当前系统中尚未定义，无法通过硬编码路径直接接入；二是 OpenCode 的能力集合（如会话继续、转录查看）可能与现有 runtime 不完全一致，直接复用现有 UI 逻辑会造成“已完整支持”的误导；三是每增加一种 runtime 都需新增一套检测与会话扫描代码，缺乏可复用的接入模式。本方案通过引入可配置的运行时适配器合约、能力边界声明机制和兼容式会话解析，在不修改现有四种 runtime 行为的前提下解决上述问题。

### 整体架构

本方案在现有 MC 运行时管理架构之上引入三个新组件：运行时适配器合约（Runtime Adapter Contract）、可配置会话发现器（Configurable Session Discoverer）和能力边界声明器（Capability Declarator）。运行时适配器合约定义了一组标准化的检测、会话发现和命令映射接口，每种新 runtime 通过实现该合约接入 MC。OpenCode 作为新的 RuntimeId（'opencode'）纳入 DETECTORS 注册表，其检测器通过适配器合约读取配置而非硬编码路径。会话发现器根据适配器提供的配置信息（状态目录、文件 glob 模式、格式提示）扫描并解析 OpenCode 的本地会话记录。能力边界声明器在 API 响应和 UI 中标记每个 runtime 的已确认能力和未确认能力，避免将未知能力展示为已完整支持。

### 运行时适配器合约

运行时适配器合约（RuntimeAdapter）是本方案的核心抽象，定义每个本地 agent runtime 需要向 MC 暴露的标准化契约。该合约包含以下接口：

- detect(): RuntimeStatus — 检测 runtime 是否已安装、版本、运行状态和认证状态。实现方式可以是二进制探测、配置文件检查或端口扫描。
- discoverSessionFiles(config: AdapterConfig): DiscoveredSessionFile[] — 返回本地状态目录中属于该 runtime 的会话文件路径、修改时间和大小，供后续解析。
- parseSessionFile(filePath: string, hints: FormatHints): SessionStats | null — 解析单个会话文件，提取会话 ID、模型、消息计数、token 用量和活跃状态。
- getContinueCommand(sessionId: string, prompt?: string): CommandSpec | null — 返回用于继续该会话的 CLI 命令规格；若该 runtime 不支持继续，返回 null。
- getCapabilities(): RuntimeCapabilities — 声明该 runtime 当前支持的能力集合，包括 session_discovery、session_continue、transcript_view、cost_tracking 等布尔标记。

AdapterConfig 是一个可由管理员通过环境变量或 MC 配置面板设置的 JSON 配置对象，至少包含：stateDir（本地状态目录路径）、sessionFileGlob（会话文件 glob 模式，如 **/*.jsonl）、formatHints（文件格式提示，如 'jsonl'、'sqlite'、'json'）和 continueCommandTemplate（继续命令模板，如 'opencode continue --session {sessionId} --prompt "{prompt}"'）。当 OpenCode 的实际状态目录、文件格式或命令在后续被确认时，只需更新 AdapterConfig 即可完成适配，无需修改检测器代码。

### OpenCode 运行时检测器

OpenCode 的运行时检测器 detectOpenCode() 遵循现有 DETECTORS 注册模式，但通过适配器配置驱动而非硬编码路径。检测逻辑分三层：

1. 二进制检测：通过 detectBinary() 工具函数在常见安装路径中搜索 opencode 或 opencode-cli 二进制，获取版本信息。搜索路径包括 ~/.local/bin/、/usr/local/bin/ 以及 pnpm/npm 全局目录，与现有 Claude/Codex 检测逻辑一致。
2. 配置目录检测：读取 AdapterConfig.stateDir（默认为 ~/.opencode/），检查该目录是否存在以及其中是否包含可识别的会话或配置文件。若目录存在，即使二进制未检测到，也将 installed 标记为 true，以覆盖二进制安装在非标准路径的场景。
3. 认证状态检测：检查 AdapterConfig 中指定的认证文件或环境变量（如 OPENCODE_API_KEY），若存在则认为已认证。

RuntimeId 'opencode' 将被加入 VALID_RUNTIMES 集合，使现有的 /api/agent-runtimes GET/POST 端点可以查询和安装 OpenCode。检测函数通过统一的 DETECTORS['opencode'] 注册，detectAllRuntimes() 自动将其纳入全局扫描。RUNTIME_META 中新增 opencode 条目，描述其基本信息、认证要求与认证提示。

### 可配置会话发现与兼容式解析

OpenCode 会话发现复用现有的会话扫描模式（Claude 扫描 ~/.claude/projects/、Hermes 读取 ~/.hermes/state.db、Codex 扫描 ~/.codex/sessions/），但通过适配器配置驱动发现路径和解析策略。

会话文件发现：discoverOpenCodeSessionFiles() 根据 AdapterConfig.sessionFileGlob（默认 **/*.jsonl）在 AdapterConfig.stateDir（默认 ~/.opencode/sessions/）下递归扫描匹配的会话文件。扫描结果按修改时间降序排列，受 MAX_SESSION_FILE_BYTES 限制跳过超大文件，与现有 Claude 会话扫描器的安全策略一致。

兼容式会话解析：parseOpenCodeSessionFile() 按 AdapterConfig.formatHints 选择解析策略。若格式提示为 'jsonl'，逐行读取 JSON 对象，通过 AdapterConfig 中可配置的字段映射表（fieldMapping）将 OpenCode 的 JSON 字段映射到 MC 统一的 SessionStats 结构。字段映射表至少包含：sessionId 字段名、model 字段名、role 字段名、message.content 路径、usage.input_tokens 路径、usage.output_tokens 路径和 timestamp 字段名。当 OpenCode 的实际 JSON schema 与默认映射不一致时，管理员可通过配置更新映射表而无需修改解析代码。对于 miss 的字段，解析器以 null 或 0 填充，不中断解析流程。

活跃状态判定：复用现有 ACTIVE_THRESHOLD_MS 机制（默认 90 分钟），根据会话最后一条消息的时间戳判断会话是否活跃。若 OpenCode 的消息文件中不含时间戳字段，通过文件的 mtime 作为降级判定依据。

### 命令替身与能力边界声明

由于 OpenCode 的 continue 命令在当前阶段尚未被确认，本方案设计了命令替身（Command Stub）与能力边界声明两层机制，确保系统在能力未确认时不向用户展示误导性功能。

命令替身机制：在 AdapterConfig 中，continueCommandTemplate 字段可配置一个命令模板字符串。当 OpenCode 的 continue 命令格式未被确认时，该模板可设置为 null，此时 getContinueCommand() 返回 null，MC 前端据此隐藏“继续会话”按钮。管理员也可将模板配置为一个 fixture 命令（如 'echo "OpenCode continue not yet available" && exit 1'），以便在 UI 中保留入口但在实际调用时返回明确的不可用提示。一旦 OpenCode 的 continue 命令被确认，管理员只需将模板更新为真实命令（如 'opencode continue --session {sessionId} --message "{prompt}"'），即可启用完整功能。

能力边界声明：每个 runtime 通过 getCapabilities() 返回一组布尔标记，至少包含：session_discovery（是否支持会话发现）、session_continue（是否支持继续会话）、transcript_view（是否支持转录查看）、cost_tracking（是否支持费用追踪）、active_detection（是否支持活跃状态检测）。MC 前端和 CLI 在渲染 runtime 相关功能时读取这些标记：标记为 false 的功能显示为“暂不支持”而非“已支持”；标记为 null 的功能（表示尚未确认）显示为“待验证”并附带说明。这一机制确保了 OpenCode 与 Claude Code 等成熟 runtime 在 UI 中呈现不同的能力矩阵，用户不会误以为所有操作都已完整支持。

此外，/api/agents/sync 端点的本地 agent 扫描路径列表 getLocalAgentRoots() 中新增 ~/.opencode/agents/ 目录，使 OpenCode 的 agent 定义文件（如 AGENT.md、soul.md）可通过现有本地同步机制被 MC 发现。扫描逻辑复用 local-agent-sync.ts 中已有的 IDENTITY_FILES 和 CONFIG_FILES 识别规则。

### 兼容性设计与技术效果

本方案在所有关键扩展点上采用增量追加而非修改的策略，确保对现有四种 runtime 的零影响：

- DETECTORS 注册表只追加 opencode 条目，不修改现有 openclaw/hermes/claude/codex 检测函数。
- VALID_RUNTIMES 集合只追加 'opencode'，不修改其余值。
- RUNTIME_META 只追加 opencode 元数据条目。
- /api/agent-runtimes 端点的 GET/POST 逻辑不修改；新增 RuntimeId 自动纳入 detectAllRuntimes() 返回结果。
- 现有 Claude/Hermes/Codex 会话扫描器不作任何改动；OpenCode 会话发现由独立模块实现。
- /api/agents/sync 端点仅在下游 getLocalAgentRoots() 中追加目录路径，不改变同步逻辑。

本方案带来的技术效果包括：（1）可扩展性——新增本地 agent runtime 只需实现适配器合约并提供 AdapterConfig，无需修改 MC 核心代码；（2）安全性——通过可配置发现路径和字段映射表，避免了对未知文件格式的硬编码假设，解析失败时以 null 降级而非中断；（3）用户体验一致性——OpenCode 会话在 MC 中以与 Claude/Hermes/Codex 会话相同的统一视图呈现（会话 ID、模型、消息计数、token 用量、活跃状态），用户可以在同一面板中查看所有 runtime 的会话活动；（4）能力透明度——通过能力边界声明机制，MC 前端自动区分“已支持”“暂不支持”和“待验证”三种能力状态，避免对 OpenCode 的能力产生误解；（5）渐进式交付——管理员可通过更新 AdapterConfig 逐步启用 OpenCode 的各项能力，无需等待一次性完整实现。

### 风险与待确认事项

以下事项需要在实际开发中进一步确认，但不影响本方案的整体架构可行性：

- OpenCode 的实际本地状态目录路径：当前方案默认 ~/.opencode/，需在 OpenCode 发布后确认；若为其他路径，修改 AdapterConfig.stateDir 默认值即可。
- OpenCode 的会话消息文件格式：方案通过 fieldMapping 支持 JSONL/JSON/SQLite 等多种格式，但字段映射表的默认值需在 OpenCode 格式确认后校准。
- OpenCode 的 continue 命令语法：当前以命令模板占位，确认后替换 continueCommandTemplate 即可启用；在此期间能力声明中 session_continue 标记为 false。
- OpenCode 的二进制名称：假设为 opencode 或 opencode-cli；若实际名称不同，修改 detectBinary() 的候选名称列表即可。
- OpenCode 的认证机制：假设支持环境变量或配置文件；若 OpenCode 使用 OAuth 或其他认证流程，需实现对应的认证检测逻辑，但适配器合约中的 detect() 接口已预留了认证检测的扩展点。
