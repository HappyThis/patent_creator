## 技术方案

### 技术问题与整体思路

Mission Control 已支持 Claude、Codex、Hermes 等多种 agent runtime，但缺少对 OpenCode 这类本地运行、自管理状态的 agent 工具的原生接入能力。用户已在本地使用 OpenCode 完成实际 agent 工作，产生了有价值的会话历史。若不接入，这些会话将成为信息孤岛：用户无法在统一面板中回顾、检索或继续使用这些会话；若仅以外部工具或手工记录的方式引用 OpenCode，则丢失了会话级粒度的管理能力，也无法享受统一的 agent 运行时生命周期管理。

### Runtime 适配器合约设计

方案的核心是在 Mission Control 的运行时管理层引入一个统一的 Runtime 适配器合约（Adapter Contract），所有 agent runtime——包括已有的 Claude、Codex、Hermes 以及新增的 OpenCode——均通过实现该合约接入系统。合约定义了以下最小接口集合：

- discover_sessions(config) -> [SessionRef]：基于配置参数发现本地或远程 runtime 的会话列表，返回会话引用（含唯一标识、创建时间、状态摘要等元信息）；
- get_session_detail(session_id) -> SessionDetail：获取单个会话的完整详情，包括消息序列、工具调用记录、运行时元数据；
- convert_message(msg) -> CanonicalMessage：将 runtime 原生消息格式转换为 Mission Control 的规范化消息模型（CanonicalMessage），该模型至少包含角色、时间戳、内容体、工具调用引用等字段；
- continue_session(session_id, input) -> SessionResult：在指定会话上下文中继续执行，提交用户输入并获取 agent 响应；
- get_capabilities() -> CapabilitySet：返回该 runtime 支持的能力集，包括是否支持流式输出、工具调用、多模态、会话恢复等。

### OpenCode 本地状态发现机制

OpenCode 的会话数据存储在用户本地文件系统中，其状态目录、文件命名规则和消息编码格式因版本和环境而异，不应在适配器中硬编码。为此，方案采用"可配置发现 + 适配器合约 + 兼容式解析"三层机制。

可配置发现机制：系统通过以下优先级链确定 OpenCode 状态目录：(1) Mission Control 配置文件中显式声明的 opencode_state_dir 路径；(2) 环境变量 OPENCODE_HOME 或 OPENCODE_STATE_DIR；(3) 操作系统约定的默认数据目录下的 OpenCode 子目录（如 Linux 下的 $XDG_DATA_HOME/opencode、macOS 下的 ~/Library/Application Support/opencode）。适配器启动时扫描该目录，通过文件名模式（如 session_*.jsonl、*.opencode.json）和内容试探（检查是否包含 role/timestamp/content 等关键字段）识别有效会话文件。未识别的文件被静默跳过，不阻塞发现流程。

### 会话格式兼容解析

为应对 OpenCode 消息格式的不确定性，适配器不依赖单一解析路径，而是实现一个可扩展的解析器链（Parser Chain）。链中每个解析器尝试按一种已知格式（如 JSONL 每行一个消息对象、NDJSON 流、带换行分隔的纯文本日志）解析会话文件，返回解析结果或声明"不匹配"。适配器按注册顺序尝试解析器，首个成功者产出规范化消息列表。

解析器在转换过程中遵循以下容错原则：(1) 已知字段（role、content、timestamp）按合约映射到 CanonicalMessage；(2) 未知字段保留在 CanonicalMessage 的扩展元数据区（metadata map），不丢弃信息；(3) 无法解析的消息行标记为 UnrecognizedMessage 类型，在 UI 中以"无法识别的消息"呈现，但保留原始文本供用户查看。这种策略确保即使 OpenCode 升级改变了内部格式，历史会话仍可被部分读取，而不会因单个字段不匹配导致整会话不可用。

### 会话生命周期与继续机制

适配器合约中的 continue_session 接口是区分"原生接入"与"外部工具引用"的关键。对于 Claude、Codex 等已完整对接的 runtime，该接口直接调用其原生 API 继续会话。对于 OpenCode，继续机制分三个信任层级实现：

- 命令替身模式（Command Stand-in）：Mission Control 通过适配器配置中声明的 opencode_cli_path 找到 OpenCode 可执行文件，以子进程方式执行 opencode continue --session <id> --input <user_message>，捕获标准输出和退出码，解析后转换为 CanonicalMessage 返回。该模式依赖于本地 OpenCode CLI 的实际存在和 continue 子命令的可用性。
- Fixture 验证模式（Fixture Verification）：系统预置一组已知输入/输出对（fixture），适配器在首次使用时通过执行 opencode continue --session <test_id> --input <fixture_input> 并比对实际输出与预期输出来验证 continue 命令的可用性和行为特征。验证结果缓存，后续调用不再重复验证。若验证失败，continue_session 接口返回能力不可用状态，UI 据此隐藏"继续"按钮。
- 直接状态写入模式（State Injection，待确认）：若 OpenCode 的会话状态以纯文件形式存储且格式可写，适配器可通过直接写入新消息行来"继续"会话，再由 OpenCode 自身在下次打开时识别。该模式为可选扩展，当前标记为待确认能力，不在初始版本中承诺支持。

