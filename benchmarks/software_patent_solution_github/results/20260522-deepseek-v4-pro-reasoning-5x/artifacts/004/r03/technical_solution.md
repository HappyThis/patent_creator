## 技术方案

本方案针对 ByteRover context tree 知识资产的团队协作与长期维护场景，提出一种将知识文件拆分为共享知识层与运行时信号层的双层存储架构。共享知识层以 Markdown 文件形式保留在版本控制中，维持可审阅、可 diff 的特性；运行时评分信号（importance、recency、accessCount、updateCount、maturity、updatedAt 等）迁出至独立的信号存储，不再因频繁更新而污染 Markdown 文件的版本历史。系统通过读时融合机制将两层数据组合为完整视图，保证搜索排序、归档判断等下游功能不受影响。

### 技术问题

ByteRover 的 context tree 以 Markdown 文件（.md）存储项目知识条目，文件 YAML frontmatter 中同时承载两类信息：一是标题、摘要、标签、关键词、关系链接等知识内容字段；二是基于 FinMem 模型的生命周期评分字段，包括 importance（重要性评分 0-100）、recency（新近度 0-1）、accessCount（搜索命中次数）、updateCount（策展更新次数）、maturity（成熟度：draft/validated/core）、createdAt（创建时间）和 updatedAt（最近更新时间）。

评分引擎在每次搜索命中时调用 recordAccessHit 增加 importance 和 accessCount，每次策展更新时调用 recordCurateUpdate 增加 importance 和 updateCount 并重置 recency 为 1.0，时间衰减函数 applyDecay 按指数规律降低 importance 和 recency，合并操作 mergeScoring 也会重新计算 importance、maturity 等字段。这些操作都会触发 Markdown 文件的重写。

在团队协作场景下，评分更新导致 Markdown 文件频繁产生无意义的 diff：文件内容本身的知识信息并未变化，仅仅是重要性从 53 变为 56、accessCount 从 12 变为 13 等。这些噪声提交污染版本历史、增加同行审阅负担，并在多人并行工作时显著提升合并冲突概率——两方各自独立触发的评分更新会因修改同一 frontmatter 区域而产生冲突。同时，真正的知识内容变更（如补充了新的关系链接或修正了摘要）被淹没在大量评分噪声中，难以快速定位。

### 核心方案：双层存储架构

核心方案是将当前 Markdown frontmatter 中的所有字段按变更频率和语义拆分为两层：

- 共享知识层（保留在 Markdown 中，纳入版本控制）：title（标题）、summary（摘要）、tags（标签）、keywords（关键词）、related（关系链接）。这些字段由人工策展或 AI 知识提取产生，变更语义明确、频率低，适合团队审阅、diff 和合并。
- 运行时信号层（迁出至独立信号存储，不纳入版本控制）：importance、recency、accessCount、updateCount、maturity、createdAt、updatedAt。这些字段由系统自动维护，变更频繁且不以知识内容变化为前提，迁出后不再触发 Markdown 文件改写。

信号存储采用文件级 JSON 格式，每个知识条目对应一个独立的 JSON 信号文件，存放在 .brv/runtime/ 目录下，通过 .gitignore 排除出版本控制。该路径与 context tree 目录（.brv/context-tree/）平级，两者在文件系统上隔离但在逻辑上通过 fileId 关联。

系统上层组件通过统一的 ContextReader 读取知识条目，ContextReader 同时访问 Markdown 文件和对应的信号 JSON 文件，在内存中融合为完整视图后返回。写入路径上，共享知识变更只写 Markdown，评分变更只写信号 JSON，两层互不干扰。

### 信号存储设计

信号存储（RuntimeStore）的设计遵循以下原则：与 Markdown 文件一一对应、可独立读写、写入操作原子化、读取失败可降级。

存储位置与命名：每个知识条目 fileId（即 Markdown 文件在 context tree 中的相对路径，如 auth/jwt-tokens/refresh-flow.md）对应信号文件 .brv/runtime/auth/jwt-tokens/refresh-flow.json。路径镜像方案保证通过 fileId 可直接定位信号文件，无需额外索引。

JSON 条目结构如下：

- fileId: 字符串，对应的 Markdown 文件相对路径，用于交叉校验。
- importance: 数值，当前重要性评分，范围 0-100。
- recency: 数值，当前新近度，范围 0-1。
- accessCount: 整数，累计搜索命中次数。
- updateCount: 整数，累计策展更新次数。
- maturity: 字符串枚举（draft/validated/core），当前成熟度。
- createdAt: ISO 时间戳，条目首次创建时间。
- updatedAt: ISO 时间戳，最近一次评分更新时间。
- _version: 整数，乐观锁版本号，每次写入递增。

