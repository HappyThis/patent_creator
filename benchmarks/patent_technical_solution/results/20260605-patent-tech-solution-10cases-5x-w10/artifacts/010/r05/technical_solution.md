## 技术方案

本技术方案针对 agent workspace 中非纯文本文件（如图片、PDF、音视频等）的多模态读取需求，提出一套在现有 Workspace 持久化虚拟文件系统基础上扩展的多模态文件读取体系。方案保留现有文本读取的行号、分页、截断和长内容控制体验，新增文件类型智能识别、多模态内容转换、读取限制控制以及多后端兼容等机制，使得当工作区包含可被多模态模型消费的文件时，系统能够自动将合适的内容传递给模型，对不适合直接传递的文件则返回结构化文件信息和限制原因。

### 1. 多模态文件类型识别与分类机制

在现有 Workspace 基于扩展名和首 512 字节零值/控制字符检测的二进制判断机制（detectFile）基础上，构建三层递进式文件类型识别体系，以准确区分可被多模态模型直接消费的文件与不可直接传递的文件。

第一层为扩展名映射表（MIME_BY_EXTENSION），在现有 js/ts/json/html/css/md/txt/png/jpg/gif/svg/tar/gz 基础上，扩展覆盖 PDF（application/pdf）、WebP（image/webp）、BMP（image/bmp）、TIFF（image/tiff）、MP4（video/mp4）、MP3（audio/mpeg）、WAV（audio/wav）、ZIP（application/zip）等常见多模态和容器格式。

第二层为魔数签名检测，在现有 isLikelyText 的前 512 字节扫描基础上，增加对常见二进制文件格式魔数（magic bytes）的识别：PNG 的 89 50 4E 47、JPEG 的 FF D8 FF、PDF 的 25 50 44 46、GIF 的 47 49 46、WebP 的 52 49 46 46、ZIP/DOCX 的 50 4B 03 04 等。魔数检测在扩展名缺失或不准确时提供兜底判断，优先级高于扩展名推断。

第三层为多模态可消费性分类。在上述检测结果基础上，将文件归入三类：可直接传递类（图片类 image/png、image/jpeg、image/gif、image/webp 等，其字节内容可通过 base64 编码为 data URL 直接嵌入模型请求）；可提取文本类（PDF application/pdf，可通过文本提取管线将页面内容转为文本后传递）；不可传递类（音视频、二进制可执行文件、加密文件等），返回结构化文件信息并附限制原因。分类依据一组可配置规则表，支持按 MIME 主类型、子类型和扩展名组合决策。

### 2. 多模态内容输出转换层

在 Workspace 现有 readFile / readFileBytes / readFileStream 三条读取路径基础上，新增统一的 readFileForModel 方法，作为面向多模态模型的内容输出转换层。该方法接收读取模式参数，根据文件类型和模型能力返回不同形态的结果。

对于可直接传递的图片类文件，readFileForModel 在内部通过 readFileBytes 获取原始字节后，自动构造符合数据 URI 规范的 base64 编码字符串（data:{mime};base64,{base64Content}）。该 data URL 可直接作为多模态模型请求中的 image_url 内容块，无需模型或上层调用者自行编码。构造过程中根据模型上下文窗口大小和配置的图片尺寸上限，检查 base64 编码后长度是否超过限制；超限时自动降级为结构化信息返回并注明原因。

对于可提取文本的 PDF 类文件，readFileForModel 触发文本提取管线：先从 R2 或 SQLite 内联存储中读取原始 PDF 字节，再通过 PDF 解析组件提取文本内容，按页组织并以结构化形式返回（包含页码、页内文本、总页数等元信息）。PDF 文本提取过程受配置的超时和页数上限约束，超出限制时返回已提取部分并附带截断标记和原因说明。

对于不可传递类文件以及超限降级的文件，readFileForModel 返回结构化文件信息（StructuredFileInfo），包含：文件路径、MIME 类型、文件大小、存储后端（inline/R2）、是否为二进制、最后修改时间、可消费性分类结果、以及限制原因的可读描述。该结构确保模型在无法消费原始内容时仍能获取文件元信息并据此决策后续操作。

