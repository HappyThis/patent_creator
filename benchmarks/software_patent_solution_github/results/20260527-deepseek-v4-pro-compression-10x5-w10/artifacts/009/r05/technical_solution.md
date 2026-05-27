## 技术方案

本方案提出一种增强 workspace read 工具的方法，使其在保持现有文本文件读取体验（行号标注、分页、截断控制）不变的前提下，能够识别并处理图片、PDF 等非文本文件。核心思路是在现有 ReadOperations 接口中引入基于字节内容的文件类型检测能力（detectFile），并为 read 工具增加针对非文本文件的处理分支：当检测到文件为非文本类型时，通过 readFileBytes 获取原始字节数据，并根据具体 MIME 类型选择合适的转换策略（如图片 base64 编码、PDF 结构化描述等），最终将转换结果以模型可消费的格式注入对话上下文。方案同时引入可配置的大小限制、多后端兼容抽象层以及系统的异常返回策略，确保在不同 workspace 后端（内存、文件系统、R2 对象存储等）上均可一致工作。

### 整体架构

增强方案涉及三个核心层次：工具层（read 工具逻辑）、接口抽象层（ReadOperations 扩展）、以及后端实现层（Workspace 及各后端适配器）。工具层负责接收模型调用、执行文件类型判断和内容转换；接口抽象层定义统一的文件检测与字节读取契约；后端实现层提供具体存储介质的读写能力。

在现有架构中，Think agent（位于 packages/think/src/think.ts）通过 createWorkspaceTools 方法创建一组 workspace 工具，其中 read 工具由 createReadTool 函数（位于 packages/think/src/tools/workspace.ts）生成。createReadTool 接收一个 ReadOperations 对象作为依赖，该对象当前仅包含 readFile（返回字符串或 null）和 stat（返回文件元信息 FileInfo）两个方法。read 工具在被调用时，先调用 stat 获取文件信息（含路径、名称、类型、MIME 类型、大小等），再调用 readFile 读取文本内容，进行行号标注、分页和截断后返回给模型。

增强方案在保持上述架构层次不变的前提下，对 ReadOperations 接口进行扩展：新增 detectFile 方法用于基于内容的文件类型检测，新增 readFileBytes 方法用于获取文件的原始字节数据。read 工具在处理逻辑中增加非文本分支——当 stat 返回的 mimeType 表明文件可能为非文本、或 readFile 返回 null 时，通过 detectFile 进一步确认文件类型，并调用 readFileBytes 获取字节数据，经适当的转换策略后输出。

### 文件类型识别机制

文件类型识别采用"扩展名映射 + 字节内容检测"双重判断策略，以确保识别的准确性和健壮性。该机制基于项目中已有的 detectFile 函数（位于 packages/shell/src/extras.ts）及其辅助设施构建。

第一层：扩展名映射。系统维护一个 MIME_BY_EXTENSION 映射表，将常见文件扩展名（如 .jpg、.png、.pdf、.gif、.svg 等）映射到对应的 MIME 类型。当 read 工具通过 stat 方法获取 FileInfo 时，FileInfo 中已包含 mimeType 字段（该字段在文件写入 workspace 时由后端根据扩展名或内容检测结果填充并持久化到 SQLite 存储中）。mimeType 字段提供了文件类型的快速初步判断——若 mimeType 以 "image/" 或 "application/pdf" 开头，即可初步判定为非文本文件。

第二层：字节内容检测。当 mimeType 不足以确定文件是否可读时（例如未知扩展名、缺少 mimeType 信息、或 readFile 返回 null），系统调用 detectFile 函数进行基于内容的深度检测。detectFile 接收文件的原始字节（前若干字节的 Uint8Array）和文件名，内部先通过扩展名查表获得候选类型，再调用 isLikelyText 函数检查前 512 字节：若发现 null 字节（0x00）或除常见空白字符（\t、\n、\r）之外的控制字符（ASCII < 9），则判定为二进制文件。detectFile 返回的 StateFileDetection 对象包含四个字段：mime（确定的 MIME 类型）、description（人类可读的文件类型描述）、extension（标准化扩展名）和 binary（是否为二进制文件的布尔标记）。read 工具根据 binary 字段决定进入文本处理分支还是非文本处理分支。

### 增强的读取工具

增强的 read 工具保持现有 createReadTool 函数的整体结构（位于 packages/think/src/tools/workspace.ts），在其处理流程中增加非文本文件分支。工具的现有文本处理管线——包括行号标注（以 "行号|内容" 格式输出）、分页（按 MAX_LINES=2000 行分页展示）和行长截断（MAX_LINE_LENGTH=2000 字符）——完整保留，不做任何更改，确保向后兼容。

