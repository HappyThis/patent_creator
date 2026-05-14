# 测试项难度复评

复评时间：2026-05-13

## 难度尺度

- `medium` / 3 分：问题边界较清楚，主要考察一个核心机制；需要读项目上下文，但跨模块组合有限。
- `hard` / 4 分：需要跨多个模块形成方案，涉及状态、接口、兼容性或安全边界；一般模型容易只写产品功能。
- `very_hard` / 5 分：涉及多子系统协作、恢复/幂等/并发/访问控制等组合机制；必须理解项目架构才能写出合理方案。

## 复评结论

| Case | 难度 | 分值 | 复评理由 |
| --- | --- | ---: | --- |
| `001` | `hard` | 4 | OpenCode runtime 接入需要理解 Mission Control 既有 runtime、session API、transcript、continue、UI 能力 gating 和文档契约，属于跨层适配机制；但问题目标较明确，低于最高档。 |
| `002` | `hard` | 4 | AgentRun 机制覆盖数据模型、provenance、eval 附着、事件流、API 和 MCP 工具访问，跨面较广；但本质是结构化观测与评估沉淀，状态恢复压力不如最高档。 |
| `003` | `medium` | 3 | Git-native 任务同步涉及外部格式映射、非阻塞同步和 Git 操作，但核心边界较集中，需求也更容易抽象为主存储到镜像仓库的同步机制。 |
| `004` | `hard` | 4 | ByteRover 运行时信号 sidecar 需要理解知识写入、检索排序、curate、dream/prune/synthesize 等路径，并处理迁移、读取融合和失败隔离，领域上下文较重。 |
| `005` | `very_hard` | 5 | Durable submissions 需要设计持久提交 ledger、幂等键、后台领取、Session 消息应用边界、恢复判定、取消和终态更新，是深 runtime 状态机问题。 |
| `006` | `medium` | 3 | 取消语义解耦聚焦在客户端 stream 生命周期与服务端 turn 生命周期，虽然要兼容 resume 和工具 continuation，但技术面相对集中。 |
| `007` | `very_hard` | 5 | Retained streaming agent tools 涉及父子 agent 编排、运行注册表、事件转发/重放、客户端聚合、访问控制、并发和取消清理，组合复杂度最高。 |
| `008` | `hard` | 4 | Browser iframe executor 虽然实现面较集中，但需要同时设计浏览器隔离执行、动态工具描述、跨上下文消息协议、安全控制、超时和清理，不能退化为简单 eval。 |
| `009` | `medium` | 3 | 多模态 workspace read 主要围绕文件类型探测、文本读取、多模态输出转换和大小限制，机制完整但边界清楚，适合作为中等难度样本。 |
| `010` | `very_hard` | 5 | 多会话 assistant 涉及用户级父目录、子会话隔离、共享 workspace、共享 MCP/OAuth、路由门禁、广播同步和父级调度，是架构级组合问题。 |

## 难度分布

- `medium`：`003`、`006`、`009`
- `hard`：`001`、`002`、`004`、`008`
- `very_hard`：`005`、`007`、`010`

当前分布偏中高难度，符合该 benchmark 用于评估软件专利技术方案生成能力的定位；如果后续需要更平滑的模型区分度，可以补充 2-3 个更轻量但仍具备新功能特征的 `medium` case。
