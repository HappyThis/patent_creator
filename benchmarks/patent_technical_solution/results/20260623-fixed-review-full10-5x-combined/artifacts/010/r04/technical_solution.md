## 技术方案

### 总体构思与处理链路

本方案针对 workspace 读取能力从纯文本扩展到多模态文件时的三个具体问题：其一，图片、PDF 或其他二进制文件若沿用 $readFile(path)$ 文本通道，容易被误解码为乱码并误导模型；其二，不同目标模型对 image、file 或附件片段的支持能力不同，文件内容需要在传递前完成能力匹配；其三，真实 workspace、共享 workspace、状态后端或自定义后端提供的二进制读取能力不一致，失败原因若不结构化会使 agent 难以恢复。为此，本方案建立“读取工具层—消息组装/模型转换层—后端适配层”三层协同机制。

一次读取请求的前置输入包括 $path$、文本分页参数、目标模型能力描述 $targetModelCapabilities$、限制配置 $readLimits$、后端能力集合 $backendCapabilities$ 以及调用上下文 $requestContext$。后端能力集合由适配层在工具初始化或首次调用时探测，并在调用失败、后端实例变化或缓存过期时重新探测；目标模型能力和限制配置由发起模型调用的运行环境提供。读取工具层只负责根据这些输入生成中立的读取结果，不直接绑定某一模型供应商的消息格式。

读取工具层的输出统一为三类对象：$TextReadResult$、$MediaReadResult$ 和 $DegradedReadResult$。三类对象均包含 $resultKind$、$status$、$path$、$mimeType$、$size$、$detectedType$、$detectionWarnings$ 和 $limitReason$ 等字段；$TextReadResult$ 额外包含带行号的 $content$、$totalLines$、$fromLine$、$toLine$ 和截断原因；$MediaReadResult$ 额外包含 $mediaKind$、$contentTransferMode$、$dataRef$ 或受限内联数据、$contentId$、$byteRange$、$sourceBackend$、$conversionRequired$；$DegradedReadResult$ 额外包含不可传递原因、缺失能力、冲突信息和建议后续动作。

### 文件类型识别与读取策略选择

在读取请求到达后，工具首先执行路径规范化和文件状态查询，取得文件类型、MIME 类型、大小、创建或修改时间等元数据。若目标不存在，返回文件不存在信息；若目标为目录，返回目录不可作为普通文件读取的信息，并可提示调用列举工具查看目录内容。该前置判断避免在缺少文件或目标类型错误时继续触发文本解码或二进制读取。

1. S1，规范化 $path$ 并解析读取参数，得到文本分页范围、是否允许媒体读取、调用顺序标识和取消信号；若路径非法，直接生成 $DegradedReadResult$。
2. S2，调用 $stat(path)$ 取得文件存在性、目录类型、$mimeType$、$size$、$modifiedAt$、$createdAt$ 和后端来源；若不存在或为目录，终止读取并生成对应状态。
3. S3，执行类型检测，综合 MIME、扩展名和必要的前缀字节检测，输出 $detectedType$、$confidence$、$detectedBy$、$sniffedBytesLength$ 和 $detectionWarnings$。
4. S4，将 $detectedType$ 与 $targetModelCapabilities$、$readLimits$ 和 $backendCapabilities$ 匹配，确定进入文本分支、媒体分支、PDF 复合分支或降级分支。
5. S5，在执行实际读取后校验读取到的字节数或修改时间；若文件在 $stat$ 后被删除、变大、变小或权限变化，则更新 $status$ 为 $fileChanged$、$notFound$、$permissionDenied$ 或 $sizeExceeded$，不得沿用旧元数据生成媒体结果。
6. S6，按分支生成 $TextReadResult$、$MediaReadResult$ 或 $DegradedReadResult$，并保留检测、限制和异常字段供消息组装层使用。

