## 技术方案

本方案针对 ByteRover 上下文树（Context Tree）中知识文件 Markdown 前端元数据（YAML frontmatter）同时承载"可共享知识元数据"和"运行时动态信号"导致的版本控制噪声与团队协作冲突问题，提出一种双存储分存方案：将知识的结构化元数据保留在共享 Markdown 中，将频繁变化的运行时动态信号迁移至本机旁车数据库（Runtime Sidecar），并通过读取融合层在检索排序和上下文注入时透明合并两路数据。

### 技术问题定位

ByteRover 的上下文树以 Markdown 文件（context.md）作为知识持久化载体，文件头部 YAML frontmatter 中同时存储两类信息：（1）结构化知识元数据——标题（title）、标签（tags）、关键词（keywords）、关联路径（related）、摘要（summary）、成熟度等级（maturity）等，这些是团队审阅和版本比较的核心对象；（2）运行时动态信号——重要性分数（importance）、访问计数（accessCount）、更新计数（updateCount）、近因分数（recency）、更新时间戳（updatedAt）等，这些信号随每次搜索命中、curate 操作或时间衰减而频繁变化。

当前架构中，FileContextTreeManifestService 在构建 _manifest.json 时直接读取每个 context.md 的 frontmatter scoring 字段以获得 importance 用于通道分配；SearchKnowledgeService 在构建 BM25 索引时读取每个 context.md 的 frontmatter scoring 传入 compoundScore() 以计算最终排序分数；记忆评分引擎（memory-scoring.ts）在 recordAccessHits() 和 recordCurateUpdate() 后将更新后的 scoring 通过 updateScoringInContent() 写回 Markdown 文件。

这导致每次搜索或 curate 操作都可能产生对共享 Markdown 文件的写入，使版本控制系统（如 Git）的状态频繁变脏；团队成员拉取远程更新时，动态信号字段成为合并冲突高发区；真正的知识内容变更被大量 scoring 数值变化淹没，审阅者难以高效判断哪些 commit 包含实质性知识更新。

### 核心技术方案：双存储分存架构

本方案的核心思想是将上下文树的知识持久化层拆分为两层：共享 Markdown 层（Shared Markdown Layer）负责可审阅、可版本控制的结构化知识；本机运行时旁车层（Runtime Sidecar Layer）负责频繁变化、不适合版本控制的动态信号。

两层通过以知识条目路径（context.md 在上下文树中的相对路径）为主键进行关联。读取路径（搜索排序、manifest 构建、上下文注入）通过读取融合层同时查询两层并将结果合并；写入路径根据信号类型分别路由到 Markdown 层（知识内容变更）或旁车层（动态信号更新）。

这种设计与 ByteRover 现有的派生产物（Derived Artifact）机制一脉相承：系统已有 _index.md、_manifest.json、.snapshot.json 等文件通过 isDerivedArtifact() 和 isExcludedFromSync() 被排除在快照跟踪和 CoGit 同步之外。Runtime Sidecar 遵循同样原则——它是本机派生的动态视图，不应纳入共享快照或团队同步。

### 字段分类与分存策略

以下基于 ByteRover 当前 FrontmatterScoring 接口（定义于 markdown-writer.ts）和 ContextData 接口，对字段进行明确分类：

保留在共享 Markdown frontmatter 中的字段（继续版本控制、团队审阅）：title（知识条目标题）、summary（摘要文本）、tags（标签列表）、keywords（关键词列表）、related（关联路径列表）、maturity（成熟度等级：draft / validated / core）。其中 maturity 从原先由 importance 分数自动推导改为由人工 curation 流程显式设定，使其成为团队对知识质量的一致判断。

迁移至 Runtime Sidecar 的字段（本机存储，不纳入版本控制）：importance（重要性分数，0-100，受搜索命中加分和 curate 更新加分影响）、recency（近因分数，0-1，随时间指数衰减）、accessCount（搜索命中累计次数）、updateCount（curate 更新累计次数）、updatedAt（最近一次 curate 更新时间戳）。createdAt（创建时间戳）保留在 Markdown 中不迁移，因为它是一次性写入的知识创建时间，不会频繁变化。

Sidecar 中额外新增的运行时索引数据：lastAccessAt（最近一次搜索命中时间戳，用于衰减计算）、decayComputedAt（最近一次衰减计算时间戳，避免每次读取都重新计算）、bm25TermFreqCache（可选，用于加速 BM25 索引重建）。

