## 技术方案

### 技术问题概述

ByteRover 的 context tree 以 Markdown 文件形式存储项目知识，每篇知识文件包含 YAML 前置元数据（frontmatter）和正文内容。当前架构中，frontmatter 同时承载两类性质不同的字段：一类是描述知识本身语义的静态字段，如 title、summary、tags、keywords、related（关联路径）；另一类是反映运行时使用状况的动态信号，如 importance（重要性得分）、recency（新鲜度得分）、accessCount（检索命中次数）、updateCount（策展更新次数）、maturity（成熟度层级）及 createdAt/updatedAt 时间戳。这些动态信号会随每次知识检索命中、策展更新或时间衰减计算而频繁变化。

由于 Markdown 文件整体受 CoGit 版本控制并在团队间同步，动态信号的每次变化都会产生一次版本差异，导致：版本控制日志被噪声淹没，真正的知识内容变更难以审计；多成员并行使用时，各自产生的动态信号更新在同一文件上形成合并冲突；维护者难以区分“知识内涵发生了变化”与“仅仅是使用统计更新了”。简单删除动态信号会丧失其对检索排序、知识清理和自动整理的利用价值；将全部内容迁移至不可审阅的数据库则牺牲了 Markdown 的可共享、可人工审阅和可版本对比的优势。

### 字段分离策略

本方案将知识文件 frontmatter 中的字段按变化频率和语义角色划分为两类：静态知识字段和动态运行时信号。静态知识字段继续保留在 Markdown 文件的 YAML frontmatter 中，参与 CoGit 版本控制和团队同步。动态运行时信号从 Markdown frontmatter 中迁出，存入独立的运行时信号存储（Runtime Signal Store，以下简称 RSS），不再写入 Markdown 文件。

静态知识字段（留在 Markdown frontmatter 中）：title（知识标题）、summary（摘要）、tags（标签）、keywords（关键词）、related（关联路径）。这些字段描述知识本身的内容语义，仅在人工或 LLM 策展（curate）修改知识内涵时才会变化，变化频率低且具有审阅价值。

动态运行时信号（迁入 RSS）：importance（重要性得分，0-100）、recency（新鲜度得分，0-1）、accessCount（检索命中累计次数）、updateCount（策展更新累计次数）、maturity（成熟度层级：draft/validated/core）、createdAt（创建时间戳）、updatedAt（最后更新时间戳）。这些字段反映的是本机或本地运行时环境中的使用统计和计算派生值，变化频繁，不具备跨团队审阅价值，且它们的值部分依赖于本机运行上下文（如本机用户的检索命中次数与另一台机器不同）。

### 双层存储架构

系统在现有 .brv/context-tree/ 目录结构基础上增加一层运行时信号存储，形成双层架构。

第一层——共享知识层（Shared Knowledge Layer）：即现有的 .brv/context-tree/ 目录，存储 Markdown 知识文件。文件 frontmatter 仅包含静态知识字段。该层受 CoGit 版本控制，在团队间同步，支持人工直接打开审阅和编辑。该层同时包含现有的派生产物（_index.md、_manifest.json、.snapshot.json、_archived/ 等），这些派生产物仍然通过 isExcludedFromSync() 机制排除在同步之外。

第二层——运行时信号层（Runtime Signal Layer）：新增 .brv/context-tree-runtime/ 目录，存储每个知识文件对应的运行时信号记录。该目录整体加入 .gitignore 和 CoGit 同步排除列表（扩展 isExcludedFromSync 谓词），完全不参与版本控制和团队同步。每条记录以知识文件的相对路径为键，值为 JSON 结构，包含全部动态运行时信号字段。实施时，RSS 采用按需加载的内存缓存策略：系统启动时不加载全部 RSS 记录，仅在首次访问某知识条目时从磁盘读取对应 JSON 文件并缓存到内存；写入采用异步批量刷盘（debounced flush），减少磁盘 I/O。

