## 技术方案

### 总体构思

本方案在现有 agent workspace 文件工具的文本读取能力基础上，增加面向多模态文件的读取决策与传递机制。这里的“文件类型”指目录、普通文件、符号链接等文件系统状态；“MIME”指元数据记录或探测得到的媒体类型；“资源形态”指工具最终交给模型的文本、图片、PDF、通用文件引用或仅元数据形态；“多模态可消费类型”指同时被策略表和当前模型能力声明允许的图片、PDF 或其他文件格式；“普通二进制”指不宜直接进入模型上下文的压缩包、可执行文件或未知字节流；“受限制文件”指因权限、大小、模型能力、后端能力或安全策略不能按期望方式读取的文件。

读取工具接收到路径后，不直接假定文件均为可按行展示的文本，而是先取得文件元数据、权限状态和读取能力信息，再将目标文件划分为文本文件、多模态资源、仅元数据对象、阻断对象或错误对象。文本文件继续按既有行号和分页方式返回；图片、PDF 等多模态文件在满足策略、大小、模型能力和后端能力约束时作为结构化资源传递；不满足条件的文件返回统一降级结果。该机制复用 workspace 已保存的路径、MIME、大小、存储位置和更新时间等信息，并在底层支持字节或流式读取时复用相应能力，使 agent 能够通过同一读取入口明确获知文件是否被读取、以何种形式被读取以及未读取的限制原因。

### 文件类型识别与读取决策

读取决策层位于 workspace 工具入口与具体文件读取操作之间，其端到端时序为：接收 path、offset、limit、页范围、模型标识和上下文预算等读取参数；对 path 做规范化，消除重复分隔符并拒绝越过 workspace 根目录的路径；执行权限检查和符号链接解析策略，确认最终目标仍位于允许范围；调用 stat 获取类型、大小、MIME、存储后端和更新时间；对后端执行能力探测；按类型策略表进行 MIME、扩展名、文件头和文本可读性匹配；生成锁定的 decision 对象；再由文本、多模态或降级分支依据该 decision 执行；最终形成统一返回对象，并由消息转换阶段消费其中的文本或资源描述。

类型策略表将文件分为 text、multimodal、metadata_only、blocked 和 error 五类决策。匹配优先级为：显式安全或权限策略最高，其次是文件系统类型和 stat 错误，其次是文件头魔数，其次是可信 MIME，其次是扩展名，最后是文本可读性检测。若扩展名为文本但文件头包含图片、PDF、压缩包或可执行文件魔数，采用文件头结果；若 MIME 缺失而扩展名和可读性检测均指向文本，进入文本分支并记录 detectedBy=readability；若 MIME 声明为图片但魔数不匹配且可读性检测显示为文本，则降低 confidence 并进入 metadata_only 或文本分支，具体取决于安全策略是否允许。文件头读取失败时不覆盖已有 MIME 和扩展名判断，而是增加 reasonCodes=magic_unavailable 并降低 confidence。

decision 对象包含 normalizedPath、originalPath、fileKind、mimeType、size、storageBackend、decisionKind、detectedBy、confidence、matchedPolicy、limits、capabilities、reasonCodes、primaryReason、fallbackAllowed、requestedRange 和 traceId 等字段。其中 decisionKind 取 text、multimodal、metadata_only、blocked、error；detectedBy 取 mime、extension、magic、readability、policy、stat；capabilities 记录 stat、readFile、readFileBytes、readFileStream、rangeRead 和 remoteReference 是否可用；limits 记录最大字节数、最大页数、最大片段数、超时和上下文预算。decision 一经生成即作为后续分支的唯一依据，后续分支不得重新猜测文件类型，也不得覆盖 normalizedPath、fileKind、mimeType、detectedBy 和 matchedPolicy，只能追加读取结果、实际字节数、片段范围和附加原因码。

读取返回对象统一包含 decision、resultKind、included、reasonCodes 和 nextAction。文本结果还包含 content、totalLines、fromLine、toLine、truncatedLines 和 truncatedColumns；多模态资源结果包含 resources 数组，每个 resource 包含 resourceId、mimeType、byteLength、delivery、fragmentIndex、pageRange 或 frameRange、contentRef、expiresAt、accessScope、included 和 omittedReason；降级结果 degradedResult 包含 metadata、primaryReason、additionalReasons、requestedRange、includedRange、omittedRange、rangeReason 和 nextAction。统一字段使调用方不依赖自然语言判断读取状态，而是依据 decisionKind、included、delivery、reasonCodes 和 omittedRange 等枚举状态消费结果。

### 文本读取兼容机制

