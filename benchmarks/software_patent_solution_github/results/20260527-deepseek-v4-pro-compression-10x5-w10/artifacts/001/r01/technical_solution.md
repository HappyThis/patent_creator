## 技术方案

本方案在 Mission Control（以下简称 MC）已有的 agent runtime 管理体系上，新增对 OpenCode 作为原生本地 agent runtime 的支持。MC 当前已支持 openclaw、hermes、claude、codex 四种 runtime，每种 runtime 通过独立的检测器（detector）、会话扫描器（session scanner）和任务扫描器（task scanner）接入 MC 的统一管理界面。本方案在此基础上引入可配置的运行时发现机制、适配器合约与兼容式解析、能力边界声明等关键机制，使 OpenCode 能够以与现有 runtime 一致的方式被 MC 识别、展示和管理，同时不要求对 OpenCode 内部实现做任何假设。

### 整体架构

MC 的 agent runtime 管理体系由三个核心层次构成：运行时检测层、会话/任务扫描层和统一展示层。运行时检测层通过 RuntimeStatus 接口统一描述各 runtime 的安装状态、版本号、运行状态和认证状态，由 DETECTORS 注册表按 RuntimeId 分发到对应的检测函数。会话扫描层为每个 runtime 提供独立的扫描器（如 scanClaudeSessions、scanHermesSessions、scanCodexSessions），将本地状态文件解析为统一结构后写入 MC 的 SQLite 数据库。统一展示层通过 REST API（如 GET /api/claude/sessions）和 MC UI 面板呈现所有已发现会话。OpenCode 的接入沿用了这一三层架构，新增 opencode 作为第五种 RuntimeId，并在各层注入适配器合约以处理 OpenCode 的未知状态格式。

### 可配置的运行时发现机制

由于 OpenCode 的本地状态目录、消息文件格式在方案设计时无法预先确定，本方案不采用为 OpenCode 硬编码固定路径和固定解析逻辑的方式。取而代之的是可配置的运行时发现机制，通过以下环境变量允许用户或部署脚本声明 OpenCode 的本地状态位置：OPENCODE_STATE_DIR（OpenCode 状态根目录，默认为 ~/.opencode）、OPENCODE_SESSIONS_DIR（会话记录子目录，默认为 ${OPENCODE_STATE_DIR}/sessions）、OPENCODE_SESSIONS_FORMAT（会话文件格式，支持 "jsonl"、"json"、"sqlite"，默认 "jsonl"）。这些配置项遵循 MC 已有的配置模式（参考 config.ts 中 claudeHome、openclawStateDir 等），由 MC 统一配置模块在启动时读取并暴露为 config.opencodeStateDir 等字段。

### 会话适配器合约与兼容式解析

为解决 OpenCode 会话格式未知的问题，本方案定义了一个会话适配器合约（OpenCodeSessionAdapter），该合约不假定 OpenCode 的内部数据结构，而是定义从原始本地数据到 MC 统一会话表示（OpenCodeSessionStats）的转换接口。

适配器合约定义如下：OpenCodeSessionAdapter 接口包含 scanSessions(rootDir, format) 方法，接收配置的会话目录和格式标识，返回 OpenCodeSessionStats 数组。OpenCodeSessionStats 结构与现有 Claude/Codex 会话统计结构对齐，包含 sessionId、projectPath、model、userMessages、assistantMessages、inputTokens、outputTokens、firstMessageAt、lastMessageAt、isActive 等字段。内置的兼容式解析器按 format 参数选择解析策略：若格式为 "jsonl"，逐行解析 JSON 并尝试从常见字段名（type、message、timestamp、usage 等）提取信息；若格式为 "json"，解析整体 JSON 对象或数组；若格式为 "sqlite"，以只读模式打开 SQLite 数据库并探测 sessions 表结构。

为验证适配器解析逻辑的正确性而不依赖真实的 OpenCode 安装，本方案引入 fixture 验证机制。在 opencode-sessions 模块的测试中，预置一组代表不同 OpenCode 版本的 fixture 文件（sample-v1.jsonl、sample-v2.json、sample-v3.db 等），适配器对每种 fixture 执行解析，断言输出的 OpenCodeSessionStats 中各字段符合预期。这一机制使得适配器可以在 OpenCode 实际版本演进时通过补充新 fixture 来验证兼容性，无需修改核心解析逻辑。

### 能力边界声明与渐进式支持

为避免给用户造成“所有操作都已完整支持”的误解，本方案在 MC 的 UI 和 API 中引入能力边界声明机制。每个 runtime 在 RuntimeMeta 中新增 capabilities 字段，为布尔型标记映射，声明该 runtime 在当前版本支持的能力。OpenCode 的初始能力声明为：session_list（支持，即浏览会话列表）、session_continue（未知，取决于 OpenCode 是否提供继续命令）、session_terminate（未知）、task_management（未知）、cost_tracking（未知，取决于会话文件是否包含 token 用量信息）、agent_identity（未知）。

