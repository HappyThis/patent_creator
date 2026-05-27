## 技术方案

### 技术问题概述

ByteRover 的 Context Tree（上下文树）以 Markdown 文件的形式存储项目知识，每个知识文件包含 YAML 前置元数据（frontmatter）和正文。前置元数据中同时承载两类信息：一是知识的语义描述字段（标题、标签、关键词、关联关系、摘要等），二是运行时动态信号字段（重要性评分 importance、新鲜度 recency、成熟度 maturity、检索命中次数 accessCount、策展更新次数 updateCount 等）。由于每次检索命中、策展更新或时间衰减都会修改这些动态字段的值，知识文件的文件哈希随之变化，导致：快照（.snapshot.json）频繁标记该文件为已修改；CoGit 同步（push/pull/merge）将这些频繁变化视为内容变更进行传输和合并；多人协作时不同成员的检索命中计数不同，引发大量不必要的合并冲突；版本控制系统的 diff 被评分数字的波动淹没，真正的知识内容变更难以审阅。

### 核心方案：静态知识与动态信号的分层存储

本方案提出一种静态知识内容与动态运行时信号的分层存储架构，核心思想是：将知识文件的 YAML 前置元数据中的运行时动态信号字段从 Markdown 文件中迁出，存入一个独立的、不受版本控制追踪的信号存储层；知识文件的 Markdown 正文和语义描述字段保持不变，继续作为可共享、可审阅、可版本控制的团队资产。在读取路径上，通过融合机制将两层的字段合并为完整视图，确保检索排序、清理判断和自动整理等下游功能不受影响。该方案在 ByteRover 现有的派生产物排除机制（derived artifact exclusion）和快照/同步架构基础上进行扩展，无需引入外部数据库，保持系统的零外部依赖特性。

### 知识内容字段与运行时信号字段的划分

基于对 ByteRover 当前 ContextData 接口和 MarkdownWriter 前置元数据生成逻辑的分析，本方案将知识文件的前置元数据字段划分为两类。

第一类，知识内容字段（保留在 Markdown 前置元数据中）：title（知识条目名称）、summary（摘要文本）、tags（分类标签）、keywords（检索关键词）、related（关联知识路径）、facts（结构化事实）、narrative（依赖关系、规则、结构等叙述性内容）、rawConcept（原始概念数据）、snippets（代码片段）、reason（知识存在原因）。这些字段表达的是项目知识的语义内容，变更频率低，属于团队协作中需要审阅和版本控制的资产。

第二类，运行时动态信号字段（迁入信号存储层）：importance（重要性评分，范围 0-100）、recency（新鲜度评分，范围 0-1 的指数衰减值）、maturity（成熟度等级：draft/validated/core）、accessCount（检索命中累计次数）、updateCount（策展更新累计次数）、createdAt / updatedAt（时间戳）。这些字段由系统在每次检索命中、策展更新、时间衰减计算时自动修改，变更频率远高于知识内容本身，是版本控制噪声的主要来源。其中 maturity 字段在迁出后，Markdown 前置元数据中保留一个可选的 declaredMaturity 字段，供团队成员手动声明知识条目的预期成熟度；信号存储层中的 maturity 由系统基于 importance 和 recency 的复合评分自动计算（通过 determineTier 函数），读取融合时以系统的自动计算结果为准。

### 运行时信号存储层设计

信号存储层采用单个 JSON 文件实现，文件路径为 .brv/context-tree/.signals.json，与现有的 .snapshot.json 和 _manifest.json 同属派生产物（derived artifact），由 .gitignore 排除出版本控制追踪。

文件结构为一个扁平映射表，以知识文件在上下文树中的相对路径为键，以信号对象为值。每个信号对象的字段包括：importance（数值）、recency（数值）、maturity（枚举字符串）、accessCount（整数）、updateCount（整数）、createdAt（ISO 时间戳）、updatedAt（ISO 时间戳）。对于已归档条目的存根（stub），信号存储层额外记录 originalPath、evictedAt 和 evictedImportance 字段，支持归档后的信号追溯。

信号的写入遵循以下规则：（1）检索命中时，更新对应路径的 accessCount 并调用 recordAccessHit 重新计算 importance，然后写入 .signals.json；（2）策展更新时，调用 recordCurateUpdate 更新 importance、recency、updateCount、updatedAt，并写入 .signals.json；（3）定期后台任务扫描所有条目，对距上次更新超过阈值天数的条目调用 applyDecay 进行衰减计算并更新 recency 和 importance。写入操作采用原子写入策略：先将新内容写入临时文件（.signals.json.tmp），再通过 rename 系统调用原子替换原文件，避免写入过程中系统崩溃导致文件损坏。

### 读取融合机制

