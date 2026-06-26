## 技术方案

### 总体构思与层次划分

本方案解决的技术问题是：在不改变既有 Workspace 文件系统语义、工具命名方式和文本读取体验的前提下，如何对图片、PDF、普通二进制、大文件以及模型能力差异进行统一判定，并生成可被模型安全消费的读取结果。为此，本方案在持久化 Workspace 与 agent 工具调用之间设置文件读取结果规范化层，使读取请求先经过类型识别、能力探测和预算检查，再决定输出文本内容、媒体片段或元信息降级结果。

规范化层包括读取决策器、内容装载器、结果描述器和模型消息适配器。读取决策器负责判断目标路径是否存在、是否为目录、实际 MIME 类型和文件大小；内容装载器分别调用文本读取、字节读取或流式读取能力；结果描述器生成包含路径、名称、类型、大小、更新时间、读取方式、截断状态和限制原因的统一结果；模型消息适配器将统一结果转换为具体 LLM SDK 可接受的文本片段、图片片段、PDF/文件片段或降级说明。

本方案中，FileInfo 或 FileStat 统一称为文件元信息，表示由 Workspace 或兼容后端返回的路径、名称、条目类型、MIME 类型、大小、时间戳和可选存储后端；contentParts 表示规范化层内部生成的文本片段或媒体片段；model parts 表示模型消息适配器按具体 LLM SDK 格式输出的消息内容。Workspace 后端只负责提供 stat、readFile、readFileBytes、readFileStream 等文件能力，规范化层负责判断和组装 WorkspaceReadResult，模型消息适配器负责把 WorkspaceReadResult 转换为模型输入，三者职责相互隔离。

通过上述层次划分，原有文本文件仍沿用带行号、分页和长行截断的读取体验，图片、PDF 等可被多模态模型消费的文件则在安全边界内以媒体内容形式传递给模型；普通二进制、大文件、目录或未知类型文件不会被强行解码，而是向模型返回足够的文件元信息和不可直接读取的原因，使模型能够据此选择列目录、读取相邻文本、要求用户转换文件或采用其他工具处理。

### 文件类型识别与读取决策

读取决策按照固定优先级执行：先对请求路径进行规范化，拒绝空路径、超长路径和包含越权含义的路径；再进行命名空间或权限检查，确保请求不能越出当前 Workspace；随后解析符号链接并记录 requestedPath 与 resolvedPath，若触发循环深度限制则返回 ELOOP 类错误；之后调用 stat 获取文件元信息，并拦截不存在、目录和不支持的特殊条目；在文件存在且可访问时，继续执行 MIME 检测、后端能力检查、模型能力检查和预算检查，最后选择 text、multimodal 或 metadata-only 读取模式。

文件类型判断生成 detectedMime、declaredMime、extensionMime 和 mimeConfidence。declaredMime 来自文件元信息，extensionMime 来自文件扩展名映射，detectedMime 来自文件头或少量安全采样字节。裁决时，明确的 magic bytes 结果优先于扩展名和声明类型；若 magic bytes 与 declaredMime 冲突，则以 detectedMime 作为实际处理类型，并将 mimeConfidence 标记为 high 或 suspicious；若仅扩展名与声明类型冲突且无法采样，则优先采用声明类型但记录 conflict；若声明为 text/plain 但采样中存在大量 NUL 字节或不可接受的控制字节，则标记为 suspicious_binary 并禁止进入文本路径；若无法确定类型，则进入 metadata-only 路径，reason 记录 unknown_mime。

类型与能力发生竞争时，系统按“原生支持优先、转换支持次之、元信息降级兜底”的顺序处理。例如文件扩展名为 .png 但 magic bytes 表示 PDF 时，系统按 PDF 处理，并在 metadata 中保留扩展名冲突；若目标模型原生支持 PDF，则生成 PDF 文件片段；若模型不支持 PDF 但配置有页渲染服务且页数预算允许，则转换为页面图片片段；若二者均不满足，则返回 metadata-only。用户可通过显式配置允许较低置信度 MIME 进入特定读取路径，但该配置不会绕过大小、权限和模型能力预算。

预算检查按可执行顺序进行：第一步用 stat.size 比较单文件字节上限，超过硬上限时不读取正文或字节；第二步根据目标模型能力计算可用媒体片段数、允许 MIME 集合和剩余总输入预算；第三步为文本路径分配最大行数、最大单行长度和字符预算，为多模态路径分配最大媒体字节数、最大页数或帧数；第四步在多个限制同时触发时按安全优先级处理，即权限和类型限制优先于能力限制，能力限制优先于字节预算，字节预算优先于文本行数截断。预算拒绝会产生 metadata-only，文本行数或长行超限则允许产生 partial 文本结果。

### 统一读取结果与模型输入转换

