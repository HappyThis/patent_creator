## 技术方案

本技术方案提出一种面向 agent workspace 的多模态文件读取方法，在保持现有文本文件读取体验（行号标注、分页偏移、截断控制）不变的前提下，扩展对图片、PDF 及其他非纯文本文件的理解能力。方案基于已有的 Workspace 持久化文件抽象层，新增统一的文件类型识别与内容转换流水线，使 agent 工具层能够根据文件的实际类型和可消费性，自动选择最合适的传递方式：对可直接被多模态模型消费的文件（如图片），转换为模型可接收的 base64 数据 URI 格式；对不适合直接传递的文件，返回结构化文件信息及限制原因。方案同时兼容真实 Workspace、共享 Workspace 及自定义文件后端。

### 总体架构

本方案的总体架构在现有 Workspace 文件抽象层与 agent 工具层之间插入一个多模态文件读取层。该层由三个核心模块组成：文件类型识别模块、多模态内容转换模块、以及统一的读取调度与降级模块。各模块通过已有 WorkspaceLike 接口与底层文件后端交互，不依赖特定后端实现。

文件类型识别模块复用并扩展已有的文件检测机制（基于魔数字节检测与扩展名映射），将文件分为：可读文本类（text/plain、application/json 等）、多模态可消费类（image/png、image/jpeg、image/gif、image/webp、image/svg+xml 等）、PDF 类（application/pdf）、以及其他二进制类（application/octet-stream 等）。文件类型判定以 mime_type 存储值为首要依据，扩展名检测为补充，魔数检测为最终兜底。

多模态内容转换模块负责将多模态可消费文件转换为适合传递给大语言模型的格式。对于位图类图片（PNG、JPEG、GIF、WebP），通过 readFileBytes 接口读取原始字节，按 data URI 规范编码为 base64 内嵌格式（如 data:image/png;base64,...）。对于 SVG 矢量图，因其本质为 XML 文本，可直接按文本内容返回。对于 PDF 文件，可提供文本提取尝试与二进制引用两种路径。对于其他无法被模型直接理解的二进制文件，返回包含 MIME 类型、文件大小、存储后端、限制原因的降级结构化信息。

统一的读取调度与降级模块是面向 agent 工具层的统一入口。该模块根据文件类型识别结果，路由到对应的内容转换策略，并统一施加大小限制、截断控制与错误处理。文本文件保持现有的行号标注、分页偏移（offset/limit）和行截断（2000 字符/行、2000 行总量上限）行为；非文本文件返回结构化结果对象，包含文件元信息、可选的转换后内容或降级说明。

### 文件类型识别

文件类型识别采用三级判定策略，逐级递进以保证识别准确性。

第一级：mime_type 字段优先。Workspace 在文件写入时已通过 writeFile/writeFileBytes 记录 mime_type 元数据（默认 text/plain，二进制写入默认 application/octet-stream）。读取时优先以该字段判定文件类别。当 mime_type 属于已知的图片 MIME 类型集合（image/png、image/jpeg、image/gif、image/webp、image/svg+xml、image/bmp、image/tiff 等）时，直接归类为多模态可消费文件。当 mime_type 为 application/pdf 时，归类为 PDF 文件。当 mime_type 为 application/octet-stream 或缺失时，触发第二级和第三级判定。

第二级：扩展名映射。基于文件路径提取扩展名，通过扩展名到 MIME 类型的映射表进行二次判定。映射表覆盖常见图片格式（.png、.jpg、.jpeg、.gif、.webp、.svg、.bmp、.ico、.tiff）、文档格式（.pdf）、以及已知文本格式。扩展名匹配到的 MIME 类型优先于 application/octet-stream，但优先级低于已有 mime_type 字段。

第三级：魔数字节检测。当以上两级均无法确定时，读取文件的前若干字节（如 512 字节），调用已有的 isLikelyText 检测逻辑：若字节序列中包含零字节或控制字符（ASCII 0-8），判定为二进制文件；否则归为文本文件。该检测复用了 @cloudflare/shell 中 detectFile 函数的现有实现。

三级判定结果最终将文件归入四个类别之一：（1）可读文本，按现有文本读取流程处理；（2）多模态可消费文件，进入内容转换流水线；（3）PDF 文件，进入 PDF 处理路径；（4）其他二进制文件，返回降级信息。

### 多模态内容转换

多模态内容转换模块针对不同文件类别提供差异化的内容处理策略，核心目标是将文件内容转换为大语言模型可直接消费的格式。

