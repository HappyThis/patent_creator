## 技术方案

本方案提出一种面向 agent workspace 的多模态文件读取方法，在保持现有文本读取能力（行号、分页、截断、长内容控制）不变的前提下，使 agent 能够识别并消费工作区中的图片、PDF 等非纯文本文件，并对无法直接消费的二进制文件返回结构化信息与限制原因。方案通过文件类型感知路由、内容转换管线、限制策略控制和多后端适配层四个核心机制协同实现。

### 1. 整体架构

方案在现有 workspace 文件读取工具层之上引入一个多模态读取适配层。该适配层位于 ReadOperations 抽象接口与 AI SDK 工具定义之间，接收来自 agent 模型的文件读取请求，根据文件的 MIME 类型和读取模式进行路由决策，选择文本管线或多模态管线处理，最终将结果转换为模型可消费的结构化输出。适配层仅依赖 ReadOperations 接口中的 readFile、readFileBytes 和 stat 三个方法，因此与真实 Workspace、共享 Workspace 代理及自定义文件后端均保持兼容。

### 2. 文件类型感知路由

文件类型感知路由依赖 workspace 已有的 mime_type 元数据字段。当文件通过 writeFile 或 writeFileBytes 写入时，调用方可显式指定 MIME 类型（如 "image/png"、"application/pdf"）；未指定时默认值为 "text/plain"。方案在此基础上构建一个类型分类映射表，将 MIME 类型归入三个能力类别：

- 模型可消费类（model-consumable）：MIME 类型属于 image/png、image/jpeg、image/gif、image/webp、image/svg+xml、application/pdf 等，其内容可经转换后供多模态模型直接理解。
- 文本可处理类（text-processable）：MIME 类型以 text/ 开头，或属于 application/json、application/xml、application/javascript 等，沿用现有文本读取管线，输出带行号的文本内容。
- 仅结构化信息类（structured-only）：MIME 类型为 application/octet-stream、application/zip、application/gzip 等无法被模型直接理解的格式，仅返回文件元数据和限制原因，不传递原始内容。

分类结果在每次读取时动态判定：先通过 stat 获取文件的 MIME 类型，再查表确定类别，最后据此选择处理管线。对于 MIME 类型缺失或无法识别的情况，可通过文件魔数（magic bytes）探测进行补充判定——读取文件前 N 个字节，匹配已知文件签名（如 PNG 的 89 50 4E 47、PDF 的 25 50 44 46）以修正或补充 MIME 类型信息。

### 3. 模型可消费内容转换

对于被分类为模型可消费类的文件，方案构建一条独立的内容转换管线，将原始二进制内容转换为多模态模型可直接嵌入消息的标准化格式。

图片文件转换：通过 readFileBytes 获取文件的原始字节（自动处理 inline base64 解码和 R2 对象读取），计算其 base64 编码字符串，拼接为符合 RFC 2397 的 data URL（格式为 "data:{mime_type};base64,{encoded_content}"）。同时从文件元数据中提取 size，并可选择性地解析图片头信息以获取宽度、高度等维度数据（对于 PNG、JPEG、GIF、WebP 等常见格式解析其头部结构字段）。输出结构包含 type="image"、mime_type、data_url、size 和可选的 dimensions 字段。

PDF 文件转换：PDF 的转换采用分层策略。首先尝试提取 PDF 的页数、标题等元数据（通过解析 PDF 文件头中的交叉引用表和文档信息字典）。对于需要视觉理解的应用场景，可将 PDF 首页光栅化为图片后走图片转换管线。同时支持按页读取模式——agent 可通过指定页码参数获取特定页的渲染结果。在无法光栅化的降级路径中，尝试提取 PDF 中的文本流（解析内容流中的 BT/ET 文本块），返回结构化文本内容。输出结构包含 type="pdf"、page_count、pages（数组，每项含 page_number、text_content 和可选的 image data_url）。

### 4. 读取模式与输出格式控制

方案扩展文件读取工具（file_read）的输入参数，新增 mode 字段以控制输出行为。mode 支持三种取值：

- text（默认）：保持现有行为。对文本文件返回带行号的文本内容、totalLines、fromLine、toLine；对二进制文件返回错误提示并建议使用 auto 或 info 模式。
- auto：自动检测文件类型并选择最佳输出格式。图片文件返回 type="image" 及 data_url；PDF 文件返回 type="pdf" 及结构化页面数据；文本文件走 text 模式逻辑；不可消费的二进制文件走 structured-only 路径，返回类型标识和限制说明。
- info：仅返回文件的结构化元数据，不传输文件内容。返回字段包括 type（file/directory/symlink）、mime_type、size、model_consumable（布尔值，指示是否可被多模态模型消费）、consumption_hint（如 "image: can be consumed as data_url"、"binary: content not suitable for model consumption"）。

所有模式共享现有的 offset 和 limit 参数。对于 text 模式，offset/limit 按行号分页；对于 auto 模式下的图片文件，offset/limit 无实际意义但保留参数兼容性；对于 auto 模式下的 PDF，offset 可用于指定起始页码（以 1 为基准），limit 控制返回的最大页数。