### Runtime Sidecar 存储设计

Runtime Sidecar 以单个 JSON 文件形式存储于 .brv/context-tree/ 目录下，文件命名为 .runtime-signals.json，并在 .gitignore（由 CONTEXT_TREE_GITIGNORE_PATTERNS 管理）中增加该文件名的忽略规则，确保不会被 Git 跟踪或 CoGit 同步。

文件内部结构为以知识条目相对路径为键的映射表：{ "domain/subtopic/context.md": { "importance": 72.5, "recency": 0.88, "accessCount": 15, "updateCount": 3, "updatedAt": "2026-01-15T10:30:00Z", "lastAccessAt": "2026-01-20T14:22:00Z", "decayComputedAt": "2026-01-20T14:22:00Z" }, ... }。每个条目仅存储该知识文件的运行时信号，不重复存储 Markdown 中已有的 title、tags 等内容。

写入语义：每次 recordAccessHits() 或 recordCurateUpdate() 操作仅更新 Sidecar 中对应路径的条目，不涉及 Markdown 文件写入。当路径在 Sidecar 中不存在时自动创建默认条目（importance: 50, recency: 1, accessCount: 0, updateCount: 0）。写入采用先写临时文件再原子重命名（atomic rename）的策略，避免并发写入导致文件损坏。

读取语义：SearchKnowledgeService 和 FileContextTreeManifestService 在需要 scoring 数据时，通过 ScoringProvider 接口读取，该接口内部先查 Sidecar 映射表，命中则返回；未命中时回退到 Markdown frontmatter 中的 scoring 字段（迁移兼容），若均无数据则返回默认值。读取为纯内存操作——Sidecar 文件在服务启动时一次性加载到 Map 中，后续读取为零 I/O。

持久化触发时机：每次更新操作后将内存中的映射表异步序列化写入磁盘；同时设置一个去抖动定时器（debounce，默认 5 秒），合并短时间内的多次更新为一次磁盘写入。进程退出时通过 shutdown handler 强制刷盘。

### 读取融合机制

引入 IScoringProvider 接口作为 scoring 数据的统一访问层，替代当前各处直接调用 parseFrontmatterScoring() 的模式。接口定义如下：

- getScoring(path: string): FrontmatterScoring —— 返回合并后的 scoring 数据
- recordAccess(path: string, hitCount: number): void —— 记录搜索命中，更新 Sidecar
- recordUpdate(path: string): void —— 记录 curate 更新，更新 Sidecar
- applyDecay(path: string): void —— 对指定路径应用时间衰减
- getBatchScoring(paths: string[]): Map<string, FrontmatterScoring> —— 批量读取

ScoringProvider 的实现逻辑：读取时，先从内存中的 Sidecar 映射表获取动态信号（importance、recency、accessCount、updateCount、updatedAt、lastAccessAt）；若 Sidecar 中不存在该路径的条目，回退读取 Markdown frontmatter 中的 scoring 字段（兼容未迁移或迁移中的文件）；从 Markdown frontmatter 中读取 maturity（成熟度等级）；将两路数据合并为一个完整的 FrontmatterScoring 对象返回给调用方。

调用方修改点：SearchKnowledgeService.buildIndex() 中，将当前对每个文档调用 parseFrontmatterScoring(content) 替换为 scoringProvider.getScoring(path)。同理，FileContextTreeManifestService.scanForManifest() 中的 parseFrontmatterScoring(content) 替换为 scoringProvider.getScoring(relativePath)。memory-scoring.ts 中的 recordAccessHits() 和 recordCurateUpdate() 改为调用 scoringProvider.recordAccess() 和 scoringProvider.recordUpdate()，不再通过 updateScoringInContent() 写回 Markdown。

compoundScore() 函数本身保持不变——它接收 FrontmatterScoring 作为输入，不关心数据来源。这使得现有搜索排序逻辑完全不受影响。

### 迁移期兼容策略

迁移采用渐进式、非破坏性策略，确保新老数据共存期间系统正常运行。

