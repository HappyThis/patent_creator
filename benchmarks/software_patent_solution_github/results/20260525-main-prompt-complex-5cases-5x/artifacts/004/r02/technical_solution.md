## 技术方案

本方案提供一种将知识库文件的静态知识内容与运行时动态信号分层存储、独立维护并在检索时自动融合的软件机制，解决共享 Markdown 知识文件因高频动态信号写入而频繁变更导致的版本控制脏状态、团队合并冲突及知识变更淹没等问题。

### 1. 技术问题

在 ByteRover 上下文树（Context Tree）中，项目知识以 Markdown 文件形式存储在 .brv/context-tree/ 目录下，通过 CoGit 进行版本控制与团队共享。每个知识文件除标题、标签、关键词、正文等静态知识字段外，其 YAML 前置元数据（frontmatter）中还存储了 importance（重要性分数）、recency（新鲜度）、maturity（成熟度等级，draft/validated/core）、accessCount（检索命中次数）、updateCount（策展更新次数）、createdAt/updatedAt 等运行时动态信号。这些信号由检索命中、策展更新、时间衰减等系统操作频繁更新——每次知识检索命中都会累加 accessCount 并上调 importance，每次索引构建时都会将累积命中批量回写到磁盘。

这种设计导致三个问题：(1) 共享 Markdown 文件中混合存储了稳定的知识内容与高频变化的运行时信号——每次知识检索命中都会导致 git 提交记录中出现大量无意义的变更噪声，真正的知识内容变化被淹没；(2) 团队协作中，由于同一文件被多人各自检索触发动态信号更新，频繁产生合并冲突；(3) 用户不希望简单丢弃这些动态信号，因为它们对知识检索、排序、清理和自动整理仍然有价值。

### 2. 整体架构

本方案的核心思路是将知识文件拆分为两个独立存储层：(1) 共享知识层——继续以 Markdown 文件形式存储在 .brv/context-tree/ 中，受版本控制，包含标题、摘要、标签、关键词、关联关系及正文内容（Reason、Raw Concept、Narrative、Facts、Snippets 等章节），不含任何运行时动态信号；(2) 动态信号层——以本地唯一数据文件形式存储在 .brv/context-tree/.signals.json 中，不受版本控制（通过 .gitignore 排除），包含每个知识文件的 importance、recency、maturity、accessCount、updateCount、createdAt、updatedAt 等全部运行时信号。

系统的知识检索、排序、衰减计算、成熟度判断等流程不再直接读写 Markdown 文件的前置元数据，而是通过一个统一的「信号融合层」同时从共享知识层和动态信号层读取数据，合并后提供给上层消费者。写入操作也通过该融合层分流：静态知识变更写入 Markdown 文件并进入版本控制；动态信号变更写入 .signals.json 本地文件，不影响 git 状态。

### 3. 字段分类与分层策略

方案对现有的 YAML frontmatter 字段进行明确分类，将频繁变化的运行时信号与稳定的知识内容分离。

保留在共享 Markdown frontmatter 中的字段（低频变更，具团队审阅价值）：title（知识主题标题）、summary（摘要）、tags（标签列表）、keywords（关键词列表）、related（关联知识路径列表）。这些字段只在人工策展或 LLM 生成新知识时变更，频率低、语义明确，适合版本控制和团队 diff 审阅。

迁入动态信号层 .signals.json 的字段（高频变更，仅本机运行时相关）：importance（重要性分数，0–100，每次检索命中 +3、每次策展更新 +5）、recency（新鲜度，0–1，指数衰减计算）、maturity（成熟度等级 draft/validated/core，由重要性阈值和迟滞判定）、accessCount（累计检索命中次数）、updateCount（累计策展更新次数）、createdAt（知识创建时间戳）、updatedAt（最后策展更新时间戳）。这些字段随检索、衰减、策展等运行时事件频繁变化，不应触发版本控制变更。

### 4. 动态信号存储设计

动态信号层以单个 JSON 文件 .brv/context-tree/.signals.json 存储，文件结构为以知识文件相对路径为键的映射，每个键对应的值为该文件的运行时信号对象。

信号文件 .signals.json 被 .gitignore 排除，确保不被版本控制追踪。写入采用原子写入策略：先将更新内容写入 .signals.json.tmp 临时文件，完成后通过 rename 系统调用原子替换，保证文件不会处于半写状态。读取时先检查 .signals.json 是否存在，若不存在（首次使用或文件被清理）则从 Markdown 文件的现有 frontmatter 中提取信号字段作为初始值自动填充。

### 5. 读取融合机制

系统通过信号融合层提供统一的读取接口，对上层检索、排序、衰减计算、成熟度判断等模块透明。读取流程如下：

(1) 读取 Markdown 文件，解析 frontmatter 获取 title、summary、tags、keywords、related 等静态字段及正文内容。(2) 从 .signals.json 中按文件路径查找对应的动态信号（importance、recency、maturity、accessCount、updateCount、timestamps）。(3) 若 .signals.json 中不存在该文件条目（新文件或迁移场景），以解析 Markdown frontmatter 时发现的旧版信号字段作为默认值，并自动在内存中补全。(4) 在内存中合并两套数据，构造完整的 FrontmatterScoring 对象，通过现有的 compoundScore（复合评分）、applyDecay（时间衰减）、determineTier（成熟度判定）等纯函数进行检索排序计算。

### 6. 写入分流机制

写入操作根据变更类型自动路由到正确的存储层，上层调用者无需感知分层细节。

