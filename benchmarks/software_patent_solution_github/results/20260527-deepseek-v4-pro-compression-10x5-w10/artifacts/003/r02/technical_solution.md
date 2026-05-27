## 技术方案

本方案提出一种任务数据可携带性增强机制，使 Mission Control（以下简称 MC）中创建和维护的任务能够以机器可读的任务镜像文件（Task Mirror File）形式同步至代码仓库（Git 仓库）中，供外部 agent 工具和开发者工作流直接读取、追踪和协作，同时保持 MC 任务看板和数据库作为主要工作入口不变。

### 整体架构

系统在现有 MC 任务管理架构之上增加一个任务镜像同步引擎（Task Mirror Sync Engine），由三个核心组件构成：镜像文件序列化器（Mirror Serializer）、Git 操作代理（Git Operator）和同步调度器（Sync Scheduler）。镜像文件序列化器负责将 MC 任务数据转换为标准化机器可读格式；Git 操作代理负责对目标仓库执行 clone/pull、文件写入、commit、push 等 Git 操作；同步调度器以事件驱动和定时轮询两种模式协调同步流程，并通过 MC 调度器框架注册为后台任务。

### 任务镜像文件格式

任务镜像文件采用 JSON 格式存储于目标 Git 仓库的指定目录（默认为 .mission-control/tasks/）下。每个文件对应一个 MC 任务，文件名为任务稳定标识符。文件结构包含以下字段：

- id：MC 中任务的数字主键，作为跨系统的稳定标识
- title：任务标题
- description：任务描述正文
- status：任务状态，取值 inbox / assigned / in_progress / review / quality_review / done
- priority：优先级，取值 low / medium / high / critical
- assigned_to：指派的 agent 名称
- project：所属项目名称及 ticket 引用
- created_at / updated_at：Unix 时间戳
- tags：标签数组
- metadata：扩展元数据对象
- sync_version：单调递增的同步版本号，用于冲突检测
- sync_source：固定为 mission-control，标明数据来源

### 稳定任务标识到文件路径的映射

为实现稳定、可预测的任务标识到仓库文件路径的映射，系统采用两级映射策略：

第一级映射：MC 任务 ID → 文件名。任务镜像文件以 MC 任务的数字主键（tasks.id）作为稳定标识，文件名为 task-{id}.json。该标识在任务创建时由 SQLite 自增主键生成，在任务整个生命周期内保持不变（即使任务被删除，该 ID 也不再复用），确保外部系统始终可以通过同一文件名追踪同一任务。

第二级映射：项目 → 仓库目录。每个 MC 项目可配置一个目标 Git 仓库地址（通过 projects 表的 task_mirror_repo 字段）和目标分支（task_mirror_branch）。镜像文件在仓库内的目录由项目 slug 决定，路径模式为 .mission-control/tasks/{project-slug}/task-{id}.json。这一设计使得多个 MC 项目可以独立同步到不同仓库或同一仓库的不同子目录。

文件路径的确定性由以下规则保证：(1) 任务 ID 在 MC 实例内全局唯一且不可变；(2) 项目 slug 在 workspace 内唯一且创建后不可修改；(3) 目录前缀 .mission-control/tasks/ 为固定常量。外部 agent 工具仅需知道 MC 实例的域名和项目 slug，即可构造出任意任务的镜像文件路径并直接访问。

### 同步引擎设计

同步引擎以两种模式运行：事件驱动同步和定时全量对账。

事件驱动同步（出站通道）：MC 内部任务发生创建、更新或删除操作时，通过事件总线（eventBus）触发相应处理。镜像文件序列化器从数据库读取最新的任务数据，将 Task 对象序列化为上述 JSON 格式，在 metadata 字段中记录当前 Unix 时间戳作为 sync_version，然后将文件写入本地 Git 工作副本、执行 git add、git commit、git push。整个过程以非阻塞 fire-and-forget 模式执行——同步操作在独立的异步路径中完成，失败仅记录日志和更新同步状态标记，不影响 MC 任务 API 的响应时间。

