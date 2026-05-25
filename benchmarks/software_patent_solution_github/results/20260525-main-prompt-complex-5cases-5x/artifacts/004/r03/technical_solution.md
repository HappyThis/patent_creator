## 技术方案

本方案针对 ByteRover 上下文树（context tree）中知识 Markdown 文件因同时承载「可共享知识内容」和「运行时动态信号」而导致的版本控制噪声、团队合并冲突和知识审阅干扰问题，提出一种双层上下文存储模型：将知识身份字段保留在共享 Markdown 文件中继续参与版本控制与团队审阅，将频繁变化的运行时评分信号迁出至本地侧车（local sidecar）文件，并通过读取融合、写入隔离、迁移兼容和归档一致性等一系列机制，实现知识可共享、可审阅与动态信号可用的兼顾。

### 技术问题分析

ByteRover 的上下文树存储结构为 .brv/context-tree/ 目录下的 Markdown 文件集合。每个知识文件的 YAML 前置元数据（frontmatter）同时包含两类字段：(1) 知识身份字段——title、summary、tags、keywords、related，用于描述知识的语义内容与关联关系；(2) 运行时评分字段——importance（重要度）、recency（新鲜度）、maturity（成熟度）、accessCount（检索命中次数）、updateCount（策展更新次数）、createdAt、updatedAt。评分字段在系统运行期间频繁变化：每次检索命中触发 accessCount 递增和 importance 加分；每次策展更新触发 updateCount 递增、importance 加分、recency 重置和 updatedAt 刷新；随时间流逝，applyDecay 函数对 importance 和 recency 施加指数衰减。这些变化导致共享 Markdown 文件持续处于脏状态，每次评分更新都产生一次 VCS diff，真正的知识内容变更被淹没在噪声中，团队并行使用时的合并冲突概率显著上升。

### 总体架构：双层上下文存储模型

本方案在现有文件系统存储架构基础上引入双层模型：(1) 共享层（Shared Layer）——即现有的 .brv/context-tree/ 目录下的 Markdown 文件，仅保留知识身份字段和正文内容，继续通过 CoGit 推送/拉取/合并参与团队协作，由 VCS 进行版本控制；(2) 本地层（Local Layer）——新增一个本地侧车文件 .brv/context-tree/.scoring.json，存储所有知识文件对应的运行时评分数据，该文件不纳入 CoGit 同步范围，不参与 VCS 版本控制，每个团队成员的工作副本中独立维护。

系统在读取知识文件时执行「读取融合」：先读取 Markdown 文件获取知识正文和身份字段，再从本地侧车文件中查询对应的评分数据，将两部分合并为完整上下文数据供上层使用。写入时执行「写入隔离」：curate 工具对知识内容的修改写入 Markdown 文件（共享层），search 工具触发的评分更新仅写入 .scoring.json 侧车文件（本地层）。读取融合接口内部维护内存缓存，减少重复的侧车文件 I/O。

### 字段分类与迁移策略

基于字段的语义特征和变化频率，将现有 Markdown frontmatter 字段分为两类：

- 共享层字段（保留在 Markdown 中）：title、summary、tags、keywords、related——这些字段描述知识的语义身份和关联拓扑，变更频率低，每次变更代表真实的知识演进，应由团队共同审阅和版本控制。
- 本地层字段（迁出至侧车文件）：importance（重要度评分，0-100）、recency（新鲜度评分，0-1）、maturity（成熟度层级：draft/validated/core）、accessCount（累积检索命中次数）、updateCount（累积策展次数）、createdAt（创建时间）、updatedAt（最后更新时间）——这些字段由运行时事件驱动变化，变化频率高，不反映知识内容本身的变化，不应触发 VCS 提交或团队合并。

Markdown 文件的 YAML frontmatter 在迁移后仅包含共享层字段。本地层字段由 .scoring.json 侧车文件以 JSON 对象形式存储，key 为知识文件的相对路径（如 "auth/jwt-tokens/refresh-flow.md"），value 为包含所有评分字段的 JSON 对象。该侧车文件本身遵循系统已有的派生工件（derived artifact）约定，被 isExcludedFromSync 谓词排除在快照、同步、推送和合并流程之外。

### 本地侧车存储设计

.scoring.json 侧车文件存储于 .brv/context-tree/ 根目录下，与 _manifest.json 同级。文件结构为单层 JSON 对象，直接以知识文件相对路径为 key，避免嵌套查询带来的额外解析开销：

