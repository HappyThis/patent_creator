## 技术方案

### 技术问题概述

Mission Control 中通过任务看板和 SQLite 数据库管理 agent 任务的全生命周期。任务数据当前仅存在于应用数据库内，外部 agent 工具或工作流系统无法在不直接访问 Mission Control 数据库或 API 的情况下读取、追踪或协作处理任务。虽然已有的 GitHub Issue 双向同步提供了一定程度的跨系统数据交换，但 Issue 格式依赖 GitHub 平台、label 语义映射引入了额外的转换层，且无法在离线或非 GitHub 环境中使用。

### 核心技术方案

本方案提出一种基于代码仓库中标准化任务镜像文件（Task Mirror File）的同步机制。每个 Mission Control 任务在启用同步的项目中，会自动生成一个机器可读的 JSON 格式任务镜像文件并提交至关联的 Git 仓库。外部 agent 工具通过读写仓库中的镜像文件即可理解任务队列状态，无需直接访问 Mission Control 数据库。同步过程为可选、异步且非阻塞的，Mission Control 数据库始终作为任务数据的权威来源。

### 任务镜像文件结构

任务镜像文件采用 JSON 格式存储于仓库约定目录（默认为 .mc-tasks/）下。每个镜像文件包含以下最小字段集：stable_id（跨仓库唯一的不可变任务标识）、title（任务标题）、status（任务状态，枚举值 inbox/assigned/in_progress/review/quality_review/done）、priority（优先级，枚举值 low/medium/high/critical）、assigned_to（指派的 agent 名称或 null）、mirror_version（镜像格式版本号，用于向前兼容）、created_at（创建时间 Unix 时间戳）、updated_at（最后更新时间戳）。

镜像文件额外包含可选扩展字段：description（任务描述）、tags（标签数组）、project_id（所属项目标识）、due_date（截止时间）、outcome（执行结果）、resolution（agent 执行输出摘要）。当任务在 Mission Control 中被删除时，镜像文件被替换为一个仅含 deleted、stable_id、deleted_at 三个字段的删除存根，以避免外部 agent 因文件不存在而产生异常。

### 稳定标识与文件路径映射

每个启用镜像同步的任务在创建时生成一个全局唯一的 stable_id，采用 UUID v4 格式。stable_id 在任务整个生命周期内保持不变，即使任务标题、状态或其他属性发生变化，该标识始终不变。stable_id 同时存储在 Mission Control 数据库（tasks 表的 mirror_stable_id 列）和镜像文件内部（stable_id 字段），构成双向关联锚点。

镜像文件路径通过确定性映射规则由 stable_id 计算得出：路径 = {mirror_root}/{stable_id}.json，其中 mirror_root 为项目级可配置的仓库内目录路径，默认为 .mc-tasks/。任何一方（Mission Control 或外部 agent）仅凭 stable_id 或文件名即可定位对应的镜像文件，无需查询数据库或维护额外的索引。

### Git 操作与同步机制

同步引擎复用项目已有的 GitHub API 客户端基础设施，通过文件级 Git API（获取文件内容、创建或更新文件、删除文件）操作镜像文件。引擎对每个操作生成幂等的 commit message（格式为 MC-MIRROR: task {stable_id} {action}），并 push 至远程仓库默认分支。

同步分为出站推送和入站拉取两个方向。出站推送：在 Mission Control 任务 CRUD API 的每个写操作完成后，以 fire-and-forget 异步方式触发。引擎检查任务所属项目是否启用 mirror_enabled，若启用则序列化当前任务数据为 JSON，调用 Git API 提交并推送。推送失败不影响任务 CRUD 操作的结果返回，仅记录错误日志并在后台重试。

入站拉取：由后台轮询器（mirror-sync-poller）定期执行，默认间隔 60 秒。轮询器扫描仓库 .mc-tasks/ 目录下所有 JSON 文件，按 stable_id 匹配 Mission Control 本地任务。匹配规则为：若镜像文件的 updated_at 晚于本地任务的 mirror_synced_at（且差值超过 5 秒防抖阈值），则以镜像文件内容更新本地任务；若本地任务不存在对应 stable_id，则在 Mission Control 中创建新任务，标记来源为 mirror-import。

### 冲突与失败处理

当同一任务在 Mission Control 和仓库镜像文件中几乎同时被修改时，系统采用 MC 优先（MC-wins）的冲突仲裁策略。具体流程：出站推送前，引擎先获取仓库侧镜像文件的当前内容与 updated_at；将其与 MC 本地任务数据做逐字段比较。对于仅在仓库侧有变化的字段，保留仓库侧的值；对于双方均有变化的同一字段，以 MC 侧的值为准，并将仓库侧的原始值写入镜像文件的 _conflict_note 字段中，commit message 标记 [MC-CONFLICT] 供人工审查。

