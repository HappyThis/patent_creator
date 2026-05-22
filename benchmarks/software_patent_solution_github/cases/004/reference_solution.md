# 隐藏参考方案

## 技术问题

ByteRover 的 context tree 使用 Markdown 文件承载项目知识，并支持版本控制、协作、检索、整理和自动维护。部分字段属于稳定语义内容，例如标题、摘要、标签、关键词、关联关系、创建时间和更新时间；另一部分字段属于运行时信号，例如访问次数、重要性、成熟度、recency、更新次数等。这些运行时信号会在查询、检索、curate、dream、prune、synthesize 等过程中频繁变化。

如果把运行时信号继续写入共享 Markdown frontmatter，会导致知识文件在没有真实内容变化时反复变脏，造成 Git diff 噪声、团队合并冲突和版本控制污染；如果完全丢弃运行时信号，又会削弱检索排序、知识成熟度判断和自动整理效果。因此需要一种把共享语义内容和本机动态运行信号分离，同时在读取和排序时重新组合的机制。

## 核心技术构思

在 ByteRover 中建立“语义 frontmatter + 运行时信号 sidecar + 新写入路径迁移 + 读取融合 + 原子更新”的 context tree 信号分层机制。

系统将 Markdown 知识文件中的 frontmatter 限定为适合共享和审阅的稳定语义字段，将频繁变化的运行时信号迁移到本机 sidecar store。知识写入、curate、检索命中、dream 操作和归档/剪枝流程在需要更新动态信号时，不再修改 Markdown 文件，而是更新对应路径的 sidecar 记录。检索、排序和知识树展示时，系统从 Markdown 读取语义字段，并从 sidecar 读取运行时信号，再融合成上层可用的完整知识对象。为兼容迁移阶段，系统应保证旧 Markdown 文件仍可被解析和使用；旧 frontmatter 中遗留的运行时信号可以被忽略、懒初始化或按需迁入 sidecar，但不得破坏知识内容读取。系统还应对 sidecar 失败进行日志记录和 fail-open 处理，避免动态信号存储故障影响知识内容本身。

## 必要技术特征

1. 字段分层：区分稳定语义 frontmatter 字段与运行时信号字段。
2. sidecar 存储：为每个知识路径建立独立运行时信号记录，保存 accessCount、importance、maturity、recency、updateCount 等动态字段。
3. 路径键映射：使用知识文件相对路径作为 sidecar 信号记录的定位依据，并支持批量读取和更新。
4. 原子更新：对单个知识路径的运行时信号提供原子读改写能力，避免并发查询或 curate 更新互相覆盖。
5. 读取融合：检索、排序、知识树和自动整理读取知识时，将 Markdown 语义字段与 sidecar 动态信号合并使用。
6. 写入剥离：Markdown 写入器和 dream 操作只写稳定语义字段，不再把运行时信号写回 frontmatter。
7. 迁移兼容：新写入和更新路径写入 sidecar；旧 Markdown 文件仍可正常解析，旧运行时字段可通过懒初始化、自愈或按需迁移进入 sidecar。
8. 失败隔离：sidecar 写入或读取失败时记录日志，业务流程尽量继续执行，避免知识内容操作失败。
9. 清理一致性：归档、删除、剪枝或合并知识文件时，同步删除或更新对应 sidecar 信号，降低孤儿记录影响后续排序的风险；全量孤儿扫描或重命名追踪可作为增强机制。

## 关键流程

### 知识写入流程

1. 用户或 agent 通过 curate / save 写入知识文件。
2. 系统将标题、摘要、标签、关键词、关联关系、创建/更新时间等稳定字段写入 Markdown frontmatter。
3. 系统为同一路径初始化或更新运行时信号 sidecar。
4. 若 sidecar 更新失败，系统记录失败信息，但不阻断知识文件写入。

### 检索与排序流程

1. 用户发起查询。
2. 系统读取 Markdown 知识内容和稳定语义字段。
3. 系统批量读取候选路径对应的 sidecar 运行时信号。
4. 排序模块结合语义匹配分数和运行时信号计算最终排序。
5. 对被访问或命中的知识路径，系统更新 accessCount、recency 或其他动态信号。
6. Markdown 文件保持不变，版本控制不会因为查询行为产生噪声。

### 自动整理流程

1. dream / consolidate / prune / synthesize 等流程分析知识树。
2. 系统从 Markdown 和 sidecar 融合后的知识对象中读取所需信号。
3. 当操作改变知识内容时，更新 Markdown 稳定字段。
4. 当操作只改变成熟度、访问统计或排序信号时，仅更新 sidecar。
5. 删除或归档知识时，清理对应 sidecar 记录。

## 技术效果

