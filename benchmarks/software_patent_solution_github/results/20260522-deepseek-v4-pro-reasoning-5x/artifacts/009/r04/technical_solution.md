## 技术方案

本方案在现有 Think agent 的 workspace read 工具基础上，引入文件类型感知层、差异化读取策略、多模态输出转换层和多后端兼容机制，使 read 工具能够智能区分文本、图片、PDF 和普通二进制文件，并为多模态模型提供可直接消费的图像/文件内容块。以下各节从整体架构、核心问题、识别机制、读取策略、输出转换、限制控制、后端兼容、处理流程、关键模块和风险确认等方面展开说明。

### 整体架构

本方案在现有 workspace read 工具基础上，新增文件类型感知层和多模态输出转换层，使 read 工具能够根据文件的实际类型采用不同的读取策略和输出格式。方案保持对文本文件的现有处理逻辑不变（行号、分页、截断），同时为图片、PDF 等可被多模态模型消费的文件提供 base64 编码的多模态内容输出，对不可消费的二进制文件返回结构化的拒绝信息。

### 核心技术问题

当前 read 工具只通过 Workspace.readFile(path) 获取文本内容，对文件类型的判断仅限于 stat 结果中的 type 字段（file/directory/symlink），不区分文本、图片、PDF 或普通二进制文件。由此产生以下技术问题：

1. 文件类型无感知：所有非目录文件均按文本处理，图片和 PDF 被当作乱码返回，模型无法理解其内容。
2. 无字节读取能力：WorkspaceLike 接口和 ReadOperations 接口仅包含 readFile（返回字符串），缺少 readFileBytes（返回原始字节）方法，工具无法获取非文本文件的原始数据。
3. 输出格式单一：工具始终返回行号文本格式，无法为多模态模型提供 image_url 等内容块。
4. 大文件无保护：图片、PDF 可能非常大，当前仅对文本行数有截断限制（MAX_LINES=2000），缺少基于字节大小的总量控制。
5. 文件类型判断不可靠：仅依赖 mimeType（可能为默认值 "text/plain" 或 "application/octet-stream"），不足以准确区分文件类型。

### 文件类型识别机制

新增 classifyFile 函数，采用三级融合策略判定文件的实际媒体类型，输出分类标签：text、image、pdf、binary。

