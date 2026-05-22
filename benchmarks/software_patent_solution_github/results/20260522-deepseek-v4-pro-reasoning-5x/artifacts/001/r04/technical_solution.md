## 技术方案

本方案在 Mission Control 已有的 OpenClaw、Hermes、Claude Code、Codex CLI 四种 agent runtime 管理能力基础上，将 OpenCode 扩展为第五种原生 runtime，使本地 OpenCode 会话与 agent 定义能被统一发现、展示与管理。方案遵循现有 runtime 接入的完整模式，同时引入可配置发现、适配器合约及能力边界声明机制，以应对 OpenCode 内部存储格式和控制命令尚未公开固化的现实约束。

### 技术问题

Mission Control 已支持 OpenClaw、Hermes、Claude Code、Codex CLI 四种 agent runtime 的检测、安装、会话扫描和继续操作。当用户在本机使用 OpenCode 完成 agent 工作后，这些工作记录无法被 Mission Control 识别和管理。直接沿用现有 runtime 接入模式面临三个障碍：(1) OpenCode 的本地状态目录和会话存储格式尚未公开固化；(2) OpenCode 是否提供等效于 claude --resume 或 codex exec resume 的继续命令尚不明确；(3) 若简单复用现有 UI 展示逻辑，可能在部分能力实际不可用时给用户造成“已完整支持”的误解。

### 核心技术方案

整体思路是在不修改现有 RuntimeId 联合类型结构的前提下，以增量方式添加 opencode 条目，并严格遵循 Claude/Codex/Hermes 已有的模式：运行时元数据注册 → 本地检测 → 会话扫描 → session API 合并 → continue/transcript API 接入。同时引入三个扩展机制以应对 OpenCode 的不确定性。

一、RuntimeId 扩展。在 agent-runtimes.ts 的 RuntimeId 联合类型中新增 'opencode'；VALID_RUNTIMES 集合同步更新；定义 RuntimeMeta { name: 'OpenCode', description: '本地 agent 运行时，支持会话管理和代码生成', authRequired: false, authHint: '' }；注册 detectOpenCode 函数检测 OPENCODE_HOME 或 ~/.opencode 目录是否存在以及 opencode 二进制是否可执行。

二、可配置的本地状态目录发现。不假设 OpenCode 使用固定的目录结构，采用三级 fallback 策略：首先检查环境变量 OPENCODE_STATE_DIR；其次尝试约定路径 ~/.opencode/state；最后回退到 XDG 规范路径 $XDG_DATA_HOME/opencode。配置文件 config.ts 中新增 opencodeHome 和 opencodeStateDir 字段，所有扫描器通过 config 读取目标路径，确保无环境变量时也能按默认约定工作。

三、适配器合约与兼容式解析。新建 opencode-sessions.ts 模块，签名对齐现有 scanCodexSessions() 和 scanHermesSessions()，输出统一的 SessionStats 结构（sessionId、projectSlug、model、messageCount、inputTokens、outputTokens、firstMessageAt、lastMessageAt、isActive）。会话文件探测同时尝试 .jsonl、.json 和 .db 三种扩展名，解析器将所有字段视为可选——缺失字段返回 null 而不抛异常，保证无论实际格式如何都能提取已有信息。配套构造 fixture 测试文件，在不依赖真实 OpenCode 安装的情况下独立验证解析器对边界输入（空文件、仅含部分字段、格式损坏）的鲁棒性。

四、会话统一展示与继续命令的显式不支持。在 sessions/route.ts 的 GET 处理中，调用 scanOpenCodeSessions() 并将结果映射为统一 Session 格式：kind='opencode'、id='opencode:{sessionId}'、source='opencode'，与其他 runtime session 一同进入去重合并和 Top 100 排序流水线。继续 API（POST /api/sessions/continue）和抄本 API（GET /api/sessions/transcript）对 kind='opencode' 返回 HTTP 501 Not Implemented 及结构化错误体 { error: 'NOT_IMPLEMENTED', message: 'OpenCode continue/transcript is not yet available' }，与静默返回空数组或成功相比，明确传达能力边界。

### 关键模块与处理流程

本方案涉及的新增和修改模块按数据处理流向组织为以下三个核心流程。

流程一：Runtime 检测与注册。agent-runtimes.ts 中新增 detectOpenCode() 函数，按以下顺序执行检测：(1) 读取 config.opencodeStateDir，若不存在则按三级 fallback 策略确定目标目录；(2) 通过 detectBinary(['opencode']) 检查 opencode 命令行工具是否可执行并获取版本号；(3) 检查目标目录下是否存在会话数据文件（.jsonl/.json/.db），若存在则 installed=true；(4) 返回 RuntimeStatus 结构。注册后 GET /api/agent-runtimes/ 自动将 OpenCode 纳入返回值，前端 Runtime 管理面板即时展示。