静态知识写入（策展生成新知识、更新正文、修改标签/关键词/关联关系）：直接写入 .brv/context-tree/ 下的对应 Markdown 文件，通过 MarkdownWriter.generateContext 生成不含 scoring 字段的 frontmatter 和正文，进入版本控制。动态信号写入（检索命中累加、策展更新计次、衰减刷新、成熟度重算）：仅更新内存中的 pendingAccessHits 映射或直接更新 .signals.json 文件，不触碰任何 Markdown 文件。其中检索命中采用批量延迟写入策略——每次检索将命中路径和次数累加到内存映射中，在下一轮索引构建（acquireIndex）时通过 flushAccessHits 一次性批量回写到 .signals.json，避免高频检索导致的写放大。

### 7. 迁移期兼容

方案设计了向前兼容的迁移机制，确保旧版 Markdown 文件（frontmatter 中包含 scoring 字段）在迁移后仍可正常工作。

首次启动信号分层功能时，系统检测 .signals.json 不存在，自动执行初始化迁移：(1) 遍历 .brv/context-tree/ 下所有 .md 文件；(2) 解析每个文件的 frontmatter，提取其中的 scoring 相关字段（importance、recency、maturity、accessCount、updateCount、createdAt、updatedAt）；(3) 将提取的信号写入 .signals.json；(4) 从 Markdown frontmatter 中移除 scoring 字段，仅保留静态知识字段，并将清理后的内容写回文件。迁移过程中若任一步骤失败（如文件被外部锁定），跳过该文件并在日志中记录，不阻塞整体迁移流程。旧版文件若无 frontmatter 或 scoring 字段，则以 applyDefaultScoring 的默认值（importance=50, maturity='draft', recency=1）初始化信号。

### 8. 并发更新与失败隔离

针对团队协作场景下 Markdown 文件同时被多人编辑、以及动态信号文件同时被多个本地进程（如多个 agent 进程并行检索）访问的并发场景，方案设计了以下机制：

(1) 共享知识层并发：Markdown 文件的并发编辑由版本控制系统（CoGit/Git）的合并机制处理。由于动态信号已迁出，同一文件的合并冲突概率大幅降低——仅当两个团队成员同时修改同一知识文件的标题、标签或正文时才会产生冲突，而过去每次检索都会产生的 scoring 字段冲突已完全消除。(2) 动态信号层并发：.signals.json 的并发写入通过文件级锁（使用已有的 async-mutex 机制）保护，确保同一时刻只有一个写入者更新信号文件。读取操作不加锁，允许在写入期间并发读取旧版本信号数据。(3) 失败隔离：动态信号写入失败（如磁盘满、权限不足）时，错误被捕获并记录日志，系统回退到使用内存中的未持久化信号值继续运行，不影响知识检索和策展的主流程。下次成功的写入会覆盖失败状态。

### 9. 归档/剪枝/合并时的清理一致性

当知识文件被归档（移入 _archived/ 目录并生成 .stub.md/.full.md）、剪枝（低重要性知识被删除）或合并（CoGit pull/merge 操作同步远程变更）时，动态信号层必须同步清理，避免 .signals.json 中积累孤立条目。

(1) 归档处理：当知识文件从 domain/topic/context.md 归档为 domain/topic/_archived/context.stub.md 和 .full.md 时，系统在归档执行器中追加信号迁移逻辑——将原路径的信号条目移动到新路径（.stub.md），保留该知识的检索命中历史和成熟度状态，而非丢弃。(2) 剪枝处理：当低重要性知识被剪枝删除（通过 Dream 子系统或策展清理流程）时，同步从 .signals.json 中删除对应条目。(3) 合并处理：在 CoGit pull/merge 流程的 snapshot diff 阶段（diffStates 函数），检测到文件被删除时，同步清理 .signals.json 中对应条目；检测到文件被重命名时，同步迁移信号条目到新路径。清理操作在 snapshot sync 的事务边界内执行，失败时不影响主合并流程——孤立信号条目仅占用微量存储空间，不会导致系统功能异常。

### 10. 技术效果

本方案通过将知识文件的静态内容与动态信号分层存储，获得以下技术效果：

(1) 版本控制噪声消除：动态信号全部迁入不受版本控制的 .signals.json，Markdown 文件的 git diff 仅反映知识内容本身的变更（标题、标签、正文等），高频检索不再污染版本历史。(2) 合并冲突显著减少：过去多人协作时，各自检索都会修改同一文件的 accessCount/importance 字段导致频繁冲突；分层后这些字段不再进入版本控制，合并冲突仅发生于真正的知识内容同时修改，概率大幅降低。(3) 知识变更可审阅性提升：团队在做 git log 或 pull request review 时，每次 diff 都对应一次有意义的知识策展操作，而非无意义的计数器更新，审阅效率和准确性显著提高。(4) 检索排序能力完整保留：动态信号层以独立文件维护全部运行时信号，compoundScore 复合评分、applyDecay 时间衰减、determineTier 成熟度判定等全部检索排序能力不受影响。(5) 迁移平滑无中断：旧版 Markdown 文件的 scoring 字段在首次启动时自动迁移至动态信号层并清理原文件，用户无感知。

### 11. 风险与待确认问题

(1) 信号文件丢失风险：.signals.json 若被误删，系统会从 Markdown 残留 scoring 字段（迁移前的旧文件）或默认值重建信号，但已累积的 accessCount 和 updateCount 将丢失，仅视为冷启动代价。(2) 多机同步：.signals.json 是本机文件不参与版本控制，不同机器上同一知识文件的信号独立演化，这与信号的本机运行语义一致，非缺陷。(3) 信号膨胀：随知识文件数量增长，.signals.json 可能变大。可通过定期清理孤立条目（引用路径已不存在的条目）控制文件大小，该清理在索引重建时自动执行。
