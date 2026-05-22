## 技术方案

本方案针对 ByteRover context tree 在团队协作场景下，知识文件（Markdown）中静态知识内容与动态运行时评分信号混存导致的版本控制污染、合并冲突噪音和知识变更淹没等问题，提出一种静态知识与动态信号分离存储、运行时按需融合的架构。方案在保持知识文件可共享、可审阅、可版本控制的前提下，将频繁变化的运行时信号迁移至侧车存储层，并通过读取融合、迁移兼容、并发控制、失败隔离和清理一致性机制，保障系统的检索排序、归档剪枝和长期维护能力。

### 技术问题概述

ByteRover context tree 的知识条目以 Markdown 文件（.md）存储在 .brv/context-tree/ 目录下，每个文件包含 YAML frontmatter 和 Markdown 正文。frontmatter 中同时存储两类性质截然不同的字段：（1）静态知识内容——title、summary、tags、keywords、related 等表达知识语义的字段；（2）动态运行时评分信号——importance、recency、accessCount、updateCount、maturity、createdAt、updatedAt 等随系统运行持续变化的字段。由于动态信号在每次搜索命中、策展更新或时间衰减后都需要写回 Markdown 文件的 frontmatter（通过 updateScoringInContent 重写整个 frontmatter 块），文件的内容哈希发生改变，导致基于内容哈希的 snapshot 差异检测将文件标记为 modified。在团队通过 Git 版本控制共享 context tree 的场景下，这种由动态信号变化引发的频繁改写造成三类问题：（a）版本控制状态频繁变脏，产生大量无实际知识变更的 commit 或 diff；（b）多人并行使用时的动态信号写入产生虚假合并冲突；（c）真正的知识内容变更被大量评分信号变更淹没，审阅者难以区分哪些是知识演进、哪些仅是运行时噪声。

### 核心方案：静态知识与动态信号分离存储

核心思路是将知识文件的静态内容与动态信号分离到两个存储层：（1）共享知识层（Shared Knowledge Layer）——Markdown 文件保留在 .brv/context-tree/ 下，仅包含静态知识字段和正文，继续通过 Git 版本控制进行团队共享和审阅；（2）本地信号层（Local Signal Layer）——动态评分信号迁移至一个不纳入版本控制的侧车存储（sidecar store），每个知识条目对应一条信号记录，由系统在运行时维护。两个存储层在文件系统级别解耦：Markdown 文件的读写不触发信号层变更，信号层的更新也不修改 Markdown 文件。

系统在检索排序、manifest 构建、归档剪枝等需要动态信号的场景中，通过读取融合器（Signal Fusion Reader）将两层数据合并，生成包含完整评分信息的逻辑视图供上层消费。写入路径上，知识内容变更（策展、合并等）仅写入 Markdown 文件；信号变更（搜索命中记录、衰减重算等）仅写入侧车存储。Snapshot 差异检测和版本控制同步仅覆盖共享知识层的 Markdown 文件，侧车存储通过 isExcludedFromSync 机制被排除在外。

### 字段分类策略

基于变更频率、语义属性和团队协作价值三个维度，将当前 frontmatter 中的字段分类如下：

- 保留在 Markdown 中的静态字段：title（知识条目名称）、summary（摘要）、tags（标签）、keywords（关键词）、related（关联条目路径）。这些字段仅在策展操作或人工编辑时变更，频率低，直接表达知识语义，是团队审阅和版本比较的核心对象。
- 迁移到侧车存储的动态信号：importance（重要度评分 0-100）、recency（新近度 0-1）、accessCount（累计访问次数）、updateCount（累计更新次数）、maturity（成熟度分级 draft/validated/core）、createdAt（创建时间）、updatedAt（最后更新时间）。这些字段随每次搜索命中、策展操作或时间衰减而频繁变化，不影响知识语义，仅服务于检索排序和生命周期管理。
- 正文部分（Raw Concept、Narrative、Facts、Reason、Snippets）保留在 Markdown 中不变，它们是知识条目的核心内容。

### 侧车存储设计

侧车存储位于 .brv/scores/ 目录下（该目录已通过 isExcludedFromSync 排除在 snapshot 和版本控制同步之外），采用以知识条目相对路径为键的扁平 JSON 文件组织。

