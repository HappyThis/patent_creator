## 技术方案

本技术方案提出一种面向 Mission Control 任务管理系统的任务数据外部镜像同步机制，用于将任务数据以机器可读的任务镜像文件形式同步至代码仓库中，使外部开发工具和 agent 工作流无需直接访问 Mission Control 数据库即可读取、追踪和协作处理任务。该方案以 Mission Control 现有任务看板和数据库为主工作入口，外部 Git 托管的任务镜像文件为可选同步目标，通过双向同步引擎、稳定任务标识映射、Git 操作事务管理和冲突/失败隔离机制，实现任务数据的可携带性和跨工具协作能力。

### 技术问题概述

Mission Control 作为 agent 编排控制台，其任务数据存储在本地 SQLite 数据库中，通过看板界面管理和调度。在分布式开发和跨工具协作场景下，存在以下问题：外部 agent 工具和 CI/CD 流水线无法在不访问 Mission Control API 的情况下获取当前任务队列状态；任务数据无法随代码仓库版本流转，导致任务上下文与代码变更脱节；当 Mission Control 服务不可用时，外部系统完全无法感知任务状态。现有 GitHub Issue 同步方式将任务映射为 GitHub Issue，但 Issue 并非代码仓库原生的机器可读文件，外部 agent 仍需通过 GitHub API 访问，且 Issue 的状态模型与 MC 任务模型存在语义差异。

### 整体架构

系统在 Mission Control 现有任务看板和 SQLite 数据库之上，新增任务镜像同步层。该层包含四个核心模块：任务序列化模块负责将 MC 任务数据转换为结构化任务镜像文件（JSON 格式）；路径映射模块基于稳定任务标识生成确定性的仓库内文件路径；Git 操作模块封装 clone、pull、commit、push 等 Git 操作；同步引擎模块协调双向同步流程，管理事件触发、轮询调度和冲突检测。外部协作载体为 Git 仓库中的任务镜像文件目录，外部 agent 工具通过读取该目录中的文件获取任务状态，无需访问 Mission Control API 或数据库。

### 任务镜像文件结构

每个 Mission Control 任务在 Git 仓库中被序列化为一个独立的 JSON 文件，包含任务的核心字段和同步元数据。文件结构定义如下：

- task_id：Mission Control 内部任务主键，作为全局唯一且稳定的任务标识
- ticket_ref：格式为 {项目前缀}-{编号} 的人类可读任务引用（如 PROJ-001）
- title、description：任务的标题和描述正文
- status：任务状态，取值包括 inbox、assigned、in_progress、review、quality_review、done
- priority：任务优先级，取值包括 low、medium、high、critical
- assigned_to：指派的目标 agent 名称
- tags：标签数组
- metadata：扩展元数据对象，包含 implementation_repo、code_location、dispatch_session_id 等自定义字段
- created_at、updated_at、due_date、completed_at：Unix 时间戳
- sync_version：单调递增的同步版本号，用于冲突检测
- sync_source：标记最后一次变更来源，取值为 mc 或 git

### 稳定任务标识到文件路径的映射

系统采用两级目录结构，基于项目分组和任务标识生成确定性文件路径，确保同一任务始终映射到同一文件位置。路径映射规则为：{repo_root}/.mission-control/{project_slug}/tasks/{task_id}.json。其中 project_slug 为项目在 Mission Control 中的唯一标识，task_id 为任务在数据库中的自增主键。

该映射的设计要点如下：（1）使用数据库主键 task_id 作为文件名主干，而非标题或 ticket_ref，因为主键在任务生命周期内绝对不变，标题和引用可能被修改；（2）使用 project_slug 而非项目名称作为目录名，因为 slug 是创建时确定的短标识，不受项目重命名影响；（3）.mission-control 顶层目录作为命名空间，避免与仓库中其他文件冲突，且以点号开头使其在类 Unix 系统的默认文件列表中被隐藏。外部 agent 工具通过扫描 .mission-control/{project_slug}/tasks/ 目录即可枚举全部任务镜像文件，无需额外的索引文件。

### 双向同步引擎

双向同步引擎是连接 Mission Control 数据库与 Git 仓库任务镜像文件的核心组件，支持出站同步（MC → Git）和入站同步（Git → MC）两个方向。

出站同步：当 Mission Control 中发生任务创建、更新或删除操作时，同步引擎在完成本地数据库写入后，以 fire-and-forget 方式异步触发出站同步流程。流程为：读取任务的当前数据库记录；根据 task_id 和 project_slug 计算目标文件路径；将任务记录序列化为 JSON 任务镜像文件；执行 git add、git commit（提交信息包含 task_id 和 ticket_ref）、git push 操作。出站同步失败不影响本地数据库操作，仅记录错误日志。

入站同步：系统通过定时轮询器（默认间隔 60 秒）检查 Git 远程仓库的变更。流程为：执行 git pull 拉取最新提交；遍历本次 pull 引入的文件变更，筛选 .mission-control/ 目录下的任务镜像文件；对每个变更文件，读取 JSON 内容并与对应数据库记录比对；如果文件的 sync_version 大于数据库记录的 sync_version，或 sync_source 为 git 且文件时间戳更新，则将该文件的变更应用到数据库。入站同步完成后记录同步日志。

### Git 操作与事务管理

Git 操作模块封装了与远程仓库交互的全部底层操作，采用以下设计保证操作的可靠性和一致性：