类型识别采用内容特征优先、可信 MIME 次之、扩展名兜底的规则。当 MIME 缺失、为 $application/octet-stream$、与扩展名冲突，或文件扩展名属于可伪装风险较高的类型时，读取不超过配置 $sniffBytesLimit$ 的前缀字节进行检测。若前缀字节显示为图片、PDF 或明显二进制，则即使扩展名为文本也不得进入文本解码分支；若前缀检测失败但 MIME 来自可信后端元数据，则以 MIME 分类并记录 $detectedBy=mime$；若仅扩展名可用，则标记低置信度 $confidence=low$；若文本编码检测失败或二进制比例超过阈值，则归入未知二进制并生成 $DegradedReadResult$。冲突信息写入 $detectionWarnings$，包括冲突字段、采用依据和被忽略依据。

文本分支在分类结果为文本且解码置信度达到阈值时执行。默认按 UTF-8 解码，并先识别 UTF-8 BOM 或其他可配置 BOM；对 $\r\n$、$\n$、$\r$ 统一归一为行边界。空文件返回 $status=ok$ 且 $totalLines=0$；仅包含不可见字符的文件仍返回文本结果并标记 $contentWarning$；非法字节序列、NUL 字节比例过高、混合二进制片段或解码替换字符比例超过阈值时，不继续生成误导性文本，而降级为 $DegradedReadResult$。成功解码后沿用行号、$offset$、$limit$、最大行数和最大单行长度限制，超长单行、过多行和分页外内容分别记录 $truncationReason$，且已生成的文本结果不因后续媒体转换失败而被覆盖。

媒体分支的能力配置至少包括：目标模型支持的 MIME 集合 $supportedMimeTypes$、支持的 $mediaKind$ 集合、单文件最大字节数 $maxFileBytes$、单次工具调用最大媒体数 $maxMediaCount$、媒体总字节数 $maxAggregateBytes$、流式分块大小 $chunkSize$、读取超时 $readTimeoutMs$、取消信号、是否允许内联 $allowInlineData$、是否允许引用 $allowDataRef$。只有当分类结果属于支持集合、文件大小未超过单文件阈值、聚合限制仍有余量、且后端提供 $readFileBytes$ 或 $readFileStream$ 时，才读取实际字节；否则生成 $DegradedReadResult$ 并写明 $limitReason$。若内联数据超限但允许引用，则回退为 $dataRef$；若引用也不可用，则不得附带不完整内容。

### 多模态内容的模型输入转换

多模态内容不作为普通字符串拼接进工具结果。读取工具取得字节或数据流后，先生成中立的 $MediaReadResult$：$resultKind=media$，$status$ 可为 $ok$、$partial$、$timeout$、$conversionRequired$ 或 $backendError$，$contentTransferMode$ 表示 $inline$、$dataRef$ 或 $streamRef$，$contentId$ 或 $hash$ 用于内容一致性校验，$byteRange$ 表示已读取范围，$sourceBackend$ 表示 inline、对象存储或自定义后端来源。$dataRef$ 由后端适配层创建，包含有效期、访问权限和可解析的引用格式；消息组装层只能通过受控接口解析该引用，不能把引用暴露为任意文件路径。

工具层与消息组装层的边界保持固定：工具层只输出 $TextReadResult$、$MediaReadResult$ 或 $DegradedReadResult$；消息组装层依据 $targetModelCapabilities$ 将 $MediaReadResult$ 转为目标模型可接受的 image、file 或 attachment 输入片段。若目标模型不支持对应 $mediaKind$、$dataRef$ 解析失败、引用过期、权限不足或供应商 SDK 拒绝该片段，消息组装层保留原结构化结果，并追加 $notAttachedReason$，不得把二进制数据转写为普通文本，也不得返回空内容冒充已读取。文本结果保持原有 $content$、$totalLines$、$fromLine$、$toLine$ 等字段，以保证既有文本读取接口兼容。

