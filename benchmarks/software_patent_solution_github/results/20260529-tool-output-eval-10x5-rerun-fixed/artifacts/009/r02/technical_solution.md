## 技术方案

本方案针对 Think agent 的 workspace 文件读取工具进行增强，解决当前 read 工具仅能处理文本文件、对图片、PDF 等非文本文件仅返回「不可读」或错误信息的缺陷。方案在保持现有文本读取体验（行号、分页、截断）不变的前提下，引入文件类型识别、读取策略分发、模型内容转换和大小限制控制等机制，使具备多模态能力的模型能够消费工作区中的图片和 PDF 文件，同时对无法转换的文件返回结构化原因。

### 1. 整体架构

方案在现有 Think agent 工具层与 Workspace 存储层之间引入一个文件读取适配层。该适配层复用 Workspace 已有的 stat() 和 readFileBytes() 能力，在 createReadTool 工具内部完成文件类型判定、策略选择和输出格式化，无需修改 Workspace 底层存储结构。

整体流程如下：read 工具接收到文件路径后，首先调用 stat() 获取文件的元数据（类型、MIME、大小）；然后根据类型进入不同的处理分支——目录返回结构化错误，文本文件沿用现有的按行读取与分页逻辑，图片与 PDF 等可转换类型通过 readFileBytes() 获取字节数据并转换为模型可接收的内容块，普通二进制文件及超大文件返回限制原因。ReadOperations 接口扩展以支持 readFileBytes 方法，Workspace、SharedWorkspace 及自定义后端均可通过实现该接口接入这套增强逻辑。

### 2. 文件类型识别机制

文件类型识别采用「声明优先、内容兜底」的两级判定策略，不单独依赖文件扩展名。

第一级：MIME 类型声明。Workspace 的 stat() 方法返回 FileInfo 结构，其中 mimeType 字段存储了文件写入时写入方声明或系统推断的 MIME 类型。对于通过 writeFile() 写入的文本文件，默认 mimeType 为 text/plain；通过 writeFileBytes() 写入的二进制文件可携带具体 MIME 类型（如 image/png、application/pdf）。read 工具从 stat 返回值中直接获取此字段作为初始分类依据。

第二级：内容特征检测。当 stat 返回的 mimeType 为通用值（如 application/octet-stream）或可能不准确时，read 工具对文件内容的前若干字节（魔数，magic bytes）进行检测。检测规则包括但不限于：PNG 文件以 89 50 4E 47 开头，JPEG 以 FF D8 FF 开头，PDF 以 25 50 44 46（%PDF）开头，GIF 以 47 49 46 38 开头，WebP 以 52 49 46 46 后接 WEBP 标识。检测仅在需要区分具体图片格式或确认 PDF 类型时触发，文本文件不触发内容检测。

