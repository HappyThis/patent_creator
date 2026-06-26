## 技术方案

### 总体构思与接入边界

本方案在现有智能工作台的本地 agent 会话汇聚能力之上，增加面向目标本地 agent runtime 的渐进式接入机制。系统不把目标 runtime 视为固定目录、固定文件格式和固定命令接口的单一对象，而是先定义适配注册项，再通过证据化探测生成能力位集合，最后由统一会话 API 和前端工作台按能力位消费结果。运行时检测、状态源识别、会话摘要扫描、transcriptRead 内容读取、resume 继续会话和 terminalAttach 终端附着被拆分为相互独立的能力；任一能力未确认时，后续层只能获得不可用或只读状态，不能把未确认能力展示为可执行操作。

本方案统一使用如下术语：runtimeKind 表示适配器类型，用于区分 Claude Code、Codex CLI、Hermes、OpenClaw 以及目标 runtime；source 表示某一适配器下的具体数据来源，可以是状态目录、状态库、日志文件集合或网关实例；id 表示源 runtime 产生的原始会话标识或由源记录稳定派生的会话标识；状态目录表示包含配置、日志、JSONL、JSON 或数据库文件的本地目录；状态库表示可只读打开的 SQLite 等本地数据库；格式证据表示从文件头、版本字段、表结构、列名、消息角色字段或时间字段取得的格式匹配结果；能力位状态 confirmed、unconfirmed、unsupported 和 error 只描述某项功能是否可用，不与 active 会话运行状态混用。

该接入机制的边界是：既有 OpenClaw 网关会话、Claude Code 本地 JSONL 会话、Codex CLI 本地 JSONL 会话和 Hermes 本地状态库会话仍按各自现有流程扫描、归一化和控制；目标 runtime 通过新增适配单元接入统一会话列表，而不是改写既有 scanner、transcript 读取器或继续会话接口的判定逻辑。目标 runtime 默认按只读源数据处理，不删除、不迁移、不写入源文件或源数据库；只有 deleteSource 或 modifySource 能力位通过独立证据确认后，才允许触发影响源 runtime 数据的操作。工作台本地的重命名、颜色标记和排序偏好存储在自身偏好表中，不回写到目标 runtime 的状态目录。

### 运行时能力探测与适配注册

系统为每一种本地 agent runtime 建立适配注册项。该注册项包括 runtimeKind、displayName、sourceResolver、candidatePaths、candidateCommands、versionProbe、formatFingerprints、capabilityProbes、summaryScanner、transcriptReader、resumeExecutor、terminalAttacher 和 operationPolicy。sourceResolver 根据用户显式配置、默认 home 目录和环境变量生成 source；candidatePaths 输出待检查的状态目录、状态库和日志文件集合；candidateCommands 输出可执行文件候选；versionProbe 接收候选命令并返回版本证据；formatFingerprints 接收候选文件或数据库并返回格式分数和必需字段命中情况；capabilityProbes 根据版本、格式和认证证据生成能力位；summaryScanner 与 transcriptReader 只在相应能力 confirmed 后读取会话摘要或内容；resumeExecutor 与 terminalAttacher 只有在操作能力 confirmed 且 operationPolicy 允许时才暴露给控制层。

证据记录与能力位一一绑定。每条证据至少包含 evidenceRef、evidenceType、source、pathHash 或 commandName、runtimeVersion、observedAt、expiresAt、confidence、matchedFields、errorCode 和 message。evidenceType 可以是 binaryVersion、authFile、statePath、jsonlFingerprint、jsonFingerprint、sqliteSchema、processReachable 或 resumeDryCheck。探测链路的输入是适配注册项与本地文件系统/命令执行许可，输出是带证据引用的 capabilityMap；后续摘要扫描、内容读取和 UI 渲染只读取 capabilityMap，不直接重新推断 runtime 名称或目录含义。

能力位采用状态机更新。初始状态为 unconfirmed；探测器取得明确匹配证据且证据未过期时转为 confirmed；探测器识别到当前版本明确不支持某项能力时转为 unsupported；发生超时、权限拒绝、解析异常、数据库锁定或命令返回异常时转为 error，并记录错误证据。证据超过 expiresAt、候选路径消失、runtimeVersion 变化、格式指纹分数低于阈值或用户撤销目录读取授权时，confirmed 回退为 unconfirmed 或 error。已确认的会话扫描能力不会自动确认继续、删除或终端附着能力，各能力必须由各自探测器独立确认。

候选项选择按照确定性优先级执行：用户显式配置路径优先于环境变量路径，环境变量路径优先于默认 home 目录；带版本配置文件或 manifest 的状态目录优先于仅存在目录的候选；SQLite 表结构完整的状态库优先于零散日志文件；格式指纹分数高、必需字段完整、解析错误率低且 observedAt 更新的证据优先于旧证据。多个候选 source 同时有效时，扫描器可以并行读取，但必须以 runtimeKind、source 和 id 形成复合键；同一源记录被多个解析器识别时，只选择格式分数最高且必需字段完整的解析器，禁止为同一源记录生成重复会话。