定时全量对账（双向通道）：同步调度器作为 MC 调度器的一个注册任务，以可配置间隔（默认 60 秒）执行。每次对账执行以下步骤：(1) git fetch 拉取远端最新状态；(2) 遍历所有开启了镜像同步的项目中处于活跃状态的任务，将每个任务的当前数据与本地镜像文件 diff 对比；(3) 对存在差异的任务，若 MC 侧数据的 updated_at 晚于镜像文件的 sync_version，则以 MC 为准覆盖镜像文件并提交推送；(4) 若镜像文件的 sync_version 晚于 MC 数据的 updated_at（表示外部 agent 曾修改过镜像文件），则读取镜像文件内容，反向更新 MC 数据库中的对应任务字段，并在 activity 日志中记录外部修改事件。

### Git 操作流程

Git 操作代理封装了完整的 Git 工作流。首次启用项目镜像同步时，代理执行 git clone --depth=1 获取目标仓库的指定分支到 MC 数据目录内的独立工作副本（路径为 .data/task-mirrors/{project-slug}/）。后续同步使用 git pull --rebase 更新本地副本。写入流程为：将序列化后的 JSON 内容写入目标路径 → git add {filepath} → git commit -m "[mission-control] task-{id}: {status} — {title}" → git push origin {branch}。commit message 中包含任务 ID、状态和标题摘要，使 Git 历史本身具有可追溯性。

关键设计决策：(1) 每个项目使用独立的 Git 工作副本，避免不同项目之间的文件冲突；(2) 使用 --depth=1 浅克隆减少磁盘占用和初始同步耗时；(3) commit 操作前检查是否有实际文件变更（git diff --name-only），无变更时跳过 commit/push 避免产生空提交。

### 冲突与失败处理

系统从三个层面处理同步过程中的异常：

（1）Git 操作失败：当 git push 因网络问题或远端冲突（non-fast-forward）失败时，同步引擎首先执行 git pull --rebase 将远端变更合并到本地，然后重新尝试 push。若 rebase 过程中产生文件冲突（同一任务镜像文件在 MC 和外部同时被修改），系统采用"最后写入者胜出"策略：比较 MC 侧 updated_at 时间戳和镜像文件内的 sync_version，以时间较新的一方为准覆盖另一方。冲突解决后自动 commit 并重试 push。若连续三次重试仍失败，该任务的本次同步被标记为 deferred，在下一个对账周期重新处理。

（2）MC 侧失败隔离：事件驱动的出站同步完全以异步 fire-and-forget 方式执行。任务创建、更新、删除的 API 响应在数据库写入完成后即刻返回，同步操作的执行和结果不影响 API 的 HTTP 响应。同步失败仅写入结构化日志并更新 tasks 表中的 task_mirror_sync_status 字段（取值 synced / pending / failed），供管理员在 MC 面板中查看。该字段的设计确保：日常任务操作不因外部仓库不可用而阻塞或报错。

（3）外部修改冲突消解：当定时对账发现镜像文件被外部 agent 修改时，系统将变更拉取到 MC 任务中，同时记录一条 activity 日志（类型为 task_mirror_external_update），包含修改来源（通过 Git author 信息推断）和变更内容摘要。MC 面板中对应任务显示"外部已修改"标记，提示用户关注。对于 MC 任务看板中的状态流转（如从 in_progress 拖动到 review），此类由明确人工操作触发的变更始终以 MC 侧为准，镜像文件的旧状态将被覆盖；此规则防止外部 agent 的自动修改意外覆盖用户的显式状态决策。

### 删除同步与墓碑机制

当 MC 中的任务被删除时，同步引擎不直接从仓库中删除对应的镜像文件，而是采用墓碑标记（Tombstone）策略：将镜像文件内容替换为一个精简的 JSON 对象，包含 id、deleted 标记（设为 true）、deleted_at 时间戳和 sync_source 字段，其余业务字段清空。同时保留原文件名不变。这一设计确保：(1) 外部 agent 可以通过文件存在性检测到任务已被删除，而非遇到"文件不存在"的模糊状态；(2) Git 历史中该文件从完整数据到墓碑标记的变更可被追踪；(3) 若任务被误删并需要恢复，MC 可以从墓碑文件中的 id 定位到该任务的原始数据备份。

