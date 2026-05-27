## 技术方案

本方案提出一种在 Mission Control 中将 OpenCode 作为原生 agent runtime 接入的系统架构与方法。方案的核心在于引入可插拔的 Runtime 适配器层，通过统一的适配器合约（Adapter Contract）将不同 agent runtime——包括已有的 Claude、Codex、Hermes 以及新增的 OpenCode——映射为 Mission Control 内部的统一会话管理模型。对于 OpenCode 这类本地 agent runtime，方案进一步提供可配置的会话发现机制、兼容式格式解析、fixture 驱动的命令替身验证以及 UI/API 能力边界声明机制，使得系统能够在如实反映 OpenCode 真实能力的前提下，将其会话纳入 Mission Control 的统一管理中。

### 整体架构

系统在 Mission Control 的会话管理层与各 agent runtime 之间引入一个 Runtime 适配器层（Runtime Adapter Layer）。该层包含一个 Runtime 注册中心（Runtime Registry）和一组遵循统一适配器合约的 Runtime 适配器实例。每个适配器负责一种 agent runtime 的会话发现、格式解析、状态同步与命令转发。新接入 OpenCode 时，只需实现一个新的 OpenCodeAdapter，注册到 Runtime Registry 即可，无需修改 Mission Control 核心逻辑，也不影响已有 ClaudeAdapter、CodexAdapter、HermesAdapter 的运行。

### Runtime 适配器合约

适配器合约定义了所有 Runtime 适配器必须实现的一组接口，Mission Control 通过该合约与各 runtime 交互而不依赖具体实现细节。合约包含以下核心接口：

- discover()：返回该 runtime 下可发现的会话列表。适配器按照自身配置的发现策略（如扫描本地目录、查询远程 API）获取会话元数据。
- parse(session_ref)：将 runtime 原生格式的会话数据解析为 Mission Control 内部统一会话表示（UnifiedSession），包括消息列表、时间戳、角色标注、工具调用记录等。
- get_capabilities()：返回该 runtime 的能力声明（CapabilityDescriptor），列出支持的操作（如继续会话、中断、重试、流式输出等）及其当前可用状态。
- execute(command, session_ref)：向该 runtime 下发操作命令，如继续执行、取消任务等。返回操作结果或能力不支持的错误码。
- validate()：执行自检，验证适配器配置是否有效（如本地路径是否存在、格式 fixture 是否能通过解析测试）。

### OpenCode 会话发现与解析

OpenCode 的会话数据存储在本地文件系统中。由于 OpenCode 的状态目录结构、消息文件格式和会话命名规则可能随版本变化，系统采用三层可配置的发现与解析机制。