{"auth/jwt-tokens/refresh-flow.md": {"importance": 72.5, "recency": 0.85, "maturity": "validated", "accessCount": 14, "updateCount": 3, "createdAt": "2025-01-15T...", "updatedAt": "2025-03-20T..."}, ...}

读写策略：(1) 全量加载——系统启动或索引重建时一次性将整个 .scoring.json 读入内存 Map，后续读取融合从此 Map 查询，避免频繁磁盘 I/O；(2) 批量回写——评分更新累积在内存中（复用现有 pendingAccessHits 缓冲机制），在索引重建（rebuildIndex）时或进程退出前，将内存 Map 整体序列化写回 .scoring.json；(3) 原子写入——回写时先写入临时文件 .scoring.json.tmp，写入成功后执行原子重命名替换，防止写入中途崩溃导致侧车文件损坏。

### 读取融合机制

读取融合机制确保上层调用方（search-knowledge-service、manifest-service、curate-tool 等）获取到的知识数据始终包含完整的评分信息，无论评分数据来自 Markdown 还是侧车文件。具体流程如下：

1. 解析目标 Markdown 文件的 YAML frontmatter，提取共享层字段（title、summary、tags、keywords、related）和正文内容。
2. 以该文件的相对路径为 key 查询内存中的侧车评分 Map。
3. 若侧车中存在该路径的评分数据，直接使用侧车数据作为 FrontmatterScoring；若侧车中不存在（新文件尚未有评分记录、或迁移前旧格式），则尝试从 Markdown frontmatter 中读取评分字段作为回退值。
4. 若 Markdown frontmatter 中也不存在评分字段，使用 applyDefaultScoring() 生成默认评分（importance=50, maturity=draft, recency=1）。
5. 将共享层字段与评分数据合并，构建完整的 ContextData 对象返回给调用方。

在内存中维护侧车评分 Map 的一个惰性更新标记：当在某次读取融合中发现 Markdown frontmatter 仍包含旧格式评分字段（即尚未完成迁移）时，系统将该条目的评分数据从 Markdown 中提取并写入内存 Map，后续回写侧车文件时一并持久化，从而在后续运行时逐步完成迁移，无需一次性全量迁移操作。

### 写入隔离与并发控制

写入隔离确保评分更新和知识内容更新流向不同的存储层，消除评分变化对共享 Markdown 的污染：

- 评分写入路径：search-knowledge-service 的 flushAccessHits 方法改为仅更新内存中的侧车评分 Map（调用 recordAccessHits 后更新 Map 对应条目），不再调用 updateScoringInContent 回写 Markdown 文件。curate-tool 的 recordCurateUpdate 同样改为仅更新内存 Map。评分数据在下一次 rebuildIndex 或进程退出时由侧车回写逻辑统一持久化到 .scoring.json。
- 知识内容写入路径：curate-tool 在生成或合并知识 Markdown 文件时，MarkdownWriter.generateContext 仅输出共享层字段（title、summary、tags、keywords、related）和正文内容，不再在 YAML frontmatter 中输出 scoring 字段。新生成的文件 frontmatter 天然处于「已迁移」状态。

并发控制方面：(1) 内存侧车 Map 的读写操作由单线程事件循环天然串行化，不存在多线程竞态；(2) .scoring.json 的磁盘回写采用「先写临时文件、再原子重命名」策略，确保写入操作的原子性；(3) 若同一项目的多个 agent 进程并行运行（agent pool 场景），每个进程独立维护自己的侧车评分 Map 和 .scoring.json 文件，评分数据在不同进程间存在一定滞后，但评分值本身即为近似信号，进程间短暂不一致不影响检索排序和知识生命周期管理的正确性——这延续了现有系统对 pendingAccessHits 在索引重建时才刷新的设计语义。

### 迁移期兼容性

方案设计为渐进式迁移，不要求全量一次性转换，确保在迁移期间系统功能完整可用：

- 回退读取：读取融合流程优先查询侧车文件，侧车不存在时回退读取 Markdown frontmatter 中的评分字段。这意味着在 .scoring.json 尚未生成或损坏时，系统仍能从 Markdown 文件中获取评分数据，评分更新功能仍然可用（继续写回 Markdown），知识检索和排序不受影响。
- 惰性迁移：读取融合过程中，若发现 Markdown frontmatter 仍包含评分字段而侧车中无对应记录，系统将该评分数据提取并写入内存 Map，标记为待回写。下一次侧车回写时，该条目的评分数据即完成迁移。此过程在常规检索和策展操作中自然触发，无需专门的迁移命令或批量扫描。
- 双向兼容的 Markdown 解析：MarkdownWriter.parseContent 和 parseFrontmatterScoring 函数保持不变，继续支持解析 frontmatter 中的评分字段。新生成的文件不再写入评分字段，旧文件中的评分字段也不会被主动删除——它们作为回退数据源继续存在，直到被惰性迁移到侧车后在下一次策展更新中自然被新的 frontmatter 输出覆盖。
- push/pull 兼容：由于评分字段已从 Markdown frontmatter 中移除（新文件）或变为冗余（旧文件），push 操作仅传输共享层字段；pull 操作接收到的 Markdown 文件不包含评分字段，本地通过侧车补充评分数据。若旧格式文件仍在仓库中，pull 操作将其拉取到本地后，读取融合流程通过回退读取正常获取评分。

