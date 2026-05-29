## 技术方案

本方案提出一种面向可共享知识库的动态信号分离方法，解决在基于 Markdown 文件的知识管理系统中，运行时动态信号反复改写共享知识文件所导致的版本控制状态变脏、团队合并冲突增多、知识内容变更被噪声淹没的问题。本方案的核心思路是将知识文件的存储结构拆分为静态知识层与动态信号层：静态知识层以 Markdown 文件为载体，包含标题、标签、关键词、关联关系、摘要等人类可审阅的知识定义字段，继续纳入版本控制；动态信号层以独立于版本控制的侧挂存储（Scoring Sidecar）为载体，记录重要性分值、访问计数、更新计数、最近访问时间戳、时间衰减后的新鲜度等运行时度量字段。系统在检索、排序、归档、剪枝、合并等生命周期操作中同时读取两层数据并在内存中融合计算复合分值，但仅将知识内容变更写回 Markdown，将信号变更写回侧挂存储，从而既保留知识文件的可共享、可审阅特性，又持续利用运行时产生的动态信号优化知识管理。

### 1. 字段分类与双层存储模型

系统将当前存储在 Markdown 文件 YAML frontmatter 中的所有字段按变更频率和知识语义重新划分为两类。静态知识字段继续保留在 Markdown frontmatter 中并纳入版本控制：包括 title（知识条目标题）、tags（分类标签）、keywords（检索关键词）、related（关联条目路径列表）、summary（内容摘要）。这些字段由人工或 LLM 在知识策展（curation）过程中显式编辑，变更频率低，且每次变更都代表知识内容本身的实质性演进，适合通过版本控制进行审阅和追溯。动态信号字段迁出至侧挂存储：包括 importance（重要性分值）、recency（新鲜度分值）、accessCount（累计搜索命中次数）、updateCount（累计策展更新次数）、createdAt（创建时间戳）、updatedAt（最后更新时间戳）。这些字段由系统在每次搜索命中、策展更新、时间衰减计算时自动修改，变更频率远高于知识内容本身，迁出后不再触发 Markdown 文件的版本控制变更。

maturity（成熟度级别：draft / validated / core）作为一项特殊字段采取双写策略：侧挂存储中维护由动态信号计算得到的运行时成熟度，用于检索排序和归档判断；Markdown frontmatter 中保留人工可覆盖的基准成熟度，用于团队审阅和手动质量标记。当侧挂存储不可用时，系统回退到 Markdown 中的基准成熟度。

### 2. 侧挂存储结构与读写接口

侧挂存储采用每个上下文树目录下独立的轻量级数据库文件（如 SQLite 或结构化 JSON），命名为 _scoring.db 或 _scoring.json，与 _index.md、_manifest.json 等派生文件同层存放。该文件不纳入版本控制（通过 .gitignore 排除），其内部结构为以 Markdown 文件相对路径为主键的键值表，每个记录包含：file_path（主键）、importance（浮点数，0-100）、recency（浮点数，0-1）、access_count（整数）、update_count（整数）、created_at（ISO 时间戳）、updated_at（ISO 时间戳）、runtime_maturity（枚举字符串）。

侧挂存储的读写接口设计为与当前 MarkdownWriter 中的 parseFrontmatterScoring / updateScoringInContent 同形的函数对：readScoring(relativePath) 从侧挂存储读取指定文件的动态信号记录，若记录不存在则返回 applyDefaultScoring() 的默认值；writeScoring(relativePath, scoring) 将信号记录写入侧挂存储。该设计使得现有的 memory-scoring 模块（compoundScore、applyDecay、recordAccessHits、recordCurateUpdate、determineTier）可以无需修改地继续工作在 FrontmatterScoring 数据结构上，仅将读写目标从 Markdown frontmatter 替换为侧挂存储。

为减少磁盘写入放大，侧挂存储的访问计数更新采用与现有 SearchKnowledgeService.pendingAccessHits 相同的批量延迟写入策略：搜索命中的路径和计数先累积在内存 Map 中，在索引重建（TTL 过期或文件变更触发）之前批量刷新到侧挂存储。策展更新则在每次知识写入完成后同步更新侧挂存储中对应记录的 update_count 和 recency。

