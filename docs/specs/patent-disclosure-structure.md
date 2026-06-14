# 专利交底书结构方案

## 文档定位

本文档定义交底书文档本体的 v3 JSON 结构。它只描述落盘的交底书内容，不包含 session 记忆、agent 推理、工具调用过程、审查意见或待确认事项。

相关文档：

- [Agent 基本设计原则](../core/agent-principles.md)
- [Tools 设计](../core/tools.md)

## 设计目标

v3 的核心目标是让 agent 面向“目录定位 + 内容块编辑”维护交底书，同时隐藏底层 JSON 的实现细节。

设计原则：

1. 交底书只承载真正的专利正文内容。
2. section 负责结构，block 承接内容。
3. 标题也是内容，因此 title 是一种 block。
4. section 和 block 都有稳定 id。
5. id 不表达业务语义，不依赖章节名称。
6. `index` 不落盘，只在工具返回中动态计算。
7. Markdown 只是导出格式，不是内部真相源。

## 顶层结构

顶层固定只有两个字段：

```json
{
  "meta": {
    "document_type": "patent_disclosure",
    "schema_version": "v3",
    "created_at": "2026-06-14T12:00:00+08:00",
    "updated_at": "2026-06-14T12:00:00+08:00"
  },
  "sections": []
}
```

不再保存：

- `meta.title`
- `meta.id_counters`
- section `type`
- section `children`

## Meta

`meta` 字段：

- `document_type`：固定为 `patent_disclosure`
- `schema_version`：固定为 `v3`
- `created_at`：创建时间，ISO 字符串
- `updated_at`：最近修改时间，ISO 字符串

`updated_at` 由写入工具在成功修改后更新。

## Section

section 是递归结构单元。

```json
{
  "id": "sec_000007",
  "title": {
    "id": "blk_000007",
    "type": "title",
    "text": "技术方案"
  },
  "blocks": [
    {
      "id": "blk_000020",
      "type": "paragraph",
      "text": "本方案包括..."
    }
  ],
  "sections": []
}
```

字段说明：

- `id`：section 稳定 id。
- `title`：该 section 的标题 block。
- `blocks`：该 section 的直接正文 blocks。
- `sections`：该 section 的直接子 section。

约束：

- `title.type` 必须是 `title`。
- `blocks` 中不得出现 `title` 类型。
- 子章节放入 `sections`，不是 `children`。
- section 不保存 `type`。

## Block

block 是交底书内容承接单元。

支持类型：

- `title`
- `paragraph`
- `list`
- `image`
- `table`

### title

只允许出现在 `section.title`。

```json
{
  "id": "blk_000007",
  "type": "title",
  "text": "技术方案"
}
```

### paragraph

```json
{
  "id": "blk_000020",
  "type": "paragraph",
  "text": "本方案包括..."
}
```

### list

```json
{
  "id": "blk_000021",
  "type": "list",
  "ordered": false,
  "items": ["第一项", "第二项"]
}
```

### image

```json
{
  "id": "blk_000022",
  "type": "image",
  "src": "assets/figure-1.png",
  "caption": "图 1 系统结构示意图",
  "alt": "系统结构"
}
```

### table

```json
{
  "id": "blk_000023",
  "type": "table",
  "columns": ["模块", "职责"],
  "rows": [["采集模块", "获取输入数据"]]
}
```

## Index 约定

`index` 不存储在 JSON 中，只由读取工具返回。

在一个 section 内：

- `index=0` 固定表示 `section.title`
- 正文 block 从 `index=1` 开始
- 子 section 的 index 单独按直接子 section 顺序计算

## 定位器

定位器是工具返回给 agent 的位置描述，不是落盘字段。

section locator：

```json
{
  "kind": "section",
  "section_id": "sec_000007",
  "section_path": ["sec_000007"],
  "index": 6
}
```

block locator：

```json
{
  "kind": "block",
  "section_id": "sec_000007",
  "section_path": ["sec_000007"],
  "block_id": "blk_000020",
  "block_type": "paragraph",
  "index": 1
}
```

说明：

- `section_path` 是从根 section 到当前 section 的 id 路径。
- block locator 同时包含所属 section 和 block 自身 id。
- agent 不需要知道底层 JSON Pointer。

## Preview

outline 和 search 中的 preview 是字符串。

短内容直接返回原文。长内容按以下格式：

```text
前缀…（省略 N 字）…后缀
```

preview 只用于定位和快速判断；需要完整正文时使用读取工具精读。

## 只读工具关系

读取遵循“先定位，再精读”。

- `disclosure_outline`：返回目录和 block preview。
- `disclosure_search`：返回关键词或正则命中的 block preview。
- `disclosure_read_section`：读取一个 section 的 title、直接 blocks、直接子 section 摘要。

分页字段统一为：

```json
{
  "returned": 20,
  "total": 83,
  "offset": 0,
  "next_offset": 20,
  "truncated": true
}
```

## 编辑工具关系

编辑统一通过 `disclosure_edit`。

所有编辑都以 `section_id` 作为工作区，只能操作该 section 的直接 block 或直接子 section。

支持操作：

- `replace_block`
- `delete_block`
- `insert_block`
- `insert_section`
- `delete_section`

不支持整章重写。整章重写需要显式拆成：

1. 删除目标 section。
2. 插入新的子 section。
3. 插入正文 blocks。
4. 插入必要的子 sections。

修改章节标题使用 `replace_block` 替换 `section.title` 的 block。

## 初始章节

新建工作区默认创建 12 个顶层 section：

1. 发明名称
2. 技术领域
3. 背景技术
4. 现有技术方案
5. 现有技术缺陷
6. 要解决的技术问题
7. 技术方案
8. 关键创新点
9. 具体实施方式
10. 技术效果
11. 附图说明
12. 权利要求建议

项目标题作为“发明名称”section 的第一个正文 paragraph block，而不是 `meta.title`。
