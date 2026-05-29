## 技术方案

本方案提出一种信号-侧车（Signal-Sidecar）架构，将知识上下文树中可共享、可审阅的静态知识内容与频繁变化的动态运行时信号分离存储，解决当前方案中动态评分信息反复改写共享 Markdown 知识文件导致版本控制污染、团队合并冲突增多、知识内容变化被噪声淹没的问题。

### 整体架构

系统将当前存储在 Markdown 文件 YAML 前置元数据（frontmatter）中的所有字段划分为两类：（1）静态知识字段，继续保留在共享的 .md 文件中供版本控制和团队审阅；（2）动态信号字段，迁移到同目录下的独立信号侧车文件中，侧车文件被版本控制系统排除（如 .gitignore），仅在本机维护。读取路径在检索和排序时自动融合两侧数据，写入路径分别路由到静知识文件和侧车文件。

### 字段划分规则

基于当前 ByteRover context tree 中 Markdown 文件的 YAML frontmatter 字段，划分如下：

- 静态知识字段（保留在 .md frontmatter 中）：title（标题）、summary（摘要）、tags（标签）、keywords（关键词）、related（关联路径）、createdAt（创建时间戳）。这些字段由人工审阅或 curate 流程写入，变化频率低，是团队共享和版本对比的核心内容。
- 动态信号字段（迁移到侧车文件）：importance（重要性评分）、recency（新鲜度）、maturity（成熟度等级：draft/validated/core）、accessCount（检索命中次数）、updateCount（curate 更新次数）、updatedAt（最后更新时间戳）。这些字段由系统在每次检索命中、curate 更新、时间衰减计算时自动修改，变化频率高，属于本机运行时状态。

### 侧车文件格式与排除规则

每个 .md 知识文件对应一个同名但不同扩展名的侧车文件，存放于同一目录下。例如，文件 auth/jwt-tokens/refresh-flow.md 对应侧车文件 auth/jwt-tokens/refresh-flow.signal.json。侧车文件采用 JSON 格式，结构如下：

- content_hash：对应 .md 文件正文（不含 frontmatter）的内容哈希，用于检测侧车是否与当前 .md 内容匹配。
- importance：数值型，范围 [0, 100]，默认 50。
- recency：数值型，范围 [0, 1]，默认 1。
- maturity：枚举型，取值为 draft、validated、core，默认 draft。
- accessCount：整数型，默认 0。
- updateCount：整数型，默认 0。
- updatedAt：ISO 8601 时间字符串。
- version：侧车格式版本号，用于未来的格式演进。

### 读取融合机制

读取融合在检索入口处执行，核心流程如下：

1. 检索器（如 SearchKnowledgeService）通过 BM25 或其他文本匹配算法在 .md 文件集合上执行全文搜索，得到候选文件路径及其原始文本相关性评分。
2. 对于每个候选文件，系统并行读取对应的 .signal.json 侧车文件。若侧车文件不存在或读取失败，使用默认信号值（importance=50, recency=1, maturity=draft），保证降级可用。
3. 通过 content_hash 校验侧车与 .md 内容的一致性：若 hash 不匹配（例如 .md 被外部编辑或版本回退），则丢弃该侧车信号，使用默认值，并在后台触发一次信号重算。
4. 将文本相关性评分与侧车信号（importance、recency、maturity）输入复合评分函数 compoundScore()，得到融合后的最终排序分。maturity 等级提供 tier 加成系数（core: 1.15, validated: 1.0, draft: 0.85）。
5. 按最终排序分降序返回结果。

### 迁移期兼容

迁移期兼容策略确保从旧格式（信号存储在 .md frontmatter 中）平滑过渡，无需用户手工操作：

1. 系统启动或首次打开项目时，扫描 context tree 中的所有 .md 文件，检测其 frontmatter 中是否含有动态信号字段（importance、recency、maturity、accessCount、updateCount、updatedAt）。
2. 若 .md 的 frontmatter 存在这些字段，且对应侧车文件不存在，则执行一次迁移：从 frontmatter 提取动态信号值写入侧车文件，然后从 .md 的 frontmatter 中移除这些字段，仅保留静态知识字段，并将更新后的 .md 写回磁盘。
3. 若 .md 的 frontmatter 存在动态信号字段，且侧车文件也存在（内容可能来自其他机器的同步），则比较两者的 updatedAt 时间戳，选择较新的信号值写入侧车文件，同时从 .md 的 frontmatter 中移除动态信号字段。
4. 迁移完成后，后续所有动态信号更新仅写入侧车文件，不再触及 .md。
5. 迁移操作为一次性行为，完成后该 .md 文件不再包含动态信号字段。迁移过程失败时记录日志并跳过，下次启动重新尝试。

### 并发更新与一致性

动态信号的写入场景包括检索命中时递增 accessCount、curate 操作时递增 updateCount 并重算 importance、以及后台 dream 任务执行时间衰减。这些写入仅针对侧车文件，且采用以下机制保证并发安全：

1. 原子写入：侧车文件更新采用写临时文件 + 原子重命名的模式（write-temp-then-rename），避免写入过程中其他进程读到不完整的 JSON。
2. 版本号乐观锁：每个侧车文件内维护 version 字段。写入前先读取当前 version，写入时将 version+1 作为条件；若在 read 和 write 之间 version 已被其他进程递增，则本次写入被拒绝，调用方可选择重试或合并后重写。
3. 批量延迟写回：检索命中产生的 accessCount 增量不立即逐条写回，而是先在内存中累积（pendingAccessHits 映射表），在索引重建或定时刷新（如 5 秒间隔）时批量写回侧车文件，降低 IO 频率。
4. 并发写入隔离：不同知识文件的侧车文件相互独立，同一文件的并发写入通过上述乐观锁串行化，不同文件的写入可完全并行。

