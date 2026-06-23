## 技术方案

### 总体架构与权威数据源

本方案解决的技术问题是：Mission Control 的任务状态、分配关系和质量评审结果只存在于应用内部主存储时，开发者和 agent 在 Git 协作空间内难以及时读取权威任务队列；若简单把 GitHub open/closed 或仓库文件修改回写为任务状态，又容易产生重复创建、状态语义折叠、双向覆盖和同步回环。本方案因此采用“主存储权威、Git-native 镜像可读且可受控回读、所有同步由绑定关系和版本水位驱动”的设计原则，在应用数据库之外建立仓库侧任务镜像层，但不使外部对象替代任务主数据。

该镜像层按工作区和项目维度启用。应用数据库中的 task_id 是本地唯一键，project_ticket 是面向人和跨系统展示的项目票号，stable_task_id 是写入 issue 正文或任务文件 front matter 的跨系统稳定标识，external_object_id 是仓库侧 issue 编号、任务文件路径或 PR 编号，canonical_hash 是由任务业务字段规范化后得到的镜像内容哈希，local_version、remote_version、last_pushed_local_version 和 last_seen_remote_version 构成同步版本水位。任务记录或任务镜像绑定记录中保存 repository_id、external_object_id、mirror_state、最近同步时间、版本水位和哈希；仓库镜像对象中嵌入 stable_task_id、project_ticket 和同步元数据。系统以 repository_id 与 task_id 或 project_ticket 建立唯一约束，配合外部对象中的 stable_task_id，实现幂等创建、更新、去重和绑定丢失后的恢复。

### 任务变更捕获与镜像同步作业

任务创建、编辑、分配、优先级调整、看板拖拽状态变更、质量评审通过后的完成动作以及任务删除或归档动作，均在同一应用事务中写入任务表和同步 outbox 事件表。outbox 事件包括 event_id、workspace_id、project_id、task_id、local_version、change_type、task_snapshot、snapshot_hash、source、dedupe_key、processing_state、created_at、locked_at、retry_count、next_retry_at 和 last_error；其中 dedupe_key 可由 repository_id、task_id、change_type 和 local_version 组成，并设置唯一约束。事件状态按 pending、processing、succeeded、retry_wait、dead_letter 流转：事务提交后事件为 pending，异步 worker 按 pending 或到期 retry_wait 领取并置为 processing，成功后写入 succeeded，失败则按错误类型计算 next_retry_at 并进入 retry_wait，超过阈值或不可恢复错误进入 dead_letter。

本地任务每次提交时递增 local_version，并基于标题、描述、内部状态、优先级、负责人、普通标签、关联分支/PR 和机器可读元数据计算 snapshot_hash。出站同步的前置条件为：项目同步开关开启、仓库配置和凭据有效、专用标签已初始化或可自动初始化、任务未处于永久删除终止态，且 local_version 高于 last_pushed_local_version。worker 先按现有 external_object_id 读取外部对象；绑定缺失时，按 stable_task_id 或 project_ticket 在远端查重并重建绑定；仍未命中时才创建新的 issue 或任务文件。出站成功后回写 external_object_id、remote_version、remote_updated_at、last_pushed_local_version、pushed_hash 和 mirror_state=synced，从而把主事务、异步事件、外部对象和版本水位连接成可恢复的数据流。

入站回读由 webhook、后台轮询或手动触发进入 pull_pending 状态。webhook 入口先校验签名并按 delivery_id 或 remote_version 去重；轮询入口只读取已配置仓库中 updated_at 高于项目读取水位的对象。回读顺序为：先校验外部对象属于当前 repository_id 和项目配置，再按 repository_id 与 external_object_id 查找绑定；未命中时解析 issue 正文或任务文件 front matter 中的 stable_task_id、project_ticket 并尝试重建绑定；仍未命中时才创建 created_by 或 source 为仓库同步的新任务。入站写入成功后更新 last_seen_remote_version、remote_updated_at 和 mirror_state=synced；若本地 local_version 也高于 last_pushed_local_version，则不直接覆盖，而转入冲突判定。

同一任务存在多个待同步事件时，worker 按 task_id 聚合并只执行最高 local_version 的最终快照，低版本事件标记为 superseded；同一外部对象的多个 webhook 按 remote_version 或 updated_at 去重，只处理高于 last_seen_remote_version 的事件。出站和入站同时发生时，系统依次执行防回环判断、版本水位比较和字段级冲突规则：若哈希等同则跳过，若单侧版本前进则应用单侧变更，若两侧版本均前进则进入冲突矩阵，不允许用外部标题或正文直接覆盖已经产生新 local_version 的本地内容。

