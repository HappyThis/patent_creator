## 技术方案

本技术方案描述一种任务数据仓库镜像同步方法，用于将 Mission Control 任务看板中的任务数据，以机器可读的文件格式同步至代码仓库中，使外部 agent 工具和开发者工作流无需直接访问 Mission Control 数据库即可读取、追踪和协作处理任务队列。该方案以 Mission Control 现有任务看板和 SQLite 数据库为主要工作入口，仓库内任务镜像文件作为可选的派生同步目标；外部同步失败不影响日常任务的创建、更新或删除操作。

### 核心技术问题

Mission Control 的任务数据存储在 SQLite 数据库（better-sqlite3，WAL 模式）中，通过 Next.js API 路由对外暴露。外部 agent 工具（如 Claude Code、自定义 CI/CD 流水线、第三方编排平台）如需获取当前任务队列，需要直接调用 Mission Control 的 HTTP API 并持有认证凭据。当团队希望任务状态跟随代码仓库流转（例如代码评审时同步查看相关任务进展、分支合并后自动更新任务状态），或让运行在 CI 环境的 agent 在不具备网络连通 Mission Control 实例的条件下理解任务上下文时，现有方式存在如下不足：（1）外部工具必须实现 Mission Control API 客户端并管理认证；（2）跨网络边界访问不可靠，无法在离线或沙盒 CI 环境使用；（3）任务数据与代码版本无法关联追溯。

### 任务文件结构与路径映射

系统在目标代码仓库中维护固定的目录布局，以保证外部工具无需额外索引即可定位任意任务。目录根为仓库内的 .mission-control/tasks/，其下按项目 slug 分子目录，每个任务对应一个以稳定 ticket 标识命名的 JSON 文件。

目录结构示例：

- .mission-control/tasks/index.json — 任务索引文件，包含所有任务 ID、ticket_ref、状态、文件路径的摘要列表
- .mission-control/tasks/<project-slug>/<ticket_ref>.json — 单任务镜像文件，例如 PROJ-001.json、PROJ-002.json
- .mission-control/tasks/_tombstones/ — 已删除任务的墓碑记录目录

单任务文件格式：每个 JSON 文件包含任务的全部可同步字段，并附加同步控制字段。关键字段包括：

- mc_task_id：Mission Control 数据库中的主键 ID，用于双向匹配
- mc_task_version：单调递增的版本号，每次 Mission Control 侧更新时自增，用于冲突检测和防乒乓同步
- ticket_ref：由 project.ticket_prefix 和 project_ticket_no 拼接的稳定标识（例如 PROJ-001），作为文件名的锚点
- project：项目 slug 名称
- title、description、status、priority、assigned_to、created_by、due_date、tags 等业务字段
- created_at、updated_at：Unix 时间戳，用于时间线比较
- quality_review：如存在 Aegis 质量审查记录，包含 reviewer、status、notes

稳定标识到文件路径的映射规则：文件路径由 project.slug 和 task.ticket_ref 确定，公式为 .mission-control/tasks/{slug}/{ticket_ref}.json。该映射的稳定性由 ticket_ref 的不变性保证——ticket_ref 在任务创建时分配（project_ticket_no 由 projects.ticket_counter 自增分配），任务迁移到其他项目时才变更（变更时旧文件写入墓碑、新位置创建文件）。索引文件 index.json 提供 O(1) 的 ticket_ref → 文件路径查找，避免遍历目录。

### 双向同步引擎

系统实现一个双向同步引擎（Task Mirror Sync Engine），由出站同步（Mission Control → 仓库）和入站同步（仓库 → Mission Control）两个通道组成。

出站同步通道：当 Mission Control 中任务发生创建、更新或删除时，通过事件总线（ServerEventBus）触发异步同步任务。具体流程如下：（1）任务 API 路由（POST/PUT/DELETE /api/tasks）在处理完数据库写入后，通过 eventBus.broadcast 广播 task.created、task.updated、task.deleted 事件；（2）同步引擎的事件监听器接收事件，检查该任务所属 project 是否启用了任务镜像同步（projects 表的 task_mirror_enabled 和 task_mirror_repo 字段）；（3）若启用，异步调用出站同步处理函数，不受任务 API 请求-响应生命周期限制——API 在广播事件后立即返回，同步失败仅记录日志和同步历史，不向 API 调用方返回错误。

