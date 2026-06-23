## 技术方案

### 总体构思：本地观察者生命周期与服务端 turn 生命周期解耦

本方案在浏览器端 agent chat 已具备流式响应、请求标识、可恢复流缓存和服务端取消注册能力的基础上，引入相互独立的两个控制面：观察者控制面用于管理浏览器标签页、组件实例、网络连接和本地 reader 对服务端输出流的订阅关系；执行取消控制面用于管理服务端 agent turn、工具调用或子 agent 运行的开始、持续执行和终止。两者通过 $requestId$、$turnId$ 或 $runId$ 建立关联，但不以本地连接存活作为服务端执行是否继续的唯一判断条件。

标识映射采用父子关系而非连接关系：$requestId$ 表示一次客户端提交及其可恢复响应流，是恢复请求、chunk 缓存和客户端确认偏移的主键；$turnId$ 表示服务端会话内一次 agent turn，可与一个 $requestId$ 一一对应或在服务端内部派生；$runId$ 表示该 turn 下的工具调用、工具 continuation 或子 agent 运行，一个 $turnId$ 下可以关联多个 $runId$。取消命令必须声明作用目标和作用范围，未声明级联时，针对 $runId$ 的取消不自动取消父 $turnId$，针对 $turnId$ 的取消也不覆盖已经结束的子运行最终状态。

在该构思下，刷新页面、组件卸载、页面路由切换、浏览器短暂断网、WebSocket 关闭以及本地 reader cancel 等事件，默认被处理为观察者释放或进入可恢复状态，仅关闭本地读取链路、解除 UI 订阅或等待后续恢复握手；服务端仍可继续推进同一 agent turn，并将后续 chunk 写入可恢复流缓存。只有用户点击停止按钮、应用业务逻辑发出主动取消命令、清空会话等能够表达明确终止意图的事件，才进入执行取消控制面，并携带目标标识触发服务端取消处理。

状态模型分为三层独立状态。观察者状态按连接实例记录，可在 `connected`、`detached`、`pending_recovery`、`replaying`、`live`、`closed` 之间迁移；turn 状态按 $turnId$ 记录，可在 `queued`、`running`、`tool_running`、`cancel_requested`、`cancelled`、`completed`、`failed`、`expired` 之间迁移；工具或子运行状态按 $runId$ 记录，可在 `created`、`running`、`cancel_requested`、`cancelled`、`completed`、`failed`、`interrupted` 之间迁移。观察者从 `connected` 变为 `detached` 不改变 turn 的 `running` 或 `tool_running` 状态；只有执行取消控制面产生的取消命令才能使可取消状态进入 `cancel_requested`。

### 事件分类与取消策略配置

事件进入处理流程时统一抽取事件来源、连接实例标识、会话标识、目标 $requestId$、$turnId$ 或 $runId$、本地已接收 chunk 序号、事件时间和显式意图标记。判定顺序为：先判断事件是否携带停止、取消、清空并终止等显式取消意图；若存在，则转入执行取消控制面。若不存在，再判断是否属于刷新、组件卸载、路由切换、网络断开、WebSocket close、本地 reader cancel 或恢复旧 reader 释放等非显式断开；该类事件只将对应观察者标记为 `detached` 或 `closed`，并记录最后确认序号。若事件属于历史同步、已完成输出恢复、缓存清理等会话维护操作，则仅执行维护动作，不改变 `running`、`tool_running` 或最终状态。

取消策略按条件—动作—状态更新执行。策略为 `preserve` 时，非显式断开只释放观察者，turn 继续运行并写入缓存；策略为 `cancel_immediately` 时，在最后一个观察者断开且业务允许中断后台任务时生成内部取消命令；策略为 `cancel_after_grace` 时，在观察者数量变为零的时刻写入 `detachedAt` 和 `expireAt=detachedAt+graceMs`，计时期间 turn 继续运行，若任一合法观察者在 `expireAt` 前恢复成功则清除 `expireAt`，若到期仍无观察者且 turn 仍处于 `running` 或 `tool_running`，则进入 `cancel_requested` 或按配置标记为后台运行过期；策略为 `explicit_only` 时，无论观察者数量如何变化，均不由断开事件触发取消。

策略分支的优先级固定为：显式取消高于非显式恢复，会话清空并终止高于单 turn 恢复，指定 $runId$ 的取消仅作用于对应子运行，除非命令声明父子级联；重复的断开事件只更新观察者最后状态和最后确认序号，不重复启动宽限计时；重复的恢复事件按同一观察者实例的最新连接替换旧连接，旧连接被置为 `closed`。该规则使 reader cancel、网络 close 与用户 stop 在同一时间附近到达时仍能得到确定结果。

### 服务端 turn、可恢复流和取消注册表的协同

