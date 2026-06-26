## 技术方案

### 总体构思与读取决策层

现有工作区读取工具若将所有文件都按文本执行 readFile、split 和行号封装，会在图片、PDF、压缩包等文件上产生二进制误读、上下文体积失控和多模态内容无法利用的问题；不同后端是否支持字节读取、流式读取或文件检测也会导致同一路径在不同运行环境中的行为不一致。本方案在读取入口与具体后端之间设置文件读取决策层，将“文件检测能力”统一定义为基于 stat 元信息、扩展名、MIME、detectFile 结果和必要字节采样得到文件判定结论的过程；将“模型内容转换”统一定义为把可消费文件本体封装为模型消息内容块的过程；将“结构化降级”统一定义为不读取或不传递文件本体、但返回可解释元信息和后续动作建议的结果。

读取请求输入包括 path、offset、limit、请求序号、是否允许多模态内容块、单文件硬上限和本次请求的文件数量上限；模型能力输入包括 supportedMimeTypes、maxRawBytes、maxEncodedBytes、maxFiles、支持的数据 URL、对象引用、流式引用等传输方式以及图片、PDF 的具体支持范围；后端能力输入包括 stat、readFile、readFileBytes、readFileStream、detectFile、hashFile、对象引用生成能力和能力版本。统一读取结果至少包括 status、kind、path、name、mime、extension、size、mtime、textLines、pageInfo、contentBlocks、fileSummary、degradeReason、suggestedActions、capabilityUsed、errorStage 和 consistency 字段。文本结果填充 textLines 与 pageInfo，多模态结果填充 contentBlocks，降级结果填充 fileSummary、degradeReason 与 suggestedActions，结构化错误结果填充 status、errorStage 和可取得的路径信息。

1. 先执行路径规范化和访问策略检查；若路径越出工作区、命中禁止访问规则、指向设备文件、管道文件或不可跟随的越界符号链接，则不读取文件本体，返回 kind 为 restricted 的结构化降级结果。
2. 调用 stat 获取类型、大小、MIME、更新时间或版本标识；若路径不存在，返回 not_found 错误；若为目录，返回 directory 错误并可附带目录摘要；若 stat 阶段无权限或后端不可达，返回 errorStage 为 stat 的错误结果。
3. 在 stat.size 超过单文件硬上限时立即终止本体读取，返回 size_limit 降级；未超过硬上限但 MIME、扩展名或类型信息缺失、冲突或为 application/octet-stream 时，触发 detectFile 或有限字节采样。
4. 依据检测结论进入互斥分支：可确认为文本时进入文本读取；可确认为模型支持的图片、PDF 或其他多模态类型且后端具备本体读取能力时进入模型内容转换；确认为二进制但模型不支持、缺少字节能力或超过模型限制时进入结构化降级；读取或封装阶段发生不可恢复异常时返回结构化错误或降级摘要。

分类冲突按风险优先处理：路径不存在、目录、受限路径和特殊文件优先于 MIME 判断；硬大小上限优先于任何内容转换；检测到 NUL 字节、魔数为常见二进制格式或控制字符比例超过阈值时，即使扩展名为 .txt 也不进入文本 split；MIME 缺失但扩展名或魔数表明为图片、PDF 时，继续进行大小和模型能力校验；MIME 为 application/octet-stream 但采样字节无 NUL、UTF-8 解码非法字节比例低于阈值且文本换行分布正常时，可按文本候选处理。若 MIME、扩展名和魔数三者不一致，则以魔数和字节采样结论为准，并在结果的 consistency 字段记录冲突来源。

### 文本读取兼容与长内容控制

文本分支的进入条件为：文件未命中受限或硬上限规则，文件检测结论为 text，或者 MIME 缺失但采样字节满足文本候选条件。采样可从文件头部读取不超过可配置 maxSampleBytes 的字节，检查 BOM、NUL 字节、UTF-8 解码非法字节比例、不可见控制字符比例和换行分布。带 UTF-8 BOM 的文件在解码前去除 BOM；CRLF、LF 或混合换行均归一为逻辑行分隔；空文件返回 totalLines 为 0 的成功文本结果；无尾随换行的末尾内容仍作为最后一行。若解码失败、NUL 字节存在或控制字符比例超过阈值，结果转为 binary_risk 降级，不再把字节内容按文本行输出。

