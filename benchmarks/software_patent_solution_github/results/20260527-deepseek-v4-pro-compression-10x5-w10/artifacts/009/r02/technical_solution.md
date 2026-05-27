## 技术方案

### 技术问题概述

在当前的 Think agent 工作区文件工具体系中，read 工具仅面向文本文件设计：通过 stat 获取文件元数据后调用 readFile 读取字符串内容，按行分割、添加行号并支持分页与截断。当工作区中包含图片（如 PNG、JPEG）、PDF 文档或其他非纯文本文件时，模型只能收到「文件不存在」或乱码字符串，无法有效理解文件内容。对于具备多模态能力的模型，这浪费了其图像理解能力；对于不适合模型直接消费的二进制文件，系统也缺乏结构化的反馈机制。

### 核心技术方案概述

本方案在现有 workspace read 工具的基础上引入文件类型感知的多模态读取能力。核心思路是：在 read 工具执行时，首先通过文件元数据与内容特征进行文件类型识别，将文件分类为文本、图片、PDF、普通二进制、目录或缺失等类别；然后根据类别执行差异化的读取策略，对文本文件保持现有的行号、分页和截断体验，对图片和 PDF 等可被多模态模型消费的文件，以模型可接收的内容块形式（如 base64 编码的 data URI 或结构化图片内容部分）传递；对大文件、普通二进制文件、目录、缺失文件等返回结构化的文件信息及限制原因。方案通过 WorkspaceLike 接口抽象实现多后端兼容。

### 文件类型识别机制

文件类型识别采用「元数据优先、内容特征补充」的双层判断机制，不单独依赖文件扩展名。

第一层：元数据判断。Workspace 后端的 stat 方法返回 FileInfo 结构，其中包含 type（file/directory/symlink）和 mimeType 字段。首先检查 type 字段：若为 directory，直接判定为目录类型，返回结构化目录信息而不尝试读取内容。若为 symlink，则解析至最终目标后重新判断。若为 file，进入第二层。