### 3. 读取融合与降级策略

系统在检索、排序、上下文注入三条路径上需要同时使用静态知识内容和动态信号。读取融合的核心机制是：在构建搜索索引（SearchKnowledgeService.acquireIndex → buildFreshIndex）时，indexOriginDocuments 函数对每个 Markdown 文件同时执行两个读取操作：从 Markdown 文件本身解析 frontmatter 获取 title、tags、keywords、summary 等静态字段；从侧挂存储读取该文件对应的 FrontmatterScoring 动态记录。两者在内存中合并为一个完整的 IndexedDocument 结构，其 scoring 字段来自侧挂存储而非 Markdown frontmatter。

搜索排序管线保持不变：runTextSearch 先通过 MiniSearch BM25 获取原始相关性分数并归一化到 [0,1)，然后调用 compoundScore() 将 BM25 分数与 scoring 中的 importance、recency 加权融合，再应用 maturity 层级加成（core: 1.15, validated: 1.0, draft: 0.85），最终按融合分数降序排列。侧挂存储不可用时的降级策略：若侧挂存储文件缺失或读取失败，系统使用 applyDefaultScoring() 生成默认分值（importance=50, recency=1.0, maturity='draft'），确保检索功能不中断，但排序质量退化为纯 BM25 加默认权重。

上下文注入路径（ContextTreeManifestService.resolveForInjection）同样需要 importance 分值来在三个通道（summaries / contexts / stubs）内按重要性排序。Manifest 构建过程中从侧挂存储读取每条目的 importance 和 maturity 进行通道分配和通道内排序，而非从 Markdown frontmatter 读取。

### 4. 迁移期兼容机制

为确保从旧格式（动态信号内嵌于 Markdown frontmatter）到新格式（动态信号迁出至侧挂存储）的平滑过渡，系统实现以下兼容机制。首次启动检测：当系统首次以新格式运行时，检查侧挂存储文件是否存在；若不存在，则触发一次性迁移扫描：遍历 .brv/context-tree/ 下所有 .md 文件（排除 _index.md、.stub.md、.full.md、.abstract.md 等派生文件），调用 parseFrontmatterScoring() 提取每个文件中已有的 scoring 字段，将其写入侧挂存储，同时调用 stripScoringFromContent() 从 Markdown 文件中移除 importance、recency、accessCount、updateCount、createdAt、updatedAt 字段，保留 title、tags、keywords、related、summary、maturity 字段。

迁移采用全有或全无事务语义：所有 Markdown 文件的改写和侧挂存储的写入在同一事务批次内完成；若任一步骤失败（如磁盘空间不足、权限错误），系统回滚已改写的 Markdown 文件（通过预先备份或基于 git checkout 恢复）并保持旧格式运行，下次启动重新尝试迁移。迁移完成后在侧挂存储中记录一个 schema_version 标记，后续启动跳过迁移。

迁移期间兼容读写：迁移扫描过程中，尚未处理的文件继续使用内嵌 frontmatter 中的 scoring 值参与搜索和排序；已处理的文件则从侧挂存储读取。这种逐文件渐进切换确保了迁移过程中系统的持续可用性。团队其他成员拉取迁移后的仓库时，由于侧挂存储文件不在版本控制中，其本地系统在首次启动时会自动从 Markdown 文件中的残余 frontmatter（如旧格式文件尚未被迁移）或无 scoring 字段的新格式文件（使用默认值）初始化各自的侧挂存储。

### 5. 并发更新与失败隔离

本方案涉及三类并发写入场景：多用户通过版本控制并发修改同一 Markdown 文件、本地搜索线程累积访问计数并批量刷新到侧挂存储、策展操作同时写入 Markdown 和侧挂存储。对于 Markdown 文件的并发写入，系统沿用现有的版本控制合并机制（Git merge / rebase），因为迁出动态信号后 Markdown 文件仅包含低频变更的静态字段，合并冲突的概率大幅降低。