增强后的处理流程如下：第一步，调用 stat(path) 获取 FileInfo，包含文件大小、mimeType、类型（file/directory）等元数据。若 path 指向目录，直接返回目录列表而非文件内容。第二步，检查 FileInfo.size 是否超过可配置的大小限制阈值（默认建议值如 10MB），若超过则返回"文件过大"提示并附带文件基本信息。第三步，根据 mimeType 字段进行分支判断——若 mimeType 为文本类型（text/*、application/json、application/javascript 等）或未知类型，优先走现有文本路径：调用 readFile 获取字符串内容，按行号标注、分页、截断后返回。第四步，若 readFile 返回 null（表明后端判定该文件不可作为文本读取），或 mimeType 明确为非文本类型（image/*、application/pdf 等），则进入新增的非文本处理分支。

非文本处理分支的核心操作：首先调用 detectFile(bytes, fileName) 获取精确的 StateFileDetection 结果；然后调用 readFileBytes(path) 获取文件的完整字节数据（Uint8Array）；根据 detection.mime 选择合适的转换策略生成模型可消费的输出内容；转换完成后，将结果与文件元信息（名称、大小、MIME 类型、检测描述等）一起作为工具返回值输出，供模型在对话上下文中使用。

### 非文本文件的内容转换

当 read 工具进入非文本处理分支后，需将 readFileBytes 返回的 Uint8Array 原始字节转换为模型可消费的格式。本方案根据 detectFile 返回的 MIME 类型选择对应的转换策略，并在项目中预留可扩展的转换器注册机制。

对于图片类文件（MIME 类型以 image/ 开头，如 image/png、image/jpeg、image/gif、image/webp 等），采用 base64 编码策略：将 readFileBytes 返回的 Uint8Array 直接进行标准 base64 编码，构造为 data URI 格式（data:{mime};base64,{encoded}），作为图片内容块嵌入工具返回值。多模态模型可以直接消费该 data URI 中的图片数据进行视觉理解。转换结果附带图片的尺寸信息（若可从文件头解析获得）、文件大小和 MIME 类型描述。

对于 PDF 文件（application/pdf），readFileBytes 获取的是 PDF 文档的原始二进制字节流。在当前方案中，PDF 转换策略为返回结构化的文件描述信息，包括文件大小、页数（若可从 PDF 头部元数据解析获得）、MIME 类型以及"该文件为 PDF 格式"的提示，引导模型根据描述进行后续判断。同时预留 PDF 页面渲染与文本提取的扩展接口，以便在后续版本中集成 PDF 解析库实现页面级别的文本和图片提取。

对于 SVG 文件（image/svg+xml），由于其本质是 XML 文本，readFile 通常可直接返回其源代码。方案将其优先作为文本处理，同时保留 base64 编码作为备选路径。对于其他可识别但缺乏专门转换策略的非文本文件（如二进制数据库文件、编译产物等），系统返回包含文件类型描述、大小和二进制标记的元信息摘要，明确告知模型"该文件为二进制格式，无法直接展示内容"，同时提供文件的基本可识别属性。

### 大小限制与安全控制

增强方案引入分层的大小限制机制，在保留现有文本截断策略（MAX_LINES 行数限制和 MAX_LINE_LENGTH 行长限制）的基础上，增加针对非文本文件的字节大小限制。

文本文件的大小控制沿用现有机制：readFile 返回字符串后，按换行符分割，超过 MAX_LINES（2000 行）的部分截断并在末尾标注"已截断，显示前 2000 行"；单行超过 MAX_LINE_LENGTH（2000 字符）的部分截断并标注"此行已截断"。这些行为在增强方案中不做任何更改，确保文本读取体验前后一致。

非文本文件的大小控制通过新增的可配置阈值 MAX_READ_BYTES 实现（默认建议值为 10MB，即 10,485,760 字节）。在调用 readFileBytes 之前，read 工具检查 FileInfo.size 字段：若文件大小超过 MAX_READ_BYTES，则不进行字节读取和转换，直接返回包含文件名、大小、MIME 类型和"文件过大（超过 X MB 限制）"提示的结构化响应。若文件大小在限制范围内，则调用 readFileBytes 获取完整字节数据后进入转换流程。对于 base64 编码后的图片 data URI，还需检查编码后字符串长度是否超出模型上下文窗口的合理范围，若超出则降低图片质量或仅返回元信息。

MAX_READ_BYTES 阈值作为 read 工具的配置参数暴露，允许不同部署场景根据模型上下文窗口大小和内存资源灵活调整。同时，workspace 后端在实现 readFileBytes 时也应内置自身的读取上限保护，防止单次读取操作消耗过多资源。

### 多后端兼容机制

Cloudflare Agents 的 workspace 模块设计了多层接口抽象，以支持不同的存储后端（内存文件系统、本地文件系统、R2 对象存储等）。增强方案需要确保新增的文件检测和字节读取能力在不同后端上均可工作，同时兼容仅支持文本读取的旧版后端。

在接口层面，增强方案对 ReadOperations 接口（位于 packages/think/src/tools/workspace.ts，当前包含 readFile 和 stat 两个方法）进行扩展，新增两个可选方法：detectFile(path: string): Promise<StateFileDetection> 和 readFileBytes(path: string): Promise<Uint8Array>。这两个方法被设计为可选方法（在 TypeScript 中标记为可选属性），使得仅实现文本读取的旧版后端无需修改即可继续工作——当后端未提供 detectFile 或 readFileBytes 时，read 工具回退到现有文本处理逻辑。

在 WorkspaceFileSystem 适配器层（位于 packages/shell/src/workspace.ts），其底层 Workspace 实例（packages/shell/src/filesystem.ts）已具备完整的 readFileBytes 方法（直接返回文件的 Uint8Array 字节数据）和通过 StateBackend 间接提供的 detectFile 能力。WorkspaceFileSystem 在构造时接收 Workspace 实例，实现 ReadOperations 扩展接口时，将 detectFile 委托给 StateBackend.detectFile，将 readFileBytes 委托给 Workspace.readFileBytes。StateBackend 接口在 packages/shell/src/backend.ts 中定义，其 detectFile 方法签名为 (path: string) => Promise<StateFileDetection>，readFileBytes 方法签名为 (path: string) => Promise<Uint8Array>。

对于不同的后端实现，兼容策略为：FileSystemStateBackend（内存后端，位于 packages/shell/src/memory.ts）通过包覆 FileSystem 接口实现 detectFile 和 readFileBytes，利用 FileSystem.read 方法读取字节后调用独立的 detectFile 函数（位于 extras.ts）进行类型检测。对于 R2 等对象存储后端，StateBackend 实现类可直接利用对象存储的元数据（如 Content-Type 响应头）作为 mimeType 的快速来源，同时提供基于范围请求（Range Request）的 readFileBytes 实现以支持大文件的部分读取。对于不具备 readFileBytes 能力的旧版或简化后端，read 工具检测到缺失该方法后自动降级为纯文本模式，并在日志中记录降级信息。

### 异常情况处理策略

增强方案为 read 工具定义了系统的异常返回策略，覆盖文件读取过程中可能出现的各类边界情况，确保模型始终能获得有意义且可操作的反馈。

路径指向目录：当 stat 返回的 FileInfo.type 为 "directory" 时，read 工具不进行文件内容读取，而是调用 list 逻辑返回该目录下的文件与子目录列表，与现有行为保持一致。

文件不存在：当 stat 调用抛出文件不存在异常时，read 工具捕获该异常并返回明确错误信息"文件未找到：{path}"，不进行后续处理。

文件过大：当 FileInfo.size 超过 MAX_READ_BYTES 阈值时，返回结构化响应，包含文件名、大小（以人类可读格式如 "12.5 MB"）、MIME 类型以及提示"文件过大（超过 {threshold} MB 限制），无法读取。请使用更精确的路径或考虑拆分文件"。对于文本文件的情形，此检查在 readFile 调用之前进行，避免不必要的大文本加载。

无法转换的非文本文件：当 detectFile 返回 binary=true 且 mime 字段不属于已注册转换策略的 MIME 类型（如 application/octet-stream、application/x-msdownload 等）时，read 工具返回文件类型描述、大小和"该文件为二进制格式，当前不支持内容转换"的提示。此响应中包含 detection.description 和 detection.mime 字段，帮助模型理解文件性质。

readFileBytes 失败：当后端支持 readFileBytes 但实际读取失败（如权限不足、存储不可达）时，read 工具捕获异常并尝试降级——若 readFile 可正常返回字符串，则按文本处理并附加警告信息"无法以二进制方式读取，已按文本模式展示"；若 readFile 也失败，则返回"文件读取失败：{错误信息}"。

空文件：当 FileInfo.size 为 0 时，无论 mimeType 如何，直接返回"文件为空"，不进入任何内容读取或转换流程。
