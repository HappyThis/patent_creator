## 技术方案

本技术方案描述如何将 OpenCode 作为一种原生 agent runtime 接入 Mission Control（以下简称 MC），使 MC 能够像管理已有的 Claude Code、Codex CLI、Hermes Agent、OpenClaw 等运行时一样，自动发现、识别、展示和继续使用用户在本地产生的 OpenCode 工作记录。

### 一、技术问题

MC 已内置对 Claude Code、Codex CLI、Hermes Agent 和 OpenClaw 四种 agent runtime 的支持，每种运行时通过各自的本地状态目录、会话存储格式和命令行接口被 MC 发现和管理。OpenCode 作为一种独立于上述四种的本地 agent 运行时，其状态目录结构、会话持久化格式和继续会话命令均与已有运行时不同。若不在 MC 中建立对 OpenCode 的统一适配机制，用户只能在 MC 之外手工管理 OpenCode 会话，无法享受跨运行时的统一会话视图、状态监控和会话继续能力。

### 二、核心架构

MC 采用三层架构将新的 agent runtime 纳入统一管理：运行时检测层发现本地安装的 OpenCode 及其版本、认证状态；会话发现层扫描 OpenCode 本地状态目录，解析会话数据并映射到 MC 统一会话模型；会话控制层通过 OpenCode 命令行接口执行会话继续操作。三层之间通过适配器合约（FrameworkAdapter 接口）与事件总线（eventBus）解耦，新增 OpenCode 不影响已有运行时的行为。

### 三、关键模块与处理流程

MC 已有的运行时管理模块（agent-runtimes.ts）维护 RuntimeId 联合类型和 RUNTIME_META 元数据映射。新增 OpenCode 需在 RuntimeId 中增加 'opencode' 枚举值，在 RUNTIME_META 中注册其名称、描述、认证需求等元数据。检测函数 detectOpenCode() 通过探测 OpenCode 可执行文件（如 'opencode --version'）和本地状态目录（如 ~/.opencode/）的存在性，返回 RuntimeStatus 结构，包含 installed、version、running、authenticated 等字段。该检测结果被 MC 仪表盘的运行时健康面板消费，与已有的 Claude、Codex、Hermes 运行时状态并列展示。

会话发现模块 opencode-sessions.ts 负责扫描 OpenCode 本地状态目录。该模块通过可配置的发现路径定位 OpenCode 状态目录（默认为 ~/.opencode/），遍历其中的会话存储文件，解析为统一的 OpenCodeSessionStats 接口。

参照已有的会话扫描模式，OpenCode 会话解析采用兼容式解析策略：优先按已知的 OpenCode 会话文件格式（JSON 或 JSONL）解析关键字段（sessionId、model、时间戳、token 统计、消息计数），对无法识别的字段静默跳过。若 OpenCode 的实际会话格式与预期不同，系统通过可配置的适配器路径支持注入自定义解析逻辑，避免因格式差异导致整个扫描流程失败。

### 四、会话继续机制

MC 已有的会话继续接口（POST /api/sessions/continue）当前支持 claude-code（通过 'claude --print --resume' 命令）和 codex-cli（通过 'codex exec resume' 命令）两种类型。新增 OpenCode 需在该接口的 ContinueKind 联合类型中增加 'opencode' 分支。

由于 OpenCode 的具体继续命令在当前项目环境中尚未确定，方案采用可配置的命令模板机制：通过配置项（如 OPENCODE_CONTINUE_COMMAND）指定继续命令模板，模板变量包括 {sessionId} 和 {prompt}。运行时在执行继续操作时将实际参数填入模板，通过 runCommand() 工具函数执行并捕获输出。该模板机制同时支持为 OpenCode 配置 fixture/命令替身（command stand-in），用于在 OpenCode 未真实安装或仅需验证集成的场景下进行自动化测试。

### 五、适配器合约与事件集成

MC 通过 FrameworkAdapter 接口定义运行时适配器的标准合约，包含 register、heartbeat、reportTask、getAssignments、disconnect 五个方法。新增 OpenCodeAdapter 实现该接口，framework 字段值为 'opencode'。该适配器在 adapters/index.ts 的适配器工厂映射中注册，使 MC 的代理注册、心跳监控、任务报告等通用流程能够复用 OpenCode 运行时，无需修改调用方代码。

OpenCodeAdapter 通过 eventBus 广播 agent.created、agent.status_changed、task.updated 等标准事件，使其与已有的 OpenClawAdapter、GenericAdapter、ClaudeSdkAdapter 等适配器通过同一事件通道被 MC 前端消费。这种设计确保 OpenCode 代理的状态变更能实时反映在仪表盘、活动时间线和代理网络面板中，无需为 OpenCode 单独开发前端组件。

