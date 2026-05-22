## 技术方案

### 技术问题概述

ByteRover 系统的上下文知识树（context tree）以 Markdown 文件形式存储在 `.brv/context-tree/` 目录下，每个知识文件通过 YAML frontmatter 携带结构化元数据。当前设计中，frontmatter 同时承载两类性质不同的字段：一类是描述知识内容本身的静态元数据（如 title、tags、keywords、related、summary），另一类是反映运行时使用情况的动态信号（如 importance、recency、maturity、accessCount、updateCount、updatedAt）。这些动态信号在系统运行过程中频繁变化——每次搜索命中会提升 importance 和 accessCount，每次知识整理（curate）操作会更新 importance 和 recency，系统还会按时间衰减模型（FinMem 启发式算法）周期性调整 importance 和 recency 的值。

由于动态信号字段嵌入在共享的 Markdown 文件中，每次信号变化都需要写回文件系统，导致这些文件在版本控制系统（Git）中频繁被标记为已修改。在团队协作场景下，不同成员机器上各自产生的信号更新（搜索命中、衰减计算结果不同）会导致同一知识文件在不同成员间出现无意义的差异，进而引发合并冲突。这些冲突掩盖了真正有意义的知识内容变更，增加了团队审阅负担，降低了知识库的可维护性。

本方案要解决的核心问题是：如何在保持知识文件作为可共享、可审阅、可版本控制的文本资产的同时，将频繁变化的运行时动态信号从共享文件中迁出，使得信号更新不再污染版本控制状态，同时保证知识检索排序、自动整理和生命周期管理功能不退化。

### 字段分类与存储分层

本方案的核心思路是引入"静态知识元数据与动态运行时信号的分层存储"机制。具体而言，将知识文件的 YAML frontmatter 拆分为两个逻辑层：共享层（Shared Layer）和信号层（Signal Layer）。共享层继续驻留在 Markdown 文件中，随知识内容一起通过 Git 进行版本控制和团队同步；信号层迁出到本地的独立存储介质中，每个团队成员维护各自私有的信号数据，不参与共享同步。

需要留在共享 Markdown frontmatter 中的字段（共享层）：title、tags、keywords、related、summary。这些字段描述知识的语义内容本身，由用户或 curate 流程显式编辑，变更频率低，变更本身具有语义意义（例如添加标签、修改关联关系），应当被版本控制追踪和团队共享。

需要迁出到本地信号存储的字段（信号层）：importance（重要性评分，0-100）、recency（新鲜度，0-1）、maturity（成熟度层级：draft / validated / core）、accessCount（搜索命中累计次数）、updateCount（整理操作累计次数）、updatedAt（最近更新时间）、createdAt（创建时间）。其中 createdAt 虽仅在创建时写入一次，但为保持字段边界的统一性和迁移规则的简单性，也一并迁出。

### 本地信号存储机制

迁出的动态信号存储在一个本地 SQLite 数据库文件中，路径为 `.brv/context-tree/_signals.db`。选择 SQLite 的理由包括：（1）支持原子读写和事务，适合多个信号写入者并发访问的场景；（2）作为单一文件，易于纳入 `.gitignore` 和系统的派生 artifact 排除机制（`isExcludedFromSync()`），天然不被版本控制同步；（3）查询性能优于逐文件解析 JSON 侧车，特别是在 manifest 构建需要批量读取大量文件信号时；（4）单文件损坏时的恢复简单——删除 `.db` 文件即可降级运行。

数据库表结构以知识文件的相对路径（相对于 `.brv/context-tree/`）作为主键，每一列对应信号层的一个字段：importance（REAL）、recency（REAL）、maturity（TEXT，取值为 draft/validated/core）、accessCount（INTEGER）、updateCount（INTEGER）、updatedAt（TEXT，ISO 时间戳）、createdAt（TEXT，ISO 时间戳）。表在首次访问时若不存在则自动创建，无需预置迁移工具。

`_signals.db` 被注册到系统的派生 artifact 排除列表中（`isExcludedFromSync()` 返回 true），从而被以下流程自动跳过：快照追踪（snapshot service）、CoGit 推送/拉取（push/pull）、合并冲突处理（merger）、写入同步（writer service）。这一机制保证了信号数据永远不会进入团队共享的版本控制通道。

### 读取融合策略

