## 技术方案

本方案针对 Think agent 的 workspace read 工具进行增强，使其能够智能识别工作区中的多种文件类型，并根据类型选择合适的读取与转换策略，将文件内容以模型可消费的形式传递给多模态模型，同时保持现有文本读取体验（行号、分页、截断）。方案核心包括：三层递进的文件类型识别机制、基于类型的读取策略路由、模型内容块转换、大小与 token 限制控制，以及统一的多 workspace 后端适配层。

### 整体架构

系统在 read 工具内部引入五个核心模块协同工作：（1）后端适配层——抽象统一的文件操作接口，屏蔽不同 workspace 实现的差异；（2）类型识别模块——采用扩展名、magic bytes、系统 MIME 三层递进策略判定文件类型；（3）限制控制模块——基于文件大小和模型 token 预算执行硬限制与软限制；（4）读取策略路由——根据识别类型选择对应的读取与处理策略；（5）内容转换模块——将原始字节转换为模型可消费的结构化内容块。各模块以管道方式串联：后端适配层提供基础能力，类型识别模块决定路由方向，限制控制模块把关安全性，读取策略执行实际读取，内容转换模块完成最终输出格式化。

### 文件类型识别机制

类型识别采用三层递进策略，确保判定结果不受单一信息源误导。第一层（L1）：提取文件路径扩展名进行快速分流，将 .txt/.py/.json 等归入候选文本，.png/.jpg 等归入候选图片，.pdf 归入候选 PDF，但不作为最终裁决依据。第二层（L2）：通过后端适配层读取文件头部最多 512 字节，与预置的 magic bytes 签名表进行匹配。签名表覆盖常见格式：PNG（89 50 4E 47）、JPEG（FF D8 FF）、GIF（47 49 46 38）、PDF（25 50 44 46）、ZIP/DOCX 等。若头部字节全部落在可打印 ASCII 范围或通过 UTF-8 合法性校验，则判定为文本文件；否则归为普通二进制。第三层（L3）：查询 workspace 后端是否提供系统级 MIME 类型信息（如通过文件系统 xattr 或对象存储元数据）。若 L3 可获得 MIME，则以其为准覆盖 L1/L2 的判定结果。最终输出统一枚举值：TEXT、IMAGE、PDF、BINARY、DIRECTORY，并附 MIME 字符串。

### 读取策略与路由

读取策略根据 FileType 枚举进行路由，各类型处理方式如下。

- TEXT 文本文件：保持现有文本读取体验。通过 read_bytes 读取原始字节并以 UTF-8 解码；按换行符拆分为行数组，每行附加行号；支持 offset（起始行号）和 limit（最大行数）参数实现分页读取；单行超过 SINGLE_LINE_MAX_CHARS（默认 2000 字符）时截断并标记 truncated；整体文本超过 TOTAL_MAX_CHARS（默认 50000 字符）时截断并附加截断提示。
- IMAGE 图片文件：通过 read_bytes 读取完整文件字节，校验其 MIME 类型属于支持的图片格式（image/png、image/jpeg、image/gif、image/webp），将字节编码为 base64 字符串，传入内容转换模块构造 image content block。对于 SVG 格式（MIME 为 image/svg+xml），因其本质为 XML 文本，按文本方式读取并作为 text content block 输出。
- PDF 文件：通过 read_bytes 读取完整文件字节，编码为 base64，构造 file content block 或 document content block。可选地，若后端支持 PDF 文本提取能力，可附带提取的文本摘要嵌入输出中以辅助模型理解。
- BINARY 普通二进制文件：不读取完整内容。通过 read_bytes 读取文件头部最多 256 字节，生成十六进制预览（hex dump），返回结构化描述信息，包括文件类型标识、大小、hex 预览和不可读原因。
- DIRECTORY 目录：调用后端的 list_directory 接口，返回目录条目列表，每项包含名称、类型和大小。

### 内容转换机制

内容转换模块负责将各类型的读取结果包装为模型可直接消费的结构化输出。模块内部定义统一的中间表示 ContentBlock，包含 type 和对应载荷字段，最终由下游适配层根据具体模型提供商格式（如 Anthropic 的 content block、OpenAI 的 vision message）进行序列化。当前定义三种 ContentBlock 类型：

