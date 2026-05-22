## 技术方案

### 技术问题

Mission Control（以下简称 MC）作为 AI agent 编排控制台，其任务数据当前主要存储在 SQLite 数据库中。任务看板（Kanban board）通过 REST API（src/app/api/tasks/route.ts）提供 CRUD 操作，任务状态在 inbox → assigned → in_progress → review → quality_review → done 六列之间流转。尽管 MC 已具备将任务双向同步到 GitHub Issues 的能力（通过 src/lib/github-sync-engine.ts 将任务映射为 GitHub Issue，利用标签映射状态和优先级），但这种同步方式存在以下不足：

- GitHub Issues 作为同步载体，其语义结构（标题/正文/标签/状态）并非为 agent 工作流设计的机器可读格式，外部 agent 工具需要理解 GitHub API 和 Issue 语义才能解析任务信息；
- 同步依赖于 GitHub 平台和 API 访问令牌（GITHUB_TOKEN），无法在离线环境或非 GitHub 托管仓库中使用；
- 任务数据无法随代码仓库的分支、标签和提交历史自然流转，团队难以将特定任务状态与代码版本绑定；
- 外部 agent 工具无法在不直接访问 MC 数据库或 GitHub API 的情况下读取当前任务队列的完整镜像。

因此，需要在保持 MC 现有任务看板和数据库作为主要工作入口的前提下，引入一种基于代码仓库内机器可读任务镜像文件的协作格式，使得任务数据可被外部 agent 工具以文件系统操作和 Git 操作的方式读取、同步和追踪，而不依赖 GitHub Issues API 或直接访问 MC 数据库。

### 核心技术方案

本方案在 MC 现有任务系统的基础上，增加一个任务镜像同步引擎（Task Mirror Sync Engine），将 MC 数据库中的任务数据序列化为机器可读的任务镜像文件，写入关联的 Git 仓库中，并通过 Git 提交和推送操作使任务状态跟随仓库流转。该引擎独立于 MC 核心任务 CRUD 路径运行，采用异步、非阻塞的设计，确保外部同步失败不影响 MC 内部的日常任务操作。

任务镜像同步引擎的整体架构包含以下核心组件：（1）任务镜像序列化器，负责将 MC 任务对象转换为机器可读文件格式；（2）任务标识到文件路径的确定性映射器；（3）Git 操作适配层，封装文件写入、暂存、提交和推送操作；（4）冲突检测与解决模块；（5）删除同步处理器，通过墓碑文件追踪已删除任务。

### 任务镜像文件结构

每个 MC 任务对应一个独立的 JSON 文件，存储在关联仓库的指定目录下。文件结构设计遵循以下原则：字段命名与 MC 内部 Task 接口（src/lib/db.ts 中定义的 Task interface）保持语义一致，便于双向映射；包含同步所需的时间戳字段以支持冲突检测；不包含 MC 内部实现的敏感数据（如数据库内部 ID 映射细节）。

任务镜像文件（task_mirror.json）包含以下核心字段：task_id（MC 内部任务 ID，整数）、title（任务标题）、description（任务描述，可选）、status（枚举值：inbox / assigned / in_progress / review / quality_review / done）、priority（枚举值：low / medium / high / urgent）、assigned_to（指派人标识，可选）、project_id 与 project_name（所属项目）、tags（字符串数组）、created_at 与 updated_at（Unix 时间戳，秒）、due_date（可选截止时间）、outcome（执行结果，可选）、mc_synced_at（MC 侧最后同步时间戳，用于冲突检测）。文件还包含一个 meta 子对象，记录镜像格式版本号（schema_version）和生成该文件的 MC 实例标识。

### 任务标识到文件路径的确定性映射

为保证外部 agent 工具能够通过稳定的路径引用同一任务，系统采用确定性映射算法将 MC 任务 ID 转换为仓库内的文件路径。映射规则如下：

1. 基础路径：任务镜像文件统一存放在仓库根目录下的 .mc/tasks/ 目录中。使用隐藏目录避免与项目源码混淆。
2. 目录分片：为避免单一目录下文件数量过大，按任务 ID 的模 1000 取商进行二级目录分片。任务 ID 为 N 的任务文件路径为 .mc/tasks/{Math.floor(N/1000)}/{N}.json。例如任务 ID 为 1042，路径为 .mc/tasks/1/1042.json。此映射是纯函数，任务 ID 不变则文件路径不变。
3. 索引文件：在 .mc/tasks/index.json 中维护一个轻量索引文件，按状态分组列出所有任务 ID 及其文件路径，供外部 agent 工具快速扫描当前任务队列而无需遍历目录。

