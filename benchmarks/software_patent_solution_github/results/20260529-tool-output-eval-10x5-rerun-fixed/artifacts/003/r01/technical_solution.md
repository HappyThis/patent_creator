## 技术方案

### 1. 技术问题

Mission Control 以 SQLite 数据库作为任务数据的主存储，提供看板界面用于任务创建、状态流转、分配和追踪。目前已有的 GitHub Issue 同步方案将任务映射为 GitHub 平台上的 Issue 实体，依赖 GitHub API、标签系统和 Issue 编号。然而，当外部 agent 工具需要在无网络访问 Mission Control 数据库的情况下理解任务队列，或团队希望任务状态跟随代码仓库通过标准 Git 工作流流转时，单纯依赖 GitHub Issue 同步存在以下不足：（1）任务数据绑定了特定 SaaS 平台的 Issue 模型，无法以纯文件形式存在于仓库中；（2）外部 agent 需要解析 GitHub API 而非直接读取仓库文件；（3）任务删除在 Issue 模型中缺乏天然的对应表达，已关闭的 Issue 仍占用编号空间且容易与完成态混淆。

### 2. 整体架构

本方案在 Mission Control 的任务生命周期中引入一种可选的"仓库任务镜像"同步通道。其核心思路为：为每个启用同步的项目配置一个目标 Git 仓库及仓库内镜像目录路径；在任务发生创建、更新、删除等变更事件时，系统在本地工作副本中将任务数据序列化为机器可读的 JSON 任务镜像文件，通过 Git 提交和推送操作将镜像文件同步到远端仓库。外部 agent 工具只需 clone 或 pull 该仓库，即可通过读取目录中的 JSON 文件直接理解任务队列，无需访问 Mission Control 数据库或任何 API。

### 3. 关键模块

系统架构由以下关键模块组成：

- 镜像引擎（Mirror Engine）：负责将任务变更事件转换为文件系统操作（写入、更新、删除），执行 Git add/commit/push 流程，处理同步失败的重试和恢复。
- 路径映射器（Path Mapper）：基于任务 ID 和项目配置，生成稳定、确定性、可逆的任务镜像文件路径，确保同一任务在不同时刻和不同副本上映射到相同路径。
- 事件监听器（Sync Listener）：订阅事件总线中的任务生命周期事件（task.created、task.updated、task.deleted），在数据库事务提交后触发异步镜像同步。
- 同步状态追踪（Sync Tracker）：在数据库中记录每次同步操作的状态、时间戳和镜像文件哈希，支持断点恢复和冲突检测。
- 工作副本管理器（Worktree Manager）：管理 Git 仓库的本地工作副本，包括 clone、pull、分支切换、锁管理，避免并发写入冲突。

### 4. 任务镜像文件格式

每个 Mission Control 任务在仓库中被序列化为一个独立的 JSON 文件，文件名即为任务镜像文件名。JSON 结构固定，包含以下字段：

- id：任务在 Mission Control 数据库中的主键 ID，作为跨系统的稳定标识。
- title：任务标题。
- description：任务描述（Markdown 原文）。
- status：任务状态，取值为 inbox、assigned、in_progress、review、quality_review、done。
- priority：优先级，取值为 low、medium、high、urgent。
- assigned_to：指派对象标识（agent session key 或用户名）。
- created_at / updated_at / completed_at：Unix 时间戳。
- due_date：截止日期（Unix 时间戳，可选）。
- tags：字符串数组。
- project：所属项目名称和 ticket_ref。
- mirror_version：镜像格式版本号，用于未来格式演进时的兼容性判断。
- mirror_updated_at：本镜像文件写入时间戳，用于外部工具判断文件新鲜度。

JSON 文件采用格式化缩进输出，确保人类和机器均可直接阅读。镜像文件不包含数据库内部字段（如 workspace_id、project_id 的原始数值），而是展开为对人类和 agent 有意义的文本表示。状态值和优先级使用英文常量串，与 Mission Control 内部状态机一致，避免含义偏差。

### 5. 任务标识到文件路径的稳定映射

路径映射器将任务 ID 稳定映射为仓库内的相对文件路径，映射规则需满足确定性、可逆性和可扩展性。

基础路径结构为：&lt;镜像根目录&gt;/&lt;项目标识&gt;/&lt;分片目录&gt;/&lt;任务ID&gt;.json。其中镜像根目录由项目配置指定（默认为 .mission-control/tasks），项目标识使用项目 slug（如 general、backend），分片目录用于避免单目录下文件数量过大。