第二层：内容特征判断。基于 mimeType 字段进行初步分类——text/* 类型归为文本类，image/* 归为图片类，application/pdf 归为 PDF 类。对于 mimeType 为 application/octet-stream 或缺失的情况，通过读取文件头部魔数（magic bytes）进行内容探测：PNG 文件头为 89 50 4E 47，JPEG 为 FF D8 FF，PDF 为 25 50 44 46，GIF 为 47 49 46 38，WebP 为 52 49 46 46 等。若魔数匹配已知图片或 PDF 格式，修正类型判断；若无法匹配任何已知格式，判定为普通二进制文件。

Workspace 后端需提供 readFileBytes 方法（返回 Uint8Array）以支持魔数读取。当前 @cloudflare/shell 的 Workspace 类已实现该方法，从 SQLite 的 base64 编码内容或 R2 对象中读取原始字节。对于不支持 readFileBytes 的自定义后端，可降级使用 readFile 返回的字符串进行有限的内容检测，或依赖 mimeType 元数据作为唯一判断依据。

### 读取策略分发

read 工具根据文件类型识别结果，执行差异化的读取与输出策略。共定义六种文件类别及对应处理方式：

一、文本文件（text/* 及检测为文本的未知类型）。保持现有处理逻辑不变：（1）通过 readFile 获取字符串内容；（2）按换行符分割为行数组；（3）以「行号\t内容」格式输出每行，行号从 1 开始；（4）支持 offset 和 limit 参数进行分页读取；（5）单行超过 MAX_LINE_LENGTH（2000 字符）时截断并追加截断标记；（6）总行数超过 MAX_LINES（2000 行）时截断并附截断说明；（7）返回结果包含 path、content、totalLines，以及可选的 fromLine、toLine。

二、图片文件（image/png、image/jpeg、image/gif、image/webp 等）。通过 readFileBytes 获取原始字节，转换为 base64 编码字符串，组装为模型可接收的图片内容块。输出结构包含：（1）type: "image"，标识内容类型；（2）mediaType: 原始 MIME 类型；（3）data: base64 编码的图片数据；（4）文件名和文件大小等元信息。该结构适配 AI SDK 的 image content part 格式，多模态模型可直接消费。同时在前导文本中说明文件基本信息，兼顾用户可解释性。

三、PDF 文件（application/pdf）。通过 readFileBytes 获取原始字节。若模型支持 PDF 内容块输入，以 base64 编码的 PDF 数据块形式传递；若模型仅支持图片输入，可对 PDF 首页进行光栅化预览（该能力取决于部署环境是否具备 PDF 渲染库）。若均不支持，返回结构化元信息（文件名、大小、页数等）及「PDF 文件无法直接展示文本内容」的限制原因。

四、普通二进制文件。不传输文件内容，返回结构化的文件信息对象，包含：path、type（binary）、mimeType、size、以及 reason 字段说明「二进制文件，无法以文本或多媒体形式展示」。用户可根据返回信息决定是否需要通过其他工具处理。

五、目录。不传输内容，返回结构化信息：path、type（directory）、以及说明「该路径为目录，请使用 list 工具查看其内容」。与现有行为保持一致。

六、文件不存在。返回 error 信息：「File not found: {path}」。与现有行为保持一致。

### 模型输出转换机制

对于图片和 PDF 等需要传递给多模态模型的文件，read 工具负责将 Workspace 的原始字节数据转换为模型可接收的内容块格式。转换过程如下：

（1）通过 Workspace 后端的 readFileBytes 方法获取文件的原始字节（Uint8Array）。当前 @cloudflare/shell 的 Workspace 类已实现该方法：对于 inline 存储的文件，从 SQLite content 列中以 base64 解码还原；对于 R2 存储的文件，通过 r2.get().arrayBuffer() 获取。

（2）将原始字节转换为 base64 编码字符串。使用分块编码策略（每块 8192 字节）以避免大文件导致调用栈溢出，与 Workspace 现有的 bytesToBase64 实现保持一致。

（3）组装为 AI SDK 兼容的 content part 结构。对于图片文件，生成 { type: "image", image: base64Data, mimeType: "image/png" } 格式的内容块。对于 PDF 文件，生成 { type: "file", data: base64Data, mediaType: "application/pdf" } 格式的内容块。这些内容块嵌入到 read 工具的返回结果中，随工具调用结果一起传递给模型。

（4）同时输出人类可读的文本描述，包含文件名、媒体类型、文件大小等信息，确保在模型不支持多模态内容块时仍能获得基本的文件信息。

### 限制控制机制

为防止过大的文件直接塞入模型上下文导致 token 超限或性能劣化，read 工具实施多层大小限制控制：

（1）总文件大小上限。设置 MAX_READABLE_SIZE 阈值（默认 10 MB），对于超过该阈值的文件，不读取内容，直接返回结构化信息（文件名、MIME 类型、实际大小）及截断原因「文件过大（{size}），超过可读取上限（{max}），请使用其他工具分块处理」。

（2）图片文件专项限制。设置 MAX_IMAGE_SIZE 阈值（默认 20 MB），因为图片的 base64 编码会使传输体积增大约 33%，且多模态模型的图像输入通常有独立的像素/文件大小限制。超过该阈值的图片返回元信息及「图片过大」原因。

（3）文本文件截断。保持现有的 MAX_LINES（2000 行）和 MAX_LINE_LENGTH（2000 字符）截断策略不变。当行数超限时在输出末尾追加截断说明；当单行过长时追加「...(truncated)」标记。

（4）文件大小从 Workspace 后端的 stat 方法获取（FileInfo.size 字段），在读取内容之前即可判断，避免不必要的 I/O 操作。当前 Workspace 的 SQLite 表结构中 size 列在每次写入时同步更新，保证元数据准确性。

（5）阈值可配置。MAX_READABLE_SIZE 和 MAX_IMAGE_SIZE 作为 read 工具工厂函数的可选参数，允许不同 Agent 实例根据其模型上下文窗口大小调整限制。

### 多后端兼容机制

方案通过接口抽象实现多后端兼容，不绑定特定 Workspace 实现。

（1）ReadOperations 接口扩展。当前 createReadTool 通过 ReadOperations 接口消费后端能力，该接口定义了两个方法：readFile(path: string): Promise<string | null> 和 stat(path: string): Promise<FileInfo | null>。为支持多模态读取，接口扩展增加一个可选方法 readFileBytes(path: string): Promise<Uint8Array | null>。当后端提供该方法时，read 工具可执行魔数检测和二进制内容读取；当后端不提供时，工具降级为纯文本模式，仅依赖 mimeType 元数据进行类型判断。

（2）WorkspaceLike 类型扩展。@cloudflare/think 中的 WorkspaceLike 类型当前选取了 Workspace 的 readFile、writeFile、readDir、rm、glob、mkdir、stat 方法。扩展后增加 readFileBytes 的可选选取，保持向后兼容——已有的 Workspace 实例自动满足新接口，自定义后端可按需实现。

（3）多后端适配场景：（a）真实 Workspace（@cloudflare/shell）：完整支持 readFile、readFileBytes、stat，提供 SQLite 内联存储和 R2 溢出存储的透明字节读取。（b）SharedWorkspace 代理（跨 DO RPC 转发）：通过在 DO RPC 调用链中传递 Uint8Array（序列化为 ArrayBuffer）支持 readFileBytes。（c）自定义文件后端（如内存文件系统、Git 后端）：只需实现 ReadOperations 的三个方法，即可接入 read 工具的全部能力；若不实现 readFileBytes，自动降级。（d）只读后端：只需实现 stat 和 readFile/readFileBytes，无需实现写入相关方法。

（4）Workspace 后端的字节读取能力要求。为支持多模态读取，Workspace 后端需提供字节级别的读取能力。当前 @cloudflare/shell 的 Workspace.readFileBytes 已实现：inline 文件从 base64 编码的 content 列解码还原；R2 文件通过 r2.get(key).arrayBuffer() 获取。对于自定义后端，至少需要能从底层存储中读取原始字节并返回 Uint8Array，或提供等价的可随机访问的字节流。

### 关键处理流程

增强后的 read 工具执行流程如下（以 createReadTool 的 execute 函数为主线）：

1. 步骤一：参数校验。接收 path、可选的 offset 和 limit 参数。path 为必填的绝对路径字符串；offset 为 1-based 起始行号（仅对文本文件有效）；limit 为读取行数（仅对文本文件有效）。
2. 步骤二：文件存在性与类型初步检查。调用 ops.stat(path) 获取 FileInfo。若返回 null，返回结构化错误 { error: "File not found: {path}" }。若 stat.type === "directory"，返回 { path, type: "directory", message: "该路径为目录，请使用 list 工具查看其内容" }。若 stat.type === "symlink"，Workspace 后端的 stat 方法已自动解析符号链接至最终目标，返回的是目标文件的 FileInfo。
3. 步骤三：大小限制检查。检查 stat.size 是否超过 MAX_READABLE_SIZE。若超过，返回 { path, mimeType, size, type: "too_large", reason: "文件过大..." }，终止读取。
4. 步骤四：MIME 类型与魔数双重判断。基于 stat.mimeType 进行初步分类。若 mimeType 为 application/octet-stream 或缺失，尝试通过 readFileBytes 读取文件头部魔数（前 16 字节）进行内容探测。匹配已知图片/PDF 魔数时修正类型判断；无法匹配时分类为普通二进制。
5. 步骤五：按类别分发。根据步骤四的分类结果执行对应的读取策略——文本文件走行号格式化流程、图片/PDF 走模型内容块转换流程、二进制文件返回结构化元信息。
6. 步骤六：结果组装与返回。将步骤五的处理结果统一包装为包含 path、type、以及类别特定字段的结果对象返回。所有类别均至少包含 path 字段，确保模型和用户能追溯操作对象。

### 技术效果

（1）多模态文件理解能力。Agent 不再将图片和 PDF 视为不可读的二进制文件，而是将其转换为模型可直接消费的内容块，使多模态模型能够基于图片内容进行推理、分析和决策。这显著扩展了 Agent 在图像分析、文档理解、设计评审等场景中的可用性。

（2）保持文本读取体验不变。对于文本文件，行号展示、分页读取、长行截断和总行数截断等现有功能完全保留，不影响已有 Agent 的行为。offset 和 limit 参数仍仅对文本文件生效。

（3）结构化不可读反馈。对于目录、缺失文件、过大的文件、普通二进制文件等不可读场景，返回结构化的信息对象而非简单的错误字符串，使模型能够理解「为什么不可读」并采取替代策略（如使用 list 工具查看目录、使用其他工具处理二进制文件）。

（4）安全的上下文管理。通过多层大小限制（总文件大小上限、图片专项限制、文本行数/行长度截断），防止大文件撑爆模型上下文窗口，确保 Agent 在遇到大文件时不会因 token 超限而中断会话。

（5）后端无关的扩展性。通过接口抽象，方案适用于真实 Workspace、SharedWorkspace 代理、自定义文件后端等多种部署形态，不锁定特定存储实现。新后端只需实现 ReadOperations 接口即可接入。

（6）渐进式降级。当后端不支持 readFileBytes 时自动降级为基于 mimeType 元数据的纯文本模式；当模型不支持多模态输入时，至少返回文件的结构化元信息。系统在各种能力组合下均能提供合理的输出。

### 风险与待确认问题

（1）PDF 全文内容提取。当前方案中 PDF 以原始字节或首页光栅化形式传递。完整的 PDF 文本提取（如通过 pdf.js 解析）需要额外的运行时依赖，是否引入需权衡包体积与功能完整度。建议作为可选增强，通过 Workspace 后端的 PDF 解析插件机制按需加载。

（2）魔数检测的覆盖范围。当前魔数表覆盖 PNG、JPEG、GIF、WebP、PDF 等常见格式。BMP、TIFF、SVG（文本格式，但 mimeType 为 image/svg+xml）、HEIC 等格式需要逐步扩充魔数表。建议魔数表以配置形式提供，允许热更新。

（3）多模态模型的兼容性差异。不同模型提供商对图片输入的要求不同（如 OpenAI 接受 base64 data URI，Anthropic 接受 image content block，某些模型有像素分辨率限制）。方案需要在模型内容块组装层适配不同提供商的格式差异。当前可以通过 AI SDK 的抽象层部分解决。

（4）R2 大文件的字节读取性能。对于存储在 R2 中的大图片文件，readFileBytes 需要先通过 R2 网络请求获取完整文件内容再进行 base64 编码，可能导致工具调用延迟较高。可考虑引入流式 base64 编码以减少内存占用，或通过 HTTP Range 请求实现按需分块读取。

（5）base64 编码膨胀。图片文件经 base64 编码后体积增大约 33%，可能接近或超过模型的上下文限制。可考虑在工具层面对超限图片进行自动缩略图生成（需图像处理库支持），或返回图片 URL 而非内联数据（需额外的图片托管能力）。

（6）SharedWorkspace 代理的字节传输。跨 DO RPC 传输大文件的 Uint8Array 时，Cloudflare Workers 的 RPC 序列化可能存在性能瓶颈或大小限制。需验证 ArrayBuffer 在 DO RPC 中的传输上限。
