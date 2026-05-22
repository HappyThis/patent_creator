## 技术方案

本方案提出一种基于 Git 仓库的任务文件镜像同步机制，使得 Mission Control 中创建和维护的任务数据能够以机器可读、人类可审阅的文件形式持久化在代码仓库中，供外部开发者和 agent 工作流读取、同步和追踪。该机制在保留 Mission Control 现有任务看板和 SQLite 数据库作为主要工作入口的前提下，将 Git 托管的协作文件格式作为可选同步目标，实现任务数据的跨工具可携带性。

### 整体架构

系统在现有 Mission Control 架构上增加一条独立的文件同步通道，与已有的 GitHub Issues 同步通道并行运行但互不干扰。整体架构自上而下分为五层：

1. API 层：复用现有任务 API（POST/PUT/DELETE /api/tasks）的请求处理流程，在 SQLite 事务提交成功后，以 fire-and-forget 模式触发文件同步钩子，不阻塞 API 响应。
2. 事件调度层：新增 file-sync-engine 模块，通过订阅 event-bus 的 task.created、task.updated、task.deleted 事件编排同步操作。该模块与现有 github-sync-engine 共享事件源但走独立输出通道。
3. 序列化层：新增 task-file-serializer 模块，负责 Task 数据结构与文件镜像格式之间的双向转换，以及从任务标识到文件路径的确定性映射。
4. Git 操作层：新增 git-ops 模块，封装 git clone、pull、add、commit、push 等原语，对每个已启用文件同步的 Project 维护一个轻量级本地工作副本缓存。
5. 冲突处理层：新增 file-conflict-resolver 模块，基于时间戳窗口和字段级比较实现反 ping-pong 机制，并在 Git 文本合并冲突时生成告警标记。

### 任务文件镜像格式与路径映射

每个 Mission Control 任务被序列化为一个独立的 Markdown 文件，文件以 YAML frontmatter 承载结构化字段，以 Markdown 正文承载任务描述。该格式兼具人类可读性和机器可解析性，且适合 Git diff 展示变更。

文件 frontmatter 包含以下字段：id（Task.id，稳定主键）、ticket_ref（由 project_ticket_no 与 ticket_prefix 拼接的任务编号）、title、status、priority、assigned_to、created_by、project_slug、tags（YAML 数组）、due_date、estimated_hours、actual_hours、outcome、resolution 以及 mc_updated_at——记录 Mission Control 端最后一次修改该任务的时间戳，用作反 ping-pong 的冲突检测锚点。metadata 中的额外 JSON 键值对平铺为 frontmatter 中的对应 YAML 键。

任务到文件路径的映射规则为：projects/<project_slug>/tasks/<ticket_ref>.md。其中 project_slug 取自 Project 表的 slug 字段，ticket_ref 优先使用 project_ticket_no 与 ticket_prefix 拼接结果，回退为 TASK-<id>。该映射为纯函数，保证同一任务始终映射到同一路径，外部系统可以通过 ticket_ref 稳定引用。

### 核心模块与职责

系统新增四个核心模块，各司其职：

- file-sync-engine（调度引擎）：作为同步流程的总编排器，订阅 event-bus 的 task.created、task.updated 和 task.deleted 事件。对 task.created 事件，调用序列化器生成新文件并推送到 Git 仓库；对 task.updated 事件，读取仓库中现有文件、执行冲突检测、写入更新后推送到 Git 仓库；对 task.deleted 事件，执行 Git 删除操作并推送。同时提供 pollRepository 方法，用于定时从 Git 仓库拉取远端变更并反向同步到 Mission Control 数据库。
- task-file-serializer（序列化器）：提供 serializeTaskToFile 方法将 Task 对象转换为 Markdown with YAML frontmatter 格式的字符串；提供 parseFileToContent 方法将文件内容解析为可合并的 Task 字段集合；提供 generateFilePath 方法基于 project_slug 和 ticket_ref 计算确定性的文件路径。
- git-ops（Git 操作封装）：对每个启用文件同步的 Project，在 Mission Control 数据目录下维护一个轻量级本地 Git 工作副本（路径：<data-dir>/git-repos/<project-id>/）。提供 ensureClone、pull、commitAndPush、getFileHistory 等方法，封装 Git 原语并处理网络异常和认证失败。
- file-conflict-resolver（冲突处理）：实现基于 mc_updated_at 时间戳窗口的反 ping-pong 判定逻辑，以及 Last-Writer-Wins 的并发写入策略。当 Git 文本合并冲突无法自动解决时，保留 Mission Control 版本、在冲突文件旁生成 .conflict.md 通知文件供人工介入。