服务端在接收新的 agent turn 时，为该 turn 分配或接收稳定的 $requestId$，并可进一步关联内部 $turnId$ 或工具、子 agent 的 $runId$。服务端以该标识为索引维护取消注册表中的 `AbortController`、turn 队列状态、可恢复流的 active stream、已连接观察者集合以及待恢复连接集合。模型推理、工具执行、消息持久化和 chunk 广播均围绕同一标识协同，使取消、恢复和广播可以定位到同一逻辑运行，而不是定位到某一个短生命周期浏览器连接。

取消注册表和运行索引至少保存以下字段：目标标识、父子映射、`AbortController` 或取消回调、`turnState` 或 `runState`、`observerSet`、`pendingRecoverySet`、`lastSequence`、`cacheOffset`、`detachedAt`、`expireAt`、`cancelSource`、`cancelReason`、`finalState` 和 `finalizedAt`。观察者断开时更新 `observerSet`、最后确认序号、`detachedAt` 和可能的 `expireAt`；恢复成功时把观察者从 `pendingRecoverySet` 移回 `observerSet` 并更新其确认偏移；显式取消时写入 `cancelSource`、`cancelReason` 并把状态置为 `cancel_requested`；完成、失败、取消或过期时写入 `finalState` 和 `finalizedAt`，随后在安全时机移除取消控制器，但保留足以支持回放和幂等判断的最终状态记录。

可恢复流缓存按 $requestId$ 分片存储 chunk，每个 chunk 记录递增 `sequence`、缓存偏移 `cacheOffset`、生成时间、所属 $turnId$ 或 $runId$、chunk 类型以及载荷摘要或载荷内容。模型或工具产生 chunk 后，服务端先分配下一个 `sequence` 并写入缓存，再把同一 chunk 放入实时广播队列；各观察者记录自己最后确认的 `sequence` 或 `cacheOffset`。终止事件也作为一种有序 chunk 写入缓存，其 `sequence` 位于最后一个普通响应 chunk 之后，从而保证回放端能够按序得到部分输出和最终状态。

当发生非显式断开时，服务端将对应观察者从 `observerSet` 移除，记录其最后确认 `sequence`，并按策略决定是否启动无观察者宽限计时；正在执行的 agent turn 不因此调用取消注册表的 cancel 操作。若仍存在其他观察者，实时广播继续发送给这些观察者；若已无观察者但策略允许后台运行，模型或工具输出仍按序写入可恢复流缓存。由此，本地连接关闭与服务端 `AbortSignal` 的触发被解耦，避免 UI 生命周期或网络抖动错误终止长时间 agent 任务。

当发生显式取消时，服务端先按目标标识定位 active turn、排队 turn、工具运行或子 agent 运行，再在状态仍为 `queued`、`running` 或 `tool_running` 时触发取消注册表或运行注册表。状态进入 `cancel_requested` 后，服务端停止向普通响应流追加新的业务 chunk，并允许取消路径写入一个有序终止事件；关联的 `AbortSignal` 或取消回调传递给模型调用、工具调用或 continuation 逻辑。取消完成后，运行状态更新为 `cancelled`、`interrupted` 或相应失败状态，取消控制器被移除，最终状态记录和已缓存 chunk 保留至恢复窗口或缓存保留期结束。

### 恢复、多标签页与工具 continuation 的兼容

恢复请求必须携带会话标识、客户端实例标识、目标 $requestId$ 或 $turnId$、本地最后接收的 `sequence` 或 `cacheOffset`，以及是否接受已终止结果的标记。服务端先校验该观察者是否属于允许恢复的会话，再检查目标 turn 或最终状态记录是否仍存在、缓存是否覆盖所请求偏移、恢复窗口是否未过期。校验通过后，观察者进入 `pending_recovery`，服务端返回恢复中状态；客户端确认后进入 `replaying`，服务端从 `lastSequence+1` 或对应 `cacheOffset` 后开始回放已缓存 chunk。

回放与实时广播之间采用隔离切换规则。观察者处于 `replaying` 时，其连接被加入待恢复集合，实时广播队列排除该连接；服务端按 `sequence` 递增发送缓存 chunk，并在回放至当前 `lastSequence` 后发送回放完成标记。观察者随后切换为 `live`，并只接收大于其已确认 `sequence` 的实时 chunk。若客户端因重试收到重复 chunk，则以 $requestId$ 和 `sequence` 去重并丢弃不大于本地确认序号的片段；若缓存已清理或偏移不连续，则返回 `expired` 或 `not_found`，不得重新创建同一 turn 或覆盖已有最终状态。

对于多标签页场景，每个标签页或组件实例均以独立观察者记录在同一 $turnId$ 的 `observerSet` 中。任一观察者的本地清理只影响该观察者自身；多个观察者同时发送显式停止时，服务端按目标标识和作用范围合并为一次取消判定：相同目标的重复取消返回同一最终或处理中状态，不重复触发 `AbortSignal`；不同目标同时取消时，`session` 级清空并终止优先于 $turnId$ 级取消，$turnId$ 级取消优先于未声明级联的单个 $runId$ 取消。终止事件写入后广播给全部在线观察者，离线观察者在后续恢复时通过缓存获得同一最终状态。

