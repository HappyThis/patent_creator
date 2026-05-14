# 隐藏参考方案

## 技术问题

Think agent 已有 `saveMessages()` 等能力，可以把消息注入会话并等待模型 turn 完成。但 webhook、RPC、父 Worker 等外部调用方常常有严格超时限制，无法一直等待模型执行结束。若调用方超时后重试，系统无法区分“请求未被接受”“请求已排队”“请求正在运行”或“请求已完成”，容易重复插入用户消息或重复执行外部任务。

同时，Think 已有会话消息、流式执行、恢复、取消和 turn queue 语义，不能为了外部提交场景另建一套割裂的执行系统。需要一种能够先持久接受提交、快速返回、后台执行、支持幂等重试和后续查询/取消的 agent 对话任务提交机制。

## 核心技术构思

在 Think agent 中建立“持久提交记录 + 快速接收边界 + 幂等身份 + 后台串行执行 + 会话应用边界 + 状态检查与取消”的程序化对话提交机制。

系统在外部调用方提交消息时，先将提交意图持久化为一条 submission 记录，并立即返回提交标识和接收状态。提交记录保存待执行消息、外部幂等标识、元数据、状态和时间戳。后台执行器按提交顺序领取待执行记录，并在真正开始执行时才把提交消息追加到 Think Session 中，随后复用现有 Think 推理、流式输出和恢复路径。系统通过幂等标识让外部调用方安全重试；通过状态查询、列表、取消和删除接口管理提交生命周期；通过会话应用边界避免恢复时重复追加消息。

## 必要技术特征

1. 持久接收边界：在模型推理前先持久化提交记录，并向调用方返回已接收状态。
2. 幂等身份：支持外部调用方提供稳定幂等键或提交标识，重试时返回已有提交而不是创建重复任务。
3. 提交状态模型：记录 pending、running、completed、aborted、skipped、error 等生命周期状态。
4. 后台领取执行：通过后台 drain 或唤醒机制领取最早待执行提交，并将其置为运行中。
5. 会话应用边界：只有提交被领取执行时才把消息追加到 Session，并记录消息已应用标记，用于恢复判断。
6. 恢复判定：重启或 hibernation 后，根据提交状态、消息是否已应用和聊天恢复证据决定重新排队、继续运行或标记错误。
7. 取消与重置：支持取消 pending/running 提交；当会话 turn 被重置时，同步处理尚未执行的提交。
8. 条件终态更新：终态更新必须防止迟到的完成、错误或恢复逻辑覆盖已经取消或跳过的提交。
9. 可观察性：提交创建、状态变化、完成、取消和错误应可被检查、列出或发送到观测事件中。

## 关键流程

### 提交流程

1. 外部调用方提交一组可序列化消息，并可携带幂等键。
2. 系统校验消息非空且可持久化。
3. 系统根据提交标识或幂等键查找已有记录。
4. 若存在已有记录，则返回该记录并标记为非新接收。
5. 若不存在，则写入 pending 提交记录，返回提交标识，并触发后台执行唤醒。

### 后台执行流程

1. 后台 drain 查询最早的 pending 提交。
2. 系统条件性地把该提交更新为 running，防止并发领取。
3. 系统将提交消息追加到 Think Session，并记录消息已应用时间。
4. 系统复用现有 Think turn 执行、流式输出和结果处理路径。
5. 执行结束后，系统把提交更新为 completed、aborted、skipped 或 error。

### 恢复流程

1. agent 启动或恢复时扫描未终结提交。
2. 对未应用消息的 running 提交，若没有会话应用证据，则回到 pending。
3. 对消息可能已部分应用但缺少安全边界的提交，标记为 error，避免重复追加。
4. 对已经应用消息且存在有效聊天恢复证据的提交，保持 running，等待恢复路径给出终态。
5. 对过期或无法证明可恢复的提交，标记为 error。

## 技术效果

- 外部调用方可以快速获得“已接收”确认，避免长时间阻塞在模型执行上。
- 超时重试不会重复插入消息或重复执行任务。
- 提交状态可被查询、取消和清理，提高外部系统集成可靠性。
- Think 原有 Session、streaming、recovery 和 cancellation 语义被复用，避免形成割裂执行体系。
- hibernation、重启和恢复场景下，系统可以根据消息应用边界做安全恢复，减少重复或丢失 turn 的风险。
- 该机制为 webhook、父 Worker、自动化系统和多 agent 编排提供可靠的单 turn 提交基础。