第一级——mimeType 优先匹配。Workspace 的 stat 操作返回 FileInfo，其中包含 mimeType 字段。当 mimeType 为明确的图片类型（如 image/png、image/jpeg、image/gif、image/webp）或 PDF 类型（application/pdf）时，直接采纳为分类结果。当 mimeType 为明确的文本类型（text/*、application/json、application/xml 等）时，直接判定为 text。

第二级——扩展名辅助判定。当 mimeType 为默认值 "text/plain" 或 "application/octet-stream"（表明写入时未指定具体类型）时，从文件路径中提取扩展名，对照扩展名映射表进行判定。映射表涵盖常见图片扩展名（.png、.jpg、.jpeg、.gif、.bmp、.webp、.svg）、PDF 扩展名（.pdf）以及常见文本/代码扩展名（.txt、.md、.ts、.js、.json、.html 等）。

第三级——magic bytes 仲裁。当 mimeType 与扩展名判定结果矛盾，或前两级均无法给出可靠结论时，通过 readFileBytes 读取文件头部字节（默认前 512 字节），与已知文件签名表比对。签名表覆盖 PNG（89 50 4E 47）、JPEG（FF D8 FF）、GIF（47 49 46 38）、BMP（42 4D）、WebP（52 49 46 46）、PDF（25 50 44 46）、ZIP/GZIP（50 4B 03 04 / 1F 8B）等常见格式。magic bytes 判定结果具有最高优先级。三种来源均无法判定时，归为 binary 兜底。

### 读取策略与分发

createReadTool 的执行流程在原有 stat 检查之后，插入 classifyFile 分类步骤，然后按分类标签分发到不同的处理路径。

文本文件路径（text）：完全保留现有逻辑。通过 Workspace.readFile(path) 获取字符串内容，按换行符分割为行数组，应用 offset/limit 分页偏移，为每行添加行号前缀（格式：行号\t内容），对超过 MAX_LINE_LENGTH（2000 字符）的单行尾部追加截断标记，对超过 MAX_LINES（2000 行）的输出追加截断说明。返回结构保持现有的 { path, content, totalLines, fromLine?, toLine? }。

图片文件路径（image）：先通过 checkSize 函数检查 stat.size 是否超过 MAX_READ_SIZE（默认 10MB），超限时返回结构化错误 { error: "file_too_large", size, maxAllowed }。未超限时，调用 Workspace.readFileBytes(path) 获取原始字节数据，将字节编码为 base64 字符串，构造 data URL（格式：data:{mimeType};base64,{base64Content}），输出格式为 { type: "image", mimeType, size, content: [{ type: "image_url", image_url: { url: dataUrl } }] }。该输出格式与 AI SDK 的多模态内容块机制对齐，多模态模型可直接消费。

PDF 文件路径（pdf）：与图片类似，先检查大小限制。读取字节数据并 base64 编码后，以 data:application/pdf;base64,... 的 data URL 形式输出。输出格式为 { type: "pdf", mimeType, size, content: [{ type: "image_url", image_url: { url: dataUrl } }] }。当模型不支持直接消费 PDF base64 时，可扩展为通过 PDF 渲染服务将 PDF 首页转为 PNG 后再编码输出。

普通二进制文件路径（binary）：不读取文件内容。直接返回结构化拒绝信息，包含 type、mimeType、size 和说明文本，如"二进制文件无法直接展示"，同时附带文件名和路径等元数据，保证用户可解释性。

目录路径和缺失路径：保持现有逻辑不变，分别返回 "is a directory" 和 "File not found" 错误。

### 多模态输出转换

多模态输出转换层负责将 Workspace 返回的原始字节数据转换为模型可消费的内容格式。转换策略按文件分类标签区分处理。

对于图片文件：通过 readFileBytes 获取 Uint8Array 原始字节，使用平台标准的 base64 编码器将字节转换为 base64 字符串，拼装为 RFC 2397 data URL。data URL 格式为 "data:{mimeType};base64,{encodedContent}"，其中 mimeType 取自 stat 返回的 FileInfo.mimeType（经过 classifyFile 确认为可靠值）。最终封装为 AI SDK image_url content block：{ type: "image_url", image_url: { url: dataUrl } }。

对于 PDF 文件：同样使用 base64 编码，但 mimeType 为 "application/pdf"。如果模型提供方不支持直接消费 PDF base64（即不支持 application/pdf 作为 image_url 的媒体类型），可降级为图片方案：使用 PDF 渲染库（如 pdf.js）提取首页渲染为 PNG 光栅图像，再按图片路径处理。降级策略通过配置开关控制，默认尝试原始 PDF 传递。

对于文本文件：不经转换层，直接使用 Workspace.readFile 返回的 UTF-8 字符串，保持现有格式化逻辑。

对于普通二进制文件：不经转换层，直接返回结构化拒绝信息。

### 限制控制机制

方案采用差异化限制策略，针对不同文件类型使用最合适的保护机制。

文本文件限制（保持现有）：通过 MAX_LINES（2000 行）和 MAX_LINE_LENGTH（2000 字符）两个常量控制输出规模。当文件行数超过 MAX_LINES 时，只输出前 MAX_LINES 行并在末尾追加截断说明。当单行超过 MAX_LINE_LENGTH 时，截断该行并追加 "... (truncated)" 标记。用户可通过 offset 和 limit 参数进一步控制读取范围。此机制已在实际使用中得到验证，无需修改。

非文本文件限制（新增）：引入 MAX_READ_SIZE 常量（默认 10MB），在读取图片、PDF 或二进制文件的字节数据前，先通过 stat 获取文件大小。当 size > MAX_READ_SIZE 时，不执行实际读取，直接返回结构化错误，包含当前文件大小和允许的最大值。此检查在字节读取之前完成，避免将超大文件加载到内存或传入模型上下文。对于图片文件，额外设置 MAX_IMAGE_SIZE（默认 5MB），因为 base64 编码会使数据膨胀约 33%，5MB 图片编码后约 6.7MB，仍在模型上下文可接受范围内。

目录递归保护：对于目录类型，不执行递归读取，直接返回错误提示该路径为目录。此行为与现有逻辑一致。

### 多后端兼容机制

方案通过接口扩展和运行时能力检测实现多后端兼容，使 read 工具能够适配真实 Workspace、共享 Workspace 代理、自定义文件后端等不同实现。

WorkspaceLike 接口扩展：在现有 WorkspaceLike 类型中新增可选的 readFileBytes 方法签名。方法声明为 readFileBytes?(path: string): Promise<Uint8Array | null>。由于是可选属性，现有 WorkspaceLike 的实现者（包括真实 Workspace、SharedWorkspace 代理等）不受影响，无需立即实现该方法。真实 Workspace（来自 @cloudflare/shell）已有 readFileBytes 实现，可直接提供字节读取能力。

ReadOperations 接口扩展：同步在 ReadOperations 接口中新增可选的 readFileBytes 方法，workspaceReadOps 工厂函数在创建 ReadOperations 实例时，检测 workspace 对象是否包含 readFileBytes 方法，若有则透传，若无则设为 undefined。

运行时能力检测与降级：createReadTool 在执行 classifyFile 后的分发阶段，当分类结果为 image、pdf 等需要字节读取的类型时，先检查 ops.readFileBytes 是否存在。若存在，按正常路径读取字节并转换输出。若不存在，返回结构化错误 { error: "unsupported_format", reason: "当前 workspace 后端不支持字节读取，无法处理 {mimeType} 文件。请使用支持 readFileBytes 的 workspace 实现。" }。此错误信息同时面向模型和用户可解释，模型可以根据提示采取替代方案（如提示用户手动查看文件）。

文本路径零影响：无论 readFileBytes 是否存在，文本文件的读取路径完全不变，始终使用 readFile 方法。这保证了对所有 WorkspaceLike 实现的向后兼容。

共享 Workspace 场景：在跨 Durable Object 的共享 Workspace 场景中（如父 agent 持有真实 Workspace，子 agent 通过 RPC 代理访问），代理对象只需在实现 WorkspaceLike 时提供 readFileBytes 的 RPC 转发，即可使子 agent 获得多模态读取能力。由于 readFileBytes 返回 Uint8Array，跨 DO 传输时需序列化为 base64 或 ArrayBuffer，由 RPC 层自动处理。

### 处理流程

增强后的 read 工具完整处理流程如下：

1. 接收参数：path（必填）、offset（可选，行偏移）、limit（可选，行数限制）。
2. stat 检查：调用 ops.stat(path) 获取 FileInfo。若返回 null，返回 "File not found" 错误。若 type 为 "directory"，返回 "is a directory" 错误。
3. classifyFile 分类：将 FileInfo（mimeType、name/path）传入 classifyFile 函数，综合 mimeType、扩展名和（必要时）magic bytes 判定文件类型，输出分类标签（text / image / pdf / binary）。
4. 路径分发：若标签为 text，进入文本读取分支（步骤 5）。若标签为 image 或 pdf，进入多模态读取分支（步骤 6）。若标签为 binary，进入二进制拒绝分支（步骤 7）。
5. 文本读取：调用 ops.readFile(path) 获取字符串，按行分割，应用 offset/limit，添加行号，执行行截断和总行数截断，返回格式化文本结果。
6. 多模态读取：先检查 stat.size 是否超过限制（图片 5MB、PDF 10MB），超限则返回错误。检查 ops.readFileBytes 是否存在，不存在则返回"不支持字节读取"错误。调用 ops.readFileBytes(path) 获取 Uint8Array，base64 编码，构造 data URL 和 image_url content block，返回多模态结果。
7. 二进制拒绝：不读取内容，直接返回包含 type、mimeType、size 和说明文本的结构化拒绝信息。

### 关键模块

方案涉及以下关键模块，均在 packages/think/src/tools/workspace.ts 中实现或扩展：

- classifyFile 函数：纯函数，输入 FileInfo（mimeType、name），可选输入字节读取器，输出分类标签。内部维护扩展名映射表（约 30 个常见扩展名）和 magic bytes 签名表（约 10 种文件签名），实现三级融合判定逻辑。magic bytes 仅在 mimeType 为默认值或与扩展名矛盾时触发，读取量限制在前 512 字节。
- checkSize 函数：纯函数，输入文件大小（字节数）和可选的最大字节限制，返回通过/拒绝判定。图片默认上限 5MB，PDF 和通用二进制默认上限 10MB。
- readImageContent / readPdfContent 函数：异步函数，调用 readFileBytes → base64 编码 → data URL 构造 → image_url content block 封装。两个函数结构相近，可通过 mimeType 参数统一为一个 readVisualContent 函数。
- readBinaryContent 函数：同步函数（不实际读取），直接构造结构化拒绝信息对象。
- createReadTool 重构：在 execute 函数中 stat 步骤之后插入 classifyFile → 分发逻辑。保持对工具输入 schema（path、offset、limit）和文本输出格式的完全兼容。

### 与项目环境的对应关系

本方案直接基于当前项目环境（Cloudflare Agents 项目）的现有架构设计，改动范围集中在 packages/think/src/tools/workspace.ts 和 packages/shell/src/filesystem.ts 的类型导出。

- Workspace 存储层（packages/shell/src/filesystem.ts）：已有 mime_type 列记录媒体类型，已有 readFileBytes 方法返回 Uint8Array，已有 readFileStream 方法返回 ReadableStream。方案直接复用这些能力，无需修改存储层。mimeType 在 writeFile/writeFileBytes 时写入，在 stat/readDir 时返回，提供分类的第一级数据来源。
- Think agent 工具层（packages/think/src/tools/workspace.ts）：createReadTool 是改动的主要位置。WorkspaceLike 和 ReadOperations 接口需新增可选的 readFileBytes 签名。classifyFile 等新增函数在同一文件中定义。createWorkspaceTools 的调用方式不变。
- Think agent 主类（packages/think/src/think.ts）：无需修改。_runInferenceLoop 中通过 createWorkspaceTools(this.workspace) 获取工具集，增强后的 read 工具自动生效。beforeToolCall、afterToolCall 等钩子不受影响。
- AI SDK 集成：工具返回的 image_url content block 格式与 AI SDK v4 的多模态消息协议对齐。streamText 在收到工具调用结果后，自动将 content block 注入后续模型请求的消息列表中。
- WorkspaceLike 接口的现有实现者：真实 Workspace（@cloudflare/shell）已实现 readFileBytes，无需额外适配。共享 Workspace 代理（如 examples/assistant 中的 SharedWorkspace）若未实现 readFileBytes，运行时检测会优雅降级为"不支持字节读取"错误。

### 风险与待确认问题

以下事项需要在实施前或实施过程中进一步确认和验证：

- AI SDK 工具结果中 image_url content block 的传递机制：需确认 streamText 在处理工具调用结果时，是否自动将 { content: [{ type: "image_url", ... }] } 格式的内容块注入后续模型请求。如果 AI SDK 仅支持特定字段名（如 experimental_toToolResultContent），需相应调整输出格式。
- 多模态模型对 PDF base64 的原生支持：GPT-4o、Claude 等主流多模态模型对 application/pdf 作为 image_url 媒体类型的支持程度不同。如果不支持，需实现 PDF→PNG 降级路径（通过 pdf.js 等渲染库提取首页）。
- magic bytes 签名表的覆盖范围：初版建议覆盖 PNG、JPEG、GIF、BMP、WebP、PDF、ZIP、GZIP 共 8 种签名。TIFF、SVG（基于 XML 文本，无需 magic bytes）、ICO 等是否需要在初版中覆盖，取决于实际使用场景。
- R2 大文件的分类效率：对于存储在 R2 中的大文件，readFileBytes 需要完整的 HTTP GET 请求。可优化为先用 HTTP Range 请求获取文件头部字节（如前 512 字节）完成分类，确认需要全量读取后再发起完整请求。此优化不影响功能正确性，仅影响大文件场景的响应延迟。
- readFileStream 的未来集成：当前方案使用 readFileBytes 一次性读取全部字节。对于超大 PDF 或多页图片场景，流式读取配合分页编码可能更适合。此优化作为后续迭代项。