运行时探测具有明确前置条件：本地文件系统可访问，候选路径集合已加载，用户允许读取相关目录，命令探测在受限参数集内执行，并设置单项探测超时。当仅检测到二进制存在而未确认状态源时，系统只显示 runtime 可发现状态，不生成会话列表；当能识别历史会话但不能确认 resumeExecutor 时，系统只开放 transcriptRead，不显示继续输入框、终端附着入口或删除入口；当版本未知导致格式证据失效时，系统保留历史偏好但将操作能力回退为 unconfirmed，等待下一次探测重新确认。

### 本地会话只读识别与归一化

目标 runtime 的本地会话接入采用只读扫描流程。扫描前置条件为 statePath 或等效状态源能力 confirmed、候选文件满足扩展名和大小上限、格式指纹达到最低匹配阈值、解析器版本与格式证据匹配。扫描器先枚举候选 source，检查路径是否为允许根目录下的规范化绝对路径，拒绝路径穿越和未授权符号链接；再以只读文件句柄或只读数据库连接访问源数据，不创建索引、不写入缓存到源目录、不修改 mtime、不迁移原始文件。对超大文件、持续增长日志、不可读目录、损坏数据库或锁定数据库，扫描器按候选粒度终止读取并返回错误证据，不影响其他候选 source。

格式指纹至少检查以下项目：JSONL 文件读取有限数量的首尾样本行，要求每行可独立解析，并检查 sessionId 或 id、timestamp、type 或 role、message 或 content、model、cwd 等字段；JSON 文件检查顶层 version、sessions、messages 或 metadata 字段，以及消息数组中的 role、content 和时间字段；SQLite 状态库以只读方式检查必需表名、列名和索引信息，例如 sessions 表的 id、model、started_at、ended_at 字段以及 messages 表的 session_id、role、content、timestamp 字段。每项指纹输出格式分数、必需字段命中数和解析错误率；低于阈值或关键字段缺失时，该候选被标记为 incompatible，而不是交给宽松解析器猜测。

解析得到的会话被转换为统一会话摘要模型。该模型至少包括 id、runtimeKind、source、displayKey、model、workingDir、startTime、lastActivity、active、userMessages、assistantMessages、toolUses、tokens、lastUserPrompt、capabilities、fieldConfidence 和 evidenceRefs。id 优先采用 runtime 原生会话 id；无原生 id 时，基于规范化 source 路径、数据库主键或文件相对路径生成稳定派生 id。displayKey 优先采用标题、项目 slug 或工作目录叶子名，否则使用 id 短形式。startTime 取最早可信消息时间或会话开始字段；lastActivity 取最后可信消息时间、结束时间或源文件 mtime 中证据等级最高者；tokens 不存在时置为 null，不以消息长度估算为已确认 token。lastUserPrompt 超过展示上限时截断并标记为 preview 字段。

字段可信度按字段级 evidenceRef 记录。sessionId、workingDir、active、resumeCommand 等会影响控制操作的字段只有在来源字段明确、路径规范化成功、格式证据未过期且未发生冲突时才标记为 high；由文件名、目录名或 mtime 推导的字段标记为 derived；缺失、冲突或时间异常的字段标记为 unknown。后续能力判断不能把 derived 或 unknown 字段提升为 confirmed 操作能力，尤其不得基于推导出的 sessionId 开启继续会话或删除源会话。

统一会话列表的去重和合并按层级执行：不同 runtimeKind 的相同 id 互不覆盖；同一 runtimeKind 下不同 source 的相同 id 保留为不同来源，除非格式证据表明二者是同一会话的摘要文件和状态库记录；同一会话多来源合并时，状态库主键和明确版本字段优先于日志文件，原生 session id 优先于派生 id，最近可信消息时间优先于文件 mtime。扫描结果只更新会话事实字段，不覆盖工作台本地保存的 displayName、colorTag、置顶或筛选偏好。排序以 lastActivity 的可信时间为主，缺失时退到 startTime 或 source mtime，并在输出数量达到上限时截断。

active 状态由目标 runtime 适配器独立计算，并与能力位状态分离。判定优先级为：明确运行进程或网关可达且能与 session id 对应时优先；状态库中未结束会话或运行中标记次之；最近消息时间落入预设活跃窗口再次之。进程存在但无法关联到当前 session id 时不得标记为 active，可标记为 unknown 或 idle；时间戳缺失、未来时间超过容忍范围、lastActivity 早于 startTime 或消息时间来自低可信字段时，不得覆盖高可信运行证据，也不得单独将会话标记为 active。