### 读取融合机制

当系统的检索、排序、上下文注入或知识浏览组件需要读取某知识条目的完整视图时，系统执行读取融合（Read-time Merge）：先从共享知识层读取 Markdown 文件，解析其 frontmatter 和正文；再从运行时信号层查询该条目对应的 RSS 记录；然后将两路数据合并为一个完整的 ContextData 结构，其中静态字段来自 Markdown，动态字段来自 RSS。

融合规则如下：（1）如果 RSS 中存在该条目的记录，则其动态字段直接使用 RSS 中的值；（2）如果 RSS 中不存在该条目的记录（例如新创建的 Markdown 文件尚未产生运行时数据，或迁移前的旧文件），则使用默认值填充：importance 取 50，recency 取 1.0，maturity 取 draft，accessCount 和 updateCount 取 0，createdAt 和 updatedAt 从文件系统的 mtime 推导；（3）Markdown 文件被删除时，对应的 RSS 记录在下次访问时被惰性清理。该融合逻辑封装在现有的 MarkdownWriter.parseContent() 扩展版本中，对上层调用者（SearchKnowledgeService、FileContextFileReader、FileContextTreeManifestService 等）保持透明。

### 运行时信号更新与隔离写

运行时信号的更新路径与 Markdown 知识文件的写入路径完全隔离。具体而言，现有的 memory-scoring.ts 中的 recordAccessHits()、applyDecay()、determineTier() 等函数保持为纯函数——它们计算新的评分值但不直接写入任何持久化存储。新增一个 RuntimeSignalWriter 模块，作为写入 RSS 的唯一入口。

写入时机分为两类：（1）同步即时写入：当用户执行知识检索（search_knowledge）且系统根据 BM25 + 综合评分返回结果后，系统对命中的知识条目调用 recordAccessHits() 更新 accessCount 和 importance，并通过 RuntimeSignalWriter 立即写入 RSS。此写入不影响 Markdown 文件，不触发 CoGit 差异。（2）异步批量写入：当 LLM 策展代理（curate agent）完成知识文件的创建、更新或合并后，系统通过 RuntimeSignalWriter 更新对应 RSS 记录的 updateCount、importance（含 UPDATE_IMPORTANCE_BONUS 加分）和 updatedAt 时间戳。同时，系统定期（如每 24 小时或守护进程启动时）对全部 RSS 记录执行一次时间衰减扫描，调用 applyDecay() 更新 recency 和 importance 值，结果写回 RSS。

RSS 写入采用文件级隔离：每个知识条目对应一个独立的 JSON 文件，路径为 .brv/context-tree-runtime/<知识文件相对路径>.json。这种设计的优势是：不同知识条目的运行时信号更新不会互相竞争同一文件锁；某一条目的 RSS 文件损坏不会影响其他条目（失败隔离）；并发写入冲突仅可能发生在同一知识条目上，概率远低于全量单文件方案。

### 迁移期兼容

系统需兼容迁移前已存在的 Markdown 知识文件（其 frontmatter 中可能仍包含 scoring 字段）与迁移后新格式文件（frontmatter 中仅有静态字段）共存的过渡期。迁移策略采用惰性迁移（lazy migration），不执行全量一次性转换。

具体机制包括：（1）Markdown 解析扩展：扩展 MarkdownWriter.parseFrontmatter() 使其同时识别 frontmatter 中的 scoring 子对象。如果 frontmatter 中存在 scoring 字段，则将其解析为 FrontmatterScoring 结构，作为读取融合时的 fallback 数据源。（2）首次访问即迁移：当系统通过读取融合路径首次访问某个仍带有 scoring frontmatter 的旧格式 Markdown 文件时，在内存中完成融合后，异步将该文件的 scoring 数据写入 RSS，同时触发一次 Markdown 文件重写——从 frontmatter 中移除 scoring 子对象，仅保留静态字段。该重写操作复用现有的 FileContextTreeWriterService.sync() 路径，通过快照对比检测为 modified 变更，生成一次 CoGit 版本差异。此差异仅发生一次（迁移完成后的后续更新不再触碰 Markdown 的 scoring 字段）。（3）RSS 缺失时的优雅降级：如果某知识条目的 RSS 记录因任何原因不可用（文件损坏、被手动删除、磁盘故障），系统回退到 Markdown frontmatter 中的 scoring 值（若存在），或使用默认值，不会因此阻塞知识检索或上下文注入。

