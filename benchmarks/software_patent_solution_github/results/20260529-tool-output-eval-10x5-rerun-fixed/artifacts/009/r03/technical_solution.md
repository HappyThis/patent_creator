## 技术方案

本方案针对 Think agent 的 workspace read 工具进行增强，使其能够识别并正确处理工作区中不同文件类型（文本、图片、PDF、普通二进制等），将模型可消费的多模态内容以合适的形式传递，对无法消费的文件返回结构化原因，同时保持现有文本读取体验（行号、分页、截断）不变。

### 整体架构

方案在现有 Workspace 与 Think 工具层之间引入一个文件读取策略层。该策略层基于 Workspace.stat() 返回的 mimeType 和文件大小，将文件划分为文本类、图片类、PDF 类和普通二进制类，分别走不同的读取和输出转换路径。整体架构自底向上为：Workspace 字节读写层（提供 readFileBytes / readFile / readFileStream / stat）→ 文件类型识别与策略分发层 → 输出转换层（将原始字节转换为模型可接收的内容块或结构化信息）→ 工具接口层。

### 文件类型识别机制

文件类型识别采用"元数据优先、内容特征兜底"的两级判断策略。

第一级：元数据判断。调用 Workspace.stat(path) 获取 FileInfo 结构，其中 mimeType 字段记录了文件写入时指定的或系统推断的媒体类型。系统将 mimeType 映射到内置的文件类别表：以 text/ 开头的归为文本类；image/png、image/jpeg、image/gif、image/webp、image/svg+xml 等归为图片类；application/pdf 归为 PDF 类；无法匹配任何已知类型的归为普通二进制类。

第二级：内容特征兜底。当 mimeType 为通用值（如 application/octet-stream 或缺失）时，系统读取文件的前 N 个字节（例如 512 字节），通过魔数（magic bytes）判断真实类型：PNG 以 89 50 4E 47 开头、JPEG 以 FF D8 FF 开头、PDF 以 25 50 44 46 开头、GIF 以 47 49 46 38 开头等。同时检查前若干字节是否全部落在 ASCII 可打印字符范围内，若是则倾向于判定为文本类。魔数检测结果不覆盖已有的明确 mimeType，仅作为兜底。

文件类型判定后，系统在内部产生一个类型标签（text / image / pdf / binary），后续读取策略和输出转换均基于此标签。类型检测逻辑封装为独立函数，接受 stat 结果和可选的字节头，不依赖文件扩展名。

### 读取策略与分发

Read 工具根据文件类型标签采用不同的读取路径。

文本类文件：沿用现有逻辑。调用 readFile() 获取字符串内容，按换行符拆分为行数组，支持 offset（1-based 起始行号）和 limit（读取行数）参数。每行附加行号并以制表符分隔。单行超过 2000 字符时截断并标注"(truncated)"；总行数超过 2000 行时只返回前 2000 行并附截断提示。输出中包含 path、content（带行号文本）、totalLines，以及分页时的 fromLine/toLine。

图片类文件：调用 readFileBytes() 获取原始字节数据，将字节转换为 Base64 编码字符串，拼接为 data URI 格式（data:{mimeType};base64,{encoded}），作为模型可接收的图片内容块返回。输出中包含 path、mediaType、size、encoding（"base64"）和 data（完整的 data URI）。支持将 data URI 包装为 AI SDK 的 content block 格式（type: "image", image: dataUri）。

PDF 类文件：调用 readFileBytes() 获取原始字节，转换为 Base64 编码，以 data:application/pdf;base64,... 的 data URI 形式返回。由于并非所有多模态模型均支持 PDF 内容直接消费，系统同时在输出中附加 size、pageCount（若可获取）等结构化元信息。对于不支持 PDF 的模型，系统可降级为返回文件元信息和一条说明（"PDF 文件，需要支持 PDF 的模型才能读取内容"）。

普通二进制文件：不读取文件内容，仅返回结构化元信息，包括 path、mediaType、size、type（"binary"）和一条 reason 字段说明（如"Binary file, content not displayed"）。这样既避免将不可消费的二进制数据污染模型上下文，又提供了可供模型推理的文件存在性和属性。

### 模型输出转换

输出转换层负责将不同文件类型的读取结果统一包装为模型可接收的格式，同时保留人类可读的结构化信息。

对于文本文件，输出格式与现有 read 工具完全兼容，为 JSON 对象 { path, content, totalLines, fromLine?, toLine? }。content 字段是带行号的文本字符串，模型可直接解析。

