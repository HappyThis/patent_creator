## 技术方案

### 技术问题概述

Mission Control 当前以 SQLite 数据库和看板为任务管理的唯一事实来源。任务数据的创建、更新、删除均在本地数据库中完成。现有 GitHub Issue 同步机制虽然支持将任务以 Issue 形式推送到 GitHub，但 Issue 是平台绑定的协作格式，不同 agent 工具或 CI/CD 流水线需通过 GitHub API 访问，无法直接以文件形式在代码仓库中读取、版本追踪或离线处理。

本方案旨在为 Mission Control 增加一种可选的外部协作格式：将任务数据以机器可读的任务镜像文件（Markdown + YAML frontmatter）写入关联的 Git 仓库，使外部 agent 工具和开发工作流无需访问 Mission Control 数据库即可读取、追踪和参与任务状态变更，同时确保 Mission Control 自身数据库和看板始终作为主工作入口，外部同步失败不影响日常任务操作。

### 整体架构

系统在现有 Mission Control 架构上新增三个核心模块：任务镜像同步引擎（TaskMirrorSyncEngine）、仓库任务文件管理器（RepoTaskFileManager）和同步调度器（MirrorSyncScheduler）。

任务镜像同步引擎是核心调度层，监听 Mission Control 内部事件总线（eventBus）上的 task.created、task.updated、task.deleted 事件，将变更写入任务镜像文件并通过 Git 提交推送至关联仓库。仓库任务文件管理器负责文件路径解析、YAML frontmatter 序列化/反序列化、文件读写及目录结构维护。同步调度器按可配置间隔轮询关联仓库的远程变更，解析新增或修改的任务镜像文件并反向同步至 Mission Control 数据库。

三个模块均通过现有事件总线与数据库层解耦。数据库层新增 task_mirrors 表记录任务 ID 与仓库文件路径的映射关系及同步时间戳。整个同步链路是附加路径，不修改 tasks 表的 CRUD 路由核心逻辑，确保 Mission Control 日常任务操作不受影响。

### 任务镜像文件结构

每个 Mission Control 任务在关联仓库的 .mission-control/tasks/ 目录下对应一个 Markdown 文件。文件采用 YAML frontmatter + Markdown 正文的双层结构。

YAML frontmatter 包含机器可解析的元数据字段：mc_task_id（Mission Control 任务 ID，作为稳定跨系统标识）、title、status（inbox/assigned/in_progress/review/quality_review/done）、priority（low/medium/high/urgent）、assigned_to、created_by、created_at、updated_at、due_date、estimated_hours、tags（数组）、outcome、project 对象（含 project_id、project_name、ticket_ref）、github 对象（含 issue_number、repo、branch、pr_number，用于与现有 GitHub Issue 同步的互引用）。Markdown 正文部分为任务的 description 字段原文。

该结构优先选用 YAML frontmatter 而非纯 JSON 文件，因为 frontmatter 格式被 Jekyll、Hugo、Obsidian 等静态站点和知识管理工具广泛支持，agent 工具无需专门解析器即可区分元数据和正文。同时，GitHub/GitLab 等平台可直接渲染该 Markdown 文件，实现仓库内的可视化浏览。

### 稳定任务标识到文件路径的映射

系统通过稳定的一对一映射将 Mission Control 任务 ID 转化为仓库内的确定性文件路径，保证同一任务始终对应同一文件，外部工具可基于任务 ID 直接定位文件而不需额外查询。

映射规则：文件路径 = {repo_root}/.mission-control/tasks/{sub_dir}/{task_id}-{slug}.md。其中 task_id 为 Mission Control 数据库中的 tasks.id（自增整数）；slug 由任务标题经小写化、空白替换为连字符、去除非字母数字字符后截取前 64 字符生成，用于提升文件列表的人类可读性；sub_dir 采用 task_id 对 1000 取模的值作为两级目录分片（如 task_id=2048 对应 2/048/），避免单目录下文件数量过大影响文件系统性能。

task_mirrors 表记录每条映射：task_id（主键关联）、repo_path（仓库根路径）、file_path（相对文件路径）、last_mirrored_at（最近镜像写入时间戳）、last_pulled_at（最近从仓库读取时间戳）、mirror_status（synced/conflict/tombstone）。外部工具可通过解析文件 frontmatter 中的 mc_task_id 字段反向定位 Mission Control 中的对应任务。

### 同步引擎设计

同步引擎分为出站同步（Mission Control → 仓库）和入站同步（仓库 → Mission Control）两条独立链路，均以异步非阻塞方式运行。

出站同步：通过订阅 eventBus 的 task.created、task.updated、task.deleted 事件触发。事件处理器首先检查该任务所属 project 是否启用了仓库镜像同步（projects 表新增 repo_mirror_enabled 和 repo_mirror_path 字段）。若启用，则调用 RepoTaskFileManager 将当前任务序列化为 frontmatter Markdown 文件，写入本地仓库工作目录，执行 git add + git commit（提交信息包含 mc_task_id），再执行 git push。整个过程在 Promise.catch 中捕获异常，失败仅记录日志和更新 task_mirrors.mirror_status 为 error，不向调用方抛出错误。