### 归档、剪枝与合并的一致性保障

当知识文件被归档（archive）、恢复（restore）、合并（merge）或删除时，侧车评分数据必须同步更新以保持一致性：

- 归档操作同步：FileContextTreeArchiveService.archiveEntry 在执行归档时，除写入 .full.md 和 .stub.md、删除原始文件外，还需更新侧车评分 Map——将原始路径（如 auth/jwt-tokens/refresh-flow.md）的评分条目迁移到新路径（如 _archived/auth/jwt-tokens/refresh-flow.stub.md），保留 evicted_importance 和 evicted_at 字段。这确保归档后的桩文件（stub）在 BM25 索引中仍可基于其历史评分参与排序。
- 恢复操作同步：restoreEntry 在恢复归档文件时，将侧车评分 Map 中 _archived/ 路径下的条目迁移回原始路径，并重置 recency 为 1.0、更新 updatedAt 为当前时间。
- 合并（merge）操作同步：FileContextTreeMerger 在执行 pull 合并时，远程文件替换本地文件后，若远程 Markdown 不包含评分字段（新格式），本地侧车 Map 中的对应评分条目保持不变；若远程文件为旧格式（包含评分字段），读取融合流程在后续首次读取时通过惰性迁移将其提升到侧车。合并过程中不修改侧车文件，避免了合并冲突扩散到评分数据。
- 删除操作同步：writer-service 的 sync 方法在删除本地文件时，同步删除侧车评分 Map 中的对应条目。当本地文件因远程删除而被清理时（preserveLocalFiles=false），侧车记录也被移除，防止孤儿评分条目累积。
- 失败隔离：若侧车 Map 更新过程中发生 I/O 错误（如磁盘满），评分更新静默失败，不影响归档/恢复/合并操作本身的主流程执行。下一次索引重建或进程重启时，系统从现有 Markdown 文件集合重新扫描，侧车 Map 中不再对应任何现有文件的孤儿条目被自动清理。

### 技术效果

本方案的技术效果体现在以下几个方面：

- 降低 VCS 噪声：评分字段迁出后，Markdown 文件的变更仅反映真实的知识内容演进，每次 curate 操作产生的 diff 只包含知识正文和身份字段的变化，消除了检索命中、时间衰减等运行时事件引发的虚假变更，VCS 提交历史可读性显著提升。
- 减少合并冲突：团队并行 curate 时，由于评分字段不再写入 Markdown，两个成员对同一文件不同部分的知识内容修改可以正常合并，不会因评分字段的并发更新产生额外冲突。
- 保留动态信号价值：所有评分数据仍然可用——importance 和 maturity 支持知识生命周期管理（自动归档低重要度条目），recency 和 accessCount 支持检索排序优化，BM25 复合评分中的重要性权重和成熟度层级提升继续生效。
- 渐进迁移零中断：惰性迁移机制使系统在升级后立即可用，无需停机迁移或批量转换，旧格式文件和新格式文件共存期间所有功能正常工作。
- 失败隔离：侧车文件的损坏或丢失不会导致系统不可用，读取融合自动回退到 Markdown frontmatter 中的评分字段，评分计算的近似性特征使得短暂的数据丢失在下次运行时自然恢复。

### 风险与待确认问题

待确认风险与后续工作：

- 侧车文件在多 agent 进程并行时的评分数据一致性：各进程独立维护侧车，评分值可能存在进程间滞后，需确认当前 agent pool 场景中不同进程是否共享评分预期。
- 大项目侧车文件规模：当知识文件数量达到数千个时，全量 JSON 加载和序列化的内存与 I/O 开销需要评估，可预留按目录分片存储的扩展点。
- 现有测试用例适配：search-knowledge-service 和 curate-tool 的单元测试中涉及 updateScoringInContent 调用的用例需要更新，改为验证侧车 Map 操作。