### Git 操作与同步流程

任务镜像同步引擎通过封装 Git 命令行操作实现文件变更的版本化管理。引擎在 MC 服务器进程内维护一个本地 Git 仓库克隆（或直接使用已关联的仓库工作副本），对任务镜像文件的增、改、删操作均通过该仓库完成。

同步操作基于 MC 现有的事件总线（src/lib/event-bus.ts）触发。当任务发生变更时，MC 通过 eventBus.broadcast 发出 task.created、task.updated、task.deleted 等事件。任务镜像同步引擎监听这些事件，并在异步处理流水线中执行以下步骤：（1）根据事件类型和任务数据，调用序列化器生成或更新对应的镜像 JSON 文件；（2）将文件变更通过 git add 暂存；（3）生成描述性提交信息（例如 "task(1042): update status to in_progress"）并执行 git commit；（4）执行 git push 将提交推送到远程仓库的指定分支（默认为 mc-task-mirror）。

每个同步周期内，引擎将多个任务变更批量聚合为一次 Git 提交，减少提交碎片。聚合策略为：在一个可配置的时间窗口（默认 30 秒）内收集所有待同步变更，窗口关闭后统一执行 git add、git commit 和 git push。对于高频变更的任务，采用防抖（debounce）机制，仅保留窗口内最后一次变更。同步操作完全异步执行，不阻塞 MC 的任务 CRUD API 响应。同步结果（成功或失败）写入 MC 的 sync_log 记录，供运维面板查看，但不影响任务本身的状态流转。

### 冲突检测与失败处理

任务镜像同步采用"MC 为权威数据源（source of truth）"的单向优先级策略，但在支持外部 agent 通过 Pull Request 反向提交变更的场景中，需要冲突检测机制。

- 冲突检测：每个任务镜像文件包含 mc_synced_at 字段，记录 MC 最后一次写入该文件的时间戳。当外部 agent 工具通过 PR 修改任务文件时，MC 在合并前比较文件中的 mc_synced_at 与 MC 数据库中该任务的 updated_at：若 MC 侧有更新的变更（updated_at > mc_synced_at），则标记为冲突，以 MC 侧数据为准覆盖。
- 推送冲突处理：git push 若因远程分支有新提交而失败（non-fast-forward），引擎执行 git pull --rebase 后重新推送。若 rebase 产生冲突，引擎以 MC 侧文件内容为准（使用 git checkout --theirs 策略），确保 MC 始终为权威源。
- 网络与认证失败：git push 因网络或认证问题失败时，引擎将失败事件记录到 sync_log 并启动指数退避重试（初始间隔 10 秒，最大间隔 10 分钟），不对 MC 任务操作产生任何阻塞。连续失败超过阈值（默认 10 次）后向 MC 管理员发出告警通知。

### 删除同步：墓碑文件机制

当 MC 中任务被删除时（通过 DELETE /api/tasks/[id] 接口），外部仓库中对应的镜像文件需要反映这一删除事实。本方案采用墓碑文件（tombstone）策略而非直接删除文件，原因在于：直接删除文件会使 Git 历史中的文件变得孤立，外部 agent 工具难以区分"该任务从未存在过"与"该任务已被删除"。

墓碑文件的具体机制：（1）当 MC 任务被删除时，同步引擎不删除原始 .mc/tasks/{shard}/{id}.json 文件，而是创建一个对应的墓碑文件 .mc/tasks/{shard}/{id}.tombstone.json，内容包含原任务 ID、删除时间戳、删除操作者、以及原任务最后一个已知状态的摘要（标题、状态）。原始任务文件被替换为一个仅含 tombstone 标记的精简 JSON。（2）外部 agent 工具读取索引文件 index.json 时，已删除的任务不出现在活跃任务列表中，但可通过墓碑文件查询删除记录。（3）墓碑文件在保留一段可配置时间（默认 90 天）后可被清理任务定期移除。

### 外部变更的入向同步

除 MC 向仓库推送任务变更外，系统也支持外部 agent 工具通过 Git 工作流向仓库提交任务镜像文件的变更，MC 通过定时轮询（pull）感知这些变更并同步回数据库。这一机制参考了 MC 现有 GitHub Sync Poller（src/lib/github-sync-poller.ts）的设计模式，但操作对象从 GitHub Issues API 变为仓库内的任务镜像文件。

