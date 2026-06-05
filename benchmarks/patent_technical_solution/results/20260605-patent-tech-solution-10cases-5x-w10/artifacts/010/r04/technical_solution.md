## 技术方案

本方案提出一种面向 agent workspace 的多模态文件读取增强方法，在保持现有文本文件读取体验（行号标注、分页截断、长内容控制）完全不变的前提下，使 agent 模型能够直接理解和消费图片、PDF 等非纯文本文件的内容。方案通过文件类型分层识别、内容格式智能转换、限制控制与安全边界、多后端兼容四个核心机制协同工作，将 workspace 文件读取能力从单一文本域扩展到多模态域。

### 总体架构

本方案在现有 agent workspace 文件工具的基础上，构建一套多模态文件读取增强系统。该系统由文件类型识别与分类模块、多模态内容转换模块、文本内容读取模块、二进制降级处理模块和限制控制模块组成，通过统一的读取工具入口对外暴露，同时保持与现有文本读取体验的完全兼容。

核心技术构思是：利用 workspace 中已持久化的 mime_type 元数据作为文件类型标识的第一信号源，结合可配置的多模态可消费类型清单，在读取工具执行时进行内容路由——对文本类文件保持现有的行号标注、分页截断和长内容控制行为；对图片、PDF 等可被多模态模型消费的文件，通过 readFileBytes 获取原始字节后转换为模型可接收的内容格式（如 base64 数据 URI 或结构化内容描述）；对无法被模型直接理解和消费的二进制文件，返回结构化的文件元信息及限制原因，避免产生无意义的乱码输出。

### 文件类型识别与分类机制

文件类型识别是读取路由的前提。系统采用分层识别策略：第一层利用 workspace 中已存储的 mime_type 字段作为主信号源。Workspace 在文件写入时通过 writeFile 或 writeFileBytes 的 mimeType 参数记录文件的 MIME 类型，默认文本文件为 text/plain，二进制文件为 application/octet-stream。该字段持久化于 SQLite 表 cf_workspace_{namespace} 的 mime_type 列中，通过 stat 或 readDir 即可高效获取，无需每次读取时重新检测。