对于图片文件，输出为 JSON 对象 { path, type: "image", mediaType, size, dataUri }。其中 dataUri 是完整的 data:image/...;base64,... 字符串。若 AI SDK 支持 content block 数组返回，工具可将 dataUri 包装为 { type: "image", image: dataUri } 的内容块，放到 tool result 的 content 数组中，模型框架自动将其作为多模态输入传递给支持视觉的模型。

对于 PDF 文件，输出为 JSON 对象 { path, type: "pdf", mediaType: "application/pdf", size, dataUri, note?: string }。dataUri 为 data:application/pdf;base64,... 字符串。note 字段在模型可能不支持 PDF 消费时提示限制。

对于普通二进制文件，输出为 JSON 对象 { path, type: "binary", mediaType, size, reason: "Binary file cannot be displayed as text" }。不包含文件内容数据。

所有输出类型均包含 path、size 和 type 字段，保证模型在收到任何类型的结果时都能获得基本的文件元信息用于后续推理。错误情况（文件不存在、目录、读取失败）的返回格式与现有 read 工具保持一致，使用 error 字段。

### 大小限制控制

为避免过大的文件（尤其是图片和 PDF 转换为 Base64 后体积膨胀约 33%）直接塞入模型上下文导致 token 超限或性能问题，系统在读取路径中嵌入多层大小控制。

第一层：读取前检查。Read 工具在调用 readFileBytes 前，先通过 stat() 获取文件大小。若文件大小超过全局最大读取阈值（默认 10 MB），直接返回错误提示 { error: "File too large", path, size, maxSize }，不读取任何内容。

第二层：按类型分开限制。不同文件类型设置不同的最大尺寸：文本文件沿用现有 2000 行、单行 2000 字符、grep 单文件 1 MB 的限制；图片文件限制为 20 MB（Base64 编码后约 27 MB），大多数模型上下文窗口可接受；PDF 文件限制为 32 MB；普通二进制文件不读取内容，尺寸仅作为元信息展示。

第三层：Base64 编码后的二次检查。在将字节转换为 Base64 data URI 后，检查最终字符串长度。若超过模型上下文窗口的安全预留值（可配置，默认取模型上下文窗口大小的 30%），则降级为仅返回文件元信息加一条说明，不返回 data URI。这一层保护对图片和 PDF 均适用。

各阈值均为可配置参数：maxFileSize（全局最大）、maxImageSize、maxPdfSize、maxBase64ContextRatio。配置可通过 createReadTool 的选项参数传入，不同 agent 实例可根据其模型能力设定不同值。

### 多后端兼容机制

方案设计考虑了不同 workspace 后端的兼容性，不要求所有后端提供完全相同的底层能力，而是通过接口层的最小必要抽象来实现。

核心接口扩展：将现有 ReadOperations 接口从仅包含 readFile() 和 stat()，扩展为增加 readFileBytes() 方法。新接口定义为：

- readFile(path): Promise<string|null> — 文本读取（已有）
- stat(path): Promise<FileInfo|null> — 文件元数据（已有）
- readFileBytes(path): Promise<Uint8Array|null> — 字节级读取（新增）

readFileBytes 是方案对 workspace 后端的最小新增要求。在 Workspace 实现中，该方法已存在（packages/shell/src/filesystem.ts 中的 readFileBytes 方法），支持从 inline SQLite 和 R2 两种存储后端读取原始字节。对于其他自定义后端（如 SharedWorkspace 代理），只需实现相同的接口签名即可。

兜底策略：对于无法提供 readFileBytes 的旧版后端（即仅实现 WorkspaceLike 原有接口），Read 工具的增强逻辑自动降级：图片和 PDF 类型文件返回结构化元信息加 "Backend does not support binary read" 说明；文本类文件仍按现有路径正常工作。降级通过运行时能力检测实现——在 Read 工具初始化时检查 ops 对象是否具有 readFileBytes 方法，将能力标记存储在闭包中。

类型兼容：WorkspaceLike 类型定义扩展为同时包含 readFile 和 readFileBytes，但 readFileBytes 标记为可选。新代码中使用类型缩窄（Type Narrowing）在调用前检查方法存在性。WorkspaceFsLike（用于 codemode 集成）已包含 readFileBytes，无需额外修改。

### 异常与边界情况处理

系统对所有文件读取的边界情况提供结构化响应，避免模型收到不可解析的原始错误信息。

文件不存在（stat 返回 null）：返回 { error: "File not found: {path}" }，与现有行为一致。

路径为目录（stat.type === "directory"）：返回 { error: "{path} is a directory, not a file", path, type: "directory" }，与现有行为一致。

文件读取失败（readFile 或 readFileBytes 返回 null）：返回 { error: "Could not read file: {path}", path, size?, mediaType? }，附带已知的元信息辅助诊断。

