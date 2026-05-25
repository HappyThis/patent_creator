## 技术方案

本技术方案针对 ByteRover 上下文树（Context Tree）系统中知识文件的版本控制友好性与运行时信号利用之间的矛盾，提出一种双层分离式知识维护架构：将可共享、可审阅的语义知识内容保留在版本控制下的 Markdown 文件中，而将频繁变化的运行时动态信号迁移至独立于版本控制的信号存储层，并在检索索引构建时进行读取融合，从而实现知识共享与信号利用的兼顾。

### 技术问题分析

ByteRover 的上下文树将项目知识以 Markdown 文件形式存储在 .brv/context-tree/ 目录下，每个知识文件包含 YAML 前置元数据（frontmatter）和 Markdown 正文。当前系统中，前置元数据既包含可共享的语义字段（如 title、summary、tags、related、keywords），也包含与运行时行为绑定的动态信号字段（如 importance、recency、maturity、accessCount、updateCount、updatedAt）。SearchKnowledgeService 在每次执行 BM25 检索时，会将搜索命中的访问计数累积在内存中（pendingAccessHits），并在索引重建时批量回写到对应 Markdown 文件的前置元数据中，更新其 importance 和 accessCount 值。Dream 子系统在执行 consolidate 和 prune 操作时，也会读取和改写这些动态字段。

由于这些动态信号随每次检索访问、每次知识整理操作而频繁变化，导致以下问题：（1）版本控制状态持续变脏：团队成员的任何检索操作都可能触发 Markdown 文件改写，使得 git status 中出现大量仅 scoring 字段变化的 diff，真正的知识内容变更被淹没在噪声中；（2）团队合并冲突增多：多个团队成员各自的本地检索行为会产生互不兼容的 scoring 更新，在合并时产生无意义的冲突；（3）审阅负担增加：在进行 Pull Request 或知识审阅时，审阅者需要从 scoring 数值变化中辨别真正的知识内容修改。

### 整体架构：双层分离模型

方案的核心思想是将上下文树的知识数据拆分为两个逻辑层：

- 共享知识层（Shared Knowledge Layer）：继续以 Markdown 文件形式存储在 .brv/context-tree/ 下，纳入版本控制。文件中仅保留可共享、可审阅的语义字段——title、summary、tags、related、keywords，以及不可变的创建时间 createdAt。这些字段由人工或 LLM 策展（curation）行为产生，变更频率低且具有审阅价值。
- 运行时信号层（Runtime Signal Layer）：新增一个独立于版本控制的信号存储，存放每个知识文件的动态评分数据——importance、recency、maturity、accessCount、updateCount、updatedAt。该存储目录（如 .brv/context-tree/.signals/）通过 .gitignore 排除出版本控制。信号层仅由系统自动维护，不接受人工直接编辑。

两层之间通过文件路径建立映射关系：信号存储使用与 Markdown 文件相同的目录树结构，每个 .md 文件对应一个同路径的信号记录文件（如 .signals/domain/topic/context.signal.yaml）。检索索引构建时，SearchKnowledgeService 同时读取 Markdown 正文内容和信号存储中的评分数据，在内存中融合后计算复合评分（compound score）。

### 字段分类与迁出策略

基于变更频率、审阅价值和共享必要性三个维度，对现有前置元数据字段进行分类：

- 保留在 Markdown 的字段：title、summary、tags、related、keywords、createdAt。这些字段代表知识的语义身份和关系拓扑，变更来自人工策展或 LLM 生成，是团队审阅和版本对比的核心对象。
- 迁出至信号存储的字段：importance（重要性评分，0-100）、recency（新鲜度，0-1）、maturity（成熟度层级：draft/validated/core）、accessCount（累计访问次数）、updateCount（累计策展更新次数）、updatedAt（最后更新时间戳）。这些字段由检索命中、时间衰减、策展操作等系统行为自动驱动，变更频繁且不携带语义含义，不适合作为版本控制的内容。

createdAt 保留在 Markdown 中是因为它表示知识文件的创建时间，一旦设定即不可变，属于知识的身份属性而非运行时状态。updatedAt 迁出是因为在双层模型中，Markdown 文件的文件系统修改时间（mtime）已经反映了知识内容的最后修改时间，而 updatedAt 在信号层中反映的是评分数据的最后更新时间，两者语义不再重合。

### 信号存储层设计

