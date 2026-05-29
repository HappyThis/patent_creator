## 技术方案

本方案提出一种面向 AI agent 协作的任务文件镜像同步系统，在 Mission Control（以下简称 MC）的 SQLite 任务数据库与 Git 仓库中的机器可读任务文件之间建立双向同步通道。该系统使 MC 中创建和维护的任务，自动以结构化文件形式落地到代码仓库的约定目录中，同时保持 MC 看板作为主要工作入口不变。

### 任务镜像文件格式

系统定义一套与 MC 任务模型一一对应的机器可读文件格式。每个任务序列化为单个 YAML 文件，包含以下核心字段：id（MC 任务 ID）、title、description、status（inbox/assigned/in_progress/review/quality_review/done）、priority（low/medium/high/urgent）、assigned_to、created_by、created_at、updated_at、due_date、estimated_hours、actual_hours、outcome、resolution、tags（数组）、metadata（键值对）。文件顶部保留 YAML 前置元数据（front matter），正文区域存放 description 的 Markdown 内容，方便 agent 在文本编辑器中直接阅读和修改。

### 稳定任务标识到文件路径的映射

为实现外部 agent 无需访问 MC 数据库即可定位特定任务文件，系统设计确定性路径映射规则。任务文件的仓库内路径由三个组件拼接而成：配置的基础目录（默认为 .mc-tasks/）、项目标识（project slug 或 project_id）、以及任务 ID。完整路径格式为 .mc-tasks/<project-slug>/<task-id>.yaml。该映射规则具备双向可计算性：给定 MC 任务 ID 和所属项目，可直接推导出文件路径；给定文件路径，可解析出任务 ID 和项目，无需维护额外的索引文件或数据库表。

### Git 仓库同步引擎

同步引擎作为独立模块运行，不侵入 MC 现有任务 CRUD 路径。引擎由三部分组成：写入适配器、拉取合并器、以及后台轮询调度器。

写入适配器（push adapter）：在 MC 任务创建、更新或删除的事务提交成功后触发，采用 fire-and-forget 模式。适配器根据任务 ID 和项目 slug 计算目标文件路径，将任务数据序列化为 YAML，执行 git add、git commit（含任务变更摘要作为提交信息）、git push。写入适配器的任何失败均不影响 MC 任务操作的返回结果，仅记录错误日志并将失败事件写入重试队列。

拉取合并器（pull merger）：在后台定时轮询或 Webhook 触发时执行。拉取合并器执行 git fetch + git pull --rebase 获取远程仓库最新状态，扫描 .mc-tasks/ 目录下所有 YAML 文件，逐一解析并与 MC 数据库中的对应任务比较。比较依据为 updated_at 时间戳：若文件中的 updated_at 晚于数据库中同一任务的 updated_at，则以文件内容覆盖数据库记录；反之以数据库为准。为防止乒乓效应，数据库记录在写入适配器写入后会设置 git_synced_at 字段；拉取侧处理时，若文件更新时间与 git_synced_at 差距在阈值（如 5 秒）内则跳过。

后台轮询调度器：与现有 GitHub Sync Poller 类似，以可配置间隔（默认 60 秒）遍历所有启用了任务文件同步的项目，依次执行拉取合并。拉取合并也可通过 Webhook 主动触发，降低延迟。

### 冲突检测与处理

冲突场景分为两类：文件级冲突（同一任务文件在远程和本地仓库中均被修改）和语义级冲突（任务状态在 MC 和外部系统之间出现不一致）。

文件级冲突：写入适配器在 git push 被拒绝（non-fast-forward）时，先执行 git pull --rebase。若 rebase 无冲突则自动继续 push；若 rebase 产生合并冲突，系统采用"本地胜出"策略——以 MC 数据库中的当前任务状态覆盖冲突文件内容，使用 git checkout --ours 解决冲突后提交并推送。该策略保证了 MC 作为权威数据源的一致性。冲突事件记录到 audit_log 表并生成告警通知。

语义级冲突：拉取合并器比较时，除时间戳外还检查状态迁移合法性。例如，若外部文件将任务从 done 回退到 in_progress，拉取合并器拒绝该变更并记录警告，因为 MC 中可能已触发质量门禁（Aegis review）。MC 定义的状态迁移规则（如 inbox→assigned→in_progress→review→quality_review→done）作为拉取侧的准入过滤器。

### 删除同步与墓碑机制

当 MC 中任务被删除时，同步引擎需要将对应的仓库内文件标记为已删除而非物理删除文件。系统采用墓碑文件（tombstone）机制：在任务文件同目录下写入 <task-id>.tombstone.yaml，包含原任务 ID、删除时间戳、删除操作者，并将原任务 YAML 文件通过 git rm 移除。拉取合并器在其他副本中检测到墓碑文件后，同步删除本地 MC 数据库中对应任务（或在配置为保守模式时仅标记为 archived 状态）。墓碑文件自身在设定的保留窗口（默认 7 天）后由后台清理任务移除，防止仓库膨胀。

### 失败隔离与重试机制

同步引擎的所有 Git 操作均在独立子进程中执行，与 MC 主请求处理线程隔离。核心设计原则包括：（1）fire-and-forget 推送：任务写入适配器的 Git 操作以异步非阻塞方式触发，失败不向 API 调用方返回错误；（2）重试队列：推送失败的任务 ID 进入基于 SQLite 的重试表，按指数退避策略重试（1s→2s→4s→...→最大 300s），最多重试 5 次后标记为 dead 并产生告警；（3）本地工作副本：同步引擎维护独立的 Git 工作树（worktree），与用户代码工作区隔离，避免污染业务仓库的暂存区和工作区；（4）变更检测：拉取侧在每次 git fetch 后执行 git diff --name-only，仅处理 .mc-tasks/ 路径下的变更文件，避免不必要的全量扫描。

