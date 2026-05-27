## 技术方案

本方案在 ByteRover 现有 context tree 文件系统架构上，引入知识双轨存储模型：将项目知识拆分为共享知识轨道（可同步、可版本控制的 Markdown 文件）和动态信号轨道（系统自动生成的 .signals.json 派生产物）。共享知识轨道保留文档正文和稳定的 frontmatter 字段（children_hash、compression_ratio、token_count 等），通过 CoGit 推拉和三路合并在团队间同步；动态信号轨道记录检索命中次数、使用频率、排序权重、成熟度判断等高频变化信息，复用现有 isExcludedFromSync() 谓词排除出快照、同步和合并路径，从根本上消除动态信息导致的版本控制脏状态和合并冲突。

方案通过以下子章节分别阐述：知识双轨存储模型的设计、静态字段与动态信号的分类标准、动态信号的派生存储与生命周期管理、读取时的融合机制、迁移期兼容策略、并发更新与写入隔离、失败隔离与回滚、以及归档/剪枝/合并时的清理一致性保证。整体方案基于现有 FileContextTreeWriterService、FileContextTreeMerger、FileContextTreeSnapshotService、QueryExecutor 和 ContextTreeStore 的接口与谓词体系扩展，不改变现有模块的公开契约，不引入不可审阅的封闭存储。

### 整体架构

本方案在 ByteRover 现有 context tree 文件系统（.brv/context-tree/）之上，将项目知识拆分为两条存储轨道：共享知识轨道（Shared Knowledge Track）和动态信号轨道（Dynamic Signal Track）。共享知识轨道由人工编写或 LLM 生成的 Markdown 文件组成，通过 CoGit 推拉、快照和三路合并在团队成员之间同步，是版本控制的唯一主体。动态信号轨道由系统自动生成的派生产物组成，记录检索命中次数、使用频率、排序权重、成熟度判断等频繁变化的运行时信息，这些派生产物独立于版本控制之外，不参与快照、同步和合并。

### 知识双轨存储模型

共享知识轨道沿用现有的 .brv/context-tree/ 目录结构，存储人工编写或 LLM 生成的项目知识 Markdown 文件。这些文件通过快照服务（FileContextTreeSnapshotService）计算内容哈希，通过 Writer 服务（FileContextTreeWriterService）写入，通过 Merger 服务（FileContextTreeMerger）进行三路合并，并通过 CoGit 在团队间推拉。共享知识轨道中的每份文档具有稳定的文件路径和 frontmatter 元数据（类型 summary、含 children_hash / compression_ratio / token_count 等静态摘要字段），构成版本控制的主线。

动态信号轨道复用现有的派生产物机制。系统在现有 isExcludedFromSync() 谓词族基础上，新增一类动态信号文件（扩展名 .signals.json），存放于 context tree 对应知识文件的同级或子目录中。动态信号文件由系统在查询执行、上下文选择、摘要生成等热路径中自动写入和更新，被 isExcludedFromSync() 排除出快照、同步、合并和 push 路径。同时，动态信号文件保持 Markdown 或 JSON 明文格式，允许开发者通过文件系统直接审阅，不引入不可审阅的封闭数据库。

### 静态字段与动态信号的分类标准

本方案对 context tree 中的信息字段做如下分类。静态字段（保留在共享 Markdown 中，参与版本控制）：文档正文内容、frontmatter 中的 children_hash（子节点内容指纹，用于变更检测）、compression_ratio（压缩比，记录摘要生成参数）、condensation_order（凝缩层级）、covers（覆盖范围描述）、token_count（原始 token 数，反映文档规模）。这些字段在文档内容不变时保持稳定，适合作为版本控制的主数据。

