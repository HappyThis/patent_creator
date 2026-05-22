## 技术方案

本方案针对 Think agent workspace 的 read 工具进行增强，使其在保持现有文本文件读取体验（按行读取、行号展示、分页、长内容截断）的前提下，能够识别并正确处理图片、PDF、普通二进制文件等多种文件类型。方案引入"后端抽象层—类型检测层—限制控制层—策略路由层—输出构建层"五层处理管线，统一输出结构兼顾模型可消费性和用户可解释性，并通过最小化后端接口实现多 workspace 后端兼容。

### 技术问题概述

Think agent 的 workspace read 工具当前仅面向文本文件设计。当工作区中存在图片、PDF 或其他非文本文件时，工具返回"二进制文件不可读"等无差别错误，导致：一、具备多模态能力的模型无法利用工作区中的视觉内容；二、用户无法获得关于文件为何不可读的结构化反馈；三、工具在不同 workspace 后端实现下行为不一致。因此需要一种文件类型感知的读取方案，在保持现有文本阅读体验的同时，将可被模型消费的非文本内容以合适形式传递，并对无法处理的情况返回结构化原因。

### 整体架构

方案在 read 工具内部构建五层处理管线：后端抽象层（Backend Adapter）→ 文件类型检测层（File Type Detector）→ 限制控制层（Limit Controller）→ 读取策略路由层（Read Strategy Router）→ 输出构建层（Output Builder）。read 工具接收文件路径和可选参数后，依次经过各层处理：后端抽象层提供统一的 stat、read_bytes、read_text 能力；类型检测层通过扩展名、magic bytes 和内容采样三级判断文件类型；限制控制层基于可配置阈值决定是否允许读取及是否降级；策略路由层根据类型选择文本/图片/PDF/二进制/目录/缺失等处理路径；输出构建层组装统一结构返回给模型。

### BackendInterface 抽象层与多后端兼容

定义 BackendInterface 抽象层，解耦 read 工具与具体存储实现。接口仅需三个方法：(1) stat(path) 返回 {exists, is_dir, size, mtime, mime_type_hint}；(2) read_bytes(path, offset?, length?) 返回原始字节，支持分片读取；(3) read_text(path, offset?, limit?, encoding?) 返回文本内容。read 工具的所有处理逻辑仅依赖此接口，不感知后端实现细节。

多后端兼容：真实文件系统后端将 stat 映射为操作系统 stat 调用，read_bytes 映射为分片文件读取；共享 workspace 后端通过 RPC/API 调用远端存储并在传输层做字节序列化；自定义后端（如内存文件系统、对象存储适配器）只需实现上述三个方法即可接入。新增后端类型不影响 read 工具核心逻辑。

后端最小能力约定：所有后端必须至少支持全量 read_bytes（不传 offset/length），分片读取为可选能力。当后端不支持分片读取时，read 工具在需要分段读取的场景下降级为全量读取后再在工具层截取。

### 文件类型识别机制

文件类型识别采用三级递进判断，不单独依赖扩展名，以防止因文件名误导导致的类型误判。

第一层——扩展名预判：维护扩展名到 MIME 类型的映射表（如 .png→image/png、.pdf→application/pdf、.jpg→image/jpeg、.gif→image/gif、.webp→image/webp），预判结果作为第二层 magic bytes 检测的候选集以缩小匹配范围。

第二层——magic bytes 验证：通过 BackendInterface.read_bytes 读取文件头部 512 字节，与已知文件签名库比对。例如 \x89PNG 对应 image/png，%PDF 对应 application/pdf，\xFF\xD8\xFF 对应 image/jpeg，GIF8 对应 image/gif。若扩展名预判与 magic bytes 结果冲突，以 magic bytes 为准并记录 warning 到输出中。

第三层——内容采样确认：针对 magic bytes 无法唯一确定的歧义类型（如无签名文件可能是 text/plain 或 application/octet-stream），采样前 N 字节计算可打印字符比例。可打印字符占比超过 95% 判定为 text/plain；存在有效 UTF-8/UTF-16 BOM 时直接判定为对应编码的 text/plain；其余情况判定为 application/octet-stream。

### 读取策略与模型输出转换

根据类型检测结果，read 工具将请求路由到六种策略之一。所有策略共享统一输出基结构：{type, filename, mime_type, size}，各策略按需追加专属字段。

策略A——文本文件（text/plain、text/markdown、application/json 等）：保持现有文本读取体验。通过 read_text 获取内容，支持按行读取并通过 offset/limit 参数分页，每行附带行号。超过 1MB 的文本文件自动截断并在输出中标记 truncated=true。输出结构包含 lines: [{num, text}]、encoding 和 truncated 字段。

