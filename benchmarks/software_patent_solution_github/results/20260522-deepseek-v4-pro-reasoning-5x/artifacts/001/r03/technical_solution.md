## 技术方案

本技术方案描述一种将本地 agent 运行时（以 OpenCode/OpenClaw 为代表）作为一等公民接入统一 agent 管理平台（Mission Control）的软件架构与方法。方案通过可配置化的本地状态发现、适配器合约、事件总线多播、配置同步引擎与会话数据读取机制，使 OpenCode 会话与 agent 能够像已有的 Claude Code、Codex CLI、Hermes 等运行时一样被识别、展示和管理，同时在不完全掌握 OpenCode 内部实现细节的情况下，通过适配器合约、离线 fixture 验证和显式能力边界声明保证系统的一致性与可演进性。

### 技术问题定位

Mission Control 作为一个多 agent 运行时管理平台，需要统一管理多种本地 agent runtime（Claude Code、Codex CLI、Hermes、OpenCode 等）。每种 runtime 有不同的本地状态存储格式、配置方式、会话记录格式和继续命令。现有方案中，OpenCode 的本地工作产物（agent 配置、会话记录、token 统计等）无法被 Mission Control 识别，用户只能将其作为外部工具或手工记录。核心挑战在于：(1) OpenCode 的本地状态目录结构、消息文件格式和继续命令可能随版本变化，系统不应假设固定格式；(2) 不同 runtime 的能力集不同，系统需在不造成“全能力已支持”误解的前提下暴露差异化的可操作能力；(3) 新 runtime 的接入应尽量复用已有的检测、同步、展示和会话管理基础设施，避免为每个 runtime 重复构建。

### 整体架构

系统采用分层架构：(1) 配置层：通过多级环境变量 fallback 机制解析 OpenCode 本地状态目录、配置文件路径、二进制路径和网关地址，实现可配置的发现。(2) 检测层：对每种运行时提供统一的 RuntimeStatus 接口（installed/version/running/authenticated），通过文件存在性检查、spawn --version 和端口探测组合判定运行时状态。(3) 适配器层：定义 FrameworkAdapter 接口合约（register、heartbeat、reportTask、getAssignments、disconnect），每种运行时实现该合约，通过事件总线与平台其他模块解耦。(4) 同步层：包含两种同步路径——openclaw.json 配置同步（agent-sync.ts）和本地目录扫描同步（local-agent-sync.ts），分别处理网关配置和本地 agent 定义文件。(5) 会话层：从 OpenCode 状态目录按约定路径读取 sessions.json，提供 TTL 缓存和活跃状态推导。(6) 网关交互层：通过 CLI 子命令封装对 OpenCode Gateway 的 RPC 调用。

### 多运行时统一管理框架

系统定义 RuntimeId 联合类型，包含 'openclaw' | 'hermes' | 'claude' | 'codex' 四种运行时。每种运行时对应一个 RuntimeMeta 描述（名称、描述、是否需要认证、认证提示），以及一个 detect 函数。所有运行时共享统一的 RuntimeStatus 数据结构：id（运行时标识）、installed（是否已安装）、version（版本号或 null）、running（是否运行中）、authRequired/authenticated（认证状态）。detectAllRuntimes() 函数遍历所有检测器，返回完整的运行时状态列表，供 API 和 UI 统一消费。安装管理也采用统一模式：startInstall(runtime, mode) 创建后台 InstallJob，通过内存 Map 管理作业生命周期，支持 local 和 docker 两种部署模式，其中 local 模式包含安全审计链（下载脚本→正则注入扫描→可选 AI 审查→执行）。

### OpenCode 运行时的检测与识别