首次启动迁移（Lazy Migration）：ScoringProvider 第一次读取某个路径的 scoring 时，若 Sidecar 中不存在该条目但 Markdown frontmatter 中存在 scoring 字段，则自动将 frontmatter 中的 importance、recency、accessCount、updateCount、updatedAt 提取并写入 Sidecar，同时在内存中缓存。此操作不修改 Markdown 文件——原 frontmatter scoring 字段保留作为回退数据源。

可选清理（Optional Pruning）：提供独立的 CLI 命令（如 brv context-tree migrate-scoring）批量扫描所有 context.md，将 frontmatter 中的 scoring 字段迁移至 Sidecar 后，从 frontmatter 中移除 scoring 相关字段（importance、recency、accessCount、updateCount）。这步由团队在合适的时机手动执行，确保在确认 Sidecar 正常工作后再清理 Markdown。

回退兼容：若 Sidecar 文件被删除或损坏，ScoringProvider 自动回退到 Markdown frontmatter 读取。搜索排序和 manifest 构建的质量仅轻微下降（因为不再有 accessCount 和 updateCount 的累计效应），但核心功能不受影响。团队仍可通过删除 .runtime-signals.json 并重新运行系统来从零重建 Sidecar。

### 并发更新与故障隔离

ByteRover 的 Agent Pool 允许同一项目的多个 agent 进程并发执行 curate 和 query 任务（AGENT_MAX_CONCURRENT_TASKS 默认为 5，AGENT_POOL_MAX_SIZE 默认为 10）。每个 agent 进程拥有独立的 ScoringProvider 实例和内存中的 Sidecar 映射表，这引入了并发写入冲突的风险。

并发控制采用"乐观写入 + 检查点合并"策略：每个进程维护自己的内存映射表，写入时先读取磁盘上的 .runtime-signals.json 作为基础，将自己的变更合并进去（对相同路径的数值字段取最大值或累加，时间戳字段取最新值），然后原子写入。具体合并规则：importance 取 max（避免不同进程的衰减计算互相覆盖导致分数偏低）；accessCount 和 updateCount 取 sum（所有进程的累计操作都应被计入）；recency 取 max（最近一次访问决定近因）；updatedAt 和 lastAccessAt 取 max（最新时间戳）。

去抖动写入（debounce write）进一步减少并发冲突窗口——5 秒内的多次内存更新合并为一次磁盘写入。在去抖动期间到达的并发写入，后到达的进程会读取到已被前一进程更新的磁盘文件，自然形成序列化。

故障隔离：若 .runtime-signals.json 读取失败（文件损坏、权限错误），ScoringProvider 降级为"纯前端元数据模式"——所有动态信号使用默认值（importance: 50, recency: 1），搜索排序仅依赖 BM25 相关性和 maturity 等级提升。降级期间记录警告日志，但查询和 curate 流程不受阻断。降级期间的 access/update 操作在内存中累积，待 Sidecar 文件恢复可写入后批量刷入。若进程崩溃，内存中未持久化的 scoring 更新丢失，但由于这些信号可从 Markdown frontmatter 回退并随系统运行逐步重建，丢失影响有限。

### 归档/剪枝/合并的清理一致性

当知识条目被归档（archive）、剪枝（prune）或通过合并（merge）操作删除时，必须同步清理 Sidecar 中对应的运行时信号条目，避免孤立数据和 Sidecar 文件无限增长。

归档一致性：FileContextTreeArchiveService 在将 context.md 归档为 _archived/xxx.full.md 并创建 .stub.md 时，调用 scoringProvider.removeEntry(originalPath) 删除原路径的 Sidecar 条目。由于归档后的 .stub.md 本质上不再参与搜索排序（stub 通道仅用于发现，不参与 importance 排序），因此无需为 stub 创建新的 Sidecar 条目。

剪枝一致性：当 curate 流程判定某知识条目过时并执行 DELETE 操作时，CurateExecutor 在删除 Markdown 文件的同时调用 scoringProvider.removeEntry(path)。若剪枝是在 merge 流程中由远程端的删除操作驱动的（FileContextTreeWriterService.sync() 中的 deleted 路径），则 WriterService 在完成文件删除后调用 scoringProvider.removeEntry(path)。