所有原本需要读取 `FrontmatterScoring`（即信号层字段）的功能模块，改为采用两阶段读取融合策略：第一阶段从 Markdown frontmatter 中解析静态元数据（title、tags、keywords、related、summary），第二阶段以文件路径为键查询 `_signals.db` 获取动态信号，最后在内存中将两部分合并为完整的元数据结构供下游消费。融合后的数据仅在内存中存续，绝不回写到 Markdown 文件中。

具体影响的功能模块包括：（1）Manifest 构建服务（`FileContextTreeManifestService`）在扫描知识文件进行三通道（summaries / contexts / stubs）分配时，从信号库读取 importance 参与排序和 token 预算分配，不再从 frontmatter 解析 importance 字段；（2）知识搜索服务（`search-knowledge-service`）在执行复合评分排序时，其 `compoundScore` 函数所需的 importance、recency、maturity 参数从信号库读取，与 BM25 文本相关性分数加权融合；（3）Prune 整理操作（`prune.ts`）在判断文件是否应被归档时，所需的 importance 和 maturity 信号从信号库读取；（4）Consolidate 合并操作（`consolidate.ts`）在合并多个知识文件的 scoring 元数据时，操作对象从 frontmatter 字段改为信号库中的对应条目。

### 迁移期兼容策略

考虑到系统中已存在大量携带 scoring 字段的旧格式 Markdown 文件，方案采用"读时迁移 + 写时清除"的两阶段兼容策略，确保零停机迁移。

读时迁移：任何模块在读取知识文件时，若 Markdown frontmatter 中仍包含旧格式的 scoring 字段（importance、recency、maturity 等），且信号库中尚无该文件的条目，则自动将旧字段值写入信号库，完成该文件的迁移。迁移在内存中标记，不额外产生文件 I/O。优先级规则为：信号库有值时始终以信号库为准；信号库无值但 frontmatter 有旧值时，以旧值初始化信号库；两者均无值时使用系统默认值。

写时清除：`MarkdownWriter` 在序列化 frontmatter 时，过滤掉所有 `FrontmatterScoring` 字段（importance、recency、maturity、accessCount、updateCount、updatedAt、createdAt），仅输出静态元数据字段。因此，任何触发 Markdown 写入的操作（如 curate 整理、consolidate 合并、内容编辑）都会在自然流程中完成旧 scoring 字段的清除。未被写入过的旧文件即使 frontmatter 中仍保留 scoring 字段，也不会造成功能问题，因为此时信号库中已有对应条目（由读时迁移完成），后续读取始终以信号库为准。

### 团队协作与信号隔离

信号数据的核心设计原则是"信号是个人数据，不参与团队共享"。每个团队成员的本地 `_signals.db` 独立维护，反映该成员机器上的搜索历史、整理操作和时间衰减计算结果。当团队通过 push/pull 同步知识文件时，仅传输和比较 Markdown 文件的静态内容（即去除 scoring 字段后的 frontmatter 与正文），信号数据完全隔离在同步通道之外。

具体处理规则如下：（1）`_signals.db` 文件加入项目的 `.gitignore` 规则，同时被 `isExcludedFromSync()` 判定为派生 artifact，自动排除在快照追踪、推送、拉取和合并流程之外；（2）当 pull 操作引入远端的新知识文件时，系统自动在本地信号库中为该文件创建默认信号条目（importance=0, recency=0, maturity='draft', accessCount=0, updateCount=0）；（3）当 pull 操作更新已存在的知识文件内容时，本地信号库中对应的信号条目保持不变——远端的内容更新不会覆盖本地的使用记录；（4）当 pull 操作删除远端已不存在的文件时，对应删除本地信号库中的信号条目，避免产生孤儿记录。文件合并（merger）仅基于不含 scoring 字段的内容计算哈希值，因此信号差异永远不会触发合并冲突。

### 失败隔离与降级策略

系统设计充分考虑了信号存储层的故障场景，确保信号库的不可用不会导致整体功能中断。当 `_signals.db` 文件损坏、被意外删除或不可读写时，系统进入降级运行模式：所有对信号库的读取操作返回预定义的默认值（importance=0, recency=0, maturity='draft', accessCount=0, updateCount=0, createdAt/updatedAt 为当前时间）；所有写入操作静默丢弃并记录错误日志，不向上层抛出异常阻塞主流程。

降级期间的功能影响有限且可控：（1）知识搜索排序退化为纯 BM25 文本相关性匹配，不叠加信号权重加成；（2）Manifest 构建的 lane 分配退化为全量 stub 模式——所有文件以默认 importance=0 参与 token 预算竞争；（3）Prune 归档操作倾向于保守策略——由于所有文件 maturity 均为 draft，只有明确标记为可归档的内容会被处理。核心的知识检索、浏览和编辑功能完全正常。

