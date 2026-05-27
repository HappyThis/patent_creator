## 技术方案

本方案在现有 Think agent 的 workspace read 工具基础上，引入文件类型识别层、差异化读取策略层和模型输出转换层，使 read 工具能够根据文件的媒体类型和内容特征，为多模态模型提供可消费的图像/PDF 等内容块，对不可直接传递的文件返回结构化的描述信息。方案的核心设计原则是：不破坏现有文本读取体验（行号、分页、截断全部保留），仅扩展非文本路径；文件类型判断以已有元数据（mimeType）为主、内容特征（magic bytes）为辅；多后端兼容通过扩展 ReadOperations 接口实现，不绑定特定 Workspace 实现。

### 文件类型识别机制

当前 Workspace 在写入文件时已持久化 mimeType 字段（存储于 cf_workspace 表的 mime_type 列），stat 方法返回的 FileInfo 包含该字段。本方案在 read 工具中新增文件类型判定步骤，基于 FileInfo.mimeType 和可选的内容特征检测，将文件分为四类：文本类（text/*）、图像类（image/*）、PDF 类（application/pdf）和普通二进制类（其他）。

判定优先级如下：首先读取 stat 返回的 FileInfo.mimeType 字段。若 mimeType 以 "text/" 开头或为常见文本类型（application/json、application/xml、application/javascript 等），归类为文本；若为 "image/png"、"image/jpeg"、"image/gif"、"image/webp"、"image/svg+xml" 等，归类为图像；若为 "application/pdf"，归类为 PDF；其余归类为普通二进制。同时，为避免扩展名伪造导致 mimeType 与实际内容不符，方案新增可选的 magic bytes 校验：对前 512 字节进行内容特征检测，若 magic bytes 与 mimeType 声明冲突，以 magic bytes 检测结果为准，并将检测到的真实类型写入返回结果中的 detectedType 字段。

### 差异化读取策略

文件类型判定完成后，read 工具进入差异化读取路径。本方案将读取策略分为四条路径：文本读取、图像读取、PDF 读取和普通二进制/目录/缺失文件的结构化返回。所有路径在调用 workspace 后端时，均通过 ReadOperations 接口（已扩展 readFileBytes 方法）进行，确保与具体 workspace 实现解耦。

文本读取路径保持现有体验不变：通过 readFile 获取字符串内容，按换行符 split，应用 offset/limit 分页参数，添加行号前缀（格式为 "行号\t内容"），对超过 MAX_LINE_LENGTH（2000 字符）的单行截断并标注 "... (truncated)"，对超过 MAX_LINES（2000 行）的结果集截断并标注 "... (N more lines truncated)"。返回结果中包含 path、content、totalLines，以及可选的 fromLine/toLine 分页信息。对于编码为非 UTF-8 的文本文件，通过 readFileBytes 读取原始字节，结合 content_encoding 字段进行解码尝试；若解码失败，回退为普通二进制处理路径。

图像读取路径通过 readFileBytes 获取原始字节数据（Uint8Array）。读取前先检查文件大小是否超过 MAX_IMAGE_SIZE（默认 20MB），超过则返回结构化错误（含 fileName、mediaType、size、error: "file_too_large"、maxSize）。未超限的图像，将字节数据转为 base64 编码字符串，构造模型可接收的媒体内容块。对于 JPEG/PNG/GIF/WebP 等二进制图像格式，构造格式为 data:[mediaType];base64,[base64Data] 的 data URL；对于 SVG（image/svg+xml），由于是文本格式，直接以文本内容返回，但仍标记为图像类型。返回结果中包含结构化元信息：path、mediaType、size、encoding（"base64"）、content（base64 数据或 data URL）。

PDF 读取路径同样通过 readFileBytes 获取原始字节，在大小限制（MAX_PDF_SIZE，默认 50MB）内将字节转为 base64 编码，并附加页数等可解析元信息（若 PDF 头部可解析）。返回结构化信息包括 path、mediaType、size、encoding、content 和可选的 pageCount。对于超出大小限制的 PDF，返回 file_too_large 错误。普通二进制路径和目录/缺失文件路径：对于 mimeType 不属于文本/图像/PDF 的二进制文件，不读取内容，直接返回结构化信息（path、mediaType、size、reason: "binary_file_not_displayable"）；对于目录，返回 {error: "path is a directory, not a file"}（保持现有行为）；对于不存在的文件，返回 {error: "File not found: path"}（保持现有行为）。所有错误返回均包含足够信息供模型理解和向用户解释。

### 模型输出转换机制

read 工具的执行结果需要同时服务于两个消费者：多模态大模型（需要结构化内容块）和终端用户（需要可读的解释信息）。本方案通过输出格式的统一封装实现兼顾：所有返回结果均为 JSON 结构，包含顶层 type 字段指示内容类别，模型和用户均可据此解析。

文本类型返回格式保持现有契约（path、content、totalLines、可选 fromLine/toLine），type 为 "text"。图像类型返回 type 为 "image"，content 字段包含 base64 编码的图像数据，额外携带 mediaType、size、width/height（若可从图像头部解析）等元数据字段。PDF 类型返回 type 为 "pdf"，content 为 base64 编码数据，携带 mediaType、size 和可选 pageCount。不可展示类型返回 type 为 "binary" 或 "error"，content 替换为 reason 字段，说明不可读的原因（如 "binary_file_not_displayable"、"file_too_large"、"directory"、"not_found"）。所有类型均携带 fileName 和 mediaType 字段以保证用户可解释性。

对于多模态模型，图像和 PDF 的 base64 数据可通过 AI SDK 的 content 块机制（如 image content part 或 file content part）直接注入模型上下文。对于不具备多模态能力的模型，系统在工具描述中声明 read 工具的能力范围，由模型自行决定是否调用；若模型仍尝试读取非文本文件，返回的结构化错误信息可引导模型向用户解释限制。方案不强制要求模型支持多模态——read 工具在所有模型中均可使用，但非多模态模型仅能获得结构化元数据而非实际图像/PDF 内容。

### 大小限制与安全控制

为避免过大文件直接塞入模型上下文窗口导致 token 消耗失控或上下文溢出，方案在多个维度设置了限制阈值。

文本路径保留现有 MAX_LINES（2000 行）和 MAX_LINE_LENGTH（2000 字符）限制，超出的行和内容分别截断并标注。图像路径限制 MAX_IMAGE_SIZE（默认 20MB），PDF 路径限制 MAX_PDF_SIZE（默认 50MB），普通二进制文件的读取限制 MAX_BINARY_SIZE（默认 10MB，只读取元数据不读取内容）。所有阈值均为可配置常量，可通过 createReadTool 的选项参数按部署环境调整。

大小限制在执行路径中的位置为：先通过 stat 获取 FileInfo.size，在读取文件内容之前进行阈值判断。对于超限文件，不读取任何字节数据，直接返回结构化错误。此设计避免了对超大文件的不必要 I/O 和内存分配。此外，方案在 workspace 后端层利用已有的 inline/R2 存储分流机制：对于存储在 R2 的大文件，readFileBytes 通过流式读取避免全量加载到 SQLite 内存；对于内联存储（inline）的小文件，base64 解码后直接获取字节。

### 多后端兼容机制

当前系统已通过 WorkspaceLike 接口实现 workspace 后端的抽象，支持真实 Workspace（SQLite+R2）、SharedWorkspace（跨 DO 代理）和自定义文件后端。本方案在此基础上将 ReadOperations 接口从仅包含 readFile + stat 扩展为包含 readFileBytes，使差异化读取策略所需的字节级读取能力通过接口契约而非具体实现获得。

扩展后的 ReadOperations 接口定义为：{ readFile(path: string): Promise<string | null>; readFileBytes(path: string): Promise<Uint8Array | null>; stat(path: string): Promise<FileInfo | null> }。真实 Workspace 天然满足此接口（已实现 readFile、readFileBytes、stat）；SharedWorkspace 代理只需在 RPC 转发层增加 readFileBytes 方法的转发（将 Uint8Array 序列化为 ArrayBuffer 传输）；自定义后端只需实现这三个方法即可接入。workspaceReadOps 工厂函数同步更新，从 ws.readFileBytes 绑定新的字节读取能力。

WorkspaceLike 类型（Think 内部使用的 workspace 最小接口）同步扩展为包含 readFileBytes 方法的 Pick 集合。向后兼容性通过可选方法检测保证：若某个 workspace 实现未提供 readFileBytes，read 工具回退为仅支持文本读取路径，非文本文件统一返回 "binary_file_not_displayable" 的结构化错误，并在原因中注明 "workspace does not support byte-level read"。此设计确保方案在不同 workspace 实现之间的渐进式部署能力。

### 关键处理流程

createReadTool 的 execute 函数改造后的完整处理流程如下。

1. stat 获取 FileInfo：调用 ops.stat(path)，若返回 null 则返回 {error: "File not found"}；若 type 为 "directory" 则返回 {error: "is a directory"}。
2. 文件类型判定：基于 FileInfo.mimeType 进行类型分类。若 mimeType 不可信或为空，且 workspace 支持 readFileBytes，读取前 N 字节（如 512 字节）进行 magic bytes 检测，以检测结果修正类型分类。
3. 大小阈值检查：根据类型分类选择对应的 MAX_SIZE 阈值，与 FileInfo.size 比较。超限则直接返回结构化错误，不读取内容。
4. 按类型读取：(a) 文本类：调用 ops.readFile(path)，按现有逻辑处理行号、分页、截断；(b) 图像类：调用 ops.readFileBytes(path)，获取 Uint8Array，编码为 base64，构造 data URL，返回结构化图像结果；(c) PDF 类：调用 ops.readFileBytes(path)，编码为 base64，解析 PDF 头部获取页数信息，返回结构化 PDF 结果；(d) 普通二进制类：不读取内容，直接返回 {path, mediaType, size, reason: "binary_file_not_displayable"}。
5. 输出封装：将各路径的结果统一为包含 type 字段的 JSON 结构返回给模型。

magic bytes 检测的核心规则：读取文件前 512 字节，按常见文件签名表匹配——PNG 为 89 50 4E 47 0D 0A 1A 0A，JPEG 为 FF D8 FF，GIF 为 47 49 46 38，PDF 为 25 50 44 46，WebP 为 52 49 46 46...57 45 42 50。若签名匹配结果与 mimeType 声明的类型冲突，以签名匹配结果为准并记录 detectedType。若签名无法识别且 mimeType 为 application/octet-stream 或缺失，归类为普通二进制。该检测仅在 readFileBytes 可用时执行；若 workspace 不支持字节读取，则完全依赖 mimeType 判断。

### 技术效果

本方案在技术效果层面实现了以下提升。

- 多模态感知能力：agent 可以读取工作区中的图片和 PDF 并以模型可消费的 base64 编码传递，使多模态模型能够在对话中直接分析图表、截图、设计稿和文档内容，无需用户手动描述或使用外部工具转换。
- 结构化可解释性：所有读取结果（包括文本、图像、PDF、二进制和错误）均携带 fileName、mediaType、size 等元数据字段和 type 分类字段，模型和用户均可据此理解文件性质和读取结果。
- 渐进式兼容：文本读取路径保持零改动，行号、分页、截断行为完全不变。workspace 后端的字节读取能力通过接口扩展引入，不支持字节读取的后端自动回退为纯文本模式，确保平滑升级。
- 安全边界控制：多级大小限制防止大文件直接注入模型上下文，阈值均可配置。类型检测结合 mimeType 和 magic bytes 双重验证，降低扩展名伪造风险。
- 多后端透明：通过 ReadOperations 接口抽象和 WorkspaceLike 类型扩展，方案对真实 Workspace、SharedWorkspace 代理和自定义后端一视同仁，不绑定特定存储实现。

### 风险与待确认问题

以下为本方案当前识别到的待确认风险和技术边界。

- readFileBytes 在 SharedWorkspace 跨 DO 代理场景下，大文件的 Uint8Array 序列化/反序列化可能带来 RPC 传输开销。需确认 RPC 通道的 ArrayBuffer 传输机制是否支持流式或分块传输，或是否需要限制跨代理读取的最大字节数。
- PDF 页数解析依赖 PDF 文件头部的线性化目录信息，对于某些非标准 PDF 可能无法解析出 pageCount，此时应回退为不携带页数的 base64 传递。
- 多模态模型的 image content block 格式因模型提供商而异（如 OpenAI 的 image_url 与 Anthropic 的 image content block 格式不同）。方案在当前阶段将 base64 数据放在 read 工具返回的 content 字段中，由上层调用方（Think agent 的 _runInferenceLoop）根据具体模型提供商将工具结果转换为对应的 content block 格式。这一转换逻辑的归属需要进一步明确。
- magic bytes 检测表需要持续维护以覆盖更多格式（如 HEIC、TIFF、BMP 等），但核心格式（PNG/JPEG/GIF/WebP/PDF）的签名已稳定，不影响核心功能。
- 当前 scheme 依赖 writeFile 时正确设置 mimeType；对于通过其他渠道（如直接 SQL 插入、R2 上传）写入的文件，mimeType 可能为默认值 application/octet-stream 或 text/plain，需要 magic bytes 检测兜底。