入站同步：MirrorSyncScheduler 按可配置间隔（默认 60 秒）对每个启用镜像同步的 project 执行 git pull，扫描 .mission-control/tasks/ 目录下所有 .md 文件。对每个文件解析 frontmatter 提取 mc_task_id，与 task_mirrors 表比对 last_pulled_at 与文件修改时间。若文件较新，则将文件内容反序列化为任务字段，通过参数化 SQL UPDATE 写入 tasks 表，并更新 last_pulled_at。若 mc_task_id 在数据库中不存在且非 tombstone 文件，则创建新任务。

入站同步复用现有数据库访问层（db.ts 的 getDatabase()），保持事务一致性和外键约束，同时触发 eventBus 广播 task.updated 事件以通知 SSE 客户端更新看板。

### 冲突检测与处理

冲突场景发生在同一任务在 Mission Control 和外部仓库中几乎同时被修改时。系统采用基于时间戳的最终写入胜出（Last-Write-Wins）策略，辅以同步日志实现可审计的冲突消解。

出站冲突处理：出站同步在 git push 前先执行 git pull --rebase。若 rebase 过程中任务镜像文件出现合并冲突，同步引擎放弃本次自动合并，将冲突文件以 .conflict 后缀保留在工作目录中，在 task_mirrors 表中记录 mirror_status='conflict' 及冲突时间。同时通过 eventBus 广播 notification.created 事件，在 Mission Control 通知中心生成一条冲突提醒，告知用户手动解决。

入站冲突处理：扫描到仓库文件比本地记录新时，比较文件的 updated_at 字段与 tasks 表的 updated_at 字段。若任务在 Mission Control 中最近被修改（updated_at 更新），则不覆盖本地数据，仅在 task_mirrors.last_pulled_at 记录检查时间点并增加 conflict_count。若仓库文件的 updated_at 更新（即外部修改是最近一次变更），则以仓库内容更新数据库。这避免了现有 GitHub Issue 同步中的绝对时间戳近似比较（±10 秒窗口）带来的不确定性。

所有冲突事件写入 task_sync_log 表（包含 task_id、direction、conflict_type、local_updated_at、remote_updated_at、resolution），供审计追溯。

### 删除同步与墓碑机制

任务删除的同步是双向协作的关键边界条件。系统通过墓碑标记机制区分「任务已删除」与「文件不存在（尚未同步）」，避免入站同步将已删除的任务重新创建。

出站删除同步：当 eventBus 收到 task.deleted 事件，同步引擎不在仓库中直接删除任务镜像文件，而是将文件的 frontmatter 中 status 字段设为 deleted 并追加 tombstone_at 时间戳，同时将文件从 .mission-control/tasks/{sub_dir}/ 移至 .mission-control/tasks/.tombstones/{task_id}-{slug}.md。提交信息标记为 "[MC-DELETE] task #{task_id}"，推送到远端。这种做法确保外部工具可通过墓碑文件获知删除事件，而非静默消失。

入站删除识别：入站同步扫描时同时检查主目录和 .tombstones/ 目录。若发现墓碑文件且其 tombstone_at 晚于 Mission Control 中对应任务的最新更新时间，则在 Mission Control 中执行 DELETE（通过现有 tasks 路由的相同数据库操作）。若主目录和墓碑目录均不存在该任务的文件，则视为未同步状态，不采取删除动作。

任务在 Mission Control 中被删除后，task_mirrors 记录保留但 status 更新为 tombstone，保留映射历史便于审计。外部 agent 可通过墓碑文件的时间戳和 mc_task_id 判定任务生命周期终止点。

### 失败隔离与容错

整个同步链路遵循「主路径优先、同步路径降级」原则：Mission Control 任务 CRUD 操作始终在 SQLite 事务中完成并返回成功响应，同步操作以 fire-and-forget 模式在事件处理器中异步执行。任何同步阶段（文件写入、git add、git commit、git push、git pull）的失败均不影响用户对任务的创建、更新或删除操作。

具体容错措施包括：（1）Git 操作超时控制，默认 30 秒超时，超时后中止本次同步并在 task_mirrors 记录 error 状态；（2）Git 仓库未初始化或远程不可达时，同步引擎进入退避模式（backoff），按 1 分钟、2 分钟、4 分钟、8 分钟递增间隔重试，最大退避 15 分钟；（3）task_sync_log 表记录每次同步操作的开始时间、结束时间、操作类型（push/pull）、结果状态及错误详情；（4）同步引擎自身异常不传播至任务 CRUD 路由的调用栈，通过独立的 error boundary 捕获和日志记录。

当退避重试连续失败超过配置的最大重试次数（默认 10 次）后，同步引擎将该 project 的 repo_mirror_status 设为 stalled，并生成 Mission Control 系统告警通知管理员检查仓库连接。管理员可通过项目管理界面手动触发重置和重新同步。

### 数据库扩展

为支持仓库任务镜像功能，数据库在现有迁移框架下新增以下表和列扩展，全部通过 ALTER TABLE 和 CREATE TABLE IF NOT EXISTS 以向后兼容方式添加。