对于位图类图片（PNG、JPEG、GIF、WebP、BMP、TIFF 等），转换流程如下：首先通过 WorkspaceLike 接口的 readFileBytes 方法读取文件的原始字节内容——该方法已能正确处理 inline（base64 编码存储在 SQLite）和 R2 两种存储后端，返回统一的 Uint8Array。然后，将字节数组按标准 base64 编码，拼接 MIME 类型前缀生成完整的 data URI 字符串（格式为 data:{mime_type};base64,{base64_content}）。返回结果中包含 dataUri 字段、原始 MIME 类型、文件大小，以及用于模型上下文估算的 token 近似数量。

对于 SVG 矢量图，由于 SVG 本质上为 XML 文本，且其 mime_type 为 image/svg+xml，模块将其作为文本文件处理：直接通过 readFile 读取字符串内容，并返回文本格式结果。同时，模块也提供可选的 SVG-to-data-URI 转换路径，在需要以图片语义传递给模型时使用。

对于 PDF 文件，提供双路径处理策略。路径一（文本优先）：尝试从 PDF 二进制数据中提取可读文本内容。由于 Workers 运行时限制（无原生 PDF 解析库），该路径可通过调用外部 AI 模型（如 Cloudflare Workers AI）的 PDF-to-text 能力实现，或提供有限的 PDF 元数据提取（页数、作者等）。路径二（二进制引用）：当文本提取不可行时，返回 PDF 文件的结构化信息（MIME 类型、文件大小、存储后端、页数估算等），并在结果中标注 'unsupported_for_direct_consumption' 原因码。路径一和路径二的输出通过统一的读取结果结构返回，agent 可根据结果中的 contentAvailable 标志判断是否获得了可读内容。

对于其他二进制文件（如编译产物、压缩包、数据库文件等），模块不尝试内容转换，直接返回降级结构化信息，包括：文件路径、MIME 类型、文件大小、存储后端（inline/R2）、二进制标记（binary: true）、以及降级原因描述（如 'binary file of type application/octet-stream cannot be rendered as text or multi-modal content'）。该信息足以让 agent 了解文件的存在和基本属性，避免静默失败。

### 统一读取调度与降级

统一的读取调度模块作为 agent 工具层的唯一入口，对内屏蔽文件类型差异和内容转换细节，对外提供一致的调用接口和返回格式。

调度流程如下：第一步，通过 stat 操作获取文件元信息（类型、大小、MIME 类型）。若文件不存在，返回 'file not found' 错误。若路径指向目录，返回 'is a directory' 错误，与现有 read 工具行为一致。第二步，执行三级文件类型判定，确定文件类别。第三步，根据类别路由到对应的处理策略：文本类走现有行号标注与分页截断路径（支持 offset/limit 参数、2000 行总量限制、2000 字符/行截断）；多模态可消费类走内容转换流水线；PDF 类走双路径处理；其他二进制类走降级路径。第四步，统一构造返回结果对象。

返回结果采用统一的 JSON 结构，包含以下字段：path（文件路径）、type（文件类型：text/image/pdf/binary）、mimeType（具体 MIME 类型）、size（字节数）、storageBackend（inline 或 r2）。对于文本文件，附加 content（带行号的文本内容）、totalLines、fromLine/toLine（分页范围）。对于多模态文件，附加 dataUri（base64 编码的数据 URI）、encoding（固定为 'base64'）。对于 PDF 文件，附加 content（提取的文本，如有）、pages（页数估算，如有）、contentAvailable（布尔值）。对于二进制文件，附加 degradation（降级原因描述）、binary（固定为 true）。

该统一调度模块通过 WorkspaceLike 接口与底层文件系统交互，所需的最小方法集为：readFile（读取文本内容）、readFileBytes（读取原始字节）、stat（获取文件元信息）。任何实现了这三个方法的对象——包括 Workspace、共享 Workspace 的跨 DO 代理、InMemoryFs 适配器、或自定义文件后端——均可无缝接入。

### 内容大小控制与截断策略

为避免多模态文件内容过大导致模型上下文溢出或内存压力，方案在读取调度层统一施加多级大小控制策略。

第一级：多模态文件总体大小上限。设置 MAX_MULTIMODAL_SIZE 阈值（可配置，默认值 20 MB）。当文件大小超过该阈值时，跳过内容转换，直接返回降级结构化信息，并在结果中标注 'file_too_large_for_multimodal' 原因码及实际文件大小。此阈值独立于 Workspace 的 inlineThreshold（1.5 MB 的 SQLite 行内/R2 溢出阈值），作用于模型消费层面而非存储层面。

