## 技术方案

本方案针对 ByteRover 上下文知识树（Context Tree）在团队协作场景下，共享 Markdown 知识文件因频繁写入运行时动态信号（检索命中计数、重要性评分、成熟度判定、排序权重等）而导致版本控制状态反复变脏、合并冲突增多、真正知识内容变更被噪声淹没的问题，提出一种知识内容与运行时信号分层存储、读时融合、写时隔离的软件技术方案。

### 1. 要解决的技术问题

ByteRover 的上下文知识树以 Markdown 文件形式存储在 .brv/context-tree/ 目录下，每个知识文件通过 YAML 前置元数据（frontmatter）同时承载两类信息：一类是稳定的知识内容元数据（标题、摘要、标签、关键词、关联关系），另一类是频繁变化的运行时动态信号（importance 重要性评分、recency 新鲜度、maturity 成熟度层级、accessCount 检索命中次数、updateCount 策展更新次数、updatedAt 最后更新时间）。这些动态信号在每次知识检索命中、重要性衰减计算、成熟度层级提升/降低时都会被重写，导致 Markdown 文件的文件系统修改时间发生变化，进而触发版本控制系统将其标记为“已修改”，造成 diff 噪声、团队合并冲突增加，且真正的知识内容变更被淹没在大量评分更新中。同时，这些动态信号对 BM25 检索排序、复合评分计算、自动清理/归档判断具有不可替代的价值，不能简单丢弃。

### 2. 核心技术方案：双存储层分离架构

本方案的核心思想是将上下文知识树的数据分为两个物理存储层：知识层保留在可共享、可审阅的 Markdown 文件中（纳入版本控制），信号层迁移至独立的信号存储中（排除出版本控制），两者通过文件路径作为关联键在读时融合、写时隔离。

知识层保留在 .brv/context-tree/ 目录下的 Markdown 文件中。其 frontmatter 仅保留稳定的、由人工策展或团队协作产生的元数据字段：title（标题）、summary（摘要）、tags（标签）、keywords（关键词）、related（关联关系路径列表），以及正文中的 Reason、Raw Concept（包含 task、changes、files、flow、patterns、author、timestamp）、Narrative（structure、dependencies、highlights、rules、examples、diagrams）、Facts 和 Snippets 等结构化内容段落。这些字段的变化频率低，变化驱动来源明确（策展操作、团队合并、手动编辑），适合纳入版本控制进行 diff 审阅和历史追溯。

信号层存储于 .brv/signals/ 目录下，每个知识文件对应一个独立的 JSON 信号文件，以知识文件在上下文树中的相对路径为键进行关联。例如，.brv/context-tree/architecture/agents/overview.md 的信号文件路径为 .brv/signals/architecture/agents/overview.json。信号文件仅包含频繁变化的动态字段：importance（重要性评分，0-100 浮点数）、recency（新鲜度，0-1 浮点数）、maturity（成熟度层级：draft / validated / core）、accessCount（检索命中累计次数）、updateCount（策展更新累计次数）、createdAt（首次创建时间戳）、updatedAt（最后更新时间戳）、lastAccessAt（最近一次检索命中时间戳，新增字段用于精确衰减计算）。信号文件采用原子写入策略（写入临时文件后重命名），与现有的 DirectoryManager.writeFileAtomic 模式一致。该目录通过 .gitignore 排除出版本控制。

### 3. 读时融合机制

系统在读取知识文件时执行双源读取与融合：首先通过现有的 MarkdownWriter.parseContent 解析 Markdown 文件 frontmatter 和正文，构造 ContextData 对象（不含评分字段）；然后并行读取对应路径的信号文件（.brv/signals/{relativePath}.json），解析出 SignalData 对象；最后将 SignalData 中的动态字段合并到 ContextData.scoring 中，生成完整的内存表示供上层消费者（检索排序、查询执行、清单构建、归档候选扫描）使用。如果信号文件不存在（新文件尚未产生信号、或迁移尚未完成），系统使用 applyDefaultScoring 生成的默认值（importance=50, recency=1, maturity=draft），确保读路径始终返回可用的完整数据。