### 会话内容读取与继续会话门控

会话内容读取器与会话摘要扫描器分离设置。transcriptRead 的前置条件为：会话 id 来自未过期摘要扫描结果，runtimeKind 与 source 匹配，格式证据仍有效，读取范围不超过条数、字节数和单条消息长度上限。当用户选中会话时，内容读取器只读取最近限定数量的消息；JSONL 按行读取并跳过坏行，坏行比例超过阈值时终止该 source；SQLite 按 session_id 和 timestamp 排序读取；跨文件会话先按消息时间排序，时间缺失时按文件 mtime 和行号稳定排序。工具调用与工具结果通过 toolUseId、call_id 或相邻消息关系配对，无法配对的工具结果以 system 部件保留但不触发控制操作。

内容读取器输出统一消息部件，包括 user、assistant 和 system 角色，以及 text、thinking、tool_use、tool_result 等部件类型。单条文本、工具输入和工具结果均在后端截断到预设最大长度，超出部分只标记 truncated，不继续读取完整源内容。读取过程中遇到恶意构造的深层 JSON、超长单行、不可解码字节或循环符号链接时，读取器终止当前候选并返回 parseError 或 unsafePath 错误证据；已经读取的其他候选消息仍可返回，且不会写入源目录。

继续会话能力作为独立门控能力处理，不随会话可识别或 transcriptRead 可用而自动开启。resumeExecutor 的前置条件为 canResume 为 confirmed、会话 id 通过字符集和存在性校验、prompt 编码有效且长度未超过上限、命令模板在白名单内、工作目录位于允许根目录且未越界、当前没有同一会话的互斥执行任务。命令构造采用固定模板加参数数组，不拼接 shell 字符串；只传入必要环境变量，默认不继承敏感变量；stdout、stderr 和可选输出文件均受长度限制；超时后终止子进程并清理句柄或临时文件。返回码映射为 ok、resumeTimeout、noOutput、commandNotFound、nonZeroExit、permissionDenied 或 unsafeWorkingDir；失败结果不更新会话摘要，也不把 canResume 重新标记为 confirmed。

对于尚不能确认继续能力的目标 runtime，工作台仍可使用只读 transcriptRead 展示历史消息，但继续输入框、终端切换、删除源会话和修改源会话按钮保持隐藏或禁用，并显示能力未确认或不支持的状态。若一次继续执行超时、无输出或返回非零码，系统只返回该次执行错误，不将 prompt 写入统一会话摘要，不推断会话已经继续成功，也不覆盖 active 状态。该规则避免未知版本 runtime 被误当作可恢复会话，降低会话污染、重复执行或破坏用户本地工作目录的风险。

### 隔离降级与工作台展示

统一会话 API 在汇聚时分别调用网关会话读取器、既有本地 runtime 扫描器和目标 runtime 适配器，并为每个 runtimeKind/source 设置独立超时、独立错误收集和独立结果数组。错误对象至少包含 runtimeKind、source、phase、code、recoverable、message 和 evidenceRef；code 可区分 pathMissing、permissionDenied、unsafePath、formatIncompatible、dbLocked、dbCorrupt、parseError、scanTimeout、probeTimeout 和 commandError。合并阶段接收部分成功结果，目标 runtime 的权限不足、文件损坏、数据库锁定或解析超时仅随该 source 的错误摘要返回，不抛出导致整个 sessions 响应失败的全局异常。

前端工作台根据统一会话摘要中的 runtimeKind、source、active、workingDir、model、tokens、lastUserPrompt、capabilities 和 fieldConfidence 渲染会话行。能力位与 UI 操作一一映射：canReadTranscript 为 confirmed 时显示 transcript 入口；canResume 为 confirmed 时显示继续输入框；canAttachTerminal 为 confirmed 时显示终端入口；canDeleteSource 为 confirmed 时显示删除源会话入口；canModifySource 为 confirmed 时显示影响源 runtime 数据的修改入口；本地 displayName 和 colorTag 只依赖工作台偏好能力，不代表源会话可修改。前端不得根据 runtime 名称、图标或历史适配经验绕过能力位；能力位缺省为不可用。

当目标 runtime 后续版本稳定后，只需在适配注册项中补充新的候选路径、格式指纹、能力探测器或 resumeExecutor，并由能力状态机在证据有效时将相应能力从 unconfirmed 更新为 confirmed；统一会话列表、transcriptRead、排序、偏好保存和 UI 门控逻辑无需重写。若后续版本改变目录结构或会话格式，旧证据因版本变化或指纹不匹配自动失效，系统回退到只读或不可用状态并保留错误摘要。由此，版本变化时的安全接入效果由候选项证据化、能力位回退、未确认能力不暴露和解析失败局部隔离共同实现。