信号存储层位于 .brv/context-tree/.signals/ 目录下，通过 .gitignore 排除出版本控制。该目录镜像上下文树的目录结构，每个知识 Markdown 文件对应一个同路径的信号记录文件，采用 .signal.yaml 扩展名：

信号记录文件为 YAML 格式，仅包含运行时动态字段。示例结构：importance: 72.5, recency: 0.83, maturity: validated, accessCount: 14, updateCount: 3, updatedAt: "2026-01-15T10:30:00.000Z"。

信号存储的写入采用以下机制：SearchKnowledgeService 的 pendingAccessHits 仍然在内存中累积，但在 flushAccessHits 阶段不再回写 Markdown 文件的前置元数据，而是写入对应的 .signal.yaml 文件。写入时采用原子写入策略（先写临时文件，再 rename 到目标路径），避免写入过程中崩溃导致的数据损坏。

信号存储的读取采用懒加载与缓存策略：SearchKnowledgeService 在构建 MiniSearch 索引时（buildFreshIndex），对每个 Markdown 文件尝试读取其对应的 .signal.yaml，若文件存在则解析其中的评分字段；若不存在（文件尚未生成或为旧格式），则回退到 Markdown 前置元数据中的评分字段（迁移兼容），或使用默认评分（importance=50, recency=1, maturity=draft）。解析后的评分数据缓存在 IndexedDocument.scoring 中，供后续复合评分计算使用。

### 读取融合机制

读取融合在 SearchKnowledgeService 的索引构建阶段（buildFreshIndex）完成，具体流程如下：

1. 对于每个待索引的 Markdown 文件，首先通过 IFileSystem 读取其完整正文内容，解析前置元数据中的 title、keywords、tags、related 等语义字段。
2. 根据该 Markdown 文件的相对路径，构造对应的信号文件路径（将 .md 替换为 .signal.yaml，并添加 .signals/ 前缀），尝试读取信号文件。
3. 若信号文件存在且解析成功，以信号文件中的评分字段为准，构建 IndexedDocument.scoring。
4. 若信号文件不存在，检查 Markdown 前置元数据中是否存在旧格式评分字段（importance、recency、maturity 等）。若存在，提取这些字段作为初始评分数据，同时在内存中标记该文件需要迁移（待 flushAccessHits 时写出信号文件并从 Markdown 中移除评分字段）。
5. 若均不存在，使用 applyDefaultScoring() 生成默认评分（importance=50、recency=1、maturity=draft）。

融合后的 IndexedDocument 包含完整的语义字段和评分字段，SearchKnowledgeService 在检索时使用 compoundScore() 函数计算复合评分。该函数以 BM25 文本相关性为基础权重（0.6），融合 importance（0.2）和 recency（0.2），再乘以 maturity 层级加成系数（core: 1.15, validated: 1.0, draft: 0.85）。融合逻辑与现有实现完全一致，唯一变化是评分数据的来源从 Markdown 前置元数据变为融合后的内存数据结构。

对于 dream 子系统中的 consolidate 和 prune 操作，评分数据的读取同样改为从信号存储层获取。consolidate 操作在执行 MERGE 时，使用 mergeScoring() 函数合并源文件和目标文件的评分数据（importance 取最大值、recency 取最大值、accessCount 求和、maturity 取较高层级），并将合并后的评分写入目标文件的信号记录。

### 并发更新与冲突处理

在单用户场景下，SearchKnowledgeService 使用内存中的 pendingAccessHits Map 累积访问计数，在索引重建的 flushAccessHits 阶段集中写入信号文件。这一批处理机制避免了每次检索都触发磁盘写入，将写入频率从每次检索降低到每次索引重建周期（默认 TTL 为 5 秒）。

在多团队协作场景下，不同成员的本地 ByteRover 实例各自维护独立的信号存储，不存在跨机器的共享信号文件竞争。当通过 brv vc push/pull 或 CoGit 同步上下文树时，仅同步 Markdown 文件（.signal.yaml 文件已被 .gitignore 排除），每个成员在拉取他人的知识内容更新后，本地的 SearchKnowledgeService 会在下一次索引重建时自动为新到达的 Markdown 文件创建信号记录（使用默认评分或从前置元数据中迁移旧评分）。

