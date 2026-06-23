# 专利交底书技术方案 benchmark 批量评估报告

- 批次：`20260623-fixed-review-full10-5x-combined`
- 重复次数：`5`
- Subject API：`responses`
- Subject 模型：`gpt-5.5`
- Base URL：`https://api.yairouter.com`
- Reasoning effort：`high`；max_output_tokens：`8192`
- Web search：`True`；context_size：`low`
- 压缩配置：max_tokens=`128000`，threshold_ratio=`0.8`，token_char_coefficient=`0.5`
- Runtime：llm_timeout=`45.0`，llm_max_retries=`2`，compression_timeout=`300.0`
- 运行参数：workers=`10`，round_timeout=`1800`，judge_timeout=`1800`，skip_judge=`False`

## 汇总表

| Case | 运行次数 | 产物成功 | 已评分 | 平均分 | 最低分 | 最高分 | 标准差 | 状态分布 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 001 | 5 | 5 (100%) | 5 | 96.2 | 95 | 97 | 0.75 | scored:5 |
| 002 | 5 | 5 (100%) | 5 | 95.2 | 93 | 96 | 1.17 | scored:5 |
| 003 | 5 | 5 (100%) | 5 | 96 | 96 | 96 | 0 | scored:5 |
| 004 | 5 | 5 (100%) | 5 | 95 | 93 | 97 | 1.41 | scored:5 |
| 005 | 5 | 5 (100%) | 5 | 93.4 | 91 | 96 | 1.74 | scored:5 |
| 006 | 5 | 5 (100%) | 5 | 95.4 | 94 | 97 | 1.2 | scored:5 |
| 007 | 5 | 5 (100%) | 5 | 96 | 94 | 97 | 1.1 | scored:5 |
| 008 | 5 | 5 (100%) | 5 | 96.4 | 95 | 97 | 0.8 | scored:5 |
| 009 | 5 | 5 (100%) | 5 | 97 | 96 | 98 | 0.63 | scored:5 |
| 010 | 5 | 5 (100%) | 5 | 96.6 | 96 | 97 | 0.49 | scored:5 |

## 逐项结果

### Case 001

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：97, 96, 97, 96, 95
- 主要扣分点：
  - confidenceScore 的计算只列出影响因素，未进一步给出阈值配置、版本迁移或冲突场景下的定量规则，作为保护性技术特征还可更凝练。
  - 可变形实施方式主要体现在候选 source、JSONL/JSON/SQLite 和后续版本适配，若再抽象说明不同证据阈值、不同权限模型或不同本地状态载体的替代实施，会更接近满分。
  - 对未知字段的原始载荷保留或扩展区机制描述较弱，主要依赖 evidence 和诊断候选，较参考方案的“保留原始载荷或扩展区”略少一层兼容说明。
- 主要缺失机制：
  - confidenceScore、可信等级阈值与 parser 版本之间的配置/迁移关系可以更具体。
  - 可进一步概括 adapter profile 的可替换实施方式，避免过度限定为 JSONL 或 SQLite。
  - 未知格式下的原始载荷保留、后续解析器升级复用该载荷的机制可以更明确。

### Case 002

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：95, 96, 96, 93, 96
- 主要扣分点：
  - 与参考标尺最核心的输入应用 checkpoint 相比，方案虽然有输入消息标识、输入消息已持久化和 INSERT OR IGNORE，但没有单独、明确地把任务输入尚未应用到会话和已经进入会话上下文作为可恢复状态边界展开。
  - 交底书表达略偏工程架构说明，字段和接口枚举非常多，部分内容更像实现设计文档，核心可保护特征需要进一步收束。
  - 对具体项目快照中的既有 agent 执行框架、现有持久化对象或会话状态来源没有明显绑定，导致部分实施条件是通用设计而非项目适配描述。
- 主要缺失机制：
  - 从专利保护角度提炼独立权利要求式的最小必要特征组合，目前细节较多但主从层次不够集中。
  - 可进一步界定与既有 agent 会话执行链路的复用边界，例如哪些阶段复用原有 turn/工具调用/流式输出机制，哪些阶段由外部任务层新增。
  - 可进一步说明在不同存储/队列实现下的等价变形，例如数据库唯一约束、分布式锁、消息队列去重键或事件日志均可承载相同机制。

