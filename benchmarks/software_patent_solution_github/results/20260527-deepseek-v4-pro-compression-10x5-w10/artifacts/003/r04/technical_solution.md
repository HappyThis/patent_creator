## 技术方案

本技术方案在 Mission Control 现有任务看板与 SQLite 数据库为主工作入口的前提下，增加一种将任务数据以机器可读的「任务镜像文件」形式写入代码仓库（Git 仓库）的同步机制。任务镜像文件采用结构化 YAML 格式，存储在仓库固定目录下，通过 Git 操作实现版本化同步，使外部 agent 工具和开发者无需访问 Mission Control 数据库即可读取、追踪和协作处理任务队列。

### 任务镜像文件结构

每个 Mission Control 任务在代码仓库中对应一个独立的 YAML 文件，存储在仓库根目录下的 .mission-control/tasks/ 路径中。文件名为 {stable_id}.yaml，其中 stable_id 是系统为每个需要镜像同步的任务分配的全局唯一、不可变的标识符（UUID v7）。

每个 YAML 文件包含两部分：文件顶部的 YAML front matter（以 --- 分隔）承载结构化元数据，front matter 之后的任务描述正文（body）以 Markdown 纯文本形式承载。元数据字段包括：stable_id（UUID）、mc_task_id（Mission Control 内部整数 ID，用于回溯）、title、status（枚举：inbox/assigned/in_progress/review/quality_review/done）、priority（枚举：low/medium/high/urgent）、assigned_to、tags（字符串数组）、created_at、updated_at、due_date、outcome、resolution，以及 sync_version（单调递增的整数版本号，每次同步变更时自增）。文件编码为 UTF-8，换行符统一为 LF。

### 稳定任务标识与文件路径映射

Mission Control 内部任务使用 SQLite 自增整数作为主键（id），但该整数在不同部署实例中不具备全局唯一性。为实现跨仓库、跨实例的任务可携带性，系统为每个启用镜像同步的任务分配一个 stable_id（UUID v7，基于时间戳排序的 UUID）。该 stable_id 在任务首次被写入目标仓库时生成，并持久化到 Mission Control 数据库的 task_mirror_uuid 列中，之后不可变更。

文件路径映射规则为：.mission-control/tasks/{stable_id}.yaml，其中 {stable_id} 为 UUID v7 的小写十六进制表示（去掉连字符或保留连字符均可，方案采用保留连字符的 36 字符标准格式以便人工可读）。Mission Control 数据库中通过 task_mirror_uuid 列建立 mc_task_id → stable_id 的正向映射；反向查找时，通过解析仓库中所有 YAML 文件的 front matter 中的 mc_task_id 字段建立 stable_id → mc_task_id 的映射。此外，仓库根目录下的 .mission-control/index.yaml 文件维护一个轻量索引，列出所有已知的 stable_id 及其对应的 mc_task_id 和 sync_version，用于加速增量同步时的变更检测。

### 同步引擎设计

同步引擎作为独立模块（task-mirror-sync-engine）运行，与现有的 github-sync-engine 并行存在但不相互依赖。引擎围绕「本地工作副本 + Git 操作」模型设计，不依赖 GitHub Issues API。

触发方式：一是事件驱动——Mission Control 中任务发生创建、更新或删除时，通过事件总线（eventBus）发布 task.mirror_sync_requested 事件，引擎异步消费事件，将变更写入本地 Git 工作副本并执行 git add、git commit、git push。二是定时轮询——引擎以可配置间隔（默认 60 秒）对已启用镜像同步的项目执行 git pull，检测远端是否有其他协作者提交的任务文件变更，并将变更合并回 Mission Control 数据库。事件驱动与定时轮询共享同一工作副本目录，通过文件锁（flock）避免并发写冲突。

增量变更检测：引擎在每次同步前，将 Mission Control 数据库中待同步任务的关键字段（title、status、priority、assigned_to、description）计算 SHA-256 内容哈希，与上一次成功同步时存储的 sync_content_hash 比对。只有哈希变化的记录才触发文件重写和 Git 提交。对于从远端拉取的任务文件变更，引擎解析 YAML front matter 中的 sync_version 和 updated_at 字段，仅当远端 sync_version 大于本地记录的版本时才更新数据库，避免基于时间戳的竞态条件。

Git 操作流程：出站同步（MC → 仓库）时，引擎在本地工作副本中写入或更新目标 YAML 文件，然后依次执行 git add {file}、git commit -m "task({stable_id}): {action} — {title}"（其中 action 为 create/update/delete）、git push origin {branch}。入站同步（仓库 → MC）时，引擎执行 git pull --rebase origin {branch} 获取远端更新，然后通过 git diff --name-only HEAD@{1} HEAD 识别变更的文件列表，仅处理 .mission-control/tasks/ 目录下的 YAML 文件变更。对于每个变更文件，解析其 front matter 中的 mc_task_id 和 stable_id 以定位对应的数据库记录。

### 冲突与失败处理

同步引擎采用「主副本优先、冲突标记、永不阻塞主流程」的策略处理冲突和失败。

Git 推送冲突：当 git push 被远端拒绝（non-fast-forward）时，引擎首先执行 git pull --rebase 拉取远端更新。如果 rebase 过程中 .mission-control/tasks/ 目录下的文件出现合并冲突（即同一文件的同一字段被 MC 和远端同时修改），引擎不尝试自动合并内容，而是：将该文件的当前 MC 版本写入 .mission-control/conflicts/{stable_id}.yaml，同时在原文件顶部插入冲突标记注释（列出冲突双方的最后修改时间和 sync_version），然后将冲突文件以标注状态提交并推送，同时在 Mission Control 数据库中设置该任务的 mirror_sync_status 为 'conflict'，在前端任务看板上显示冲突指示器，提示用户手动选择保留哪个版本。

