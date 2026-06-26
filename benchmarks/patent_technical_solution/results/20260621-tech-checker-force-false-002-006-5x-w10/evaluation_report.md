# 专利交底书技术方案 benchmark 批量评估报告

- 批次：`20260621-tech-checker-force-false-002-006-5x-w10`
- 重复次数：`5`
- Subject API：`responses`
- Subject 模型：`gpt-5.5`
- Base URL：`https://api.yairouter.com`
- Reasoning effort：`high`；max_output_tokens：`8192`
- Web search：`True`；context_size：`low`
- 压缩配置：max_tokens=`128000`，threshold_ratio=`0.8`，token_char_coefficient=`0.5`
- Runtime：llm_timeout=`45.0`，llm_max_retries=`2`，compression_timeout=`180.0`
- 运行参数：workers=`10`，round_timeout=`900`，judge_timeout=`900`，skip_judge=`False`

## 汇总表

| Case | 运行次数 | 产物成功 | 已评分 | 平均分 | 最低分 | 最高分 | 标准差 | 状态分布 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 002 | 5 | 4 (80%) | 4 | 94.5 | 91 | 97 | 2.29 | round_failed:1, scored:4 |
| 006 | 5 | 5 (100%) | 5 | 90.4 | 84 | 93 | 3.32 | scored:5 |

## 逐项结果

### Case 002

- 运行次数：5
- 产物成功：4 (80%)
- 已评分次数：4
- 状态分布：round_failed:1, scored:4
- 分数：91, 96, 94, 97
- 失败状态：round_failed
- 主要扣分点：
  - 任务输入写入会话前后的应用边界没有形成明确的 input_applied 类持久标记；方案更多依赖已持久化消息、流片段和检查点作恢复证据。
  - 后台执行器的条件领取机制虽可从版本号和前驱状态约束推出，但没有像参考方案那样单独强调多执行者并发领取同一任务时的事务性 claim 规则。
  - 后台执行器的条件领取机制表达不够突出，虽然提到队列、事务保护和持久执行单元，但没有明确说明多个执行者并发领取同一待执行任务时通过条件更新或锁定防止重复执行。
- 主要缺失机制：
  - 可补充执行者租约过期或心跳超时后的重领规则，用于区分仍在运行、可恢复和应标记 interrupted 的 running 任务。
  - 可进一步明确后台执行器领取任务时采用 compare-and-set 或事务更新，将 queued 改为 injecting/running 的同时写入执行者租约，防止多个恢复器或队列消费者同时领取。
  - 外部副作用步骤的确认凭证或去重令牌，以支撑恢复时判断是否可重试。

### Case 006

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：93, 91, 84, 91, 93
- 主要扣分点：
  - sidecar/本机动态信号层故障时的失败隔离和 fail-open 策略基本缺失；方案提到提交失败保留增量重试，但没有说明动态层不可读或不可写时不阻断 Markdown 内容读写和检索主流程。
  - 信号层接口形态仍偏概括，虽然列出 overlay、sidecar、本地数据库、键值存储等变体，但缺少更明确的记录结构或批量读取接口边界。
  - 公式处出现占位式引用，实际公式未完整展开，表达上略影响交底书严整性。
- 主要缺失机制：
  - sidecar 覆盖数据在批量检索场景下的批量读取、缓存失效和版本一致性规则。
  - sidecar 读取或写入失败时的 fail-open 规则、诊断记录和默认信号回退。
  - sidecar/本机信号层读失败、写失败、记录损坏或锁冲突时的 fail-open 与诊断隔离机制。