存储结构：每个 context tree 项目维护一个 scores.json 文件，其顶层为 Map 结构——键为知识条目在 context-tree/ 下的相对路径（如 "auth/jwt-tokens/refresh-flow.md"），值为 ScoringRecord 对象。ScoringRecord 包含：importance（number）、recency（number）、accessCount（integer）、updateCount（integer）、maturity（enum: draft|validated|core）、createdAt（ISO 8601 string）、updatedAt（ISO 8601 string）。文件中不存在的条目视为使用默认值（importance=50, recency=1, maturity=draft）。

读写接口：提供原子性的 getSignals(path)、putSignals(path, record)、batchGetSignals(paths)、batchPutSignals(entries) 四个原语。写入时对整个 scores.json 采用写时复制 + 原子重命名策略：先将当前内容读入内存，修改目标条目，写入临时文件，然后 rename 覆盖，保证写入的原子性和崩溃安全。读取时直接解析当前 scores.json，对于未命中的路径返回默认值。

### 读取融合机制

读取融合器（SignalFusionReader）封装了从两个存储层合并数据的逻辑，对上层调用者透明。

单条目读取流程：调用方请求某知识条目的完整 ContextData（含 scoring）。融合器同时发起两个异步读取——Markdown 文件通过 IContextFileReader 读取并解析 frontmatter 和正文，侧车存储通过 getSignals 读取 ScoringRecord。如果侧车存储中无对应记录（新条目或迁移前条目），使用默认评分值（importance=50, recency=1, maturity=draft, accessCount=0, updateCount=0）。融合器将两路数据合并为完整的 ContextData 对象返回。

批量读取流程：在 manifest 构建和查询场景中，融合器提供批量接口。先通过 IContextFileReader.readMany 批量读取 Markdown 文件列表，再通过 batchGetSignals 一次性获取所有对应信号记录。在计算 compoundScore 时，BM25 相关性分由搜索引擎基于正文内容独立计算，importance 和 recency 从融合后的 scoring 中获取，tier boost 基于 maturity 决定。整个融合过程对外表现为一次完整的知识条目读取。

关键设计点：融合器本身不缓存结果——每次读取都从两个存储层获取最新数据，避免缓存失效问题。对于性能敏感场景（如 manifest 构建中需要遍历数百个条目），利用 batchGetSignals 的单次文件解析来降低 I/O 开销。

### 迁移兼容机制

为确保从当前混存模式平滑过渡到分离存储模式，系统设计了自动迁移机制，无需用户干预。

首次访问触发迁移（Lazy Migration）：当融合器读取某个知识条目时，如果 Markdown 文件的 frontmatter 中仍包含 scoring 字段（importance、recency 等），而侧车存储中无对应记录，融合器执行以下迁移步骤：（1）从 frontmatter 中解析出全部 scoring 字段值，构造 ScoringRecord 写入侧车存储；（2）从 Markdown 文件的 frontmatter 中剥离 scoring 字段，只保留 title、summary、tags、keywords、related 等静态字段；（3）将剥离后的 Markdown 内容写回文件。迁移后的 Markdown 文件不再包含任何动态信号字段，后续所有信号更新仅写入侧车存储。

低峰期批量迁移（Batch Migration）：除了 lazy migration，系统在 brv status 或 brv vc status 等低负载命令执行时，可以触发一次后台批量迁移扫描：遍历 context tree 中所有非 derived 的 .md 文件，对仍包含 scoring frontmatter 字段的文件执行上述剥离和迁移操作。批量迁移以事务批处理模式执行，每批处理 N 个文件后写入一次 scores.json，失败时回滚当前批次。

回退兼容：在迁移完成前（即部分文件已迁移、部分未迁移的阶段），融合器对每个文件独立判断：有侧车记录则从侧车读取信号并从 Markdown 中跳过 scoring 解析；无侧车记录则从 Markdown frontmatter 中降级解析 scoring 字段。这保证了迁移期间系统功能的连续性。

### 并发更新与一致性

由于静态知识层（Markdown 文件）和动态信号层（scores.json）在物理上分离，需要处理两层之间的并发更新一致性问题。

