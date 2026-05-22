## 技术方案

### 技术问题概述

在 ByteRover 的 context tree 系统中，项目知识以 Markdown 文件形式存储在 .brv/context-tree/ 目录下，并通过 brv vc（类 Git 版本控制）在团队间共享。每个知识条目的 Markdown 文件头部包含 YAML frontmatter，其中既存储了可共享的知识内容字段（title、summary、tags、keywords、related），也存储了动态运行时信号字段（importance、recency、accessCount、updateCount、maturity、createdAt、updatedAt）。这些动态信号会在每次查询命中（accessCount 递增、importance 加分）、每次 curate 更新（recency 重置为 1、importance 加分）以及时间衰减计算（importance 和 recency 随时间指数衰减）时被重写回 Markdown 文件，导致共享知识文件频繁产生非内容性的版本差异，污染版本历史、增加团队合并冲突概率，并使真正的知识内容变化被噪声淹没。

### 整体架构

本方案提出一种双层存储架构，将知识条目的可共享内容与动态运行时信号分离到两个独立的存储层：（1）共享 Markdown 层，仅保留标题、摘要、标签、关键词、关联关系和正文内容等可审阅、可版本对比的结构化知识内容，继续纳入 brv vc 版本控制；（2）本地信号层，在每个项目的 .brv/context-tree/ 下新增一个 .signals/ 目录，以 JSON 文件存储每个知识条目对应的动态信号（importance、recency、accessCount、updateCount、maturity、时间戳），该目录通过 .gitignore 排除出版本控制。

系统在读取路径上实现融合层：当需要查询、排序、归档判断或清理决策时，融合层同时读取共享 Markdown 内容和本地信号文件，将两组数据合并为完整的知识条目视图。在写入路径上，curate 等知识内容变更仅写入共享 Markdown 文件；query 命中、衰减计算等信号变更仅写入本地信号文件，两个写入路径互不交叉，从根本上避免动态信号污染共享文件的版本历史。

### 字段分离策略

系统将知识条目的前端元数据字段按照「是否随知识内容语义变化」的准则划分为两类。

第一类为可共享内容字段，继续保留在 Markdown frontmatter 中：title（条目标题）、summary（摘要文本）、tags（标签列表）、keywords（关键词列表）、related（关联条目路径列表）。这些字段的值由人工审阅或 LLM curate 过程产生，变化频率低，且每次变化都代表知识内容的真实语义更新，适合纳入版本控制供团队 diff 审阅。

第二类为动态运行时信号字段，迁出到本地信号文件：importance（重要性评分，0–100）、recency（新近度，0–1 的指数衰减值）、accessCount（累计查询命中次数）、updateCount（累计 curate 更新次数）、maturity（成熟度 tier：draft / validated / core）、createdAt 和 updatedAt（时间戳）。这些字段由系统在每次查询命中、curate 更新、dream 后台整理或时间衰减计算时自动更新，变化频率高且与知识内容语义无关，迁出后不再写入共享 Markdown。

### 本地动态信号存储

本地信号文件存储于 .brv/context-tree/.signals/ 目录下，与被代理的知识条目 Markdown 文件保持路径对应关系。例如，知识条目 auth/jwt-tokens/refresh-flow.md 对应的信号文件为 .signals/auth/jwt-tokens/refresh-flow.signal.json。

每个信号文件为一个扁平 JSON 对象，包含以下字段：

- importance：number，重要性评分，默认 50
- recency：number，新近度，默认 1.0
- accessCount：number，累计查询命中次数，默认 0
- updateCount：number，累计 curate 更新次数，默认 0
- maturity："draft" | "validated" | "core"，成熟度 tier，默认 "draft"
- createdAt：string，ISO 8601 时间戳
- updatedAt：string，ISO 8601 时间戳
- contentHash：string，对应 Markdown 文件的 SHA-256 内容哈希，用于检测外部内容变更导致信号是否需要重置

.signals/ 目录在 context tree 的 .gitignore 中追加排除规则，确保所有信号文件不被 brv vc 追踪，不会进入 push/pull/merge 流程，不会在团队间同步。每个团队成员的本机运行实例维护各自独立的信号文件副本，反映本机的查询使用模式。

### 读写融合机制

系统在以下场景需要消费知识条目的完整视图（内容 + 信号）：BM25 检索排序、manifest 车道预算分配、归档候选筛选、dream 后台整理（consolidate/prune/synthesize）、查询结果上下文注入。

融合层在读取路径上实现统一的解析入口 parseEntryWithSignals(path)：