对于工具 continuation 和子 agent 场景，服务端维护父子映射表，字段包括父 $turnId$、子 $runId$、运行类型、所属可恢复流、取消回调或远端控制地址、级联策略和最终状态。父侧观察者流关闭只更新父 turn 的观察者集合，不取消子 $runId$；父侧显式取消在级联策略允许时向仍处于运行态的子 $runId$ 发送取消控制消息；指定 $runId$ 取消仅作用于对应工具或子 agent；会话级取消按映射表遍历尚未终结的 turn 和 run。跨 Durable Object 或外部服务边界时，取消意图通过显式控制消息、父侧取消回调或状态轮询桥接，不依赖直接序列化浏览器侧 `AbortSignal`。

### 显式取消传递与终止状态同步

显式取消命令的处理链路为：解析目标标识和作用范围；校验命令来源、会话归属和业务权限；读取目标当前状态；对 `completed`、`failed`、`cancelled`、`expired` 等终态返回幂等结果且不改写最终状态；对 `queued`、`running`、`tool_running` 等可取消状态写入 `cancelSource` 和 `cancelReason`，把状态置为 `cancel_requested`；触发目标对应的 `AbortSignal`、取消回调或远端控制消息；停止普通业务 chunk 的继续追加；写入有序终止事件；向在线观察者广播终止状态；最后在保留最终状态和缓存索引的前提下清理取消控制器。

迟到取消不得覆盖既有结果。若取消命令到达时目标已经完成或失败，服务端返回当前 `finalState`，不把状态改写为 `cancelled`；若目标已取消，返回相同取消记录；若缓存已过期或目标不存在，返回 `expired` 或 `not_found`，不创建新的 turn。取消后是否允许继续回放已生成内容由保留策略决定：保留时，已缓存普通 chunk 和最后的 `cancelled` 终止事件均可回放；删除历史时，消息和缓存被清理，但应同时记录清理状态，避免后续恢复请求误认为仍可继续运行。

当工具或外部 API 不响应取消时，服务端将对应 $runId$ 保持在 `cancel_requested`，记录取消发出时间和取消超时阈值，并停止把该运行产生的后续内容追加到父 turn 的普通响应流。若外部调用随后返回，结果按策略丢弃、隔离为诊断记录或标记为已中断结果，不再覆盖父 turn 的 `cancelled` 终止状态；若超过取消超时阈值仍未返回，则将子运行标记为 `interrupted` 或取消超时，并向观察者同步该状态。

通过上述处理，技术效果与具体手段形成对应关系：事件分类和策略优先级避免本地 reader cancel、页面刷新或网络 close 误触发服务端 `AbortSignal`；稳定标识、父子映射和取消注册表使停止命令能够定位到具体 turn、工具 continuation 或子 agent 运行；chunk 序号、缓存偏移、回放隔离和实时广播切换避免重复输出和乱序显示；最终状态记录、墓碑记录和幂等取消规则保证刷新、多标签页和重连后的 UI 一致。由此，长时间 agent 对话既能保留断线恢复和后台继续执行能力，又能在用户明确停止时可靠终止目标运行。

### 并发与异常处理

恢复与取消并发到达时，以服务端已写入的状态版本和 chunk 序号为准。若取消先进入 `cancel_requested`，随后到达的恢复请求只能回放取消前已缓存 chunk 及终止事件，不能使 turn 回到 `running`；若恢复先进入 `replaying`，随后取消到达，则回放连接继续排除实时广播，直到收到缓存中的终止事件或回放完成后接收终止广播。重复恢复请求按客户端实例标识去重，较新的连接替换旧连接；重复取消命令按目标标识和状态版本幂等返回。

清空会话与运行完成并发时，清空命令根据其作用范围拆分为 UI 历史删除、缓存删除和运行取消三个动作。若配置为仅删除历史，则不改变仍在运行的 turn；若配置为删除历史并终止运行，则先对未终结目标执行取消链路，再清理消息和缓存；若运行已在清空前写入 `completed` 或 `failed` 终态，则清空只删除可见历史和缓存，不把终态改写为取消。服务端重启后，若可恢复流元数据中存在未终结 active stream，则恢复其 active 标识和最后序号；若取消注册表中的内存控制器无法恢复，则将该运行标记为 `interrupted` 或等待业务层重新附着取消控制器，避免形成既不可取消又显示运行中的悬挂状态。

缓存清理以最终状态和保留期限为边界。对已完成、已失败或已取消的 turn，超过保留期后可以删除 chunk 载荷，但应保留最小最终状态索引或墓碑记录，使迟到恢复请求得到 `expired` 而不是触发新的执行。对仍处于 `running` 或 `tool_running` 的 turn，缓存写入失败时应停止向客户端宣称可恢复，并向在线观察者发送错误或降级状态；恢复请求晚于缓存清理时返回不可恢复结果，客户端不得用本地 reader cancel 再触发服务端 abort。