detectOpenClaw 采用三步检测策略：(1) 文件存在性检查——通过 config.openclawConfigPath（多级环境变量 fallback 解析得到）判断 openclaw.json 配置文件是否存在；(2) 版本探测——通过 spawnSync 调用 openclaw --version 获取版本字符串；(3) 服务活性检查——通过 net.Socket 连接 gatewayHost:gatewayPort 判断 gateway 端口是否在监听。配置解析采用 6 级 fallback 链：OPENCLAW_STATE_DIR → CLAWDBOT_STATE_DIR → OPENCLAW_HOME/CLAWDBOT_HOME → OPENCLAW_CONFIG_PATH 所在目录 → 默认 ~/.openclaw。这种多级 fallback 保证了对不同安装方式、不同环境变量约定和不同命名历史的兼容。

### 适配器合约与事件总线

FrameworkAdapter 接口定义了 5 个方法，构成 agent runtime 与平台的标准化交互契约：(1) register(AgentRegistration)——将 agent 注册到平台，广播 agent.created 事件；(2) heartbeat(HeartbeatPayload)——上报心跳与状态指标，广播 agent.status_changed；(3) reportTask(TaskReport)——上报任务进度与结果，广播 task.updated；(4) getAssignments(agentId)——从平台数据库查询分配给该 agent 的待处理任务，按优先级和截止时间排序，限 5 条；(5) disconnect(agentId)——标记离线。OpenClawAdapter 实现该接口时，所有操作均通过 eventBus 广播而非直接数据库写入，实现了适配器与平台存储的解耦。适配器注册表（adapters/index.ts）采用工厂模式，支持 6 种 adapter（openclaw、generic、crewai、langgraph、autogen、claude-sdk），新增运行时只需实现接口并注册工厂函数。

### Agent 配置同步机制

syncAgentsFromConfig 从 openclaw.json 的 agents.list 数组中读取 agent 定义，经过 enrichAgentConfigFromWorkspace 补充 workspace 中的 identity.md（角色身份）和 TOOLS.md（工具清单）内容后，映射为 MC 数据库记录。映射规则：name 取 identity.name || name || id，role 取 identity.theme || 'agent'，config 为完整 JSON 配置，soul_content 为 workspace 中的 soul.md。同步使用数据库事务 upsert：按 name 匹配已有记录，比较 config JSON 和 soul_content 是否变更，仅写入有变化的记录，并记录审计日志和广播事件。同时提供 previewSyncDiff 进行只读差异预览，以及 writeAgentToConfig/removeAgentFromConfig 支持 UI 侧反向写入 openclaw.json。local-agent-sync.ts 提供补充路径：扫描 ~/.agents/、~/.codex/agents/、~/.claude/agents/、~/.hermes/skills/ 四个本地目录，通过 SHA-256 哈希比较检测新增、变更和消失的 agent 定义。

### 会话数据读取与活跃状态推导

OpenCode 的会话数据按约定路径存储在 {OPENCLAW_STATE_DIR}/agents/{agentName}/sessions/sessions.json 中。每个文件是一个以 session key（如 "agent:research-bot:main"）为键的 JSON 对象，包含 sessionId、updatedAt（最后活动时间戳）、chatType、channel、model（含 primary 字段）、totalTokens、inputTokens、outputTokens、contextTokens 等字段。getAllGatewaySessions 遍历所有 agent 目录下的 sessions.json 文件，解析并合并为统一的 GatewaySession 列表，按 updatedAt 降序排列。为减少磁盘 I/O，系统维护一个 30 秒 TTL 的内存缓存，force 参数可强制刷新。getAgentLiveStatuses 基于会话时间戳推导 agent 活跃状态：最后活动在 5 分钟内为 active，1 小时内为 idle，其余为 offline。同时提供 pruneGatewaySessionsOlderThan 支持按保留天数裁剪过期会话记录，裁剪时使用原子写入（先写临时文件再 rename）保证数据一致性。

### 能力边界与降级策略

