## 技术方案

本方案在 Mission Control 已有的多运行时（openclaw、hermes、claude、codex）管理架构之上，将 OpenCode 作为一种新的原生 agent runtime 接入。方案遵循现有运行时管理体系的统一模式：运行时检测与安装、会话发现与统一映射、Agent 自注册与同步，以及可选的会话接续。针对 OpenCode 本地状态格式尚未标准化的问题，方案引入适配器合约（Adapter Contract）与可配置发现机制，通过 fixture 替身和命令替身实现可验证的兼容式接入，并在 UI/API 层显式声明能力边界，避免向用户展示未经确认的功能。

### 技术问题

Mission Control 目前已支持 openclaw、hermes、claude、codex 四种 agent runtime 的原生管理，包括检测安装状态、发现本地会话记录、Agent 注册与同步，以及对 Claude Code 和 Codex CLI 会话的接续。OpenCode 是一种用户在本机使用的 agent 工具，其会话记录保存在本地文件系统中，但当前 Mission Control 无法识别这些记录。用户希望在 Mission Control 中看到真实的 OpenCode 会话并尽可能复用，而不是将其视为外部工具或手动记录。本方案要解决的技术问题包括：(1) 将 OpenCode 纳入 Mission Control 的运行时检测与安装体系；(2) 发现并解析 OpenCode 本地会话数据，映射为统一会话格式；(3) 在能力受限时向用户明确展示边界，避免误导。

### 核心技术方案：三层接入架构

OpenCode 接入方案沿袭 Mission Control 已有的运行时管理三层架构：(1) 运行时层——在 agent-runtimes 模块中新增 opencode 运行时类型，复用 detectAllRuntimes/detectRuntime/startInstall 接口；(2) 会话层——新增 opencode-sessions 会话扫描器，参照 codex-sessions 的 JSONL 文件扫描模式，通过适配器合约解析 OpenCode 本地状态目录；(3) Agent 管理层——扩展 local-agent-sync 的扫描根目录，将 OpenCode 的 agent 定义目录纳入发现范围，并在 sessions/continue API 中按需扩展 ContinueKind。

### 运行时检测与安装

在 agent-runtimes.ts 的 RuntimeId 联合类型中新增 'opencode'，并在 RUNTIME_META 中注册其元信息（名称、描述、认证提示）。检测函数 detectOpenCode 实现与 detectCodex 类似的候选二进制搜索：遍历 opencode、opencode-cli 等候选名称及 ~/.local/bin、/usr/local/bin 等常见路径，通过 spawnSync 执行 --version 获取版本信息。安装路径 installOpenCodeLocal 沿用 downloadAndReviewScript 安全流程：从可配置的安装脚本 URL 下载、经过注入扫描和 AI 安全审查后在临时目录执行，完成后验证二进制可用性。Docker 模式则通过 generateDockerSidecar 生成 OpenCode sidecar 容器模板。

### 适配器合约与可配置发现

由于 OpenCode 的本地状态目录、会话文件格式和继续命令在当前项目中尚未标准化，方案引入适配器合约（Adapter Contract）作为可配置的抽象层。该合约定义以下配置项（通过环境变量或 config.ts 注入）：(1) OPENCODE_STATE_DIR——OpenCode 本地状态根目录，默认 ~/.opencode；(2) OPENCODE_SESSIONS_DIR——会话文件子目录，默认 {STATE_DIR}/sessions；(3) OPENCODE_SESSION_FILE_PATTERN——会话文件匹配模式，默认 *.jsonl；(4) OPENCODE_CONTINUE_COMMAND——继续命令模板，默认 ['opencode', 'continue', '{sessionId}']；(5) OPENCODE_BIN——CLI 二进制名称，默认 opencode。所有默认值均为可配置的合理猜测，可通过 fixture 替身（如 tests/fixtures/opencode/ 下的样例文件）和命令替身（如 playright 测试中的 mock server）进行验证。

### 会话发现与统一映射

新增 opencode-sessions.ts 模块，参照 codex-sessions.ts 的 JSONL 扫描模式实现。核心流程：(1) listRecentOpenCodeSessionFiles——从可配置的 OPENCODE_SESSIONS_DIR 递归遍历，按 mtime 排序取最近 N 个 JSONL 文件（受 MAX_SESSION_FILE_BYTES 限制）；(2) parseOpenCodeSessionFile——逐行解析 JSONL，提取 sessionId（通过文件名推导或 JSON 内的 id 字段）、projectSlug、model、消息计数、token 用量、时间戳等字段，容忍缺失字段并使用默认值；(3) 字段映射——将解析结果映射为 CodexSessionStats 兼容结构，使其能直接汇入 sessions/route.ts 中的 getLocalOpenCodeSessions() 函数，与会话统一列表合并。兼容式解析意味着遇到未知字段不报错、遇到缺失字段使用 null/0 占位。

### 统一会话 API 集成

在 sessions/route.ts 的 GET 处理函数中新增 getLocalOpenCodeSessions() 调用，与会话合并逻辑 mergeLocalSessions 集成。OpenCode 会话以 kind='opencode'、source='local' 出现在统一会话列表中，展示 agent、model、token 用量、活跃状态等核心字段。agent 字段默认填充为 opencode-local，若会话 JSONL 中包含 project 或 agent_name 信息则优先使用。