入向同步流程：（1）任务镜像同步引擎以可配置的间隔（默认 60 秒，与现有 github-sync-poller 的 GITHUB_SYNC_INTERVAL_MS 配置项保持设计一致）执行 git fetch 拉取远程 mc-task-mirror 分支的最新提交。（2）通过 git diff 获取两次轮询之间的文件变更集合。（3）对每个变更的镜像文件进行反序列化，提取任务数据。（4）与 MC 数据库中的对应任务进行时间戳比较：若镜像文件的 mc_synced_at 晚于 MC 数据库中的 updated_at，则更新 MC 侧数据；否则跳过（防回环）。新出现的任务文件（MC 数据库中无对应 ID）则创建新任务。出现墓碑文件则标记对应 MC 任务为已删除。（5）所有入向同步操作记录在 sync_log 中。

### 与现有系统的关系

本方案与 MC 现有系统的集成关系如下：

- 复用 MC 现有事件总线（src/lib/event-bus.ts）：任务镜像同步引擎监听 task.created / task.updated / task.deleted 事件触发外发同步，与现有 GitHub Issue 同步引擎（pushTaskToGitHub 调用点）共享同一事件源。
- 独立于 GitHub Issues 同步：任务镜像同步是 GitHub Issues 同步的补充而非替代。项目可同时启用两种同步（例如 GitHub Issues 用于人工协作，任务镜像文件用于 agent 工具链），两者互不干扰。
- 复用 MC 任务数据模型（src/lib/db.ts 的 Task interface）：镜像文件的字段设计直接映射 Task 接口的 status、priority、tags、metadata 等字段，无需额外的数据转换层。
- 新增 sync_log 持久化表：记录每次同步操作的详情（方向、任务数、成功/失败状态、错误信息），类似现有 github_syncs 表的设计模式。
- 与 MC 后台调度器集成：轮询拉取外部变更的逻辑可注册到现有 scheduler（src/lib/scheduler.ts）中作为定时任务执行。

### 技术效果

本方案带来了以下技术效果：

- 任务数据的可携带性：每个 MC 任务以独立、自描述的 JSON 文件存在于仓库中，任务数据不再锁定于 MC 数据库。其他工具只需读取文件即可理解任务状态，无需理解 MC 数据库模式或 API。
- 与代码仓库的深度集成：任务状态变更以 Git 提交的形式记录，自动关联提交者、时间戳和变更摘要。团队可将任务变更与代码变更在同一仓库历史中追踪，支持 git log -- .mc/tasks/ 等方式审计任务演进。
- 离线与去中心化访问：克隆仓库即可获得完整任务队列镜像。外部 agent 工具可在无网络连接或无法访问 MC 服务器的情况下，基于本地任务镜像文件执行工作流。
- 平台无关性：任务镜像文件是纯 JSON 文本，不依赖 GitHub API、特定数据库驱动或网络协议。任何支持文件读取和 JSON 解析的工具或 agent 均可消费。
- 同步的健壮隔离：外发同步采用异步、非阻塞设计，同步失败不影响 MC 任务创建、更新和删除操作。MC 看板始终可用，同步在后台自动恢复。
- 防回环与一致性：基于 mc_synced_at 时间戳的双向同步比较机制，防止 MC 与外部 agent 之间的无限循环更新。MC 始终作为权威数据源，避免任务状态在不同系统间出现含义偏差。

### 风险与待确认问题

以下为当前方案中需要后续确认或关注的风险点：

- 大仓库下的性能：当任务数量达到万级时，单层目录结构（即使有模 1000 分片）可能导致文件系统操作性能下降。可考虑进一步分片（如两级分片）或引入 pack 文件（将多个任务打包为单个 JSON 数组文件），但需权衡文件粒度和读取便利性。
- Git 操作开销：若任务变更频率极高（如数百次/分钟），批量聚合窗口内的提交压缩率可能不足，导致 Git 提交数量膨胀。需要在实际负载下评估聚合窗口大小的最优值。
- 入向同步的安全边界：当前方案依赖轮询 git pull 感知外部变更。若外部 agent 直接推送了格式错误或恶意内容的镜像文件，需要 JSON schema 校验层（参考现有 src/lib/validation.ts 中 createTaskSchema 的模式）进行拦截。
- 与现有 GitHub Issues 同步的语义重叠：两种同步方式在任务状态字段上可能产生不一致。建议在配置层明确：同一项目只启用一种外部同步格式，或明确定义优先级规则。
- 墓碑文件的存储增长：长期运行后 .mc/tasks/ 目录下将积累大量墓碑文件。需要在清理策略（默认 90 天）与审计需求之间权衡。