选型说明：采用 JSON 文件而非 SQLite 等嵌入式数据库，是为了保持可审阅性——JSON 文件可用任何文本编辑器打开查看，与 Markdown 知识文件的可审阅理念一致；当条目数量超过数千时，可配置切换为 SQLite 后端。JSON 文件的独立性也避免了单一大文件写入带来的锁争用。

### 读时融合与写时分离

ContextReader 是双层架构的核心融合组件，所有需要完整知识视图的上层模块（搜索服务、manifest 构建、归档候选扫描、查询注入等）统一通过 ContextReader 获取数据，不再直接读取 Markdown 文件。

读取流程：

1. 根据 fileId 读取 Markdown 文件，解析 frontmatter 获得共享知识字段（title、summary、tags、keywords、related）和正文内容。
2. 根据 fileId 构造信号文件路径，读取 JSON 信号文件。若 JSON 文件不存在、格式损坏或读取失败，进入降级路径：尝试从 Markdown frontmatter 中读取旧格式评分字段（兼容未迁移条目），若仍无则使用默认值（importance=50、recency=1、maturity=draft）。
3. 将共享知识字段与信号字段合并为统一的内存 Summary 对象。对于 maturity，当 Markdown 中存在人工标注的 maturity 值（通过策展流程显式设置）时，人工值优先于信号存储中自动计算的值。
4. 调用 memory-scoring 的 compoundScore 函数实时计算复合评分，用于搜索排序——复合评分不作为持久化字段，每次读取时基于当前 importance、recency、maturity 动态计算。

写入分离：

1. 共享知识写入：策展工具（curate-tool）产生的知识内容变更（新增标题、修改摘要、添加标签/关系等）仅写入 Markdown 文件，通过 MarkdownWriter.generateContext 完成，不触及信号存储。
2. 评分写入：memory-scoring 模块的 recordAccessHit、recordCurateUpdate、applyDecay 等函数在计算新的评分值后，调用 RuntimeStore.update 写入对应的 JSON 信号文件。写入采用原子操作（先写临时文件，再 rename），不修改 Markdown 文件。
3. 快照与同步：现有 snapshot 服务和 CoGit sync 流程仅跟踪共享知识层的 Markdown 文件（通过 derived-artifact 的 isExcludedFromSync 排除信号文件），因此评分更新不会产生 snapshot 变更，也不会触发 push/pull/merge 流程。

### 迁移期兼容机制

系统从单层 Markdown 存储迁移到双层架构时，需保证已有知识资产不受影响，迁移过程不要求停机。

三层读取优先级：ContextReader 在读取时遵循 RuntimeStore JSON → Markdown frontmatter 旧字段 → 默认值的降级路径，确保任一阶段的数据存在即可正常工作。首次读取一个尚未迁移的条目时，若 Markdown frontmatter 中存在评分字段而 RuntimeStore 中无对应 JSON，ContextReader 自动触发懒迁移：提取 Markdown 中的评分字段写入 RuntimeStore JSON，同时在 Markdown frontmatter 中追加 _scoring_migrated: true 标记。后续该条目的评分更新将仅写入 RuntimeStore，不再更新 Markdown。

批量迁移工具：提供独立命令执行全量迁移，读取所有 context tree 条目，提取评分字段写入 RuntimeStore，并更新 Markdown frontmatter 中的迁移标记。支持 --dry-run 预览和 --rollback 回滚（从 RuntimeStore 反向写回 Markdown frontmatter）。

解析兼容：summary-frontmatter 和 markdown-writer 的 frontmatter 解析函数保持不变，继续兼容旧格式中的评分字段；当解析到已迁移标记时，跳过前端旧评分字段，从 RuntimeStore 获取。新创建的条目直接使用双层格式，Markdown frontmatter 中不再包含评分字段。

### 并发更新与失败隔离

在多进程/多用户环境中，同一知识条目可能被并发访问或更新（例如搜索触发 accessHit 的同时，策展流程在执行 curateUpdate）。信号存储通过以下机制保证数据一致性：

乐观锁并发控制：每个信号 JSON 文件包含 _version 字段。RuntimeStore.update 操作执行时，先读取当前文件内容获取 _version，计算新值后以 compare-and-swap 方式写入——仅当当前文件的 _version 与读取时一致时才执行写入，否则表示存在并发修改。写入成功时 _version 递增。冲突时重试最多 3 次，每次重试重新读取最新值和 _version；3 次均失败则放弃本次更新并记录日志，由下次读取时的实时计算弥补（见降级策略）。

文件锁辅助：对于同一进程内的并发写入，使用 per-file 互斥锁（{fileId}.lock 文件，5 秒超时防死锁）作为轻量级补充。由于每个知识条目对应独立的 JSON 文件，不同条目的评分更新完全并行、互不阻塞。