动态信号（迁出到 .signals.json 派生产物，不参与版本控制）：检索命中次数（query_hit_count，记录该知识文件在全文搜索中被命中的累计次数）、使用频率（usage_frequency，记录该文件在上下文选择中被加载为上下文的频次，按时间窗口衰减）、排序权重（ranking_weight，由命中次数、频率、新鲜度加权计算的综合排序得分）、成熟度判断（maturity_label，如 draft / stable / deprecated，由系统根据文件修改间隔和引用关系自动判定）。这些信号随每次查询和使用高频变化，若写入共享 Markdown 将导致版本控制持续处于脏状态并引发合并冲突。

### 动态信号的派生存储与生命周期

动态信号以 .signals.json 文件形式存储于对应知识文件的同级目录或 _signals/ 子目录中。每个 .signals.json 文件包含一个 JSON 对象，键为知识文件相对路径，值为该文件的动态信号记录。动态信号文件由 isExcludedFromSync() 谓词排除出同步，沿用现有 .stub.md（归档桩文件）的排除模式——可被搜索索引读取，但不参与快照哈希计算、不在 push/pull 中传输、不在三路合并中处理。

动态信号的生命周期如下：① 创建——当知识文件首次被检索命中或加载为上下文时，QueryExecutor 或 ContextTreeStore 在热路径中检测对应 .signals.json 是否存在，不存在则创建并初始化各字段为零值。② 更新——每次查询命中、上下文加载时，系统原子地递增 query_hit_count 并更新 usage_frequency 的时间加权计数，重新计算 ranking_weight。更新采用 compare-and-swap（CAS）语义，读取当前值、计算新值、写入时校验版本号，冲突时重试。③ 衰减——usage_frequency 按指数衰减窗口（默认 7 天半衰期）自动衰减，由 ContextTreeStore 的异步 compact 路径在触发时批量执行衰减计算。④ 清理——当知识文件被归档、剪枝或删除时，对应的动态信号记录同步清理（详见清理一致性章节）。

### 读取融合机制

查询和上下文选择时，系统并行读取共享知识文件（Markdown）和对应动态信号文件（.signals.json），在内存中完成融合。融合后的逻辑视图对上层消费者（QueryExecutor、ContextTreeStore）表现为统一的知识条目，包含正文、静态 frontmatter 和运行时动态信号。融合过程对现有查询策略透明——QueryExecutor 的 5 层查询策略（精确缓存→模糊缓存→直接搜索→优化 LLM→完整 agentic）无需修改调用接口，仅在结果组装阶段附加动态信号字段。

读取融合的关键设计点：① 指纹计算——computeContextTreeFingerprint() 仅基于共享知识轨道文件（排除 isExcludedFromSync() 的所有派生产物）的 mtime 计算哈希，动态信号更新不会导致指纹变化，避免不必要的缓存失效。② 缺失容错——当 .signals.json 文件不存在（如新克隆环境、迁移初期），融合层返回默认零值信号，不阻断查询流程。③ 缓存策略——查询结果缓存（TTL 30 秒）和指纹缓存（TTL 30 秒）保持不变，但缓存键仅基于共享知识文件的指纹；动态信号在缓存命中后从 .signals.json 实时读取并覆盖缓存中的过期信号值。④ 搜索索引——动态信号文件虽排除出同步，但保留在搜索范围内（类似现有 .stub.md 模式），允许按 ranking_weight 排序搜索结果。

### 迁移期兼容策略

迁移期兼容策略确保从当前纯 Markdown 单轨模式平滑过渡到双轨模式，不中断现有团队工作流。迁移分为三个阶段：检测阶段、共存阶段和清理阶段。整体原则是：无 .signals.json 时完全兼容旧行为，有 .signals.json 时自动启用增强行为。