失败处理遵循故障隔离原则：所有镜像操作在 try-catch 块中执行，单次推送或拉取失败仅记录日志并写入 sync_log 表（表结构复用现有 github_syncs 表），不影响其他任务的处理。推送失败时任务数据保持 Mission Control 侧最新状态不变，下次推送将覆盖。拉取失败时跳过该文件继续处理下一个。Git API 层面的网络超时、认证失败等异常通过指数退避重试策略处理。

### 删除同步

当 Mission Control 中任务被删除时，系统不物理删除仓库中的镜像文件，而是将其替换为删除存根（deletion stub）。存根 JSON 格式为：{"deleted": true, "stable_id": "{原 stable_id}", "deleted_at": {Unix时间戳}}。采用逻辑删除而非物理删除的原因在于：外部 agent 工具可能已缓存或依赖该文件的 stable_id 做追踪，物理删除会导致 file-not-found 异常，而删除存根让外部系统能明确感知任务已终止，做出相应清理。

入站拉取时若发现镜像文件为删除存根且本地对应任务仍存在，则根据配置决定行为：保守模式下仅记录告警日志不做自动删除；自动模式下将本地任务也标记为删除。删除存根在仓库中保留一个可配置的保留期（默认 30 天），超期后由后台清理任务物理删除。

### 关键模块与处理流程

系统由以下模块组成：mirror-schema 负责镜像 JSON 的序列化、反序列化与格式版本管理；mirror-path 负责 stable_id 生成和确定性路径映射；mirror-git-ops 封装文件级 Git 操作（读、写、删、commit、push、冲突检测）；mirror-sync-engine 编排推送和拉取的完整同步流程；mirror-sync-poller 注册到 scheduler.ts 的 tick 循环中执行定期拉取。

出站推送流程：任务 CRUD API 完成写操作后，检查 projects.mirror_enabled → 生成或读取 stable_id → mirror-schema 序列化任务为 JSON → mirror-git-ops 获取仓库侧当前文件 SHA → 比较时间戳判断方向 → 无冲突则覆写推送并更新 mirror_synced_at，有冲突则执行 MC-wins 合并 → 记录 sync_log。

入站拉取流程：poller 定时触发 → 扫描仓库 .mc-tasks/ 目录获取文件列表 → 逐文件读取 JSON → 按 stable_id 匹配本地 tasks 表记录 → 比较 updated_at 与 mirror_synced_at（防抖阈值 5 秒）→ 若镜像更新则更新本地任务，若为删除存根则按策略处理，若 stable_id 不存在于本地则创建新任务（来源标记 mirror-import）→ 记录 sync_log。

### 与项目环境的对应关系

本方案直接基于 Mission Control 现有架构设计。数据库层面，projects 表新增 mirror_enabled、mirror_path_template 字段控制同步开关与目录路径；tasks 表新增 mirror_stable_id、mirror_file_path、mirror_synced_at、mirror_version 字段存储镜像元数据。模块层面，镜像同步引擎 mirror-sync-engine 与现有 github-sync-engine 并列，共享 github.ts 的 Git API 封装层和 github-sync-poller 的轮询模式。调度层面，镜像拉取轮询器注册到 scheduler.ts 的 tick 循环中，受 settings 表的 general.mirror_sync 开关控制。API 层面，任务 CRUD 路由（tasks/route.ts、tasks/[id]/route.ts）中在现有 pushTaskToGitHub 调用点同级增加 pushTaskToMirror 调用。

### 技术效果

相比于仅将任务数据存储在应用数据库中的方式，镜像文件同步机制带来以下技术效果。第一，任务数据的可携带性：外部 agent 工具通过读取仓库中的标准化 JSON 文件即可获取完整任务状态，无需依赖 Mission Control API 或数据库连接，支持离线环境和自动化工作流。第二，跨工具协作能力：任务状态跟随代码仓库流转（commit、push、pull、branch），开发者和 CI/CD 流程可在代码变更上下文中直接感知任务变化。第三，故障隔离：同步为可选、异步、fire-and-forget 模式，镜像操作失败不会影响 Mission Control 核心任务管理功能。第四，语义一致性：镜像文件使用与 Mission Control 内部相同的状态枚举和字段命名，避免现有 GitHub Issue label 映射方式中可能出现的语义偏差。第五，确定性定位：stable_id 到文件路径的确定性映射使任何一方无需索引即可定位任务镜像文件。

### 风险与待确认问题

以下为需要后续确认的技术决策点。镜像文件目录结构：当前设计采用 .mc-tasks/{stable_id}.json 扁平结构，若任务量极大（超过数万），单目录下文件数量可能影响 Git 操作性能，可考虑按 stable_id 前两位字符拆分子目录。稳定标识生成策略：当前采用 UUID v4，若需要人类可读性可改为项目前缀加自增编号的复合键。Git 操作方式：当前设计复用 GitHub 文件 API（通过 githubFetch），若需支持非 GitHub 仓库（如 GitLab、Gitea），需抽象 Git 操作接口层。删除存根保留期与自动清理：30 天默认值需根据实际使用场景调整。镜像文件格式版本迁移：mirror_version 字段为后续 schema 演进预留，当格式变更时需定义新旧版本兼容读取策略。