当 readFileForModel 检测到目标文件为纯文本（MIME 为 text/* 或经 isLikelyText 判定为非二进制）时，自动回退到现有的 readFile 路径，保持行号、分页（start_line/limit）、截断标记（truncated）和长内容控制（MAX_DIFF_LINES 等）的完整文本读取体验。文本读取结果包装为与多模态结果统一的外部结构，以便上层调用者以一致的方式处理所有 readFileForModel 返回值。

### 3. 多级读取限制与降级控制

多模态文件通常体积较大，需要在现有 Workspace 的 inlineThreshold（1.5MB SQLite 行上限）和 MAX_STREAM_SIZE（100MB 流式读取上限）基础上，引入面向多模态消费场景的多级限制控制机制。

第一级为文件大小阈值。设置可配置的多模态直接传递最大字节数（如 20MB），文件大小在该阈值以下时允许进行 base64 编码和直接传递；超过该阈值时，无论文件类型如何，一律返回结构化文件信息并注明大小超限。该阈值独立于 Workspace 的 inlineThreshold，因为即使是存储在 R2 中的大文件，其 base64 编码后体积膨胀约 33%，直接传递可能超出模型上下文窗口。

第二级为编码后长度检查和截断。对于图片类文件，在构造 data URL 前预计算 base64 编码后的字符串长度。若超过配置的编码长度上限（如 30MB 字符串），在编码前即降级为结构化信息返回，避免不必要的内存分配和编码开销。对于 PDF 类文件，提取过程受页数上限和单页字符数上限约束，超出部分截断并标记。

第三级为存储后端的读取策略差异。对于存储在 R2 中的大文件，readFileForModel 利用 R2 对象的 Range 请求实现按需读取：文件类型识别阶段仅读取文件首部足够魔数检测和 isLikelyText 判断的字节数（如 4KB），避免将整个大文件加载到内存；确认为可传递类型后，再通过流式方式逐步读取和编码。对于 SQLite 内联存储的文件，由于已在一次查询中获取完整内容，直接进行编码转换。

### 4. 多后端兼容架构

本方案的多模态读取能力通过扩展现有 FileSystem 接口和 StateBackend 抽象层实现，保证对真实 Workspace（DO SQLite + R2）、共享 Workspace（跨 DO RPC 代理）和自定义文件后端（InMemoryFs 或第三方 FileSystem 实现）的统一兼容。

在现有 FileSystem 接口（定义于 fs/interface.ts）中新增 readFileForModel 方法签名，该方法接受 ReadMode 参数（枚举值：auto / text_only / multimodal_preferred）和 ReadLimit 参数（maxFileSize、maxEncodedLength、maxPdfPages 等可选限制覆盖），返回统一的 ReadResult 类型（可区分 text 分支、image_data_url 分支、pdf_text 分支和 structured_info 分支）。所有实现 FileSystem 接口的后端均需提供该方法。

WorkspaceFileSystem（workspace.ts 中的 FileSystem 适配器）实现 readFileForModel 时，内部委派给 Workspace 的原生 readFileBytes 获取字节、detectFile 获取类型判定，然后执行内容转换逻辑。当 Workspace 配置了 R2 且文件存储在 R2 中时，使用 R2 Range 读取首部字节进行类型判定，避免全量下载。WorkspaceFsLike 接口同步扩展，确保跨 DO RPC 代理场景下共享 Workspace 的代理对象也能通过 RPC 转发 readFileForModel 调用。

InMemoryFs（shell/src/fs/in-memory-fs.ts）提供 readFileForModel 的纯内存实现，直接对内存中的 Uint8Array 执行魔数检测和内容转换，适用于测试和轻量级场景。自定义 FileSystem 后端只需实现接口中定义的 readFileForModel 方法，复用的检测与转换逻辑通过共享工具函数（如 detectFile、buildDataUrl、extractPdfText）调用，无需各后端重复实现。

### 5. 模型工具接口与 state 扩展

在现有 stateTools / StateBackend 体系内，新增 state.readFileForModel 方法，使多模态读取能力通过 codemode 沙箱或直接工具调用对模型暴露。该方法的参数设计充分考虑模型的使用习惯和自动决策需求。

readFileForModel 方法签名设计为：readFileForModel(path: string, options?: { mode?: 'auto' | 'text' | 'multimodal', maxFileSize?: number, maxEncodedLength?: number }): Promise&lt;ReadResult&gt;。mode 默认值为 'auto'，由系统根据文件类型自动选择最佳传递方式；'text' 强制按纯文本处理（保持现有行为）；'multimodal' 优先尝试多模态编码。ReadResult 为区分联合类型：text 分支包含 content、startLine、endLine、totalLines、truncated 等字段保持与现有文本读取一致；image 分支包含 dataUrl、mimeType、sizeBytes；structured 分支包含 path、mimeType、size、storage、classification、limitationReason 等结构化信息。

在 STATE_METHOD_NAMES 数组中注册 'readFileForModel'，由 createStateToolProvider 自动生成对应的 ToolProvider 条目。STATE_TYPES 类型声明中同步追加 ReadMode、ReadResult 等相关类型定义。同时扩展 state.detectFile 方法，使其返回更丰富的分类信息（classification: 'direct' | 'extractable' | 'unsupported' 和 limitationReason 字段），使模型可以在调用 readFileForModel 前先通过 detectFile 预判文件可消费性，避免无效读取。

### 6. 整体处理流程

综合上述各组件，一次完整的多模态文件读取处理流程如下，各步骤的协同确保类型判定、内容转换、限制检查和结果输出的正确衔接：

1. 路径解析与存在性检查：通过 Workspace 的 stat 方法确认目标路径存在且为文件类型。若不存在则抛出 ENOENT；若为目录则抛出 EISDIR。
2. 文件元信息获取：查询 SQLite 中的 mime_type、size、storage_backend、content_encoding 等字段，获取文件大小，判断是否超过多模态直接传递大小上限。超限则直接返回结构化信息。
3. 文件类型深度识别：通过 R2 Range 读取（R2 存储）或 SQLite content 字段（内联存储）获取文件首部字节（至少 4KB），依次执行魔数签名匹配、扩展名推断和 isLikelyText 判定，确定文件的真实 MIME 类型和可消费性分类。
4. 模式决策：结合 ReadMode 参数（auto/text/multimodal）和上一步的分类结果，决定最终的内容传递方式。text 模式或文本文件走现有 readFile 路径；multimodal 模式或自动判定为图片/PDF 类型走对应转换路径。
5. 内容转换：图片类文件通过 readFileBytes 获取完整字节后构造 base64 data URL；PDF 类文件通过 PDF 解析组件提取文本并以分页结构组织；不可传递类直接生成结构化文件信息。转换过程中实时检查编码后大小，超限时截断并标记。
6. 结果封装与返回：将转换后的内容或结构化信息封装为统一的 ReadResult 联合类型，返回给模型或上层调用者。结果中附带完整的元信息（路径、MIME、大小、存储后端、分类、截断状态、限制原因等）。

### 7. 异常处理与边界情形

本方案在现有 Workspace 异常体系（ENOENT、EISDIR、ELOOP、ENAMETOOLONG 等 POSIX 风格错误）基础上，针对多模态读取场景补充以下异常与边界处理机制：

R2 不可用时的降级：当文件标记为 R2 存储但 R2 bucket 未配置或不可达时，readFileForModel 不抛出异常，而是返回结构化信息并注明 'R2 backend unavailable' 限制原因。SQLite 内联大文件超限：当文件以 inline 方式存储在 SQLite 中但大小接近或超过 2MB 行限制时，读取仍可成功（Workspace 在写入时已做 base64 编码），但返回结果附带警告标记。编码失败处理：当 PDF 文本提取因文件损坏、加密或格式不支持而失败时，返回结构化信息并注明具体失败原因（如 'PDF encrypted'、'PDF malformed'），不抛出未捕获异常。

符号链接处理：readFileForModel 复用现有 resolveSymlink 机制（最多 40 层递归），在类型识别和内容读取前完成符号链接解析，确保指向图片或 PDF 的符号链接能够被正确识别和传递。解析目标为目录时抛出 EISDIR，解析链路形成环时抛出 ELOOP。命名空间隔离：多模态文件读取与现有 Workspace 命名空间机制完全兼容，不同 namespace 下的同名文件独立进行类型判定和内容转换，不会互相干扰。
