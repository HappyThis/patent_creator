## 技术方案

本方案针对 ByteRover 上下文树（context tree）中知识文件的可共享性与运行时动态信号之间的矛盾，提出一种静态知识与动态评分的双层分离存储架构。核心思路是将 Markdown 知识文件中的 YAML 前置元数据（frontmatter）拆分为两层：第一层保留可审阅、可版本控制的静态知识字段，继续以 Markdown 文件形式存放在 .brv/context-tree/ 目录下接受 Git 管理；第二层将频繁变化的动态评分信号迁出至独立的评分侧车文件（_scoring.json），该文件不纳入版本控制。系统在检索、排序、归档、剪枝等所有需要评分的环节，通过融合层统一读取两层数据。

### 静态知识与动态信号的字段划分

当前系统将以下字段统一写入每个知识文件（如 context.md、_index.md）的 YAML frontmatter 中，导致任何检索命中或整理操作都会触发文件重写和版本控制变更。

- 静态知识字段：title、summary、tags、keywords、related（关系链接）、narrative、rawConcept、facts、snippets——这些字段表达的是知识内容本身，由人类或 Agent 通过 curate/expand_knowledge 等工具主动写入，适合通过 Git 进行版本追踪和团队审阅。
- 动态评分字段：importance（重要性分数）、recency（新鲜度）、accessCount（检索命中次数）、updateCount（整理更新次数）、maturity（成熟度等级：draft/validated/core）、createdAt（创建时间）、updatedAt（最后更新时间）——这些字段由系统在每次检索命中或整理操作后自动更新，变化频率远高于知识内容本身。

### 评分侧车文件的设计与位置

将动态评分字段从 Markdown frontmatter 迁出至评分侧车文件 _scoring.json，该文件存放于 .brv/context-tree/ 根目录下，通过 .gitignore 排除出版本控制。_scoring.json 以知识文件的相对路径为键，值为对应的评分对象，结构如下：

- 每个入口包含：importance（0-100 浮点）、recency（0-1 浮点）、accessCount（整数）、updateCount（整数）、maturity（'draft' | 'validated' | 'core'）、createdAt（ISO 时间戳）、updatedAt（ISO 时间戳）。
- 键使用知识文件相对于 context-tree 根目录的 Unix 风格路径（如 auth/jwt-tokens/refresh-flow.md）。
- 对于 _index.md 类汇总文件，按相同方式存储评分，键为汇总文件的相对路径加 /_index.md 后缀。

### 评分融合层与读写分离

系统在检索、排序、清单构建（manifest）、归档、剪枝等所有需要读取评分的环节，通过统一的评分融合层获取知识条目的完整评分。融合层的读取逻辑如下：

1. 优先读取 _scoring.json 中该路径的评分记录；
2. 若 _scoring.json 中不存在对应记录（文件尚未迁移或为新创建），则回退到从 Markdown 文件的 frontmatter 中解析评分字段（向后兼容）；
3. 融合层统一返回 FrontmatterScoring 结构，调用方无需关心数据来源。

对于评分写入路径，系统通过评分写入层统一管理：

- 所有动态评分的更新（recordAccessHits、recordCurateUpdate、applyDecay 等）统一写入 _scoring.json，不再回写 Markdown 文件的 frontmatter 中的评分字段；
- 静态知识的写入（title、summary、tags、keywords、related 等）仍通过 MarkdownWriter.generateContext 写入 Markdown 文件的 frontmatter，但不再附带评分字段；
- Markdown 文件的 frontmatter 不再包含 importance、recency、accessCount、updateCount、maturity、createdAt、updatedAt 七个动态字段。

### 迁移期兼容机制

为了保证迁移期间系统正常运行，系统采用渐进式迁移策略，无需停机或一次性批量转换。

- 自迁移启用标记：系统启动时检查 .brv/context-tree/_scoring.json 是否存在。若不存在，说明尚未开始迁移，所有读写保持原有行为。一旦 _scoring.json 被创建（无论是否包含数据），系统即切换至新写入模式。
- 首次触达即迁出：当任何操作需要更新某个知识文件的评分时，先检查 _scoring.json 中是否存在该条目的记录。若不存在，从该文件 frontmatter 解析现有评分值（若存在），写入 _scoring.json，并从 frontmatter 中剥离评分字段——此即"触达迁移"。对于从未被检索或整理触及的文件，其 frontmatter 中的评分字段保持不变，直到被首次触达。
- 向后兼容读取：在读取评分时，融合层同时查询 _scoring.json 和 frontmatter。若 _scoring.json 已有记录则优先使用；前端仍按原有格式解析，保证未迁移条目正常工作。
- 迁移完成判定：系统可提供后台巡检任务，统计 _scoring.json 覆盖率。当所有现存知识文件的评分均已迁出后，可标记迁移完成，后续创建新文件时直接从初始默认评分写入 _scoring.json。

### 并发更新与失败隔离

由于评分更新可能由多个并发操作触发（并行检索、同时进行的整理任务、后台剪枝等），系统需要保证 _scoring.json 的并发安全。