失败隔离与降级：

- 信号存储读取失败：Markdown 文件完好时，ContextReader 降级到 Markdown frontmatter 旧字段或默认值，搜索排序和归档判断仍可继续，仅评分精度暂时降低。
- 信号存储写入失败：评分更新操作失败时记录错误日志，不重试（避免雪崩）；下一次读取时，applyDecay 基于最后一次已知的 updatedAt 和当前时间估算衰减后的 importance 和 recency，保证评分大致可用。
- Markdown 写入不受信号存储状态影响：策展产生的知识内容变更始终正常写入 Markdown 文件。
- 信号存储整体不可用（如目录权限错误、磁盘满）：系统以降级模式运行，所有条目的评分均使用默认值，搜索按纯 BM25 相关性排序，归档功能暂停（无 importance 数据无法判断候选）。

### 生命周期操作的一致性保证

双层架构下，归档、合并、剪枝等生命周期操作需要同时维护 Markdown 文件和信号文件的对应关系，保证不会出现孤儿信号或僵尸条目。

归档操作（archive）：当 archive-service 的 findArchiveCandidates 通过 ContextReader（已融合 importance）筛选出低重要性候选条目后，执行归档时：将 Markdown 文件移至 _archived/ 目录并生成 .stub.md 和 .full.md；对应的信号 JSON 文件同步移至 _archived/.brv-runtime/ 子目录，保留完整的评分历史。归档桩（stub）的 frontmatter 中记录 evicted_importance（归档时的重要性快照），但不随运行时继续更新。恢复（drillDown + restoreEntry）时，信号 JSON 文件随 Markdown 文件一同恢复到原始位置。

合并操作（merge）：CoGit 的 push/pull/merge 流程通过 derived-artifact 的 isExcludedFromSync 函数排除信号存储目录，因此评分文件不参与远程同步和合并。这意味着：同一知识条目在不同机器上的评分数据各自独立维护（符合其本机运行时语义）；Markdown 合并冲突因评分字段不再出现在 frontmatter 中而显著减少；合并完成后，各机器的本地评分数据保持不变，无需额外清理。

剪枝与清理：

- 孤儿信号清理：定期扫描 .brv/runtime/ 目录，对每个 JSON 条目检查其对应的 Markdown 文件是否存在；若 Markdown 文件已被删除且未在 _archived/ 中找到对应桩文件，则删除该孤儿信号 JSON。清理操作在后台低频率执行（如每次 daemon 启动时），不影响正常读写。
- 条目删除：当用户显式删除某个知识条目（Markdown 文件）时，同时删除对应的信号 JSON 文件；若信号文件删除失败，记录警告但不回滚 Markdown 删除——孤儿清理流程会在后续回收。
- query-log 不受影响：查询日志（.brv/query-log/）独立于信号存储，其条目记录搜索时的 matchedDocs（含路径和 BM25 分数），不依赖信号存储的实时状态。

### 技术效果

本方案在保持 ByteRover context tree 知识资产可共享、可审阅、可版本控制的前提下，解决了运行时评分信号频繁改写 Markdown 文件引发的一系列问题，具体技术效果如下：

- 版本控制噪声消除：评分更新（搜索命中、策展更新、时间衰减）仅写入信号 JSON 文件，不再修改 Markdown 文件。Git 工作区的 diff 仅反映真实的知识内容变更，commit 历史可读性大幅提升。
- 合并冲突减少：Markdown frontmatter 不再包含动态评分字段，团队多人并行策展同一知识条目时，frontmatter 区域的冲突概率显著降低。估算合并冲突可减少至原方案的 5-10%。
- 知识变更可追溯：由于评分噪声被移除，每次 Markdown 文件变更都对应真实的知识内容演化（如新增关系、修正摘要、补充关键词），便于团队通过 git log 快速定位知识变更历史。
- 检索质量不降低：读时融合机制保证搜索排序仍使用完整的 importance、recency、maturity 信号，compoundScore 的计算逻辑不变。信号文件单独存储不影响 BM25 相关性+评分权重的复合排序效果。
- 归档判断保持准确：archive-service 通过 ContextReader 读取融合后的 importance，归档候选筛选逻辑不变；信号文件独立存储后，归档操作同步迁移信号数据，不丢失评分历史。
- 本机信号独立性：不同机器上的评分数据各自独立维护，符合 accessCount（本机检索命中次数）的本机语义。团队共享的知识内容通过 Markdown + CoGit 同步，各机器基于本地使用模式独立积累评分信号。
- 降级鲁棒性：信号存储故障不影响知识内容的可读性和可编辑性，系统以降级模式运行，保证核心功能不中断。
