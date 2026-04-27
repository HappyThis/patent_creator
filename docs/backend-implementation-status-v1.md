# 后端实现状态 v1

## 已核对文档

已按当前后端实现逐项核对 `docs/` 下 13 份文档：

- `README.md`
- `agent-principles-v1.md`
- `agent-prompt-context-spec-v1.md`
- `api-design-v1.md`
- `frontend-interaction-v1.md`
- `patent-disclosure-structure-v1.md`
- `render-ast-schema-v1.md`
- `round-lifecycle-v1.md`
- `session-log-v1.md`
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
- SSE：支持 `agent_output`、`tool_call_started`、`tool_call_finished`、`document_changed`、`round_finished`、`round_failed`
- 工具体系：实现 `document_read`、`document_edit`、`execute_subagent`、`exec_command` 的 v1 协议骨架
- 权限边界：子 agent 可读文档，但不能调用 `document_edit` 和 `execute_subagent`
- 文档写入：`document_edit` 成为 `disclosure.json` 的唯一业务写入口，支持原子校验后写入
- 自动提交：回合结束后按文档变更执行工作区 git commit
- chat 输入模型：以 `message` 为唯一用户语义输入，后端在回合内自行提取章节线索与任务目标
- OpenAI 兼容模型接入：支持通过 `OPENAI_COMPAT_BASE_URL` / `OPENAI_COMPAT_API_KEY` / `OPENAI_MODEL` 配置真实模型调用
- DeepSeek 默认配置：默认以 `https://api.deepseek.com/v1` 和 `deepseek-v4-pro` 作为测试模型口径
- `section_writer`：已接入真实 LLM runtime，输出 `document_edit_proposal` 后再通过 `document_edit` 落盘
- 目录结构：已拆出 `app/agents` 与 `app/runtime`，将 agent 能力、上下文管理和执行器职责分开

## 暂未实现为真实能力

- 真实 LLM 主 agent loop：当前是可替换的规则化编排流程
- `material_analyst` / `solution_refiner` / `consistency_reviewer`：当前仍是占位实现，协议和目录已对齐
- prompt 模板与 prefix cache：当前仅完成结构拆分，尚未做前缀缓存优化
- 复杂 `exec_command` 安全策略：当前按工作区目录执行命令，后续需要加白名单或审批

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