出站同步的单任务处理逻辑：（a）根据任务 ticket_ref 和 project.slug 计算目标文件路径；（b）从本地工作副本拉取最新提交（git pull --rebase），获取远程最新状态；（c）读取目标文件当前内容（如存在），比较 mc_task_version 和 updated_at 时间戳；（d）若本地 mc_task_version 高于或等于文件中的版本，或本地 updated_at 晚于文件中时间戳，则用最新任务数据覆盖写入文件（更新 mc_task_version 为当前值）；（e）执行 git add、git commit（提交信息格式："mc:sync task {ticket_ref} ({status})"）、git push；（f）将同步结果（成功/失败、commit hash、文件变更）写入 task_mirror_syncs 表。

入站同步通道：通过定时轮询机制检测仓库中任务文件的外部变更。轮询器（以可配置间隔，默认 60 秒）遍历所有启用了任务镜像的项目，对每个项目执行 git pull 获取远程变更，比对本地工作副本中的任务文件与 Mission Control 数据库中的对应任务。比对逻辑：（1）读取所有 .mission-control/tasks/{slug}/*.json 文件，解析 mc_task_id 字段；（2）若 mc_task_id 对应数据库中存在任务且文件中的 mc_task_version 高于数据库记录，则更新数据库（拉入变更）；（3）若 mc_task_id 对应数据库中不存在任务，则创建新任务（来源标记为 'mirror-sync'）；（4）若数据库中任务在索引文件中有记录但对应文件不存在且无墓碑记录，标记为"外部已删除"状态而非自动删除数据库记录。防乒乓机制：每次数据库侧更新后写入 mirror_synced_at 时间戳，入站同步时跳过 mirror_synced_at 与文件 mtime 在 10 秒窗口内的记录，避免将刚推送的变更再次拉回。

### Git 操作与冲突处理

Git 操作层封装对代码仓库的克隆、拉取、提交和推送操作，通过操作系统的 git CLI 或 isomorphic-git 库执行，不需要 GitHub API。关键设计要点：

本地工作副本管理：系统在 Mission Control 数据目录下维护每个镜像仓库的本地克隆（路径：.data/mirror-repos/{hash(repo_url)}/）。首次启用任务镜像时执行 git clone，后续操作复用该工作副本。每次同步前执行 git pull --rebase 以获取远程最新状态，减少非快进推送的几率。

冲突检测与处理：出站同步在写入文件前，先比较工作副本中文件的当前内容与远程版本。具体策略：（1）如果目标文件在远程的最近变更来自非 Mission Control 的提交（通过提交信息前缀 "mc:sync" 判断），且远程版本的 mc_task_version 高于本地待写入版本，则判定为外部优先——将远程内容写入 .mission-control/tasks/_conflicts/{ticket_ref}_{timestamp}.json 并记录冲突事件，本地变更暂不覆盖；（2）如果远程版本 mc_task_version 不高于本地，或最近变更为 Mission Control 提交，则本地覆盖并推送；（3）git push 失败（非快进）时，自动执行 git pull --rebase 后重试一次；重试仍失败则记录失败事件并等待下一轮轮询重试。

认证与鉴权：Git 操作使用项目配置中存储的 Git 凭据（支持 HTTPS token 或 SSH 密钥路径），与 GitHub Issues 同步的 GITHUB_TOKEN 独立配置，不共享凭据。

### 删除同步与墓碑机制

任务删除的同步需要特殊处理，以避免外部工具在文件消失后重建已删除任务（产生僵尸任务），同时保留删除意图的可追溯性。

墓碑记录：当 Mission Control 中任务被删除时（DELETE /api/tasks/[id]），出站同步不在仓库中直接删除该任务文件，而是执行以下步骤：（1）将原任务文件内容追加 deleted_at 和 deleted_by 字段后，移动到 .mission-control/tasks/_tombstones/{ticket_ref}_{deleted_at}.json；（2）在 .mission-control/tasks/_tombstones/index.json 中追加一条记录，包含 mc_task_id、ticket_ref、deleted_at；（3）提交信息格式为 "mc:sync delete task {ticket_ref}"。墓碑文件在仓库中保留可配置时长（默认 30 天），超期后由清理任务移除。

入站删除处理：入站同步轮询器检测到 .mission-control/tasks/_tombstones/ 目录下出现新的墓碑记录时，查找 mc_task_id 对应的数据库任务，若该任务在数据库中仍存在且 mirror_synced_at 早于墓碑的 deleted_at，则同步删除数据库中的对应任务。若墓碑记录在数据库中无对应任务（已在 Mission Control 侧删除），则忽略。

防僵尸任务机制：外部工具如试图在仓库中创建与墓碑记录中 ticket_ref 相同的任务文件，入站同步检测到墓碑记录存在且 deleted_at 晚于新文件的创建时间，则拒绝创建并将冲突写入 _conflicts 目录。

### 与现有系统的集成

任务镜像同步作为可选功能集成到现有 Mission Control 架构中，复用已有基础设施，不重写现有路径。

数据库扩展：在 projects 表新增字段 task_mirror_enabled（INTEGER，是否启用任务镜像）、task_mirror_repo（TEXT，目标仓库 URL）、task_mirror_branch（TEXT，目标分支，默认 'main'）、task_mirror_credentials（TEXT，加密存储的 Git 凭据）。在 tasks 表新增字段 mirror_version（INTEGER，任务镜像版本号，每次同步自增）、mirror_synced_at（INTEGER，上次同步时间戳）。新增 task_mirror_syncs 表记录同步历史（id、project_id、direction、ticket_ref、commit_hash、file_path、status、error、created_at）。

事件总线集成：复用现有 ServerEventBus。在 initWebhookListener 同级位置注册任务镜像监听器，订阅 task.created、task.updated、task.deleted 事件，触发出站同步。该监听器与 webhook 监听器并行运行，互不影响。

调度器集成：复用现有 scheduler.ts 的定时任务框架，注册 mirrorSyncPoll 任务（默认间隔 60 秒），遍历启用镜像的项目执行入站同步。其实现模式与 github-sync-poller.ts 一致，共用相同的 stop/start/status 管理接口。

与 GitHub Issues 同步的关系：任务镜像同步和现有 GitHub Issues 同步是两条独立通道。项目可同时启用二者——GitHub Issues 同步将任务映射为 GitHub issue 及其标签（mc:inbox、priority:high 等），任务镜像同步将任务写入仓库文件系统的 .mission-control/tasks/ 目录。两条通道共享 projects 表的 github_repo 用于确定目标仓库，但使用独立的启用开关和同步历史表。

API 路由：新增 GET/POST /api/projects/[id]/mirror 端点，用于查看同步状态和手动触发同步；新增 GET /api/projects/[id]/mirror/conflicts 端点，用于列出未解决的冲突文件。

### 技术效果

本方案相比现有方式产生的技术效果：

- 可携带性：任务数据以自描述的 JSON 文件形式存在于代码仓库中，外部 agent 工具仅需文件读取权限即可解析任务队列，无需实现 HTTP 客户端或持有 Mission Control 认证凭据。Claude Code、自定义 CI 脚本等工具可直接 cat/read 任务文件。
- 离线可用性：git clone 仓库后，所有任务镜像文件在本地文件系统可用，CI 沙盒、开发环境可离线读取任务状态，不依赖 Mission Control 实例的网络可达性。
- 版本关联：每个任务文件的变更随 Git 提交历史记录，可追溯"谁在何时因何变更了任务"，任务状态与代码变更通过相同的 Git blame/log 机制追踪，实现任务-代码的版本关联审计。
- 非侵入式：同步完全异步且 fire-and-forget，Git 推送失败不影响 Mission Control 中的任务 CRUD 操作的响应时间和成功率。任务看板始终以 SQLite 数据库为准，镜像文件为只读派生副本或外部协作入口。
- 防乒乓与冲突消解：通过 mc_task_version 单调版本号和 mirror_synced_at 时间窗口，防止同一变更在双向通道间反复同步。外部冲突通过提交信息前缀检测和版本号比较自动裁决，非自动可裁决的冲突记录到 _conflicts 目录供人工处理。
- 安全删除：墓碑机制确保已删除任务不会因外部工具重建而复活，同时保留删除记录的可追溯性。

### 风险与待确认问题

以下为待确认和需关注的风险点：

- 大仓库性能：对于大型 monorepo，git pull/push 操作耗时可能超过轮询间隔。建议对大型仓库启用 shallow clone（git clone --depth 1）和 sparse checkout（仅拉取 .mission-control/ 目录），以降低 Git 操作延迟。
- 并发推送冲突：多个 Mission Control 实例同时对同一仓库不同任务执行 git push 时，后推送者会遇到非快进错误。当前方案内置一次 pull-rebase 重试；高频并发场景下可引入文件级锁（对同一仓库的同步操作串行化）。
- 凭据安全：task_mirror_credentials 存储在 SQLite 数据库中，需加密存储。可复用现有 AUTH_SECRET 派生的加密密钥对凭据进行 AES 加密。
- 文件编码与特殊字符：任务标题和描述可能包含换行符、Unicode 表情符号等。JSON 序列化自动转义这些字符，但外部工具解析时需正确处理 Unicode 转义序列。
- 与 GitHub Issues 同步的互操作：两项功能同时启用同一仓库时，GitHub issue 更新和任务镜像文件更新可能产生语义不一致。建议在项目配置中提供互斥或优先级选项。
- 初始全量同步：首次为已有大量任务的项目启用镜像时，需一次性写入所有任务文件并提交。此操作应通过后台任务执行并展示进度，避免阻塞 API。