第二级：base64 编码后大小检查。由于 base64 编码会使数据体积膨胀约 33%，对于接近阈值上限的图片文件，编码后的 data URI 字符串可能显著增大。模块在生成 data URI 后检查最终字符串长度，若超过可配置的 MAX_DATA_URI_LENGTH 阈值（默认值 30 MB 字符长度），同样触发降级，确保传递给模型的内容不会过大。

第三级：文本文件保持现有限制。对于文本文件，复用现有 read 工具的截断策略：单行最大 2000 字符（超出部分截断并标注 truncated），总行数限制 2000 行（超出部分截断并给出提示），与原有行为完全兼容。同时，文本读取仍支持 offset 和 limit 参数实现分页读取，允许 agent 分段消费大文本文件。

第四级：流式读取预留。对于超大文本文件，方案预留了基于 readFileStream 的流式读取路径。当文件大小超过 STREAM_THRESHOLD（可配置，默认 5 MB）且为文本类型时，可通过流式接口逐块读取并分段返回，避免一次性加载全部内容到内存。该路径不影响现有工具的行为，作为可选增强。

### 多后端兼容设计

本方案的多模态文件读取能力通过 WorkspaceLike 接口与底层文件系统解耦，确保对不同后端实现的广泛兼容。

方案定义 MultiModalReadOps 接口作为多模态读取所需的最小操作集，包含三个方法：readFile(path: string): Promise&lt;string | null&gt;（读取文本内容）、readFileBytes(path: string): Promise&lt;Uint8Array | null&gt;（读取原始字节）、stat(path: string): Promise&lt;FileInfo | null&gt;（获取文件元信息）。该接口是 WorkspaceLike 接口的纯子集，无需底层实现任何新增方法。

对于真实 Workspace 后端（基于 Durable Object SQLite + R2）：readFile 自动处理 inline/base64 解码和 R2 对象获取，readFileBytes 返回统一 Uint8Array，stat 返回包含 mimeType、size、storageBackend 的 FileInfo。多模态读取层无需感知底层存储细节，R2 存储的大图片文件通过 readFileBytes 透明获取字节流。

对于共享 Workspace 后端（跨 DO 代理场景）：如 examples/assistant 中的 SharedWorkspace 代理，其将文件操作通过 Durable Object RPC 转发到父 agent 的真实 Workspace。由于代理实现了相同的 WorkspaceLike 接口，多模态读取层无需任何修改即可工作。RPC 序列化对 Uint8Array 的原生支持确保二进制数据传输的正确性。

对于自定义文件后端：任何实现了上述三方法接口的对象均可接入。例如，InMemoryFs（基于内存的虚拟文件系统）通过 WorkspaceFileSystem 适配器满足 FileSystem 接口，进而可通过适配层暴露 readFile/readFileBytes/stat 方法。方案不要求后端必须提供 SQLite 或 R2 能力，仅需提供基本的文件读写和元数据查询。

对于无 readFileBytes 能力的退化后端：部分简化后端可能只实现了 readFile（文本读取）而未实现 readFileBytes（二进制读取）。此时，多模态读取层在尝试读取非文本文件时会捕获方法缺失错误，自动降级为基于 stat 信息返回结构化元数据，确保不会因方法缺失而导致工具调用崩溃。

### 错误处理与可观测性

方案为多模态文件读取定义了结构化的错误与降级信息体系，确保 agent 在任何异常路径下都能获得可操作的回馈信息。

错误与降级分为三个层级。层级一（文件级错误）：沿用现有 read 工具的错误语义——文件不存在返回 'file not found' 错误、路径为目录返回 'is a directory' 错误、路径遍历越界返回安全拒绝信息。层级二（类型级降级）：当文件类型不支持直接内容传递（如 application/octet-stream 二进制文件）时，返回 degradation 对象，包含 reason 字段（机器可读原因码，如 'unsupported_mime_type'）、description 字段（人类可读说明）、以及完整的文件元信息（mimeType、size、storageBackend）。层级三（大小级降级）：当文件超过多模态处理阈值时，返回原因码 'file_too_large_for_multimodal' 及文件大小，同时仍包含完整的文件元信息，让 agent 了解文件存在但无法直接消费。

所有读取操作通过现有的 observability 通道（agents:workspace diagnostics channel）发布结构化事件，事件类型为 'workspace:read_multimodal'，包含字段：path、mimeType、fileSize、classification（text/image/pdf/binary）、action（converted/degraded/streamed）、duration、storageBackend。该通道仅在存在订阅者时激活，零订阅者时开销为零。事件发布独立于读取结果返回，不影响工具调用的响应延迟。