系统在设计上承认不同 runtime 的能力差异，并通过以下机制避免给用户造成“所有操作均已完整支持”的误解：(1) 会话继续 API（sessions/continue）当前仅支持 claude-code 和 codex-cli 两种 kind，OpenCode 的继续操作需要通过 gateway CLI 或直接调用 openclaw 命令实现，API 明确拒绝未知 kind；(2) 本地 agent 目录扫描（local-agent-sync）当前的 4 条扫描路径不包含 OpenCode 的 agents 目录，OpenCode agent 通过 openclaw.json 同步路径独立管理；(3) detectOpenClaw 的 running 状态判断当前采用“已安装即视为运行中”的降级策略，端口探测异步执行但不阻塞返回；(4) 配置路径通过多级环境变量 fallback 实现可配置发现，不硬编码固定路径，支持 OPENCLAW_/CLAWDBOT_ 双命名体系兼容历史版本；(5) 离线测试模式下，系统通过 fixture 文件（如 tests/fixtures/openclaw/ 下的 openclaw.json 和 sessions.json）模拟 OpenCode 状态目录结构，使会话读取、配置同步等核心链路可在无实际 OpenCode 安装的环境中验证。

### 技术效果

本方案相比将 OpenCode 作为外部工具或手工记录的方式，产生以下技术效果：(1) 统一管理体验：OpenCode 的 agent 和会话与其他运行时在同一个 UI 中展示，用户无需切换工具；(2) 可配置发现：通过多级环境变量 fallback 而非硬编码路径，适应不同安装方式和命名历史，降低部署耦合；(3) 适配器合约解耦：新增运行时只需实现 FrameworkAdapter 接口并注册工厂函数，核心管理逻辑无需修改，保证对 Claude、Codex、Hermes 等已有运行时的兼容；(4) 离线可验证：通过 fixture 文件模拟 OpenCode 状态目录，核心读取、同步和展示链路可在无真实 OpenCode 安装的环境中测试；(5) 显式能力边界：通过 kind 枚举限制、独立同步路径和降级策略，避免将未实现功能展示为已支持，防止用户误操作；(6) 会话状态推导：基于会话时间戳自动推导 agent 活跃/空闲/离线状态，无需 agent 主动上报心跳。

### Gateway CLI 调用与诊断

系统通过 CLI 子命令封装对 OpenCode Gateway 的远程过程调用。callOpenClawGateway 函数将 method 名称和 params 参数序列化为 openclaw gateway call 命令：['gateway', 'call', method, '--timeout', timeout, '--params', JSON.stringify(params), '--json']。关键设计：(1) runOpenClaw 显式设置 OPENCLAW_STATE_DIR 环境变量，防止 CLI 将 OPENCLAW_HOME 解释为父目录导致双重嵌套路径；(2) parseGatewayJsonOutput 从 stdout 中定位第一个 '{' 或 '[' 并匹配闭合括号提取 JSON 对象，容忍 CLI 在 JSON 前后输出日志或装饰性文本；(3) 解析失败时抛出明确错误而非静默降级，确保调用方感知异常。此外，openclaw-doctor.ts 提供对 openclaw doctor 诊断命令输出的结构化解析，向 UI 暴露配置健康度、状态一致性和安全警告。

### 风险与待确认问题

(1) 命名一致性：当前代码中统一使用 OpenClaw 命名，若 OpenCode 是其前身或别名，需确认术语统一；(2) 会话继续机制：sessions/continue API 当前仅支持 claude-code 和 codex-cli，若需支持 OpenCode 的会话继续，需新增 kind 分支并通过 openclaw gateway 或 CLI 实现继续操作，其具体命令格式需待 OpenCode 产品侧确认；(3) 端口探测精度：detectOpenClaw 的 running 字段当前返回 installed 而非真实端口状态，若需精确反映 gateway 运行状态，需改为异步检测并缓存结果；(4) 本地同步覆盖范围：local-agent-sync 的扫描路径不含 OpenCode agents 目录，若需统一管理入口，可扩展扫描路径或合并两者同步逻辑；(5) Gateway CLI 输出格式：parseGatewayJsonOutput 依赖括号匹配策略，其兼容范围受限于 CLI 输出中 JSON 是否以独立子串出现。