### Git-native 任务镜像表达

在一种实现中，GitHub Issue 作为任务镜像载体。issue 标题承载任务标题，issue 正文除人可读说明外，还包含受标记包围的机器可读区；labels 承载状态、优先级和普通标签；assignees 根据用户映射表写入。机器可读区至少包括 stable_task_id、project_ticket、state、priority、assignee、labels、local_version、remote_version、source、canonical_hash、branch 和 pull_request。出站更新时只替换本系统维护的机器可读区和 mc/priority 专用标签，保留仓库用户添加的普通标签和评论；入站回读时先解析机器可读区，再读取专用标签和 issue 生命周期状态，以恢复任务对象。

| 字段 | 位置 | 用途 |
| --- | --- | --- |
| stable_task_id | issue 正文机器可读区或任务文件 front matter | 跨系统稳定定位任务，绑定丢失时用于重建 |
| project_ticket | issue 正文、任务文件路径或 front matter | 面向人和仓库搜索的项目票号 |
| state / priority | 专用标签和 front matter | 恢复 Mission Control 内部状态和优先级 |
| local_version / remote_version | 本地绑定记录和镜像元数据 | 判断出站、入站和冲突的版本水位 |
| canonical_hash | 本地绑定记录和镜像元数据 | 识别回环、重复写入和非业务差异 |
| source | 镜像元数据 | 区分 Mission Control 写入、仓库用户修改和 agent 修改 |

在另一种实现中，仓库内 Markdown 或 YAML 文件作为任务镜像载体，路径可采用 .mission-control/tasks/{project_ticket}-{slug}.md 或按项目分目录的等价命名，文件 front matter 保存 stable_task_id、project_ticket、state、priority、assignee、labels、local_version、remote_version、source 和 canonical_hash，正文保存任务说明和验收信息。写入时由同步 worker 在配置的同步分支上提交，提交作者为系统同步账号，commit message 包含 project_ticket、task_id 和 local_version；若要求保护默认分支，则通过 PR 合入。遇到默认分支已前进或文件版本冲突时，worker 先拉取最新树并按 stable_task_id 重新定位文件，rebase 后重算 canonical_hash 并重试；若同一正文区两侧均变更且无法自动合并，则将 mirror_state 置为 conflict，而不强行覆盖仓库或本地内容。

当 issue 与仓库任务文件同时启用时，默认以 issue 作为状态主镜像，用于表达 state、priority、assignee、评论和协作讨论；任务文件作为离线扫描辅助镜像，用于批量读取任务快照和关联代码上下文。若二者状态不一致，系统先以 issue 的专用状态标签和 remote_version 解释任务工作流，再将解析后的规范状态写回任务文件；若任务文件的 local_version 更新但 issue 未更新，则文件变化只作为说明或元数据候选输入进入入站冲突判定。该主辅关系避免多个镜像载体同时争夺任务状态权威。

### 状态语义映射与回读冲突控制

本方案将仓库对象生命周期状态与 Mission Control 业务状态分离。内部状态枚举统一为 $inbox$、$assigned$、$in\_progress$、$review$、$quality\_review$、$done$，外部专用状态标签一一映射为 $mc:inbox$、$mc:assigned$、$mc:in-progress$、$mc:review$、$mc:quality-review$、$mc:done$；优先级标签统一为 $priority:low$、$priority:medium$、$priority:high$、$priority:critical$。出站同步时，系统先删除旧的 $mc:$ 状态标签，再写入当前唯一状态标签；除 $done$ 可使 issue 进入 closed 外，其余内部状态均保持 issue 为 open，因此 open/closed 只表示外部对象生命周期，不折叠 review 或 quality_review 等中间业务状态。

入站状态解释按确定顺序执行：先读取机器可读区中的 state 并校验其是否合法；再读取 $mc:$ 专用状态标签；最后才参考 issue 的 open/closed。若多个 $mc:$ 状态标签同时存在，选择 remote_version 所属机器可读区中的 state，并在下一次出站同步中唯一化标签；若没有状态标签且 issue 为 open，则保持本地原状态或按新建任务进入 $inbox$；若状态值非法，则不更新本地状态并记录镜像异常；若 issue closed 但仍带有非 $mc:done$ 标签，则优先保持标签所表达的业务状态并记录“生命周期与业务状态不一致”；若已完成质量评审并处于 $done$ 的任务被外部 reopen，除非机器可读区或评论中存在明确 reopen 指令并通过权限校验，否则不自动回退本地状态。