最终分类结果将文件归入以下类别之一：文本文件（text/*）、图片文件（image/png、image/jpeg、image/gif、image/webp）、PDF 文件（application/pdf）、目录（type === 'directory'）、不存在（stat 返回 null）、普通二进制文件（无法识别且非文本的其他类型）。

### 3. 读取策略与分发

read 工具根据文件类型判定结果，进入不同的读取与输出分支。新增的 ReadOperations 接口在原有 readFile() 和 stat() 基础上增加 readFileBytes() 方法，使工具层可以获取文件的原始字节数据。

分支一：文本文件（mimeType 匹配 text/* 且内容检测未发现二进制特征）。沿用现有逻辑，通过 readFile() 获取字符串内容，按换行符拆分为行数组，支持 offset/limit 分页参数，逐行附加行号，单行超过 2000 字符时截断并标注 (truncated)。总输出行数超过 2000 行时截断并告知剩余行数。输出格式为包含 path、content、totalLines 及可选 fromLine/toLine 的结构化结果。

分支二：图片文件（mimeType 匹配 image/png、image/jpeg、image/gif、image/webp 或通过魔数检测确认）。通过 readFileBytes() 获取文件的完整字节数组，将字节数据编码为 base64 字符串，按对应 MIME 类型构造 data URL（格式为 data:{mimeType};base64,{encoded}）。输出结果为包含 path、mimeType、size、content（data URL 字符串）和 contentEncoding: 'base64' 的结构化内容块。对于支持多模态的模型，此 data URL 可直接作为图片内容被模型消费。

分支三：PDF 文件（mimeType 为 application/pdf 或通过魔数检测确认）。通过 readFileBytes() 获取 PDF 文件的完整字节数组，编码为 base64。输出结果为包含 path、mimeType、size、content（base64 编码字符串）和 contentEncoding: 'base64' 的结构化内容块，并附加 mediaType: 'application/pdf' 标识，供支持 PDF 输入的模型消费。

分支四：目录。stat 返回 type === 'directory' 时，不读取内容，直接返回结构化错误信息，包含 path、error（说明该路径为目录），并提示可使用 list 工具查看目录内容。

分支五：不存在。stat 返回 null 时，返回结构化错误信息，包含 path、error（文件不存在）和 reason: 'not_found'。

分支六：普通二进制文件。对于 mimeType 为 application/octet-stream 或其他非文本、非图片、非 PDF 的二进制类型，且魔数检测未匹配已知可转换格式时，返回结构化受限信息，包含 path、mimeType、size、error（说明该文件类型不支持内容传递）和 reason: 'unsupported_media_type'，同时提示文件的具体 MIME 类型和大小，供用户判断是否需要以其他方式处理。

### 4. 模型可消费内容的转换

图片和 PDF 文件在通过 readFileBytes() 获取原始字节后，需要转换为模型可接收的内容形式。方案采用以下转换策略：

对于图片文件，使用 data URL 方案（RFC 2397）。将原始字节通过 base64 编码后，构造 data:{媒体类型};base64,{编码内容} 格式的字符串。此方案的优势在于：无需外部存储或上传步骤，编码后的内容直接嵌入工具返回结果中；主流多模态模型（包括 GPT-4o、Claude 3.5 Sonnet 等）原生支持 data URL 格式的图片输入；无需修改 Workspace 的底层存储机制。

对于 PDF 文件，同样采用 base64 编码方案。返回结果中明示 mediaType 为 application/pdf，使支持 PDF 输入的模型（如 Claude 3.5 Sonnet）能够识别并处理该内容。对于不支持 PDF 输入的模型，模型自身会忽略或拒绝该内容块。

编码后的内容嵌入在工具返回结果的 content 字段中，与文本文件的格式化行号输出保持一致的返回结构。对于同时具备多模态和文本理解能力的模型，这些内容块可被模型直接作为上下文消费，无需额外工具调用或文件路径解析。

### 5. 大小限制与输出截断

为避免将过大的文件直接塞入模型上下文导致 token 消耗过大或超出模型输入限制，方案引入多级大小控制机制：

第一级：读取前大小检查。read 工具在调用 stat() 后，首先检查文件的 size 字段。对于图片和 PDF 文件，设定可读取大小上限（默认为 10 MB）。超过上限时，不读取文件内容，直接返回结构化限制信息，包含 path、mimeType、size、error 说明文件过大以及 sizeLimit 阈值，同时给出 reason: 'file_too_large'。

第二级：base64 编码后大小控制。图片和 PDF 经 base64 编码后体积约膨胀 33%。在编码完成后检查编码后字符串长度，若超过模型上下文安全值（如 20 MB 编码后内容），同样触发截断保护，返回限制信息而非内容。

第三级：文本文件的现有限制保留。文本文件维持现有的 MAX_LINES（2000 行）和 MAX_LINE_LENGTH（2000 字符）限制，超过部分截断并标注。这三项机制共同确保不会将超大文件不经控制地注入模型上下文。

读取限制阈值（图片/PDF 上限、base64 后上限）设计为可配置参数，允许不同部署场景根据模型上下文窗口和成本控制需求调整。

### 6. 多后端兼容机制

方案通过接口抽象实现多后端兼容。当前项目中存在多种 workspace 实现：基于 Durable Object SQLite + R2 的 Workspace（@cloudflare/shell）、跨 DO 代理的 SharedWorkspace、以及用户自定义的文件后端。方案在以下层面保证兼容性：

接口层：ReadOperations 接口从原有的 readFile() + stat() 两个方法扩展为三个方法——增加 readFileBytes(path: string): Promise<Uint8Array | null>。对于不具备字节读取能力的后端，readFileBytes 可通过 readFile() 的结果经 TextEncoder 编码回字节数组实现降级兼容，但仅对文本文件有意义；对于二进制文件，降级路径可能导致数据损坏，因此鼓励后端实现原生字节读取。

WorkspaceLike 类型扩展：Think 工具层的 WorkspaceLike 类型在原基础上增加对 readFileBytes 方法的选取。Workspace 原生已实现 readFileBytes（从 SQLite base64 列解码或从 R2 获取原始字节），因此可直接满足扩展后的接口要求。SharedWorkspace 等代理实现只需在 RPC 转发层增加对应方法的转发即可。

后端能力声明：方案设计一种可选的「后端能力」声明机制。后端可通过一个 getCapabilities() 方法返回自身支持的能力列表（如 'readFileBytes'、'streamRead' 等）。当后端不支持字节读取但遇到图片/PDF 文件时，read 工具可返回结构化限制信息，说明当前后端不支持二进制读取，而非抛出异常。此机制使 read 工具的行为随后端能力自适应调整。

这种接口抽象设计使得无论是真实的 Workspace、跨 DO 的共享 workspace 代理，还是基于内存的 InMemoryFs，只要实现了扩展后的 ReadOperations 接口，即可接入增强后的 read 工具。

### 7. 结构化错误与限制输出

所有非正常读取路径均返回结构化错误或限制信息，使模型能够根据具体原因采取下一步行动，而非仅获得一个模糊的失败提示。每种受限情况包含明确的 reason 字段：

- not_found：文件不存在，提示模型检查路径拼写或使用 find 工具定位
- is_directory：路径为目录，提示使用 list 工具查看内容
- file_too_large：文件超过大小限制，告知当前文件大小和阈值，模型可建议用户压缩或分段处理
- unsupported_media_type：文件类型不支持内容传递，告知具体 MIME 类型，模型可建议用户转换格式或通过其他途径提供
- backend_unsupported：当前后端不支持字节读取，模型可建议切换后端或使用其他方式访问
- read_error：读取过程发生 IO 错误，附带错误详情

每个错误输出统一包含 path、mimeType（如已知）、size（如已知）、error（人类可读描述）、reason（机器可读原因码）字段。这种结构使模型既能向用户解释发生了什么，也能根据 reason 字段进行程序化的条件判断和下一步决策。

### 8. 技术效果

本方案通过上述机制，在技术层面取得以下效果：

- 文件类型覆盖扩展：read 工具从仅支持文本文件扩展为支持文本、PNG、JPEG、GIF、WebP、PDF 等多种文件类型的内容读取与传递，使 agent 能够理解工作区中的多模态文件。
- 现有体验零退化：文本文件保持行号展示、offset/limit 分页、长行截断、总行数截断等全部现有功能，文本读取路径不受影响。
- 模型可消费性：图片通过 data URL、PDF 通过 base64 编码直接嵌入工具返回结果，模型无需额外工具调用即可消费文件内容。
- 安全防护：多级大小限制防止超大文件进入模型上下文，魔数检测防止基于扩展名的伪装攻击，目录和不存在路径给出明确的引导性错误而非崩溃。
- 后端解耦：通过 ReadOperations 接口和可选的后端能力声明，Workspace、SharedWorkspace、InMemoryFs 及自定义后端均可接入，无损兼容现有部署。
- 结构化可操作：每种受限情况均附带机器可读的 reason 字段，模型可以根据原因码进行条件判断和自动化的后续操作（如切换工具、建议用户处理等）。

### 9. 关键处理流程

增强后的 read 工具执行流程可归纳为以下步骤：

步骤1——元数据获取：调用 ops.stat(path) 获取 FileInfo，若返回 null 则输出 { error, reason: 'not_found' } 并终止。

步骤2——类型检查：若 type === 'directory'，输出 { error, reason: 'is_directory' } 并终止。

步骤3——MIME 类型判定：读取 stat 结果中的 mimeType 字段。若 mimeType 为 application/octet-stream 或其他需要进一步确认的类型，进入步骤4 进行魔数检测；否则直接使用 mimeType 进入步骤5 的分类路由。

步骤4——魔数检测（可选）：对通过 readFileBytes() 获取的字节前若干字节进行魔数匹配，修正或确认最终的文件类型分类。

步骤5——分类路由：根据最终类型进入对应分支——text/* 走文本分行分页逻辑；image/* 走 data URL 编码；application/pdf 走 base64 编码并标注 mediaType；其他类型走 unsupported_media_type 响应。

步骤6——大小检查：对于图片和 PDF 分支，检查文件 size 是否超过可配置阈值，超过则返回 { error, reason: 'file_too_large', size, sizeLimit }。

步骤7——内容读取与编码：文本分支通过 readFile() 获取字符串；图片/PDF 分支通过 readFileBytes() 获取字节并 base64 编码。

步骤8——输出格式化：文本文件输出带行号的格式化内容；图片输出 data URL；PDF 输出 base64 编码内容块。所有输出统一包含 path、mimeType 和 size 元数据。

### 10. 与项目代码的对应关系

方案与当前项目代码的关系如下：

核心改动位于 packages/think/src/tools/workspace.ts 中的 createReadTool 函数。当前该函数的 execute 回调仅处理文本文件的读取、分行、编号、截断逻辑。增强方案在此函数内部增加步骤3-6的类型判定与分支路由逻辑。

ReadOperations 接口（同文件第29-32行）需扩展 readFileBytes 方法声明。workspaceReadOps 工厂函数需同步返回 readFileBytes 的绑定。WorkspaceLike 类型（同文件第20-23行）需增加对 'readFileBytes' 的选取。

Workspace 类（packages/shell/src/filesystem.ts）已原生提供 readFileBytes() 方法（第569行），无需修改。stat() 方法（第500行）已返回包含 mimeType 的 FileInfo 结构，无需修改。MAX_STREAM_SIZE 常量（第191行，100MB）可作为文件大小上限的参考值。

魔数检测逻辑为新增代码，可置于 packages/think/src/tools/workspace.ts 内或抽取为独立的类型检测模块。大小限制阈值和 MIME 类型映射表建议作为 createReadTool 的可选配置参数暴露，允许不同 agent 按需定制。

### 11. 风险与待确认事项

以下为当前方案需要后续确认和关注的风险点：

- 模型兼容性：并非所有模型均支持 data URL 格式的图片输入或 base64 编码的 PDF 输入。方案将内容以结构化方式嵌入返回结果，由模型自身决定是否消费；但需要确认目标部署模型的具体多模态能力后再设定默认的图片/PDF 转换开关。
- base64 体积膨胀：base64 编码使内容体积增大约 33%，对于接近限制阈值的大文件可能导致编码后超限。方案已设计编码后二次检查机制，但值得评估是否对较大图片进行缩略图生成或分辨率降低等预处理。
- 魔数检测完整性：当前魔数检测覆盖常见图片格式和 PDF，但未覆盖 SVG（实质为 XML 文本）、TIFF、BMP 等格式。这些格式可按需扩展，但需权衡检测代码的维护负担。
- PDF 多页处理：当前方案将整个 PDF 文件编码传递，模型可消费但可能无法精细定位到特定页面。如果未来需要逐页读取，Workspace 层需要 PDF 解析能力。
- 图片元数据与 EXIF：当前方案直接传递原始图片字节，保留 EXIF 等元数据。对于隐私敏感场景可能需要剥离元数据后再传递。
- 灰度降级路径：对于不支持 readFileBytes 的后端，方案设计了降级路径（从 readFile 经 TextEncoder 回编），但这种降级对二进制文件无意义。需要在实际部署中明确后端能力要求文档。