### 4. 写时隔离机制

写路径根据变更性质严格分流：当策展操作（curate）或合并操作（merge）导致知识内容发生变化时，仅写入 Markdown 文件（不含评分字段的 frontmatter）；当检索命中（search access）、重要性衰减计算（applyDecay）、成熟度重新判定（determineTier）或策展更新触发评分变化时，仅写入信号文件。两个写路径完全解耦：Markdown 写入沿用 DirectoryManager.writeFileAtomic 的原子写入模式，信号文件写入同样采用 tmp 文件 + rename 的原子策略。策展操作完成后，MarkdownWriter.generateContext 在生成 frontmatter 时不再包含 importance、recency、maturity、accessCount、updateCount、createdAt、updatedAt 字段，但会在写入 Markdown 后额外调用信号更新逻辑，将本次策展产生的初始评分或更新评分写入信号文件。

### 5. 迁移期兼容机制

为平滑过渡现有系统中的数据，方案采用"读写兼容、惰性迁移"策略。读取 Markdown 文件时，如果 frontmatter 中仍存在 importance 等评分字段（旧格式），且对应的信号文件不存在，则从 Markdown frontmatter 中提取评分数据作为信号源，同时触发一次惰性迁移：将提取的评分数据写入信号文件，然后重写 Markdown 文件去除评分字段（仅写知识元数据）。这种两阶段迁移确保：第一步写入信号文件成功后，即使第二步 Markdown 重写失败，下次读取时信号文件已存在，评分数据不会丢失；Markdown 重写采用原子写入，失败时 Markdown 保持旧格式（下次仍可重试迁移）。迁移后的 Markdown 不再包含评分字段，后续的策展、检索、合并操作都遵循新的双存储层协议。

### 6. 并发更新与失败隔离

信号文件与 Markdown 文件相互独立，单侧写入失败不影响对侧数据完整性。具体设计：信号文件写入失败时，系统以默认评分值降级运行，不阻塞策展或检索流程（fail-open）；Markdown 写入失败时，信号文件保持与上一版本 Markdown 对应的评分状态，知识树整体回退到写入前状态（Markdown 原子写入保障）；信号文件因磁盘故障等原因损坏时，JSON 解析失败返回默认评分值，上层功能继续可用。并发更新方面，信号文件采用 last-write-wins 策略（原子 rename），对于评分类数据（重要性小幅增减、计数累加），最终一致性可接受；对于需要精确计数的场景（accessCount），可由检索执行器批量累积后一次性写入信号文件（参考现有 memory-scoring.recordAccessHits 的批量模式），减少写入冲突窗口。

### 7. 归档/剪枝/合并的清理一致性

归档（archive）、剪枝（prune）、合并（merge）等操作涉及知识文件和信号文件的联动清理，方案规定了严格的操作顺序和一致性保障策略。归档操作：先将原始 Markdown 内容完整写入 _archived/{path}.full.md，再生成 ghost cue 写入 _archived/{path}.stub.md，然后将信号文件从 .brv/signals/{path}.json 移动到 .brv/signals/_archived/{path}.json（保留信号历史供恢复时使用），最后删除原始 Markdown 文件。如果任一中间步骤失败，已写入的归档文件和已移动的信号文件构成一致的部分归档状态——下次 findArchiveCandidates 扫描时，信号路径下的文件若对应的原始 Markdown 不存在则被忽略，不影响正确性。恢复操作（restore）：先将 _archived/{path}.full.md 内容写回原始 Markdown 路径，再将归档信号文件移回正常信号路径，最后删除 stub 和 full 归档文件。合并操作：FileContextTreeMerger 在处理远程变更时，对于新增文件（added），在写入 Markdown 后同步创建对应的默认信号文件；对于删除文件（deleted），同时删除对应的信号文件；对于冲突文件（conflicted），本地版本重命名为 _N.md 后，为新路径和旧路径分别维护各自的信号文件（旧路径信号跟随 _N.md 文件，新路径信号由远程内容重新初始化）。剪枝操作（prune）：通过 Dream 模块的 prune 操作删除低价值知识条目时，同步删除对应的信号文件。