分页参数采用 0 基 offset 表示从第几行开始截取，limit 表示最多返回的逻辑行数；limit 缺省时使用默认值，超过 maxLinesPerRead 时收敛到最大值，limit 为 0 时返回空 textLines 和完整 pageInfo，负数 offset 或 limit 作为参数错误返回。offset 超过 totalLines 时返回空行列表而不报文件错误。每个返回行包含 lineNumber、text、truncated、originalLength 字段；单行截断按字符数执行，超过 maxLineChars 时保留前缀并设置 truncated=true，同时不改变 totalLines。若 stat 后文件被追加或修改，系统比较读取前后的 size、mtime、etag、hash 或版本号；轻微变化时重新读取并重新分页一次，仍不一致时返回 file_changed 降级，避免行号对应到已经变化的文件。

文本读取分支兼容只具备 stat 和 readFile 的旧后端，但该兼容不覆盖已经识别为二进制风险的结果。若后端缺少 readFileBytes 或 detectFile，且 stat.mimeType 与扩展名均指向文本，则使用 readFile 解码；若 stat 或扩展名指向图片、PDF、压缩包、音视频、数据库等二进制类型，则返回 backend_capability_missing 降级，而不是调用 readFile 后 split。这样，已成功生成的文本结果不会被后续检测失败覆盖为通用失败，已识别的二进制风险也不会因旧后端仅有文本接口而被误读。

### 多模态文件识别与模型内容转换

模型能力表用于约束多模态内容转换，其字段包括 supportedMimeTypes、imageLimits、pdfLimits、maxRawBytesPerFile、maxEncodedBytesPerBlock、maxFilesPerRequest、maxBlocksPerRequest、transferModes、allowPartialPdfPages 和能力版本。transferModes 至少区分 dataUrl、objectRef 和 streamRef：dataUrl 内容块携带 mediaType、name、path、size、source="dataUrl"、data 和可选 hash；objectRef 内容块携带 mediaType、name、path、size、source="objectRef"、objectId 或临时 URL、过期时间和 hash；streamRef 内容块携带 mediaType、name、path、size、source="streamRef"、streamId、累计字节上限和 hash。文件适配器的输入为文件摘要、后端能力、模型能力和读取策略，输出为 contentBlocks 或结构化降级结果。

读取方式选择按模型支持和后端成本确定优先级：若模型支持 objectRef 且后端能够生成受控对象引用，则优先返回对象引用以避免编码膨胀；否则在原始大小不超过内联阈值且模型支持 dataUrl 时，使用 readFileBytes 读取字节并编码为数据 URL；若文件适合流式传入、模型支持 streamRef 且后端提供 readFileStream，则返回流式引用或按累计字节上限收集流；若上述方式都不可用，则返回 backend_capability_missing 或 transfer_mode_unsupported 降级。readFileBytes 失败时可尝试 readFileStream，readFileStream 中断或累计字节超过上限时丢弃已收集本体并返回 stream_limit 或 stream_read_failed；对象引用过期、生成失败或安全策略拒绝时，可按顺序尝试 dataUrl 或 streamRef，仍失败时保留文件摘要并降级。

多模态内容转换执行双阶段限制校验。第一阶段在读取本体前按 stat.size 或可取得的对象大小校验 maxRawBytesPerFile、maxFilesPerRequest 和 maxBlocksPerRequest，超过硬上限时立即返回 raw_size_limit 或 file_count_limit，不读取完整字节。第二阶段在封装前计算实际内容块体积：dataUrl 按 base64 后长度与 MIME 前缀计算，objectRef 按引用描述和安全令牌长度计算，streamRef 按引用描述和流累计上限计算；若编码膨胀导致 content block 超过 maxEncodedBytesPerBlock，则从 dataUrl 回退到 objectRef 或 streamRef，模型不支持回退方式时返回 encoded_size_limit 降级。该机制由前置大小校验、流式累计上限、封装后体积校验和内容块数量限制共同防止二进制内容无界进入模型上下文。

图片、PDF 和其他多模态文件分别适用独立边界。图片文件需同时满足 MIME 位于 supportedMimeTypes、像素尺寸或字节大小不超过 imageLimits、编码后体积不超过内容块限制；格式受支持但尺寸或编码体积超限时返回 image_limit 或 encoded_size_limit。PDF 文件需同时满足 MIME 支持、页数或原始大小不超过 pdfLimits；当模型只支持图片不支持 PDF 时，PDF 不被直接传递，除非后端或外部适配器已经生成符合图片限制的页级衍生文件；当 allowPartialPdfPages 开启且请求指定页范围时，可仅对页级对象执行同样的大小和数量校验，超出页范围或页数仍过多时返回 pdf_limit。未知二进制文件即使大小较小，也只有在 MIME 或魔数落入模型能力表时才进入内容转换。