对于侧挂存储的并发写入，采用以下策略。搜索访问计数累积：SearchKnowledgeService 在内存中维护 pendingAccessHits 映射，搜索线程仅写入内存映射而不竞争侧挂存储的磁盘 I/O。批量刷新（flushAccessHits）在索引重建的构建锁（buildingPromise）保护下执行，确保同一时刻只有一个刷新操作在读写侧挂存储。策展写入：curate 操作在完成 Markdown 文件写入后，同步调用 writeScoring() 更新侧挂存储中对应记录的 update_count 和 recency。若侧挂存储写入失败，Markdown 文件的写入已经成功（知识内容已持久化），侧挂存储的更新丢失不会导致数据不一致——下次读取时该条目的 update_count 和 recency 保持旧值，仅丢失一次策展更新的重要性加成。策展操作的 Markdown 写入和侧挂写入不要求原子性，因为两者的不一致是自愈的：侧挂信号最终会通过后续的搜索命中和时间衰减自然调整。

失败隔离策略：侧挂存储文件损坏或不可读写时，系统以 applyDefaultScoring() 的默认值参与所有排序和过滤计算，确保知识检索和策展功能不降级为不可用。侧挂存储的写入失败被静默吞没（best-effort），不影响主流程。Markdown 文件写入失败则按现有错误处理路径返回失败，策展操作整体标记为失败。

### 6. 归档、剪枝与合并的清理一致性

知识生命周期管理中的归档（archive）、剪枝（prune）、合并（merge/consolidate）操作均涉及对 Markdown 文件和动态信号的联动变更，需要保证清理一致性。归档操作：当 FileContextTreeArchiveService.archiveEntry() 将低重要性条目从 .md 归档为 _archived/ 下的 .stub.md + .full.md 时，同步执行以下侧挂操作——将被归档条目在侧挂存储中的记录标记为 archived 状态并保留其 evicted_importance 和 evicted_at；将新生成的 .stub.md 在侧挂存储中注册一条新记录，其 importance 设置为原条目的归档时重要性值，maturity 设置为 draft（归档桩的初始成熟度）。drillDown（展开归档条目）时从侧挂存储读取 .stub.md 的动态信号用于排序，展开后的完整内容因不参与独立检索故不需要独立的动态信号。

剪枝操作：Dream prune 流程在通过 LLM 审查确定要归档的候选文件列表后，对每个归档目标同时调用 archiveEntry()（处理 Markdown 层）和侧挂存储的迁移接口（处理信号层），两者使用相同的文件路径作为关联键。若 Markdown 归档成功但侧挂迁移失败，该条目的侧挂记录保留在原路径下，成为孤立记录——这在下次搜索索引重建时因对应 Markdown 文件已不存在而自动被 indexOriginDocuments 跳过（不产生搜索结果），且系统定期运行孤记录清理（orphan cleanup）：在每次索引重建完成后，遍历侧挂存储中的所有记录，删除 file_path 在上下文树中不存在对应 .md 或 .stub.md 文件的记录。

合并操作：Dream consolidate 在执行 MERGE 动作时，将源文件的内容合并到目标文件后删除源文件。对应的侧挂清理策略为——调用 mergeScoring() 将源文件和目标文件的动态信号合并（importance 取最大值、recency 取最大值、accessCount 求和、updateCount 求和加一），合并结果写入目标文件在侧挂存储中的记录；源文件在侧挂存储中的记录标记为 merged 状态并保留指向目标文件路径的引用，供孤记录清理时跳过（避免误删），同时在下次孤记录清理周期中被物理删除。合成操作（synthesize）：新生成的合成文件在侧挂存储中以默认分值初始化，不继承源域信号的任何部分。

### 7. 关键处理流程

搜索与排序流程完整路径如下：（1）SearchKnowledgeService.search() 接收查询字符串；（2）acquireIndex() 检查缓存有效性，若需要重建索引则先调用 flushAccessHits() 将累积的访问计数批量写入侧挂存储；（3）buildFreshIndex() 遍历上下文树中所有 Markdown 文件，对每个文件分别读取 Markdown frontmatter（静态字段）和侧挂存储（动态信号），合并为 IndexedDocument；（4）MiniSearch 执行 BM25 文本检索得到原始相关性分；（5）normalizeScore() 将 BM25 分映射到 [0,1)；（6）applyDecay() 根据文件 mtime 计算时间衰减后的 importance 和 recency；（7）compoundScore() 按权重 0.6×BM25 + 0.2×importance_norm + 0.2×recency 计算复合分，并应用 tier 加成；（8）结果按复合分降序排列，同时 accumulateAccessHits() 将命中路径加入 pendingAccessHits 内存映射；（9）返回最终排序结果。

