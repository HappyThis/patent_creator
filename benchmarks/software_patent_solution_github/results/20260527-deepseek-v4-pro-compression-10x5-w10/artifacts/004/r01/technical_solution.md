## 技术方案

本方案针对 ByteRover context tree 中知识文件的可共享性需求与运行时动态信号维护之间的矛盾，提出一种「Markdown 存知识、侧车存信号、读时融合」的双层存储架构。系统将知识文件的字段分为稳定知识字段和动态运行时信号两类：稳定知识字段（标题、标签、关键词、摘要、关联关系、正文各节内容）继续保存在 .brv/context-tree/*.md 的 YAML frontmatter 和正文中，保证团队可通过 Git 审阅、合并和版本追溯；动态运行时信号（importance、recency、accessCount、updateCount、maturity、createdAt、updatedAt）迁出到本机私有的侧车存储 .brv/runtime/scoring.json，避免高频写入污染共享知识文件。

### 字段分类与存储分层

系统将知识文件中每个条目的字段划分为两类，并分别存储到不同介质中。稳定知识字段包括：title（标题）、summary（摘要）、tags（标签）、keywords（关键词）、related（关联关系）以及正文各部分（Raw Concept、Narrative、Facts、Snippets、Reason）。这些字段一旦写入，仅在用户主动编辑（curate）或团队合并（pull/merge）时变更，变更频率低，适合作为版本控制追踪的知识资产。这些字段继续以 YAML frontmatter 和 Markdown 正文的形式保存在 .brv/context-tree/ 目录下的 .md 文件中。

动态运行时信号包括：importance（重要性分数 0-100）、recency（新鲜度 0-1）、accessCount（检索命中次数）、updateCount（curate 更新次数）、maturity（成熟度：draft/validated/core）、createdAt（创建时间）、updatedAt（最后更新时间）。这些信号每次检索命中、每次 curate 操作都会更新，属于高频变化的本机运行时状态。方案将这些字段迁出到本机私有的侧车存储中，不写入共享 Markdown 文件。

### 侧车存储结构与访问策略

侧车存储采用 .brv/runtime/scoring.json 文件，以 JSON 对象格式保存，键为知识文件相对于 context-tree 目录的路径（如 "auth/jwt-tokens.md"），值为 ScoringRecord 对象。每条 ScoringRecord 包含：importance（number）、recency（number）、accessCount（integer）、updateCount（integer）、maturity（enum: draft/validated/core）、createdAt（ISO 8601 string）、updatedAt（ISO 8601 string）。该文件不纳入 Git 版本控制（通过 .gitignore 排除），每台机器独立维护各自的 scoring 状态。

侧车存储的读写采用惰性加载策略：系统启动时一次性将 scoring.json 加载到内存中的 Map<string, ScoringRecord>；写入操作先在内存中累积，通过定时批量刷盘（如每 30 秒或在索引重建前）将内存状态持久化到文件。这种设计避免了每次检索命中都触发磁盘写入，减少了 IO 开销。同时，一次加载避免了频繁的文件解析。

### 读时融合机制

当系统需要读取知识条目的完整信息（如检索排序、manifest 构建、归档候选判断）时，执行读时融合流程。系统首先从 Markdown 文件读取稳定知识字段（通过现有的 MarkdownWriter.parseContent 解析 frontmatter 和正文），然后从内存中的 scoringMap 查找该条目对应的 ScoringRecord。若找到，则使用侧车存储中的 scoring 数据；若未找到（如新克隆仓库、侧车文件缺失），则使用默认 scoring（importance=50, maturity='draft', recency=1, accessCount=0, updateCount=0）。

融合后的完整条目数据用于后续的 compoundScore 计算、manifest 重要性排序、及档案候选判断。对外接口（如 search-knowledge-service、manifest-service、archive-service）无需感知数据来源差异——它们始终接收已融合的完整数据。这一层抽象由新增的 ScoringProvider 模块实现，该模块封装了「从 Markdown 读稳定字段 + 从侧车读动态信号」的合并逻辑。

### 写时分离机制

写入操作根据变更性质自动路由到不同存储介质。当用户执行 curate（新增/编辑/合并知识条目）时，MarkdownWriter.generateContext 仅写入稳定知识字段到 .md 文件的 YAML frontmatter 和正文中，不再写入 importance、recency 等 scoring 字段。同时，curate 操作会通过 recordCurateUpdate 更新侧车存储中对应条目的 scoring（增加 updateCount、提升 importance、重置 recency 为 1.0、更新 updatedAt）。

当用户执行搜索（search_knowledge）时，search-knowledge-service 不再将 accessCount 和 importance 增量写入 Markdown 文件的 frontmatter。取而代之，命中累加器（pendingAccessHits Map）在累积计数后，通过 flushAccessHits 将更新批量写入侧车存储的 scoring.json，而非逐个写入 .md 文件。这从根本上消除了检索操作对 Git 工作树的污染。

### 迁移期兼容策略

为兼容已存在 scoring 字段的旧版知识文件，系统引入迁移期兼容策略。迁移过程分为两个阶段：提取阶段和剥离阶段。在提取阶段，系统在首次启动或首次索引构建时，扫描所有 .md 文件的 frontmatter，检测是否包含 importance/recency/accessCount/updateCount/maturity 等字段。若检测到这些字段且侧车存储中尚无该条目的记录，则解析 frontmatter 中的 scoring 值写入侧车存储，并标记该条目为「待剥离」。在剥离阶段，系统对标记条目执行 Markdown 文件重写，从 frontmatter 中移除 scoring 字段，仅保留稳定知识字段。剥离操作是幂等的——重复执行不会产生副作用。

在迁移窗口期内（用户尚未剥离旧 frontmatter 中的 scoring 字段），ScoringProvider 的读取优先级为：侧车存储中的记录优先；若侧车中无记录但 frontmatter 中有 scoring 字段，则使用 frontmatter 中的值并触发后台提取写入侧车。这确保了在迁移完成前系统行为不受影响。迁移完成后，Markdown 文件的 diff 仅显示 scoring 字段的移除，不涉及知识内容的变更。

### 并发更新与冲突隔离

本方案从根本上消除了因动态信号写入引起的两类并发问题。第一类问题是本机并发写入侧车存储：侧车存储的写入路径为单线程异步批处理——pendingAccessHits 的累积和 flushAccessHits 的刷盘由 search-knowledge-service 内部的事件循环串行执行；curate 操作的 scoring 更新在写入 .md 文件的事务内同步更新内存中的 scoringMap，并在下一次批量刷盘时统一持久化。这避免了侧车文件的并发写入冲突。

第二类问题是跨机器通过 Git 的并发冲突：由于 .md 文件不再包含频繁变化的 scoring 字段，团队成员在不同机器上的日常检索操作不会产生任何 .md 文件的变更。仅当团队成员有意编辑知识内容（curate）或执行 pull/merge 操作时，.md 文件才会发生变更。这大幅降低了 Git 冲突的概率——冲突现在仅发生在真正的知识内容编辑冲突上，而非 scoring 数字的竞争更新上。每台机器的侧车存储完全独立，不存在跨机器合并需求。

### 失败隔离与降级

侧车存储被设计为「尽力而为」组件，其故障不影响知识内容的完整性和系统核心功能。具体降级策略如下：若 scoring.json 文件损坏或解析失败，系统记录警告日志并使用空 scoringMap 启动，所有条目回退到默认 scoring 值（importance=50, maturity='draft', recency=1）。若刷盘写入失败（如磁盘满），系统保留内存中的 scoringMap 继续服务，并在下一次刷盘周期重试；连续失败超过阈值后降级为「仅内存模式」，不再尝试持久化。若 Markdown 文件读取失败，按现有错误处理逻辑跳过该文件。

侧车存储与 Markdown 文件之间不存在事务性耦合关系。curate 操作写入 Markdown 文件成功但 scoring 更新失败时，仅意味着本次 curate 的 scoring 激励丢失，知识内容本身已安全持久化。反之，若 scoring 更新成功但 Markdown 写入失败，Markdown 文件回滚后侧车中的 scoring 记录将在下一次 decay 计算或索引重建时自然调整，不产生数据不一致。

### 归档/剪枝/合并时的清理一致性

当知识条目被归档（archiveEntry）、删除（sync delete）或合并（mergeContexts）时，侧车存储中的对应 scoring 记录必须同步清理，避免「孤儿记录」累积。每种操作的清理规则如下：

归档操作：调用 archiveEntry 将原始 .md 文件替换为 _archived/ 下的 .stub.md + .full.md 后，同步删除侧车存储中原始路径的 scoring 记录。归档桩（.stub.md）由 archive stub frontmatter 携带静态元数据（evicted_at、evicted_importance、original_path 等），不产生运行时 scoring 更新。归档桩的搜索权重由 evicted_importance 静态决定，不参与 compoundScore 动态计算。

同步删除操作：在 sync（pull/merge）流程中，当远程快照指示某文件应被删除时，FileContextTreeWriterService 或 FileContextTreeMerger 在删除 .md 文件后，同步调用 ScoringStore.delete(path) 移除侧车记录。合并操作：当 mergeContexts 将源文件和目标文件合并为一个新文件时，先分别读取两者的 scoring 记录，通过 mergeScoring 函数合并（importance 取最大值、recency 取最大值、accessCount 求和、updateCount 求和加一），将合并结果写入新路径的侧车记录，再删除旧路径的侧车记录。

为应对清理过程中可能发生的崩溃，系统在每次索引重建（buildManifest）时执行一次「孤儿清理」：遍历侧车存储中的所有路径，检查对应 .md 文件（或其归档桩）是否仍存在于 context-tree 目录中；若不存在，则移除该记录。此机制作为安全网，确保侧车存储不会无限膨胀。

### 技术效果

本方案在保持知识文件完全可读、可审阅、可进行 Git diff 和合并的前提下，实现了运行时动态信号与知识内容的存储分离。具体技术效果包括：第一，消除检索噪声——日常搜索操作不再产生 .md 文件变更，Git 工作树仅在知识内容被有意编辑时才变脏，从根本上解决了「检索即污染」的问题。第二，降低合并冲突——团队成员在多机并行使用场景下，.md 文件的冲突仅源于真正的知识编辑冲突，消除了因 scoring 字段并发更新导致的虚假冲突。第三，故障容错——侧车存储的损坏或丢失不影响知识内容的完整性，系统可降级为默认 scoring 继续运行。第四，迁移平滑——迁移期兼容策略使旧版知识文件无需一次性批量转换，可渐进式完成剥离。

### 实现改造点

本方案涉及的关键实现改造点如下：（1）新增 ScoringStore 模块（server/infra/scoring/），封装侧车存储的加载、查询、更新、删除和批量刷盘逻辑；（2）新增 ScoringProvider 模块，封装读时融合逻辑，作为 Markdown 稳定字段和侧车动态信号之间的合并层；（3）修改 MarkdownWriter.generateContext 和 updateScoringInContent，使其不再生成或更新 scoring 字段到 frontmatter 中——generateContext 仅输出稳定的知识 frontmatter 字段；（4）修改 search-knowledge-service 的 flushAccessHits 方法，将写入目标从 .md 文件的 frontmatter 切换为 ScoringStore；（5）修改 curate-tool 的 recordCurateUpdate 调用路径，使其更新 ScoringStore 而非写入 .md frontmatter；（6）修改 archive-service、merger、writer-service 的相关流程，在文件生命周期变更时同步清理侧车记录；（7）在 .gitignore 中添加 .brv/runtime/ 目录。

### 风险与待确认问题

以下为当前方案需要注意的风险点和待确认问题：（1）跨机器 scoring 不可比风险——由于每台机器的 scoring 数据独立演进，同一知识条目在不同机器上的 importance 可能差异显著。对于需要跨机器一致的决策（如自动归档），建议以 Markdown 中可选的静态 maturity 标注为准，动态 scoring 仅作为本机排序参考。（2）scoring.json 文件体积增长——随着知识条目数量的增加，scoring.json 可能变得较大。建议监控文件大小并在超过阈值（如 10MB）时考虑分片存储或仅在索引重建时全量加载。但基于当前 context-tree 的规模预期（通常数百至数千条目），单一 JSON 文件的体积可控。（3）冷启动时的 scoring 丢失——新克隆仓库或清除本机 .brv/runtime/ 后，所有条目的 scoring 回退到默认值，导致排序质量暂时下降。经过一段时间的正常使用后，scoring 会自然收敛。可考虑提供「从远程同步 scoring 种子」的可选功能，但不作为核心方案的一部分。（4）抽象摘要（.abstract.md）的 scoring 归属——.abstract.md 是 .md 文件的派生摘要，其检索排序应使用原始 .md 文件的 scoring。需要在 ScoringProvider 中建立 .abstract.md → 原始 .md 的映射关系。
