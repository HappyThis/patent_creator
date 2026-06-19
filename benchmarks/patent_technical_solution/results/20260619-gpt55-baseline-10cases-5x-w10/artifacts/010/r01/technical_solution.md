## 技术方案

### 总体构思与工作链路

本方案在既有 agent workspace 文件工具之上增加“读取请求解析—文件元信息探测—内容可消费性判定—文本或多模态载荷构造—结构化结果返回”的统一链路。工作区仍通过 read、write、edit、list、find、grep、delete 等工具暴露给模型，其中 read 工具从仅返回文本行内容扩展为根据文件属性选择返回文本视图、模型可消费文件视图或不可消费文件说明。这样，模型在面对源码、Markdown、JSON 等文本文件时仍获得带行号的分页文本；在面对图片、PDF 等可由多模态模型理解的文件时，可以获得带媒体类型和受控数据引用的文件部件；在面对普通二进制或超限文件时，则获得文件大小、MIME 类型、路径、限制原因和建议处理方式。

该链路复用现有 workspace 的文件元信息和二进制读写能力。workspace 已保存 path、type、mimeType、size、createdAt、updatedAt 等文件属性，并支持 readFile、readFileBytes、readFileStream、stat、glob 等接口；小文件可内联存储，大文件可由 R2 承载，且 WorkspaceLike 允许真实 workspace、共享 workspace 或自定义代理后端以相同最小接口接入。本方案将多模态读取能力实现为工具层的能力增强，而不是改变底层持久化模型，从而保持已有文件写入、目录列举、搜索和删除行为稳定。

### 文件类型识别与读取策略

读取工具接收到路径、起始行和行数限制后，首先调用 stat 获取文件类型、媒体类型和大小，并校验路径存在性、目录/文件差异及符号链接解析结果。对于目录，工具返回“该路径为目录而非文件”的错误；对于不存在路径，返回文件不存在；对于文件，则进入类型识别。类型识别优先使用 workspace 元信息中的 mimeType，其次结合扩展名和少量文件头字节进行补充判断，以区分 text/*、application/json、application/xml、image/png、image/jpeg、image/gif、image/svg+xml、application/pdf、application/octet-stream 等类别。

读取策略分为三类。第一类为文本读取，沿用现有 read 工具的行号、offset、limit、最大行数、最大行长和截断提示，将文件拆分为行并返回 fromLine、toLine、totalLines 等字段。第二类为多模态读取，针对模型支持的图片、PDF 或可视化文档，在大小、媒体类型和模型能力均满足条件时读取字节内容并构造模型输入部件。第三类为降级读取，针对未知二进制、压缩包、视频、音频、加密文件、过大文件或当前模型不支持的类型，不读取完整内容，而返回结构化元信息和 cannotReadReason，避免把乱码或超大二进制塞入上下文。

为避免误判，文本识别不只依赖扩展名。对于未声明或声明为 application/octet-stream 的文件，可以在安全上限内抽样读取前若干字节，检查空字节比例、UTF-8 解码成功率和可打印字符比例；满足文本条件时进入文本路径，不满足时进入二进制降级。对于 SVG、Markdown 中内嵌数据、PDF 等边界类型，优先按媒体类型和模型能力表确定是否可作为文件部件传递，只有在需要保持现有文本体验且内容可安全解码时才按文本返回。

### 模型可消费内容的转换与返回

对于可被多模态模型消费的文件，工具将 workspace 字节内容转换为 AI SDK 消息部件可识别的形式，例如 type 为 file、mediaType 为 image/png 或 application/pdf、url 为受控 data URL 或内部可解析文件引用。若运行环境支持直接传递 Uint8Array 或 Blob，则可避免中间字符串膨胀；若只能使用消息流中的文件部件，则在大小阈值内生成 base64 data URL，并保留 filename、path、size、mediaType、source 为 workspace 等元信息。模型侧输出经过统一转换器进入 UIMessage parts，使既有聊天流已支持的 file、source-url、source-document 等部件可以承载多模态读取结果。

多模态返回结果与文本返回结果采用统一外壳：path 表示读取对象，kind 表示 text、modelFile 或 metadataOnly，mimeType 和 size 描述原始文件，truncated 表示是否因限制未完整传递，content 或 parts 承载实际可用内容。对于文本文件，content 仍为带行号的字符串；对于多模态文件，parts 包含一个或多个可发送给模型的文件部件，并可附带 caption 或说明文本提示模型该部件来自工作区路径；对于降级文件，parts 为空，metadata 中写入 unsupportedType、tooLarge、binaryDetected、missingBackendCapability 等原因。

当模型返回或中间流产生文件相关输出时，转换器将底层流块中的 mediaType、url、filename、sourceId、title 等字段映射为 UI 可持久化的消息部件，并保持与现有 stream accumulator 的处理逻辑一致。这样，多模态文件读取既可作为工具调用结果供模型继续推理，也可在前端消息历史中以文件卡片或来源文档形式展示，不需要为图片和 PDF 另建一套消息协议。

### 限制控制与多后端兼容

本方案设置多层限制控制。文本路径继续使用最大行数和最大行长控制上下文规模，并通过 offset、limit 分页读取。多模态路径设置单文件字节上限、单次工具调用总字节上限、可传递文件数量上限和媒体类型白名单；超过阈值时不读取完整字节，只返回文件信息和限制原因。搜索类工具继续跳过超大文件，并可进一步跳过非文本 MIME，防止 grep 对图片、PDF 或压缩包执行无意义解码。对于 R2 后端的大文件，优先根据 stat.size 在读取前拦截，只有满足阈值时才调用 readFileBytes 或流式读取，避免因先取全量对象造成资源浪费。

为兼容真实 workspace、共享 workspace 和自定义文件后端，读取能力被拆为最小能力集和增强能力集。最小能力集仍为 readFile、stat、readDir、glob 等 WorkspaceLike 接口，保证文本读取、列举和搜索不受影响；增强能力集通过可选的 readFileBytes 或 readFileStream 暴露二进制读取能力。工具执行时先进行能力探测：若后端提供字节读取，则可进入多模态转换；若后端只提供文本读取，则对图片、PDF 等返回 backendDoesNotExposeBytes 的结构化原因；若共享 workspace 通过 RPC 转发，则只要求其转发相同方法和元信息字段，不要求调用方知道真实文件存放在本地、SQLite 内联区还是 R2。

错误说明采用可机读且可向模型解释的结构，而不是仅抛出异常。返回对象至少包括 errorCode、message、path、mimeType、size、readMode 和 recoveryHint；例如文件过大时提示可缩小文件、分块导出或请求用户提供压缩预览，类型不支持时提示可转换为图片或文本，后端能力不足时提示需要启用字节读取接口。通过这种方式，模型既不会误把失败当成空文件，也能根据限制原因选择下一步操作，从而在保持原有文本文件读取体验的同时扩展对非纯文本文件的理解能力。
