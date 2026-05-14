# 隐藏参考方案

## 技术问题

Mission Control 已经具备多个 agent runtime 的发现、会话展示和继续操作能力，但新增 OpenCode 这类本地 agent runtime 时，系统不能简单复用已有 Claude、Codex 或 gateway 会话路径。不同 runtime 的本地状态来源、会话记录格式、可继续操作能力、终端附着能力和 UI 标识均可能不同。若直接把 OpenCode 伪装成现有 runtime，会导致会话发现不完整、历史记录解析不可信、继续操作错误承诺、终端入口误开启或 API 契约与真实能力不一致。

因此，需要一种面向多 agent runtime 的原生接入机制，使系统能够在不破坏现有运行时和会话体系的情况下，识别新的本地运行环境、读取真实会话、归一化展示，并对不同能力边界进行显式控制。

## 核心技术构思

在 Mission Control 中建立“运行时类型注册 + 本地状态适配 + 会话归一化 + 能力边界声明”的 agent runtime 接入机制。

系统为 OpenCode 增加独立 runtime 类型，并将其纳入运行时发现、会话查询、会话详情、会话继续、设置页、引导流程和工作区展示等既有路径。系统不把 OpenCode 伪装成其他 runtime，而是通过 OpenCode 专属适配层读取本地状态，解析真实会话记录，并输出与现有 session API 兼容的统一会话对象。同时，系统为每类会话标注 runtime kind、展示品牌、可用操作和不可用操作，使前端、API 文档和继续会话逻辑能够根据真实能力展示入口或返回错误。

## 必要技术特征

1. 运行时类型注册：在 agent runtime 枚举、检测接口、设置页和引导流程中增加 OpenCode 类型，使其作为一等 runtime 被识别和展示。
2. 本地状态发现：为 OpenCode 建立独立发现逻辑，支持显式配置路径优先，并在未配置时按本地安装习惯查找候选状态位置。
3. 状态结构适配：读取 OpenCode 本地会话状态时采用兼容式解析，避免把单一版本的本地结构硬编码为唯一格式。
4. 会话对象归一化：把 OpenCode 会话转换为 Mission Control 既有 session 列表、详情和 transcript 接口可以消费的统一结构，同时保留 runtime kind。
5. transcript 支持：会话详情接口能够读取 OpenCode 的历史消息，并把不同消息载荷归一化为上层可展示的 transcript。
6. continue 能力控制：继续会话接口可以调用本地 OpenCode 命令继续已有会话，但当本地 provider、模型或环境无法满足时，应返回真实错误而不是伪造成功。
7. UI 能力分层：会话列表、会话品牌、设置页、引导流程和工作区入口展示 OpenCode 身份，但对尚未支持的终端附着能力进行禁用或隐藏。
8. 契约同步：OpenAPI、API index 和测试夹具同步描述 OpenCode 的真实能力，避免文档、测试和接口行为不一致。
9. 可复现验证：通过真实结构的本地状态夹具和可控命令替身，验证运行时发现、会话列表、transcript 和 continue 路径，而不依赖开发者机器上的实际 OpenCode 安装。

## 关键流程

### 运行时发现流程

1. 系统加载现有 runtime detection 配置。
2. 对 OpenCode 执行独立检测，判断是否存在可用的本地状态、可执行命令或显式配置。
3. 检测结果写入统一 runtime 列表，并标注 runtime 类型、显示名称、可用状态和配置提示。
4. 设置页和引导流程根据统一 runtime 列表展示 OpenCode 卡片。

### 会话查询流程

1. 会话列表 API 汇聚既有 runtime 会话来源。
2. 调用 OpenCode 会话适配器读取本地状态。
3. 适配器解析会话元数据、更新时间、标题、路径或上下文信息。
4. 系统把 OpenCode 会话归一化为统一 session 对象，并附加 `runtime_kind = opencode`。
5. 前端根据 runtime kind 展示 OpenCode 品牌和可用操作。

### transcript 与 continue 流程

1. transcript API 根据 session kind 路由到 OpenCode transcript 解析逻辑。
2. 解析器读取 OpenCode 历史消息并归一化为统一消息序列。
3. continue API 根据 session kind 调用 OpenCode CLI 或等价本地继续命令。
4. 若本地 OpenCode 环境缺少必要 provider、模型或认证条件，接口返回真实失败原因。
5. 对暂未支持的 PTY 或终端附着路径，前端保持禁用，避免误导用户。

