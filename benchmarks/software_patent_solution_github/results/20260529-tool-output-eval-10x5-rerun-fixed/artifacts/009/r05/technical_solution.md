## 技术方案

### 技术问题概述

在 Think agent 的 workspace 文件工具集中，现有 read 工具通过调用 Workspace.readFile() 获取文件文本内容，按行分割后附加行号、支持偏移量（offset）和行数限制（limit）进行分页输出，并对超长行和超多行进行截断。这一实现假设所有文件均为文本文件，未考虑工作区中可能存在的图片、PDF 及其他非纯文本文件类型。当模型调用 read 工具读取图片或 PDF 时，底层返回的 base64 解码后二进制内容被强制作文本解释，产生无意义的乱码输出，模型无法获取有效信息。

### 整体架构

本方案在保持现有文本读取体验（行号、分页、截断）不变的前提下，扩展 workspace read 工具以支持多类型文件，核心机制包括：（1）基于多源信息的文件类型识别；（2）按文件类型分发的读取策略；（3）面向多模态模型的输出内容转换；（4）统一的大小限制与截断控制；（5）针对无法读取场景的结构化错误返回。方案通过扩展 WorkspaceLike 接口和 ReadOperations 抽象层实现与多种 workspace 后端的兼容。

### 文件类型识别机制

文件类型识别采用三层判断机制，按优先级依次为：数据库元数据、文件内容特征（magic bytes）、以及扩展名推断。

第一层：数据库元数据。Workspace 的表结构已包含 mime_type 字段（默认值为 'text/plain'）。当文件通过 writeFileBytes 写入时，调用方可显式传入 MIME 类型；当 MIME 类型为非默认值（即非 'text/plain' 且非空）时，read 工具直接信任该元数据作为类型判断依据。例如，图片文件在写入时被标记为 'image/png'，read 工具即可据此识别为图片类型。

第二层：内容特征检测。当 mime_type 为默认值或缺失时，read 工具通过 Workspace 新增的 readFileBytes 接口读取文件头部字节（首 512 字节），与已知文件魔数（magic numbers）进行比对。支持的检测类型包括：PNG（89 50 4E 47）、JPEG（FF D8 FF）、GIF（47 49 46 38）、WebP（52 49 46 46）、PDF（25 50 44 46）、BMP（42 4D）等常见格式。检测结果与对应的 MIME 类型建立映射。

第三层：扩展名推断。当上述两层均无法确定类型时，从文件路径中提取扩展名，与内置的扩展名-MIME 映射表进行匹配（如 .jpg → image/jpeg、.pdf → application/pdf、.md → text/markdown）。无匹配扩展名时，回退为 'application/octet-stream'（普通二进制）。

### 读取策略与内容分发

read 工具在执行时，先通过 stat 调用获取文件的 FileInfo（含 type、mimeType、size），然后调用文件类型识别器确定最终文件类别。根据识别结果，进入不同的处理分支：