对于 R2 存储读取失败的场景（如 R2 对象被外部删除导致 r2.get 返回 null），多模态读取层捕获该异常并返回明确的错误信息，包含原始文件路径和 'r2_object_missing' 原因码，区别于 'file not found'（后者表示 SQLite 元数据中无记录）。同样，对于 inline 存储但 base64 解码失败的场景，返回 'content_decode_error' 原因码及文件路径。

### Agent 工具接口

多模态文件读取能力通过 AI SDK 工具的形式暴露给 agent，与现有 createWorkspaceTools 工具集并行存在，agent 可按需选择使用。

新增 createMultiModalReadTool 工具工厂函数，接受 MultiModalReadOps 操作接口，返回一个 AI SDK tool 定义。工具描述明确告知模型：该工具可读取文本文件（返回带行号的内容）、图片文件（返回 base64 data URI）、PDF 文件（返回提取的文本或结构化信息）、以及其他文件（返回元信息和降级说明）。工具的 inputSchema 接受以下参数：path（必填，文件绝对路径）、offset（可选，文本模式下的起始行号，1-indexed）、limit（可选，文本模式下读取的行数）。

参数设计保持了与现有 read 工具的完全一致性：path、offset、limit 三个参数的语义和类型完全相同。对于文本文件，工具行为与现有 read 工具一致——返回带行号的文本内容，支持分页和截断。对于非文本文件，offset 和 limit 参数被忽略（因为内容转换后不是按行组织），工具自动选择最合适的返回格式。这种参数兼容性确保 agent 在不确定文件类型时，可以统一使用同一个工具调用模式。

工具返回格式为结构化 JSON 对象，agent 通过返回对象的 type 字段判断文件类别，进而决定后续处理逻辑：若 type 为 'text'，按文本内容消费；若 type 为 'image'，将 dataUri 字段作为多模态消息的 image_url 内容块传递给模型；若 type 为 'pdf'，根据 contentAvailable 标志决定使用提取文本还是文件引用；若 type 为 'binary'，根据 degradation 信息向用户说明文件不可直接查看。

工具的 createWorkspaceTools 集成点：在 createWorkspaceTools 函数中，新增一个 'read_multimodal' 工具条目，与现有 read、write、edit、list、find、grep、delete 并列。agent 可在同一次 onChatMessage 调用中同时使用 read（纯文本）和 read_multimodal（多模态感知）两个工具，模型根据文件路径和上下文选择最合适的工具。

### 处理流程示例

以下描述一次完整的多模态文件读取数据流，从 agent 发起工具调用到获得结构化结果。

步骤 1：agent 通过 AI SDK 调用 read_multimodal 工具，传入 { path: '/images/screenshot.png' }。工具执行器接收参数后，首先通过 ops.stat(path) 获取文件元信息，确认文件存在且为普通文件（非目录）。

步骤 2：执行文件类型判定。stat 返回的 mimeType 为 'image/png'，直接命中多模态可消费类别。若 mimeType 不可靠，依次执行扩展名检测（.png → image/png）和魔数检测兜底。

步骤 3：检查文件大小。stat 返回的 size 为 450 KB，低于 MAX_MULTIMODAL_SIZE（20 MB）阈值，通过大小检查。若文件为 R2 存储，readFileBytes 会透明地从 R2 获取对象字节。

步骤 4：内容转换。调用 ops.readFileBytes(path) 获取 PNG 图片的原始字节 Uint8Array。将字节数组进行 base64 编码，拼接为 'data:image/png;base64,iVBORw0KGgo...' 的 data URI 字符串。检查 data URI 长度不超过 MAX_DATA_URI_LENGTH。

步骤 5：构造返回结果。返回 JSON 对象：{ path, type: 'image', mimeType: 'image/png', size: 460800, storageBackend: 'inline', dataUri: 'data:image/png;base64,...', encoding: 'base64' }。

步骤 6：agent 接收返回结果，识别 type 为 'image'，将 dataUri 包装为多模态消息的 image_url 内容块，发送给多模态模型进行理解和分析。

若同一流程中文件为 application/octet-stream 的二进制文件（如编译后的 .wasm 文件，2.3 MB），则在步骤 2 判定为二进制类别，步骤 3 通过大小检查但步骤 4 判定无需转换，步骤 5 返回降级结果：{ path, type: 'binary', mimeType: 'application/octet-stream', size: 2411724, storageBackend: 'inline', binary: true, degradation: { reason: 'unsupported_mime_type', description: 'Binary file of type application/octet-stream cannot be rendered as text or multi-modal content. File size: 2.3 MB. Consider using a specialized tool for WebAssembly files.' } }。Agent 据此向用户说明该文件无法直接查看。