### 能力边界声明与 UI 适配

不同 runtime 的能力集合天然存在差异。方案要求每个适配器通过 get_capabilities() 返回一个显式的能力集（CapabilitySet），其中每个能力项包含：(1) 能力标识（如 streaming_output、tool_calling、multimodal_input、session_resume、message_editing）；(2) 支持状态（supported / unsupported / partial / unknown）；(3) 附加说明（如"partial：仅支持纯文本工具调用结果展示"）。

Mission Control 的会话管理面板在渲染时读取当前选中会话所属 runtime 的能力集，动态调整 UI：(1) 不支持的能力对应的操作按钮置灰或隐藏，并悬浮提示"当前 runtime 不支持此操作"；(2) 状态为 unknown 的能力以问号图标标记，点击后展开说明"该能力在适配器验证阶段未能确认，实际可用性取决于本地 OpenCode 版本"；(3) 会话详情页顶部常驻一条能力摘要条，以图标矩阵方式展示当前 runtime 的核心能力状态。API 层同样返回能力集信息，使外部调用方也能据此做自适应处理，避免假定所有 runtime 具有相同能力。

### 处理流程

当 Mission Control 启动或用户触发"扫描运行时"操作时，系统执行以下流程：

1. 运行时注册加载：Mission Control 从配置中读取已注册的 runtime 适配器列表（含 OpenCode 适配器），逐一遍历并调用各适配器的初始化方法。初始化失败的适配器（如 OpenCode CLI 不可达）标记为 degraded 状态，不影响其他适配器。
2. 会话发现：对每个初始化成功的适配器，调用 discover_sessions(config) 获取会话引用列表。OpenCode 适配器在此步骤执行状态目录扫描、文件探测和内容试探。发现结果合并到统一的会话列表中，按时间排序，并标注所属 runtime 类型。
3. 会话详情获取：用户点击某个 OpenCode 会话时，Mission Control 调用适配器的 get_session_detail(session_id)，适配器读取本地会话文件并通过解析器链转换为规范化消息序列，返回给 UI 渲染。
4. 继续会话（条件执行）：若用户发起"继续"操作，Mission Control 先检查该运行时能力集中 session_resume 是否为 supported。若为 supported，调用 continue_session(session_id, user_input)，由适配器通过命令替身或直接写入方式执行继续操作并返回结果。若为 unsupported 或 unknown，UI 阻止操作并显示原因。
5. 能力集刷新：适配器可在运行时重新执行 fixture 验证（如用户升级 OpenCode 后手动触发），刷新能力集并通知 UI 更新。

### 技术效果

该方案带来以下技术效果：

- 统一管理平面：将 OpenCode 与 Claude、Codex、Hermes 等 runtime 纳入同一套会话发现、列表、详情、继续的生命周期管理体系，消除信息孤岛。
- 格式演进兼容：通过可配置发现、适配器合约和解析器链的设计，OpenCode 内部格式的变化不会导致整个接入方案失效，只需增减或调整解析器即可适配新版本。
- 诚实的能力边界：通过显式能力集声明和 fixture 验证，系统不会将未确认的能力展示为已支持，避免误导用户；unknown 状态的透明展示也为用户提供了自主判断的空间。
- 渐进式接入：命令替身模式作为最小可行接入路径，不要求修改 OpenCode 源码或预知其内部格式；后续可按需升级到直接状态写入模式，无需改变适配器合约。
- 与现有 runtime 兼容：适配器合约是对已有 Claude/Codex/Hermes 接入模式的抽象和统一，不改变现有接入逻辑，只是在合约层增加 OpenCode 实现。

### 风险与待确认问题

以下为当前方案中已识别但尚未确认的风险点，需要在实施前逐一验证：

- OpenCode 会话文件的实际格式：当前方案假设 OpenCode 以某种结构化文本格式（如 JSONL）存储会话。若实际采用二进制格式或嵌入式数据库（如 SQLite），解析器链需要增加对应的二进制解析器或数据库读取器。风险等级：中。
- OpenCode continue 子命令的存在性与行为：命令替身模式依赖 opencode continue 命令。若 OpenCode 未提供该子命令，或参数接口与预期不同，需要调整为其他交互方式（如通过 stdin 模拟交互式会话）。风险等级：高。
- 会话标识的稳定性：适配器需要 OpenCode 会话有稳定的唯一标识，以便在多次发现之间正确关联同一会话。若 OpenCode 使用随机临时标识或在会话重命名后改变标识，需要引入基于内容哈希的模糊匹配机制。风险等级：中。
- 并发访问的冲突：若用户同时在 Mission Control 和 OpenCode 原生界面中操作同一会话，可能出现状态不一致。方案建议在 continue_session 执行期间对会话文件加文件锁，但需要验证 OpenCode 自身是否也有类似的锁机制。风险等级：中。
- 跨平台路径差异：不同操作系统下 OpenCode 状态目录的默认路径不同，当前方案通过优先级链覆盖主流平台，但若用户使用非标准安装路径或容器化环境，需手动配置。风险等级：低。
