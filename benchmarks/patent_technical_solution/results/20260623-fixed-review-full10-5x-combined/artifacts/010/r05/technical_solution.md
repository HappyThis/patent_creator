## 技术方案

### 总体构思

本方案在现有 workspace 文件工具之上增加“文件检查—读取决策—内容封装—结果返回”的增强读取链路。读取工具接收到路径后，先取得文件元数据，再根据条目类型、MIME 类型、文件扩展名、大小和后端能力判断该文件应按文本读取、按多模态附件读取，还是仅返回结构化说明。这样，模型在面对代码、配置、Markdown 等文本文件时仍获得带行号的文本内容；面对图片、PDF 等模型可消费文件时，可以获得可传入模型上下文的文件部件；面对普通二进制或超限文件时，不再把字节流误解码成乱码，而是得到明确的文件信息和限制原因。

该增强链路复用 workspace 已有的持久化文件系统基础：文件条目具有路径、名称、类型、MIME 类型、大小、创建时间和更新时间等元数据；文件内容既可以以 UTF-8 文本形式保存，也可以以字节形式保存；较小内容可内联存储，较大内容可由对象存储承载并在元数据中保留索引。读取工具不改变写入、列举、搜索、编辑和删除等现有工具语义，而是在 read 工具的输出层引入面向多模态模型的内容选择和安全降级能力。

### 输入输出与状态定义

增强 read 请求的输入由基础定位参数和上下文控制参数组成。基础定位参数包括 `path`、`offset` 和 `limit`，其中 `offset` 表示 1 起始行号，`limit` 表示请求行数；上下文控制参数包括 `allowAttachment`、`modelCapabilityId`、`remainingAttachmentCount`、`remainingRawBytes` 和 `remainingEncodedBytes`。`allowAttachment=false` 时，即使文件类型可被模型消费，也只返回元数据降级结果；`modelCapabilityId` 用于查询目标模型能力表，能力表记录支持的 MIME 集合、最大单文件字节数、是否接受 base64、data URI 或对象引用。

| 字段组 | 字段 | 含义 |
| --- | --- | --- |
| 元数据 | `path`、`name`、`type`、`mimeType`、`size`、`updatedAt` | 来自 `stat` 或等效接口的文件条目信息，`type` 区分文件、目录和符号链接 |
| 能力 | `canReadText`、`canReadBytes`、`canReadStream`、`hasMimeMetadata`、`hasObjectRef` | 由运行时探测得到的后端能力矩阵，用于限定可进入的读取分支 |
| 预算 | `remainingAttachmentCount`、`remainingRawBytes`、`remainingEncodedBytes` | 单轮附件数量、原始字节和编码后字节的剩余额度 |
| 类型状态 | `contentKind` | 取值为 `text`、`multimodal`、`binary`、`directory`、`missing`、`unknown` |
| 执行状态 | `status` | 取值为 `ok`、`degraded`、`error` 或 `retryable_error` |

统一返回结构包括 `status`、`contentKind`、`metadata`、`range`、`text`、`attachment`、`reason`、`warnings` 和 `retryable`。文本分支中 `text` 非空、`attachment` 为空；多模态分支中 `attachment` 非空、`text` 为空或仅包含简短说明；降级分支中 `text` 和 `attachment` 均为空，`reason` 必填。`metadata` 始终保留路径、类型、MIME、大小和经过校正后的 `correctedMimeType`、`typeConfidence`；`warnings` 记录非主失败原因，例如 MIME 与扩展名不一致、预算接近上限或 stat 大小与实际读取大小不一致。

### 文件识别与读取决策机制

读取决策按固定状态机执行，任一强约束失败即进入降级终态，不再被后续规则覆盖。状态顺序为：S0 规范化路径并消解 `.`、`..` 和重复分隔符；S1 解析符号链接并限制最大解析深度，出现循环或越权路径时返回 `reason=PATH_INVALID`；S2 调用 `stat` 获取元数据，不存在返回 `NOT_FOUND`，目录返回 `IS_DIRECTORY`，未知条目返回 `UNSUPPORTED_ENTRY`；S3 探测后端能力；S4 基于 MIME、扩展名、文件头和文本解码探测确定候选类型；S5 校验文本或附件预算；S6 执行实际读取并封装结果。

类型判定采用“强文件头优先、可信 MIME 次之、扩展名补充、解码探测兜底”的优先级。工具读取不超过 4096 字节的文件头用于魔数判断：命中 PNG、JPEG、GIF、WebP 或 PDF 标识时，记录 `correctedMimeType` 并优先进入多模态候选，即使扩展名为 `.txt` 也不得进入文本分支；MIME 为 `text/*` 或 JSON、JavaScript、TypeScript、Markdown、YAML 等白名单类型时，只有在文件头未呈现二进制魔数且解码探测通过后才进入文本候选；MIME 缺失或为 `application/octet-stream` 时使用扩展名作为候选依据，但扩展名候选必须再通过文件头或解码探测确认。