规范化层输出统一的 WorkspaceReadResult。status 取值包括 ok、partial、not_readable 和 error：ok 表示选定模式完整成功；partial 表示文本被分页或截断、多个片段中部分成功，或只取得可用元信息；not_readable 表示文件存在但因类型、能力或预算不适合直接读取；error 表示路径、权限、后端或适配过程发生不可恢复错误。mode 取值为 text、multimodal 或 metadata-only；mode 为 text 时 contentParts 至少包含一个 text part，mode 为 multimodal 时 contentParts 至少包含一个完整 media part，mode 为 metadata-only 时 contentParts 为空或仅包含说明性 text part。

metadata 为必填对象，至少包含 requestedPath、resolvedPath、entryType、name、size、declaredMime、detectedMime、extensionMime、mimeConfidence、createdAt、updatedAt、statTime 和 readTime；当后端能够提供时，还包含 storageBackend、etag 或 version、symlinkDepth、targetPath 和 namespace。reason 用于记录当前结果的主原因，例如 unsupported_mime、budget_exceeded、model_capability_missing 或 decode_failed；limits 用于记录被触发的具体阈值和实测值，例如 maxBytes、actualBytes、maxLines、actualLines、maxMediaParts、allowedMimeTypes。reason 说明为什么选择该状态，limits 说明哪些量化边界被触发，二者不相互替代。

文本路径中，系统优先按 UTF-8 解码，并在开头存在 UTF-8 BOM 时去除 BOM；后端已经提供字符串时直接进入行处理，后端只提供字节时先按 UTF-8 解码。若解码出现非法字节，系统根据配置选择替换为 U+FFFD 后返回 partial，或拒绝文本路径并降级为 metadata-only；若采样发现 NUL 字节比例超过阈值，则认定为混合二进制而不进入文本路径。offset 采用 1 起算，缺省为 1；limit 缺省为配置上限，0 或负值被规范化为参数错误；offset 超出总行数时返回空 content 但保留 totalLines；文件末尾无换行仍作为最后一行计数。

文本内容按行切分后生成带行号文本块，行号对应原文件实际行号。超过最大单行长度的行被截为前缀内容并追加固定截断标记；超过最大行数或字符预算时，仅返回预算内行区间，并在 truncated 中记录 byLineLimit、byLineLength 或 byTokenBudget。limits 同时记录请求的 offset、limit、实际返回 fromLine/toLine、totalLines、maxLineLength 和被截断行号集合，使模型能够判断缺失内容位置并按需再次读取。

多模态路径中，只有在文件完整装载且装载后 MIME 校验仍与允许类型一致时，系统才生成 image、pdf 或 file 类 media part。系统优先使用 readFileBytes 获取完整字节；当文件接近上限且后端提供 readFileStream 时，按固定块大小逐块读取，每次加入累计缓冲前检查加入后是否超过预算，超过时取消 reader、关闭流并返回 metadata-only 或 partial metadata，不把截断字节作为有效媒体片段。读取完成后比较实际字节数、stat.size 和可选哈希或 etag；若大小变化、校验失败或 MIME 复核失败，则不生成媒体片段并记录 integrity_mismatch 或 mime_changed。

PDF、GIF 和其他多页或多帧媒体按照独立预算处理。PDF 读取前可通过文件头或轻量解析获得是否加密、页数或近似页数；加密 PDF、页数超过预算或需要但未配置渲染服务时，返回 metadata-only，并将 reason 分别标记为 encrypted_pdf、page_budget_exceeded 或 renderer_missing。GIF 可配置为仅传首帧、抽样帧或完整文件；若模型不支持动图或帧数超限，则优先生成受预算约束的帧图片片段，无法安全抽帧时返回 metadata-only。

模型消息适配器按 contentParts 类型生成 model parts：text part 转换为包含 type、text、sourcePath 和 lineRange 的文本消息；image part 转换为包含 type=image、mediaType、data 或引用句柄、sourcePath 和 byteLength 的图片消息；pdf/file part 转换为包含 type=file、mediaType、filename、data 或引用句柄、sourcePath 和 byteLength 的文件消息；metadata-only 结果转换为结构化文本说明，包含 metadata、reason、limits 和 actionHints。若目标 SDK 只接受字符串，所有 contentParts 被序列化为结构化文本；若 SDK 支持内容数组，则文本说明与媒体 part 一并放入同一条消息。

模型消息适配失败不覆盖已经形成的 WorkspaceReadResult。若读取成功但目标 SDK 不支持对应 part、base64 长度超过 SDK 限制或引用句柄生成失败，系统将状态调整为 partial 或 not_readable，保留原始 metadata、成功的文本 part 和读取完成标记，并将 reason 标记为 adapter_unsupported 或 adapter_budget_exceeded。这样可以把“文件已读到”和“无法交给当前模型”两个状态区分开，避免丢失路径、大小和类型等诊断信息。

### 限制控制、降级与错误反馈