### 同步触发与执行流程

同步分为出站（Mission Control → Git 仓库）和入站（Git 仓库 → Mission Control）两个方向。

出站同步流程：当用户在 Mission Control 看板上创建、更新或删除任务时，API 处理程序（tasks/route.ts 或 tasks/[id]/route.ts）在 SQLite 事务提交成功后，以 fire-and-forget 模式调用 pushTaskToGitRepo 方法。该方法执行以下步骤：（1）检查 Project 的 github_sync_enabled 和 github_repo 字段，确定目标仓库已启用同步；（2）调用 git-ops 的 ensureClone 确保本地工作副本存在，然后执行 pull 获取远端最新状态；（3）调用 task-file-serializer 序列化当前任务为文件内容；（4）调用 file-conflict-resolver 检查是否需要跳过（例如 10 秒内的 echo）；（5）将文件写入工作副本，执行 git add、git commit、git push。出站同步失败（如网络异常或认证失败）不影响 API 返回，仅在日志中记录错误，后续定时对账机制会重试。

入站同步流程：系统通过定时器（复用现有 scheduler.ts 的调度框架）周期性地对每个启用文件同步的 Project 执行 pollRepository。步骤为：（1）git pull 拉取远端最新提交；（2）通过 git diff --name-only 获取变更文件列表；（3）对每个变更文件调用 task-file-serializer.parseFileToContent 解析文件内容；（4）提取 frontmatter 中的 id 和 mc_updated_at 字段；（5）查询 Mission Control 数据库中对应 Task 记录的 updated_at；（6）比较远端 mc_updated_at 与本地 updated_at：若差值小于阈值（默认 10 秒），判定为 echo 并跳过；若远端更新且本地无并发修改，将远端内容合并到 Mission Control 数据库；若双方均有修改，采用 Last-Writer-Wins 策略。（7）记录同步结果到 github_syncs 表中。

### 冲突检测与失败处理

冲突处理是本方案的关键设计，确保多源写入场景下数据一致性。系统将冲突场景分为三类：

- Echo 跳过：当远端文件的 mc_updated_at 与本地 Task 的 updated_at 差值小于阈值（默认 10 秒）时，判定该变更为 Mission Control 刚推送出去的同步回波，直接跳过。该机制与现有 github-sync-engine.ts 中基于 github_synced_at 的时间戳窗口策略一致，但适配文件场景的 frontmatter 锚点字段。
- 真正并发冲突：当远端和本地在时间戳差值超过阈值的情况下均有非同步触发的修改时，采用 Last-Writer-Wins 策略，以 mc_updated_at 较大的版本覆盖另一方，并记录日志和 activity。Mission Control 数据库始终是权威数据源，文件镜像为衍生副本。
- Git 文本合并冲突：当同一文件的同一行在两端分别被修改且 Git 无法自动合并时，保留 Mission Control 版本作为合入版本，在冲突路径旁写入同名 .conflict.md 文件，包含冲突双方的差异内容和产生时间，通知仓库维护者人工介入。同时通过 Mission Control 的通知系统（db_helpers.createNotification）向项目相关 agent 发送冲突告警。

出站同步失败（Git push 被远端拒绝、网络超时或认证失败）时，系统不阻塞 Mission Control 的正常任务操作。同步请求在本地工作副本中保持未推送状态，下一次出站同步或入站轮询时重新尝试推送。同时，在 github_syncs 表中记录失败日志，包含错误信息、失败时间和重试计数，供运维排查。连续失败超过阈值时触发 alert_rules 告警。

### 删除同步策略

当用户在 Mission Control 中删除任务时，系统通过两种可配置的策略处理 Git 仓库中的对应文件：

- 物理删除模式（默认）：在出站同步中执行 git rm 删除对应任务文件，提交并推送。文件从仓库历史中移除，但可通过 Git 历史回溯查看。该模式保持仓库内容与 Mission Control 任务列表的严格一致。
- 墓碑标记模式：将任务文件的状态字段更新为 deleted，在文件头部追加删除时间和删除者信息，保留文件在仓库中的位置。该模式适合需要长期审计追踪的场景，外部 agent 可通过过滤 status != 'deleted' 获取活跃任务列表。

模式选择通过 Project 的 metadata 字段中的 file_sync.delete_mode 配置项指定（取值为 physical 或 tombstone，缺省为 physical）。入站方向不处理删除：文件镜像中标记为 deleted 的任务不会反向创建或删除 Mission Control 中的任务记录，以避免误删。