在读取路径上，所有需要完整知识视图的下游模块统一通过一个新的 SignalFusionService 获取融合后的数据。该服务提供两个核心方法。

方法一：resolveEntry(relativePath)。读取知识文件的 Markdown 正文和前置元数据，同时从 .signals.json 中读取该路径的信号对象；将信号对象的字段与 Markdown 前置元数据的语义字段合并，返回一个同时包含语义信息和运行时信号的对象。若 .signals.json 中不存在该路径的信号记录，则以默认值填充（importance=50, recency=1, maturity='draft', accessCount=0, updateCount=0）。

方法二：resolveAllForManifest()。用于 FileContextTreeManifestService 构建清单时，对每个候选条目调用 resolveEntry，获取融合后的完整对象用于车道预算分配（lane budgeting）和排序。融合后的 importance 直接参与重要性排序，maturity 参与成熟度分级，确保迁出信号后的排序和分配行为与迁出前完全一致。

在 ByteRover 现有的 FileContextTreeManifestService.scanForManifest 方法中，原本直接调用 parseFrontmatterScoring(content) 从 Markdown 前置元数据中提取评分字段。改造后，scanForManifest 改为通过 SignalFusionService.resolveEntry 获取融合后的评分，自身的 parseFrontmatterScoring 调用仅用于兼容尚未迁移的旧格式文件（见迁移期兼容策略）。

### 迁移期兼容策略

为保证系统平滑升级，方案设计了迁移期兼容策略，确保在 .signals.json 尚未生成或只覆盖部分文件时，系统仍能正常运行。

首次启动迁移：系统启动时检测 .signals.json 是否存在。若不存在，遍历上下文树中的所有知识文件，读取其 Markdown 前置元数据中的 scoring 字段；若发现这些字段，将其值迁移写入 .signals.json，然后从前置元数据中移除 scoring 相关字段（importance、recency、maturity、accessCount、updateCount）并写回文件。迁移完成后生成 .signals.json。迁移过程为幂等操作——若 .signals.json 已存在，则跳过迁移。

混合模式读取：迁移期间或迁移未完成时，SignalFusionService.resolveEntry 采用"信号优先、Markdown 兜底"策略：优先从 .signals.json 读取信号；若信号存储中不存在对应条目，则回退到解析 Markdown 前置元数据中的 scoring 字段；若两者都不存在，使用默认值填充。这意味着未迁移的旧文件可以和新文件共存，系统行为不中断。

渐进迁移：策展（curate）操作触发知识文件重写时，由 MarkdownWriter 在生成新前置元数据时自动排除 scoring 字段，确保新写入的文件不再包含动态信号。同时，策展流程更新 .signals.json 中对应条目的 updateCount 和 updatedAt。这样，随着团队日常策展活动的进行，旧格式文件逐步被替换为新格式，无需执行全量一次性迁移。

### 并发更新与失败隔离

并发更新处理：由于 .signals.json 是单一文件，多个并发操作（如同一时刻的多次检索命中、策展任务、衰减任务）可能同时尝试写入，存在竞争条件。方案采用"最后写入者胜出但合并化"策略：每个写入者先读取当前 .signals.json 的完整内容，在内存中修改目标条目，再通过原子 rename 写回。若在读取和写入之间被其他进程修改（通过比较写入前的文件 mtime 或内容哈希检测），则重新读取、重新合并修改、重新写入，最多重试 3 次。对于检索命中这种高频操作，采用"累积-批量写入"优化：agent 进程内的 ContextTreeStore 在内存中累积 accessCount 增量，定期（如每 10 秒或累积 50 次命中后）批量合并写入 .signals.json，减少文件 I/O 竞争。

失败隔离：.signals.json 的损坏或不可用不应影响知识检索和上下文注入的基本功能。SignalFusionService 在读取 .signals.json 时捕获所有 I/O 异常和 JSON 解析异常，捕获后以默认信号值降级运行，同时记录告警日志。写入失败时（如磁盘满、权限不足），不阻塞知识文件的正常写入流程，信号更新被静默丢弃，下次成功的写入将覆盖丢失的更新。这种"信号层故障不影响知识层"的隔离设计，确保运行时信号存储作为增值而非关键路径存在。

多机协作场景：在 CoGit 同步（push/pull）流程中，.signals.json 被 isExcludedFromSync 函数排除在同步范围外，因此不同机器上的 .signals.json 相互独立。每台机器基于自身的检索使用模式独立累积信号，这恰好符合设计意图——检索频率和重要性判断是本机使用模式的真实反映，不应被远程机器的使用模式覆盖。团队共享的知识内容通过 Markdown 文件版本控制同步，每台机器的本地信号独立演化。

### 归档、剪枝与合并时的清理一致性