PDF 作为典型复合文件进入专门分支。默认优先级为：目标模型支持 $application/pdf$ 原始文件输入且大小、页数和权限满足限制时，生成原始文件型 $MediaReadResult$；若模型不支持原始 PDF 但支持图片，且配置存在 PDF 渲染组件，则按请求页码或默认首页范围渲染为受限数量的页面图片，并记录页码、分辨率和渲染范围；若模型仅支持文本或渲染不可用，则调用文本抽取组件生成文本型结果或摘要；若 PDF 加密、损坏、页数超过上限、扫描件无法抽取文本、渲染失败或抽取失败，则生成 $DegradedReadResult$，分别标记 $encrypted$、$corrupt$、$pageLimitExceeded$、$scannedPdf$、$renderFailed$ 或 $extractFailed$。当已成功生成原始文件结果时，不再用低保真渲染结果覆盖；当仅得到部分页面时，$status=partial$ 且默认不传递给模型，除非调用上下文显式允许部分结果。

### 限制控制、结构化降级与后端兼容

限制控制分为读取前、读取中和结果级三个阶段。读取前依据 $stat$ 的 $size$、$mimeType$、目录类型、文件修改时间和 $readLimits$ 判断是否进入实际读取；读取中对 $readFileBytes$ 或 $readFileStream$ 设置 $maxBytes$、$chunkSize$、$readTimeoutMs$ 和取消信号，超过限制即停止读取；结果级校验 $maxMediaCount$、$maxAggregateBytes$、是否允许内联、是否允许 $dataRef$ 和消息组装层可解析性。若单文件超限，状态为 $sizeExceeded$；若聚合超限，按用户显式请求顺序优先，其次按模型支持度和文件大小保留结果，超出的文件生成 $DegradedReadResult$ 并记录 $aggregateLimitExceeded$。

异常恢复统一映射到结果状态而非未捕获错误。读取过程中若文件在 $stat$ 后被删除，返回 $status=notFound$；若修改时间或实际字节数与预判不一致，返回 $fileChanged$ 并重新检查阈值，超限则返回 $sizeExceeded$；若权限不足，返回 $permissionDenied$；若对象存储临时不可访问或后端调用失败，返回 $backendError$ 并保留后端来源；若流式读取超时或被取消，返回 $timeout$ 或 $cancelled$；若只读取到部分内容，返回 $partial$ 并默认不作为完整媒体传递；若 dataRef 创建失败或转换组件失败，返回 $referenceFailed$ 或 $conversionFailed$。已生成 $DegradedReadResult$ 后不得再附带不可访问的 $dataRef$ 或残缺内联数据。

多后端兼容通过能力探测和适配层实现。适配层面向抽象 workspace 或 workspace-like 对象，在运行时检测 $stat$、$readFile$、$readFileBytes$、$readFileStream$、$detectFile$、引用创建和权限校验能力，并将真实 workspace、共享 workspace、文件系统状态后端或自定义 SQL 后端统一映射为读取操作集合。能力探测结果带有来源、时间戳和可失效标记；当调用返回“方法不存在”、权限变化或对象存储配置缺失时，探测缓存失效并重新降级。只支持文本读取的后端仍可生成 $TextReadResult$，多模态路径生成缺失能力的 $DegradedReadResult$；支持二进制或流式能力的后端在满足限制条件后生成 $MediaReadResult$。

上述机制形成明确的技术效果推导链：检测为非文本或解码置信度不足时禁止进入 $readFile$ 文本解码分支，因此避免图片、PDF 和未知二进制被返回为乱码；媒体内容经 $MediaReadResult$ 和消息组装层转换为目标模型支持的 image、file 或 attachment 片段，因此模型能够获得可消费的多模态输入；读取前大小阈值、读取中 $maxBytes$、超时和取消控制、结果级聚合限制共同约束成本；能力探测和缺失能力降级使不同 workspace 后端在能力不一致时仍能返回可解释状态；文本分类成功后继续输出原有行号、分页和截断字段，因此多模态扩展不破坏既有文本读取兼容性。