### 并发更新与失败隔离

双层架构下，并发更新的冲突面显著缩小。Markdown 文件的写入仅发生在 LLM 策展代理执行 curate 操作（ADD/UPDATE/UPSERT/MERGE/DELETE）时，频率低且变更具有语义含义。RSS 文件的写入发生在检索命中和策展完成后的评分更新时，频率高但每个条目独立。

并发写入的处理策略如下：（1）Markdown 文件的并发策展写入仍由现有的 FileContextTreeMerger 处理——通过快照对比、三路合并和冲突文件备份（.brv/context-tree-conflicts/）机制解决。RSS 的分离不会增加 Markdown 文件的并发写入频率。（2）RSS 文件的并发写入采用乐观并发控制：每条 RSS 记录在 JSON 结构中携带一个 version 字段（单调递增整数）。RuntimeSignalWriter 在写入前先读取当前磁盘版本号，如果读取到的版本号与内存缓存中的版本号不一致（说明另有写入者已更新），则重新读取最新 RSS 记录，在最新值基础上重新应用本地的增量更新（如 accessCount+1、importance+bonus），再尝试写入。version 字段的读取-比较-写入三步在单文件粒度上通过原子重命名（write to temp file then rename）实现。（3）失败隔离：单个 RSS 文件的写入失败（如磁盘满、权限错误）仅影响该条目的运行时信号更新，不会传播到 Markdown 文件层，也不会阻塞其他条目的 RSS 更新。失败的写入记录到日志，下次访问该条目时系统重新尝试从 Markdown frontmatter 或默认值恢复 RSS 状态。

### 归档/剪枝/合并时的清理一致性

当知识条目被归档（archive）、剪枝（prune）或合并（merge）时，其对应的 RSS 记录需要同步清理，避免残留的运行时信号在后续检索中被错误融合到不存在的知识条目上。

具体清理策略：（1）归档清理：当 FileContextTreeArchiveService.archiveEntry() 将低重要性知识条目从活跃目录移入 _archived/ 时，系统同步将该条目的 RSS 记录迁移到 RSS 归档子目录 .brv/context-tree-runtime/_archived/，保留其运行时信号以便将来 drill-down 恢复时一并还原。同时，归档 stub（.stub.md）的 ghost cue 正文中嵌入指向 RSS 归档记录的引用键，drillDown() 路径在恢复完整内容时同步恢复 RSS。（2）剪枝清理：当知识条目被彻底删除（rm）时，对应的 RSS 记录在下次定期衰减扫描中被检测为孤儿记录（orphan），系统将其移入 RSS 回收目录并保留宽限期（如 30 天），宽限期内若同名 Markdown 文件被重建则自动关联恢复；超期后物理删除。（3）合并清理：当两个知识条目通过 MERGE 操作合并为一个时，源条目被删除、目标条目被更新。系统在 MERGE 完成的事务钩子中：删除源条目的 RSS 记录，将源条目的 accessCount 累加到目标条目的 RSS 记录中（保留累积使用信息），并重置目标条目的 updateCount 增量。该清理操作与 MERGE 本身的 Markdown 文件写入在同一事务上下文中执行，确保一致性。（4）定期一致性校验：守护进程定期（如每 6 小时）执行一次 RSS-Markdown 一致性扫描：遍历 RSS 中的所有记录键，检查对应的 Markdown 文件是否仍然存在；不存在且不在归档目录中的记录标记为孤儿并进入回收流程；存在但 RSS 记录的 updatedAt 早于 Markdown 文件 mtime 的记录，标记为可能过时，在下一次读取融合时触发 RSS 刷新。