projects 表新增列：repo_mirror_enabled（INTEGER，默认 0，控制仓库镜像同步开关）、repo_mirror_path（TEXT，关联的本地 Git 仓库路径）、repo_mirror_status（TEXT，active/stalled/error）、repo_mirror_remote（TEXT，远程仓库 URL）。这些列与现有 github_repo、github_sync_enabled 列并列，互不干扰。

新增 task_mirrors 表：task_id（INTEGER，主键关联 tasks.id）、repo_path（TEXT）、file_path（TEXT，相对 .mission-control/tasks/ 的路径）、last_mirrored_at（INTEGER，Unix 时间戳）、last_pulled_at（INTEGER）、mirror_status（TEXT，synced/conflict/tombstone/error）、conflict_count（INTEGER，默认 0）。

新增 task_sync_log 表：id（自增主键）、task_id、direction（inbound/outbound）、operation（create/update/delete/conflict）、local_updated_at、remote_updated_at、status（success/failed/conflict）、error_detail（TEXT）、created_at。该表提供同步操作的可审计日志，支持排查同步异常和冲突溯源。

### 关键规则与状态一致性

为防止不同系统之间出现任务状态的含义偏差，方案定义了一套统一的字段语义规范和状态解释规则，在 Mission Control 和外部任务镜像文件中一致执行。

状态映射规则：Mission Control 的六种任务状态（inbox、assigned、in_progress、review、quality_review、done）在镜像文件中以原值直接写入 frontmatter 的 status 字段，不做转换。外部 agent 工具写入或修改 status 字段时，必须使用上述六种枚举值之一；入站同步在解析到非法 status 值时，将其映射为 inbox 并在 task_sync_log 中记录 warning，同时保留文件中的原始值在 metadata.original_status 供人工排查。优先级和 outcome 字段同理：priority 限定为 low/medium/high/urgent，outcome 限定为 success/failed/partial/abandoned。

语义一致性保障：入站同步的 SQL UPDATE 复用现有 PUT /api/tasks/[id] 路由的同款参数化更新逻辑，包括 Aegis 审批门禁检查（done 状态前需 quality_review 审批）、mentions 解析、自动订阅和通知生成。这确保无论变更是来自 Mission Control 用户界面还是外部仓库，任务状态转换都遵循同一套业务规则。外部 agent 无法绕过 Aegis 审批直接将任务标记为 done。

### 技术效果

相比现有仅支持 GitHub Issue 同步的方案，本技术方案带来以下技术效果：

第一，消除平台绑定。任务镜像文件是纯文本的 Markdown + YAML frontmatter，无需 GitHub API 或任何平台专有接口即可被任何支持文件系统读取的工具消费。CI/CD 流水线可通过简单的 YAML 解析获取任务状态，代码审查工具可在 PR 中自动关联任务文件变更。

第二，Git 原生版本追踪。每个任务的每次状态变更在仓库中对应一次 Git 提交，外部 agent 可通过 git log 追溯任务从创建到关闭的完整生命周期，包括谁在何时修改了什么字段。这比 GitHub Issue 的时间线 API 更易于脚本化处理和离线分析。

第三，任务数据与代码同仓流转。任务镜像文件与项目代码位于同一仓库，代码分支可以直接携带对应任务文件。当开发者在 feature 分支中修改任务文件标记进度时，该变更随代码合并请求一起提交 review，实现任务状态与代码变更的原子化追踪。

第四，渐进式采用。仓库镜像同步通过 projects 表的 repo_mirror_enabled 字段按 project 粒度启用，不与现有 GitHub Issue 同步冲突。未启用的 project 保持原有行为不变，已启用 GitHub Issue 同步的 project 可同时开启仓库镜像同步，两者独立运作。

### 风险与待确认问题

以下几点需要在实施前进一步确认或细化：

（1）Git 操作库选择：当前项目依赖中不含 Node.js Git 客户端。建议引入 isomorphic-git（纯 JavaScript 实现，无需系统 Git）或 simple-git（需系统安装 Git）。前者利于容器化部署但 API 复杂度较高，后者对 monorepo 和大仓库性能更好。

（2）大任务量下的文件系统性能：当单 project 任务数超过 10 万时，.mission-control/tasks/ 目录的文件数量即使有子目录分片（1000 取模），也可能在 git status 和 git pull 时产生性能压力。可考虑引入 pack 机制——将已完成（done/deprecated）任务的镜像文件合并为按月的归档 JSON。

（3）多实例并发安全：当前 Mission Control 为单进程 Next.js 应用，SQLite WAL 模式提供了一定并发能力。若未来水平扩展至多实例，入站同步调度器需引入分布式锁（如基于 SQLite 的 advisory lock 或外部 Redis 锁）防止多个实例同时执行同一 project 的 git pull 和数据库写入。

（4）敏感数据泄露风险：任务描述中可能包含凭证或内部信息。镜像文件写入仓库后受仓库的访问控制策略保护，但需在同步前对 description 内容执行现有的 secret-scanner 检测（src/lib/secret-scanner.ts），发现疑似凭证时拒绝写入镜像文件并生成安全告警。