1. 读取共享 Markdown 文件，解析其 YAML frontmatter 和正文内容，提取 title、summary、tags、keywords、related、正文各节（Raw Concept、Narrative、Facts、snippets）。
2. 计算 Markdown 文件的内容哈希（SHA-256）。
3. 尝试读取对应的 .signals/ 下的信号文件。若文件存在且 contentHash 与当前 Markdown 内容哈希一致，直接采用信号文件中的动态字段值。
4. 若信号文件不存在，或 contentHash 不匹配（说明 Markdown 内容已通过 curate 或 merge 被外部修改），则执行信号初始化：对全新条目应用默认信号值（importance=50、recency=1、maturity=draft 等）；对 contentHash 不匹配的已有条目，保留原 importance 和 maturity（因为它们是长期积累的知识价值指标），但重置 recency 为 1 并更新 contentHash。
5. 若信号文件读取失败（IO 错误、JSON 解析错误），采用默认信号值并使流程继续（fail-open）。
6. 将解析后的内容字段与信号字段合并为完整的条目视图返回给调用方。

信号文件的写入发生在以下时机：query 执行后对命中条目调用 recordAccessHit() 更新 accessCount 和 importance；curate 执行后对涉及条目调用 recordCurateUpdate() 更新 updateCount、importance、recency 和 updatedAt；dream 后台整理的 consolidate 合并操作后调用 mergeScoring() 更新合并后条目的信号；时间衰减计算在读取时实时计算并写入更新后的信号值。

### 迁移期兼容

对于升级前已存在、在 Markdown frontmatter 中直接包含 importance、recency、accessCount 等动态字段的旧格式知识条目，系统采用渐进式迁移策略，无需一次性批量转换。

迁移流程如下：（1）在读取旧格式条目时，若发现 Markdown frontmatter 中存在动态信号字段，则将其提取并写入对应的 .signals/ 信号文件，同时保留 Markdown 文件中的原有内容字段不变，但标记该条目为「已迁移」状态（通过在信号文件中设置 migrated: true）。（2）下一次该条目因 curate 更新需要重写 Markdown 文件时，Markdown 写入路径检查信号文件中的 migrated 标志，若为 true，则在生成新的 Markdown frontmatter 时仅输出内容字段，不再输出动态信号字段。若 migrated 为 false（尚未迁移），则保持兼容模式：同时将动态信号写入 Markdown frontmatter 和信号文件两面。（3）对于从未被读取或更新的旧条目，其 Markdown 中仍保留原有动态信号字段，系统在读取时能同时从 frontmatter 和信号文件中获取信号值，以两者中信号文件的值优先（若信号文件存在且 contentHash 匹配）。这样确保在迁移过渡期内，新旧格式条目共存且均能正常工作。

### 并发更新与冲突避免

由于内容写入和信号写入分别针对不同的存储文件，两者天然不发生文件级写冲突。但在分布式团队场景中，一个团队成员通过 brv vc pull/merge 获得另一个成员的知识内容更新时，其本地的信号文件可能包含针对旧版本内容积累的动态信号。

系统通过 contentHash 机制处理此问题：每个信号文件中存储对应 Markdown 文件的内容哈希。在读取融合时，若发现 contentHash 不匹配，说明共享 Markdown 内容已通过版本控制同步发生变更。此时系统执行"信号继承"策略：保留 importance 和 maturity（它们反映知识条目的长期价值，不应因内容更新而丢弃），重置 recency 为 1.0（因为内容刚被更新），将 accessCount 和 updateCount 保持不变（它们反映的是本机的使用历史），更新 contentHash 为新值。信号文件的写入采用原子写策略（先写临时文件，再 rename 到目标路径），避免并发读取时读到半写状态。

### 失败隔离

本地信号存储层的故障（磁盘满、权限错误、JSON 解析失败）不应影响系统的核心功能——知识查询、检索和共享。

系统在以下环节实现 fail-open 策略：（1）信号文件读取失败时，返回默认信号值（importance=50、recency=1、maturity=draft），查询排序和归档判断降级为仅依赖默认值或 BM25 文本相关性，核心查询和 curate 功能不受影响。（2）信号文件写入失败时，本次信号更新（如 accessCount 递增）静默丢弃，不阻塞当前操作（查询响应、curate 完成），不重试写入以免产生阻塞延迟，信号丢失的影响限于该条目的排序权重略微偏离实际使用频率。（3）Markdown 内容文件始终是 knowledge 的 authoritative source，信号文件的损坏或丢失不会导致知识内容丢失。（4）后台 dream 整理任务在执行前通过 try-catch 包裹每个条目的信号操作，单个条目的信号失败不影响其他条目的整理流程。