canonical_hash 用于识别回环和重复写入。计算时先把标题、正文业务区、内部状态、优先级、负责人、普通标签、关联分支/PR 和机器可读字段转换为规范 JSON：字段名排序，标签集合排序去重，换行统一，空值归一化，排除 updated_at、remote_updated_at、source、同步时间、标签排列顺序等非业务差异。入站对象的 canonical_hash 等于 pushed_hash 且 remote_version 不高于 last_seen_remote_version 时，视为本系统出站后的回显并跳过；防抖时间窗口仅作为辅助信号，不能单独覆盖版本水位和哈希判断。

| 字段 | 两侧同时变更时的默认处理 | 不得覆盖边界 |
| --- | --- | --- |
| 状态 | 本地主存储优先；外部状态仅在通过专用标签、权限和版本校验后作为候选 | 本地已 $done$ 且有质量评审结果时，外部 reopen 不自动回退 |
| 优先级 | 本地主存储优先，外部优先级标签记录为候选差异 | 外部删除 priority 标签不清空本地优先级 |
| 负责人 | 按用户映射成功时可回读，否则保持本地负责人并记录异常 | 外部无法识别、权限不足或 assignee 被删除时不得覆盖本地分配 |
| 标题 | 单侧变更则更新；双侧变更进入 conflict | local_version 高于 last_pushed_local_version 时不被外部标题直接覆盖 |
| 正文 | 机器可读区由系统维护；业务说明双侧变更进入 conflict 或追加差异 | 不得用外部正文覆盖本地最新描述 |
| 评论 | 追加合并并保留作者和时间 | 评论不改变任务主状态 |
| 普通标签 | 集合合并并去重 | 普通标签不得覆盖 $mc:$ 和 $priority:$ 专用标签 |
| 专用标签 | 由状态和优先级映射层唯一化 | 多个或非法专用标签只触发修复，不直接改写本地关键字段 |
| PR/分支关联 | 按外部最新有效对象补充关联字段 | 已归档或不属于项目的 PR/分支不回写任务 |

负责人回读依赖用户映射表，映射项包括 Mission Control 用户标识、git_username、email 和 provider_account_id。出站时只有存在有效映射且同步凭据具备分配权限，才把本地负责人写入仓库 assignee；入站时外部 assignee 必须能反查到同一工作区内的用户或 agent，才允许作为分配候选。若外部用户无法映射、已离开仓库、权限不足或外部分配被删除，系统保持本地 assigned_to 不变，并在 mirror_state 或错误明细中记录负责人映射异常，避免外部权限变化破坏 agent 调度。

### 故障隔离与恢复

同步绑定记录中设置镜像状态机，用于把外部镜像故障与应用内任务主状态隔离。镜像状态包括 $unbound$、$creating$、$synced$、$dirty$、$pull_pending$、$conflict$、$error$、$delete_pending$ 和 $archived$：任务尚未建立外部对象时为 $unbound$；创建或首次绑定期间为 $creating$；本地版本与已推送版本一致且远端无待处理变化时为 $synced$；本地 $local_version$ 高于 $last_pushed_local_version$ 时为 $dirty$；接收到远端变更但尚未完成字段级判定时为 $pull_pending$；双侧变更无法按预设规则合并时为 $conflict$；可重试或需人工修复的同步失败进入 $error$；删除或归档动作未完成时进入 $delete_pending$；删除镜像已完成或任务被归档后进入 $archived$。界面提示镜像滞后时，不以外部对象显示结果为准，而根据 $mirror_state$、$local_version$、$last_pushed_local_version$、$last_seen_remote_version$、$last_error$ 和 $next_retry_at$ 判断，使操作者能够区分内部任务已更新、外部镜像待推送、远端回读待确认以及需要人工修复的状态。

外部 Git 托管服务不可用、网络超时或返回 5xx 时，同步作业保持原任务提交结果不回滚，将事件置为 $retry_wait$ 并按指数退避重试；返回 429 时优先采用服务端限流重置时间计算 $next_retry_at$；返回 401 或 403 时，项目进入待修复同步状态，停止高频重试，仅保留低频探测或等待凭据更新后重放；返回 404 时先判断绑定对象是否被外部删除，若本地任务仍有效则清除失效绑定并按 $stable_task_id$ 或 $project_ticket$ 查重后重建镜像，若本地已删除则把外部对象不存在视为删除动作成功，若远端删除与本地更新并发则标记为 $conflict$。标签初始化失败时，不执行依赖专用标签的状态写入，相关事件保留在待同步队列，待标签创建或权限修复后按原 $local_version$ 顺序重放。

