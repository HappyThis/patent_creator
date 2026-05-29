## 技术方案

本方案在现有 Think agent 的 workspace read 工具基础上，扩展文件类型识别、分级读取策略、模型输出转换、限制控制及多后端兼容机制，使 workspace 中的非纯文本文件能够被具备多模态能力的模型消费，同时保持对已有文本读取体验（行号、分页、截断）的完全兼容。

### 文件类型识别机制

workspace read 工具读取文件前，通过两级识别确定文件类型。第一级为元数据查询：调用 workspace 后端的 stat 方法，从 FileInfo 中获取已存储的 mimeType 字段和文件大小。该 mimeType 在文件写入时由调用方通过 writeFile/writeFileBytes 的 mimeType 参数设置，默认 text/plain 或 application/octet-stream。

第二级为内容特征检测：当第一级获得的 mimeType 为 application/octet-stream 或无法明确归类时，read 工具通过 workspace 后端提供的 readFileBytes 方法读取文件头部若干字节（如前 512 字节），与预置的魔数签名表进行匹配。该签名表维护常见文件类型的字节特征：如 PNG 文件的 89 50 4E 47 开头、PDF 文件的 25 50 44 46 开头、JPEG 文件的 FF D8 FF 开头、GIF 文件的 47 49 46 38 开头等。匹配成功则修正文件类型；匹配失败或不在签名表中时，归类为普通二进制文件。对于扩展名与内容特征不一致的情况，优先以内容特征为准。

### 分级读取策略

read 工具根据文件类型识别结果，采用分级策略决定如何读取和输出文件内容。该策略在现有的 createReadTool 函数内部扩展，核心逻辑位于 execute 回调中 stat 查询之后、readFile 调用之前。