对于标记为“未知”的能力，MC 前端在渲染 OpenCode 会话时通过视觉区分来传达不确定性：例如会话列表中的“继续”按钮以禁用态展示并附带 tooltip 说明“该能力在当前 OpenCode 版本中尚未确认支持”；成本列显示“--”而非 0.00 以避免误导。同时，RuntimeMeta 中的 capabilities 字段可通过环境变量或配置文件覆盖：当用户确认 OpenCode 实际支持某能力后，可设置 OPENCODE_CAP_SESSION_CONTINUE=true 等变量启用对应 UI 控件。这一机制使得 OpenCode 的集成可以渐进式演进——随着 OpenCode 本身的成熟和 MC 适配器的完善，能力边界逐步收缩，UX 逐步与 Claude、Codex 等 runtime 对齐。

### 处理流程

OpenCode 会话从发现到展示的完整处理流程分为五个步骤，贯穿 MC 的三层架构。

1. 运行时检测：当 MC 启动或调用 GET /api/agent-runtimes 时，detectOpenCode() 函数根据 config.opencodeStateDir 检查 OpenCode 状态目录是否存在、尝试执行 opencode --version 获取版本号。检测结果封装为 RuntimeStatus 返回，包含 installed、version、running、authenticated 等字段。
2. 会话扫描触发：扫描可由前端手动触发（POST /api/opencode/sessions，遵循与 POST /api/claude/sessions 相同的模式），也可由 MC 内置的定时调度器按可配置间隔（默认 60 秒）自动触发。扫描遵循 30 秒节流策略，避免频繁磁盘 I/O。
3. 适配器解析：scanOpencodeSessions 函数读取配置的会话目录，按 OPENCODE_SESSIONS_FORMAT 选择解析器，对每个文件执行兼容式解析。解析逻辑优先匹配已知字段名，无法匹配的字段静默跳过，不抛出异常导致整体扫描失败。
4. 数据库同步：解析后的 OpenCodeSessionStats 通过 upsert 写入 MC 的 SQLite 数据库中的 opencode_sessions 表（表结构与 claude_sessions 对齐）。写入前将所有已有记录标记为非活跃，写入后根据 lastMessageAt 重新计算 isActive。
5. 展示与控制：MC 前端通过统一的会话面板渲染 OpenCode 会话，根据 capabilities 声明控制各操作按钮的可用状态。API 层提供 GET /api/opencode/sessions（含 active、project、limit、offset 查询参数）和 POST /api/opencode/sessions（触发手动扫描）。

### 技术效果

本方案通过将 OpenCode 作为原生 runtime 接入 MC，相比将其视为外部工具或手工记录，具有以下技术效果：第一，统一管理体验——用户可以在 MC 的同一面板中查看 OpenCode、Claude、Codex、Hermes 的会话，无需切换到 OpenCode 自身的界面或手工整理记录；第二，可扩展的适配器合约——当 OpenCode 版本更新导致状态格式变化时，只需补充新的 fixture 或调整解析策略，无需改动 MC 核心架构；第三，诚实的能力边界——通过 capabilities 声明和 UI 层面的视觉区分，用户明确知道哪些操作当前可用、哪些尚不支持，避免了“假支持”带来的操作失败和信任损耗；第四，与现有 runtime 架构的兼容——新增 opencode 不改变 DETECTORS 注册表、RuntimeStatus 接口、会话数据库表结构等现有抽象，Claude、Codex、Hermes 的检测和扫描逻辑完全不受影响。

### 风险与待确认问题

以下是本方案在当前阶段需要后续确认的风险点和待确认问题：第一，OpenCode 的会话文件具体格式（字段名、嵌套结构、时间戳精度）尚未确认，当前的兼容式解析器基于常见 CLI agent 工具的通用字段名设计，可能与实际 OpenCode 输出存在差异——建议在获得 OpenCode 真实会话文件后运行 fixture 验证并调整解析逻辑。第二，OpenCode 是否提供 CLI 命令（如 opencode continue <session-id>）以恢复历史会话尚不确定——如果不存在此类命令，“继续会话”能力将无法支持，UI 中对应按钮应保持禁用态。第三，OpenCode 的版本号查询命令（如 opencode --version）的具体输出格式未知——detectOpenCode 中的版本提取正则可能需要针对实际输出调整。第四，OpenCode 在多实例并发场景下的会话文件锁定策略未知——scanOpencodeSessions 当前以只读方式访问文件，如果 OpenCode 以排他锁写入会话文件，扫描器应在文件访问失败时静默跳过而非中断整体扫描。
