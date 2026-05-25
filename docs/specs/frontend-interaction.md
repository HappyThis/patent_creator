# Frontend Interaction

## 文档定位

本文档定义前端如何展示当前单主 agent 执行过程。界面不再展示子 agent 启动、子任务 pipe 或任务看板状态。

## 聊天事件

前端通过 SSE 接收 round 事件：

- `round_started`：本轮开始。
- `assistant_delta`：主 agent 文本流。
- `tool_call_started`：主 agent 工具调用开始。
- `tool_call_finished`：主 agent 工具调用结束。
- `document_changed`：文档已更新。
- `round_finished`：本轮完成。
- `round_failed`：本轮失败。

## 过程展示

工具调用以简短状态展示，例如：

- `开始读取章节`
- `章节已读取`
- `开始写入文档`
- `文档更新已完成`
- `开始执行诊断命令`
- `诊断命令已完成`

工具详情可以在调试视图中展开，但默认界面应优先展示用户可理解的短摘要。

## 文档预览

收到 `document_changed` 后，前端刷新 outline、预览和选中状态。`round_finished` 中的 `changed` 字段用于决定是否显示本轮产生了文档变更。

## 上下文压缩

当收到上下文压缩事件时，前端显示简短状态即可。压缩内容不直接展示给用户，也不作为可编辑文档内容。