策展写入流程完整路径如下：（1）CurateService.curate() 接收策展操作列表；（2）executeCurate() 对每个操作执行 Markdown 文件的 ADD / UPDATE / UPSERT / MERGE / DELETE，通过 MarkdownWriter 生成新的 Markdown 正文和 frontmatter（仅包含静态字段）；（3）文件写入成功后，对 ADD/UPDATE/UPSERT 操作调用 recordCurateUpdate() 计算新的动态信号分值，通过 writeScoring() 写入侧挂存储；（4）对 MERGE 操作调用 mergeScoring() 合并源和目标信号，写入目标文件的侧挂记录，源文件的侧挂记录标记为 merged；（5）对 DELETE 操作调用 deleteScoring() 删除侧挂记录。若侧挂写入失败，静默吞没错误，不影响策展操作的整体成功状态。

### 8. 技术效果

相比现有将动态信号内嵌于 Markdown frontmatter 的方案，本方案产生以下技术效果。（1）版本控制噪声消除：搜索命中、时间衰减、策展更新等高频操作不再触发 Markdown 文件的磁盘写入，每日数百次搜索场景下可消除 99% 以上的知识库 VCS 变更记录。团队成员的 git status、git diff 仅显示真正的知识内容变化。（2）合并冲突减少：迁出 6 个动态字段后，Markdown frontmatter 中仅保留 5 个低频变更的静态字段，多人并行策展同一域不同条目时不在同一文件上产生冲突；即使同时编辑同一文件，冲突也仅来自知识内容差异而非动态信号的数值竞争。（3）知识可审阅性保持：人类审阅者打开 Markdown 文件时看到的是标题、标签、摘要、关联关系等知识定义信息，不被重要性 87.35、访问次数 142 等机器信号干扰。同时知识内容仍可通过 git log、git blame 追溯完整的修改历史。（4）检索质量不降级：重要性、新鲜度、成熟度等信号虽然不再存储于 Markdown 中，但仍通过侧挂存储在检索时参与复合排序，检索结果质量与迁出前一致。侧挂存储不可用时系统自动回退到默认权重，检索功能不中断。（5）团队协作友好：侧挂存储文件不进入版本控制，每个团队成员维护各自的本地动态信号视图。共享的知识内容通过 git push/pull 同步，个人使用模式产生的信号差异不影响他人。

### 9. 风险与待确认问题

待确认与风险点：（1）侧挂存储的跨平台兼容性：SQLite 在不同操作系统和 Node.js 版本下的行为差异需要验证；备选方案为使用结构化 JSON 文件（与 _manifest.json 同模式），虽然并发写入性能较低但实现更简单且完全跨平台。（2）maturity 双写一致性：当侧挂存储中的运行时成熟度因重要性变化而升级（如 draft→validated），但 Markdown 中的基准成熟度未被人工同步更新时，两者可能出现长期分歧。建议在 Dream 周期中增加 maturity 同步步骤，当运行时成熟度持续高于基准成熟度超过 N 天时，自动提示或自动更新 Markdown 中的 maturity 字段。（3）孤记录清理的性能影响：在全量上下文树（例如 10,000 个文件）上执行孤记录清理需要遍历侧挂存储所有记录并逐一检查文件是否存在，可能成为索引重建的瓶颈。可通过维护一个 dirty_files 集合（在 archive/merge/delete 操作时写入）来缩小清理范围。（4）共享知识源（shared sources）场景：当前 SearchKnowledgeService 支持通过 sources.json 配置的共享来源上下文树。侧挂存储是否需要随共享源分发还是保持在各自本地，取决于共享源的访问模式：如果共享源以只读方式被引用，其侧挂存储应保持在引用方本地；如果共享源本身也参与策展，侧挂存储可随源分发但需额外处理多引用方的写入冲突。
