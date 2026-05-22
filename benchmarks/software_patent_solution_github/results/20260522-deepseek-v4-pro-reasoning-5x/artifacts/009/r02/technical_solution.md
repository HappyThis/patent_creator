## 技术方案

本方案在现有 Workspace 虚拟文件系统（基于 SQLite + R2 的持久化文件存储）和 Think agent 工具链基础上，增强 workspace read 工具的文件理解能力。当工作区中存在图片、PDF 及其他非纯文本文件时，read 工具不再是简单返回「二进制文件不可读」这类结果，而是基于文件类型识别、内容转换和限制控制机制，将合适的文件内容以模型可消费的形式传递给多模态模型，对不适合直接传递的文件返回结构化的文件信息和限制原因。

### 1. 技术问题

当前 workspace read 工具（createReadTool）仅面向文本文件设计：通过 ops.readFile(path) 获取字符串内容，按换行符分拆、附加行号、支持 offset/limit 分页和长行/长内容截断（MAX_LINES=2000, MAX_LINE_LENGTH=2000）。当模型试图读取图片、PDF 等非文本文件时，readFile 返回的是 base64 编码字符串或乱码，模型无法从中获取可理解的信息，只能得到无意义的字符序列。系统缺乏以下能力：（1）区分文本、图片、PDF、普通二进制等文件类型；（2）将可被多模态模型消费的文件内容（如图片、PDF 页面）转换为模型可接收的内容块形式；（3）对过大文件、目录、缺失文件、不可转换的二进制文件返回结构化原因而非静默失败；（4）文件类型判断不依赖单一扩展名，结合数据库元数据和内容特征。」

### 2. 核心技术方案

方案在现有 createReadTool 架构上扩展，核心思路是：在不改变 WorkspaceLike 接口的前提下，增强 read 工具的 execute 函数，使其在执行路径中引入文件类型识别、读取策略选择、内容转换和结构化输出四个环节。同时，在 Workspace 后端补充字节级读取能力（readFileBytes 已存在），使 read 工具能够获取原始字节数据进行类型检测和内容转换。

增强后的 read 工具执行流程如下：（1）接收 path、offset、limit 参数（与现有接口兼容）；（2）调用 stat 获取文件元数据，若不存在则返回 { error: "file_not_found" }，若为目录则返回 { error: "is_directory" }；（3）基于 mimeType、magic bytes、扩展名的三级级联策略判定文件类别，归类为 text、image、pdf、binary 之一；（4）进入对应读取策略分支执行内容获取与转换；（5）构造结构化输出对象返回。以下详述各模块设计。

### 3. 文件类型识别模块

文件类型判定采用三级级联策略。第一级——数据库元数据：调用 stat() 获取 FileInfo.mimeType。Workspace 在 writeFile/writeFileBytes 时已将 mimeType 持久化到 SQLite 的 mime_type 列（如 writeFile 默认 'text/plain'，writeFileBytes 由调用方传入如 'image/png'）。若 mimeType 为明确的类型（如 image/png、application/pdf、text/plain 等），直接采用。第二级——magic bytes 检测：当 mimeType 为 application/octet-stream 或缺失时，通过 readFileBytes 读取文件头部字节（默认读取前 512 字节，可在配置中调整），与内置签名表比对。签名表覆盖常见格式：PNG（89 50 4E 47 0D 0A 1A 0A）、JPEG（FF D8 FF）、PDF（25 50 44 46）、GIF（47 49 46 38）、WebP（52 49 46 46）、BMP（42 4D）、ZIP/DOCX（50 4B 03 04）、MP4（...ftyp）等。匹配到的类型与 mimeType 合并，以 magic bytes 结果修正不可靠的元数据。第三级——扩展名回退：若前两级均未得出明确类型（magic bytes 未命中任何签名），从路径中提取扩展名并查表映射（.png→image/png, .jpg→image/jpeg, .pdf→application/pdf 等）。扩展名推断结果标记 confidence 字段为 'low'，供上层决策参考。