- 仓库准备：首次同步时，系统在 Mission Control 本地工作目录中执行 git clone 获取远程仓库；后续同步复用已有本地克隆，通过 git pull 获取增量更新。本地工作副本位于 Mission Control 数据目录下的 sync/{project_slug}/ 路径。
- 原子提交：出站同步将一个或多个任务变更合并为单个 git commit，commit message 包含变更摘要和被影响的任务标识列表。每个 commit 对应一次同步事务，避免产生大量无意义的微型提交。
- 推送重试：git push 失败时（如网络错误、远程冲突），系统执行指数退避重试策略，最大重试次数默认为 3 次，每次重试前执行 git pull --rebase 合并远程变更后再尝试推送。
- 文件删除操作：当 Mission Control 中任务被删除时，同步引擎通过 git rm 从仓库中移除对应的任务镜像文件，而非创建占位文件或标记字段。删除操作同样经过 commit 和 push 流程。

### 冲突检测与失败隔离

为防止 Mission Control 与外部 agent 同时对同一任务进行修改导致状态不一致，系统实现了多层防乒乓和冲突检测机制：

- 同步版本号：每个任务镜像文件中包含 sync_version 字段，每次通过 Mission Control 更新时单调递增。入站同步时，仅当文件的 sync_version 大于数据库中的版本号时才应用变更，防止过期数据覆盖。
- 同步源标记：sync_source 字段取值为 mc 或 git，标识最近一次变更的发起方。出站同步刚完成时，sync_source 被设为 mc，入站轮询器在短时间内（默认 10 秒窗口）检测到 sync_source 为 mc 的更新时跳过处理，避免自身产生的变更被重复拉回。
- 时间戳仲裁：当 sync_version 不可比对时（如外部 agent 直接编辑文件未更新版本号），采用 updated_at 时间戳作为次级仲裁依据，较新的时间戳优先。
- JSON 合并冲突：当 git pull --rebase 产生文件级合并冲突时，系统采用 MC 数据库版本优先策略，以数据库中的当前记录覆盖冲突文件，并将冲突前的远程版本备份到 .mission-control/.conflicts/ 目录供人工审查。
- 失败隔离：出站同步采用 fire-and-forget 异步模式，同步失败仅记录错误日志和失败计数，不阻塞、不回滚 Mission Control 的本地数据库操作。入站同步中，单个任务文件的解析或写入失败不中断其他文件的同步流程。

### 删除同步机制

任务删除同步是本方案区别于仅支持状态标记删除的关键设计。当 Mission Control 中任务被删除时，同步引擎通过以下流程确保仓库中的任务镜像文件也被对应移除：

1. 数据库监听：任务删除操作触发数据库级联删除（comments、task_subscriptions、quality_reviews 等相关记录同步移除），并通过事件总线广播 task.deleted 事件，事件数据中包含被删除任务的 id、title 和 project_id。
2. 路径反算：同步引擎监听 task.deleted 事件，根据被删除任务的 id 和所属项目的 slug 反算出对应的仓库文件路径 .mission-control/{project_slug}/tasks/{task_id}.json。
3. Git 删除：同步引擎对本地仓库副本执行 git rm {file_path}，生成 commit（message 包含 "delete task {task_id}" 标识），并推送至远程。
4. 删除幂等性：如果对应的任务镜像文件已不存在（例如已被外部 agent 提前删除），git rm 操作失败时同步引擎捕获该错误并以警告日志记录，不阻塞其他删除同步。
5. 墓碑记录（可选）：在 .mission-control/.deleted/ 目录下写入一个仅包含 task_id 和 deleted_at 时间戳的墓碑文件，使外部 agent 可感知曾有任务被删除，而不依赖 Git 历史查询。

### 技术效果

本技术方案在以下几方面相对于现有方式产生技术效果：

- 直接可读性：外部 agent 工具通过读取 Git 仓库中的 JSON 文件即可获取任务状态，无需依赖 Mission Control API 或数据库连接，支持离线场景和 CI/CD 流水线中的批量任务处理。
- 版本可追溯：任务状态变更以 Git commit 历史的形式完整保留，每次变更可追溯到具体的操作人员和变更时间，支持 git diff 比对任务内容的差异。
- 语义一致性：任务镜像文件直接映射 Mission Control 任务数据模型（status、priority、assigned_to 等），避免通过 GitHub Issue、Label 等外部抽象层转换引入语义偏差。
- 主路径保护：出站同步以 fire-and-forget 异步模式运行，同步失败不阻塞 Mission Control 的日常任务 CRUD 操作，确保任务看板始终可用。
- 跨工具协作：多个外部 agent 工具可并行读取同一仓库中不同项目或不同任务的文件，通过 Git 的分支和合并机制支持多工作流并行演进，最终由 Mission Control 统一收敛。

### 风险与待确认问题

以下为当前方案中需要后续确认或持续关注的风险点：

- 大文件仓库性能：当任务数量达到数千级别时，单层目录下的 JSON 文件数量可能影响 Git 操作性能和文件系统扫描速度，可考虑按 task_id 范围分片（如 tasks/000-099/、tasks/100-199/）。
- 并发写入冲突：当多个 Mission Control 实例同时向同一 Git 仓库执行出站同步时，可能出现 push 竞争，当前重试机制可缓解但无法完全消除，需评估是否需要引入分布式锁。
- 外部 agent 直接修改文件：外部 agent 可能以不符合 JSON Schema 的方式编辑任务镜像文件，入站同步解析时需做好容错处理和 schema 校验失败的降级策略。
- Git 仓库权限管理：仓库的读写权限由 Git 托管平台控制，Mission Control 本身不管理外部 agent 的访问权限，需依赖仓库的 collaborator 或 team 权限机制。
