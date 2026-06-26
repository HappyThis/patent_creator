## 技术方案

### 总体接入架构

新增本地 agent runtime 是指区别于 OpenClaw、Claude Code、Codex CLI、Hermes 等既有 runtime、但同样在用户本机保存运行状态和会话记录的 agent 执行环境。其本地目录、数据格式、会话标识和继续会话命令可能随版本变化；若直接接入现有工作台，容易造成会话误识别、未确认能力被误展示为可执行操作、继续会话命令误执行、不同 runtime 记录相互覆盖以及扫描异常扩散到统一会话接口等技术风险。

本方案在现有智能工作台的本地会话管理能力之上，为新增本地 agent runtime 设置独立接入适配层。该适配层包括 runtime 描述符、状态目录发现器、格式识别器、会话解析器、能力探针和结果隔离器；其不替换既有 runtime 的检测、扫描和管理逻辑，而是把新增 runtime 的本地状态转换为工作台已经使用的统一会话对象，再交由现有聚合、去重、排序、缓存和展示流程处理。

runtime 描述符采用结构化对象声明接入边界，至少包括 runtimeId、displayName、executableCandidates、versionProbe、authProbe、stateRootCandidates、carrierCandidates、parserCandidates、commandTemplates、capabilityConditions、limits 和 priority 字段。其中 runtimeId 作为命名空间标识；executableCandidates、stateRootCandidates 和 carrierCandidates 均为按优先级排列的候选项；versionProbe 和 authProbe 声明版本与认证探测方式；parserCandidates 声明可用解析器及版本约束；commandTemplates 仅声明经探针确认后可执行的命令模板；capabilityConditions 声明只读展示、转录读取、继续会话等能力的确认条件；limits 声明扫描深度、文件大小、记录数量、命令超时和提示长度等边界；priority 用于同等证据下的解析器或候选项排序。

工作台启动或刷新时，处理流程依次为：加载 runtime 描述符，执行分层探针，生成候选会话载体，识别载体格式，选择并运行解析器，产生带证据链的候选会话，映射为统一会话模型，按复合键聚合并写入新增 runtime 命名空间缓存，计算 runtime 级、载体级和会话级能力，最后由统一接口输出给前端。任一阶段的输出均作为下一阶段的输入；若某阶段失败，失败结果被写成局部状态或诊断证据，不向既有 runtime 的处理链路传播。

### 本地状态发现与能力确认

本地状态发现阶段输出统一的 ProbeResult 对象，其字段包括 runtimeId、probeLayer、candidateId、status、version、resolvedPath、capabilities、evidence、errorCode 和 checkedAt。probeLayer 表示安装、配置、状态根目录、会话载体、命令能力等探测层级；status 取未检测、已确认、部分确认、不可用、未知或错误。安装探针接收 executableCandidates 并输出可执行文件路径、版本和安装状态；配置探针接收 authProbe 和配置候选并输出认证或模型配置状态；目录探针接收 stateRootCandidates 并输出存在且可读的状态根目录；载体探针接收 carrierCandidates 并输出候选载体及其类型；命令探针接收 commandTemplates 并输出可执行命令规格。

各层探针按“能否确认能力”而非“前一层是否完全成功”决定是否继续。未发现可执行文件但发现可读状态目录时，系统仍可进入只读历史会话读取，但 runtime 级执行能力保持未知或不可用；认证状态无效时，历史摘要和转录读取可在只读条件满足时展示，继续会话和停止会话不得确认。每层探针均只读运行，失败只改变新增本地 agent runtime 的局部能力状态，不中断既有 runtime 的扫描，也不向统一会话接口抛出未捕获异常。

证据链 Evidence 至少包括 evidenceId、sourcePath、carrierType、parserId、parserVersion、matchedFeatures、requiredFieldStatus、timeSource、sessionIdSource、messageCountSource、capabilityCondition、errorCode、confidenceScore 和 timeAdjusted 字段。requiredFieldStatus 记录 sessionId、lastActivity、messageCount 等必要字段是否存在且类型合法；matchedFeatures 记录目录、扩展名、表结构、JSON 字段、schema 指纹等命中特征；confidenceScore 根据必要字段完整性、schema 指纹匹配度、时间自洽性、消息结构完整性和解析器优先级计算。