### Case 003

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：96, 96, 96, 96, 96
- 主要扣分点：
  - 共享资源变更同步主要体现在目录广播、跨会话检索和共享记忆版本刷新，未像参考方案那样突出一个独立的共享资源变更广播机制；不过其版本冻结、下一轮刷新和广播会话列表已覆盖大部分一致性需求。
  - 共享资源变更对其他会话或多个客户端的主动广播机制写得不如参考方案明确，更多是通过 resource_version、目录变化广播和重读冲突处理体现一致性。
  - 共享资源变更广播有涉及，但对工作区文件变更向多个会话视图广播的机制不如其他部分明确。
- 主要缺失机制：
  - 共享 Workspace、工具连接或授权状态变化后的通用变更广播/订阅机制及客户端刷新规则。
  - 几乎无关键机制缺失；若要进一步增强，可将核心保护点压缩为会话注册表门禁、用户级资源代理、版本化生命周期和共享变更广播四个独立必要特征。
  - 可增加更专利化的总括技术效果段，将父子隔离、代理访问、注册表门禁、版本广播和删除状态机分别对应到技术效果。

### Case 004

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：96, 94, 97, 93, 95
- 主要扣分点：
  - agent 排行或聚合统计仅在外部系统复用处作为筛选应用被提到，缺少更明确的运行级数据如何形成 agent/任务/时间维度排行或统计的机制。
  - 参考方案强调运行创建、状态变化、完成和评估附着时发布事件供前端、监控或自动化订阅；该方案有事件归集和时间线，但对向外发布事件/订阅机制描述不足。
  - 对已有项目事实的表述有少量过确定化倾向，例如默认存在质量评审、token_usage、mcp_call_log 或 eval_traces 等能力；若项目快照未确认这些对象，应表述为新增或可扩展记录。
- 主要缺失机制：
  - 可进一步抽象核心权利要求层级，区分必选机制和可选扩展机制，避免保护范围被过多实现细节限制。
  - 可进一步明确输入摘要或触发请求摘要如何脱敏、截断并与 run_id 绑定，以便复盘时同时看到输入侧事实。
  - 基于运行状态、成本、评估结果生成 agent 维度、任务维度、时间维度统计和排行榜的聚合规则。

### Case 005

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：91, 94, 92, 96, 94
- 主要扣分点：
  - 与 rubric 中偏向“仓库文件 + Git commit 历史”的参考路径相比，本方案主要落在 Git 托管平台 issue/label 载体，未说明 Git 提交历史、文件稳定路径或仓库内机器可读文件结构，因此在交底表达适配度和参考标尺完全贴合度上略有折扣。
  - 入站创建本地任务和双向回写机制较强，但与“应用数据库为唯一可信源”的边界需要更明确，例如哪些外部条目可成为新任务、哪些只能作为观测或冲突记录。
  - 参考方案强调的镜像仓库初始化、管理入口、同步状态查询和手动触发同步只被间接覆盖，缺少作为用户或管理员可观察可恢复入口的明确技术安排。
- 主要缺失机制：
  - 仓库文件型镜像的稳定路径映射、文件格式和 Git commit/push 持久化机制未覆盖；本方案以 issue 编号和元数据区作为等价稳定关联机制，但不提供文件级 Git 历史。
  - 删除/归档任务到外部对象的生命周期映射规则，例如删除文件、关闭 issue、写入删除标记或保留 tombstone。
  - 对外部 issue 正文中机器可读结构的格式约束，例如 frontmatter、JSON 块或固定锚点字段，以便 agent 稳定解析。

### Case 006

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：96, 97, 94, 96, 94
- 主要扣分点：
  - sidecar 或本机信号层读写失败时的 fail-open 机制没有被明确展开，例如失败诊断、降级读取默认值、写入延迟队列与不阻断 Markdown 主流程之间的关系不够直接。
  - sidecar 或本机状态库故障的 fail-open 机制已有描述，但主要集中在反馈事件和检索静态回退，对知识写入、人工整理保存时 sidecar 故障如何隔离的说明稍弱于其他部分。
  - sidecar 或本机状态库的具体存储接口、批量读取 API 形态未直接命名为 sidecar，但其本机状态库、缓冲队列和融合流程已达到等价深度。
- 主要缺失机制：
  - sidecar 批量读取接口或索引级预取策略的更明确边界，例如按候选 id 集合一次性加载并处理缺失记录。
  - sidecar 读写失败后的诊断记录、重试或延迟补偿策略。
  - 动态 sidecar 与现有项目快照中具体知识文件解析/索引入口的衔接方式未展开，但这不构成高分必要条件。

