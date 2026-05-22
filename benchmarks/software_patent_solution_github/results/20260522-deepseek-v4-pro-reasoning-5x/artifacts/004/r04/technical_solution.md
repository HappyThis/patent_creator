## 技术方案

### 技术问题概述

ByteRover context tree 将项目知识存储为 Markdown 文件，通过 YAML frontmatter 同时承载两类信息：一是可共享、可审阅的静态知识内容（标题、摘要、标签、关键词、关系链接、正文中的事实/叙事/原始概念/片段等）；二是频繁变化的动态运行时信号（importance 重要性分值、recency 时效分值、accessCount 访问计数、updateCount 更新计数、maturity 成熟度档位、createdAt/updatedAt 时间戳）。由于两类信息混合在同一文件的同一 YAML 块中，每次搜索命中导致的 access hit 批量回写、时间衰减重算、curate 更新触发的重要性调整等操作，都会覆写 Markdown 文件，引发三个连锁问题：第一，版本控制系统频繁检测到文件变更，使 Git 状态持续为脏，真正的知识内容变化被噪声淹没；第二，团队成员并行操作时，同一知识文件的 scoring 写入与另一成员的内容编辑产生合并冲突；第三，scoring 的时间衰减依赖文件修改时间戳，若版本控制系统在检出时重置了 mtime，衰减计算的基础数据失真。

### 核心技术方案

