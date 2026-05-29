## 技术方案

本方案提出一种将知识文件的静态知识内容与动态运行时信号分层存储的方法：将可审阅、可版本控制的知识内容保留在共享 Markdown 文件中，同时将频繁变化的运行时信号（重要性评分、访问计数、更新时间、成熟度等级等）剥离到独立的本机动态信号存储中。系统在检索、排序、清理和自动整理时通过读取融合层将两层数据合并使用，使知识文件保持版本控制清洁、团队合并冲突减少，同时继续利用运行时信号优化知识生命周期管理。

### 字段分层设计

交底书涉及的 ByteRover 系统的知识文件采用 Markdown + YAML 前置元数据（frontmatter）的格式存储在 .brv/context-tree/ 目录下。当前前置元数据中混合了两类字段。

留存在共享 Markdown 前置元数据中的字段（静态知识层）：title（知识条目标题）、summary（语义摘要）、tags（分类标签）、keywords（搜索关键词）、related（关联知识条目路径）。这些字段表达的是知识条目的语义内容、分类体系和关系网络，变更频率低，变更是真实的知识变更，适合团队审阅和版本比对。

迁出到动态信号存储的字段（运行时信号层）：importance（重要性评分，0-100）、recency（新鲜度，0-1 指数衰减值）、accessCount（搜索命中累计次数）、updateCount（策展更新累计次数）、maturity（成熟度等级：draft/validated/core）、createdAt（创建时间戳）、updatedAt（最后更新时间戳）。这些字段由搜索命中、策展操作、时间衰减等运行时事件驱动变化，与知识内容本身无关，变化极其频繁，不应触发版本控制变更。

动态信号存储采用文件路径为键的本地键值结构，例如以 .brv/signals/ 目录下的 JSON 或二进制日志文件存储，不纳入版本控制（.gitignore）。每个知识文件路径对应一条信号记录。

### 读取融合与写入分离机制

当系统需要检索或排序知识条目时，读取融合层负责将静态 Markdown 知识与动态信号存储中的信号合并为完整视图。

读取流程：首先从 Markdown 文件解析前置元数据和正文，获得 title、summary、tags、keywords、related 及 RawConcept/Narrative/Facts/Snippets 等知识内容；然后以该知识文件的相对路径为键查询动态信号存储，获取 importance、recency、accessCount、updateCount、maturity、时间戳等运行时信号；若动态信号存储中不存在该路径的记录（新文件或信号尚未生成），则使用默认值：importance=50、recency=1、maturity=draft、accessCount=0、updateCount=0、时间戳取当前时间。融合后的完整视图传递给现有的 compoundScore() 函数计算复合排序分数，该函数已支持 BM25 相关性 × 权重 + importance × 权重 + recency × 权重，并叠加 maturity 层级加成。

写回流程分离：策展操作（ADD/UPDATE/UPSERT/MERGE/DELETE）仅写入 Markdown 文件的知识内容部分（title、summary、tags、keywords、related、正文），不再在 Markdown 前置元数据中写入或更新 scoring 字段。策展操作完成后，调用 recordCurateUpdate() 生成更新后的运行时信号，仅写入动态信号存储，不触碰 Markdown 文件。搜索命中时的 recordAccessHit() 同样仅更新动态信号存储。写入动态信号存储时采用原子覆写（先写临时文件再 rename），确保崩溃不产生损坏记录。

### 迁移期兼容机制

为兼容已存在的知识文件（其前置元数据中仍包含 scoring 字段），方案设计以下迁移期兼容策略。

读取时优先级规则：当读取融合层查询某个知识文件的运行时信号时，优先从动态信号存储读取；若动态信号存储中不存在该路径的记录，则回退到从 Markdown 文件前置元数据的 scoring 字段提取（通过已有的 parseFrontmatterScoring() 函数），作为初始信号值。提取后，系统将该初始值写入动态信号存储，完成该条目的静默迁移。此后的运行时更新仅写入动态信号存储。

Markdown 写入时的降级策略：策展操作生成新的 Markdown 内容时（MarkdownWriter.generateContext()），不再传入 scoring 参数，或传入空值，使生成的前置元数据中不包含 importance、recency、accessCount、updateCount、maturity、createdAt、updatedAt 字段。对于仍包含 scoring 字段的旧文件，策展 UPDATE 操作在重写文件时自然剥离这些字段。对于从未被策展更新的旧文件，其 scoring 字段保留在 Markdown 中作为回退数据源，直到某次策展更新将其自然移除。此降级策略确保零停机迁移：系统在所有中间状态下均可正常工作。

### 并发更新与失败隔离

并发更新与失败隔离策略分别针对两种场景：多个策展操作并发修改同一知识文件（Markdown 层），以及多个运行时事件并发更新同一信号记录（信号存储层）。

Markdown 层的并发控制复用现有的 writeFileAtomic 原子写入机制：策展操作通过先写临时文件再原子 rename 的方式保证单文件写入的原子性；冲突检测与解决由已有的 detectStructuralLoss() 和 resolveStructuralLoss() 函数处理，在 UPDATE/UPSERT 操作中读取现有文件内容、检测 LLM 生成内容是否丢失既有数据、自动合并丢失项。由于运行时信号不再写入 Markdown，同一知识文件在两次策展之间的 scoring 字段变化不再产生额外的版本控制差异，团队并行策展的合并冲突仅来自真正的知识内容变更。

