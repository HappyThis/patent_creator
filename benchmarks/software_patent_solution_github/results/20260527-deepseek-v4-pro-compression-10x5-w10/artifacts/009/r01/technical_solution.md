## 技术方案

### 整体架构

当前 workspace read 工具（createReadTool）仅处理纯文本文件：通过 ReadOperations.readFile 读取字符串内容，按行分割、附加行号、按 MAX_LINES（2000）和 MAX_LINE_LENGTH（2000）截断后返回。当工作区中存在图片、PDF 等非纯文本文件时，该工具将无意义地尝试把二进制内容当作文本解析，导致乱码或错误。

本方案在现有架构基础上扩展 read 工具，使其具备文件类型感知能力。核心思路是：在读取文件前先判定其类型，根据类型选择对应的读取策略——文本文件保持现有行号分页行为不变；图片文件通过 readFileBytes 获取字节数据并编码为 base64，再转换为 AI SDK 多模态 content part；PDF 等不可直接传递的文件返回结构化信息（文件大小、MIME 类型、页数等）以及不可读取的原因说明；普通二进制文件返回类型提示和限制信息。

扩展涉及四个核心层：（1）文件类型识别层，综合扩展名映射、数据库中的 mimeType 字段和必要时 magic bytes 检测来确定文件类型；（2）读取策略分发层，根据类型选择文本读取、字节读取加编码或结构化拒绝；（3）模型输出转换层，将读取结果转换为 AI SDK 兼容的返回格式——文本文件保持现有 { path, content, totalLines, fromLine?, toLine? } 格式，图片文件输出 { path, mimeType, contentParts: [...] } 格式；（4）限制控制层，对文件大小、图片像素、总内容长度施加约束并给出明确截断信息。所有扩展通过扩展 ReadOperations 接口实现，保持 WorkspaceLike 多后端兼容。

### 文件类型识别

文件类型识别是读取策略分发的依据。本方案采用三级递进识别机制，优先级从高到低如下：