文本读取分支的前置条件是 decisionKind=text，且后端具备 readFile 或等价文本读取能力。该分支先根据 decision 中的编码判断结果读取字符串；未明确编码时，按 BOM、UTF-8、UTF-16 的顺序探测，若检测到 NUL 字节或不可打印字节比例超过策略阈值，则中止文本分支并生成 encoding_unreadable 或 binary_suspected 原因码。解码过程中记录替换字符或解码失败比例，超过阈值时不返回半乱码文本，而是转为 degradedResult；低于阈值时返回文本并附带 encodingWarning。

文本内容生成时统一将 CRLF、CR 和 LF 识别为换行边界，行号以规范化后的逻辑行递增，但返回内容保留原始可显示字符。offset 和 limit 只作用于逻辑行范围，最大行数用于限制单次返回的行数量，最大单行长度用于截断过长行；被截断的行在 content 中保留行号和前段字符，并在 truncatedColumns 中记录原始长度、保留长度和行号。若文件总行数超过返回范围，结果标记 included=partial，并返回 fromLine、toLine、totalLines 和 remainingLines。已经按文本分页返回的结果不得再被多模态封装改写 content 字段，以保证既有文本读取调用方兼容。

当文本读取过程中发生后端错误、实际大小超过 stat 记录、流式片段中断或编码状态与 decision 冲突时，分支保留原 decision 并追加 backend_error、size_mismatch、stream_interrupted 或 encoding_conflict 等原因码。若已形成完整的逻辑行范围，则返回 included=partial 并标明最后完整行号；若无法保证行边界完整，则丢弃不完整尾部片段并返回 degradedResult。该规则使“避免乱码”的效果由 MIME、文件头、可读性检测、不可打印字节阈值和解码失败比例共同保证，而不是依赖人工判断文件名。

### 多模态内容传递与消息转换

多模态分支的前置条件是 decisionKind=multimodal，并且同时满足策略表允许、MIME 或文件头匹配、模型能力支持、文件大小或页数未超过限制、后端提供 readFileBytes、readFileStream 或可访问资源引用、资源引用可被模型调用链访问。模型能力由当前推理配置、模型注册信息或运行时能力声明取得，并按模型标识和提供方版本缓存；缓存项包含支持的 MIME 集合、最大字节数、最大图片数量、最大页数和是否支持外部引用。每次调用前以当前模型标识和预算复核缓存，若模型标识变化、配置版本变化或能力声明过期，则重新获取。模型不支持时返回 model_not_multimodal 或 unsupported_mime，不得退回按文本读取图片、PDF 或其他已判定为多模态的二进制内容。

资源封装输出为 resources 数组。每个 resource 的 resourceId 由 workspace 标识、规范化路径、文件更新时间、片段序号和随机 nonce 生成，用于避免不同版本文件复用同一引用；contentRef 指向内联字节、临时对象引用或流式句柄；expiresAt 表示临时引用有效期；accessScope 限定为当前 agent 会话、当前工具调用或共享 workspace 授权范围。delivery 按以下规则选择：文件字节数和预算均低于内联阈值时使用 inline；文件位于可被模型调用链访问的对象存储且安全策略允许时使用 reference；文件较大但支持分块读取且模型接口允许流式输入时使用 stream。任一方式均不得把原始二进制写入文本 content 字段。

消息转换阶段只消费 included=true 或 included=partial 且资源引用未过期的 resource。delivery=inline 时，转换层把 contentRef 中的受控字节和 mimeType 映射为模型消息的图片、PDF 或文件片段；delivery=reference 时，转换层把 contentRef 映射为带访问权限的文件引用，并附带 mimeType、byteLength 和 resourceId；delivery=stream 时，转换层登记流式读取句柄并按片段顺序注入。转换前再次扣减上下文预算，预算不足时将相应 resource 标记为 omittedReason=context_budget_exceeded，并保持工具返回中的 partial 状态，使大型文档不会在消息转换阶段突破预算限制。

对于 PDF、多页图片、多帧图片或可拆分文件，读取工具先尝试读取目录、页表或容器头以获得 pageCount 或 frameCount；若元信息无法解析，则按流式读取顺序生成片段，并以 pageCount=unknown 或 frameCount=unknown 标记。用户指定范围与策略允许范围取交集作为 includedRange，超出的部分写入 omittedRange，rangeReason 取 range_out_of_bounds、page_limit_exceeded 或 frame_limit_exceeded。片段按页号、帧号和 fragmentIndex 稳定排序；部分页解析失败时，不丢弃已解析片段，而是返回 included=partial，并在对应片段或降级结果中记录 omittedReason=parse_failed。

用户未指定页范围或帧范围时，默认读取策略表中配置的起始安全范围，例如首若干页、首若干帧或首若干片段；用户指定范围完全超出文件实际范围时，不回退读取无关默认范围，而是返回 included=false、requestedRange、omittedRange 和 rangeReason=range_out_of_bounds；用户指定范围部分有效时仅读取交集并返回 included=partial。由此，范围裁剪、片段排序和 partial 返回机制共同防止大型文件一次性占满上下文，并让 agent 能够准确知道哪些页面或片段已进入模型。