恢复策略为：删除损坏的 `_signals.db` 文件后重启系统，信号库自动重新创建空表。若旧格式 Markdown 文件的 frontmatter 中仍保留 scoring 字段（尚未被写时清除流程处理），读时迁移机制会重新将这些旧值导入信号库，实现自动恢复。

### 生命周期操作的信号一致性

知识生命周期管理涉及三种会改变文件集合的操作——归档（Prune）、合并（Consolidate）和删除（Sync），每种操作都需要同步维护信号库中的对应条目，确保不产生孤儿信号记录或信号丢失。

归档操作（Prune）：当知识文件因重要性过低或长期未更新被归档至 `_archived/` 目录时，原路径对应的信号条目从信号库中删除。若需保留归档文件的历史信号信息用于后续分析，可选择将信号条目迁移到信号库中以归档目标路径（`_archived/` 前缀路径）为键存储。推荐默认不保留归档信号以简化实现。

合并操作（Consolidate）：当多个知识文件被判定为重复或高度重叠，通过 LLM 辅助合并为一个文件时，对信号库执行以下流程：读取所有源文件的信号条目，按现有合并规则（`mergeScoring` 函数逻辑）计算合并后的 importance、recency、maturity 等值，写入目标文件的信号条目，最后删除所有源文件的信号条目。合并后的 recency 重置为 1.0（表示刚被更新），accessCount 和 updateCount 取各源文件的最大值。

删除操作（Sync/Writer）：当知识文件通过远程同步被确认删除时（local 存在但 remote 不再包含），`FileContextTreeWriterService` 在删除文件的同时清理信号库中对应的条目。定期或在 Prune/Consolidate 流程中增加一致性校验步骤：遍历信号库的所有条目，检查对应的 Markdown 文件是否仍然存在；对于指向不存在文件的孤儿条目，自动清理。

### 关键处理流程

本方案涉及的关键处理流程如下，覆盖信号从产生到消费的完整链路。

信号写入流程：当搜索命中发生时，`recordAccessHit()` 计算新的 importance 和 accessCount 值，通过信号库写入接口以文件路径为主键执行 UPDATE 操作（若条目不存在则先 INSERT 默认值再 UPDATE）；当 curate 整理完成时，`recordCurateUpdate()` 计算新的 importance、updateCount 和 recency 值，同样写入信号库。写入采用 SQLite 的 REPLACE 语义保证幂等性。

时间衰减流程：系统的周期任务或懒加载触发 `applyDecay()`，遍历所有需要衰减的信号条目，根据距上次更新时间的天数计算衰减后的 importance 和 recency 值，并通过 `determineTier()` 重新评估 maturity 层级（采用滞后阈值防止层级振荡），最后批量 UPDATE 信号库。该流程不再触发任何 Markdown 文件的读写。

Manifest 构建流程：`FileContextTreeManifestService.buildManifest()` 扫描知识文件时调用两阶段读取：先通过 `parseFrontmatter()` 获取静态元数据和 token 估算，再批量查询信号库获取所有文件的 importance 和 maturity 值，合并后按优先级排序进行三通道 lane 分配。生成的 `_manifest.json` 仅包含 importance 快照值（用于查询时的上下文选择），不替代信号库的权威数据。

Push/Pull 同步流程：推送时仅序列化 Markdown 文件的完整内容但过滤 scoring 字段（或保留原始内容但快照比对时忽略 scoring 差异）；拉取时通过 merger 的三方比对机制（本地当前状态 vs 快照状态 vs 远端状态）判定冲突，由于信号字段不再影响文件内容哈希，信号差异不会触发合并冲突。

### 技术效果

本方案通过将动态运行时信号从共享 Markdown 文件中迁出至独立的本地信号存储，取得以下技术效果。

第一，版本控制噪声消除。信号字段（importance、recency、accessCount 等）的高频更新不再触发 Markdown 文件的文件系统写入，Git 工作区不再因搜索操作或时间衰减而产生无意义的脏状态。团队成员在 `git status` 中看到的变更仅包含真正有意义的知识内容修改，审阅效率显著提升。

第二，合并冲突大幅减少。在团队协作场景下，不同成员机器上独立产生的信号变化因各自存储在本地信号库中而互不干扰。Push/pull 同步仅比较和合并 Markdown 的静态内容，信号差异永远不会进入合并冲突解决流程。