- 文件级写锁：对 _scoring.json 的读写操作使用基于文件系统的排他锁（或进程内互斥锁），确保同一时刻只有一个写入者修改该文件。读取操作在写入锁持有时等待或读取缓存副本。
- 批量写入与合并：采用与当前 flushAccessHits 相同的批量写回策略：检索过程中累积的评分变更暂存于内存中的 pendingAccessHits 映射表中，在索引重建时一次性读取 _scoring.json、合并所有累积变更、再原子写回。这避免了每次检索都触发磁盘写入。
- 写入失败隔离：写入 _scoring.json 失败时不影响知识文件本身的操作。评分更新采用尽力而为（best-effort）策略——若评分写回失败，仅丢弃本次评分增量，不阻塞知识检索或整理的主流程。这与当前 flushAccessHits 的 try-catch 语义一致。
- 评分数据损坏恢复：若 _scoring.json 文件损坏（JSON 解析失败），系统回退至从各 Markdown 文件 frontmatter 读取评分字段的兼容模式，并异步重建 _scoring.json。

### 归档、剪枝、合并与删除时的清理一致性

知识文件被归档、剪枝、合并或删除时，其对应的评分侧车记录必须同步清理，防止 _scoring.json 中残留孤立条目。

具体清理规则如下：

- 归档（archive）：archiveEntry 将原始文件移动至 _archived/ 并创建 .stub.md 和 .full.md。此时从 _scoring.json 中删除原路径的评分记录，因为归档文件的评分已在 stub 的 evicted_importance 中冻结，不再需要动态更新。归档文件的评分信息通过 ArchiveStubFrontmatter 中的 evicted_importance 字段保留。
- 剪枝（prune）：prune 操作在执行 archiveEntry 时触发上述归档清理。若剪枝决策为 KEEP（保留），则只更新 _scoring.json 中的 updatedAt，不删除记录。
- 合并（consolidate/merge）：MERGE 操作将源文件内容合并入目标文件并删除源文件。合并时，系统调用 mergeScoring 计算合并后的评分（取最大 importance、最大 recency、求和 accessCount/updateCount），写入 _scoring.json 中目标文件的记录，同时删除 _scoring.json 中源文件的记录。合并操作中对 Markdown frontmatter 的 rewriting 只处理静态字段，不再处理评分字段。
- 删除（DELETE）：curate 工具的 DELETE 操作在调用 DirectoryManager.deleteFile 删除知识文件后，同步删除 _scoring.json 中对应路径的评分记录。
- 跨引用清理：通过 related 字段关联的知识条目被删除时，其评分记录也被删除。关系链接本身在 Markdown frontmatter 中维护，不受评分侧车清理的影响。
- 孤立条目巡检：系统可提供定期巡检任务，扫描 _scoring.json 中所有键，检查对应的知识文件是否仍存在于 context-tree 目录中，对已不存在的文件对应的评分记录进行惰性清理。

### 清单与归档服务适配

清单构建服务（FileContextTreeManifestService）在构建 _manifest.json 时，通过评分融合层获取每个条目的 importance 和 maturity，而非直接解析 Markdown frontmatter。ManifestEntry 中保留 importance 字段，其值来自融合后的评分。清单的 source_fingerprint 计算（基于文件 mtime+size 的哈希）不受影响——因为评分变更不再修改 Markdown 文件，mtime 只在知识内容真正变更时才更新，因此清单可以更准确地判定哪些内容发生了变化。

归档服务（FileContextTreeArchiveService）的 findArchiveCandidates 方法通过评分融合层读取每个条目的 importance 和 maturity，与阈值 ARCHIVE_IMPORTANCE_THRESHOLD（35）比较来判断是否应归档。archiveEntry 在创建 stub 时从融合层获取当前 importance，写入 evicted_importance 以保留归档前的评分快照，随后同步清理 _scoring.json 中的记录。

### 技术效果总结

本方案通过双层分离存储架构，在保持知识文件可共享、可审阅的前提下，继续利用运行时动态信号，带来以下技术效果：

- 版本控制干净：知识文件的 mtime 和内容哈希仅在知识内容真正变更时才发生变化，不再因为检索命中、评分衰减、成熟度升/降级等系统自动操作而反复重写。这消除了因评分波动导致的 Git diff 噪声和团队合并冲突。
- 可审阅性保持：团队仍可通过 Git diff、PR review 等方式审阅知识内容的变化（标题、摘要、标签、关键词、关系链接、正文等）。评分相关的自动变更不再污染审阅视图。
- 检索质量不变：所有基于评分的排序（BM25 复合评分）、清单通道分配（按 importance 排序）、归档候选筛选（按 importance 阈值）等功能完全保留，只是评分数据的物理存储位置发生变化。
- 迁移平滑：采用触达迁移策略，无需停机，旧文件在新写入发生前继续正常工作。迁移过程对用户透明。
- 故障隔离：评分侧车文件损坏或写入失败时，系统自动回退至 frontmatter 评分读取，不影响知识检索和整理的核心功能。
- 清理一致性：归档、剪枝、合并、删除等操作同步清理评分侧车记录，避免孤立数据和存储膨胀。
