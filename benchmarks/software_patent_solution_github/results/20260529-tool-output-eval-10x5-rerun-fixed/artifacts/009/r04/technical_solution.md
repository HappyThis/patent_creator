## 技术方案

本技术方案针对 Think agent 的 workspace 文件读取工具（read tool）进行增强，使其能够识别并正确处理工作区中不同类型文件，包括文本文件、图片、PDF 及普通二进制文件，在保持现有文本读取体验（行号、分页、截断）的基础上，为具备多模态能力的模型传递可消费的文件内容，并为不可转换的文件返回结构化的限制信息。

### 技术问题

现有 workspace read 工具仅调用 Workspace.readFile(path) 获取字符串内容，对二进制文件（图片、PDF 等）直接返回乱码或报错。模型的 workspace 工具无法区分文件类型，无法将多模态内容传递给模型，也无法向用户解释限制原因。需要在保持文本文件的行号、分页、截断体验不变的前提下，实现文件类型识别、按类型分发读取策略、将可消费内容转换为模型接收格式、以及大小限制控制。

### 整体架构

方案在现有 createReadTool 的执行流程中，于 stat 调用之后、内容读取之前插入一个文件类型判定层（FileTypeResolver），根据 stat 返回的 mimeType 和文件内容特征将文件归类为：文本文件、图片文件、PDF 文件、目录、普通二进制文件或缺失文件。之后由读取策略分发器（ReadStrategyDispatcher）选择对应的处理策略，最终由输出转换器（OutputFormatter）生成结构化的工具返回结果。整体架构保持与现有 WorkspaceLike 接口的兼容性，新增能力通过扩展 ReadOperations 接口和引入独立的文件类型识别与格式转换模块实现。

### 文件类型识别机制

文件类型识别采用元数据优先加内容特征兜底的两级判定策略。第一级：从Workspace.stat返回的FileInfo中提取mimeType字段，Workspace在文件写入时记录mimeType，可直接识别text类型、image/png、image/jpeg、image/gif、image/webp、application/pdf等。第二级：当mimeType为通用类型如application/octet-stream或缺失时，读取文件头部魔数字节进行内容特征判定，如PNG的89504E47、JPEG的FFD8FF、PDF的25504446等。最终将文件归入文本、图片、PDF、目录、普通二进制或缺失六个类别之一。

### 读取策略分发与文本文件处理

ReadStrategyDispatcher根据FileTypeResolver的判定结果选择相应策略。文本文件：保持现有createReadTool的全部行为，调用ops.readFile获取字符串内容，按行分割、添加行号前缀、应用MAX_LINE_LENGTH=2000单行截断和MAX_LINES=2000总行数截断，返回path、content、totalLines及可选的fromLine/toLine。目录：直接返回结构化错误{path, type: directory, message}。缺失文件：返回{path, error: File not found}。普通二进制文件：调用ops.readFileBytes获取原始字节，返回{path, type: binary, mimeType, size, reason: unsupported_media_type}，不返回原始二进制内容。

### 图片与PDF的模型输出转换

对于图片和PDF等可被多模态模型消费的文件类型，方案通过OutputFormatter将文件内容转换为模型可接收的内容块格式。图片处理：通过Workspace.readFileBytes获取原始字节后，将字节转换为base64编码字符串，根据mimeType构造data URI格式（如data:image/png;base64,...），输出为包含type: image、media_type、data字段的结构化内容块。PDF处理：读取PDF原始字节后进行base64编码，输出为包含type: file、media_type: application/pdf、data字段的内容块。该输出格式可直接嵌入模型请求的user message的content数组中，作为多模态内容块被模型消费。

### 大小限制与截断控制

方案设置分层大小限制以防止过大的文件直接塞入模型上下文。文本文件：保持现有MAX_LINES=2000和MAX_LINE_LENGTH=2000限制。图片文件：设置MAX_IMAGE_BYTES上限（默认10MB），超出时返回{type: image, too_large: true, size, limit, reason}而非base64内容。PDF等文档文件：设置MAX_FILE_BYTES上限（默认20MB），超出时同样返回限制信息。此外，总工具输出大小受MAX_TOOL_OUTPUT限制保护。所有限制值均可通过ReadToolOptions配置，不同部署场景可按需调整。

### 多后端兼容机制

方案通过接口扩展实现多后端兼容。在现有WorkspaceLike接口基础上，新增ReadOperationsWithBytes接口扩展ReadOperations，增加readFileBytes方法签名。Workspace后端需提供字节读取能力：真实Workspace已有readFileBytes返回Uint8Array，同时支持readFileStream返回ReadableStream用于流式读取大文件。共享Workspace代理（如跨DO的RPC代理）需在其代理层同时转发readFileBytes调用。自定义文件后端只需实现ReadOperationsWithBytes接口即可接入。对于仅支持文本读取的旧后端，read工具降级为纯文本模式，对非文本文件返回{type: binary, reason: backend_unsupported}。

### 结构化输出格式

为兼顾模型可用性和用户可解释性，read工具的所有返回结果采用统一的结构化格式，包含公共字段：path（文件路径）、type（text/image/pdf/binary/directory/error）、mimeType（媒体类型）、size（字节数）。文本类型额外返回content、totalLines、fromLine、toLine、truncated。图片类型额外返回image_data（base64 data URI）或too_large标志。PDF类型返回file_data（base64编码）或too_large标志。错误类型返回error或reason字段说明具体限制原因，如unsupported_media_type、file_too_large、backend_unsupported、is_directory、file_not_found等枚举值。

### 处理流程

增强后的read工具执行流程如下：（1）接收path、offset、limit参数；（2）调用ops.stat获取FileInfo，若为空返回文件不存在错误；（3）若type为directory返回目录错误；（4）FileTypeResolver根据mimeType和魔数字节判定文件类别；（5）ReadStrategyDispatcher选择策略：文本走字符串读取加行号格式化；图片/PDF走字节读取加base64编码构造内容块；普通二进制返回类型信息不返回内容；（6）大小检查：超出配置阈值则返回限制原因；（7）OutputFormatter组装结构化输出返回。每一步的中间状态不暴露给模型，仅返回最终结构化结果。

### 风险与待确认问题

（1）多模态模型兼容性：base64 data URI的图片传递方式需要具体模型提供商支持，不同模型的content block格式可能存在差异，需在集成层做适配。建议引入模型能力探测机制，在工具注册时查询模型是否支持image/file内容块。（2）SVG文件歧义：image/svg+xml既是图片也是文本，当前方案按图片处理进行base64编码，但部分场景下模型可能更希望直接读取SVG源码。建议增加read工具的mode参数允许用户显式选择。（3）PDF多页处理：当前方案将PDF整体编码传递，大PDF可能粒度太粗。后续可考虑增加页面范围参数以支持按页提取。（4）流式读取：对于超大文件，Workspace已支持readFileStream，read工具未来可结合流式分块机制实现渐进式传递。