## 技术效果

- Mission Control 能在无需 gateway bridge 的情况下识别和展示真实 OpenCode 本地会话。
- 新 runtime 接入不会破坏 Claude、Codex、Hermes 等既有 runtime 的会话合并和展示逻辑。
- 上层 UI 和 API 能基于 runtime kind 区分不同会话能力，减少错误入口和不实能力承诺。
- transcript 和 continue 行为与本地 OpenCode 环境保持一致，失败时可解释、可诊断。
- 通过统一会话对象和专属适配层，后续接入其他本地 agent runtime 时可以复用相同扩展模式。

## 目标能力边界

必须解决的是“把 OpenCode 作为原生 runtime 纳入 Mission Control 的会话发现、展示、转录和继续链路”，而不是只在设置页增加一个 OpenCode 卡片。方案可以不要求立即支持完整 PTY attach、所有 OpenCode provider 或远程同步，但必须清楚声明这些能力边界，并在 UI/API 层避免把未支持能力展示为可用。

高分方案应保持现有 Claude、Codex、Hermes 等 runtime 的行为不变。OpenCode 接入应是新增 runtime kind 和适配层，不应通过改写现有 runtime 语义、复用错误品牌或把 OpenCode 状态硬塞进已有 Claude/Codex parser 来实现。

## 核心数据结构与接口契约

- `RuntimeKind` 或等价枚举应新增 `opencode`，并进入 runtime detection、settings、onboarding、session list 和 API schema。
- OpenCode 本地会话适配器应输出统一 session 对象，至少包含 `id`、`runtimeKind`、`title`、`updatedAt`、`workspacePath` 或上下文标识、`canContinue`、`canAttachTerminal`、`sourcePath` 等字段。
- transcript 解析结果应转为统一消息结构，保留 role、时间、文本内容、工具片段或不可解析载荷占位。
- continue API 应根据 `runtimeKind=opencode` 路由到 OpenCode 专属命令构造逻辑，并返回可诊断错误字段，例如 binary missing、provider missing、session missing、unsupported attach。
- OpenAPI 和测试夹具应反映 OpenCode runtime 的真实响应字段，避免接口契约与实现脱节。

## 项目集成点

该方案应落到 Mission Control 既有 runtime registry、session API、transcript API、continue API、settings runtime section、onboarding modal、conversation list、terminal view 和 OpenAPI 生成路径。高分答案会说明 OpenCode 专属解析模块由这些路径调用，而不是要求新增一个完全独立的 OpenCode 页面或旁路服务。

## 必须命中的评分锚点

- 明确区分 runtime 注册、会话发现、transcript 解析、continue 执行和 UI 能力 gating。
- 使用 OpenCode 专属本地状态适配器读取真实会话，而不是手工导入或用户填写历史记录。
- 统一输出 Mission Control 现有 session/transcript API 可消费的数据结构。
- 对未支持的终端附着或 provider 能力做显式禁用或错误返回。
- 说明真实本地状态 fixture、命令替身和 API/E2E 测试如何验证该 runtime。

## 常见错误方案

- 只说“增加 OpenCode 支持”或“调用 OpenCode CLI”，没有说明会话发现、transcript 和 continue 如何进入现有 API。
- 把 OpenCode 伪装成 Claude/Codex runtime，导致品牌、能力和状态解析混乱。
- 默认所有 OpenCode 会话都可继续或可附着终端，没有能力检测和 UI gating。
- 要求用户手工复制 OpenCode 历史到 Mission Control，未实现原生本地状态读取。
- 新增独立 OpenCode 管理页面，但不接入 Mission Control 的 session list、settings 和 API 契约。

## 对应真实实现

真实 PR #586 采用了如下实现方向：

- 新增 `src/lib/opencode-sessions.ts` 作为 OpenCode 本地会话读取适配层。
- 将 OpenCode 接入 `src/lib/agent-runtimes.ts`、session list、transcript 和 continue API。
- 在 onboarding、settings、conversation list、session branding 和 terminal view 中区分 OpenCode。
- 对 OpenCode 保持 transcript / continue 支持，但不宣称现有 PTY 后端已经支持。
- 增加真实结构 SQLite fixture、OpenCode binary 替身和 API / E2E 测试。
- 更新 OpenAPI 与 API index，保持契约和实现一致。
