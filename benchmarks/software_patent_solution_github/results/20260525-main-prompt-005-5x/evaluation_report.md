# 软件专利技术方案 benchmark 批量评估报告

- 批次：`20260525-main-prompt-005-5x`
- 重复次数：`5`

## 汇总表

| Case | 运行次数 | 产物成功 | 已评分 | 平均分 | 最低分 | 最高分 | 标准差 | 状态分布 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 005 | 5 | 5 (100%) | 5 | 78.4 | 75 | 85 | 4.27 | scored:5 |

## 逐项结果

### Case 005

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：75, 85, 82, 75, 75
- 主要扣分点：
  - DELETE running 任务先取消再删除的策略会删除追踪记录，随后异步执行完成或恢复逻辑如何避免回写已删除记录没有说明。
  - cleanTask 描述与表设计不一致：表中不保存完整消息内容，却说清理消息内容释放 SQLite 空间，若清理 assistant_messages 又会影响普通会话语义。
  - 关键缺口是接收提交时就在同一事务中把用户消息写入会话，缺少持久 submission ledger 到 Session 的明确消息应用边界，例如 messagesAppliedAt、applied message ids、turnId/requestId checkpoint 等；这违反 rubric 中高分方案对写入前/写入后崩溃区分的要求。
- 主要缺失机制：
  - list/inspect API 与 submission lifecycle observability events。
  - list/inspect 与 submission lifecycle observability events。
  - list、delete、inspect 或等价生命周期管理接口，以及幂等键保留窗口的确定策略。
