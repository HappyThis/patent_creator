## 技术方案

本方案提出一种面向团队共享知识库的静态知识内容与动态运行时信号分离存储及融合检索的方法。核心思路是将知识文件的 YAML 前置元数据（frontmatter）拆分为两个独立存储层：共享 Markdown 文件仅保留可审阅、可版本对比的静态知识字段；而搜索命中计数、重要性评分、新鲜度、成熟度等级等频繁变化的动态信号，迁出至本机独立的评分叠加层（Scoring Overlay）中。检索时，系统将两层数据透明融合，使排序和过滤效果与分离前一致，同时消除动态信号对版本控制的污染。

### 技术问题分析

当前 ByteRover 系统的知识库以 Markdown 文件（.context.md）存储在 .brv/context-tree/ 目录下，每个文件的 YAML 前置元数据中同时包含两类信息。（1）静态知识字段：title、summary、tags、keywords、related（关联路径），这些是人类审阅和版本对比的核心内容。（2）动态评分字段：accessCount（搜索命中次数）、importance（重要性评分 0-100）、recency（新鲜度 0-1）、maturity（成熟度等级 draft/validated/core）、updateCount（策展更新次数）、createdAt/updatedAt（时间戳）。系统在每次搜索命中时通过 flushAccessHits 方法将累积的命中计数批量写回 Markdown 文件，在每次策展更新时通过 recordCurateUpdate 更新评分。这导致：（a）即使知识内容未变，每次搜索都可能触发文件写入，污染 Git 变更历史；（b）不同团队成员本机产生的评分变化在 CoGit 拉取/合并时产生频繁冲突；（c）真正的知识内容变更被大量评分噪声淹没，审阅者难以判断哪些变更需要关注。

### 三层存储模型

方案在现有架构基础上引入三层存储模型，在不破坏现有检索、排序、策展、归档流程的前提下实现动静分离。

第一层：共享知识文件层（Shared Markdown）。即现有的 .context.md 文件，继续存储在 .brv/context-tree/ 下，通过 CoGit 在团队间同步。该层仅保留以下静态字段：title、summary、tags、keywords、related，以及正文内容（## Raw Concept、## Narrative、## Facts、代码片段等）。所有动态评分字段（accessCount、importance、recency、maturity、updateCount、createdAt、updatedAt）不再写入该层。该层文件的 YAML 前置元数据格式保持兼容，仅字段集缩小。

第二层：评分叠加层（Scoring Overlay）。每台机器本地维护一个独立的评分数据库，以知识文件的相对路径为键，存储该文件对应的动态评分记录。实现上可复用现有 FrontmatterScoring 数据结构（accessCount、importance、recency、maturity、updateCount、createdAt、updatedAt），存储介质可选用 SQLite 数据库文件或 JSON 文件，放置于 .brv/ 目录下（如 .brv/scoring-overlay.db），不纳入版本控制（通过 .gitignore 排除）。不同机器的叠加层完全独立，互不干扰。

第三层：衍生制品层（Derived Artifacts）。即现有的 _index.md、_manifest.json、_archived/ 等，由系统自动生成，已通过 isExcludedFromSync 排除在同步范围外，维持现有机制不变。

### 字段分类与迁移策略

对现有 ContextData 和 FrontmatterScoring 接口中的字段进行如下分类。

| 字段 | 分类 | 存储位置 | 说明 |
| --- | --- | --- | --- |
| title | 静态 | 共享 Markdown | 知识条目标题，可审阅 |
| summary | 静态 | 共享 Markdown | 语义摘要，可审阅 |
| tags | 静态 | 共享 Markdown | 分类标签 |
| keywords | 静态 | 共享 Markdown | 搜索关键词 |
| related | 静态 | 共享 Markdown | 关联路径 |
| 正文内容 | 静态 | 共享 Markdown | RawConcept、Narrative、Facts、Snippets |
| accessCount | 动态 | 评分叠加层 | 搜索命中次数，本机变化频繁 |
| importance | 动态 | 评分叠加层 | 重要性评分，随访问和更新变化 |
| recency | 动态 | 评分叠加层 | 新鲜度，随时间衰减 |
| maturity | 动态 | 评分叠加层 | 成熟度等级，依赖本机评分阈值判断 |
| updateCount | 动态 | 评分叠加层 | 策展更新次数 |
| createdAt | 动态 | 评分叠加层 | 创建时间戳 |
| updatedAt | 动态 | 评分叠加层 | 最后更新时间戳 |