单条目并发写入策略：侧车存储的 putSignals 采用乐观版本号机制。每个 ScoringRecord 携带一个单调递增的 version 字段。写入时，putSignals 先读取 scores.json 中该路径的当前 version，如果与待写入记录的 baseVersion 不一致，说明存在并发修改，触发冲突解决策略：对于 accessCount 和 updateCount 这类累加型字段，将增量（待写入值 - baseVersion 时的值）叠加到当前值；对于 importance 和 recency 这类覆盖型字段，取 max；对于 maturity，取较高的 tier。解决后的结果写入 scores.json，version 递增。

Markdown 文件与侧车存储的交叉一致性：由于分离后 Markdown 文件的策展更新不再触发信号层写入，策展操作本身通过显式的两阶段调用保证一致性——先写入 Markdown（通过 FileContextTreeWriterService），成功后调用 recordCurateUpdate 更新侧车存储。如果在 Markdown 写入成功但侧车更新失败时，侧车存储中该条目仍保留旧信号值，但不影响数据正确性（旧信号值通过衰减仍可参与排序），系统在下一次读取或批量迁移时通过 lazy repair 自动修复不一致。

知识文件删除时的信号清理：当知识条目被删除（通过 merge/sync 的 delete 操作或 direct unlink），系统在删除 Markdown 文件后同步调用侧车存储的 deleteSignal(path) 删除对应信号记录。如果信号删除失败，残留的信号记录在下一次 manifest 构建时因对应 Markdown 文件不存在而被垃圾回收——manifest 扫描逻辑在发现侧车中有记录但无对应 Markdown 文件时，自动清理孤儿信号记录。

### 失败隔离与降级

侧车存储作为辅助层，其不可用不应阻塞核心知识操作。系统设计了多层降级策略。

读取降级：融合器在读取侧车存储失败时（文件不存在、JSON 解析错误、权限不足等），不抛出异常，而是对该次读取中的所有条目使用默认评分值（importance=50, recency=1, maturity=draft, accessCount=0, updateCount=0）。同时触发一次后台异步修复任务，尝试重建 scores.json。这保证了即使侧车文件损坏，知识检索和 manifest 构建仍可继续运行。

写入降级：信号写入（如搜索访问记录）失败时，采用内存缓冲 + 重试策略。失败的信号更新先写入内存中的 WritePendingQueue，后台定时任务每隔 T 秒尝试将队列中的更新批量写入 scores.json。队列有容量上限（默认 10000 条），超出时按 FIFO 丢弃最旧的条目并记录 warning 日志。如果 scores.json 持续不可用（如磁盘满），信号更新被静默丢弃，系统在日志中记录丢弃计数，但不影响 Markdown 文件的正常读写。

崩溃恢复：scores.json 的写时复制 + 原子重命名机制保证了写入过程中的崩溃安全——如果系统在写入临时文件时崩溃，scores.json 保持上一个一致版本；如果 rename 成功但后续操作崩溃，scores.json 已是完整的新版本。临时文件（.scores.json.tmp）在下次启动时被自动清理。

### 归档/剪枝/合并时的清理一致性

分离存储后，归档（archive）、剪枝（pruning）和合并（merge）操作需要同步维护侧车存储中的信号记录，避免残余数据污染。

归档清理：当 FileContextTreeArchiveService 将低重要度条目从 context tree 归档到 _archived/ 目录时（生成 .full.md 和 .stub.md），同步执行以下信号层操作：（1）从 scores.json 中读取该条目的当前信号，将其 evicted_importance 写入 .stub.md 的 archive stub frontmatter；（2）从 scores.json 中删除该条目的信号记录。如果信号删除失败，该条目在后续 manifest 扫描时会被孤儿回收逻辑自动清理。归档恢复（restoreEntry）时，从 .stub.md 的 evicted_importance 重建 ScoringRecord 并写回 scores.json，初始 maturity 设为 draft。

剪枝（pruning）清理：剪枝操作基于归档候选扫描（findArchiveCandidates），检测 importance 低于 ARCHIVE_IMPORTANCE_THRESHOLD（35）且 maturity 为 draft 的条目。在分离存储后，该判断的数据源改为侧车存储而非 Markdown frontmatter。剪枝执行时，先创建归档产物、删除原始 Markdown 文件，再删除侧车信号记录。三步中任一步失败时，已完成的步骤不回滚（归档是幂等的），但会记录 warning 供后续自动修复。