### 降级返回、限制控制与错误说明

当 decisionKind 为 metadata_only、blocked 或 error，或读取分支在执行中触发不可恢复限制时，工具返回 degradedResult。degradedResult 包含 metadata、primaryReason、additionalReasons、requestedRange、includedRange、omittedRange、rangeReason、failedStage、retryable 和 nextAction。原因码按优先级确定主原因：权限和安全阻断高于路径不存在和目录类型，路径和类型错误高于后端能力缺失，后端能力缺失高于模型能力不支持，模型能力不支持高于文件大小或页数超限，超限高于编码不可读，编码不可读高于解析失败和上下文预算不足。所有同时触发的原因均进入 additionalReasons，主原因用于决定默认 nextAction。

nextAction 为可枚举动作，包括 list_directory、request_smaller_range、convert_to_text、compress_image、use_text_read、upload_supported_format、enable_bytes_read、switch_multimodal_model、retry_later 和 inspect_metadata。is_directory 对应 list_directory；too_large、page_limit_exceeded 或 context_budget_exceeded 对应 request_smaller_range 或 compress_image；unsupported_mime 对应 upload_supported_format 或 convert_to_text；bytes_unavailable 对应 enable_bytes_read；model_not_multimodal 对应 switch_multimodal_model；encoding_unreadable 对应 convert_to_text 或 inspect_metadata；backend_error 且 retryable=true 时对应 retry_later。该枚举动作使 agent 的后续选择来自机器可读状态，而不是依赖自然语言提示。

限制控制在读取前、读取中和消息转换前三个位置执行。读取前检查最大直接传输字节数、允许 MIME 白名单、最大页数、最大片段数和后端能力；读取中检查实际字节数、远端对象读取超时、流式中断和元数据大小与实际读取大小不一致；消息转换前检查模型上下文预算和资源引用有效期。远端读取超时或临时后端错误可按策略重试有限次数；重试后仍失败则返回 backend_error。若流式读取中断且已完成的片段满足边界完整性，则返回 included=partial；若实际字节数超过 stat 记录并突破限制，则终止后续读取，保留已安全封装片段并记录 size_mismatch 和 too_large。

降级分支保留原始检测类型、失败环节和存储定位信息，不把已经判定为多模态的图片或 PDF 回退成文本读取，也不把已经生成的文本 content 改写为资源描述。对于普通二进制文件，返回 metadata_only 和 inspect_metadata；对于权限或安全策略阻断文件，返回 blocked 且不暴露内容引用；对于文件不存在、符号链接越界或目录读取请求，返回 error 或 metadata_only 并附带 normalizedPath。通过该边界，工具既避免乱码和越权访问，又能向 agent 提供足够的可恢复信息。

### 多后端兼容机制

为兼容真实 workspace、共享 workspace 和自定义文件后端，本方案采用基础能力与增强能力分层的能力探测机制。stat 和 readFile 作为基础能力，readFileBytes、readFileStream、rangeRead、remoteReference 和对象存储访问作为增强能力。能力探测在首次读取、后端标识变化、workspace 版本变化、共享 workspace 代理重连、连续读取失败或缓存过期时执行，探测结果按 workspace 标识和后端标识缓存，并写入 decision.capabilities。读取分支只能调用 capabilities 中标记为可用的接口；接口不存在或运行时复核失败时，不抛出未处理异常，而是追加 bytes_unavailable、stream_unavailable 或 backend_error 并转入降级结果。

真实 workspace 可直接利用其文件元数据、MIME、大小、内联存储和外部对象存储读取能力；共享 workspace 通过代理将 stat、readFile 或字节读取请求转发到实际持有文件系统的工作区，并在返回值中保留 ownerWorkspace、sharedWorkspace 和 storageBackend 等定位字段；自定义后端只需提供基础接口即可继续完成文本读取和元数据降级。对于外部对象存储对象缺失、访问令牌过期或共享代理断开等异常，工具保留 normalizedPath、storageBackend、r2Key 或等价对象定位信息，返回 retryable 状态和 nextAction=retry_later 或 enable_bytes_read，使增强能力可以渐进接入而不破坏既有文本接口。

能力探测结果直接参与读取决策，而不仅用于错误说明。若 capabilities.readFileBytes=false 且文件被策略表识别为图片或 PDF，decisionKind 直接为 metadata_only，primaryReason=bytes_unavailable；若 capabilities.readFileStream=true 但 inline 阈值不足，decisionKind 仍可为 multimodal，delivery 预选为 stream；若共享代理仅暴露 readFile，文本文件进入文本分支，二进制和多模态文件进入降级分支。通过把能力状态写入 decision 并在运行时复核，本方案避免调用不存在的接口，同时允许后端逐步增加字节读取、流式读取或远端引用能力。