迁移策略采用渐进式兼容方案。新写入的知识文件（ADD、UPDATE、UPSERT 操作）不再将 scoring 字段写入 Markdown 的 YAML 前置元数据，而是将评分数据写入评分叠加层。已存在的旧文件不做批量回写迁移；读取时优先从评分叠加层获取评分数据，若叠加层无记录则回退读取 Markdown 文件中的内联评分字段（parseFrontmatterScoring），并将读取到的评分数据异步回填至叠加层。这样旧文件无需主动改写即可逐步过渡到新存储模型。

### 评分叠加层设计

评分叠加层是本方案的核心新增组件。它记录每条知识路径到其动态评分数据的映射，并在搜索索引构建时与共享知识文件中的静态字段融合。

数据结构。叠加层中每条记录以知识文件的相对路径（如 auth/jwt/refresh-token-rotation.md）为主键，值类型为 FrontmatterScoring，包含 accessCount、importance、recency、maturity、updateCount、createdAt、updatedAt 七个字段。该结构与现有 memory-scoring.ts 中的评分函数完全兼容，compoundScore、applyDecay、determineTier、recordAccessHits、recordCurateUpdate、applyDefaultScoring、mergeScoring 等函数无需修改即可复用。

存储介质。推荐使用 SQLite 数据库文件（.brv/scoring-overlay.db），原因：（1）单文件存储，无需额外服务进程；（2）支持原子事务，保证并发安全；（3）支持高效的批量读写，适合 flushAccessHits 的批量更新模式；（4）数据库文件通过 .gitignore 排除，不影响版本控制。备选方案为 JSON 文件，适合评分数据量较小的场景。

关键操作接口。（1）getScoring(path): 读取指定路径的评分数据，叠加层无记录时返回 undefined。（2）upsertScoring(path, scoring): 写入或更新评分数据，使用 INSERT OR REPLACE 语义。（3）deleteScoring(path): 删除指定路径的评分记录，用于知识文件被删除或归档时清理。（4）batchGetScoring(paths): 批量读取，用于索引构建时一次性获取所有评分数据。（5）batchUpsertScoring(entries): 批量写入，用于 flushAccessHits 的批量回写。

### 读取融合机制

读取融合是保证检索排序效果与分离前一致的关键机制。系统在构建搜索索引（buildIndex）时，对每个知识文件执行以下融合步骤。

步骤一：从共享 Markdown 文件读取静态内容。解析 YAML 前置元数据获取 title、summary、tags、keywords、related，并解析正文获取全文内容用于 BM25 索引。此步骤不再调用 parseFrontmatterScoring 从 Markdown 中提取 scoring 字段。

步骤二：从评分叠加层批量读取动态评分数据。以所有待索引文件的相对路径列表为输入，调用 batchGetScoring 一次性获取评分映射。若叠加层中某文件无记录（新文件或尚未迁移的旧文件），则回退读取该 Markdown 文件的 YAML 前置元数据中的内联 scoring 字段，读取成功后将该评分数据异步写入叠加层（回填），下次即可直接从叠加层获取。

步骤三：融合构建 IndexedDocument。将静态字段（title、content、path 等）与动态评分字段（scoring）组合为 IndexedDocument 对象，交给 MiniSearch 建立 BM25 索引。后续 compoundScore 计算、propagateScoresToParents 传播、tier 过滤等逻辑完全不需修改，因为它们都从 IndexedDocument.scoring 读取数据，与数据来源无关。

### 迁移期兼容策略

迁移期兼容策略确保系统在引入评分叠加层后，对已有知识库和现有工具链完全向后兼容。

（1）读取兼容。MarkdownWriter.parseContent 和 parseFrontmatterScoring 保持不变，仍能解析旧文件中的内联 scoring 字段。新增的 ScoringOverlayService.getScoring 优先返回叠加层数据，叠加层无数据时回退到 Markdown 内联解析，确保新旧文件均可正常检索排序。