文件过大（超过对应类型的最大阈值）：返回 { error: "File too large", path, size, maxSize, type }，不尝试读取。

后端不支持二进制读取（readFileBytes 不存在且文件为非文本类型）：返回 { error: "Binary read not supported by workspace backend", path, mediaType, size, type }。

Base64 超出上下文安全比例：返回 { path, type, mediaType, size, note: "File data exceeds safe context window ratio, only metadata returned" }，不包含 data URI。

空文件（size === 0）：对于文本返回空 content 和 totalLines: 0；对于图片/PDF 返回相应元信息和 size: 0。

所有错误响应均包含 path、type 和足够的上下文信息，使模型能够在收到错误后做出合理决策（如尝试其他文件、请求用户提供额外信息等），而不是在遇到非文本文件时得到意义不明的"二进制不可读"消息。

### 处理流程

Read 工具的完整执行流程如下：

1. 接收参数 { path, offset?, limit? }。对 path 进行规范化（标准化斜杠、解析相对路径）。
2. 调用 ops.stat(path) 获取 FileInfo。若 stat 返回 null，返回文件不存在错误。若 type === "directory"，返回目录错误。
3. 调用 detectFileType(stat, ops) 进行类型识别：以 stat.mimeType 为主，必要时通过 ops.readFileBytes 读取文件头魔数兜底。
4. 检查 size 是否超过对应类型阈值。若超过，返回文件过大错误。
5. 根据类型标签走分支处理：text 分支调用 ops.readFile() 按行处理；image 分支调用 ops.readFileBytes() 转 Base64 data URI；pdf 分支同 image 并附加兼容性说明；binary 分支仅返回元信息。
6. 对 image/pdf 分支，检查 Base64 data URI 长度是否超过上下文安全比例。若超过，降级为元信息返回。
7. 构造最终输出对象，包含 path、type、size 等通用字段和类型特定字段。
8. 返回结果给 AI SDK 工具调用框架。

整个流程中，stat() 调用是必经的入口，保证了即使后续读取失败，系统也能返回部分元信息（如 path、size、mediaType）供模型参考。类型检测在读取实际内容之前完成，避免对超大二进制文件的不必要读取。

### 技术效果

本方案的技术效果体现在以下方面：

提升模型对工作区文件的理解范围：从仅能读取文本文件扩展到可消费图片和 PDF 内容，使多模态模型能够直接查看工作区中的截图、设计稿、扫描文档等非文本资产。对于普通二进制文件，至少提供结构化的存在性和属性信息。

保持现有体验不变：文本文件的读取完全沿用现有路径（行号、分页、截断、offset/limit），不做任何破坏性修改。现有 WorkspaceLike 后端的兼容性通过可选接口和能力检测保证。

精确的文件类型判断：基于数据库存储的 mimeType 结合魔数检测，不依赖文件扩展名，避免扩展名伪装或缺失导致的误判。元数据优先确保已标记类型的文件零额外 I/O 开销。

安全的上下文控制：多层大小限制（全局阈值、分类型阈值、Base64 后二次检查）防止大文件撑爆模型上下文窗口，保护模型调用的稳定性和成本。

结构化错误信息：所有异常情况均返回带有关键字段的结构化 JSON，模型可基于 path、type、size、reason 等信息做出后续决策，而非遇到无意义的原始系统错误。

### 风险与待确认问题

本方案在实施中需要关注以下风险点和待确认问题：

- 魔数检测的字节读取开销：兜底检测需要读取文件头 512 字节。对于 R2 存储的大文件，这会产生一次额外的 R2 get 调用。建议在 stat 返回的 mimeType 已经明确为非通用值（非 application/octet-stream）时跳过魔数检测，仅在 mimeType 缺失或为通用类型时触发。
- AI SDK 的 content block 返回能力：当前 AI SDK（ai 包）的 tool execute 返回值是否支持返回 content 数组（含 image content block）需要确认具体的 SDK 版本。如果不支持，可降级为在 content 文本字段中嵌入 data URI 字符串，由模型自行解析。
- PDF 的多模态支持差异：不同模型对 PDF 的消费方式不同（有的接受 base64 data URI，有的要求 URL，有的不支持）。方案中的 data URI 方式是一种通用策略，具体效果取决于模型能力。
- 模型上下文窗口比例的安全值：maxBase64ContextRatio 的默认值（如 30%）需要根据主流模型的实际上下文窗口大小（如 128K、200K tokens）和 Base64 编码膨胀率进行校准。
- writeFileBytes / writeFileStream 的 mimeType 准确性：方案依赖写入时正确设置 mimeType。建议在 writeFile 和 writeFileBytes 工具的描述中提示模型在写入非文本文件时指定正确的 mimeType。