对于共享知识源（Shared Knowledge Source，通过 brv source add 链接的其他项目上下文树），处理方式为：源项目的信号存储仅对源项目本地有效，不会随共享读取传播。消费者在构建共享源的索引时，若共享源 Markdown 文件的前置元数据中包含旧格式评分字段，则从中提取初始评分；若共享源已采用双层分离方案，则信号文件不在共享范围内，消费者使用默认评分初始化。这种设计确保了共享源所有者本地的检索行为不会影响消费者的评分结果，反之亦然。

对于 dream 子系统可能产生的并发写入（如 consolidate 操作与 flushAccessHits 同时触发），信号文件的原子写入策略（临时文件 + rename）保证了单次写入的完整性。两个并发写入者可能先后覆盖同一信号文件，但考虑到 consolidate 和 flushAccessHits 的触发频率较低且通常不会同时操作同一文件，实际冲突概率很低。若需要更强的并发安全，可在信号文件写入时引入基于文件 mtime 的乐观锁检查。

### 迁移期兼容策略

为确保从现有单层模式平滑过渡到双层分离模式，方案设计了渐进式迁移策略，不要求一次性转换所有历史文件：

- 向后兼容读取：SearchKnowledgeService 在构建索引时，对每个 Markdown 文件优先尝试读取 .signal.yaml。若不存在，回退到读取 Markdown 前置元数据中的评分字段。这意味着未迁移的旧格式文件和已迁移的新格式文件可以在同一上下文树中共存，检索行为不受影响。
- 惰性迁移写入：当 flushAccessHits 需要为某个 Markdown 文件写入更新的评分数据时，检查该文件的前置元数据是否仍包含旧格式评分字段。若包含，则同时执行两个操作：将新评分写入 .signal.yaml，并从 Markdown 文件中移除评分字段（调用 updateScoringInContent 将 importance、recency、maturity、accessCount、updateCount、updatedAt 从前置元数据中删除）。移除操作本身是一次对 Markdown 文件的修改，但这是一次性的迁移修改，后续的评分更新仅影响 .signal.yaml。
- 迁移标记：已迁移的 Markdown 文件在前置元数据中保留一个迁移标记字段（如 _signalMigrated: true），使后续的读取路径可以快速判断是否需要回退读取，避免每次都尝试读取不存在的 .signal.yaml。
- 版本控制影响：惰性迁移过程中对 Markdown 文件的修改（移除评分字段）会被版本控制记录为一次有意义的变更——即该文件已从单层模式迁移至双层模式。团队成员拉取此变更后，本地的 SearchKnowledgeService 将自动识别 _signalMigrated 标记，不再从 Markdown 中读取旧评分。

对于共享知识源（brv source）场景，迁移策略保持一致：源项目的迁移由源项目所有者在其本地完成，消费者无需感知迁移过程。消费者在索引共享源时，若遇到未迁移文件（前置元数据含评分字段），提取评分使用；若遇到已迁移文件（含 _signalMigrated 标记），使用默认评分初始化本地信号记录。

### 失败隔离与降级

信号存储层被设计为系统中的一个可选增强层，其不可用不应影响知识检索的核心功能。

- 信号文件读取失败：若 .signal.yaml 文件因磁盘错误、权限问题或格式损坏而无法解析，SearchKnowledgeService 静默回退到使用默认评分（importance=50, recency=1, maturity=draft），并记录一条调试级别日志。该文件的 BM25 文本检索不受影响，仅排序权重使用默认值。
- 信号文件写入失败：flushAccessHits 在写入 .signal.yaml 时，采用 try-catch 包裹每个文件的写入操作，单个文件的写入失败不影响其他文件的写入，也不影响索引构建的完成。失败的访问计数不会丢失——pendingAccessHits 仅在写入成功后才从内存中清除对应条目，失败的条目保留到下一个写入周期重试。
- 信号存储目录不存在：若 .signals/ 目录被误删除或从未创建，系统在首次写入时自动创建目录（使用 mkdir recursive），降级行为对用户透明。
- dream 操作中的评分读取：consolidate 和 prune 操作在读取评分时，若信号文件不可用，回退到 Markdown 前置元数据或默认评分。这确保了即使信号存储层完全不可用，知识整理操作仍可正常执行。

### 归档、剪枝与合并时的清理一致性

Dream 子系统中的 consolidate（合并）、prune（剪枝/归档）和 synthesize（综合）操作会创建、删除或合并 Markdown 文件，对应的信号记录必须保持一致性。方案设计了以下清理规则：