（2）写入兼容。MarkdownWriter.generateContext 在生成 Markdown 内容时，若传入的 ContextData.scoring 非空，仍将其序列化为 YAML 前置元数据中的 scoring 字段（保持 generateFrontmatter 现有行为不变）。同时，策展工具（curate-tool）在写入文件后，额外调用 ScoringOverlayService.upsertScoring 将评分数据写入叠加层。这样既兼容仍使用内联评分的旧版客户端，又确保新版客户端能从叠加层获取评分。

（3）同步兼容。CoGit 同步（push/pull/merge）仅传输 Markdown 文件，不传输评分叠加层。现有 file-context-tree-merger.ts 的合并逻辑不受影响，因为合并仅比较 Markdown 文件内容（包括前置元数据中的静态字段），不涉及评分字段。迁移后 Markdown 文件中的 scoring 字段逐步消失，合并冲突概率降低。（4）索引重建兼容。SearchKnowledgeService 在构建索引时通过 ScoringOverlayService 获取评分数据，叠加层无数据时回退到 Markdown 内联评分。随着旧文件被策展更新，评分数据逐步迁移至叠加层，最终 Markdown 文件不再包含评分字段。

### 并发更新处理

评分叠加层是本机独占的，不需要处理跨机器的并发冲突。需要处理的并发场景集中在本机多 Agent 并行执行时的叠加层访问，以及评分更新与 CoGit 合并的时序问题。

（1）本机并发写入。SearchKnowledgeService 在多 Agent 并行执行时可能同时触发 flushAccessHits。方案使用 SQLite 的 WAL（Write-Ahead Logging）模式和事务机制保证写入原子性。batchUpsertScoring 操作在单个事务中完成，利用 INSERT OR REPLACE 语义避免重复键冲突。读取操作（getScoring、batchGetScoring）在 WAL 模式下不会被写入阻塞。（2）pendingAccessHits 内存缓冲区。现有的 pendingAccessHits Map 保持不变，累积访问命中数。flushAccessHits 清空缓冲区并将命中数写入叠加层（而非写入 Markdown 文件），同样采用 best-effort 策略：单条写入失败时静默丢弃，不影响检索主流程。（3）CoGit 合并后的评分处理。当 CoGit pull/merge 导致本地 Markdown 文件被新增、修改或删除时，评分叠加层需要同步调整：新增文件使用 applyDefaultScoring 初始化默认评分；修改文件保留叠加层中已有评分（不改写，除非文件内容发生实质性变更导致评分需要重新计算）；删除文件对应的叠加层记录在下次归档/清理时移除。

### 失败隔离与降级

评分叠加层被设计为可丢失的缓存层，其故障不应影响知识检索的核心功能。

（1）叠加层不可用时的降级路径。若 SQLite 数据库文件损坏、被误删或无法打开，SearchKnowledgeService 在索引构建时检测到叠加层不可用，自动降级为从 Markdown 文件的 YAML 前置元数据中读取内联评分字段（即现有的 parseFrontmatterScoring 路径）。降级后的检索排序效果与完全不使用叠加层时一致。（2）写入失败处理。flushAccessHits 中原先写入 Markdown 文件的逻辑改为写入叠加层，写入失败时静默丢弃本次命中计数（best-effort），不影响检索结果返回。累积的命中计数丢失仅影响后续检索排序的精度，属于可接受的降级。（3）叠加层恢复。叠加层文件丢失后，系统在下次索引构建时通过回退读取 Markdown 内联评分自动重建叠加层数据。若 Markdown 文件中已无内联评分（已完全迁移），则使用 applyDefaultScoring 初始化所有条目的默认评分，评分数据从零开始累积。

### 归档、剪枝与合并时的清理一致性

当知识文件被归档、删除或合并时，评分叠加层中的对应记录需要同步清理，防止叠加层无限膨胀，并避免已删除文件的评分数据被错误应用于新文件。