第一级——文本文件：当类型识别结果为 text/*（如 text/plain、text/html、text/css、application/json、application/javascript 等），沿用现有文本读取路径。通过 ReadOperations.readFile 获取字符串内容，按换行符拆分，支持 offset/limit 分页参数，以 1-based 行号前缀格式化输出（格式为"行号\t内容"），行长超过 MAX_LINE_LENGTH（2000 字符）时截断并标记"... (truncated)"，总行数超过 MAX_LINES（2000 行）时截断并提示被省略行数。输出结构保持现有 path、content、totalLines、fromLine、toLine 字段不变。

第二级——可被多模态模型消费的文件（image/png、image/jpeg、image/gif、image/webp、application/pdf 等）：read 工具通过扩展后的 ReadOperations 接口新增的 readFileBytes 方法获取文件的完整字节内容（Uint8Array），将字节数据与对应的 mediaType 封装为模型可接收的内容块格式。具体格式取决于 AI SDK 的 content part 规范，如对图片封装为 { type: "image", mediaType: "image/png", data: base64String }，对 PDF 封装为 { type: "file", mediaType: "application/pdf", data: base64String }。该内容块直接嵌入工具返回结果，模型无需二次请求即可消费文件内容。同时，返回结果中附带文件名、媒体类型、文件大小等结构化元信息。

第三级——普通二进制文件（application/octet-stream 或经内容检测未匹配任意签名）：read 工具不读取文件内容，返回结构化信息块，包含文件名、路径、媒体类型、文件大小（格式化显示）、不可读原因（"Binary file: content cannot be displayed as text"）。同时返回提示信息，建议用户通过其他专用工具处理该类型文件。

第四级——目录和缺失文件：保持现有行为。stat 返回 type 为 "directory" 时，返回 { error: "{path} is a directory, not a file" }。stat 返回 null 时，返回 { error: "File not found: {path}" }。这两类结果不触发任何内容读取。

### 模型输出转换机制

read 工具的输出结构从单一的 { path, content, totalLines } 文本格式，扩展为区分内容类型的统一输出模式。工具返回结果增加 contentDisposition 字段，取值为 "text"、"media" 或 "meta"，分别对应文本内容、多模型媒体内容和仅元信息。

对于 contentDisposition 为 "text" 的结果，输出保持现有结构不变，包含 path、content（带行号的格式化文本）、totalLines、可选的 fromLine 和 toLine。

对于 contentDisposition 为 "media" 的结果，在保持 tool result 标准结构的同时，通过 AI SDK 的 experimental_toToolResultContent 钩子或等效机制，将 Uint8Array 字节数据和 mediaType 转换为模型可接收的 content part 数组。具体转换流程：通过 workspace 后端的 readFileBytes 获取原始字节 → 将字节编码为 Base64 字符串 → 根据 mediaType 构造对应的 content block。对于图片类型（image/*），封装为 { type: "image", mediaType, data: base64 }；对于 PDF 类型（application/pdf），封装为 { type: "file", mediaType, data: base64 }。返回结果同时附带人类可读的元信息，包括 path、mediaType、fileSize、encoding（"base64"）。

对于 contentDisposition 为 "meta" 的结果（对应普通二进制文件），输出不包含文件内容数据，仅包含结构化信息：path、mediaType、fileSize（人类可读格式如"1.2 MB"）、reason（如 "Binary file: content cannot be displayed as text"）、suggestion（如 "Use a dedicated tool to process this file type"）。对于目录、文件缺失及其他错误场景，维持现有的 { error: "..." } 格式。

### 限制控制机制

为防止过大文件直接塞入模型上下文导致 token 超限或性能问题，read 工具对所有非文本读取路径施加大小限制。该限制与现有的行数/行长限制机制并行，构成双层截断体系。

可配置的 MAX_MEDIA_SIZE 阈值（默认值建议为 20 MB）控制图片和 PDF 等媒体文件的最大读取大小。在通过 readFileBytes 获取字节数据之前，read 工具先通过 stat 获取文件的 size 字段。若 size 超过 MAX_MEDIA_SIZE，不执行字节读取，直接返回 { contentDisposition: "meta", path, mediaType, fileSize, reason: "File too large to include in context", maxAllowed: MAX_MEDIA_SIZE }。同时建议用户使用分块读取或专用工具处理。

对于文本文件，保持现有的双层限制：MAX_LINE_LENGTH（2000 字符/行）控制单行截断，MAX_LINES（2000 行）控制总行数截断。对于通过 Base64 编码传递的媒体内容，在编码后额外检查编码字符串长度是否超过 AI SDK 或模型上下文的内容块大小限制（该限制由具体模型和 SDK 定义，工具通过环境配置获取）。超出该限制时同样降级为 meta 输出。此外，在 R2 存储后端的场景下，通过 workspace 已有的 readFileStream 接口可支持大文件的分块读取，但当前版本不对流式媒体输出做模型内容块转换——该能力列为后续扩展点。

### 多后端兼容机制

本方案通过接口扩展而非实现绑定来支持不同的 workspace 后端，包括真实 Workspace（基于 DO SQLite + R2）、共享 Workspace（跨 DO 代理转发）及自定义文件后端。

核心改动是在 ReadOperations 接口中新增 readFileBytes 方法。现有 ReadOperations 仅包含 readFile(path: string): Promise<string | null> 和 stat。扩展后增加：readFileBytes(path: string): Promise<Uint8Array | null>。该接口由 workspaceReadOps 工厂函数实现，直接委托给 WorkspaceLike.readFileBytes。所有后端只需实现 WorkspaceLike 要求的 readFileBytes 方法即可使 read 工具获得字节读取能力。

对于真实 Workspace 后端（基于 DO SQLite）：readFileBytes 已实现完整，内部根据 storage_backend 字段路由：inline 时从 base64 编码的 content 列解码还原为 Uint8Array；R2 存储时通过 r2.get(key).arrayBuffer() 返回字节。该路径天然支持任意大小的二进制文件，无需额外开发。

对于共享 Workspace 后端（跨 DO 代理）：父 DO 拥有真实 Workspace 实例，子 agent 通过 RPC 代理转发调用。代理只需将 readFileBytes 调用转发到父 Workspace 的对应方法，RPC 框架自动处理 Uint8Array 的序列化与传输。WorkspaceFsLike 类型已包含 readFileBytes，共享 Workspace 代理只要实现了该类型的完整方法集即可通过编译时类型检查。

对于自定义文件后端：自定义后端只需实现 ReadOperations 接口的 readFileBytes 方法，从任意底层存储（本地磁盘、S3、内存等）读取并返回 Uint8Array | null。readFileBytes 返回 null 表示文件不存在，此时 read 工具走缺失文件错误路径。对于无法提供字节读取能力的极简后端（仅支持文本 readFile），可让 readFileBytes 抛出或返回 null，此时 read 工具降级为文本-only 模式，对非文本文件统一返回 meta 元信息，功能安全退化而不报错。

### 技术效果

本方案在不破坏现有文本读取体验的前提下，实现了以下技术改进：其一，通过两级文件类型识别（元数据查询 + 魔数签名匹配），使文件类型判断不依赖扩展名，可准确区分文本、图片、PDF 和普通二进制文件；其二，通过分级读取策略，使具备多模态能力的模型可直接消费 workspace 中的图片和 PDF 文件，无需额外的文件导出或路径传递步骤；其三，通过可配置的大小限制和双层截断体系，防止过大文件撑爆模型上下文窗口；其四，通过接口扩展（ReadOperations 增加 readFileBytes）而非实现绑定，使方案兼容真实 Workspace、共享 Workspace 代理和自定义文件后端；其五，对不支持的二进制文件、超大文件和缺失文件均返回结构化错误原因，兼顾模型可用性和人类可解释性。

### 风险与待确认问题

第一，魔数签名表的完整性与维护：当前方案依赖预置的魔数签名表进行内容特征检测，该表需要覆盖足够多的常见文件格式。对于未纳入签名表但实际可被模型消费的格式（如 SVG、WebP），可回退到扩展名辅助判断。签名表的维护策略（是否允许用户扩展、是否从外部配置加载）有待确认。

第二，Base64 编码膨胀：将 Uint8Array 编码为 Base64 会使数据体积膨胀约 33%。对于接近 MAX_MEDIA_SIZE 阈值的文件，编码后可能超出 AI SDK 内容块的大小限制。当前方案在编码后二次检查大小，超出则降级为 meta 输出。是否需要在工具参数中增加 maxSize 选项供用户按需调整，有待讨论。

第三，多模态模型的兼容性：不同模型提供商对 content part 的格式要求不一（如 Anthropic 使用 image 类型的 content block，OpenAI 使用 image_url 类型），输出格式需要与具体模型的 content part 规范对齐。当前方案通过 AI SDK 的抽象层（如 experimental_toToolResultContent）解耦模型差异，但该机制的稳定性和覆盖范围需要在实际接入中验证。

第四，RPC 传输开销：在共享 Workspace 场景下，readFileBytes 需要将整个文件的字节数据通过 DO RPC 从父 agent 传输到子 agent。对于超大文件（接近 MAX_MEDIA_SIZE），RPC 传输可能引入显著延迟。可考虑引入流式传输或按需分块策略优化，但这增加了跨 DO 通信的复杂度。当前方案将流式传输列为未来优化方向。