### 8. 关键模块与处理流程

方案涉及以下核心模块的改造和新增，均基于现有项目架构进行扩展：

- 信号存储模块（SignalStore，新增）：负责信号文件的读写、惰性迁移触发和默认值回退。接口设计参考 IQueryLogStore，提供 getByPath(relativePath)、save(relativePath, signalData)、deleteByPath(relativePath)、migrateFromMarkdown(relativePath, content) 方法。信号文件格式为 JSON，schema 包含 importance、recency、maturity、accessCount、updateCount、createdAt、updatedAt、lastAccessAt 字段。
- MarkdownWriter 改造（修改）：generateContext 方法中，generateFrontmatter 函数不再将 FrontmatterScoring 序列化到 YAML frontmatter 中；parseContent 方法增加双源读取逻辑：解析 Markdown 后，通过注入的 SignalStore 读取对应信号文件，合并评分数据到 ContextData.scoring；更新现有 updateScoringInContent 方法——该方法原本用于直接在 Markdown 中原地更新评分字段，改造后改为调用 SignalStore.save。
- memory-scoring 模块（无需大改）：纯函数逻辑（compoundScore、applyDecay、determineTier、recordAccessHit、mergeScoring 等）保持不变，因为其操作的数据结构 FrontmatterScoring 不变，仅数据来源从 Markdown frontmatter 解析变为从信号文件读取。
- FileContextTreeArchiveService 改造（修改）：archiveEntry 方法在删除原始 Markdown 文件后，增加信号文件归档案骤；restoreEntry 方法在恢复 Markdown 文件后，增加信号文件恢复步骤。findArchiveCandidates 方法从直接解析 Markdown frontmatter 改为通过 SignalStore 读取重要性评分。
- FileContextTreeMerger 改造（修改）：runMerge 方法在处理 added/edited/deleted/conflicted 操作时，同步处理对应的信号文件；合并评分（mergeScoring）数据在合并后写入信号文件而非嵌入 Markdown。
- FileContextTreeManifestService 改造（修改）：scanForManifest 方法中的 importance 提取从 parseFrontmatterScoring(content) 改为通过 SignalStore.getByPath(relativePath) 读取。
- QueryExecutor 与 SearchExecutor（无需大改）：检索排序中的 compoundScore 计算通过注入的 SignalStore 获取评分数据，而非从 Markdown frontmatter 解析。fingerprint 计算逻辑不变（基于知识文件 mtime 和 size），因为评分变更不改变知识文件的 mtime。

### 9. 技术效果

通过本方案的双存储层分离设计，取得以下技术效果：第一，版本控制友好性——Markdown 知识文件仅在其知识内容发生实质性变更时产生 diff，运行时信号更新不再污染版本历史；团队成员的 push/pull/merge 操作不再因评分更新产生无意义的冲突，diff 审阅聚焦于真正的知识内容变化。第二，信号数据可继续利用——所有动态信号（重要性、新鲜度、成熟度、访问计数）通过独立的信号存储层完整保留，BM25+复合评分排序、自动归档候选判断（importance < ARCHIVE_IMPORTANCE_THRESHOLD 且 maturity=draft）、清单车道预算分配（按 importance 降序排列填充）等功能不受影响。第三，读写性能优化——知识文件的写操作频率大幅降低（仅在策展或合并时写入），信号文件的写操作独立且轻量（JSON 原子写入），两者不存在锁竞争；知识文件的 fingerprint 计算仅依赖 mtime/size（stat-only），不再因评分变化而触发缓存失效。第四，渐进式迁移——惰性迁移策略确保旧格式文件与新系统共存期间系统正常运行，无需全量数据迁移即可逐步过渡。