可信等级按明确条件划分：sessionId、lastActivity、messageCount 等必要字段全部存在且相互自洽、载体格式被解析器确认、异常比例未超过描述符 limits 阈值的记录，进入可展示会话；缺少非必要字段但必要字段完整的记录可展示但不开放高风险操作；仅匹配目录或扩展名、缺少 sessionId 或时间不可解析的记录进入诊断候选；载体可访问但 schema 不匹配或异常比例超限的记录标记为不可解析载体；命令、认证或参数映射缺乏确认时，对应能力为未知，不因会话可读而自动确认。

格式识别按载体类型、抽样读取、schema 指纹、必要字段集合和消息结构的顺序执行。JSONL 载体先读取文件头部、尾部或最近修改片段，遇到半写入行时丢弃末尾不完整行，逐行解析并统计异常行比例；异常比例超过 limits 中阈值时降级为诊断候选，未超过阈值时跳过异常行继续解析。SQLite 载体以只读连接打开，先查询表和字段元数据，再限定返回最近记录；数据库锁定、损坏或权限不足时记录 errorCode 并返回不可解析或未知状态，不重试写锁。目录型载体使用固定最大深度和访问节点集合避免符号链接循环，文件过大时仅按安全窗口截断读取，编码错误时尝试通用文本解码并标记 evidence，扫描中遇到文件删除竞态或网络盘延迟时跳过该载体并保留批次错误。

### 会话解析与统一会话模型映射

会话解析阶段将新增本地 agent runtime 的候选记录映射为统一会话模型。sessionId 优先取原始记录中的显式会话标识；若显式标识为空、重复或含非法字符，则使用 runtimeId、sourcePath、载体内稳定偏移或消息序列哈希生成派生 ID，并把该记录限制为只读展示或诊断，不作为继续会话参数。runtime、kind 和 source 来自描述符与载体来源；startTime 取首条消息时间、会话开始字段或文件创建时间中的可信来源；lastActivity 取最后消息时间、会话结束时间、文件修改时间中的最大可信值，并保留 timeSource。

消息计数由用户、助手和系统消息序列统计得到；toolCallCount 从工具调用块、工具调用表或事件字段统计得到；token 或成本统计仅在原始记录提供 usage、token_count 或 cost 字段时写入，否则为空；lastUserPrompt 从按时间排序后的最后一条用户文本截取获得，消息体过大时按 limits 截断；workingDirectory 优先取会话元数据中的 cwd 或 projectPath，并在路径非法、越界或缺失时置空。开始时间晚于最近活动时间时，若二者来源可靠则交换并标记 timeAdjusted，否则降级可信等级；超过本机当前时间容忍阈值的未来时间被钳制到当前时间并写入 evidence。

解析器候选链采用竞争仲裁规则。系统先根据版本号、schema 指纹或目录结构选择优先解析器，优先解析器无法确认 sessionId、lastActivity、messageCount 等必要字段，或字段冲突、异常比例超过 limits 时，降级尝试通用 JSONL、通用 SQLite 或目录摘要解析器。多个解析器命中同一会话时，按 schema 指纹精确匹配、必要字段完整性、时间自洽性、消息结构完整性、解析器 priority 和上一批稳定结果的顺序排序；同等级时保留上一批稳定结果或选择描述符优先级更高的解析器。

映射后的会话以复合键参与聚合，复合键由 namespace、runtimeId、source 和 originalSessionId 构成。同一 runtime 内 originalSessionId 为空或非法时，不进入可执行管理集合；重复标识若来自同一载体，则保留 lastActivity 较新且证据等级更高的记录；若同一会话同时出现在 JSONL、SQLite 或目录摘要等多个载体中，则选择证据等级最高的载体作为主记录，其他载体只作为补充 evidence，不覆盖主记录中已确认的字段。该规则避免新增会话编号与既有 runtime 或 gateway 会话编号碰撞，也避免新增扫描结果覆盖其他 runtime 的记录。

### 操作能力门控与隔离管理

