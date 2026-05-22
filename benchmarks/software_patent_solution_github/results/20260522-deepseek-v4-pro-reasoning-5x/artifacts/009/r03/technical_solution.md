## 技术方案

### 技术问题概述

在现有 Think agent 的 workspace 工具集中，read 工具通过 ReadOperations 接口读取文件内容，底层 Workspace 以文本字符串形式返回文件数据（readFile），再将内容按行分割、添加行号后返回给模型。该流程对文本文件有效，但面对图片、PDF 及普通二进制文件时存在明显不足：readFile 对 base64 编码的二进制内容执行 UTF-8 解码后返回乱码字符串，模型无法消费；read 工具未区分文件类型，对所有文件统一按文本处理；缺乏针对非文本文件的读取策略、大小限制和结构化原因反馈。这导致 agent 在工作区包含多媒体文件时只能得到“无法读取”或乱码结果，显著降低了多模态场景下的可用性。

### 总体架构

本方案在现有 read 工具与 Workspace 后端之间引入一个文件类型感知的读取管线，核心由四个环节组成：（1）文件类型识别层——综合扩展名、mimeType 元数据和内容特征字节（magic bytes）判定文件类别；（2）读取策略路由层——根据文件类型选择对应的读取方式（文本行读取、字节级读取、流式读取）；（3）内容转换层——将原始字节转换为模型可接收的内容块，文本保持行号/分页/截断，图片转为 base64 data URI 或模型原生图片块，PDF 提取文本或转为图片；（4）限制与反馈层——对大文件、目录、缺失文件、不可转换的普通二进制文件等场景，输出结构化原因信息。Workspace 后端需提供 readFileBytes（字节读取）和 stat（元数据查询）能力，这是本方案的基础依赖。

### 文件类型识别机制

文件类型识别采用三级判定策略，按优先级依次执行。第一级：扩展名快速分类。维护一个扩展名到类型标签的映射表，将 .png/.jpg/.gif/.webp/.svg 映射为 image，.pdf 映射为 pdf，.txt/.md/.json/.ts/.js/.py/.html/.css 等映射为 text。扩展名命中时直接返回类型标签，但仅作为初始判定——后续级别可覆写。第二级：mimeType 元数据判定。Workspace 在 writeFile/writeFileBytes 时持久化 mimeType，stat() 返回的 FileInfo 携带此字段。当 mimeType 以 image/ 开头时判定为图片，application/pdf 判定为 PDF，text/ 开头判定为文本，application/octet-stream 标记为普通二进制。第二级结果可覆写第一级。第三级：magic bytes 内容特征校验。对前两级判定为非文本的文件，读取文件头部若干字节（如 512 字节）与已知文件签名比对：PNG（89 50 4E 47）、JPEG（FF D8 FF）、PDF（25 50 44 46）、GIF（47 49 46 38）、WebP（52 49 46 46）等。若 magic bytes 与元数据/扩展名矛盾，以 magic bytes 为准并标记为类型不一致（在输出中提示）。三级均未命中时归类为 binary（普通二进制）。

### 读取策略路由

根据文件类型识别结果，read 工具路由到对应的读取策略。文本类文件：沿用现有 readFile（字符串）路径，按行分割、添加 1-indexed 行号、支持 offset 和 limit 分页、单行超过 MAX_LINE_LENGTH 时截断并标注、总行数超过 MAX_LINES 时截断并提示。图片类文件：调用 readFileBytes 获取原始字节，根据模型能力转换为 base64 data URI（如 data:image/png;base64,...）或构造为模型 SDK 支持的图片内容块（如 AI SDK 的 image 类型 part）。输出中附带文件名、媒体类型、尺寸、字节数等元信息。PDF 类文件：首先读取字节内容，若系统配置了 PDF 解析能力（如 PDF.js 集成），提取文本内容并以带行号的文本格式返回；若无解析能力，将 PDF 首页渲染为图片后按图片策略返回，同时标注“PDF 文本提取不可用，已转为图片”。普通二进制文件：不传递文件内容，返回结构化信息包括文件名、大小、mimeType、不可读取原因（如“binary file, content not displayable”），并建议使用其他工具处理。