分片策略采用基于任务 ID 的取模分片：分片目录名 = String(task_id % 1000).padStart(3, '0')。例如任务 ID 为 1042 的任务，其文件路径为 .mission-control/tasks/general/042/1042.json。该策略保证同一任务始终映射到同一路径，且 1000 以内的任务均匀分布在 1–3 层目录中。

路径映射器同时支持反向解析：给定一个任务镜像文件路径，可直接从文件名提取任务 ID（去除 .json 后缀并解析为整数），无需查询索引表即可定位对应的 Mission Control 任务记录。这使外部 agent 仅通过文件路径即可建立文件与任务的一一对应关系。

当任务从项目 A 移动到项目 B 时，路径映射器生成新路径并执行"写入新文件 + 删除旧文件"的原子操作序列，通过单次 Git 提交同时体现增删，避免外部工具在 pull 后看到重复任务。

### 6. Git 操作流程

仓库任务镜像同步的核心 Git 操作流程包含初始化、增量同步和周期性维护三个阶段。

（1）初始化：当项目首次启用仓库任务镜像同步时，系统验证目标仓库 URL 和认证凭据（支持 HTTPS + token 或 SSH 密钥），在本地磁盘的专用工作目录下执行 git clone --depth=1 获取仓库。如果镜像根目录不存在则创建。克隆完成后，执行一次全量同步：遍历该项目所有活跃任务，生成对应的 JSON 镜像文件，执行 git add + git commit + git push。初始化成功后，在项目记录中标记 mirror_initialized 状态。

（2）增量同步——创建任务：事件监听器收到 task.created 事件后，镜像引擎根据任务 ID 通过路径映射器计算目标文件路径，将任务数据序列化为 JSON 写入该路径，然后执行 git add &lt;文件路径&gt;、git commit -m "task(1042): create '修复登录超时' [inbox]"、git push origin &lt;分支名&gt;。提交消息包含任务 ID、操作类型和任务标题，便于 Git 日志审计。

（3）增量同步——更新任务：事件监听器收到 task.updated 事件后，镜像引擎对比变更类型决定是否需要同步。仅当状态、标题、描述、优先级或指派对象发生变更时才触发文件同步。引擎读取当前任务数据，重新生成 JSON 并覆盖写入对应文件路径，执行 git add + git commit -m "task(1042): update status inbox→in_progress" + git push。

（4）增量同步——删除任务：事件监听器收到 task.deleted 事件后，镜像引擎通过路径映射器定位目标文件，执行 git rm &lt;文件路径&gt;，然后 git commit -m "task(1042): delete" + git push。即使文件已被外部手动删除，git rm 失败时回退为普通文件删除并记录警告，不阻塞提交。

（5）周期性维护：系统以可配置的间隔（默认 5 分钟）执行 git pull --rebase，将外部可能的任务文件变更拉取到本地副本。但本方案以 Mission Control 数据库为唯一权威数据源，外部对镜像文件的直接修改不会被反向导入 Mission Control（区别于现有 GitHub Issue 双向同步），仅用于避免 push 时的非快进冲突。

所有 Git 操作使用操作锁（文件级 fcntl/flock 或内存互斥锁）串行化同一仓库的并发写入，避免多个任务事件同时触发时产生 Git 索引冲突。

### 7. 同步触发机制

同步触发采用异步事件驱动模型，与现有 GitHub Issue 同步采用相同的 fire-and-forget 模式，确保任务在 Mission Control 中的操作不会被同步延迟或失败所阻塞。

具体机制如下：（1）任务 API（POST/PUT/DELETE /api/tasks）在数据库事务提交后，通过事件总线（ServerEventBus）广播 task.created、task.updated、task.deleted 事件。（2）镜像同步监听器订阅上述事件类型，在收到事件后立即将同步任务推入内存中的同步队列。（3）同步队列的工作线程（单线程按仓库串行）从队列中取出任务，依次执行文件写入/删除和 Git 操作。（4）每次同步操作后更新数据库中的同步追踪记录（mirror_syncs 表），记录文件路径、操作类型、Git commit SHA 和时间戳，供管理面查询同步状态。

同步队列采用去重优化：当同一任务 ID 的多个更新事件在队列中堆积时，仅保留最新事件，中间状态不产生多余的 Git 提交。去重窗口为队列处理周期（通常小于 1 秒），由队列消费者在处理前对队列按 task_id 进行合并。

### 8. 冲突与失败处理

仓库任务镜像同步涉及文件系统写入和网络 Git 操作，可能面临多种失败场景。方案采用分层防护策略，确保同步失败不影响 Mission Control 的核心任务操作。