### 5. 限制策略与降级处理

方案引入多层限制策略，防止大文件或不可处理文件对 agent 上下文窗口和推理性能造成负面影响。

文件大小限制：设置可配置的 max_consumable_size 阈值（默认 10 MB）。对模型可消费类文件，若其 size 超过该阈值，不执行内容转换，而是返回结构化降级信息：包含 type="file_too_large"、mime_type、size、max_allowed、suggestion（建议使用 info 模式查看元数据，或通过外部工具处理）。对文本文件，保持现有 MAX_FILE_SIZE（1 MB，用于 grep）和 MAX_LINES（2000 行）的截断机制不变。

二进制降级路径：对于 classified 为 structured-only 的文件，返回包含 type="binary_not_consumable"、mime_type、size、hex_preview（前 128 字节的十六进制转储，便于 agent 确认文件实际类型）和 reason（说明该 MIME 类型不支持模型直接消费）的结构化响应。hex_preview 的存在使 agent 可以通过魔数二次确认文件类型，避免因错误的 MIME 标注导致误判。

错误处理：当文件读取过程中发生 R2 不可用、base64 解码失败、内容损坏等异常时，返回明确的错误类型码和可操作的错误说明，而非空值或通用错误。错误类型码包括："r2_unavailable"（R2 存储不可用但文件需要从 R2 读取）、"decode_failed"（base64 解码失败，内容可能已损坏）、"unsupported_format"（MIME 类型在分类表中无匹配且魔数探测失败）。

### 6. 多后端兼容设计

多模态读取适配层的设计遵循依赖最小化原则，仅依赖 ReadOperations 接口中的三个方法：readFile（返回文本或 null）、readFileBytes（返回 Uint8Array 或 null）和 stat（返回 FileInfo，其中包含 mime_type 和 size 字段）。这使得适配层天然兼容以下三种后端形态：

- 真实 Workspace：通过 @cloudflare/shell 的 Workspace 实例直接提供 SQLite + R2 混合存储后端的文件访问能力。readFileBytes 自动处理 inline base64 解码和 R2 对象获取。
- 共享 Workspace 代理：在多 agent 协作场景（如 examples/assistant 中的 SharedWorkspace 模式）中，子 agent 通过跨 Durable Object RPC 代理访问父 agent 的 Workspace。代理只需透传 readFileBytes 和 stat 调用，适配层对代理透明。
- 自定义文件后端：实现 ReadOperations 接口的任意对象均可接入适配层。例如，可实现对本地文件系统、S3 兼容存储、内存文件系统等的后端适配。适配层不感知底层存储实现，仅通过接口方法获取数据和元数据。

对于不提供 readFileBytes 方法（仅提供 readFile 文本方法）的简化后端，适配层在初始化时检测后端能力：若缺少 readFileBytes，则对模型可消费类文件统一走降级路径，返回 binary_not_consumable 响应并注明"backend does not support binary read"。

### 7. 查找与搜索工具的协同增强

方案同步扩展文件查找（file_glob）和内容搜索（file_search）工具的输出信息，使 agent 在读取文件前即可获知文件的多模态可消费性。

file_glob（find）工具：在返回的每个匹配条目中增加 mime_type 和 model_consumable 字段。agent 可据此过滤和选择需要读取的文件，避免对不可消费文件发起无效的 auto 模式读取。同时保留现有的 truncated、count 等分页控制字段。

file_search（grep）工具：在现有按 glob 模式筛选文件后、逐文件搜索前，增加基于 MIME 类型的预过滤。对于 model-consumable 和 structured-only 类别的文件（即非文本文件），自动跳过而不尝试进行文本正则匹配。跳过的文件数量和原因汇总在结果中的 files_skipped 字段（扩展为结构化数组：每项含 file_path、mime_type、skip_reason）。这消除了当前 grep 工具对以 base64 编码存储的二进制内联文件执行无意义文本匹配的问题。

### 8. 典型执行流程

单次多模态文件读取的完整处理流程如下：

1. agent 模型发起 file_read 调用，传入 path="/logo.png"、mode="auto"。
2. 适配层调用 stat(path) 获取文件的 mime_type（"image/png"）和 size（245760 字节）。
3. 类型分类器将 "image/png" 映射为 model-consumable 类别。
4. 大小检查：245760 < max_consumable_size（10 MB），通过。
5. 适配层调用 readFileBytes(path)，Workspace 底层从 SQLite content 列（encoding=base64）解码得到原始 PNG 字节。
6. 图片转换器对字节进行 base64 编码，构造 data:image/png;base64,... 格式的 data URL。
7. 适配层返回结构化结果：{ type: "image", mime_type: "image/png", size: 245760, data_url: "data:image/png;base64,iVBOR..." }。
8. agent 模型将 data_url 作为多模态消息的 image 部分嵌入后续推理上下文。
