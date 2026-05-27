## 技术方案

本方案在 Think agent 的 workspace 工具链基础上，对 read 工具进行增强，使其能够识别并正确处理文本、图片、PDF 及普通二进制等多类型文件。整体思路是：在保留现有文本读取能力（行号展示、分页、长行截断）不变的前提下，在读入口增加文件类型判断分支，根据类型选择不同的读取策略和编码转换方式，并在工具返回值中扩展媒体类型和二进制内容字段，使上游 agent 能够直接获取可用的图片或 PDF 数据。

### 整体架构

增强后的 read 工具在原有分层架构上扩展，自下而上分为三层。最底层是 Workspace 存储层（基于 SQLite + R2），已具备 readFile（返回字符串）和 readFileBytes（返回原始字节 Uint8Array）两种读取能力，并在文件元数据中持久化 mimeType 字段。中间层是 ReadOperations 接口抽象层，原有接口仅暴露 readFile 和 stat，增强方案在该接口中新增 readFileBytes 方法，使工具执行函数无需关心底层是 Workspace、SharedWorkspace 还是其他注入实现。最上层是工具执行函数 createReadTool，在读入口增加类型判断逻辑，依据文件类型选择文本路径或二进制路径，并负责编码转换、大小校验和返回值构造。

### 文件类型识别机制

文件类型识别采用两级策略。第一级：读取 Workspace 中已存储的 mimeType 字段。该字段在文件写入时由调用方传入并持久化于数据库，stat 操作可直接返回，无需额外 I/O。当 mimeType 明确为 text/*、image/*、application/pdf 等已知类型时，直接进入对应的读取分支。第二级：当 mimeType 缺失或为通用的 application/octet-stream 时，通过读取文件头部魔数（magic bytes）进行补充判断——例如 PNG 文件的 89 50 4E 47、PDF 文件的 25 50 44 46、JPEG 文件的 FF D8 FF 等。魔数检测只需读取文件前若干字节（通常不超过 16 字节），通过 readFileBytes 实现，开销极低。两级策略互补：mimeType 快速命中常见场景，魔数检测兜底未知或不可信类型。

### 按类型的分发读取策略

工具执行函数在获取文件类型后，进入分发逻辑：若为 text/* 或无法识别但魔数检测表明非已知二进制格式，走文本路径，调用 readFile 获取字符串内容，沿用现有的行号标注、offset/limit 分页、长行截断（MAX_LINE_LENGTH=2000）和总行数限制（MAX_LINES=2000）机制，输出体验与原有行为完全一致。若为 image/*，走图片路径，通过 readFileBytes 读取原始字节，根据具体图片格式（PNG、JPEG、GIF、WebP、SVG 等）确定对应的 MIME 类型，进行 base64 编码后构造 data URI。若为 application/pdf，走 PDF 路径，同样通过 readFileBytes 读取原始字节并进行 base64 编码。若为其他可识别二进制类型（如 application/zip 等），统一走通用二进制路径，进行 base64 编码并标记原始 MIME 类型，供上游按需处理。

### 输出结构设计

增强后的 read 工具返回值在原有文本字段基础上扩展。对文本文件，返回结构保持原有格式（content 字段携带字符串、行号前缀、截断提示等）。对图片和 PDF 等二进制文件，返回结构新增 content_type 字段指示媒体类别（如 "image"、"pdf"、"binary"），新增 media_type 字段携带具体 MIME 类型（如 "image/png"），新增 data 字段携带 base64 编码后的数据内容。图片额外提供 width 和 height 字段（如能从文件头解析）。同时新增 truncated 字段标记是否因大小限制被截断，以及 original_size 字段记录文件原始字节数。该结构向后兼容——文本读取路径不产生新增字段，已有调用方不受影响。

### 大小限制与安全控制

为防止读取超大图片或二进制文件导致内存膨胀和 base64 编码后的字符串过大，方案引入可配置的大小限制。对图片和 PDF 文件，设定默认最大读取字节数（如 10MB），超过限制时拒绝读取并返回错误提示，建议调用方使用分块读取或缩小文件。对文本文件，保持原有 MAX_LINES=2000 和 MAX_LINE_LENGTH=2000 的限制不变。大小检查通过 stat 操作获取文件字节数（该信息已存储于数据库，无需读取全部内容），在进入 readFileBytes 之前完成，避免无效 I/O。限制阈值可通过工具创建时的参数注入，不同部署环境可按需调整。

### 多后端兼容

增强方案保持对 Workspace、WorkspaceFileSystem、SharedWorkspace 等多种后端的兼容性。ReadOperations 接口新增 readFileBytes 方法签名，返回 Uint8Array | null。各后端实现类分别实现该方法：Workspace 后端直接调用已有的 readFileBytes，SharedWorkspace 后端通过其远程通信协议传递字节数据。对于仅实现了 readFile（字符串）的旧有后端或自定义注入实现，提供默认适配层——先通过 readFile 获取字符串再编码为 Uint8Array，虽有效率损失但保证接口统一。stat 方法在各后端均已返回 mimeType 字段，无需额外适配。

### 关键流程

增强 read 工具的端到端流程如下：(1) 接收文件路径 path 及可选参数 offset、limit。(2) 调用 ops.stat(path) 获取 FileInfo，包含 type（file/directory）、mimeType、size。(3) 若 type 为 directory，按现有逻辑返回错误。(4) 以 mimeType 为首要依据判断文件类别；若 mimeType 缺失或为通用类型，通过 readFileBytes 读取头部魔数进行二次判定。(5) 文本类：调用 readFile 获取字符串，执行行分割、行号标注、offset/limit 截取、长行截断，构造文本返回值。(6) 图片/PDF/二进制类：先检查 size 是否超过配置阈值，超过则返回 size_exceeded 错误；否则调用 readFileBytes 获取完整字节，进行 base64 编码，按类别填充 content_type、media_type、data 等字段后返回。(7) 所有路径均在返回值中携带 truncated 标记和 original_size，便于上游判断内容完整性。
