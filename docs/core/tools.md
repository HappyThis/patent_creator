# Tools 设计

本文档定义当前主 agent 可见工具集合、权限边界和返回协议。所有工具调用均由主 agent 直接发起。

## 工具清单

当前注册工具：

1. `disclosure_outline`
2. `disclosure_search`
3. `disclosure_read_section`
4. `disclosure_edit`
5. `file_glob`
6. `file_search`
7. `file_read`
8. `innovation_kernel_kit`
9. `exec_command`

## 权限模型

只有 `main_agent` 一个工具调用 scope。执行器收到工具名后：

1. 从工具 registry 查找声明。
2. 检查工具是否允许当前 scope 使用。
3. 调用工具实现并返回标准结果。

所有交底书写入都必须通过 `disclosure_edit` 完成。工具失败时返回：

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

## 交底书读取

只读工具分散设计，避免一个工具承担过多模式：

- `disclosure_outline`：分页返回交底书目录和 block preview，用于定位。
- `disclosure_search`：全文搜索 block，支持普通关键词或正则，大小写不敏感，分页返回命中结果。
- `disclosure_read_section`：精读一个 section 的 title、直接 blocks 和直接子 section 摘要；可分页，也可指定直接 block ids。

主 agent 在缺少正文依据时应先定位再精读，不应凭历史记忆猜测当前交底书内容。

## 交底书编辑

`disclosure_edit` 是唯一交底书写入工具。它以 `section_id` 作为工作区，只能操作该 section 的直接 block 或直接子 section。

支持操作：

- `replace_block`
- `delete_block`
- `insert_block`
- `insert_section`
- `delete_section`

编辑边界：

- 不提供整章重写；整章重写必须拆成删除、插入 section、逐个插入或替换 block。
- 修改章节标题使用 `replace_block` 替换该 section 的 title block。
- title block 只能替换，不能删除，不能在其前方插入正文 block。
- `insert_section` 只创建子章节标题，正文后续通过 `insert_block` 小步写入。
- 单次新增或替换文本总量不得超过 1500 字。

写入正文必须是最终态文本，不包含对话过程、修改说明或旧方案对比叙述。

## 命令执行

`exec_command` 在当前项目工作区内执行命令字符串，用于诊断项目文件、运行测试或查看状态。命令输出作为工具结果返回给主 agent。

## 工具声明

工具说明由函数元数据自动生成并注入主 agent prompt。新增工具时，应优先补充工具函数 docstring、参数模型和测试，避免在 prompt 中手写重复规则。