### 大小限制与安全机制

为防止过大文件直接塞入模型上下文导致 token 超限或内存溢出，本方案设置多层大小限制。文件总大小限制：超过阈值（默认 10 MB 图片、5 MB PDF、1 MB 文本）的文件不读取内容，返回文件名、大小、类型和超限原因。文本截断参数：MAX_LINES（默认 2000 行）和 MAX_LINE_LENGTH（默认 2000 字符）继续适用于文本文件。图片尺寸限制：对图片类文件，若像素尺寸超过阈值（如 4096×4096）或字节数超过限制，返回缩略信息并标注“图片过大，已省略内容”。这些限制均为可配置参数，不同 workspace 实现可通过构造选项覆盖默认值。

### 多后端兼容机制

本方案通过接口抽象兼容不同 workspace 实现。对 Workspace 后端的最小能力要求为：提供 readFileBytes(path) 返回 Uint8Array 或 null，提供 stat(path) 返回包含 type、mimeType、size 字段的 FileInfo 或 null。当前 Workspace 实现（SQLite+R2）和 WorkspaceFileSystem 适配器已满足该要求，InMemoryFs 等其他 FileSystem 实现也具备等效能力。对于跨 DO 代理场景（如 SharedWorkspace），代理层只需透传 readFileBytes 和 stat 调用即可。read 工具本身不直接依赖具体后端实现，而是通过 ReadOperations 接口的扩展版本（增加 readFileBytes 方法）访问数据。具体集成方式：在 createReadTool 的 ReadOperations 接口中增加可选的 readFileBytes 方法；若后端提供该方法，启用文件类型感知管线；若未提供，回退到现有纯文本读取行为，保证向后兼容。

### 结构化输出与错误反馈

read 工具的输出采用统一的结构化格式，兼顾模型可用性和用户可解释性。成功读取文本时返回：{ path, content（带行号文本）, totalLines, fromLine, toLine, fileType: "text", mimeType, size }。成功读取图片时返回：{ path, fileType: "image", mimeType, size, width, height, content: [{ type: "image", data: "base64..." }] }（content 格式取决于模型 SDK 要求）。无法读取时返回：{ path, fileType, mimeType, size, error: "原因描述", hint: "建议操作" }。其中 error 字段使用枚举化原因码，包括：file_not_found、is_directory、binary_not_displayable、file_too_large（附实际大小和限制值）、unsupported_format、no_pdf_parser。hint 字段给出面向模型的建议，如“use a dedicated PDF tool”、“file is a directory, use list tool instead”。这种结构化输出使模型能根据 fileType 和 error 字段做出恰当的后续决策。

### 处理流程

read 工具执行时按以下步骤处理。步骤 1：路径校验与 stat 查询——规范化路径，调用 ops.stat(path) 获取 FileInfo；若返回 null，输出 file_not_found 错误；若 type 为 directory，输出 is_directory 错误并提示使用 list 工具。步骤 2：文件类型识别——基于 FileInfo.name（扩展名）、FileInfo.mimeType 执行前两级分类；若判定为非文本，调用 ops.readFileBytes 读取头部 magic bytes 校验；最终确定 fileType。步骤 3：大小检查——将 FileInfo.size 与对应 fileType 的阈值比较；超限则返回 file_too_large 错误。步骤 4：按类型读取——文本走 readFile 字符串路径、应用行号/分页/截断；图片走 readFileBytes 字节路径、编码为 base64 data URI 或模型图片块；PDF 优先尝试文本提取、降级为图片渲染；二进制走结构化错误输出。步骤 5：构造输出——将读取结果或错误信息封装为统一结构返回。

### 关键模块