合并（merge）清理：FileContextTreeMerger 的 merge 操作处理来自远程的 Markdown 文件增删改。在分离存储后，merge 流程增加信号层同步步骤：（a）远程新增文件 → 侧车存储中创建默认 ScoringRecord；（b）远程删除文件 → 侧车存储中删除对应信号记录；（c）远程编辑文件 → 由于远程文件的信号值仅反映远程机器的使用情况，本地不采纳远程信号——保留本地侧车中已有的信号记录不变（如果是新条目则使用默认值），仅将远程 Markdown 文件写入本地。Merge 的冲突处理逻辑不变（local → _N.md, remote → original path），冲突产生的 _N.md 在侧车存储中不额外创建记录，待下次 manifest 构建时自动发现并建立默认信号。

孤儿回收：系统在每次 manifest 构建（buildManifest）和批量迁移扫描中执行孤儿信号回收：遍历 scores.json 中所有键，检查对应 Markdown 文件是否在 context tree 中存在（排除 _archived/ 目录），不存在的键视为孤儿并删除。这保证了即使上述各操作的信号清理步骤部分失败，系统也能在后续运行中自动修复一致性。

### 技术效果

本方案相比当前混存模式，在团队协作和长期维护方面带来以下技术效果：

- 消除动态信号引发的版本控制噪音：分离后，Markdown 文件仅在策展操作或人工编辑时发生变更，文件变更频率从「每次搜索命中都可能触发」降低到「仅知识内容演进时触发」。版本控制 diff 仅反映有意义的语义变更，审阅者可以清晰判断每次 commit 中的知识内容变化。
- 降低团队合并冲突率：多人并行使用时，各自的搜索访问和衰减计算仅写入本地侧车存储，不会修改共享的 Markdown 文件。合并冲突从「动态信号 + 知识内容」的双重冲突源减少为仅知识内容冲突。同时，侧车存储不纳入版本控制，不存在远程合并场景。
- 保持检索排序和生命周期管理能力：侧车存储完整保留了 importance、recency、maturity 等评分信号，通过读取融合器在检索和 manifest 构建时透明合并。compoundScore 计算、成熟度分级（含滞后机制）、归档候选扫描等功能全部正常工作，不受分离影响。
- 兼容现有架构和迁移路径：方案不要求删除动态信号或迁移到不可审阅的数据库。lazy migration 和批量迁移机制保证从现有混存模式零停机过渡。迁移完成后 Markdown 文件仍可通过任意文本编辑器或 Git 工具审阅和比较。
- 故障容忍：侧车存储的不可用不影响知识内容的读写和版本控制操作。降级策略（默认值回退 + 内存缓冲 + 后台重试 + 孤儿回收）保证系统在磁盘满、文件损坏等异常情况下继续提供核心服务，且能在恢复后自动修复一致性。

### 风险与待确认问题

以下风险点需要在实施前进一步确认：

- scores.json 的单文件写入瓶颈：当大量知识条目并发更新信号时（如批量搜索场景），单 JSON 文件的写时复制 + 原子重命名模式可能成为 I/O 热点。可考虑按子目录分片（如 scores/auth.json、scores/network.json），降低单文件锁竞争。
- 侧车存储的跨机器同步：当前设计中侧车存储是本机独占的，不通过 Git 同步。对于同一团队成员在不同机器上使用同一 context tree 的场景，各机器的信号值（importance、recency 等）会分化。是否需要以及如何设计跨机器的信号融合策略（如在 push/pull 时可选地同步信号摘要），需根据团队实际使用模式确认。
- Manifest source_fingerprint 的适配：当前 fingerprint 基于 stat-only（path:mtime:size），分离存储后 Markdown 文件的 mtime 不再因信号更新而变化，fingerprint 的失效触发将更加准确。但需要确认 manifest 构建逻辑是否正确读取融合后的信号值用于 context lane 的 importance 排序。
- archive stub 中 evicted_importance 的来源变更：归档时 evicted_importance 的取值从 Markdown frontmatter 改为侧车存储后，需要确认 .stub.md 生成逻辑中的 extractImportance 调用路径已更新为通过融合器读取。