能力统一称为 capability，并区分 runtime 级、载体级和会话级。runtime 级 capability 表示该 runtime 是否已安装、已配置、可执行命令；载体级 capability 表示某类本地状态载体是否可读、可解析、可读取转录；会话级 capability 表示某条会话是否可展示、可读取转录、可继续或可停止。只读展示需要会话级必要字段完整且载体可解析；转录读取需要载体级转录解析器确认且会话 ID 可定位；继续会话需要 runtime 级命令确认、认证有效、会话级原始 ID 可映射、参数模板确认和操作者权限满足；停止会话需要 runtime 正在运行、停止命令模板确认并且会话仍处于可停止状态。

继续会话请求进入执行链路前，后端重新读取 capability 快照并进行二次校验，不使用仅由解析器推断的命令路径或参数。命令模板从描述符 commandTemplates 中选择，且必须已经由命令探针确认为 confirmed；会话标识只能来自原始会话 ID 的白名单映射，参数名必须属于模板允许集合，参数值经过长度限制、字符白名单或转义处理；提示内容超过 limits.promptMaxLength 时直接拒绝或按配置截断；输出文件只能生成在工作台受控临时目录内；命令执行设置超时、最大输出长度和退出码检查。

继续会话失败被映射为命令不存在、认证失效、权限不足、参数非法、执行超时、退出码非零、输出为空或输出格式不符等错误类型。参数非法、单次超时、输出为空和退出码非零仅影响当前请求；命令不存在、认证失效或命令模板连续校验失败会使对应 runtime 级 capability 从 confirmed 降级为未知或不可用，等待下一次探针重新确认。无论失败类型如何，失败处理不得删除、覆盖或标记 inactive 任何会话聚合记录，也不得影响其他 runtime 的 capability。

会话扫描和转录读取设置局部隔离边界：权限不足、数据库锁定、文件正在写入、JSONL 半行、符号链接循环、网络盘延迟、文件删除竞态、编码错误和消息体过大等异常均被捕获为 evidence.errorCode，并按载体或会话粒度降级。新增本地 agent runtime 的解析异常返回空会话、诊断候选或不可解析载体状态，不抛出到统一会话接口；空扫描结果、异常扫描结果或未确认候选结果不得删除其他 runtime 的会话，也不得把同一 runtime 上一批稳定结果直接置为不存在。

### 与现有工作台流程的协同

新增本地 agent runtime 的扫描采用增量同步状态机。每次刷新生成 batchId，并读取上一批缓存的水位线、文件修改时间、文件大小或哈希摘要；本批次解析成功的会话按 namespace、runtimeId、source、originalSessionId 和 batchId 写入缓存，触发 upsert；上一批存在但本批次未命中的记录先标记为 stale，不立即删除；连续多个成功批次均未命中或明确收到删除证据时，才标记 inactive 或写入 tombstone；批次取消、载体级错误或全量扫描失败时，不提交覆盖性清理，继续保留上一批稳定结果。

活跃状态按优先级计算：首先检查会话结束标志，若存在可信结束时间则判定为 inactive；其次检查最近活动时间，若其在描述符定义的活跃窗口内且 runtime 进程或 gateway 探针为 confirmed，则判定为 active；若文件仍在更新但消息结构不完整，则保持上一批状态并标记 stale 或 partial；若最近活动时间超过本机时间容忍阈值，则钳制为当前时间并降低可信等级；若 runtime 正在运行但会话结束标志可信，则结束标志优先。该规则避免仅凭文件存在、目录命中或进程运行把历史会话误判为活动会话。

工作台仍以统一会话聚合接口向前端提供数据，前端不直接读取新增 runtime 的本地文件或数据库。前端根据统一会话对象中的 kind、runtimeId、source、active、lastActivity、confidenceScore 和 capability 字段进行分组、计数、筛选和展示；只有后端输出 confirmed capability，且该会话的参数映射成功时，才显示继续会话、停止会话等可执行入口。诊断候选、不可解析载体和能力未知状态仅显示为状态或提示，不提供可执行按钮。

通过复合键、缓存命名空间、批次提交和 capability 门控的协同，新增 runtime 的空扫描、异常扫描和未确认候选均只影响其自身命名空间，不会删除、覆盖或污染 OpenClaw、Claude Code、Codex CLI、Hermes 或 gateway 的会话记录。证据链和可信等级阈值保证只有已确认的本地会话进入统一列表；只读连接、异常隔离和批次提交保证解析失败不会中断既有会话列表；后端 confirmed capability 与会话级参数映射共同保证未确认能力不会被呈现为可用操作。