- 可配置发现路径：系统管理员或用户在 Mission Control 中配置 OpenCode 的会话根目录（session_root），支持 glob 模式（如 ~/.opencode/sessions/*.json）和环境变量占位符。OpenCodeAdapter 在 discover() 调用时，按配置的路径模式扫描匹配的会话文件，提取会话 ID、时间戳、摘要等元数据。
- 兼容式格式解析：parse() 接口中，OpenCodeAdapter 不假定固定的消息文件 schema，而是采用宽容解析策略（lenient parsing）。对于已知字段（如 role、content、timestamp）按约定映射到 UnifiedSession；对于未知字段保留在扩展属性中，不中断解析流程。解析失败时返回 PartialSession 而非抛出异常，标记缺失字段供上层决策。
- Fixture 驱动验证：系统提供一组 fixture 文件（示例 OpenCode 会话记录的快照），作为适配器自检的基准。validate() 方法加载 fixture 执行解析，比较输出与预期 UnifiedSession 的差异，差异超过阈值则标记适配器状态为 degraded。fixture 可随 OpenCode 版本更新而追加，无需修改适配器代码。

### 能力边界声明与命令替身

为避免用户误以为 OpenCode 已完整支持所有 Mission Control 操作，系统在每个 runtime 的能力描述符（CapabilityDescriptor）中显式声明三层能力状态。第一层为已完整支持（supported），对应通过适配器合约验证且可正常执行的操作。第二层为部分支持（partial），对应解析通过但命令执行依赖命令替身（command stub）的操作，系统在 UI 中展示为降级模式。第三层为不支持（unsupported），对应 OpenCode 原生不具备且未提供替身的操作，UI 中直接隐藏或灰显。

对于部分支持的操作，系统引入命令替身机制：OpenCodeAdapter 在 execute() 中为 OpenCode 不原生支持的命令（如流式输出、中断重试）提供本地 stub 实现。stub 返回标准化的能力不支持响应，同时记录操作日志供审计。stub 行为通过 fixture 验证其响应格式符合合约约定，确保即便功能缺失，系统行为也是可预测的而非未定义。

### 处理流程

OpenCode 接入 Mission Control 的完整处理流程分为注册、发现、同步和交互四个阶段。

1. 注册阶段：Mission Control 启动时加载 Runtime Registry，扫描已配置的适配器列表。OpenCodeAdapter 实例化后调用 validate() 执行自检——加载 fixture 验证解析能力、检查 session_root 路径可访问性、测试 stub 命令响应。自检通过则将适配器状态设为 active，失败则设为 degraded 并记录错误详情。
2. 发现阶段：用户打开 Mission Control 会话列表时，系统遍历所有 active 适配器调用 discover()。OpenCodeAdapter 按 session_root 配置扫描本地文件系统，将匹配的会话文件解析为会话元数据列表，合并到统一会话视图中。每个会话标记来源 runtime 类型（opencode）。
3. 同步阶段：用户选中某个 OpenCode 会话查看详情时，系统调用 parse() 获取 UnifiedSession。解析结果包含完整的消息历史、工具调用记录和扩展属性。UI 根据该 runtime 的 CapabilityDescriptor 渲染操作按钮——已完整支持的操作高亮可用，部分支持的操作标记降级提示，不支持的操作隐藏。
4. 交互阶段：用户在 Mission Control 中对 OpenCode 会话执行操作（如继续对话）时，系统调用 execute() 将命令转发到 OpenCodeAdapter。适配器判断命令是否在能力范围内：若支持则调用 OpenCode 原生命令执行；若仅部分支持则通过 stub 返回标准化降级响应，UI 向用户展示当前不可用的说明。

### 统一会话模型

为实现不同 runtime 的会话在 Mission Control 中的统一呈现，系统定义统一的内部会话模型 UnifiedSession，作为所有适配器 parse() 接口的标准输出格式。UnifiedSession 包含以下核心字段：

- session_id：全局唯一会话标识，由 runtime 类型前缀与原生会话 ID 拼接（如 opencode::abc123），避免不同 runtime 间 ID 冲突。
- runtime_type：标识来源 runtime（claude、codex、hermes、opencode）。
- title / summary：会话标题或摘要，由适配器从原生数据中提取。
- created_at / updated_at：时间戳，统一为 UTC。
- messages：消息列表，每条消息包含 role（user/assistant/system/tool）、content、timestamp 和可选的 tool_calls 子结构。
- metadata：扩展属性映射，存放 runtime 特有的非标准化字段（如 OpenCode 的自定义标签、模型参数等），供 UI 按需展示但不影响核心逻辑。
- capabilities：该会话关联的 CapabilityDescriptor 快照，记录解析时刻的能力状态。

### 技术效果

本方案通过可插拔适配器架构和统一的适配器合约，在不修改 Mission Control 核心逻辑、不影响已有 Claude/Codex/Hermes 适配器的前提下，实现了 OpenCode 的原生接入。具体技术效果包括：

- 运行时无关的会话统一管理：不同 agent runtime 的会话通过 UnifiedSession 模型统一呈现，用户无需关心底层 runtime 差异即可在单一界面中浏览和管理所有 agent 工作记录。
- 可扩展性：新增任意 agent runtime 只需实现适配器合约的五个接口并注册到 Runtime Registry，核心系统零改动，符合开闭原则。
- 如实反映能力边界：CapabilityDescriptor 的三层能力声明和命令 stub 机制确保 UI 不会将未完整支持的操作展示为可用，避免误导用户。
- 宽容解析与降级运行：兼容式解析和 PartialSession 机制使得即使 OpenCode 格式发生变化，系统仍能尽可能多地提取有效信息，而非整体失败。
- 可验证的正确性：fixture 驱动的 validate() 自检机制使得适配器的解析和 stub 行为可在部署前和升级后得到自动化验证，降低因 OpenCode 版本升级导致的集成风险。

### 风险与待确认问题

以下事项需要在方案落地前进一步确认或持续关注：

- OpenCode 状态目录与消息格式尚未在项目中落实：当前方案假设 OpenCode 的会话数据以某种结构化文件（如 JSON 或 JSONL）存储在本地目录中。实际接入前需确认 OpenCode 的真实存储路径、文件命名规则和消息 schema。兼容式解析和 fixture 验证机制已为此类不确定性预留了缓冲，但 fixture 本身需根据真实格式编写。
- 继续命令（continue command）的具体行为未确认：OpenCode 是否支持通过 CLI 或 API 在已有会话上执行继续操作、继续操作的参数格式和返回格式为何，当前无法确认。方案通过命令 stub 机制保证即使不支持也能给出标准化降级响应，但理想的继续体验依赖于 OpenCode 的真实能力。方案的 execute() 接口已预留会话引用参数，一旦确认即可实现。
- 多实例并发与状态一致性：若用户同时在 Mission Control 和 OpenCode 原生界面操作同一会话，可能出现状态不一致。方案目前未覆盖分布式锁或冲突检测机制，建议作为后续迭代补充。
- OpenCode 版本兼容性：OpenCode 的存储格式和命令接口可能随版本变化。fixture 版本管理机制可部分缓解此问题——维护与 OpenCode 版本对应的 fixture 集合，validate() 时按当前安装版本选择匹配的 fixture 基准。