归档（archive）清理：当 FileContextTreeArchiveService 将低重要度知识条目归档到 _archived/ 目录时，除了现有的操作（写 .full.md 保留原文、写 .stub.md 生成可检索存根、删除原始文件），还需同步更新 .signals.json：将原始路径的信号记录转移到 stub 路径下，并在信号记录中补充 originalPath、evictedAt 和 evictedImportance 字段。转移后的信号记录保持与存根文件的生命周期一致——存根仍可被检索命中，因此其信号应继续累积。

恢复（restore）清理：当 restoreEntry 将归档条目恢复到原始路径时，将 .signals.json 中的信号记录从 stub 路径转移回原始路径，清除 originalPath、evictedAt、evictedImportance 等归档标记字段。

删除清理：当知识文件被明确删除（非归档，而是从上下文树中移除）时，从 .signals.json 中删除对应路径的信号记录。对于因远程同步删除（CoGit pull 发现远程已删除且本地未修改）导致的删除，FileContextTreeMerger.runMerge 在处理删除路径时同步清除 .signals.json 中的对应条目。

合并冲突清理：FileContextTreeMerger 在发生合并冲突时生成 _N.md 重命名文件（冲突保留文件），此时 .signals.json 中应将该路径的信号记录复制一份到 _N.md 对应的键下，确保冲突文件也能被信号层追踪。合并完成后，由用户手动解决冲突并删除 _N.md 文件时，对应的信号记录一并清除。

一致性保证：所有涉及知识文件生命周期变更的操作（归档、恢复、删除、合并冲突保留）通过一个统一的 SignalLifecycleHook 接口实现信号存储的联动更新。该接口在每次知识文件生命周期操作的事务性代码块中调用，信号存储更新与文件操作在同一 try-catch 块内执行。若信号存储更新失败，记录告警但不回滚文件操作——遵循"信号层故障不影响知识层"的隔离原则，信号不一致可在下次策展或定期一致性检查任务中修复。

### 技术效果

第一，版本控制噪声消除。运行时动态信号字段从 Markdown 前置元数据迁出后，知识文件的文件哈希不再因检索命中或时间衰减而变化。.snapshot.json 中标记为"已修改"的文件仅反映真正的知识内容变更，CoGit 同步传输和合并的数据量显著减少，版本控制 diff 中不再出现评分数字的波动。

第二，合并冲突减少。多人协作场景下，不同成员的本地检索命中计数不再引发知识文件的合并冲突。知识文件的合并冲突仅发生在真正的知识内容并行修改时，冲突解决聚焦于语义差异而非评分数字差异。

第三，检索排序与自动整理功能保持完整。通过 SignalFusionService 的读取融合机制，检索排序、成熟度判断、归档候选识别、清单车道分配等所有依赖运行时信号的下游功能，获得与迁出前等价的完整信号视图，功能行为不受影响。

第四，本机使用模式独立。每台机器的 .signals.json 独立演化，重要性评分真实反映本机的检索使用模式，不被团队其他成员的无关检索行为干扰。这使得重要性驱动的归档判断和上下文预算分配对每个使用者更加准确。

第五，零外部依赖，保持可审阅性。方案不引入外部数据库，运行时信号存储为单个 JSON 文件，其结构简单、可被任何文本编辑器打开查看。知识内容仍以 Markdown 文件形式存在，团队可在 GitHub、GitLab 等平台上直接审阅和讨论知识变更。

### 风险与待确认问题

风险点一：.signals.json 单文件写入竞争。在极高并发场景下（如大量并行检索命中），原子写入的重试机制可能导致短暂延迟。缓解措施：ContextTreeStore 的累积-批量写入已大幅降低写竞争频率；若未来并发度进一步增长，可考虑将 .signals.json 按知识域拆分（如 .signals-domain.json）以降低锁粒度，但当前方案对此做简化处理。

风险点二：.signals.json 在 CoGit 同步时被排除，不同机器上的信号独立演化。这虽然在设计上合理，但可能导致新加入团队的成员在首次 clone 后没有任何历史信号积累，所有条目的重要性从默认值 50 开始。缓解措施：可考虑在 CoGit push 时附带一个可选的摘要式信号快照（如仅包含 maturity 字段），供新成员初始化信号基线，但不包含原始计数。

风险点三：Markdown 前置元数据中 declaredMaturity 字段与信号存储层自动计算的 maturity 字段之间可能存在语义冲突。当前方案以系统自动计算结果为准；若用户期望手动声明的成熟度具有更高优先级，可在融合逻辑中调整优先级策略。

待确认点：需确认 ContextTreeStore（agent 侧）的 store/compact 流程与新的 .signals.json 写入路径的集成方式，特别是 compact 产生的 summaryHandle 是否需要关联信号记录。