检测阶段：系统启动时扫描 .brv/context-tree/ 目录，识别尚未关联 .signals.json 的知识文件，为其创建初始动态信号文件（所有字段为零值）。此过程不修改任何现有 Markdown 文件，不影响现有快照哈希。共存阶段：旧版客户端（仅识别 Markdown）与新方案客户端（识别 Markdown + .signals.json）可在同一团队共存。旧客户端因 .signals.json 已被 .gitignore 排除且在 CoGit push/pull 中不传输，不受影响；新客户端本地生成和使用动态信号，不同步到远程。清理阶段：所有团队成员升级后，可运行一次性清理脚本，将已在 Markdown frontmatter 中遗留的动态字段（如有）移除以保持 Markdown 纯净，该脚本通过 isExcludedFromSync() 谓词确保仅操作共享文件。

### 并发更新与写入隔离

动态信号写入与共享知识写入在存储层面天然隔离——两类文件路径不同，锁粒度独立。共享知识文件的并发写入沿用现有 Merger 的三路合并机制，不受动态信号影响。动态信号文件的并发写入采用文件级 CAS（compare-and-swap）策略：每次更新读取当前 .signals.json 的内容和版本号（基于文件 mtime + 内容哈希），计算新值后以原子写入（先写临时文件再 rename）方式提交；若写入前检测到版本号已变（其他进程抢先更新），则重新读取、重新计算、重新提交，最多重试 3 次。

写入隔离的具体保证：① 共享知识文件（.md）与动态信号文件（.signals.json）写入路径完全不重叠——Writer 服务（FileContextTreeWriterService）的同步过滤（isExcludedFromSync）确保动态信号文件不被写入同步批次，反之动态信号更新器仅操作 .signals.json 文件。② 三路合并（FileContextTreeMerger.runMerge()）在按路径逐文件决策时，isExcludedFromSync() 谓词自动跳过所有 .signals.json 文件，合并冲突仅发生在共享 Markdown 上。③ .signals.json 的 CAS 重试上限（3 次）防止极端并发下的活锁；超过重试上限时，放弃本次更新并在日志中记录，下一轮查询或使用自然触发重新更新。

### 失败隔离与回滚机制

动态信号系统的任何故障不得影响共享知识轨道的正常读写和团队同步。失败隔离在三个层面实现：读取隔离、写入隔离和合并隔离。

读取隔离：融合层读取 .signals.json 失败（文件损坏、解析错误、权限错误）时，返回默认零值信号并记录警告日志，不向上层抛出异常。QueryExecutor 和 ContextTreeStore 的热路径因此不受动态信号故障影响。写入隔离：动态信号 CAS 写入失败（磁盘满、权限错误、超过重试上限）时，放弃本次更新并记录日志；不影响对应共享 Markdown 文件的读写和同步。合并隔离：Merger 的合并前全量备份（context-tree-backup 目录）仅备份共享知识文件（排除 isExcludedFromSync），回滚时仅恢复共享文件。动态信号文件损坏时，系统在下一次查询或使用中自动重建（所有字段归零），无需回滚。

### 归档、剪枝与合并时的清理一致性

当共享知识文件发生归档、剪枝或合并删除时，对应的动态信号记录必须同步清理，避免孤儿信号累积和存储膨胀。本方案通过生命周期钩子机制保证清理一致性，不依赖定时扫描或人工维护。

具体机制：① 归档——当知识文件被移入 _archived/ 目录并生成 .stub.md 桩文件时，归档流程同步删除对应 .signals.json 中的记录。若 .signals.json 变为空对象，则删除该文件以保持目录清洁。② 剪枝——ContextTreeStore.compact() 在触发 LLM 摘要压缩并删除原文件时，同步删除对应动态信号记录。剪枝操作在 compact 的异步冷路径中执行，不阻塞同步热路径。③ 合并删除——Merger 在三路合并中检测到远程删除了某共享知识文件（本地和快照均有、远程无），执行本地删除的同时，触发 post-merge 钩子清理对应动态信号记录。钩子在合并事务提交后异步执行，失败不影响合并结果。④ 一致性校验——系统在启动时执行一次快速一致性校验：遍历所有 .signals.json 中引用的路径，确认对应知识文件存在；不存在的路径视为孤儿记录，自动清理并记录日志。