### 失败隔离

侧车文件的故障不应影响核心知识检索和团队协作功能。系统采用 fail-open 策略：

- 侧车文件读取失败（文件不存在、JSON 解析错误、权限错误）：使用默认信号值，检索和排序继续正常工作，仅排序精度暂时下降。
- 侧车文件写入失败（磁盘满、权限错误）：记录警告日志并跳过本次更新，该次信号变化丢失但系统继续运行，下次操作重新计算。
- content_hash 校验失败（.md 已被外部修改导致侧车不匹配）：丢弃侧车信号，使用默认值，同时触发后台异步任务基于当前 .md 内容重算侧车信号。
- 孤儿侧车清理：在 dream 维护任务（prune）中检测无对应 .md 文件的侧车文件，自动删除。

### 归档、合并与同步的清理一致性

在现有 dream 操作的归档（archive）、剪枝（prune）、合并（consolidate）流程中，侧车文件需与对应的 .md 文件保持生命周期一致：

1. 归档操作：当 prune 操作判定一个 .md 文件需要归档时，archiveEntry 服务在将 .md 内容写入 _archived 目录的 .full.md（无损完整内容）和 .stub.md（可搜索幽灵线索）的同时，将该 .md 对应的 .signal.json 侧车文件也复制到 _archived 目录下对应路径，并删除原始目录下的侧车文件。归档后，signal 文件从活跃区消失，但在 _archived 区保留完整信号历史。
2. 合并操作：当 consolidate 操作判定两个 .md 文件需要合并（MERGE 动作）时，系统将两个源文件的侧车信号按 mergeScoring 规则合并（importance 取较高值、recency 取较新值、accessCount 和 updateCount 累加、maturity 以 merged importance 重新判定），写入合并后新 .md 对应的侧车文件，同时删除被合并源文件的侧车文件。
3. CoGit 同步排除：侧车文件（.signal.json）被加入 isExcludedFromSync 谓词，与现有的 _index.md、_manifest.json、_archived/* 等派生产物一起被排除在快照追踪、push/pull/merge 等版本同步操作之外，确保团队共享的是干净的静态知识内容。
4. 孤儿清理：prune 操作在扫描 candidate 文件时，同时检测侧车目录中是否存在无对应 .md 文件的孤儿侧车，若存在则一并删除。

### 知识写入与信号写入的双通道分离

改造后的写入路径实现知识内容与运行时信号的双通道分离：

- 知识通道：curate 工具执行后，MarkdownWriter.generateContext() 仅生成包含静态知识字段的 frontmatter（title、summary、tags、keywords、related、createdAt），不再包含动态信号字段。生成的 .md 文件写入 context tree 目录。
- 信号通道：curate 执行完成后，curate-executor 或后续的信号重算流程为每个新建或修改的 .md 文件生成初始侧车信号（importance=50、recency=1、maturity=draft、accessCount=0、updateCount=1），写入对应的 .signal.json。后续 search 触发的 accessCount 递增和 dream 任务触发的时间衰减均仅修改侧车文件。
- content_hash 写入：每次 curate 更新 .md 后，系统重新计算 .md 正文（去除 frontmatter 部分）的内容哈希并写入侧车文件的 content_hash 字段，作为后续读取融合时的内容一致性校验锚点。

### 技术效果

本方案在保持知识检索和排序能力不降级的前提下，实现以下技术效果：

- 版本控制清洁：动态信号写入不再触发 .md 文件变更，团队 push/pull 时仅传输真正的知识内容变化，消除了因 accessCount、importance 等字段变更导致的合并冲突。
- 知识变更可审阅：curate 操作产生的 diff 仅包含知识内容本身的变化（新增事实、更新描述、修改关联关系等），不再混杂信号噪声，团队 code review 效率显著提升。
- 信号保真度不变：侧车文件完整保存所有动态信号，复合评分函数 compoundScore() 的输入参数和数据流与改造前完全一致，检索排序精度不变。
- 故障隔离：侧车文件的任何故障不影响知识检索核心路径，系统始终可用，仅信号精度暂时回退到默认值。
- 迁移无感知：一次性自动迁移将旧格式 .md 中的动态信号字段提取到侧车文件，用户无需手工操作。旧格式和新格式文件可在同一 context tree 中共存，渐进式完成迁移。

### 风险与待确认问题

以下为本方案中需要后续确认和验证的技术风险点：

- 信号精度回退窗口：迁移期存在新旧格式共存阶段，此期间旧格式 .md 的动态信号更新仍会改写 .md 文件，产生短暂的版本控制噪声。完全消除需等待全量迁移完成。
- 乐观锁重试开销：在高并发检索场景（多 agent 同时查询同一知识文件）下，侧车文件 version 冲突可能导致多次重试。建议监控重试率并设置最大重试次数（如 3 次）后降级为 last-write-wins。
- CoGit 远端兼容：当前 CoGit 快照文件（CogitSnapshotFile）结构假设 .md 文件自包含所有上下文信息。若远端旧版本客户端仍向 .md frontmatter 写入动态信号字段，需要在 merge 流程中增加字段过滤逻辑，在写入本地 disk 前剥离动态信号字段并转存到侧车文件。
- dream 任务的双写过渡：在 prune 和 consolidate 操作中，当前代码通过 parseFrontmatterScoring 从 .md 中读取信号。改造后需要修改这些读取点为从侧车文件读取，同时保留从 .md frontmatter 读取的兼容路径。