对于一次镜像更新中部分字段已经写入外部对象但整体同步返回失败的情形，本方案不简单重复完整覆盖，而是先重新读取外部对象，取得新的 $remote_version$、更新时间、专用标签、机器可读区和 $canonical_hash$。若重读结果与待推送快照等价，则补写绑定记录并把事件标记为成功；若只有非业务字段不同，则按规范化哈希忽略；若远端业务字段已经被第三方修改，则进入字段级冲突判定。该补偿步骤使超时、连接中断或外部接口分阶段成功造成的不确定结果被收敛为“已成功”“可安全重试”或“需冲突处理”三类状态。

任务删除或归档通过 tombstone 记录实现幂等收敛，tombstone 至少保存 $task_id$、$stable_task_id$、$project_ticket$、原 $external_object_id$、删除发起方、删除类型、删除版本、保留期限、处理状态和最后一次外部响应。出站删除优先将外部 issue 关闭并写入归档标签，或删除/迁移仓库任务文件；若外部对象已不存在，且绑定信息与 tombstone 匹配，则判定删除镜像已经完成。重复收到同一删除事件时，以 tombstone 的删除版本和外部对象标识去重，不再次创建新镜像；保留期届满后可以清理业务正文，但保留最小审计字段以防止旧 webhook 或旧任务文件使任务复活。若外部对象在 tombstone 保留期内被重新打开或重新出现，入站作业不直接恢复本地任务，而标记外部重开冲突，并要求根据项目规则选择恢复、重新创建或保持归档。

同步终止规则用于避免已经失去项目归属或不再允许同步的对象继续被写入。项目同步开关关闭、仓库配置被删除、凭据被撤销、任务永久删除且 tombstone 已完成、适配器判定外部对象已移动到非本项目仓库或不再包含有效 $stable_task_id$ 时，出站更新停止生成新的外部写入请求；既有绑定记录保留最近状态、错误和审计信息。若后续重新开启同步，系统按项目当前配置重新校验仓库归属和稳定任务标识，只有通过查重和权限验证后才恢复 $dirty$ 或 $pull_pending$ 事件的处理。

### 镜像适配器与承载方式扩展

为使同一任务镜像机制能够适配不同 Git 托管平台或仓库内承载形式，本方案在同步作业与具体平台之间设置镜像适配器。同步作业只处理任务快照、语义映射、版本水位、绑定查重和故障隔离，适配器负责把这些统一语义转换为平台对象操作；因此 GitHub Issues、GitLab Issues、Forgejo/Gitea Issues、仓库内任务文件、分支命名约定或 PR 元数据等载体可以共用同一套本地状态机和冲突规则，而不需要为每一种平台重新定义任务生命周期。

镜像适配器至少提供以下能力：$createMirror$ 根据任务快照创建外部对象并返回 $external_object_id$ 与初始 $remote_version$；$updateMirror$ 按字段补丁或规范化快照更新外部对象；$closeMirror$ 执行完成、归档或删除对应的外部动作；$readChangedMirrors$ 按仓库、项目和水位读取远端变化；$parseState$ 将外部标签、状态、文件内容或 PR 元数据解释为内部状态候选；$writeLabels$ 幂等创建和替换专用标签；$computeRemoteVersion$ 从 issue 更新时间、etag、commit SHA、文件 blob SHA 或 PR head SHA 中生成可比较的远端版本。适配器返回结果除成功数据外，还应包含错误类别、是否可重试、建议下次重试时间和是否需要重新读取外部对象，以便故障隔离层统一处理。

当承载方式为 issue 类对象时，适配器把内部状态映射为专用状态标签并把完成态映射为关闭动作；当承载方式为仓库任务文件时，适配器把任务快照写入固定路径的 front matter 和正文业务区，并以 commit SHA 或 blob SHA 作为 $remote_version$；当承载方式为分支或 PR 元数据时，适配器只把分支、PR、评审状态等作为任务镜像的关联字段，不替代应用数据库中的任务主记录。无论采用哪一种承载方式，外部对象都必须包含或可反查 $stable_task_id$ 或 $project_ticket$，否则只作为候选信息进入人工确认或冲突队列，不得直接覆盖本地任务。

适配器选择由项目配置、仓库能力和权限共同决定。一个项目可以启用单一主镜像，也可以启用主辅组合；主镜像负责状态回读和冲突判定，辅助镜像仅提供离线可读或索引用途。切换适配器时，系统先冻结原适配器出站写入，读取并固化现有 $external_object_id$、$remote_version$ 和 $canonical_hash$，再用稳定任务标识在新载体中查重或创建镜像；迁移完成前，来自旧载体的入站变化只进入候选队列，避免同一任务在不同载体之间相互覆盖。