- text block：{ "type": "text", "text": "..." }，用于文本文件内容和目录列表的格式化输出。
- image block：{ "type": "image", "source": { "type": "base64", "media_type": "image/png", "data": "<base64>" } }，用于图片文件；media_type 从类型识别模块获取的 MIME 字符串填入。
- file block：{ "type": "file", "file_type": "pdf", "source": { "type": "base64", "media_type": "application/pdf", "data": "<base64>" } }，用于 PDF 等可被模型直接消费的非图片文件。

转换模块同时负责输出格式的统一封装。所有 read 工具的输出均包含通用元数据字段：path（文件路径）、type（类型枚举）、mime_type（MIME 字符串）、size（字节数）、readable（布尔值，标识模型是否可直接消费该内容）。对于文本文件，附加 lines 数组（每项含 line_no、content、truncated 字段）、total_lines 和 display_range。对于图片和 PDF，在内容可通过限制检查时附加 content_block 字段和 encoding 标记。对于不可读场景（二进制、过大、缺失等），附加 error、error_detail 和 suggestion 等字段。

### 限制控制机制

限制控制模块执行两级检查，防止过大文件或过长内容破坏模型上下文窗口或导致工具调用超时。

- 硬限制（Hard Limit）：在类型识别之后、实际读取之前执行。检查文件的原始字节大小是否超过 MAX_RAW_BYTES 阈值（默认 10MB，可通过配置调整）。若超过，直接拒绝读取，返回包含 error="file_too_large"、文件大小、max_allowed_bytes 和 suggestion 的结构化错误输出。
- 软限制（Soft Limit）：在内容编码或文本收集完成后执行。对于文本文件，检查解码后的总字符数是否超过 TOTAL_MAX_CHARS；对于图片和 PDF，检查 base64 编码后的字符串长度是否超过 TOKEN_BUDGET_PCT × 上下文 token 预算（默认不超过预算的 30%）。Token 估算采用近似方法：文本 token 数 ≈ chars/4（英文为主）或 chars×1.5（中文为主），base64 token 数 ≈ encoded_bytes/3。若触发软限制，文本执行截断并标记 truncated，图片/PDF 降级为结构化信息（不传递 content_block，返回文件名、类型、大小和降级原因）。

限制控制模块的阈值参数通过 read 工具的函数签名或全局配置注入，支持按 workspace 或按会话粒度调整。Token 预算需要由主 agent 调用 read 工具时作为可选参数传入，未传入时使用默认安全值（如 8000 tokens）。

### 多后端兼容机制

为兼容真实 workspace（本地文件系统）、共享 workspace（网络存储）及自定义文件后端（如对象存储、虚拟文件系统），系统定义统一的 WorkspaceBackend 抽象接口，包含以下方法：

- exists(path): boolean —— 判断路径是否存在。
- stat(path): {size, is_directory, mime_type?, modified_at?} —— 获取文件元数据；mime_type 为可选字段，由后端尽力提供。
- read_bytes(path, offset, limit): bytes —— 按字节范围读取文件内容；offset 默认为 0，limit 默认为文件大小。
- list_directory(path): [{name, type, size}] —— 列出目录条目；type 取值为 file 或 directory。

所有 workspace 后端实现必须提供上述接口。对于本身不具备字节读取能力的后端（如仅支持整文件读取的对象存储），适配层内部通过分段读取和缓存模拟 read_bytes 语义。真实 workspace 的后端实现直接映射到本地文件系统的 open/read/stat 系统调用；共享 workspace 后端通过 RPC 或 REST API 与远程存储通信；自定义后端由用户实现接口并注册到 workspace 工厂中。read 工具通过 workspace 工厂按名称或配置选择后端实例，对上层完全透明。新增 workspace 类型只需实现 WorkspaceBackend 接口并在工厂中注册，无需修改 read 工具的核心逻辑。

### Read 工具处理流程

read 工具的整体处理流程如下，从接收参数到输出最终结果共七个步骤。