| 条件 | 处理分支 | 主 reason 或输出 |
| --- | --- | --- |
| 路径不存在 | 降级终态 | `NOT_FOUND`，`retryable=false` |
| 路径为目录 | 降级终态 | `IS_DIRECTORY`，返回可用操作 `list` |
| MIME 为文本且解码通过 | 文本分支 | `contentKind=text` |
| MIME 为文本但文件头为图片/PDF | 多模态候选 | 记录 MIME 冲突 warning |
| MIME 缺失或 `application/octet-stream`，扩展名为文本且解码通过 | 文本分支 | `typeConfidence=extension+decode` |
| 扩展名为文本但文件头含二进制魔数或控制字符超阈值 | 降级终态 | `BINARY_NOT_SUPPORTED` 或多模态候选 |
| 魔数命中图片/PDF且模型能力允许 | 多模态分支 | `contentKind=multimodal` |
| 类型无法确认 | 降级终态 | `TYPE_UNKNOWN` |

### 文本读取兼容与长内容控制

文本分支的进入条件为：候选类型为文本、后端具备 `readFile` 或可由字节流严格解码为 UTF-8、文件头未命中已知二进制魔数，并且解码探测通过。读取后先去除 UTF-8 BOM，再将 CRLF 和单独 CR 统一为 LF；最后一行即使没有换行符也作为一行计入 `totalLines`，空文件返回 `totalLines=0`、`content=""`。分页参数在执行前归一化：`offset` 缺省为 1，`limit` 缺省为文件剩余行数；`offset<1` 或 `limit<1` 返回 `INVALID_RANGE`；`offset` 大于总行数时返回空内容并保留 `fromLine=offset`、`toLine=totalLines`。

文本解码采用严格 UTF-8 校验；出现替换字符、截断字节序列或非法编码时，工具终止文本分支并返回 `DECODE_FAILED`。若前 4096 字节中空字节或不可打印控制字符比例超过 1%，且这些字符不属于制表符、换行符、回车符等常见空白字符，则判定为非文本。单行超过最大行长时只截取前段内容并附加“truncated”提示，但行号仍使用原始行号；单次返回超过最大行数时仅截取返回区间的前若干行，并在 `warnings` 中记录剩余行数，确保长文件的定位、编辑和后续读取不会因截断而改变行号体系。

对兼具文本和附件特征的文件采用确定优先级：SVG、HTML、Markdown 等以文本编辑为主要用途的类型默认进入文本分支，除非调用参数明确要求附件且模型能力表支持对应 MIME；PDF 默认进入多模态分支，因为其字节结构不能稳定按行编辑；Markdown 中引用的图片不随 Markdown 文件自动附加，必须由 agent 对图片路径单独发起读取请求。文本分支已经成功返回指定行区间后，不再因为附件预算仍有剩余而附加同一文件的二进制副本。

### 多模态文件封装与模型输入转换

多模态分支的进入条件为：`allowAttachment=true`，模型能力表支持候选 MIME，后端具备 `readFileBytes`、`readFileStream` 或可用对象引用能力，且预算校验通过。附件对象采用确定结构：`type="file"`、`contentId`、`path`、`mimeType`、`dataEncoding`、`data`、`size`、`checksum`。`contentId` 由路径、更新时间和内容摘要生成，用于绑定附件层与摘要元数据；`checksum` 对实际传入模型的原始字节计算，防止摘要层与附件层错配。若模型接受 base64，则 `dataEncoding="base64"`；若接口要求 data URI，则 `dataEncoding="data-uri"` 并把 MIME 头部计入编码字节；若运行环境支持对象引用，则 `dataEncoding="object-ref"`，`data` 保存受控引用而非原始字节。

工具返回数据与推理框架内部的模型输入表示分层处理。工具返回的结构化结果保存 `attachment` 和 `metadata`，其中 `attachment.contentId` 与 `metadata.contentId` 必须一致；推理框架在组装模型消息 parts 时，只把 `status=ok` 且 `contentKind=multimodal` 的附件转换为消息 part。降级结果、警告和元数据仅作为工具结果文本或结构化对象供 agent 读取，不被转换为大段 base64 文本。多模态分支失败后不得退回到强制 base64 文本输出，而是进入降级终态并保留失败原因。

预算扣减按“能力匹配—单文件阈值—附件数量—原始字节预算—编码字节预算”的顺序执行。先比较 `size` 与模型能力表中的单文件上限；再检查 `remainingAttachmentCount>0`；随后以原始字节数扣减 `remainingRawBytes`。采用 base64 或 data URI 时，编码后大小按 `ceil(size/3)*4` 计算，data URI 还需加上 `data:<mimeType>;base64,` 的头部长度；采用对象引用时，编码预算只计入引用字符串长度，但原始字节预算仍按文件大小扣减。多个附件在同一轮竞争预算时，按工具调用确认的读取顺序扣减，先通过校验的附件占用预算；后续附件不足时返回 `BUDGET_EXCEEDED`，并在 `warnings` 中保留已触发的其他限制。

