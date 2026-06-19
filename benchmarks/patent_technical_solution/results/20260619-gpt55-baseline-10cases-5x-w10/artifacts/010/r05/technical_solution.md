## 技术方案

### 总体构思与工具接口扩展

本方案在现有 agent workspace 文件工具基础上增加“多模态读取”能力，但不改变文本读取工具已经形成的行号、分页、截断和长内容控制体验。其核心构思是：在读文件工具的执行链路中引入文件能力判定层、内容装配层和结果规范化层。工具接收路径、偏移和限制参数后，先通过 workspace 的 stat 能力取得类型、大小和 MIME 信息；对于目录、缺失路径或不可读取路径继续返回结构化错误；对于普通文本文件继续按既有逻辑读取字符串并生成带行号的文本片段；对于图片、PDF 等可由多模态模型消费的文件，则读取字节内容并转换为模型消息可接受的文件部件或图片部件，同时随返回值保留文件元数据、内容形态和截断状态。

该方案复用项目中已经存在的 workspace 抽象作为文件来源。现有 workspace 既能通过 readFile 返回文本，又能通过 readFileBytes 返回字节，并通过 FileInfo 保存 path、name、type、mimeType、size、createdAt 和 updatedAt 等元数据；同时支持 SQLite 内联存储与 R2 大文件外溢、符号链接解析、glob、目录分页和自定义 WorkspaceLike 代理。因此，多模态读取不直接绑定某一种存储实现，而是在 ReadOperations 中扩展可选的 readFileBytes 能力，并把 stat 返回的文件元数据作为分流依据，使真实 workspace、共享 workspace 或自定义后端只要满足相同能力面即可接入。

### 文件识别、读取分流与内容成形

文件识别采用“显式元数据优先、路径特征补充、字节特征兜底”的多级策略。第一优先级使用 workspace 写入时保存的 mimeType，例如 image/png、image/jpeg、application/pdf 或 text/plain；当 mimeType 缺失、泛化为 application/octet-stream 或与扩展名明显不一致时，再结合文件扩展名和少量文件头魔数进行校正。识别结果不只给出单一类型，还给出读取类别：text 表示可按行读取，multimodal 表示可直接传递给模型，binary 表示普通二进制降级，unsupported 表示类型已知但当前模型或策略不支持。

读取分流时，文本类别保持原有执行路径：调用 readFile 得到字符串，按换行符计算 totalLines，根据 offset 和 limit 截取目标行，逐行增加行号，并对超长行和超多行进行截断。多模态类别则调用 readFileBytes 获取原始字节，不再尝试把二进制强制解码为文本；系统根据 MIME 类型和大小将字节编码为可被模型消费的数据引用，例如 data URL、临时对象 URL 或消息文件部件。对于 PDF，可以整体作为 application/pdf 文件部件传入；对于图片，可以作为 image 部件传入；对于 SVG 等同时具有文本和图像属性的文件，可以优先按文本读取并在需要时提供图像化传递的可选分支。

为避免读取工具因二进制内容破坏现有文本体验，读取结果采用统一 envelope。该 envelope 至少包含 path、fileType、mimeType、size、contentKind、readable、reason 和 limits 字段；当内容为文本时包含 content、totalLines、fromLine、toLine 和 truncated；当内容为多模态文件时包含 mediaType、filename、bytes、modelPart 或 fileRef，并标识是否已传递完整文件；当内容为不可直接传递的普通二进制时，不返回原始二进制正文，而返回文件信息、类型判断、大小、限制原因以及建议操作。

### 模型输入转换与结果返回

模型输入转换层位于工具返回结果与 AI SDK 消息构造之间。现有消息构造器已经能够把 stream chunk 中的 file、source-document 等类型转换为 UIMessage parts，并携带 mediaType、url、filename 等字段；本方案在工具侧生成与该消息结构兼容的内容部件，使模型调用方可以把读取到的图片或 PDF 作为输入消息的一部分，而不是把二进制内容伪装为文本。转换时保留文件名、MIME 类型、路径和来源标识，便于模型在回答中引用文件，同时便于前端或日志系统呈现文件来源。

当模型支持目标 MIME 类型且文件未超过策略限制时，工具返回可直接消费的模型部件，并在 structured result 中声明 consumedByModel=true；调用方据此把该部件拼入下一次模型请求。若模型不支持该类型，或者运行环境不允许直接携带字节，则结果转换为“结构化文件摘要”：包括文件名、路径、MIME、大小、存储后端、可选的哈希或预览信息，以及不能消费的原因。这样，agent 即使无法理解文件内容，也能知道工作区存在该文件、文件属于何种类别以及为什么没有被直接读取，避免把“读取失败”误判为“文件不存在”。

### 限制控制、降级说明与多后端兼容

限制控制采用文件级、文本级和模型级三组阈值协同。文件级阈值根据 stat.size 判断是否允许直接读取字节，超过阈值时不加载全文而返回 too_large 限制原因；文本级阈值继续沿用最大行数、单行最大长度、offset 和 limit 控制，保证长文本不会一次性占满上下文；模型级阈值根据当前模型可接受的媒体类型、单文件大小、总附件大小和附件数量确定是否生成多模态部件。三组阈值的判断结果统一写入 limits 字段，使调用方能够区分“文件过大”“类型不支持”“后端未提供字节读取能力”和“模型不支持该媒体类型”等不同情形。

普通二进制降级不是简单报错，而是返回可用于后续决策的结构化信息。对于压缩包、可执行文件、数据库文件、未知二进制或超大文件，工具返回 path、mimeType、size、updatedAt、storageBackend、readable=false、reason 和 suggestedNextStep；对于字节读取过程中出现的 R2 配置缺失、对象不存在、符号链接循环或权限拒绝等错误，错误码和人类可读说明同时返回。该设计使 agent 能够根据限制原因选择列目录、查找同名文本说明、请求用户提供转换文件，或在具备专用工具时改用解析工具，而不会把二进制乱码注入模型上下文。

多后端兼容通过能力探测而非类型判断实现。真实 Workspace 可以直接提供 readFile、readFileBytes、stat、readDir 和 glob；共享 workspace 或跨 Durable Object 代理只需转发这些方法；自定义后端如果暂时只实现文本 readFile，也仍可按旧方式工作。工具初始化时根据 ops 是否具有 readFileBytes、stat 是否返回 mimeType 和 size、是否允许生成外部 URL 等能力决定可用分支；缺失能力不会破坏文本读取，而是在多模态请求中返回 backend_capability_missing。对于 SQLite 内联和 R2 外溢两种存储，读取层只依赖统一的 workspace 方法，由 workspace 内部负责从内联内容或 R2 对象取回字节，从而保持上层工具逻辑与具体存储位置解耦。