- FileTypeResolver：文件类型识别模块，封装扩展名映射表、mimeType 规则集、magic bytes 签名库，输入 stat 结果和 readFileBytes 能力，输出标准化的 fileType 标签和置信度。
- ReadStrategyRouter：读取策略路由模块，根据 fileType 选择对应处理器（TextHandler / ImageHandler / PdfHandler / BinaryHandler），每个处理器实现统一的 handle(path, stat, ops) => ReadResult 接口。
- ContentTransformer：内容转换模块，TextHandler 内部复用现有行号格式化逻辑；ImageHandler 负责 base64 编码和图片尺寸解析；PdfHandler 负责 PDF 文本提取或首页图片转换。
- SizeGuard：大小限制模块，维护可配置的 fileType => maxBytes 映射，在读取前执行检查，超限时生成包含实际值、限制值和 fileType 的结构化错误。
- ReadResultBuilder：输出构建模块，将各处理器的返回值和错误信息统一封装为包含 path、fileType、mimeType、size、content/error、hint 字段的标准结构。

### 技术效果

本方案在不破坏现有文本读取体验的前提下，赋予 workspace read 工具感知和处理多种文件类型的能力。具体技术效果包括：（1）通过三级文件类型判定（扩展名→mimeType→magic bytes），准确识别文本、图片、PDF 和普通二进制文件，避免依赖单一信息源导致的误判；（2）图片和 PDF 内容以模型原生可消费的内容块形式传递，而非仅仅返回文件路径，使多模态模型能直接理解和处理工作区中的视觉信息；（3）多层大小限制机制（文件级、行级、像素级）防止过大内容撑爆模型上下文；（4）结构化错误输出携带原因码和操作建议，使模型能够做出合理的后续决策（如改用其他工具、提示用户等）；（5）基于接口抽象的读取管线兼容真实 Workspace、共享 Workspace 代理和自定义文件后端，通过可选的 readFileBytes 能力检测实现平滑降级。

### 风险与待确认问题

以下是本方案实施前需确认的风险点和技术决策事项：（1）模型图片内容块格式：不同模型 SDK 对图片内容的接收格式存在差异（如 OpenAI 使用 image_url 类型、Anthropic 使用 image 类型 source base64），需确认 Think agent 的目标模型生态并设计兼容适配层。（2）PDF 解析能力边界：是否依赖外部 PDF 解析库（如 pdf.js）还是仅使用图片降级方案；若引入解析库，需评估 Web Worker/DO 环境的兼容性和冷启动开销。（3）magic bytes 库维护：需维护文件签名数据库，覆盖常见图片、文档和压缩格式；该库需支持可扩展注册以适配自定义文件后端。（4）大文件读取的性能边界：虽然设置了大小限制，但对于接近限制值的大图片（如 10 MB PNG），base64 编码会使体积膨胀约 33%，需在内存和上下文占用之间做权衡。（5）与现有 edit/write/grep 工具的协同：当前 edit 和 grep 工具同样假设文件为文本；若未来扩展其支持非文本文件，可复用本方案的文件类型识别和读取策略路由基础设施。

### 与当前项目环境的对应关系

本方案直接基于当前项目环境中的以下模块设计。Think Agent（packages/think/src/think.ts）：通过 createWorkspaceTools(this.workspace) 创建工具集，本方案的增强 read 工具通过修改 createReadTool 实现。Workspace 工具文件（packages/think/src/tools/workspace.ts）：ReadOperations 接口当前包含 readFile 和 stat 方法，本方案将其扩展为增加可选的 readFileBytes 方法，createReadTool 内部根据 readFileBytes 是否可用决定是否启用文件类型感知管线。Workspace 文件系统（packages/shell/src/filesystem.ts）：已提供 readFile、readFileBytes、stat、readFileStream 等方法；FileInfo 类型已包含 mimeType、size、type 字段；writeFile/writeFileBytes 在写入时已持久化 mimeType；storage_backend 字段可区分 inline 和 R2 存储。FileInfo 类型中的 mimeType 和 size 字段是本方案文件类型识别和大小检查的直接数据来源，无需修改底层存储结构。WorkspaceFileSystem 适配器（packages/shell/src/workspace.ts）：已将 Workspace 包装为 FileSystem 接口，readFileBytes 方法已实现并在文件不存在时抛出 ENOENT。