- 归档（ARCHIVE）操作：当 prune 将 Markdown 文件归档为 .stub.md（存放于 _archived/ 目录）时，对应的 .signal.yaml 也需归档。系统将 .signal.yaml 移动至 _archived/.signals/ 下的对应路径，保留历史评分数据以备审计或恢复。若日后从归档恢复该文件（通过 archive service 的 restore），信号数据同步恢复。
- 合并（MERGE）操作：当 consolidate 将多个 Markdown 文件合并为一个时，源文件的 .signal.yaml 被删除，目标文件的 .signal.yaml 使用 mergeScoring() 函数合并所有源文件的评分数据（importance 取最大值、recency 取最大值、accessCount 求和、updateCount 求和加一、maturity 取较高层级）。这确保了合并后的文件不会丢失累积的访问历史和成熟度。
- 剪枝删除：若 prune 操作直接删除不再需要的文件（非归档），对应的 .signal.yaml 同步删除。
- 孤儿信号清理：系统在索引构建（buildFreshIndex）时，遍历 .signals/ 目录并移除所有在 Markdown 文件树中无对应文件的孤儿信号记录。这处理了 Markdown 文件被外部删除而信号文件残留的情况。

### 技术效果

本方案通过双层分离架构，在不丢弃运行时动态信号的前提下，实现了知识文件的可共享性和版本控制友好性。

- 版本控制噪声消除：importance、recency、accessCount 等频繁变化的评分字段从 Markdown 前置元数据中迁出至 .gitignore 排除的信号存储后，git diff 中将不再出现仅评分数值变化的提交。团队成员的版本历史中只保留有审阅价值的知识内容变更，Pull Request 的审阅效率显著提升。
- 团队合并冲突减少：由于评分数据不再存储于共享 Markdown 文件中，不同团队成员的检索行为不会产生互不兼容的 Markdown 改写，消除了因 scoring 字段并发更新导致的合并冲突。
- 检索质量保持：迁移后的检索排序仍然使用 importance、recency、maturity 等信号进行复合评分计算，BM25 文本相关性（权重 0.6）与运行时信号（权重合计 0.4）的融合逻辑不变。知识检索的排序质量与迁移前完全一致。
- 迁移平滑性：惰性迁移策略使得历史 Markdown 文件无需一次性批量转换。旧格式文件和新格式文件可在同一上下文树中共存，系统自动识别并处理。用户无需手动干预迁移过程。
- 失败容忍性：信号存储层被设计为可选增强，其完全不可用时系统可回退至默认评分，BM25 文本检索核心功能不受影响。这确保了系统在任何磁盘或 I/O 异常下均能维持基本可用性。
- 知识资产管理增强：信号数据的独立存储使得团队可以对信号数据进行独立分析（如识别高频访问但低成熟度的知识资产、追踪知识新鲜度衰减趋势），而无需解析 Markdown 文件的版本历史。未来可在此基础上扩展信号数据的聚合统计和可视化功能。

### 风险点与实现边界

本方案基于对当前 ByteRover 项目源码的分析提出，以下为需要后续确认的风险点和实现边界：

- 惰性迁移中 Markdown 前置元数据的评分字段移除操作会触发一次版本控制变更。对于拥有大量历史文件的项目，初次迁移可能产生较大规模的批量 diff。建议在实现时提供一个可选的批量预迁移命令（如 brv context-tree migrate-signals），允许团队在受控条件下一次性完成迁移。
- 信号存储的文件格式（.signal.yaml）采用 YAML 以保持与现有 Markdown 前置元数据格式的一致性，但 YAML 的解析性能低于二进制格式。若上下文树文件数量极大（万级以上），可考虑后续升级为 MessagePack 或 SQLite 格式。当前方案优先考虑可调试性和与现有工具链的兼容性。
- 共享知识源（brv source）场景下，源项目的评分数据迁移至信号存储后，消费者的索引中将以默认评分初始化。这可能短期影响消费者侧的检索排序，直到消费者本地累积足够的访问数据。可在后续迭代中考虑提供评分快照的可选共享机制。
- 方案的核心改动集中在 SearchKnowledgeService（索引构建与访问追踪）、memory-scoring 模块（评分计算逻辑不变）、markdown-writer 模块（新增信号文件读写方法）、dream 子系统（consolidate/prune 操作中的评分读写路径）以及 derived-artifact 模块（信号文件需标记为 isDerivedArtifact，排除出版本控制同步）。其他子系统不受影响。