（1）非阻塞隔离：所有镜像同步操作在独立于请求处理线程的异步路径中执行。任务创建/更新/删除的 API 响应不等待同步完成，同步失败仅记录错误日志并递增失败计数，不向用户返回错误。

（2）Push 冲突处理：当 git push 因远程有新提交而失败（non-fast-forward）时，镜像引擎执行 git pull --rebase --autostash 拉取远程变更并重放本地提交。若 rebase 过程产生文件冲突（同一任务文件在本地和远程均有修改），由于本方案以 Mission Control 数据库为权威数据源，策略为"本地覆盖"：在 rebase 完成后强制以本地最新任务数据重新生成冲突文件的内容，然后执行 git add + git commit --amend + git push。为避免无限重试，单次同步最多重试 3 次 push，3 次仍失败则放弃本次同步并记录严重错误日志，等待下一轮事件触发或周期性重试。

（3）工作副本损坏恢复：当本地 Git 工作副本因磁盘故障、进程崩溃或手动篡改而损坏时，镜像引擎在检测到 Git 操作异常（如 .git 目录缺失、索引锁定、对象损坏）后，自动删除本地工作副本并重新执行 git clone，然后对当前所有活跃任务执行一次全量重建同步。此恢复过程对用户透明，仅在日志中记录恢复事件。

（4）部分失败续传：当一次同步批次中部分任务文件操作成功、部分失败时，成功的文件变更正常提交和推送，失败的任务在同步追踪表中保留 pending 状态。下一轮周期性同步（或下一次该任务的事件触发）会检测 pending 记录并重试。超过可配置的最大重试次数（默认 10 次）后，记录为 dead 状态并在管理面板中提示管理员介入。

（5）并发写入锁：同一仓库的 Git 操作通过仓库级互斥锁串行执行。锁的获取超时时间为 30 秒，超时后同步任务重新入队等待下一轮处理，避免因长时间 Git 操作（如大仓库 push）导致后续同步堆积超时。

### 9. 删除同步机制

任务删除的镜像同步与普通状态变更有本质区别：创建和更新产生文件的存在性变更，而删除要求文件从仓库中消失。本方案通过以下机制确保删除同步的正确性和可追溯性。

（1）删除触发：当 Mission Control 中的任务被删除（DELETE /api/tasks/[id]），事件总线广播 task.deleted 事件。镜像同步监听器收到该事件后，不依赖任务数据（此时数据库记录已不存在），而是从事件携带的 payload（含任务 ID 和标题）和同步追踪表中缓存的路径信息（mirror_syncs 表记录的 file_path）定位要删除的文件。

（2）文件定位：由于任务记录已从数据库移除，无法通过路径映射器重新计算路径。方案在每次写入同步成功时将文件路径写入 mirror_syncs 表的 file_path 字段；删除同步时优先从该表读取上一次写入路径。如表记录缺失（例如初始化前创建的任务），则回退路径映射器基于任务 ID 重新计算路径并尝试删除。

（3）删除提交：执行 git rm &lt;文件路径&gt;，生成提交消息 "task(ID): delete '标题'"，推送到远程。若文件在远程已被外部删除（git rm 报告文件不在索引中），则跳过该文件并在提交消息中标注 "(already removed)"，确保提交操作不因文件缺失而失败。同步追踪表记录 delete 操作和 commit SHA。

（4）墓碑记录：为防止任务删除后外部 agent 因缓存旧文件而产生混淆，可选地在镜像根目录维护一个墓碑文件（tombstones.json），记录已删除任务 ID 列表及删除时间。外部 agent 读取任务目录时，可交叉比对墓碑文件以识别已过期的缓存任务。墓碑条目在超过配置的保留期（默认 30 天）后由周期性维护任务清理。

### 10. 数据库扩展

为支持仓库任务镜像同步，在现有数据库 schema 上进行以下扩展，复用现有 projects 和 tasks 表结构：

（1）projects 表新增字段：mirror_repo_url（TEXT，Git 仓库 URL）、mirror_enabled（INTEGER，0/1）、mirror_root_path（TEXT，仓库内镜像根目录，默认 .mission-control/tasks）、mirror_branch（TEXT，推送目标分支，默认 main）、mirror_initialized（INTEGER，初始化完成标记）。

（2）新增 mirror_syncs 表，结构为：id（INTEGER PRIMARY KEY）、task_id（INTEGER）、project_id（INTEGER）、workspace_id（INTEGER）、operation（TEXT，create/update/delete）、file_path（TEXT，镜像文件相对路径）、file_hash（TEXT，写入文件的 SHA256 哈希）、commit_sha（TEXT，Git 提交 SHA）、status（TEXT，success/pending/failed/dead）、retry_count（INTEGER）、error_message（TEXT）、created_at（INTEGER）。