### Case 007

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：94, 96, 97, 97, 96
- 主要扣分点：
  - 个别机制如服务端重启后恢复运行实例、缓冲压缩为终态摘要等属于扩展边界，未进一步限定实施条件，但不影响主方案成立。
  - 交底书表达偏工程设计文档，字段和表格较密，专利交底中的必要特征提炼可以更集中；不过内容本身具有可保护技术特征。
  - 可配置策略有默认 durable 模式和资源约束模式，但未形成非常清晰的配置矩阵，例如 durable、request-lifetime、timeout-cancel 等模式下各事件的具体映射表。
- 主要缺失机制：
  - 可补充更简洁的既有系统适配边界，例如哪些字段是新增、哪些只是可选实施例，避免把实施例误读为当前项目事实。
  - 可进一步压缩并抽象配置策略的表达，使 preserve、cancel_immediately、cancel_after_grace、explicit_only 与既有协议/应用配置的绑定更清楚。
  - 可进一步提炼一组最小必要权利要求式特征，避免大量可选字段稀释核心保护点。

### Case 008

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：97, 95, 96, 97, 97
- 主要扣分点：
  - A few implementation-specific names and capabilities are asserted as if they are existing framework facts rather than framed only as optional implementation choices, which creates minor support risk if the project snapshot did not contain them.
  - The causal technical-effect discussion is present throughout but not separately synthesized as strongly as the mechanism sections; effects are mostly implicit from the proposed state, replay, and control mechanisms.
  - The live-tail reattachment fallback is conservative but leaves one branch where continued child execution may no longer be visible to the parent, so the recovery policy could be tightened for complete observability.
- 主要缺失机制：
  - A clearer account of how parent LLM context is bounded when many child chunks are visible to the user but only summaries or structured outputs are returned to the parent model would further strengthen the parent-model integration boundary.
  - A more direct technical-effect section tying each mechanism to each effect would improve disclosure readability, although the causal links are mostly inferable.
  - A more explicit normalized retention policy matrix for different terminal states, storage quotas, and retention durations would strengthen the cleanup/保留 portion, though the core cleanup mechanism is already present.

### Case 009

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：97, 98, 97, 97, 96
- 主要扣分点：
  - 关于默认禁止 eval、Function、动态 import、fetch 等全局能力的表述偏强，实际落地需要 CSP、sandbox iframe、Worker 创建策略或运行时改写等配套约束，方案未展开这些隔离配置条件。
  - 协议状态中没有显式展开 reference 中的 ready/execute/execution-result 消息，但通过会话前置条件、运行状态、toolCall/toolResult/toolError 和最终执行结果机制已基本覆盖等价功能。
  - 协议类型没有直接命名 ready/execution-result/cleanup 等参考方案中的部分状态，但已有代码执行开始、工具结果、取消、超时、心跳和清理控制，属于等价覆盖而非实质缺失。
- 主要缺失机制：
  - 可进一步明确最终 execution-result 如何与单个 tool-result 区分并返回外层 agent chat。
  - 可进一步明确隔离容器 ready 握手与 execute 消息之间的启动时序。
  - 可进一步说明敏感字段脱敏规则或 schema 摘要的生成方式，以避免实现时规则过宽。

### Case 010

- 运行次数：5
- 产物成功：5 (100%)
- 已评分次数：5
- 状态分布：scored:5
- 分数：97, 96, 97, 97, 96
- 主要扣分点：
  - PDF 页级处理只说明在已有页级衍生文件或页范围请求下执行限制校验，对 PDF 页数获取、页级衍生物生成责任边界说明略简。
  - 个别后端形态和运行场景表述偏具体，如果探索材料没有这些对象，会有轻微无依据风险。
  - 交底书表达整体技术性强，但字段表和 reason 表偏工程接口规格，专利式必要技术特征提炼还可以更凝练。
- 主要缺失机制：
  - PDF 页数检测或页级对象生成在不同后端中的最小可实施方式。
  - 可进一步补充多模态内容块进入模型时附带简短来源说明文本的机制，例如文件名、路径、MIME、大小与片段范围如何与资源块一起呈现。
  - 可进一步说明安全策略表如何配置或更新，以避免字段枚举很多但策略来源不够明确。