### 六、可配置发现机制

OpenCode 的本地状态目录、配置文件和可执行文件路径通过环境变量实现可配置发现，遵循 MC 已有的多级环境变量回退模式。新增配置项包括：OPENCODE_STATE_DIR（状态目录，默认为 ~/.opencode/）、OPENCODE_CONFIG_PATH（配置文件路径，默认为 ~/.opencode/config.json）、OPENCODE_BIN（可执行文件路径，默认为 'opencode'）。这些配置项在 config.ts 中统一解析，支持与已有的 openclawConfigPath、claudeHome 等配置并列管理。

会话发现模块 opencode-sessions.ts 使用该配置路径进行目录扫描。若 OpenCode 的实际状态目录结构与预期不同，用户可通过设置 OPENCODE_STATE_DIR 环境变量指向实际路径，无需修改代码。该机制与已有的 OPENCLAW_STATE_DIR、MC_CLAUDE_HOME 等环境变量保持一致的命名和使用模式，降低用户学习成本。

### 七、统一会话视图

MC 的会话 API（GET /api/sessions）已实现跨运行时的统一会话合并逻辑。该接口在 getLocalOpenCodeSessions() 中调用 opencode-sessions.ts 的扫描函数，将会话数据映射为统一的会话展示结构（包含 id、key、agent、kind、age、model、tokens、channel、flags、active、source 等字段），然后通过 mergeLocalSessions() 和 dedupeAndSortSessions() 函数与 Claude、Codex、Hermes 会话合并，按 lastActivity 降序排列，最终在统一的会话列表中展示。

### 八、能力边界与风险声明

为遵循"不将未知能力展示为已完整支持"的原则，OpenCode 会话在 MC 统一会话列表中的 kind 字段标记为 'opencode'，与已有的 'claude-code'、'codex-cli'、'hermes' 等标记明确区分。对于 OpenCode 特有但 MC 尚未适配的能力（如特定的会话控制操作、独有的模型参数配置），系统通过以下机制声明能力边界：（1）会话详情面板根据 kind 字段动态展示可用的操作项，仅对已适配的能力显示操作按钮；（2）POST /api/sessions 的会话控制接口（set-thinking、set-verbose、set-reasoning、set-label）对 OpenCode 会话返回明确的能力不支持提示，而非静默忽略或伪造成功。

当前方案存在的待确认风险：（1）OpenCode 的实际本地状态目录结构和会话文件格式需在接入时验证，若与设计假设（JSON 或 JSONL 格式存储会话元数据）存在差异，需调整 opencode-sessions.ts 中的解析逻辑；（2）OpenCode 的继续命令参数格式（如是否会话标识使用 --resume 或 --session 等选项）需根据实际 CLI 接口确认后填入命令模板；（3）若 OpenCode 采用不同于已有运行时的进程模型（如 daemon 后台进程），则运行状态检测逻辑需相应调整。

### 九、技术效果

（1）跨运行时统一管理：OpenCode 会话与 Claude Code、Codex CLI、Hermes Agent、OpenClaw 会话在 MC 中并列展示和管理，用户无需切换工具即可掌握所有 agent 工作记录。会话数据按统一的 lastActivity 时间线排序，消除不同运行时之间的信息孤岛。（2）零侵入接入：OpenCode 接入不要求修改 OpenCode 自身的代码或数据格式，仅通过读取本地状态目录和调用标准 CLI 接口实现集成。适配器合约和事件总线机制确保新增 OpenCode 不影响已有运行时的稳定性和行为。

（3）可配置与可扩展：OpenCode 状态目录、配置文件路径和继续命令模板均通过环境变量可配置，支持不同操作系统和安装场景。适配器合约和命令模板机制可复用于未来接入其他新运行时，降低每次接入的边际开发成本。（4）能力边界透明：通过 kind 字段区分和会话控制接口的能力检查，用户不会误以为 OpenCode 已支持所有 MC 会话操作，避免误导性交互。

### 十、本地代理同步扩展

MC 已有的 local-agent-sync.ts 模块通过扫描 ~/.agents/、~/.codex/agents/、~/.claude/agents/、~/.hermes/skills/ 等目录发现本地代理定义。对于 OpenCode，可扩展 getLocalAgentRoots() 函数增加对 OpenCode 代理目录（如 ~/.opencode/agents/）的扫描。OpenCode 代理定义若采用类似 soul.md、identity.md、AGENT.md 等标记文件，可直接复用已有的 extractRole() 和 parseYamlFrontmatter() 解析逻辑。若 OpenCode 采用不同的代理定义格式，则通过可配置的解析适配器注入处理。