### 技术效果

本方案通过将动态运行时信号从版本控制的 Markdown 知识文件中分离至独立的非版本化存储，实现了以下技术效果：

第一，版本控制噪声消除。Markdown 文件仅在知识内涵发生真实变更（人工或 LLM 策展修改 title、summary、tags、keywords、related 或正文）时才产生 CoGit 版本差异。运行时信号的高频更新（检索命中、时间衰减、成熟度跃迁）完全在 RSS 内部完成，不触碰 Markdown 文件。这使团队可以通过 git log 或 CoGit 审阅界面直接看到知识库的语义演进历史，而非被评分字段的数值抖动淹没。

第二，合并冲突显著减少。在迁移前架构中，两个团队成员各自在不同机器上使用系统，各自产生的 scoring 字段更新会在 CoGit pull/merge 时在同一文件的 frontmatter 上形成冲突。迁移后，Markdown 文件只包含低频变更的静态字段，冲突概率大幅降低；RSS 不参与同步，各自机器独立维护本机运行时信号，无跨机冲突问题。

第三，知识可审阅性不变。Markdown 文件仍然保持人类可直接打开阅读和编辑的形式，所有描述知识语义的字段（标题、摘要、标签、关键词、关联路径、正文）完整保留在 Markdown 中。团队成员可以通过任意 Markdown 编辑器或浏览器直接查看和修改知识内容，不依赖专用工具。

第四，动态信号利用价值保留。所有运行时信号仍然存在且可用——importance、recency、accessCount 等继续驱动 memory-scoring.ts 中的复合评分、成熟度判定和时间衰减计算。知识检索的 BM25 + 综合评分排序、低重要性条目的自动归档、成熟度层级的晋升/降级等既有机制不受影响。分离只是改变了存储位置，不影响消费逻辑。

第五，失败隔离与弹性增强。单条 RSS 记录的损坏或写入失败不会影响其他知识条目的运行时信号，也不会阻塞 Markdown 文件的正常读写和同步。读取融合路径的 fallback 机制确保在任何 RSS 异常情况下系统仍能基于 Markdown frontmatter 残留值或默认值正常运行。

### 待确认风险点

（1）RSS 文件数量增长：每个知识条目对应一个独立 JSON 文件，当知识库规模达到数万条目时，.brv/context-tree-runtime/ 目录下将存在大量小文件。建议后续评估是否需要在 RSS 之上引入基于 SQLite 的合并存储后端作为可选替代，同时保持相同的读取融合接口。

（2）跨机信号不一致：不同机器上的 RSS 记录各自独立演化，同一知识条目在不同机器上可能具有不同的 importance 和 recency 值。这在大多数场景下是合理且期望的行为（各机器的使用模式不同），但对于需要跨机聚合统计的场景（如团队级别的知识使用热度报告），需要额外的聚合层。当前方案暂不覆盖跨机聚合。

（3）惰性迁移的过渡期：在迁移完成前（所有旧格式 Markdown 文件被首次访问并完成 scoring 字段移除），部分文件的 frontmatter 仍包含 scoring 字段。此期间读取融合路径的 fallback 逻辑会处理兼容，但旧文件在迁移前仍可能在 CoGit 同步中产生 scoring 相关的版本差异。团队可通过手动触发一次全局 curate 扫描来加速完成迁移。

（4）与现有 isExcludedFromSync 的整合：需扩展 derived-artifact.ts 中的 isExcludedFromSync() 谓词，将 .brv/context-tree-runtime/ 整体加入排除列表，同时更新 FileContextTreeSnapshotService 的目录扫描逻辑使其跳过该目录。这些扩展改动范围小，风险可控。