### 数据库扩展

为支持任务文件同步，MC 数据库需新增以下字段和表：

- tasks 表新增 file_sync_enabled（INTEGER，默认为 0，该任务是否启用文件同步）、file_synced_at（INTEGER，上次文件同步时间戳）
- projects 表新增 file_sync_repo（TEXT，任务文件镜像所在 Git 仓库地址）、file_sync_enabled（INTEGER）、file_sync_base_path（TEXT，默认 .mc-tasks）、file_sync_branch（TEXT，默认 main）
- 新增 file_sync_retry_queue 表：id、task_id、operation_type（push/delete）、retry_count、next_retry_at、last_error、created_at
- 新增 file_sync_log 表：id、task_id、operation_type、status（success/failed/conflict）、detail、created_at

### 处理流程

任务写入（push）流程：MC API 接收任务创建/更新请求 → 数据库事务提交 → 事件总线广播 task.created/task.updated → 文件同步适配器监听事件 → 检查任务所属项目是否启用 file_sync → 计算目标文件路径 → 在独立 Git worktree 中生成/更新 YAML 文件 → git add/commit/push → 更新 tasks.file_synced_at → 失败则写入 file_sync_retry_queue。

任务拉取（pull）流程：后台定时器或 Webhook 触发 → 遍历启用 file_sync 的项目 → git fetch origin → git diff --name-only 识别 .mc-tasks/ 下变更文件 → 逐一解析变更的 YAML 文件 → 对每个文件：比较 updated_at 与 db 记录的 git_synced_at → 若文件较新则检查状态迁移合法性 → 写入/更新 MC 数据库 tasks 表 → 记录 file_sync_log。

任务删除（tombstone）流程：MC API 接收删除请求 → 任务从 tasks 表删除 → 事件总线广播 task.deleted → 文件同步适配器在 worktree 中 git rm 原任务文件 → 写入墓碑文件 → git add/commit/push → 拉取侧检测墓碑文件 → 同步删除本地 MC 数据库中的对应记录。

### 技术效果

本方案相比现有方式（仅通过 GitHub Issues API 进行任务同步）带来以下技术效果：

- 离线可访问性：外部 agent 无需网络连接 MC 实例或 GitHub API，只需 git clone 仓库即可获取完整任务队列的机器可读快照，支持本地文本工具 grep、yq、jq 等直接查询和过滤任务。
- 传输载体复用：任务文件跟随代码仓库的分支、合并、标签等 Git 工作流自然流转，无需额外配置 API token 或 webhook。任务状态与代码变更可在同一 commit 中原子关联。
- 格式自主可控：YAML 文件格式由 MC 定义且不依赖任何第三方平台（如 GitHub Issues 的 API 语义），避免了 label 名称空间污染和状态映射复杂性。外部 agent 只需理解 YAML schema 即可消费。
- 写入隔离：fire-and-forget 推送 + 独立 Git worktree 的设计保证了即使远程仓库不可达、Git 操作超时或发生冲突，MC 任务看板的核心增删改操作不受任何影响，同步失败仅记录日志和触发重试。
- 确定性路径映射：任务 ID 到文件路径的映射规则使外部 agent 无需任何索引或查询接口，即可通过任务 ID 直接定位对应文件。多个 agent 可并发读取同一任务文件而无需访问共享数据库。
- 墓碑化删除：删除操作通过墓碑文件而非静默移除传播，使外部系统可以区分'任务不存在'和'任务已被删除'，支持审计追溯和误删恢复。

### 与现有 GitHub Issues 同步的差异

本方案的核心创新在于将任务管理系统的数据出口从 API 调用模式转变为文件系统镜像模式。与现有 GitHub Issues 同步机制（通过 REST API 将任务转换为 platform-specific 的 Issue 对象、依赖 label 命名约定传递状态语义）不同，本方案以代码仓库自身作为协作载体：任务状态直接序列化为仓库内结构化文件，通过 Git 的版本控制、分支、合并、diff 等原语实现分布式同步，消除了对第三方平台 API 的语义耦合。具体创新特征包括：确定性 ID→路径映射替代外部索引查询；Git worktree 隔离避免业务仓库污染；墓碑文件传播删除语义；以及文件级时间戳比较 + 状态迁移规则的双层冲突防护。

### 风险与待确认问题

以下方面需要在实际实现中进一步确认和细化：

- Git 并发推送：多 MC 实例同时对同一仓库执行 git push 时可能产生竞态。建议评估基于文件级锁或分布式锁（如 SQLite advisory lock + 仓库级互斥）的方案，或限定每个项目仅一个 MC 实例具有推送权限。
- 仓库权限模型：任务文件写入需要仓库的写权限，与现有 GitHub Issues 同步仅需 issue 读写权限不同。需明确是通过 deploy key、GitHub App installation token 还是用户 OAuth token 授权。
- 大任务量下的文件膨胀：若单个项目任务数超过数万条，.mc-tasks/ 目录下可能产生大量文件。可评估引入按时间或状态分桶的子目录结构（如 .mc-tasks/<project>/open/<task-id>.yaml 与 .mc-tasks/<project>/closed/<task-id>.yaml）。
- YAML vs JSON 选择：本方案选定 YAML 因其对 Markdown 多行文本友好且人类可读性高，但 YAML 解析性能低于 JSON。对性能敏感场景可提供 JSON 格式作为备选。
- 与现有 GitHub Issues 同步的共存策略：同一项目可能同时启用 GitHub Issues 同步和任务文件同步，两者需避免双向反馈环路。建议在 file_sync_log 和 github_syncs 表之间建立互斥标记机制。
