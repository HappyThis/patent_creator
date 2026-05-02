# 后端实现状态 v1

## 已核对文档

已按当前后端实现逐项核对 `docs/` 下核心设计与状态文档：

- `README.md`
- `agent-principles-v1.md`
- `agent-prompt-context-spec-v1.md`
- `api-design-v1.md`
- `frontend-interaction-v1.md`
- `patent-disclosure-structure-v1.md`
- `render-ast-schema-v1.md`
- `round-lifecycle-v1.md`
- `session-log-v1.md`
- `context-management-v1.md`
- `subagents-v1.md`
- `tech-stack-v1.md`
- `tools-v1.md`
- `workspace-init-v1.md`

## 已实现

- 后端技术栈：`Python 3.11 + FastAPI + Uvicorn + uv`
- 文件系统持久化：`project.json`、`disclosure.json`、`sessions/*.jsonl`、`exports/`、`assets/`、`runtime/`
- 工作区初始化：创建标准交底书骨架，并初始化独立 git 仓库
- 核心 API：项目创建、项目读取、目录读取、render_ast、原始文档、chat 消息、SSE、session 事件、Markdown 导出
- render_ast：支持 `document`、`section`、`paragraph`、`list`、`image`、`table`
- session log：按 `user_input`、`agent_output`、`tool_call`、`tool_result` 记录 jsonl
- ContextManager：主 agent 可从当前 session log 恢复多轮 `user` / `assistant` messages，并输出上下文用量统计
- 上下文压缩：超阈值时会压缩当前用户输入之前的主 agent 历史，写入 `context_summary`；压缩失败或仍超限时写入 `context_pruned` 并按 cursor 兜底裁剪
- SSE：支持 `assistant_delta`、`tool_call_started`、`tool_call_finished`、`document_changed`、`round_finished`、`round_failed`、`round_cancelled`
- 运行中恢复：支持通过 `GET /sessions/{session_id}/stream` 重新订阅运行中 session 的 SSE
- 运行中取消：支持通过 `POST /sessions/{session_id}/rounds/{round_id}/cancel` 取消当前 round
- 工具体系：实现 `document_read`、`document_edit`、`execute_subagent`、`exec_command` 的 v1 协议骨架
- 权限边界：子 agent 可读文档，但不能调用 `document_edit` 和 `execute_subagent`
- 文档写入：`document_edit` 成为 `disclosure.json` 的唯一业务写入口，支持原子校验后写入
- 自动提交：回合结束后按文档变更执行工作区 git commit
- chat 输入模型：以 `message` 为唯一用户语义输入，后端在回合内自行提取章节线索与任务目标
- OpenAI 兼容模型接入：支持通过 `OPENAI_COMPAT_BASE_URL` / `OPENAI_COMPAT_API_KEY` / `OPENAI_MODEL` 配置真实模型调用
- DeepSeek 默认配置：默认以 `https://api.deepseek.com` 和 `deepseek-v4-pro` 作为测试模型口径
- LLM 超时与重试：支持 `PATENT_CREATOR_LLM_TIMEOUT` 与 `PATENT_CREATOR_LLM_MAX_RETRIES`
- 真实 LLM 主 agent loop：已接入 OpenAI-compatible tool calling，支持多工具调用、流式文本 delta 和 DeepSeek thinking/tool-call 协议
- 子 agent loop：4 个子 agent 均通过统一 loop 运行，可调用 `document_read` 与 `exec_command`
- 子 agent JSON 输出：子 agent 最终响应启用 JSON mode；若仍返回非法 JSON，会转为 `execute_subagent` 的 failed 工具结果交回主 agent 决策
- 子 agent SSE：子 agent 内部工具调用会以 `scope=subagent:<agent_id>` 写入 session log 并实时推送
- `section_writer`：已接入真实 LLM runtime，输出 `document_edit_proposal` 后再通过 `document_edit` 落盘
- `material_analyst` / `solution_refiner` / `consistency_reviewer`：已接入真实 LLM runtime，并输出各自 proposal envelope
- `solution_refiner`：支持 `analysis_result`，也支持在必要时返回 `document_edit_proposal`
- `call_type`：已区分 `task_only_specialist`、`rich_context_specialist`、`forked_context` 的上下文装配策略
- `exec_command`：已接入主 agent 与子 agent，以项目工作区为 cwd 执行命令字符串，不做命令白名单限制
- 前端上下文用量展示：当前 chat composer 区显示上下文窗口估算用量
- 前端 Markdown 导出：可从 Chat 区触发导出，并回显导出文件路径
- `disclosure.json` 写入：已使用临时文件替换方式落盘，避免半写入文件
- 目录结构：已拆出 `app/agents` 与 `app/runtime`，将 agent 能力、上下文管理和执行器职责分开

## 暂未实现为真实能力

- 显式 prefix cache：当前已按稳定/半稳定/动态片段组织 prompt，但未接入供应商级显式缓存控制
- 子 agent run-local 压缩：主 agent 跨轮上下文压缩已实现，子 agent 单次运行内部压缩尚未接入
- 更细粒度命令审批：当前 `exec_command` 不做命令白名单限制，尚未加入用户审批流
- 子 agent 并行调度：v1 仍采用同步子 agent 调用模型

## 结构原则

后端当前按以下分层组织：

- `app/api`：请求响应、路由注册、错误转换
- `app/services`：回合编排、SSE 事件总线、服务装配
- `app/agents`：agent 声明、prompt、OpenAI 兼容模型调用、subagent worker
- `app/runtime`：上下文管理器、执行器、工具权限和工具实现
- `app/domain`：纯文档协议、schema 校验、render 转换、结构遍历
- `app/storage`：文件系统、工作区 git、导出和持久化
- `app/core`：配置、错误、ID 工具
- `app/schemas`：Pydantic DTO