流程二：会话扫描与同步。opencode-sessions.ts 模块执行：扫描 config.opencodeStateDir 目录树，收集所有 .jsonl/.json/.db 候选文件；按 mtime 排序取最近 120 个文件；每个文件送入兼容式解析器 parseOpenCodeSessionFile()，逐行/逐条读取，尝试提取 sessionId、timestamp、model、message role、token usage 等字段；输出 OpenCodeSessionStats[] 列表。sessions/route.ts GET 中调用该函数，将结果映射为统一 Session 格式（kind='opencode'、source='opencode'），与 Claude/Codex/Hermes/gateway 四类 session 合并去重。

流程三：能力边界声明与反馈。会话统一对象新增 capabilities 字段（类型为 { continue: boolean, transcript: boolean }），对所有 runtime 通用。OpenCode 的 capabilities 初始值为 { continue: false, transcript: false }。前端 Runtime 卡片和会话详情面板据此渲染能力矩阵——可用操作为可点击按钮，不可用操作为灰色图标并附带 tooltip 说明。continue/transcript API 对 kind='opencode' 返回 501+结构化错误，确保 API 消费者不会将无响应误判为成功。

### 必要技术特征

特征一：RuntimeId 联合类型增量扩展。在不修改现有 openclaw/hermes/claude/codex 四个条目的前提下，新增 opencode 条目，VALID_RUNTIMES、DETECTORS、INSTALL_FNS 三个注册表同步更新，保证全链路类型安全。

特征二：三级 fallback 可配置发现机制。状态目录的确定遵循环境变量（OPENCODE_STATE_DIR）→ 约定路径（~/.opencode/state）→ XDG 规范（$XDG_DATA_HOME/opencode）的优先级链，通过 config.ts 集中管理，所有扫描器统一读取。

特征三：兼容式解析器。会话文件解析器对 .jsonl/.json/.db 三种格式同时探测，每条记录的字段（sessionId、model、timestamp、message role、tokens）全部声明为可选，缺失返回 null 而不中断解析流程，保证在格式不完全匹配时仍能提取可用信息。

特征四：能力矩阵显式声明与会话级能力字段。Session 统一对象新增 capabilities: { continue: boolean, transcript: boolean } 字段，所有 runtime 会话均携带该字段。前端据此渲染可用/不可用操作，不可用操作附带说明 tooltip。

特征五：fixture 驱动离线验证。解析器通过构造 fixture 测试文件（含正常、空文件、仅部分字段、格式损坏四种场景）独立验证，不依赖真实 OpenCode 安装。fixture 同时充当 OpenCode 会话格式的文档化参考。

### 技术效果

效果一：统一管理体验。OpenCode 会话与 Claude/Codex/Hermes 会话在同一个 Session 列表中展示，用户无需切换工具或手工记录即可查看所有本地 agent 工作。

效果二：零破坏兼容。整个方案以增量方式实现，不修改现有四种 runtime 的任何检测、扫描、API 逻辑；统一 Session 格式的 capabilities 字段对已有 runtime 默认设为 true，前端仅对 opencode 展示差异化。

效果三：能力边界清晰无误导。通过 capabilities 字段 + 501 状态码 + 前端灰色 tooltip 三层机制，用户在任何界面都能明确获知哪些操作当前可用、哪些尚不支持，避免“已完整支持”的误解。

效果四：可演进架构。三级 fallback 发现、兼容式解析和 fixture 验证机制使系统在 OpenCode 存储格式正式公开后，只需更新解析逻辑和 capabilities 字段即可提升支持等级，无需修改架构或接口。

### 风险与待确认问题

风险一：OpenCode 会话存储格式尚未公开。当前方案采用 .jsonl/.json/.db 三种格式同时探测，解析器字段全部可选。若实际格式与三种假设均不匹配，会话列表将为空，但不会产生错误。建议向 OpenCode 社区确认实际格式后调整解析逻辑。

风险二：继续命令是否存在。若 OpenCode 无等效于 --resume 的 CLI 参数，continue API 将长期保持 501。建议确认 OpenCode 是否支持从指定会话继续交互，若支持则按 claude-code 模式接入；若当前不支持，可接受“只读展示”的阶段性交付。

风险三：OpenCode Agent 定义格式兼容性。local-agent-sync.ts 当前支持目录型（AGENT.md + config.json）和单文件 YAML frontmatter 型两种 agent 定义格式。若 OpenCode 采用第三种格式，需额外适配解析逻辑。

风险四：UI 能力矩阵改造范围。Session 统一对象的 capabilities 字段和 Runtime 卡片的能力矩阵展示需要前端同步改造，应确认本次交付范围是否包含前端改动。