### 二进制降级、错误说明与限制控制

降级结果是不可直接传入模型的终态或可重试终态，其结构与成功结果保持同一 schema：`status` 为 `degraded`、`error` 或 `retryable_error`，`contentKind` 表示已知内容类别，`metadata` 保留可确认的文件信息，`text=null`，`attachment=null`，`reason` 为主原因，`warnings` 保存次要原因，`retryable` 表示再次读取是否可能成功，`nextActions` 给出可执行恢复方式。主原因按路径和权限错误、条目类型错误、后端能力缺失、类型不支持、预算不足、读取失败的优先级选择；例如后端不支持字节读取且文件超出预算时，主原因返回 `BACKEND_BYTES_UNAVAILABLE`，预算问题进入 `warnings`。

对象存储和流式后端遵循“先元数据、后字节、超限即丢弃”的读取规则。`stat.size` 可信且已超过单文件阈值时，不拉取对象内容，直接返回 `FILE_TOO_LARGE`；`stat.size` 缺失或不可信时，以流式 chunk 累计实际字节数，累计值一旦超过单文件阈值、剩余原始字节预算或编码预算，即取消读取、丢弃已缓存 chunk，并返回 `BUDGET_EXCEEDED` 或 `FILE_TOO_LARGE`，不得返回不完整附件。若对象存储读取超时、对象缺失、内容摘要校验失败或读取过程中断，保留元数据并返回 `OBJECT_READ_FAILED` 或 `CHECKSUM_FAILED`，`retryable` 根据错误类型标记为 true 或 false。

| reason | 产生步骤 | retryable | 恢复方式 |
| --- | --- | --- | --- |
| `NOT_FOUND`、`IS_DIRECTORY`、`PATH_INVALID` | 路径检查或 stat | 否 | 更换路径、对目录使用 list |
| `BACKEND_BYTES_UNAVAILABLE` | 能力探测 | 否或取决于后端 | 切换具备字节读取的 workspace 或后端 |
| `MODEL_MIME_UNSUPPORTED` | 模型能力匹配 | 否 | 转换为模型支持的图片或 PDF 类型 |
| `TYPE_UNKNOWN`、`BINARY_NOT_SUPPORTED` | 类型分类 | 否 | 转换为文本、图片或 PDF 后重新写入 |
| `DECODE_FAILED` | 文本解码探测 | 否 | 以二进制方式保存或转换编码 |
| `FILE_TOO_LARGE`、`BUDGET_EXCEEDED` | 预算检查或流式累计 | 是 | 压缩、裁剪、分页或提高预算 |
| `OBJECT_READ_FAILED` | 对象存储读取 | 是 | 重试或检查对象存储配置 |
| `CHECKSUM_FAILED` | 读取后校验 | 是 | 重试读取或重新写入文件 |

### 多后端兼容与能力探测

多后端兼容通过能力矩阵而不是具体类名实现。工具初始化或每次调用时探测 `stat`、`readFile`、`readFileBytes`、`readFileStream`、MIME 元数据和对象引用能力：缺少 `stat` 时只能返回后端能力错误，不能进入任何读取分支；具备 `stat+readFile` 时支持文本分支；具备 `stat+readFileBytes` 或 `stat+readFileStream` 时支持文件头校验和多模态字节封装；具备 MIME 元数据时优先使用元数据分类，缺失时转入扩展名与文件头探测；具备对象引用时可在模型支持的情况下使用 `object-ref` 编码。

真实 workspace、共享 workspace 和自定义文件后端均通过同一矩阵进入状态机。真实 workspace 通常提供 `stat`、文本读取、字节读取、流式读取和 MIME 元数据；共享 workspace 代理对这些方法逐项透传，若父级返回超时或方法不存在，则该能力在本次调用中视为缺失并产生对应降级原因；自定义后端的最小兼容要求为 `stat+readFile`，在此条件下仍完整保留文本读取体验。由于每个分支只依赖显式探测到的能力，增强读取不会迫使旧后端一次性实现所有接口，也不会在能力缺失时尝试不安全的二进制文本化。

| 探测到的能力组合 | 允许分支 | 不允许分支及处理 |
| --- | --- | --- |
| `stat` 缺失 | 无 | 返回 `BACKEND_STAT_UNAVAILABLE` |
| `stat + readFile` | 文本读取、目录/缺失判断 | 多模态分支返回 `BACKEND_BYTES_UNAVAILABLE` |
| `stat + readFile + mimeMetadata` | MIME 优先的文本分类 | MIME 缺失时仍需扩展名或解码探测 |
| `stat + readFileBytes` | 文件头校验、图片/PDF 附件 | 大文件仍受单文件和总预算限制 |
| `stat + readFileStream` | 流式大文件探测和附件读取 | 超限时取消流并丢弃 chunk |
| `stat + objectRef` | 对象引用附件 | 仅在模型能力表允许对象引用时使用 |