同步失败隔离：所有面向 Git 远程仓库的操作（push、pull、clone）均以异步、fire-and-forget 模式执行，与 Mission Control 核心的任务 CRUD 操作完全解耦。任务创建、更新、删除在 Mission Control 数据库中的执行不等待同步结果。同步失败时，引擎将失败的变更记录写入 outbox 队列表（task_mirror_outbox），由后台重试器按指数退避策略（初始 5 秒，最大 5 分钟，最多 10 次）重新尝试推送。超过最大重试次数后，任务在数据库中标记为 mirror_sync_status='failed'，并在看板中显示告警图标，但任务的日常操作不受影响。

### 删除同步机制

Mission Control 中任务的删除操作需要同步到仓库，使外部消费者能感知任务已不存在，而非静默丢失。

系统采用墓碑文件（tombstone）模式处理删除同步：当任务在 Mission Control 中被删除时，同步引擎不直接删除仓库中的 YAML 文件，而是在同目录下创建一个 .mission-control/tasks/{stable_id}.tombstone.yaml 文件，其中包含：原 stable_id、mc_task_id、删除时间戳、删除操作者，以及 deleted: true 标记。同时，原任务 YAML 文件的内容被替换为仅包含 front matter 的最小存根，status 字段设为 'deleted'。外部工具在读取任务目录时，通过检测 tombstone 文件或 status='deleted' 字段即可判断任务已终止。索引文件 .mission-control/index.yaml 中该条目的 sync_version 设为 -1 表示已删除。原始 YAML 文件不直接删除，保留 Git 历史中的完整变更轨迹。

### 数据库扩展

为支持任务镜像同步，需要在现有数据库中进行以下扩展（通过迁移脚本新增，不影响现有表结构）：

tasks 表新增列：task_mirror_uuid TEXT（stable_id）、mirror_sync_status TEXT（枚举：pending/synced/conflict/failed，默认 NULL 表示未启用同步）、sync_content_hash TEXT（上次成功同步时的内容哈希）、mirror_sync_version INTEGER（上次成功同步时的版本号）。projects 表新增列：mirror_enabled INTEGER（是否启用镜像同步，默认 0）、mirror_repo_url TEXT（目标 Git 仓库地址）、mirror_branch TEXT（目标分支，默认 'main'）、mirror_work_dir TEXT（本地工作副本路径）。新增 task_mirror_outbox 表：id、task_id、action（create/update/delete）、payload TEXT（变更内容 JSON）、retry_count、next_retry_at、status（pending/processing/failed/dead）、created_at。新增 task_mirror_sync_log 表：id、project_id、task_id、action、direction（outbound/inbound）、git_commit_sha、status、error_message、created_at。

### 技术效果

本方案相比现有方式产生以下技术效果：

- 可携带性：任务数据以自描述的 YAML 文件形式存在于 Git 仓库中，不依赖 Mission Control 运行时或数据库。任何能读取 YAML 和访问 Git 仓库的工具、agent 或 CI/CD 流水线即可消费任务数据。
- 跨工具协作：外部 agent 工具通过 git pull 即可获取最新任务队列，通过 git push 提交任务文件变更即可反馈状态更新，无需理解 Mission Control 的 HTTP API 或数据库 Schema。
- 版本化追踪：所有任务变更通过 Git 提交历史完整记录，每次同步的 commit message 包含 stable_id 和操作类型，可通过 git log -- .mission-control/tasks/ 独立审计任务演进过程。
- 故障隔离：同步引擎的异步 outbox 架构确保 Git 操作失败（网络不可达、权限不足、冲突）不会阻塞 Mission Control 核心任务工作流。任务看板和数据库始终可用。
- 含义一致性：通过 sync_version 单调递增机制和内容哈希比对，避免基于时间戳的时钟偏移竞态。墓碑文件模式保留删除语义，避免远端消费者将「文件消失」误解为「临时故障」。
- 与现有 GitHub Issues 同步互补：镜像文件同步不替代现有的 issue 同步通道。项目可同时启用两种同步：issue 同步面向 GitHub 原生协作工作流，镜像文件同步面向 agent 工具和 CI/CD 的机器消费场景。

### 风险与待确认问题

以下为当前方案中需要后续确认和验证的风险点：

- 大仓库性能：当任务数量达到数千级别时，.mission-control/tasks/ 目录下的文件数量可能影响 git status 和 git pull 的性能。可考虑按 stable_id 前两位字符进行分片子目录（如 .mission-control/tasks/a1/b2/{uuid}.yaml）。
- 多实例写入：当多个 Mission Control 实例同时向同一仓库推送时，冲突频率会升高。冲突标记模式能防止数据丢失，但需要确认用户对冲突解决工作流的接受度。
- Git 凭证管理：同步引擎需要长期持有目标仓库的写入凭证（SSH 密钥或 Personal Access Token）。凭证的轮换、失效检测和权限最小化需要与现有 GITHUB_TOKEN 管理机制统一设计。
- 二进制文件膨胀：任务的 description 字段如果包含大量 base64 图片或长日志，会导致 YAML 文件体积急剧膨胀，影响 Git 仓库克隆和拉取速度。可考虑对大字段（超过 10KB）采用外部引用而非内联存储。
- stable_id 生成时机：stable_id 需要在任务首次同步时生成，但如果用户创建任务后长时间不启用同步，数据库中将存在大量未分配 stable_id 的记录。需确认是否接受这种延迟分配策略，还是在任务创建时即预分配 stable_id。