- 参数校验：解析 path（必填）、offset、limit、token_budget 等参数，校验 path 合法性，拒绝路径遍历攻击（如包含 "../"）。
- 后端定位与存在性检查：通过 workspace 工厂获取当前活跃后端实例，调用 exists(path)。若不存在，返回 {path, error:"not_found"}。
- 元数据获取与目录判定：调用 stat(path) 获取文件大小和类型信息。若 is_directory 为 true，调用 list_directory 获取条目列表，包装为 text block 输出，流程结束。
- 类型识别：调用类型识别模块的三层递进策略，产出 FileType 枚举和 MIME 字符串。
- 硬限制检查：若文件大小超过 MAX_RAW_BYTES，返回 {path, type, mime_type, size, readable:false, error:"file_too_large", max_allowed_bytes, suggestion}。
- 按类型路由读取：（TEXT）调用 read_bytes 读取全部或按 offset/limit 指定的范围，UTF-8 解码，分行，附加行号和截断标记，执行软限制检查；（IMAGE/PDF）调用 read_bytes 读取完整文件，base64 编码，执行软限制检查——通过则构造 content_block，不通过则降级为结构化信息；（BINARY）调用 read_bytes 读取头部 256 字节生成 hex dump，返回结构化描述。
- 统一输出封装：将读取结果通过内容转换模块包装为最终输出结构，确保所有输出包含 path、type、mime_type、size、readable 等通用字段及类型特定字段。

整个流程中，任何步骤发生的异常（如权限拒绝、解码失败、后端超时）均被捕获并转换为结构化错误输出，而非抛出未处理的异常。错误输出包含 error 字段、error_detail 字段和可选的 suggestion 字段，指导模型或用户采取下一步行动。

### 技术效果

本方案预期的技术效果包括：（1）多模态感知能力提升——具备多模态能力的模型可以直接读取工作区中的图片和 PDF 内容，而不仅获得"不可读"提示，扩展了 agent 可处理的信息范围；（2）文本体验一致性——现有文本文件的按行读取、分页、行号标注和长内容截断能力完整保留，已有调用方式无需修改；（3）安全性与可控性——通过硬限制和软限制两级控制，防止过大文件破坏模型上下文或导致工具超时；不可处理的文件类型返回结构化说明而非报错；（4）可扩展性——WorkspaceBackend 接口将文件系统访问与工具逻辑解耦，新增存储后端仅需实现四个方法并注册，read 工具核心逻辑无需修改；（5）可解释性——所有输出均包含文件名、媒体类型、大小、可读性标记和错误原因，方便模型和用户理解文件的处理状态。

### 风险与待确认问题

以下为当前方案需要后续确认的关键风险和技术决策点：

- 源码对照：当前项目环境中未包含 Think agent 的 workspace 工具源码，方案的函数签名、现有 read 参数的命名和返回结构、workspace 后端的现有接口定义均基于需求推导。落盘前需对照真实源码确认兼容性，尤其是现有文本 read 的返回格式和 workspace 后端的实际方法签名。
- Content Block 格式适配：不同模型提供商（Anthropic、OpenAI、Google 等）对 image block 和 file block 的格式定义存在差异。方案中定义了中间表示 ContentBlock，但需要下游适配层按模型动态选择序列化格式。适配层的实现方式和触发时机需与主 agent 的消息构造逻辑协调。
- Token 预算传递机制：软限制依赖模型上下文 token 预算。需确认主 agent 是否能在调用 read 工具时传入当前剩余 token 预算，以及预算信息的获取方式（运行时注入 vs 全局配置）。
- Magic Bytes 数据库：方案描述的签名表为简化版本，生产环境建议集成成熟的文件类型检测库（如 libmagic / python-magic），覆盖更全面的签名并持续更新。
- PDF 多页处理：当前方案对 PDF 采用整文件 base64 编码方式，大 PDF（如数百页的文档）容易触发软限制降级。可考虑后续增加按页提取（通过 PyPDF2 等库）和按页返回的增强策略。
- 向后兼容：需确保旧的纯文本调用方式（不带 token_budget 参数、不期望 content_block 输出）仍然正常工作，不破坏现有 agent 的 read 调用行为。