### 4. 读取策略路由与内容转换

根据文件类型判定结果，read 工具进入不同的处理分支。文本文件（mimeType 匹配 text/* 或常见代码类型如 application/json、application/javascript 等）：保持现有读取行为——通过 readFile 获取字符串，按行分割、附加行号、支持 offset/limit 分页、长行截断（MAX_LINE_LENGTH）和总行数截断（MAX_LINES），输出格式包含 path、content（带行号的文本）、totalLines、fromLine、toLine 字段。图片文件（mimeType 匹配 image/png、image/jpeg、image/gif、image/webp、image/bmp 等）：通过 readFileBytes 获取原始字节，编码为 base64 字符串，构造 data URI（如 data:image/png;base64,...）。对于多模态模型，此 content 可作为图片内容块直接传入模型上下文。PDF 文件（mimeType 为 application/pdf 或 magic bytes 检测为 PDF）：通过 readFileBytes 获取原始字节，编码为 base64 字符串。由于 PDF 的多页结构，提供 page 参数（默认为第 1 页）以支持按页读取；若运行环境具备 PDF 解析能力（如 PDF.js），可提取指定页面渲染为图片后再做 base64 编码。在不具备解析能力时，返回 base64 编码的完整 PDF 数据，标记 type 为 'pdf' 并附带页数等结构化元信息。普通二进制文件（mimeType 不属于上述任何类别，如 application/zip、application/octet-stream、可执行文件等）：不直接将内容传递给模型，而是返回结构化信息，包含 path、name、type（标记为 'binary'）、mimeType、size、readable（标记为 false），以及 reason 字段说明不可读原因（如 'unsupported_media_type'）。同时提供可选的 hex_dump 字段，返回文件头部少量字节的十六进制表示（如前 64 字节），供模型了解文件内容特征。

### 5. 大小限制控制

为避免过大的文件直接塞入模型上下文造成 token 超限或性能问题，方案设置两层大小限制。第一层——文件级限制：在读取前通过 stat() 获取 fileSize，与配置阈值 MAX_READABLE_SIZE（默认 10 MB）比较。若文件超过此阈值且为图片或 PDF，返回 { error: 'file_too_large', path, size, maxAllowed, hint: '建议使用分页参数或降低分辨率' }。对于超大文本文件，现有的 MAX_LINES=2000 和 MAX_LINE_LENGTH=2000 已提供行级截断保护。第二层——输出级限制：对图片和 PDF 的 base64 编码结果，检查编码后字符串长度是否超过 MAX_BASE64_OUTPUT（默认 20 MB 对应约 15 MB 原始数据）。若超出，对图片可降采样：JPEG 文件可降低质量重新编码、PNG 可缩小尺寸；对 PDF 可按页拆分，仅编码指定页。若仍超出限制，返回 { error: 'output_too_large', ... }。这些限制值可通过 createReadTool 的 options 参数配置，不同 agent 实例可根据模型上下文窗口大小灵活调整。

### 6. 结构化输出格式

read 工具的输出采用统一的结构化 JSON 格式，兼顾模型可消费性和用户可解释性。所有文件读取结果均包含以下公共字段：path（文件绝对路径）、name（文件名）、type（'text' | 'image' | 'pdf' | 'binary'）、mimeType（MIME 类型字符串）、size（字节数）。type 为 'text' 时额外包含 content（带行号的文本）、totalLines、fromLine、toLine、truncated（是否有截断）。type 为 'image' 时额外包含 content（data URI 格式的 base64 编码图片）、encoding（'base64'）、width 和 height（若可从图像头解析）。type 为 'pdf' 时额外包含 content（base64 编码的 PDF 数据）、encoding（'base64'）、pages（若可获取页数）。type 为 'binary' 时额外包含 readable（false）、reason（不可读原因代码）、hex_dump（可选，文件头部十六进制预览）。错误情况统一通过 error 字段返回，包含错误代码（如 'file_not_found'、'is_directory'、'file_too_large'、'unsupported_media_type'、'output_too_large'）和描述信息，使模型能够理解失败原因并采取替代策略（如读取目录列表、调整参数重试等）。

### 7. 多后端兼容机制

方案依赖 Workspace 后端提供的字节读取能力，当前 Workspace 类已有 readFileBytes(path) 方法返回 Uint8Array | null，WorkspaceFsLike 类型也包含 readFileBytes。增强后的 ReadOperations 接口需要在现有 readFile(path): Promise<string | null> 和 stat(path): Promise<FileInfo | null> 之外，增加可选的 readFileBytes(path): Promise<Uint8Array | null> 方法。createReadTool 的 options 中 ops 参数类型从 ReadOperations 扩展为 EnhancedReadOperations，新增 readFileBytes 为可选方法。当 readFileBytes 不可用时（某些精简 workspace 实现可能仅提供文本读取），工具回退到仅支持文本文件 + stat 中 mimeType 判断的降级模式：图片和 PDF 返回 { error: 'unsupported_media_type', reason: 'backend_lacks_byte_read' }，不对内容做 base64 编码。这种设计保证了方案与真实 Workspace、共享 Workspace（SharedWorkspace 代理转发）、自定义文件后端（如 InMemoryFs）等多种实现的兼容性——只要后端提供 readFileBytes 和 stat，即可获得完整的多媒体读取能力；只提供 readFile 的后端则优雅降级。

### 8. 完整处理流程

增强后的 read 工具完整执行流程如下：（1）参数校验——验证 path 非空、offset≥1（若提供）、limit≥1（若提供）。（2）元数据获取——ops.stat(path) 获取 FileInfo；若返回 null，输出 { error: 'file_not_found', path }；若 type 为 'directory'，输出 { error: 'is_directory', path, hint: '使用 list 工具查看目录内容' }。（3）类型判定——执行三级级联检测（mimeType→magic bytes→扩展名），输出 resolvedType 和 detectionSource（'metadata' | 'magic_bytes' | 'extension'）。（4）大小检查——若 size > MAX_READABLE_SIZE，输出 { error: 'file_too_large', ... }。（5）分支处理——text 分支：调用 readFile 获取文本内容，执行分页/行号/截断逻辑，构造文本输出；image 分支：调用 readFileBytes 获取字节，检查编码后大小是否超限，构造 base64 data URI 输出；pdf 分支：调用 readFileBytes，若需分页则应用 PDF 解析，构造 base64 输出；binary 分支：构造结构化不可读输出，可选附带 hex_dump。（6）输出组装——将各分支产物与公共字段合并，返回结构化 JSON 对象。各步骤中的异常均捕获并转换为带 error 字段的结构化输出，避免工具抛出未捕获异常导致 agent 循环中断。

### 9. 必要技术特征

本方案的必要技术特征包括以下七点。特征一：三级级联文件类型判定机制——利用数据库 mimeType 元数据、magic bytes 内容签名和扩展名映射按优先级递进判断文件真实类型，不依赖单一来源。特征二：类型驱动的多分支读取策略——根据判定结果将文件归类为 text/image/pdf/binary 四大类，每类采用不同的内容获取和转换路径。特征三：图片/PDF 到模型内容块的转换——通过 readFileBytes 获取原始字节后编码为 base64 data URI，使多模态模型可直接消费。特征四：双层大小限制控制——文件级阈值（MAX_READABLE_SIZE）和输出编码级阈值（MAX_BASE64_OUTPUT）配合工作，防止模型上下文溢出。特征五：统一结构化输出格式——所有读取结果（含错误）均返回包含 type、mimeType、size 等公共字段的 JSON 对象，错误通过 error 和 reason 字段传递可操作信息。特征六：字节读取能力的可选依赖——EnhancedReadOperations 将 readFileBytes 设计为可选方法，后端不提供时自动降级为纯文本模式。特征七：保持现有文本读取体验——文本文件的分页（offset/limit）、行号展示、长行截断（MAX_LINE_LENGTH）、总行截断（MAX_LINES）逻辑完全保留不动，仅在类型判断为非文本时才进入新分支。

### 10. 与项目环境的对应关系

本方案与现有项目环境的对应关系如下。Workspace 类（@cloudflare/shell/src/filesystem.ts）：已有 readFileBytes(path): Promise<Uint8Array | null> 方法、stat(path): Promise<FileStat | null> 方法（包含 mimeType 字段）、FileInfo 类型（包含 type/mimeType/size/path/name）。WorkspaceFsLike 类型：已包含 readFileBytes 方法签名。createReadTool（@cloudflare/think/src/tools/workspace.ts）：当前 createReadTool 接收 ReadOperations 接口（含 readFile 和 stat），方案将扩展为 EnhancedReadOperations（增加可选 readFileBytes），execute 函数内部增加类型判断和分支逻辑。WorkspaceLike 类型：当前已包含 readFile/stat，方案新增 readFileBytes 为可选。常量与配置：现有 MAX_LINES=2000、MAX_LINE_LENGTH=2000 保持不变；新增 MAX_READABLE_SIZE、MAX_BASE64_OUTPUT、MAGIC_BYTES_READ_SIZE 等配置项，通过 createReadTool 的 options 传入。

### 11. 技术效果

本方案的技术效果包括：（1）多模态模型可直接读取工作区中的图片和 PDF 内容，无需人工下载和重新上传，工作流程从「感知到二进制文件→无法处理→人工介入」变为「read 工具自动识别和转换→模型直接理解」。（2）文本读取体验零退化——所有现有文本读取行为（行号、分页、截断）完整保留，仅在需要时进入多媒体分支。（3）错误可操作——模型收到的不是无意义的乱码或静默失败，而是带错误代码和描述的结构化信息，可以据此调整行为（如换用 list 工具浏览目录、用 offset 分批读取大文本文件等）。（4）后端无关——通过将 readFileBytes 设计为可选依赖和自动降级机制，方案适用于真实 Workspace、共享 Workspace、InMemoryFs 及任意自定义文件后端。（5）类型判断准确——三级级联策略比单一扩展名判断更可靠：即使文件扩展名被误标或缺失，mimeType 和 magic bytes 仍能给出正确类型；即使元数据缺失，magic bytes 不依赖任何外部信息即可识别文件格式。

### 12. 风险与待确认问题

以下为方案实施中需关注的风险和待确认问题。（1）magic bytes 签名表的维护——签名表需覆盖常见图片和文档格式，但新格式不断涌现（如 AVIF、HEIC、JPEG XL 等）。建议将签名表设计为可扩展结构，支持通过 createReadTool options 注入自定义签名条目。（2）PDF 按页读取的实现——PDF 解析依赖运行环境中可用的 PDF 库（如 PDF.js）。在不具备解析能力的精简部署中，只能返回完整 base64 编码的 PDF；需确认目标部署环境是否支持引入 PDF 解析依赖。（3）图片降采样策略——当图片 base64 编码超出限制时，降采样涉及质量损失。需定义降采样参数（JPEG 质量系数、PNG 最大尺寸等）的默认值和可配置范围。（4）base64 编码的性能开销——大图片（如 10 MB PNG）的 base64 编码会增加约 33% 的体积，可能影响工具响应时间。建议对超过一定大小的图片在编码前进行压缩或降采样。（5）多模态模型的兼容性——不同多模态模型对 data URI 的支持方式可能不同（部分模型要求特定格式的 content block 结构）。建议在输出中同时提供 data URI 和原始字节长度的 base64 字符串，由上层适配器按模型要求组装最终消息格式。（6）WorkspaceLike 接口变更的影响范围——在 WorkspaceLike 中新增 readFileBytes 为可选方法需要评估对现有 SharedWorkspace 代理和第三方实现的兼容性影响。由于设计为可选，现有实现无需修改即可兼容，仅在需要多媒体能力时才需实现该方法。