分支一：文本文件（text/*、application/json、application/xml、text/x-* 等）。保持现有行为：调用 readFile 获取字符串内容，按换行符分割，附加行号，支持 offset/limit 分页偏移，对超过 MAX_LINE_LENGTH（2000 字符）的单行进行截断标记，对超过 MAX_LINES（2000 行）的总输出附加截断说明。输出格式为：{ path, content, totalLines, fromLine?, toLine? }。

分支二：图片文件（image/png、image/jpeg、image/gif、image/webp、image/bmp、image/svg+xml 等）。调用 readFileBytes 获取原始字节，使用 base64 编码后构造 data URL（格式：data:{mimeType};base64,{encoded}）。输出以模型可接收的图片内容块形式返回，包含：{ path, type: 'image', mimeType, size, dataUrl }。对于 SVG 格式，直接返回文本内容（XML），不进行 base64 编码。同时附加图片的基本元数据（尺寸字节数、格式）。

分支三：PDF 文件（application/pdf）。调用 readFileBytes 获取原始字节，base64 编码后以 data URL 形式传递（data:application/pdf;base64,...）。输出包含：{ path, type: 'pdf', mimeType, size, dataUrl, pagesEstimate }。pagesEstimate 基于文件大小粗略估算页数（按平均每页 50KB 估算），供模型参考是否值得深入分析。

分支四：普通二进制文件（application/octet-stream 及其他未能识别为可消费类型的文件）。不传递原始二进制内容给模型，而是返回结构化的文件信息：{ path, type: 'binary', mimeType, size, reason: 'Binary file type not supported for content reading', hint: 'Use stat or other tools to inspect file metadata' }。对压缩包（application/zip、application/gzip、application/x-tar 等）同样归类为不可消费类型，但 hint 会建议使用 execute 工具解压后读取。

分支五：目录。stat 返回 type 为 'directory' 时，返回：{ error: '{path} is a directory, not a file', type: 'directory', hint: 'Use list tool to view directory contents' }。这与现有行为兼容。

分支六：文件不存在。stat 返回 null 时，返回：{ error: 'File not found: {path}' }，与现有行为兼容。

### 大小限制与截断控制

为防止过大文件直接塞入模型上下文导致 token 超限或性能下降，read 工具实施多层大小控制：

第一层：文件总大小限制。新增参数 maxFileSize（默认 10MB），由 stat 返回的 size 字段与 maxFileSize 比较。超过阈值时，不读取文件内容，直接返回：{ error: 'File too large', path, size, maxAllowed: maxFileSize, hint: 'Use offset/limit for text files, or process the file with external tools' }。

第二层：Base64 编码后大小限制。对于图片和 PDF 等需要 base64 编码后传入模型的类型，编码后的字符串长度可能膨胀约 33%。系统在编码前检查原始字节数是否超过 base64MaxSize（默认 5MB），超过时拒绝编码并返回结构化错误，说明原因和建议（如压缩图片、使用外部 OCR 服务等）。

第三层：文本截断保持。现有文本路径的 MAX_LINES（2000 行）和 MAX_LINE_LENGTH（2000 字符）限制保持不变，确保长文本文件不会无限制输出。

maxFileSize 和 base64MaxSize 均作为 read 工具的可选参数暴露给模型，模型可根据自身上下文窗口大小动态调整限制值。

### 多后端兼容机制

为兼容真实 Workspace、SharedWorkspace 代理、自定义文件后端等不同实现，方案在接口层面进行抽象：将 WorkspaceLike 类型扩展为 EnhancedWorkspaceLike，在原有 readFile、stat 等方法基础上新增 readFileBytes 方法要求。

EnhancedWorkspaceLike 定义如下核心方法：readFile(path) → string | null（文本读取，保持兼容）；readFileBytes(path) → Uint8Array | null（字节读取，图片/PDF 读取的基础）；stat(path) → FileInfo | null（获取包含 mimeType、size、type 的完整元数据）。任何满足该接口的 workspace 实现均可作为 read 工具的后端。

对于真实 Workspace（@cloudflare/shell），其已具备 readFile、readFileBytes、stat 三个方法，可直接适配。对于 SharedWorkspace（跨 DO 代理），需要在代理层确保 readFileBytes 方法通过 RPC 转发并正确序列化 Uint8Array 为可传输格式（如 base64 字符串或 ArrayBuffer）。对于自定义文件后端，只需实现上述三个方法即可接入。

同时，ReadOperations 接口同步扩展为 EnhancedReadOperations，新增 readFileBytes(path) → Uint8Array | null 方法。createReadTool 函数接受 EnhancedReadOperations，确保不同 workspace 后端经各自的 operation factory 适配后均可获得完整的多类型读取能力。对于仅提供文本读取的旧版后端，通过降级逻辑（检测 readFileBytes 是否可用）回退为纯文本模式，仅输出文本文件内容，对非文本文件返回结构化错误。

### 处理流程

read 工具的整体处理流程如下：

1. 模型调用 read 工具，传入参数 { path, offset?, limit?, maxFileSize?, base64MaxSize? }。
2. 调用 EnhancedReadOperations.stat(path) 获取 FileInfo。若 stat 返回 null，返回「文件不存在」错误。若 type 为 'directory'，返回「路径为目录」错误。
3. 检查 fileInfo.size 是否超过 maxFileSize。若超过，返回「文件过大」结构化错误。
4. 调用文件类型识别器 determineFileType(fileInfo.path, fileInfo.mimeType, ops)：优先使用 mimeType（若非默认值）；否则通过 readFileBytes 读取头部 magic bytes 检测；最后使用扩展名推断。返回归一化的 MIME 类型字符串。
5. 根据 MIME 类型进入处理分支：若为 text 类型，调用 readFile 获取文本，执行行号附加、分页截断、行截断，返回文本结果。若为 image 类型，检查原始 size 是否超过 base64MaxSize；若未超过，调用 readFileBytes 获取字节，base64 编码为 data URL，返回图片内容块。若为 application/pdf，与图片类似，base64 编码后返回 PDF 内容块附加页数估算。若为其他二进制类型，返回结构化「不支持内容读取」信息。
6. 返回结果中包含 path、type 字段、mimeType、size、以及根据类型附加的 content/dataUrl/error/reason 等字段，确保模型能根据 type 字段判断结果性质并采取后续行动。

### 输出格式规范

为兼顾模型可用性和用户可解释性，所有输出结果均包含统一的元数据字段 path、type、mimeType、size，type 字段取值为 'text'、'image'、'pdf'、'binary'、'directory'、'error' 之一。模型可根据 type 字段快速判断结果性质而无需解析内容。

文本结果额外包含 content（带行号的文本字符串）、totalLines、fromLine/toLine（分页时）。图片结果包含 dataUrl（data:image/...;base64,...）、width/height（若能从头解析，PNG/GIF/BMP/JPEG 可从头部字节提取尺寸）。PDF 结果包含 dataUrl、pagesEstimate。二进制/不支持的结果包含 reason 和 hint 字段，说明限制原因和建议操作。错误结果包含 error 和可选的 hint。

### 技术效果

本方案通过多类型文件读取能力扩展，解决了 Think agent 无法理解工作区中非文本文件的技术问题。具体技术效果包括：

- 多模态模型可消费图片和 PDF 内容：通过 data URL 格式传递 base64 编码后的文件内容，多模态模型可直接在上下文中查看图片、阅读 PDF，无需额外工具链或外部服务。
- 文本读取体验零退化：现有文本读取逻辑（行号、分页、截断）完整保留，text 类型文件的输出格式和行为不变，保证向后兼容。
- 类型识别可靠：三层判断（元数据→magic bytes→扩展名）比单一扩展名判断更可靠，尤其对于无扩展名或扩展名不准确的文件（如临时文件、下载文件）仍能正确识别。
- 优雅的错误与限制处理：过大文件、目录、不存在文件、不支持的类型均返回结构化信息而非崩溃或无意义输出，模型可根据 reason 和 hint 字段做出合理决策。
- 后端无关性：通过 EnhancedWorkspaceLike 接口抽象，方案适用于真实 Workspace、SharedWorkspace 代理以及任何实现了 readFile/readFileBytes/stat 的自定义文件后端，具备良好的可扩展性。
- 文件类型向下兼容：即使后端不支持 readFileBytes（旧版 workspace），read 工具自动降级为纯文本模式，仅对能识别的文本类文件返回内容，对其他类型返回结构化限制说明。

### 风险与待确认事项

以下为方案实施中需要关注的风险点和待确认事项：

- SharedWorkspace RPC 传输：SharedWorkspace 通过 DO RPC 代理转发调用，readFileBytes 返回的 Uint8Array 需要序列化。Cloudflare Workers 的 Durable Object RPC 支持 structured clone，Uint8Array 可直接传输，但大数据量（如 5MB 图片）的序列化开销需要验证。建议对超过一定大小的字节传输采用流式分块传输或使用 R2 预签名 URL 替代。
- base64 编码膨胀：图片/PDF 进行 base64 编码后大小膨胀约 33%。默认 5MB 的限制意味着原始文件最大约 3.75MB。对于高分辨率图片，可能需要在工具描述中提示模型使用压缩或缩略图。
- 模型上下文窗口：base64 编码后的 data URL 直接嵌入到模型上下文中。对于非常大的 PDF 文件，即使单页也可能超出部分模型的上下文限制。pagesEstimate 字段为模型提供决策参考。
- magic bytes 检测局限：仅通过首 512 字节检测文件类型，某些格式（如 ZIP 内嵌的特定格式）需要更深层次的检测。对于压缩包，当前方案不尝试解压后检测内容类型。
- mimeType 元数据准确性：Workspace 的 mimeType 默认值为 'text/plain'，如果写入时未正确设置，第一层判断将失效，依赖第二层 magic bytes 和第三层扩展名的回退机制。建议在 writeFileBytes 工具中增强 MIME 类型自动检测。