限制控制采用“先 stat 预判、再能力预算、后装载校验”的计算过程。stat 阶段根据 size、entryType 和 MIME 置信度排除明显不可读对象；能力预算阶段根据模型配置得到 allowedMimeTypes、maxInputBytes、maxMediaParts、maxPdfPages、maxGifFrames、maxTextLines 和 maxLineLength；装载阶段按文本或多模态路径扣减预算。文本 part 主要占用上下文预算，媒体 part 同时占用媒体数量预算和字节预算；当剩余预算不足以容纳完整媒体文件时，不生成媒体 part，而是返回 metadata-only 或保留已成功的其他 part。

partial 状态的边界按内容可用性确定。文本分页、长行截断或字符预算截断属于 partial，允许保留已返回的 text part；批量读取多个文件或一个容器内多个可独立媒体片段时，部分成功也属于 partial，failedParts 记录失败的路径、页码、帧号或原因；单个不可分割图片、PDF 文件或普通文件若未完整读取，不得作为有效 media part 输出，只能返回 partial metadata 或 metadata-only；后端在已返回若干独立片段后中途失败时，保留成功片段并记录 backend_interrupted。

降级结果由 metadata、reason、limits 和 actionHints 组成。actionHints 根据主原因生成：目录路径建议调用列表工具；unknown_mime 建议查看扩展名、同名说明文件或用户确认类型；budget_exceeded 建议压缩、拆页、降低分辨率或分段读取；model_capability_missing 建议切换支持相应 MIME 的模型或启用转换服务；decode_failed 建议以二进制方式下载或转换编码。上述建议只作为结构化字段返回，不替代底层错误码和限制记录。

错误码与状态映射保持稳定：ENOENT 对应 status=error、mode=metadata-only、reason=file_not_found；EISDIR 对应 not_readable 和 reason=is_directory；权限或命名空间失败对应 error 和 reason=permission_denied；符号链接循环对应 error 和 reason=symlink_loop；Workspace 元数据存在但 R2 对象缺失对应 error 或 not_readable，reason=object_missing，并标记可重试；unknown_mime、model_capability_missing 和 budget_exceeded 通常对应 not_readable；文本解码失败可按配置对应 partial 或 not_readable；消息适配失败对应 partial 或 not_readable，并保留已生成的 WorkspaceReadResult。

针对 stat 与实际读取之间的变化，系统记录 statTime 与 readTime，并在后端提供 version、etag 或修改时间时进行二次比较。若 stat 后文件被删除，则以读取阶段的 ENOENT 为准；若文件被替换导致 MIME、大小或版本变化，则重新执行 MIME 和预算判定；若实际读取字节数超过 stat.size 或超过预算，立即中止并返回 size_changed 或 budget_exceeded；若权限在两步之间变化，则返回 permission_denied。旧 stat 信息只作为诊断元信息保留，不覆盖读取阶段得到的实际错误和实际大小。

流式读取在资源释放和完整性方面采用保守规则。每个块在写入累计缓冲或提交转换服务前先计算加入后的累计字节数，超过预算时不接收该块的部分内容，而是取消读取器并关闭流；流异常时记录已读字节数、最后成功块序号和异常原因。若目标转换服务明确支持分片输入，可以把分片作为独立 part 处理，否则单个文件必须完整读取后才允许生成媒体 part。

### 多后端兼容与工具接入

为兼容真实 Workspace、SharedWorkspace 代理和自定义文件后端，本方案将读取能力声明为 capability descriptor。工具工厂创建读取工具时检查 stat、readFile、readFileBytes、readFileStream 的存在性和函数签名，生成 supportsStat、supportsText、supportsBytes、supportsStream、supportsMetadata 和 maxKnownReadSize 等能力字段；descriptor 可按会话缓存，并在代理调用失败、方法缺失或后端返回 unsupported 时失效重建。基础后端只需提供 stat 与 readFile 即可继续支持文本读取，具备 readFileBytes 或 readFileStream 的后端则逐级启用媒体装载和流式装载。

在 Think agent 场景中，现有 workspace read 工具可以被多模态读取工具包裹，write、edit、list、find、grep、delete 工具保持原语义。对于 codemode 的 state.* 文件系统调用，本方案不改变 sandbox 中 readFile 返回字符串、readFileBytes 返回字节的语义；动态代码仍按普通文件系统 API 工作，WorkspaceReadResult 到 model parts 的转换仅发生在 agent 工具层或模型消息组装层。若包裹工具检测到后端只支持文本能力，则自动退回现有带行号读取路径。

对于父级目录持有真实 Workspace、子级会话通过 SharedWorkspace 转发调用的场景，规范化层只依赖代理暴露的 stat、readFile、readFileBytes 等方法，因此无需关心文件实际位于当前会话、父级 Durable Object、SQLite 内联行还是 R2 对象中。若代理调用 readFileBytes 或 readFileStream 失败但 stat 与 readFile 可用，系统回退到文本或 metadata-only 路径；若代理返回的元信息与读取结果不一致，则按 stat 后变化规则重新判定。对于自定义后端，开发者只需按 capability descriptor 提供对应能力，缺失能力会被记录为 capability_missing 并触发降级。