1. 数据库 mimeType 字段优先：Workspace 存储层在文件写入时已通过 SQLite 的 mime_type 列持久化了 MIME 类型。Workspace.put 时由调用方传入或由文件扩展名推断（当前默认 text/plain 或 application/octet-stream）。read 工具直接读取 FileInfo.mimeType 作为第一优先级判断来源。
2. 扩展名辅助判定：当 mimeType 为通用值（如 application/octet-stream）或缺失时，通过文件路径扩展名按映射表匹配具体类型。映射表覆盖常见类型：图片类（png→image/png、jpg/jpeg→image/jpeg、gif→image/gif、webp→image/webp、svg→image/svg+xml）、文档类（pdf→application/pdf）、文本类（txt/md/json/xml/html/css/js/ts/py 等→text/*）。
3. magic bytes 内容检测：对于扩展名不可信或 mimeType 为通用二进制的场景，读取文件头若干字节（如 512 字节）检测 magic bytes。例如 PNG 文件头 89 50 4E 47、JPEG 文件头 FF D8 FF、PDF 文件头 25 50 44 46、GIF 文件头 47 49 46 38。此步骤作为兜底校验，也可发现扩展名与实际内容不匹配的异常情况。

识别结果归入四大类别：（1）text 类——MIME 以 text/ 开头或映射表判定为文本类，走现有文本读取路径；（2）image 类——MIME 以 image/ 开头且为可编码格式（png/jpeg/gif/webp，排除 svg），走字节读取+base64 编码路径；（3）pdf 类——MIME 为 application/pdf，走结构化信息路径；（4）binary 类——其余所有类型，走拒绝+结构化信息路径。SVG 虽然 MIME 为 image/svg+xml，但本质是 XML 文本，归入 text 类处理。

### 读取策略与分发

根据文件类型识别结果，read 工具的 execute 函数采用策略模式分发到四条处理路径：

路径一——文本文件（text 类）：完全复用现有逻辑。调用 ReadOperations.readFile 获取字符串内容，按换行符分割，附加行号前缀（格式为 "{lineNumber:>6}| {text}"），支持 fromLine/toLine 范围参数实现分页。受 MAX_LINES=2000 和 MAX_LINE_LENGTH=2000 约束，超出时截断并附加 "... [truncated]" 提示。返回格式保持 { path, content, totalLines, fromLine?, toLine? } 不变。

路径二——图片文件（image 类）：通过建议新增的 ReadOperations.readFileBytes 方法获取文件完整字节数据（Uint8Array），然后编码为 base64 字符串。不附加行号或文本截断逻辑。如果图片大小超过 IMAGE_MAX_SIZE 阈值（建议默认 20MB），拒绝读取并返回结构化信息：{ path, mimeType, size, category: "image", reason: "file_too_large", limit: IMAGE_MAX_SIZE }。

路径三——PDF 文件（pdf 类）：当前项目环境不含 PDF 解析库，因此不提取文本内容。read 工具返回结构化信息：{ path, mimeType, size, category: "pdf", reason: "unsupported_format", hint: "PDF 文件无法直接读取文本内容。建议使用专门的 PDF 处理工具或先将 PDF 转换为文本/图片格式后再放入工作区。" }。同时提供文件大小和 MIME 类型，方便 agent 判断是否需要采取替代方案。

路径四——普通二进制文件（binary 类）：返回结构化拒绝信息：{ path, mimeType, size, category: "binary", reason: "binary_file_not_readable", hint: "此文件为二进制格式，无法以文本方式读取。" }。对于已知但不可读的格式（如 .zip、.exe、.bin），在 hint 中给出针对性建议。

所有路径在 stat 阶段先检查 type === "directory"，目录直接返回错误信息（保持现有行为）。大文件在 stat 阶段即检查 size 是否超过全局 MAX_FILE_SIZE（1MB，与 grep 工具保持一致），超限时直接拒绝，不进入后续类型识别和读取流程。

### 模型输出转换

模型输出转换层负责将读取策略层的原始结果转换为 AI SDK 工具可返回的格式。当前 createReadTool 使用 AI SDK 的 tool() 函数，execute 返回普通 JS 对象。扩展后需支持多模态 content parts，涉及以下转换规则：

文本文件（路径一）：不改变返回结构。execute 直接返回 { path, content, totalLines, fromLine?, toLine? }，其中 content 为带行号的纯文本字符串。AI SDK 将此对象序列化后作为工具调用结果传递给模型，模型以文本方式理解。

图片文件（路径二）：execute 返回 { path, mimeType, size, category: "image", content: [{ type: "image", data: "<base64_string>" }] }。其中 content 为数组，每个元素对应一个 AI SDK content part。对于单张图片，数组中只有一个 type: "image" 的元素，data 字段为 base64 编码字符串。如果 AI SDK 要求 image part 使用特定字段名（如 image 或 source），在转换层做对应映射，但核心数据保持 base64 编码不变。图片 content part 可与文本说明组合：当 agent 同时读取文本和图片时，工具结果可包含 type: "text" 和 type: "image" 的混合数组。

PDF 和二进制文件（路径三、四）：execute 返回 { path, mimeType, size, category, reason, hint }，不包含 content 字段。category 为 "pdf" 或 "binary"，reason 为不可读取的具体原因标识，hint 为面向 agent 的自然语言建议。模型可根据此结构化信息决定后续操作（如提示用户手动处理、调用外部工具等）。

所有路径统一在外层包裹 { path, stat: { type, size, mtime } }，确保调用方始终能获取文件元信息。文本路径额外包含 totalLines/content，非文本路径额外包含 category/reason/hint。这种分层结构使得上游 agent 逻辑可以仅根据 category 字段判断结果类型，无需解析 content 内容。

### 限制与容错

限制控制层在多个阶段施加约束，确保读取操作不超出资源和性能边界，并在超出时给出明确、可操作的反馈。

全局文件大小限制：与 grep 工具保持一致，设定 MAX_FILE_SIZE = 1MB。在 stat 阶段即检查 FileInfo.size，超过 1MB 的文件直接返回 { path, error: "file_too_large", size, limit: MAX_FILE_SIZE }，不进入类型识别和读取流程。此限制适用于所有文件类型。

图片专用限制：在路径二（图片）中，额外施加 IMAGE_MAX_SIZE = 20MB 限制。虽然 MAX_FILE_SIZE 已将大多数图片拦截至 1MB，但保留此独立阈值以应对未来 MAX_FILE_SIZE 上调的场景。此外，解码图片头部获取像素尺寸（通过读取文件头若干字节解析 IHDR/JPEG SOF 等），若像素总数超过 IMAGE_MAX_PIXELS（建议默认 4096×4096=16,777,216 像素），拒绝读取并返回 { path, reason: "image_too_large", width, height, limit: IMAGE_MAX_PIXELS }。像素检测仅读取文件头，不完整解码图片，开销可控。

文本截断保持：现有 MAX_LINES=2000 和 MAX_LINE_LENGTH=2000 不变。单行超过 2000 字符时截断并追加 "... [line truncated]"；总行数超过 2000 时截断并追加 "... [truncated: showing first 2000 of N lines]"。这些截断提示保持现有格式，确保已有 agent 逻辑不受影响。

错误与异常处理：readFileBytes 读取失败时（如文件在 stat 后被删除、权限变化），返回 { path, error: "read_failed", message: <错误信息> }。base64 编码失败时（内存不足等极端场景），返回 { path, error: "encode_failed", mimeType }。MIME 类型解析异常时回退为 application/octet-stream，走 binary 类路径。所有异常路径均返回结构化错误对象而非抛出异常，确保工具调用不会因单次读取失败而中断整个 agent 推理循环。

总内容长度控制：当一次 read 调用读取多个文件或一个文件的多个分页时，累计返回内容总长度不超过 MAX_TOTAL_CONTENT_LENGTH（建议默认 100,000 字符）。超出时对后续文件或分页返回截断提示并附带 omit 标记。此机制防止单次工具调用结果过大导致模型上下文溢出。

### 多后端兼容

本方案在所有后端兼容层面遵循最小接口扩展原则：仅在 ReadOperations 接口中建议新增一个方法，其余机制通过组合现有接口实现。

ReadOperations 接口扩展：当前 ReadOperations 定义在 workspace.ts 中，包含 readFile(path: string): Promise<string | null> 和 stat(path: string): Promise<FsStat | null>。为支持图片和二进制文件的字节级读取，建议新增方法：readFileBytes(path: string): Promise<Uint8Array | null>。该方法语义与 readFile 一致，但返回原始字节而非 UTF-8 字符串。对于文本文件，调用方仍使用 readFile；对于图片等二进制文件，调用方使用 readFileBytes。两种方法可共存，不强制所有后端同时实现——未实现 readFileBytes 的后端在路径二/三/四中返回 { path, error: "bytes_read_unsupported" }，优雅降级。

WorkspaceLike 兼容：WorkspaceLike 是更高层抽象，包含 workspace、sharedWorkspace 等多种变体（定义于 workspace.ts）。当前 createReadTool 已接受 WorkspaceLike 参数，通过解构获取 readOperations。扩展后不改变 WorkspaceLike 的结构——各变体只需在其内部 readOperations 对象上增加 readFileBytes 方法。真实 workspace 对应的 FilesystemWorkspace 已在存储层（shell 包的 Workspace 类）实现了 readFileBytes（Uint8Array 级别）和 readFileStream，可直接对接。

存储层对接：shell 包中，Workspace.readFileBytes 已存在（签名：readFileBytes(path: string): Promise<Uint8Array | null>），内部根据文件大小决定从 SQLite 内联读取还是从 R2 溢出对象读取。R2 对象通过 httpMetadata.contentType 携带 MIME 类型，SQLite 行通过 mime_type 列存储。FileInfo 的 mimeType 字段已随 stat 调用返回。因此从接口到存储的完整链路已打通，扩展 read 工具无需修改存储层代码。

共享 workspace 后端：sharedWorkspace 通过 HTTP API 与远程 workspace 通信。扩展时需要 API 响应支持二进制传输（Content-Type: application/octet-stream + base64 编码或直接二进制流）。如果远程 API 短期内无法支持，sharedWorkspace 的 readFileBytes 实现可返回 null，触发优雅降级，确保向前兼容。