### 不可直接消费文件的结构化降级

结构化降级用于文件本体不能或不应进入模型上下文但仍可向 agent 提供可靠文件事实的场景；结构化错误用于路径不存在、目录读取、参数非法或 stat 阶段失败等无法形成正常内容的场景。降级摘要保留已取得的信息，不伪造 textLines 或 contentBlocks，其字段包括 path、name、type、mime、extension、size、createdAt、updatedAt、binary、target、detection、capabilityUsed、degradeReason、errorStage 和 suggestedActions。errorStage 按 stat、detect、classify、read_bytes、read_stream、object_ref、encode、package、consistency 标记失败阶段，使后续处理能够区分是元信息、检测、读取、引用生成还是封装失败。

degradeReason 包括 not_found、directory、permission_denied、restricted_path、special_file、backend_capability_missing、detect_failed、binary_risk、mime_unsupported、transfer_mode_unsupported、raw_size_limit、encoded_size_limit、file_count_limit、stream_limit、object_ref_failed、package_failed、file_changed 和 unsafe_mime。不同原因映射到不同建议动作：raw_size_limit 或 encoded_size_limit 建议分页、缩小页范围、生成缩略图或外部转换；mime_unsupported、unsafe_mime 或 binary_risk 建议仅展示元信息、执行哈希或使用专用转换器；backend_capability_missing 建议切换具备字节读取或流式读取的后端；permission_denied、restricted_path 或 special_file 建议检查访问策略；file_changed 建议重新 stat 后重试；detect_failed 建议退回扩展名和 MIME 判定，仍冲突时只返回摘要。

在多文件读取场景下，读取请求按用户指定顺序形成候选队列，并为每个候选执行相同分类流程。已满足 maxFilesPerRequest 或 maxBlocksPerRequest 后，后续仍可执行 stat 和 detect 以生成摘要，但不再读取本体，degradeReason 记为 file_count_limit。若需要在数量受限时提高有效信息量，可在用户未指定顺序时按文本优先、模型支持 MIME 优先、小文件优先的顺序选择直传对象；被挤出的文件保留摘要和建议动作。降级结果不得覆盖已经成功生成的文本行或内容块；多模态封装失败时也应保留 stat、detect、hash 或版本校验阶段已经获得的摘要信息。

### 多后端兼容与能力协商

能力协商以模型能力和后端能力两张表为输入。后端能力表记录 stat、readFile、readFileBytes、readFileStream、detectFile、hashFile、objectRef、能力版本和最大流式读取字节数；模型能力表记录支持 MIME、传输方式、大小限制和文件数量限制。读取工具在初始化时缓存能力表，在每次读取前校验能力版本；模型切换、代理重连、对象存储配置变化或能力版本不一致时重新协商。若能力为空或协商失败，仅保留 stat/readFile 能力所能支撑的文本读取；已识别为二进制或多模态候选但缺少本体读取能力时，返回 backend_capability_missing 降级。

能力矩阵按照可用方法启用分支：仅有 stat 和 readFile 时启用文本读取和受限降级；增加 detectFile 或有限字节采样时启用更可靠的文本/二进制分类；增加 readFileBytes 时启用 dataUrl 或字节封装；增加 readFileStream 且模型支持 streamRef 时启用流式引用或受限流收集；增加 objectRef 且模型支持 objectRef 时启用对象引用；增加 hashFile、etag 或版本号时启用读取前后一致性校验。完整 Workspace 可启用文本、字节、流式、检测和对象存储相关分支；共享 Workspace 代理通过远端转发能力表启用相同分支；自定义后端只启用其声明能力对应的分支，未声明能力不被推断。

stat 后到 read 之间的文件变化通过一致性字段处理。系统在 stat 阶段记录 size、mtime、etag、hash 或版本号中的可用项，读取本体后再次获取其中至少一项进行比较；若一致，则结果标记 consistency="stable"；若不一致且重试次数未超过上限，则重新执行分类和读取决策；重试后仍变化，返回 file_changed 降级，且不输出可能错位的 textLines 或内容块。对象引用模式还记录引用生成时间和过期时间，模型调用前发现过期时重新生成引用；重新生成失败时按 object_ref_failed 降级。