第二层为可配置的多模态可消费类型映射表。系统维护一份预置的 MIME 类型分类清单，将常见文件类型划分为三类：（1）文本类型，包括 text/*、application/json、application/xml、application/javascript、text/x-python 等，适用行号标注式文本读取；（2）多模态可消费类型，包括 image/png、image/jpeg、image/gif、image/webp、image/svg+xml、application/pdf 等，适用 base64 数据 URI 或结构化内容转换；（3）不可消费二进制类型，即其他未被前两类覆盖的类型，适用降级处理。该分类清单可通过配置扩展，以适配不同多模态模型的能力边界。

第三层为魔数回退检测。当 mime_type 为 application/octet-stream 或缺失时，读取工具读取文件的前若干字节（如 512 字节），通过魔数（magic bytes）匹配已知文件签名（如 PNG 的 89 50 4E 47、PDF 的 25 50 44 46、JPEG 的 FF D8 FF），推断真实文件类型并更新分类结果。

### 多模态内容读取与转换

当文件被识别为多模态可消费类型时，系统采用与文本读取完全不同的处理路径。核心处理流程为：首先通过 stat 获取文件的 mimeType 和 size，进行大小上限校验；校验通过后调用 readFileBytes 以 Uint8Array 形式读取文件的原始字节内容；然后将字节内容转换为多模态模型可接收的格式。

对于图片类文件（image/png、image/jpeg、image/gif、image/webp 等），系统将原始字节编码为 base64 字符串，按 data URI 格式构造为 "data:{mimeType};base64,{base64Content}" 形式，作为模型输入中的 image 类型内容块（content part）返回。这使得多模态模型能够直接解码并理解图像内容，而无需额外的外部存储或 URL 引用。

对于 PDF 文件（application/pdf），系统提供两种可选读取模式：一是将 PDF 整体以 base64 数据 URI 传递给模型（适用于原生支持 PDF 理解的多模态模型）；二是当模型不支持直接 PDF 消费时，将 PDF 的前若干页转换为图片后按图像路径处理。模式选择通过工具参数中的 readMode 控制。对于 SVG 文件（image/svg+xml），由于其本质为 XML 文本，除了按图像返回 base64 外，也可选择直接返回 SVG 源码供模型进行结构化分析。

读取结果在返回给模型时，附带结构化的元信息：文件路径、MIME 类型、文件大小、读取模式（multimodal）以及内容块类型标识，使得模型能够明确知晓它正在处理一个非文本文件，从而进行适当的推理和响应。

### 文本内容读取保持

对于文本类型文件，系统完整保持现有读取体验不变。读取工具继续通过 readFile 以字符串形式获取内容，按换行符分割后添加行号标注（格式为 "行号\t内容"），支持 offset 和 limit 参数实现分页读取。长行截断（单行超过 2000 字符时截断并标注 truncated）、最大行数控制（2000 行上限，超出部分截断并告知剩余行数）等已有控制逻辑全部保留。返回结果包含 path、content、totalLines、fromLine、toLine 等字段，与现有接口完全兼容。

文本读取路径与多模态读取路径在实现上互不干扰：文件类型识别结果决定路由走向；文本路径使用 readFile（内部通过 TextDecoder 处理 base64 编码的二进制存储），多模态路径使用 readFileBytes（直接获取 Uint8Array），两者共用同一套 stat 接口获取文件元信息。这种设计确保对纯文本文件的读取延迟、输出格式和用户体验没有任何回退。

### 二进制文件降级处理

当文件被识别为不可消费二进制类型（如 application/octet-stream、application/zip、audio/*、video/* 等）时，系统不尝试将内容传递给模型，而是返回结构化的降级信息。降级响应包含：文件路径、MIME 类型、文件大小、可读性标识（readable: false）以及不可读原因说明。原因说明根据具体情形分为多类："文件类型 {mimeType} 不是可读文本格式"、"文件为二进制格式且当前模型不支持直接消费"、"文件大小 {size} 超出多模态读取上限" 等。

降级响应同时提供可操作的建议信息，例如建议使用 file_glob 或 list 查看同目录下其他可读文件，或建议通过外部工具对二进制文件进行预处理后再放入 workspace。这种设计避免模型因收到无意义的乱码而产生错误推理，同时为模型提供了足够的上下文信息来调整后续行为。

### 限制控制与安全边界

多模态文件读取引入额外的资源消耗和上下文窗口压力，本方案在多个层面设置限制控制机制。第一层为文件大小上限：多模态读取设置独立的大小阈值（可配置，默认 20MB），超过该阈值的文件直接走降级处理，返回结构化元信息而不读取内容，防止大文件（如高清图片序列、大型 PDF）撑爆模型上下文窗口。该阈值与 workspace 自身的 inlineThreshold（1.5MB，控制 SQLite 内联与 R2 溢出的分界）独立，互不影响。

第二层为内容截断控制：对于 base64 编码后的内容字符串，系统在传递给模型前检查其总长度；当编码后内容超过模型上下文预留上限（可配置，如 500KB base64 字符串）时，对图片进行尺寸压缩或对 PDF 进行页数截取后重新编码。第三层为批量读取控制：当模型在一次工具调用中请求同时读取多个多模态文件时，系统累加所有文件编码后的大小，超过总量上限时按优先级排序截断，并返回被跳过文件的清单及原因。

安全边界方面，系统对所有文件路径进行归一化校验（防止 .. 遍历攻击），对 MIME 类型字符串进行白名单格式校验，对 base64 编码输出限制为仅包含合法字符集。读取过程发布结构化诊断事件（通过 node:diagnostics_channel 的 agents:workspace 通道），包含文件路径、MIME 类型、读取模式、大小和耗时，便于监控和异常排查。

### 多后端兼容机制

本方案的多模态读取增强完全基于现有的 WorkspaceLike 接口设计，不引入新的持久化存储或 Durable Object 依赖。WorkspaceLike 接口（定义于 @cloudflare/think/tools/workspace）仅要求 readFile、writeFile、readDir、rm、glob、mkdir、stat 七个方法，为方案的最小依赖集。具体到多模态读取，仅需 stat（获取 mimeType 和 size）和 readFileBytes（获取原始字节）两个方法。

此设计保证与以下三种后端实现完全兼容：（1）真实 Workspace——由 @cloudflare/shell 提供的 Workspace 类，基于 SQLite + R2 的完整实现，同时满足 WorkspaceLike 和 WorkspaceFsLike 接口；（2）共享 Workspace 代理——如 examples/assistant 中的 SharedWorkspace，通过 Durable Object RPC 将调用转发到父 DO 的真实 Workspace，因其实现了 WorkspaceFsLike 接口而天然支持 stat 和 readFileBytes；（3）自定义文件后端——任何实现了 stat 方法（返回包含 mimeType 字段的 FileInfo）和 readFileBytes 方法（返回 Uint8Array）的对象，均可作为读取工具的后端接入。系统通过 TypeScript 的结构类型系统（structural typing）而非名义继承来判断后端兼容性，无需显式注册或适配器包装。

对于仅实现了 WorkspaceLike（不含 readFileBytes）的后端，系统在运行时检测到缺少 readFileBytes 方法时自动降级：多模态文件走二进制降级路径返回结构化信息，文本文件走正常的 readFile 文本路径。此降级机制通过能力探测（capability detection）在工具创建时完成，后续每次读取无需重复判断。

### 可选实施方式与待确认边界

上述方案在实施时存在若干可选的扩展路径。在文件类型检测方面，可引入基于文件扩展名的辅助判断，作为 mime_type 缺失时的补充信号；在内容转换方面，对于不支持原生 PDF 理解的模型，可集成服务端的 PDF 转图像管线（如 pdf-to-image 转换器），将 PDF 各页转为 PNG/JPEG 后按图片路径处理；在压缩控制方面，可在读取工具参数中增加 maxWidth 和 maxHeight 选项，允许模型按需指定图片最大分辨率，系统在服务端进行等比缩放后再编码传输，从而在保证可读性的前提下减少上下文消耗。

几个关键边界需要在实际部署中根据模型能力确认：多模态可消费 MIME 类型清单的具体条目因模型而异（如 GPT-4V 支持 image/png、image/jpeg、image/gif、image/webp，Claude 额外支持 application/pdf），应由部署方按实际对接的模型配置；大文件阈值和 base64 编码上限应根据模型的上下文窗口大小和计费模型设定；对于特别大的 workspace 或包含大量多模态文件的工作区，索引和分类操作的开销需要通过缓存 mimeType 分类结果来优化。