（1）归档清理。当 IContextTreeArchiveService.archiveEntry 将知识文件归档为 _archived/ 目录下的 .stub.md + .full.md 时，同步调用 ScoringOverlayService.upsertScoring 将原文件的评分数据迁移到归档桩（stub）路径下，然后调用 deleteScoring 删除原路径的记录。这样归档桩在搜索时仍能利用历史评分数据参与排序（stub 本身是搜索可见的），同时原路径的评分记录被释放。（2）删除清理。当 curate-tool 执行 DELETE 操作删除知识文件时，同步调用 ScoringOverlayService.deleteScoring 移除对应评分记录。（3）合并清理。当 curate-tool 执行 MERGE 操作将源文件合并到目标文件后删除源文件时，调用 mergeScoring 将源文件和目标文件的评分数据合并（取 importance/recency 最大值，accessCount/updateCount 求和，maturity 取较高等级，createdAt 取较早时间），将合并结果通过 upsertScoring 写入目标路径，再 deleteScoring 删除源路径记录。（4）定期孤儿清理。系统在索引构建完成后，遍历叠加层中的所有路径，检查对应 Markdown 文件是否仍存在于 context tree 中。若文件已不存在（被外部删除或 CoGit 同步移除），则从叠加层中移除对应记录。此清理步骤为 best-effort，失败不影响检索。

### 技术效果分析

本方案通过将动态评分信号从共享 Markdown 文件迁移至本机评分叠加层，带来以下技术效果。

（1）消除版本控制噪声。搜索访问不再触发 Markdown 文件写入，Git 工作区不再因日常检索操作变脏。团队成员的 CoGit 提交历史中只包含有意义的知识内容变更（新增、修改、删除知识条目），评分波动不再产生虚假的 diff 和合并冲突。（2）保留排序能力。评分数据虽迁出 Markdown，但在索引构建时通过叠加层透明融合，compoundScore、tier boost、propagateScoresToParents 等排序逻辑的输出与迁移前完全一致。搜索结果的排序质量不受影响。（3）降低合并冲突率。CoGit 合并时仅比较 Markdown 文件内容。随着内联 scoring 字段逐步消失，两个团队成员同时修改同一知识条目时，冲突仅发生在真正的知识内容层面，评分字段不再成为冲突源。（4）本机评分独立性。不同机器维护独立的评分叠加层，每台机器的评分反映各自的使用模式（搜索频率、策展操作），互不干扰。这比共享评分更合理：团队中频繁检索某主题的成员会在本机看到该主题排序更靠前，而不影响其他成员的排序偏好。（5）可丢失性带来的运维简化。评分叠加层是缓存性质的，丢失后可自动重建（回退到 Markdown 内联评分或默认评分），不需要备份、不需要同步、不需要版本控制。这降低了运维复杂度。（6）归档一致性。归档、删除、合并操作同步维护叠加层，保证评分数据与知识文件的完整生命周期一致，不会出现已删除文件的评分数据残留。

### 风险与待确认事项

以下为实施中需要关注的风险点和待确认事项。

（1）渐进迁移期间的双写开销。在新旧兼容期内，策展写入操作需要同时更新 Markdown 文件（含内联 scoring）和评分叠加层，存在短暂的双写开销。待团队确认迁移窗口期后，可通过配置开关关闭 Markdown 文件中的 scoring 字段写入。（2）叠加层与 Markdown 内容的一致性。若用户在外部直接编辑 Markdown 文件的 YAML 前置元数据修改评分字段，叠加层不会自动感知。需要确认是否需要在索引构建时增加 checksum 或时间戳比对来检测外部修改。（3）多项目/多工作区的叠加层管理。当前方案假设 .brv/scoring-overlay.db 位于项目根目录。若同一台机器上有多个项目副本（如多个克隆），每个副本独立维护叠加层，评分数据不会跨副本共享。这通常是期望行为，但需要确认。（4）SQLite 依赖。引入 SQLite 作为新依赖，需要评估目标运行环境（如沙箱、容器）是否支持原生 SQLite 模块。备选 JSON 文件方案可消除此依赖，但并发性能较差。（5）叠加层数据增长。随着知识库规模增长，叠加层中可能积累大量已删除文件的孤儿记录。需要确认定期孤儿清理的频率和触发时机（当前建议在每次索引构建完成后执行）。
