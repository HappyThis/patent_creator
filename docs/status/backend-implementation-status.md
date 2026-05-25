# Backend Implementation Status

## 当前架构

后端采用主 agent 直接执行的架构。主 agent 负责读取上下文、调用工具、写入交底书并输出最终回复。

## 已实现能力

- FastAPI 项目、文档、会话、导出接口。
- SSE round 事件流。
- 主 agent OpenAI-compatible 工具调用 loop。
- Markdown 上下文压缩与兜底裁剪。
- 交底书文档读取工具。
- 五个小步文档写入工具。
- `exec_command` 项目诊断工具。
- 工作区 JSON 持久化与 git commit。
- 后端回归测试。

## 当前工具集

- `document_read`
- `document_replace_section_blocks`
- `document_append_block`
- `document_replace_block`
- `document_append_child_section`
- `document_clear_section_blocks`
- `exec_command`

## 测试状态

后端测试覆盖：

- 主 agent prompt 和工具声明。
- API/SSE 主流程。
- 上下文压缩和恢复。
- 文档工具行为。
- 主 agent loop 工具调用恢复。
- OpenAI-compatible provider 适配。
- 项目恢复。