## 目标能力边界

必须解决的是“外部调用方可靠提交一个 Think turn 并快速返回”，不是简单提供一个异步 HTTP endpoint。提交一旦被接受，就必须有持久记录、状态查询、幂等重试和恢复策略。模型推理可以后台执行，但消息何时真正写入 Session 必须有边界，不能在重试或恢复时重复插入用户消息。

方案不应重写 Think 的普通聊天、streaming、chat recovery 或 turn queue。高分方案会把 durable submission 设计为 Think 现有 turn 执行入口外的一层接收 ledger，并在执行阶段回到现有推理路径。

## 核心数据结构与状态模型

提交记录至少包含：

- `submissionId`：系统生成或外部传入的稳定提交标识。
- `idempotencyKey`：外部系统重试用键，可与调用方或会话范围绑定。
- `messages`：待应用到 Session 的可序列化消息。
- `metadata`：调用方、webhook、trace id、业务标签等。
- `status`：`pending/running/completed/aborted/skipped/error` 或等价状态。
- `createdAt/updatedAt/startedAt/completedAt`。
- `messagesAppliedAt`：提交消息已经写入 Session 的时间，用于恢复去重。
- `requestId/turnId/recoveryId`：与实际 Think turn、stream 或 recovery 证据关联。
- `error`、`resultSummary`、`cancelledAt`、`deletedAt`。

关键状态转移：

- `pending -> running`：后台 drain 条件领取，必须防止多个 drain 同时领取。
- `running -> completed/aborted/skipped/error`：只能由实际执行结果或恢复判定写入。
- `pending/running -> aborted`：取消请求可触发，但需要条件更新，避免已完成提交被取消覆盖。
- `running -> pending`：仅当恢复发现消息尚未应用且没有有效执行证据时允许回滚。
- `running -> error`：消息可能已部分应用但无法安全恢复时，宁可标错也不能重复追加。

## 恢复与竞态处理

- 外部请求重试时，系统先按 submission id 或 idempotency key 查找 ledger，返回已有记录，不重新排队。
- drain 领取 pending 时使用条件更新或事务，确保只有一个执行者把状态改为 running。
- 写入 Session 前后必须记录消息应用边界。若写入前崩溃，可重新 pending；若写入后崩溃，需要通过 recovery 证据继续或标错。
- 迟到的完成事件不得覆盖用户已经取消、清理或 reset 后标记的终态。
- reset/clear conversation 时，应将尚未执行或正在执行但不可恢复的 submission 标记 skipped/aborted/error，并清理关联 active turn。
- 删除 submission 不应删除已经写入 Session 的消息，除非另有显式清理策略。

## 项目集成点

方案应接入 Think 的 Session 消息保存、turn 执行、streaming/recovery、AbortRegistry 或取消机制、observability events、docs/webhooks 或 programmatic API。它不应绕开 Think 直接写自定义消息表并单独推理。

## 必须命中的评分锚点

- 有持久 submission ledger，而不是内存队列。
- 有幂等键和提交 id 重试语义。
- 快速返回 accepted，不等待模型完成。
- 消息应用到 Session 有明确边界。
- 有 pending/running/terminal 状态机和条件终态更新。
- 有恢复判定，能处理写入前崩溃、写入后崩溃和 hibernation。
- 查询、取消、删除/list inspect API 或等价管理能力完整。

## 常见错误方案

- 用普通异步队列接收 webhook，但没有持久 ledger 和幂等键。
- 收到请求时立刻写入 Session，重试导致重复用户消息。
- 只返回 task id，不支持状态查询、取消和清理。
- 恢复时盲目重新执行 running submission，导致重复 turn。
- 另建一套推理执行系统，绕开 Think 原有 streaming/recovery。

## 对应真实实现

真实 PR #1511 采用了如下实现方向：

- 在 Think 中新增 `submitMessages()` 及 inspect/list/cancel/delete 等 companion API。
- 使用 `cf_think_submissions` 作为持久 submission ledger。
- 支持 submissionId、idempotencyKey、metadata、状态和时间戳。
- 通过 scheduled drain 领取 pending submission 并执行。
- 使用 `messages_applied_at` 作为 Session 消息应用边界，支撑恢复判定。
- 增加 pending/running/terminal 状态转换、取消、reset、cleanup 和恢复规则。
- 增加 submission lifecycle observability events、用户文档、设计文档和示例。