### 与现有 GitHub Issues 同步的关系

本方案的文件镜像同步通道与现有 GitHub Issues 同步通道（github-sync-engine.ts）在设计上保持独立但共享基础设施。两者核心差异如下：

- 输出载体：文件镜像同步输出为仓库内的 Markdown 文件（路径由 project_slug 和 ticket_ref 确定），GitHub Issues 同步输出为 GitHub Issue 对象（通过 REST API 创建/更新，以 github_issue_number 关联）。
- 状态映射：文件镜像同步直接使用 Mission Control 原生的 status 和 priority 字符串值，不作转换；GitHub Issues 同步通过 github-label-map.ts 将状态映射为 mc:inbox、mc:in-progress 等标签，将优先级映射为 priority:high 等标签。
- 冲突检测锚点：文件镜像同步使用 frontmatter 中的 mc_updated_at 字段作为反 ping-pong 的时间锚点；GitHub Issues 同步使用 tasks 表中的 github_synced_at 列。
- ID 关联：文件镜像同步通过 ticket_ref 和文件路径定位任务；GitHub Issues 同步通过 github_issue_number 列和 github_repo 列关联。
- 启用控制：两者通过 Project 表中的独立配置项控制——github_sync_enabled 控制 GitHub Issues 同步，新增的 file_sync_enabled 控制文件镜像同步。一个 Project 可以同时启用两种同步，也可以只启用其中一种。

两者共享 Project 的 github_repo 字段作为目标仓库标识，共享 github_syncs 表记录同步历史，共享 scheduler.ts 的定时调度框架驱动入站轮询。出站同步均采用 fire-and-forget 模式，失败不回滚 Mission Control 数据库操作。

### 技术效果

本方案通过引入 Git 仓库任务文件镜像同步机制，实现以下技术效果：

- 任务数据可携带：任务不再锁定在 Mission Control 的 SQLite 数据库中，而是以标准化、机器可读的文件格式持久化在 Git 仓库中。外部 CI/CD 流水线、代码编辑器和 agent 工作流可直接通过文件系统读取任务队列，无需接入 Mission Control API 或数据库。
- 确定性路径引用：通过 ticket_ref 到文件路径的纯函数映射，外部系统可构造稳定的文件路径引用特定任务（例如在 commit message 或 PR 描述中通过文件路径关联任务），实现跨工具的稳定关联。
- Git 原生协作能力：任务变更以 Git 提交的形式呈现完整历史，支持 diff、blame、cherry-pick 等 Git 原语。团队成员可通过标准的 Git 工作流审阅任务变更，任务状态随代码仓库分支流转。
- 数据库与同步解耦：出站同步采用 fire-and-forget 模式，同步失败不阻塞 Mission Control 的日常任务创建、更新或删除。Mission Control 数据库始终是权威数据源，文件镜像为衍生副本，不存在因外部系统不可用而影响核心任务操作的风险。
- 双重同步通道并行：文件镜像同步与 GitHub Issues 同步作为两条独立通道可同时启用、各自独立配置，满足不同协作场景需求而不互相干扰。

### 风险与待确认问题

- 大仓库性能：当 Project 任务量较大（如数千个任务文件）时，git pull 和 git diff 操作耗时增加，需评估是否需要文件分片目录（如按 ticket_ref 前缀分子目录）减轻单目录压力。
- Git 合并冲突的人工介入：当同一任务文件在 Mission Control 端和仓库端被并发修改时，即使 Last-Writer-Wins 覆盖了数据层面，Git 文本合并冲突仍可能发生。.conflict.md 通知文件依赖于人工或 agent 主动发现和处理。
- 入站同步的语义保真度：外部 agent 直接编辑任务文件时，可能写入 Mission Control 不识别的 status 值或缺失必填字段。入站同步需要在 parseFileToContent 阶段进行 schema 校验，对不合法值拒绝合入或回退为安全默认值。
- Fire-and-forget 的对账需求：长期异步推送失败可能导致仓库端滞后。需要定期全量对账（扫描 Mission Control 所有任务与仓库文件列表比对），该对账机制当前方案未详细设计。
- 文件格式兼容性：如果后续 Mission Control 的 Task 数据模型新增字段，前端 YAML frontmatter 格式需保持向后兼容，外部系统解析旧格式文件不应出错。建议采用宽松解析策略：忽略未知字段，保留原始格式中未能映射的键值。
