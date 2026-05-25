# Tools 设计

## 文档定位

本文档定义当前主 agent 可见工具集合、权限边界和返回协议。所有工具调用均由主 agent 直接发起。

## 工具清单

当前注册工具：

1. `document_read`
2. `document_replace_section_blocks`
3. `document_append_block`
4. `document_replace_block`
5. `document_append_child_section`
6. `document_clear_section_blocks`
7. `exec_command`

## 权限模型

只有 `main_agent` 一个工具调用 scope。执行器收到工具名后：

1. 从工具 registry 查找声明。
2. 检查工具是否允许当前 scope 使用。
3. 调用工具实现并返回标准结果。

所有文档写入都必须通过文档写入工具完成。工具失败时返回：

```json
{
  "status": "failed",
  "output": {
    "code": "error_code",
    "message": "错误说明"
  }
}
```

成功时返回：

```json
{
  "status": "success",
  "output": {}
}
```

## 文档读取

`document_read` 用于读取项目上下文、目录、章节、block 和搜索正文。主 agent 在缺少正文依据时应先读取，不应凭历史记忆猜测当前文档内容。

## 文档写入

文档写入工具保持小步、明确、可回滚：

- `document_replace_section_blocks` 替换一个章节的 blocks。
- `document_append_block` 向一个章节追加一个 block。
- `document_replace_block` 替换一个 block。
- `document_append_child_section` 向父章节追加一个子章节。
- `document_clear_section_blocks` 清空一个章节的 blocks。

写入正文必须是最终态文本，不包含对话过程、修改说明或旧方案对比叙述。

## 命令执行

`exec_command` 在当前项目工作区内执行命令字符串，用于诊断项目文件、运行测试或查看状态。命令输出作为工具结果返回给主 agent。

## 工具声明

工具说明由函数元数据自动生成并注入主 agent prompt。新增工具时，应优先补充工具函数 docstring、参数模型和测试，避免在 prompt 中手写重复规则。