动态信号存储的并发控制：每个知识文件路径的信号记录独立存储（一个路径对应一条记录），不同知识文件的信号更新互不冲突。同一知识文件的并发信号更新（例如同时发生搜索命中和策展更新）采用乐观合并策略：每次更新读取当前信号值、应用纯函数变换（如 recordAccessHit 或 recordCurateUpdate）、再原子覆写。如果覆写时检测到信号记录已被其他操作修改（通过版本号或内容哈希比对），则重新读取、重新应用变换、重新写入，循环至成功或达到最大重试次数。由于信号更新函数均为幂等的纯数值运算，最终一致性可接受。

失败隔离：动态信号存储的读写失败不应阻塞知识检索和策展操作。当信号存储不可用时（例如磁盘满、权限错误），读取融合层使用默认信号值（importance=50、recency=1、maturity=draft），排序质量下降但不影响知识内容可用性。信号写入失败时静默丢弃本次信号更新，不影响策展操作的成功返回。信号存储恢复后，后续操作自然积累新的信号数据。

### 归档/剪枝/合并时的清理一致性

知识生命周期中的归档、剪枝和合并操作需要同时维护 Markdown 层和信号存储层的一致性。

归档（Archive）：当知识条目被归档（从活跃 context tree 移至 archive 目录），系统在移动 Markdown 文件的同时，将对应的动态信号记录标记为 archived 状态或移至信号存储的归档分区。若后续需要恢复该条目，信号记录随 Markdown 文件一同恢复。归档时的信号快照（特别是 evicted_importance）保留在归档存根（archive stub）的前置元数据中，与现有 ArchiveStubFrontmatter 设计兼容。

剪枝（Prune/Delete）：策展 DELETE 操作删除 Markdown 文件时，同时删除动态信号存储中对应的信号记录。删除操作采用先标记后清理的两阶段策略：先将信号记录标记为 tombstone，再异步删除，防止删除过程中的查询返回不完整数据。

合并（Merge）：策展 MERGE 操作将源文件合并到目标文件时，源文件的 Markdown 被删除、目标文件的 Markdown 被重写。对应的信号处理：源文件的信号记录被删除；目标文件的信号使用 mergeScoring() 函数融合两个文件的信号数据（importance 取最大值、recency 取最大值、accessCount 累加、updateCount 累加并 +1、maturity 取更高层级、createdAt 取更早日期、updatedAt 取当前时间），结果写入动态信号存储。由于信号合并是纯数值运算且不涉及 Markdown 文件内容，MERGE 操作只产生一条目标文件的版本控制变更（来自知识内容合并），源文件的删除也是确定性操作，不会像现有方案那样因 scoring 字段变化产生额外的 diff 噪声。

### 技术效果

与现有方案（所有字段混在 Markdown 前置元数据中）相比，本方案带来以下技术效果。

版本控制清洁度提升：运行时信号（accessCount、recency、importance 等）的频繁更新不再触发 Markdown 文件变更，Git 工作区不再因搜索命中或时间衰减而产生脏状态。团队成员的 git status 只反映真实的知识内容修改，代码审阅者无需在 diff 中过滤大量 scoring 字段的数值抖动。

团队合并冲突减少：多人并行策展不同知识条目时，各自产生的动态信号更新原先会导致共享 Markdown 文件产生冲突（尤其是同一条目的 importance 字段被多人独立更新）。分层后，信号更新写入本机信号存储，不影响共享 Markdown，合并冲突仅来自真正的知识内容语义冲突。

信号可用性不变：读取融合层在检索和排序时透明地合并两层数据，compoundScore()、determineTier()、applyDecay() 等现有评分函数无需修改即继续工作。知识检索的排序质量、成熟度推进和自动清理策略完全保留。

迁移零停机：回退兼容策略使新老格式文件可共存，系统在迁移期的所有中间状态下均可正常工作。无需批量改写现有知识文件。

故障降级而非崩溃：动态信号存储不可用时，系统使用默认信号值继续提供知识检索服务，排序精度下降但不影响知识内容的可用性。这与将全部知识迁移到不可审阅的数据库的方案形成对比——后者一旦数据库不可用则知识完全不可访问。

### 风险与待确认点

以下为基于当前项目环境分析识别的待确认或需要注意的技术点。

动态信号存储的具体文件格式需要在 JSON 行格式（append-only log + 定期压缩）、每个条目独立 JSON 文件、或嵌入式键值存储（如 SQLite）之间选择。JSON 行格式适合高写入吞吐但需要压缩逻辑；独立 JSON 文件写入简单但大量条目时目录性能需评估。

信号存储的跨设备同步：当前动态信号存储是本机的，团队成员各自维护独立的信号数据。如果需要跨设备共享信号（如团队成员 A 的检索热度对成员 B 也有参考价值），可考虑后续扩展为可选的信号同步机制，但不应将信号直接放回版本控制的 Markdown 中。

现有 search-knowledge-tool 中通过 parseFrontmatterScoring() 直接从 Markdown 读取 scoring 的调用点需要适配为通过读取融合层获取信号，需确认适配范围和工作量。

ContextTreeStore（map 处理结果的内存缓冲）与知识文件信号存储是独立的两套存储，互不影响。本方案不涉及 ContextTreeStore 的修改。