第三，知识内容可审阅性保持。共享 Markdown 文件保留了 title、tags、keywords、related、summary 等语义元数据以及完整正文，团队成员和代码审阅者可以通过标准 diff 工具清晰看到每次知识变更的语义内容，而不被大量重要性评分的数值变化所淹没。

第四，检索排序功能不退化。所有依赖动态信号的检索排序、自动整理和生命周期管理功能通过两阶段读取融合策略继续正常运行。信号库的读写性能优于逐文件解析 frontmatter 的方案——Manifest 构建时可通过单次 SQL 批量查询获取所有文件的信号值，避免了 N 次文件读取。

第五，系统鲁棒性增强。信号库损坏时系统自动降级为默认信号运行，核心知识检索和浏览功能不受影响，优于将信号嵌入 Markdown 时文件损坏可能阻塞整个知识树解析的方案。

### 风险与待确认问题

本方案存在以下需要后续确认的技术风险点和待决策事项。

风险点一：信号库与知识文件的一致性。当文件被外部工具直接删除或重命名（绕过 ByteRover 的文件写入服务）时，信号库中可能产生孤儿条目。需要在 Prune 或 Manifest 构建流程中增加周期性一致性校验，扫描孤儿条目并清理。

风险点二：信号冷启动。新加入团队的成员拉取知识库后，本地信号库为空，所有文件以默认 importance=0 参与排序。在积累足够本地使用信号之前，搜索排序质量会退化。可考虑在 pull 流程中附带一份可选的"种子信号"快照（基于团队平均使用数据），但需权衡引入共享信号带来的复杂性。

风险点三：createdAt 字段的处理。createdAt 虽仅创建时写入一次，但若保留在 Markdown frontmatter 中，其语义更接近"知识资产的出生证明"，具有静态属性。若迁入信号库，则在冷启动场景下新成员无法获知文件的创建时间。建议将 createdAt 保留在 Markdown 中作为静态元数据，仅在信号库中保留一份副本用于 scoring 计算。

待确认事项：（1）信号库的确切存储路径选择——`.brv/context-tree/_signals.db`（与知识文件同目录）还是 `.brv/_signals.db`（顶层）；（2）归档文件（`_archived/` 目录下的 `.stub.md` 文件）是否需要保留独立信号——当前架构中 archive stub 的 frontmatter 已包含 evicted_importance 等静态摘要信息；（3）是否需要提供手动触发信号库重建的命令（如 `brv signals rebuild`）以便管理员在信号数据异常时快速恢复；（4）多机同步场景下，是否允许可选的信号快照共享（如通过 `.brv/context-tree/_signals-snapshot.json` 作为轻量共享参考，但不作为权威数据源）。

### 方案与项目模块对应关系（新增）

本方案与 ByteRover 项目现有代码模块的对应关系和改造范围如下。

新增模块：SignalStore —— 封装 SQLite 信号库的初始化、读写、批量查询和迁移逻辑，提供 get(path)、set(path, scoring)、getBatch(paths)、delete(path)、migrateFromFrontmatter(path, frontmatter) 等接口。信号库文件 _signals.db 在 derived-artifact.ts 的 isExcludedFromSync() 中注册排除，在 constants.ts 的 CONTEXT_TREE_GITIGNORE_PATTERNS 中增加 _signals.db。

### 方案与项目模块对应关系（改造）

改造模块：（1）memory-scoring.ts —— recordAccessHit、recordCurateUpdate、applyDecay 等函数的输出目标从返回新对象改为通过 SignalStore.set() 写入信号库。（2）markdown-writer.ts —— generateFrontmatter() 不再序列化 scoring 字段；parseFrontmatterScoring() 增加读时迁移逻辑——若检测到旧 scoring 字段且信号库无对应条目，自动写入信号库。（3）file-context-tree-manifest-service.ts —— importance 读取从 parseFrontmatterScoring() 改为 SignalStore.getBatch() 批量查询。（4）search-knowledge-service.ts —— compoundScore() 从信号库读取 importance、recency、maturity。（5）prune.ts —— extractImportance/extractMaturity 从信号库读取而非从文件内容正则提取。（6）consolidate.ts —— 合并 scoring 时操作信号库条目，完成后删除源文件信号条目。（7）file-context-tree-writer-service.ts —— 删除文件时同步调用 SignalStore.delete()。（8）file-context-tree-merger.ts —— pull 新增文件时初始化默认信号，远端删除时清理信号。