策略B——图片文件（image/png、image/jpeg、image/gif、image/webp 等）：通过 read_bytes 获取完整字节后进行 base64 编码，构建符合模型多模态接口的 image 内容块：{type: "image", source: {type: "base64", media_type, data}}。同时通过解析图片头部（无需完整解码）获取宽度和高度，附在输出的 width、height 字段中供模型和用户参考。

策略C——PDF 文件（application/pdf）：优先将 PDF 字节进行 base64 编码，以 application/pdf 媒体类型作为文件内容块传递给多模态模型。若模型不支持直接消费 PDF 内容块，降级返回结构化信息：页数、标题/作者元数据（从 PDF 文档信息字典提取）及前若干页的文本预览。

### 异常情况处理与结构化返回

策略D——普通二进制文件（application/octet-stream）：不尝试将内容传递给模型。返回结构化原因：{type: "binary", filename, mime_type, size, reason: "binary file cannot be rendered as content", hex_preview}，其中 hex_preview 为文件前 32 字节的十六进制表示，供用户判断文件性质。

策略E——目录：通过 stat 检测到路径为目录时，不读取目录内容。返回：{type: "error", filename, reason: "path_is_directory", suggestion: "使用 list 工具查看目录内容"}。

策略F——文件不存在：stat 返回 exists=false 时，返回：{type: "error", filename, reason: "file_not_found", suggestion: "检查路径拼写或使用 find 工具搜索"}。

策略G——权限不足：stat 或 read_bytes 因权限问题失败时，返回：{type: "error", filename, reason: "permission_denied", detail}，detail 包含操作系统返回的具体错误描述。

### 限制控制策略

限制控制层在策略路由之前执行，基于可配置的阈值判断文件是否允许进入内容转换流程，防止过大的文件占用模型上下文。

分级阈值设计：文本文件阈值默认 1MB，超限后仍可读取但自动截断并在输出中标记 truncated=true；图片文件阈值默认 20MB，超限后不执行 base64 编码，降级为返回元数据（文件名、MIME 类型、大小、宽度、高度）并附带 reason="file_too_large_for_image_encoding"；PDF 文件阈值默认 50MB，超限后降级为返回元数据和前若干页文本预览；普通二进制文件不设阈值，统一返回结构化信息不传递内容。

阈值作为 read 工具的可选参数暴露给调用方（如 max_size_bytes），允许单次调用覆盖默认值，但工具内部设有硬上限（如文本 10MB、图片 100MB、PDF 200MB），用户参数不得超过硬上限。阈值配置存储在工具级配置中，可由系统管理员按部署环境调整默认值和硬上限。

### 技术效果

本方案的技术效果包括：(1) 文件类型感知：通过三级递进检测，即使文件扩展名缺失或错误，仍能准确判断真实类型，避免将图片误作文本读取或遗漏可消费内容；(2) 多模态内容传递：图片和 PDF 以 base64 编码的内容块形式传递给模型，使具备多模态能力的模型可以直接理解工作区中的视觉内容，无需外部工具转换；(3) 统一的输出结构：所有文件类型共享公共字段，模型可统一解析，同时用户可通过 filename、mime_type、size、reason 等字段获得可解释的反馈；(4) 保持兼容：文本文件的按行读取、行号、分页、截断等体验不变，现有调用方无需修改参数即可继续使用；(5) 后端无关：通过最小化 BackendInterface（仅三个方法），方案可适配真实文件系统、共享 workspace、对象存储等多种后端。

### 风险与待确认问题

以下为需要后续确认的风险点和待确认问题：(1) 模型多模态能力探测——方案假设系统可以查询当前模型是否支持图片/PDF 内容块；若无法探测，需采用保守策略仅返回文本和结构化元数据，确认探测接口的可用性；(2) PDF 页数与元数据提取——需要解析 PDF 文件结构（如读取交叉引用表和文档信息字典），涉及额外的 PDF 解析逻辑；若不引入解析依赖，页数字段降级为"未知"；(3) 大图片尺寸获取——图片宽度和高度通过解析头部字段获取，对于渐进式 JPEG 等复杂格式需要额外处理逻辑；(4) 文本编码检测——非 BOM 文件的编码检测依赖采样试探，存在误判可能（如 GBK 编码被误判为 Latin-1），需明确误判容忍度及是否引入 chardet 类库；(5) 后端分片读取的可用性——部分后端（如对象存储）可能不支持 offset+length 分片读取，需在接口中约定最小能力基线。