- 共享 Markdown 知识文件只反映真实语义内容变化，减少 Git diff 噪声和团队合并冲突。
- 检索排序、成熟度判断和自动整理仍可使用动态运行时信号，不牺牲智能检索质量。
- 高频查询和使用统计不再污染 context tree 版本控制状态。
- sidecar 失败不会破坏核心知识写入流程，系统可靠性更好。
- 字段分层为后续更多本机个性化信号、团队共享信号或隐私隔离策略提供扩展空间。

## 目标能力边界

必须解决的是“稳定共享知识内容”和“本机动态运行信号”之间的存储分层。Markdown frontmatter 应只承载适合团队共享和代码审查的稳定语义字段；频繁变化、与本机检索和使用行为相关的信号进入 sidecar。方案不是简单删除 accessCount 等字段，也不是把所有知识都迁移到数据库。

该能力应兼容现有 context tree、search、curate、dream、archive 和 manifest 读取路径。高分答案应强调旧文件解析兼容、新写入路径 sidecar 化、缺省信号和后续使用自愈，而不是一次性破坏已有 frontmatter 或强制要求一次性全量回填。

## 核心数据结构与状态模型

- 稳定 frontmatter：title、summary、tags、keywords、relations、createdAt、updatedAt、source 等。
- runtime signal 记录：以知识相对路径作为 key，记录 `accessCount`、`importance`、`maturity`、`recency`、`updateCount` 等动态字段；`lastAccessedAt`、`scoreHints`、`schemaVersion` 等可作为等价实现的扩展字段，不要求固定存在。
- sidecar store 接口：`get(path)`、`getMany(paths)`、`set(path, signals)`、`update(path, patch/updater)`、`batchUpdate`、`delete`、`list`。
- 读取融合对象：Markdown 语义字段 + sidecar 信号 + 缺省信号值，供检索和 dream 流程统一消费。
- 清理状态：文件删除、归档、剪枝或合并时，对应 sidecar 记录同步删除、迁移或标记 stale；重命名追踪和孤儿扫描可作为增强能力。

并发更新应使用原子 read-modify-write 或单记录 updater，避免多个检索命中同时更新 accessCount 时互相覆盖。

## 项目集成点

方案应接入 markdown writer、memory scoring、search knowledge service/tool、memory symbol tree、curate tool、context tree archive/manifest、dream consolidate/prune/synthesize 和 service initializer。只改 writer 不够，因为排序和自动整理读取路径也必须从融合对象取信号。

## 必须命中的评分锚点

- 明确字段分层规则，哪些字段留在 Markdown，哪些进 sidecar。
- 检索和排序读取时重新融合，而不是丢弃运行信号。
- 高频信号更新不再改写共享 Markdown。
- sidecar 失败 fail-open，不阻断知识内容保存。
- 删除、归档、剪枝或合并时清理 sidecar，降低孤儿信号风险。
- 有迁移期兼容策略，例如旧文件解析兼容、新写入路径 sidecar 化、缺省信号、懒初始化或按需迁移。

## 常见错误方案

- 把所有 Markdown 知识迁入数据库，破坏可版本控制的共享资产。
- 直接删除运行时信号，导致检索排序和成熟度判断退化。
- 只在保存时分离，检索和 dream 仍然读旧 frontmatter 字段。
- sidecar 失败导致知识写入失败，降低核心流程可靠性。
- 用绝对路径作为唯一 key，导致团队环境或仓库移动后信号无法匹配。

## 对应真实实现

真实 PR #456 采用了如下实现方向：

- 新增 `RuntimeSignalsSchema`，定义运行时信号字段和稳定语义 frontmatter 字段边界。
- 新增 `RuntimeSignalStore` 和 `IRuntimeSignalStore`，使用路径键存储运行时信号，并提供 get、getMany、set、update、batchUpdate、delete、list 等能力。
- 修改 markdown writer、memory scoring、search knowledge、curate tool、memory symbol tree 等路径，将运行时信号从 Markdown frontmatter 迁移到 sidecar，并使 legacy frontmatter 动态字段不再污染新写入。
- 对 curate、search、archive、manifest、consolidate、prune、synthesize、dream 等流程进行 sidecar 写入、读取融合或清理一致性改造。
- 增加 sidecar failure logging、dual-write pipeline、VC clean regression 等测试，验证运行时信号不再污染版本控制状态。

## 等价机制说明

`sidecar` 表示与共享 Markdown 分离的本机运行时信号存储，不限定具体实现。SQLite、JSON/KV、本机轻量数据库或等价旁路存储，只要提供路径键映射、批量读取、原子更新、读取融合、迁移兼容和 fail-open，都应按等价机制评价。