### Agent 同步与注册扩展

扩展 local-agent-sync.ts 的 getLocalAgentRoots() 函数，新增 join(homedir(), '.opencode', 'agents') 扫描根目录。若该目录存在，其中的子目录（含 soul.md、identity.md、AGENT.md 等标记文件）将被扫描为 diskAgent 并参与双向同步。同时，扩展 VALID_RUNTIMES 集合和 POST /api/agent-runtimes 的 action=detect 支持 opencode 类型。

### 能力边界声明与降级展示

方案在以下层面显式声明能力边界，避免向用户展示未经确认的功能：(1) 运行时状态卡片——若 OpenCode 的认证状态无法通过标准方式检测（例如 OpenCode 无 auth status 命令或认证文件路径未知），authenticated 字段标记为 false，UI 显示“认证状态未知，请检查 opencode 配置”；(2) 会话接续——OpenCode 暂不加入 ContinueKind 联合类型（当前仅支持 claude-code 和 codex-cli），会话详情页不显示“继续此会话”按钮，待 OPENCODE_CONTINUE_COMMAND 经 fixture 验证后按需开启；(3) 会话活跃度检测——若 OpenCode 无 PID 文件或守护进程，isActive 仅基于 JSONL 文件的 mtime 新鲜度推断，UI 中以不同图标区分“推定活跃”和“确认活跃”（如 gateway 进程确认的场景）；(4) 安装状态——若 opencode 二进制不可检测但 OPENCODE_STATE_DIR 存在会话文件，运行时列表仍显示“已安装（会话可浏览）”状态。

### Fixture 替身与测试验证

参照 tests/fixtures/openclaw/ 的已有模式，在 tests/fixtures/opencode/ 下预置样例数据：opencode.json（模拟配置文件）、agents/ 和 sessions/ 子目录中的样例 JSONL 文件。新增测试用例 opencode-harness.spec.ts，验证：(1) GET /api/agent-runtimes 返回 opencode 运行时且 installed 状态正确；(2) GET /api/sessions 包含来自 fixture 的 OpenCode 会话；(3) POST /api/agents/sync?source=local 能发现 .opencode/agents/ 下的 agent。对于 OPENCODE_CONTINUE_COMMAND 的验证，通过 playwright 的 route 拦截构造命令替身，在不依赖真实 OpenCode 安装的前提下验证调用参数构造的正确性。

### 技术效果

(1) 统一管理体验：OpenCode 会话与 Claude Code、Codex CLI、Hermes 会话并列展示在同一会话列表中，用户无需切换工具即可掌握所有 agent runtime 的工作状态。(2) 零侵入接入：通过可配置的适配器合约，不需修改 OpenCode 源码或假定其内部格式，仅需配置本地状态目录即可启用会话发现。(3) 兼容现有体系：OpenCode 作为 RuntimeId 新成员，复用 agent-runtimes 的检测、安装、状态轮询基础设施，不破坏 Claude、Codex、Hermes 的已有逻辑。(4) 安全可控：安装流程沿用 downloadAndReviewScript 的双重审查（正则注入扫描 + AI 安全审查），Docker 模式通过 sidecar 模板隔离运行。(5) 诚实的能力声明：通过运行时状态卡片、继续按钮的条件渲染、活跃度图标区分等机制，确保用户不会对 OpenCode 的支持程度产生误解。

### 风险与待确认问题

(1) OpenCode 本地状态目录和会话文件格式可能随版本变化，需持续维护适配器合约的默认配置项。缓解措施：所有路径和格式参数均可通过环境变量覆盖，提供 fixture 回归测试。(2) OpenCode 的 continue 命令参数格式未经确认，当前方案不开放该能力；待 OpenCode 文档明确或通过逆向验证后，再扩展 ContinueKind。(3) OpenCode 可能不存在独立的 agent 定义目录，此时 local-agent-sync 的扫描结果为空，不影响其他运行时。(4) 若 OpenCode 的 JSONL 格式与 Codex 差异显著，兼容式解析可能丢失部分字段；方案中的 parseOpenCodeSessionFile 采用宽松解析策略，未知字段跳过、缺失字段填默认值，在 UI 中对应字段显示为“--”而非报错。

### 与项目环境及需求场景的对应关系

(1) 运行时检测与安装入口：src/lib/agent-runtimes.ts——新增 detectOpenCode、installOpenCodeLocal，扩展 RuntimeId 和 RUNTIME_META、VALID_RUNTIMES。(2) 配置与适配器合约：src/lib/config.ts——新增 OPENCODE_STATE_DIR、OPENCODE_SESSIONS_DIR 等配置项。(3) 会话扫描器：新增 src/lib/opencode-sessions.ts——参照 src/lib/codex-sessions.ts 模式。(4) 统一会话 API：src/app/api/sessions/route.ts——新增 getLocalOpenCodeSessions() 调用。(5) 会话接续：src/app/api/sessions/continue/route.ts——待验证后扩展 ContinueKind。(6) Agent 同步：src/lib/local-agent-sync.ts——扩展 getLocalAgentRoots()。(7) 运行时管理 API：src/app/api/agent-runtimes/route.ts——VALID_RUNTIMES 新增 'opencode'。(8) 测试：新增 tests/opencode-harness.spec.ts 和 tests/fixtures/opencode/。