（3）mirror_syncs 表上的索引：idx_mirror_syncs_task_id、idx_mirror_syncs_status、idx_mirror_syncs_project_id。

### 11. 与现有系统的集成

仓库任务镜像同步作为现有 Mission Control 同步体系的一个新通道，与已有 GitHub Issue 同步机制平行运行、互不干扰。

与事件总线的集成：镜像同步监听器复用现有 ServerEventBus，订阅与 GitHub 同步相同的 task.created、task.updated、task.deleted 事件。两个同步通道各自独立消费事件，互不阻塞。

与任务 API 的集成：在 POST/PUT/DELETE /api/tasks 的现有流程中，无需添加任何同步等待代码。镜像同步完全由事件总线驱动，任务 API 在数据库事务提交后广播事件即可返回响应。这保持了现有 API 的响应时间和可靠性。

同步配置入口：在现有项目设置面板中增加"仓库任务镜像"配置区域，允许项目管理员配置目标仓库 URL、凭据（复用 GITHUB_TOKEN 或独立配置）、镜像根目录和分支名。同步启用/禁用开关独立于 GitHub Issue 同步。

与现有 GitHub 同步的区别：现有 GitHub Issue 同步是双向的（pull 方向将 Issue 变更反向写入 Mission Control），而仓库任务镜像同步是单向的（Mission Control → 仓库文件），以 Mission Control 数据库为唯一权威数据源。这避免了镜像文件的手动修改引发含义偏差。

### 12. 技术效果

本方案相比现有的 GitHub Issue 同步和纯数据库存储方式，带来以下技术效果：

- 可携带性：任务数据以独立 JSON 文件形式存在于代码仓库中，可跟随代码仓库 clone、fork、mirror、归档，不受 Mission Control 实例生命周期限制。任何能读取 Git 仓库的工具都可以获取完整的任务队列状态。
- 跨工具协作：外部 agent 工具无需理解 Mission Control API 或数据库 schema，仅需解析 JSON 文件和目录结构即可理解任务队列。文件路径与任务 ID 的确定性映射使 agent 可以在仓库中直接定位和引用特定任务。
- Git 原生审计：每次任务变更产生独立的 Git commit，commit message 包含任务 ID、操作类型和标题，可通过 git log 追溯任务生命周期中的每一次状态变更，审计粒度与代码变更一致。
- 同步非侵入：镜像同步完全异步执行，不增加任务 CRUD API 的响应延迟。同步失败不影响 Mission Control 中的任务操作，任务看板和数据库始终是可靠的主工作入口。
- 删除语义明确：任务删除对应文件的 git rm 操作，墓碑文件提供删除记录，区别于 GitHub Issue 模型中删除不存在、close 与完成态容易混淆的问题。
- 状态一致性：单向同步架构以 Mission Control 数据库为唯一权威数据源，镜像文件始终反映数据库的最新状态，不会因外部修改镜像文件而产生含义偏差或冲突协调开销。
- 分片可扩展：基于取模的路径分片策略使单目录文件数可控（每千个任务一个子目录），支持数万级别任务的仓库存储而不会触发文件系统目录项性能瓶颈。

### 13. 风险与待确认问题

以下为方案实施中需要后续确认的风险点和技术边界：

- 大仓库性能：当仓库历史提交数量极大（如 Chromium 级别）时，git clone --depth=1 可缓解初始克隆时间，但 git pull --rebase 在大仓库上的耗时仍需评估。建议在实际部署中监控 Git 操作耗时并设置合理的同步间隔。
- 认证凭据管理：方案依赖 Git 认证凭据（token 或 SSH key）的可用性。当前 GitHub 同步使用 GITHUB_TOKEN 环境变量，仓库镜像同步可复用该凭据或独立配置。需考虑 token 过期或权限变更时的检测和告警机制。
- 镜像文件的外部修改：本方案采用单向同步，外部对镜像文件的直接修改不会被反向导入。但外部修改可能在 git pull --rebase 时引发冲突。当前策略为本地覆盖，需确认团队是否接受此行为，或是否需要可配置的冲突策略。
- 并发项目数限制：每个启用同步的项目独立维护一个 Git 工作副本。当同时启用数十个项目的镜像同步时，磁盘空间（每副本约占仓库大小）和并发 Git 操作需要资源规划。单机部署建议设置最大并发同步项目数上限。
- 大文件附件：当前镜像文件仅包含任务元数据，不包含附件（图片、文档等）。如需同步附件，需额外考虑大文件存储方案（Git LFS 或对象存储 URL 引用），不在本方案当前范围内。