本方案的核心思想是将动态运行时信号从共享 Markdown 文件中迁出，存入独立的本机评分侧车存储（Scoring Sidecar Store），同时保持 Markdown 文件仅承载可共享、可审阅的静态知识内容。评分侧车存储位于 .brv/context-tree/.scoring/ 目录下，通过 .gitignore 排除出版本控制，与已有的 derived artifact 排除体系（_index.md、_manifest.json、_archived/*.stub.md 等）保持一致的设计范式。

方案由五个关键机制协同构成：（1）信号分离策略——明确界定哪些字段留在 Markdown、哪些迁入侧车；（2）评分侧车存储——提供面向知识文件路径的键值式读写接口，保证原子写入和故障隔离；（3）读取融合——在搜索、查询、摘要生成等消费路径中，从 Markdown 读取内容、从侧车读取评分，在内存中合并为完整记录；（4）迁移兼容——首次访问时自动检测 Markdown frontmatter 中残留的 scoring 字段，提取并写入侧车，同时从 Markdown 中剥离；（5）清理一致性——在归档、剪枝、合并等操作触发知识文件删除或移动时，同步清理侧车中的对应评分记录。

### 信号分离策略

基于内容稳定性、可审阅性和团队协作价值三个维度，对当前 Markdown frontmatter 中的所有字段进行分类：

- 留在 Markdown 的静态知识字段：title、summary、tags、keywords、related（关系链接）。这些字段由人工或 LLM 策展产生，变化频率低，具有团队审阅价值，适合作为版本控制追踪的知识资产。
- 迁入评分侧车存储的动态信号字段：importance（重要性分值）、recency（时效分值）、accessCount（搜索命中计数）、updateCount（策展更新计数）、maturity（成熟度档位 draft/validated/core）、createdAt（创建时间）、updatedAt（最后更新时间）。这些字段随系统运行自动变化，不具备人工审阅意义，迁出后可消除版本控制噪声。
- 正文 body 内容（Raw Concept、Narrative、Facts、Snippets、Reason 等 Markdown 正文段）保持不动，继续留在 Markdown 文件中。

### 评分侧车存储设计

评分侧车存储（ScoringStore）是一个面向文件路径的轻量键值存储，位于 .brv/context-tree/.scoring/ 目录下。该目录被 .gitignore 排除，不在版本控制范围内，与 _index.md、_manifest.json 等 derived artifact 一起构成不被同步的本机衍生数据层。

数据组织方式：以知识文件的相对路径为键（如 auth/jwt-tokens/refresh-flow.md），对应一个 JSON 文件存储其评分记录（如 auth/jwt-tokens/refresh-flow.json），维持与 context tree 相同的目录层级结构。每条评分记录包含：importance（浮点数，默认 50）、recency（浮点数，默认 1.0）、accessCount（整数，默认 0）、updateCount（整数，默认 0）、maturity（枚举 draft/validated/core，默认 draft）、createdAt（ISO 时间戳）、updatedAt（ISO 时间戳）。

写入机制：使用原子写入模式（先写入临时文件，再 rename 到目标路径），保证单个评分记录的写入是原子的。对于批量写入场景（如 flushAccessHits），采用 Promise.allSettled 并行写入，单个文件写入失败不影响其他文件。读取缺失的评分记录时返回默认值，不会因侧车文件损坏或缺失导致搜索或查询中断。

### 读取融合机制

在搜索（SearchKnowledgeService.search）、查询、摘要生成等所有消费知识记录的路径中，实施读取融合机制。读取融合的核心流程为：从 Markdown 文件读取静态知识内容（通过 parseFrontmatter 解析 title/summary/tags/keywords/related 及 body 正文）；从评分侧车存储读取对应路径的动态信号记录；在内存中将两部分合并为完整的知识记录结构（ContextData），供后续的 compound scoring、decay、tier determination 等纯函数使用。

索引构建（buildFreshIndex）时，对每个 .md 文件同时读取其内容及其侧车评分记录，构建 IndexedDocument 时将 scoring 从侧车填充。搜索排名计算（compoundScore）照常使用 BM25 + importance + recency + tier boost 的加权公式，输入数据的来源改变但计算逻辑不变。访问命中累积（accumulateAccessHits）照常暂存于内存的 pendingAccessHits Map 中，但 flushAccessHits 写入目标从 Markdown frontmatter 改为评分侧车存储。

若评分侧车存储中的记录缺失（如为新文件尚未生成评分记录），读取融合返回默认评分值（importance=50, recency=1.0, maturity='draft', accessCount=0, updateCount=0），语义与当前 parseFrontmatterScoring 中缺失 scoring 字段时的默认行为一致。

### 迁移兼容机制

为兼容已存在的、frontmatter 中仍嵌有 scoring 字段的旧格式 Markdown 文件，引入迁移期兼容机制。该机制遵循"读取时检测、写入时剥离"的原则，不设专门的批量迁移命令，避免在全量迁移过程中引入大规模文件变更。

具体流程：parseFrontmatter 在解析 Markdown 文件时，若发现 frontmatter 中存在 importance/recency/accessCount/updateCount/maturity 等 scoring 字段，同时对应的评分侧车记录不存在，则将 scoring 字段提取出来写入侧车存储。写入成功后，调用 stripScoringFromContent 操作，从 Markdown 文件的 frontmatter 中移除所有 scoring 相关字段，仅保留静态知识字段，然后将净化后的内容覆写回 Markdown 文件。覆写后的 Markdown 文件的 snapshot 哈希将发生变化，在下一次 push 时作为内容清理的编辑被同步到团队仓库。

若侧车写入成功但 Markdown 覆写失败（如权限问题），系统不阻塞当前操作，迁移状态记录在内存中标记为"部分迁移"，下一次访问同一文件时重新触发迁移。若侧车写入失败（如磁盘满），系统回退到从 Markdown frontmatter 读取 scoring 的兼容路径，保证搜索和查询功能不中断。已成功迁移的文件不在 frontmatter 中保留任何 scoring 字段，避免新旧数据源并存导致的不一致。

### 并发更新与失败隔离

评分侧车存储与 Markdown 文件分离后，并发写入和失败隔离的处理策略如下：

并发更新策略：评分侧车采用乐观写入模式——每次写入前不检查版本号，直接覆盖。原因是 scoring 字段（importance、recency、accessCount 等）本身为启发式指标，非严格一致性数据，偶发的写入覆盖不会影响系统正确性。对于同一文件的多实例并发写入场景，后写入者的值覆盖先写入者，符合 scoring 作为"最新运行时状态"的语义。

失败隔离：评分侧车写入失败（磁盘满、权限错误、JSON 序列化错误等）不影响 Markdown 文件本身的读写。搜索流程中，若某个文件的侧车记录读取失败，该文件以默认评分参与排名，不抛出异常。flushAccessHits 批量写入时，对每个文件的写入使用独立的 try-catch，单个文件写入失败不影响其他文件的 scoring 更新，也不影响搜索请求的返回。

Markdown 文件同步（push/pull/merge）不受评分侧车影响：file-context-tree-writer-service 和 file-context-tree-merger 仅操作 .md 文件，侧车存储目录被 .gitignore 排除且不在 snapshot 扫描范围内。Markdown 文件的 snapshot 哈希不再因 scoring 变化而改变，从而消除了 scoring 写入引发的虚假内容变更检测和合并冲突。

### 归档剪枝合并的清理一致性

归档、剪枝和合并操作会改变知识文件的存在状态，需要同步维护评分侧车中的对应记录，防止孤立评分数据累积。

归档一致性：当 FileContextTreeArchiveService.archiveEntry 将低重要度知识条目从原始路径移至 _archived/ 目录时，同步将评分侧车中的记录迁移到归档路径对应的侧车位置。具体做法是：在 archiveEntry 中，unlink 原始 .md 文件之前，读取该路径的评分记录，将其写入 _archived/ 下对应 .stub.md 路径的侧车记录中，然后删除原始路径的侧车记录。归档路径的侧车记录保留 evicted_at（归档时间戳）和 evicted_importance（归档时的重要性分值），供 _manifest.json 构建和搜索 ranking 使用。

恢复一致性：restoreEntry 将归档条目恢复时，从 _archived/ 路径的侧车记录读取评分，写回原始路径的侧车记录，然后删除归档路径的侧车记录。

合并一致性：MarkdownWriter.mergeContexts 执行知识合并时，同步调用 mergeScoring 对源和目标两个路径的侧车评分记录进行合并（取 importance/recency 最大值、求和 accessCount/updateCount、取较高成熟度档位），将合并结果写入目标路径的侧车记录，并删除源路径的侧车记录（若源路径被合并后不再独立存在）。

剪枝一致性：FileCurateLogStore 和 FileQueryLogStore 的 pruneOldest 操作仅清理日志文件，不涉及知识文件，无需维护侧车。若未来引入知识文件的自动剪枝（如根据 importance 阈值删除低价值条目），剪枝操作应同时删除被剪枝文件路径的侧车评分记录。

### 技术效果

本方案在保持知识文件可共享、可审阅的前提下，将动态运行时信号从 Markdown 中迁出，产生以下技术效果：

- 消除评分噪声对版本控制的污染：Markdown 文件的 snapshot 哈希不再因搜索命中、时间衰减、计数器变化而改变，Git 状态仅在知识内容发生实质性变化时才变脏，团队成员在 git log 和 diff 中看到的是有意义的知识演进历史。
- 减少合并冲突：团队协作场景下，成员 A 的搜索操作不再触发 scoring 写入导致成员 B 的内容编辑产生虚假冲突。Markdown 文件仅承载内容变更，合并冲突的语义与知识内容编辑直接相关。
- 保持知识可读性：Markdown 文件仍然包含 title、summary、tags、keywords、relations 及完整正文，团队成员可以直接阅读、编辑、审阅，无需依赖专用工具解析评分数据。
- 搜索排名功能完整保留：读取融合机制确保 compoundScore、decay、tier determination 等核心排名逻辑无缝运作，搜索质量不因数据源变化而下降。
- 侧车故障不扩散：评分侧车存储的任何故障（文件损坏、磁盘满、写入失败）均降级为默认评分值，不影响知识内容的正常读取、搜索和同步。

### 风险与待确认问题

以下风险点需要在实施阶段进一步确认和验证：

- 迁移期并发风险：当旧格式 Markdown 文件正在被迁移（strip scoring frontmatter + 覆写）的同时，另一个进程正在执行 push/pull，可能导致迁移中的文件被当作"本地已修改"而触发合并。缓解方案：迁移操作在索引构建（acquireIndex）期间串行执行，该阶段已有锁保护。
- 侧车存储格式演进：当前评分字段集合（7 个字段）未来可能扩展（如增加特定场景的权重因子），侧车 JSON 格式需支持向前兼容。建议在侧车 JSON 中增加 version 字段。
- 跨机器评分不可移植：评分侧车数据不在版本控制中，团队成员在不同机器上的评分状态不共享。这是设计预期——评分反映的是本地使用模式，但需要明确文档化，避免用户期望跨机器评分同步。
- curate 路径中 scoring 写入点的完整梳理：curate-service、curation-helpers、MCP brv-curate-tool 等路径中是否有直接写入 frontmatter scoring 的代码，需要在实施前完整审计，确保所有写入点统一切换为侧车存储接口。