### 归档、剪枝与合并时的清理一致性

当系统执行归档（archive）、剪枝（prune）或合并（consolidate/merge）操作删除或合并知识条目时，必须同步清理对应的信号文件，避免信号文件成为孤立垃圾。

归档操作：archiveEntry() 将低重要性知识条目从原路径移至 _archived/ 目录并生成 .stub.md 和 .full.md 文件。在原 Markdown 文件被删除的同时，对应的 .signals/ 信号文件也被删除。归档的 .stub.md 前端元数据中保留归档时刻的 importance 快照（evicted_importance），供后续 drill-down 恢复时参考，但不再维护动态信号更新。

剪枝操作：prune 通过 LLM 审查决定对过期条目执行 ARCHIVE。执行归档后同步删除信号文件。若决定为 KEEP（保留），系统通过更新信号文件中的 updatedAt 时间戳来刷新条目活跃度，但不修改共享 Markdown 文件。

合并操作：consolidate 执行 MERGE 动作时，两个源条目合并为一个目标条目，源条目的 Markdown 文件被删除。此时系统执行 mergeScoring() 逻辑合并两个源条目各自的信号数据（importance 取最大值、accessCount 和 updateCount 分别求和、maturity 取较高 tier、recency 取最大值），将合并后的信号写入目标条目的信号文件，同时删除两个源条目对应的信号文件。若合并后的目标条目是全新路径，同时创建新的信号文件；若目标条目已存在信号文件，则执行信号合并写入。

版本控制同步合并：brv vc merge 操作将远程内容变更写入本地 context tree。若远程删除了某条目，merge 流程在删除本地 Markdown 文件的同时，同步删除对应的信号文件。若远程新增了某条目但本地无对应信号文件，merge 后首次读取时通过融合层的信号初始化逻辑自动创建默认信号文件。

### 技术效果

与现有方式相比，本方案带来以下技术效果：

- 版本控制噪声消除：动态信号字段不再写入共享 Markdown 文件，brv vc 的 diff、commit、merge 流程仅反映知识内容的真实语义变更，团队成员的版本历史变得干净可读，合并冲突显著减少。
- 知识可审阅性保持：共享 Markdown 文件保留完整的结构化知识内容（标题、摘要、标签、关键词、正文），团队成员仍可通过标准 diff 工具审阅知识内容的每次变更，不需要特殊的数据库查询工具。
- 动态信号价值保留：importance、recency、accessCount 等信号继续在本机维护，BM25 + 信号复合排序、成熟度 tier 判定、重要性衰减、归档候选筛选等依赖动态信号的功能完全不受影响，只是信号的存储位置从共享文件迁移到本地文件。
- 迁移无中断：渐进式迁移策略使新旧格式条目可在过渡期内共存，不需要一次性批量转换全部历史数据，不强制团队成员同时升级客户端。
- 失败不扩散：本地信号存储层的故障被隔离在本机、本条目范围内，不影响知识内容共享、查询响应和团队协作，核心路径始终保持可用。
- 清理一致性保证：归档、剪枝、合并、版本控制同步等操作均包含对应的信号文件清理逻辑，避免孤立信号文件积累，保持存储空间整洁。

### 风险与待确认问题

信号文件数量增长：随着知识条目数量增加，.signals/ 目录下的 JSON 文件数量等比例增长，在超大项目（数万条目）中可能产生大量小文件。可通过定期将低频访问条目的信号文件合并为单一归档文件或使用嵌入式数据库（如 SQLite）替代独立 JSON 文件来缓解。

多机信号合并策略：同一团队成员在不同机器上对同一知识条目积累了不同的 importance 和 accessCount 信号。当前方案将这些信号视为本机私有数据不同步，这可能导致不同成员对同一条目的「重要性」认知不一致。可考虑在 brv vc push/pull 时附加一个可选的信号摘要交换机制，或依赖 cloud 端的聚合查询统计来弥补。

maturity tier 的团队共识：maturity（draft/validated/core）原本存储在共享 Markdown 中，可以作为团队对知识条目质量等级的共识。迁出到本机信号文件后，maturity 变为本机私有判断，团队不再有统一的成熟度视图。如果团队需要共享的成熟度评定，可考虑在 knowledge review 流程中由人工审核确定 maturity 并将结果写入共享 Markdown 的不可变字段，而本机信号中的 maturity 作为本机快速筛选的辅助参考。