合并（merge）一致性：FileContextTreeMerger 在合并过程中仅操作 Markdown 文件；Sidecar 不参与合并。合并完成后，若出现冲突文件被重命名为 _N.md，需要对旧路径调用 removeEntry 并对新路径进行 scoring 迁移。具体而言：在 writeConflict() 中将 localContent 保存为 _N.md 后，调用 scoringProvider.migrateEntry(originalPath, newPath) 将原路径的运行时信号迁移至新路径；若合并导致文件被删除（deleted 列表），调用 removeEntry。

垃圾回收：定期（如每次 curate 完成后或每天首次启动时）执行 Sidecar 垃圾回收：遍历 Sidecar 中的所有条目路径，检查对应的 Markdown 文件是否存在（包括检查 _archived/ 中的 .stub.md），若文件已不存在则从 Sidecar 中移除对应条目。此操作轻量——仅需对 Sidecar 键集合执行文件存在性检查。

### 技术效果

本方案在保持 ByteRover 现有上下文树架构和搜索排序能力的前提下，解决了动态信号与共享知识存储耦合带来的系列问题。

（1）版本控制噪声消除：日常搜索和 curate 操作不再对 Markdown 文件产生写入，Git 工作区仅在知识内容发生实质性变更时才变脏。团队成员提交的 diff 只包含 title、tags、summary、正文内容等有审阅价值的变化，审阅效率显著提升。

（2）合并冲突减少：由于 importance、accessCount、recency 等高频变更字段不再存在于共享 Markdown 中，团队 push/pull 时的合并冲突从"几乎所有活跃条目都冲突"降为"仅真正同时编辑了同一条目正文内容时才冲突"。FileContextTreeMerger 的冲突处理逻辑无需变更即可受益。

（3）搜索排序能力保持：读取融合层确保 compoundScore() 依然获得完整的 importance、recency 和 maturity 信号，搜索排序质量与迁移前一致。importance 的累计效应（accessCount 和 updateCount 的加分）和 recency 的时间衰减机制完整保留在 Sidecar 中。

（4）可重建性：Sidecar 中的所有数据均可从 Markdown frontmatter 的初始 scoring 值和系统运行过程中重新累积。删除 .runtime-signals.json 后，系统在首次启动时从 Markdown 回退读取初始值，后续搜索和 curate 操作逐步重建完整信号。这保证了本方案不会引入不可恢复的数据依赖。

（5）与现有架构兼容：方案复用 ByteRover 已有的 isDerivedArtifact() / isExcludedFromSync() 机制和 CONTEXT_TREE_GITIGNORE_PATTERNS 忽略列表，仅需新增一条 .runtime-signals.json 的忽略规则。IScoringProvider 接口替换 parseFrontmatterScoring() 的调用点集中在 SearchKnowledgeService 和 FileContextTreeManifestService 两处，改动范围可控。

### 风险与待确认问题

（1）多机协同场景：Runtime Sidecar 是本机存储，不同团队成员的 Sidecar 数据独立演化。当两个成员各自对同一知识条目进行了不同频次的搜索访问后，其 importance 和 recency 会分叉。这本身不影响知识内容的一致性（因为知识正文仍在共享 Markdown 中保持同步），但可能导致不同成员的搜索结果排序略有差异。如果团队期望搜索排序也完全一致，需要进一步考虑是否将 Sidecar 的部分数据（如仅 accessCount 和 updateCount 的累计值）纳入 CoGit 同步。当前方案暂不处理此场景，可留作后续优化方向。

（2）maturity 字段的人工化：方案将 maturity 从 importance 自动推导改为人工 curation 设定。这要求 curate 流程或用户显式管理 maturity 字段，否则所有新条目将长期停留在默认的 draft 状态，影响搜索排序中的 tier boost 效果。需要在 curate prompt 中增加 maturity 评估指令，或在 review 流程中增加 maturity 修改入口。

（3）Sidecar 文件大小增长：随着知识库规模增长（10,000+ 条目），.runtime-signals.json 可能达到数 MB。当前设计采用全量加载到内存，需验证在大型项目中的内存占用和加载时间是否可接受。若不可接受，可进一步拆分为按 domain 子目录分片存储。

（4）BM25 索引失效：当前 SearchKnowledgeService 在 Markdown 文件 mtime 变更时重建 BM25 索引。迁移后 Markdown 文件不再因 scoring 更新而修改 mtime，减少了不必要的索引重建。但这意味着 BM25 索引缓存有效期可能变得更长，需要确认索引刷新策略是否需要调整（如增加基于时间的强制刷新）。