墓碑文件在仓库中保留一个可配置的保留期（默认 30 天），超期后由定时清理任务执行 git rm 并提交推送，从仓库中物理删除。此机制平衡了可追溯性和仓库清洁度。

### 与现有系统的关系

本方案与 MC 已有 GitHub Issues 同步机制的关系是互补而非替代：(1) GitHub Issues 同步以 GitHub API 为通道，以 Issue 的 label 字段映射任务状态和优先级，适合在 GitHub UI 中查看和管理任务；任务镜像文件同步以 Git 仓库文件系统为通道，以结构化 JSON 文件为载体，适合 agent 工具和 CI/CD 流水线以文件读取方式消费任务数据。(2) 两者分享 projects 表中部分字段（如 github_repo），但使用独立的启用开关和同步调度器任务，互不干扰。(3) 任务镜像文件通过 sync_source 字段和文件路径命名规约表明数据来源，与 GitHub Issues 中通过标签前缀 "mc:" 识别 MC 来源的设计一致。

与 MC 现有事件总线（eventBus）和调度器（scheduler）框架的集成方式：同步引擎监听 eventBus 的 task.created、task.updated、task.deleted 事件触发即时出站同步；定时全量对账注册为调度器的一个 tick 任务（task_mirror_sync），与其他调度任务（task_dispatch、aegis_review 等）在同一框架下按相同节奏执行，共享调度器的启停控制和状态查询接口。

### 技术效果

(1) 可携带性：MC 任务数据以标准化 JSON 文件形式落地到 Git 仓库，任务状态跟随代码仓库流转，不再绑定于 MC 实例的 SQLite 数据库。任何能访问该 Git 仓库的工具或 agent 都能直接读取任务数据，无需访问 MC 数据库或 API。

(2) 跨工具协作：外部 agent 工具可通过读取仓库中 .mission-control/tasks/ 目录下的 JSON 文件获取当前任务队列，也可通过修改镜像文件并提交推送来反向更新 MC 中的任务状态，形成以 Git 仓库为中介的松耦合协作模式。

(3) 故障隔离：同步引擎以 fire-and-forget 模式运行，同步失败不影响 MC 的任务创建、更新、删除主流程。任务 API 的响应延迟不受 Git 操作耗时影响。

(4) 可追溯性：所有同步操作（包括外部修改）均以 Git commit 形式记录在仓库历史中，commit message 包含任务 ID、状态和操作摘要，结合墓碑机制，提供完整的任务生命周期审计追踪。

(5) 确定性映射：任务 ID 到文件路径的映射规则是纯确定性的，外部系统无需查询 MC 即可计算出任意任务的镜像文件路径，降低了跨系统集成的复杂度。

### 风险与待确认问题

(1) 仓库写入权限：任务镜像同步需要目标 Git 仓库的写入权限（通过 GITHUB_TOKEN 或 deploy key），该依赖已在现有 GitHub Issues 同步中建立，本项目复用同一认证机制。

(2) 大任务量下的 Git 性能：当 MC 中活跃任务数量较大（如超过 1000 个）时，定时全量对账的 git diff 遍历可能产生性能开销。可通过分页对账（每次最多处理 200 个任务）和增量文件时间戳预检优化。

(3) 外部修改的语义校验：当前方案对外部 agent 写入镜像文件的内容仅做基本字段类型校验，不做业务规则校验（如状态流转合法性）。若外部 agent 写入非法状态值，MC 在反向同步时会记录告警日志并保留 MC 侧原有状态不变。是否需要更严格的语义校验取决于实际部署场景。

(4) 与现有 GitHub Issues 同步的数据一致性：两个同步通道（Issue 同步和镜像文件同步）共享 tasks 表中的部分字段（如 github_repo），需要确保两者的启用开关相互独立，且各自的同步状态标记不互相覆盖。建议在 projects 表中使用独立字段 task_mirror_repo 和 task_mirror_enabled 与现有 github_repo / github_sync_enabled 区分。
