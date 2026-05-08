# render_ast Schema

## 文档定位

本文档定义交底书渲染层使用的统一中间展示模型 `render_ast`。

`render_ast` 既不是内部真相源 `disclosure.json`，也不是最终导出格式 `Markdown`。

它位于两者之间，用于：

1. 前端渲染
2. Markdown 导出
3. 章节定位与高亮

相关文档：

- [专利交底书结构方案](/Users/yangchaoqun/myProj/patent_creator/docs/patent-disclosure-structure.md)
- [API 设计规范](/Users/yangchaoqun/myProj/patent_creator/docs/api-design.md)
- [Agent 基本设计原则](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles.md)

## 目标

`render_ast` 需要同时满足：

1. 让前端稳定渲染当前交底书
2. 让前端支持章节定位、滚动和高亮
3. 让 Markdown 导出和前端展示保持语义一致

## 一、总体关系

推荐的数据流如下：

```text
disclosure.json -> render builder -> render_ast -> React 渲染
disclosure.json -> render builder -> render_ast -> Markdown 导出
```

说明：

- `disclosure.json` 是内部真相源
- `render_ast` 是统一展示模型
- `Markdown` 是导出格式，不是内部真相源

## 二、定位原则

`render_ast` 的定位字段使用文档 id 体系：

- `section_id`
- `block_id`
- `anchor`

说明：

- section 节点使用 `id` 表示章节 id。
- block 节点使用 `id` 表示 block id。
- block 节点同时带 `section_id`，便于前端归属和高亮。
- `anchor` 用于前端滚动定位。

## 三、顶层结构

`render_ast` 顶层建议如下：

```json
{
  "type": "document",
  "title": "一种图像检测方法",
  "meta": {
    "document_type": "patent_disclosure",
    "schema_version": "v1"
  },
  "outline": [],
  "children": []
}
```

字段说明：

- `type`：固定为 `document`
- `title`：当前文档标题
- `meta`：展示层需要的少量元信息
- `outline`：目录树，直接服务左侧目录区域
- `children`：正文渲染树

## 四、核心节点类型

支持以下 6 种节点类型：

1. `document`
2. `section`
3. `paragraph`
4. `list`
5. `image`
6. `table`

## 五、节点通用字段

section 节点通用字段：

```json
{
  "type": "section",
  "id": "technical_solution",
  "anchor": "technical_solution"
}
```

block 节点通用字段：

```json
{
  "type": "paragraph",
  "id": "blk_000001",
  "section_id": "technical_solution"
}
```

字段说明：

- `type`：节点类型
- `id`：节点稳定 id
- `section_id`：block 所属章节 id
- `anchor`：前端滚动定位锚点

## 六、各节点结构

### 1. document

```json
{
  "type": "document",
  "title": "一种图像检测方法",
  "meta": {
    "document_type": "patent_disclosure",
    "schema_version": "v1"
  },
  "outline": [],
  "children": []
}
```

### 2. section

```json
{
  "type": "section",
  "id": "technical_solution",
  "title": "技术方案",
  "level": 2,
  "anchor": "technical_solution",
  "children": []
}
```

字段说明：

- `id`：章节 id
- `title`：章节标题
- `level`：标题层级，例如 `2` 表示一级正文标题，`3` 表示二级子章节标题
- `anchor`：前端滚动锚点
- `children`：内容节点或子章节节点

### 3. paragraph

```json
{
  "type": "paragraph",
  "id": "blk_000001",
  "section_id": "technical_solution",
  "text": "本发明提供一种图像检测方法。"
}
```

### 4. list

```json
{
  "type": "list",
  "id": "blk_000002",
  "section_id": "technical_solution",
  "ordered": true,
  "items": [
    "获取输入图像",
    "提取图像特征",
    "输出检测结果"
  ]
}
```

### 5. image

```json
{
  "type": "image",
  "id": "blk_000003",
  "section_id": "drawings",
  "src": "/assets/fig1.png",
  "caption": "图1 系统整体架构图",
  "alt": "系统整体架构图"
}
```

### 6. table

```json
{
  "type": "table",
  "id": "blk_000004",
  "section_id": "technical_solution",
  "columns": ["模块", "作用"],
  "rows": [
    ["特征提取模块", "提取图像特征"],
    ["检测模块", "输出检测结果"]
  ]
}
```

## 七、outline 结构

左侧目录区域不应自己从正文树中再提取目录。

建议后端直接在 `render_ast` 中提供 `outline`。

示例：

```json
{
  "outline": [
    {
      "id": "technical_field",
      "title": "技术领域",
      "level": 2,
      "anchor": "technical_field",
      "children": []
    },
    {
      "id": "technical_solution",
      "title": "技术方案",
      "level": 2,
      "anchor": "technical_solution",
      "children": [
        {
          "id": "processing_flow",
          "title": "处理流程",
          "level": 3,
          "anchor": "processing_flow"
        }
      ]
    }
  ]
}
```

这样做的好处：

1. 左侧目录区可以直接消费
2. 目录与正文锚点天然一致
3. 目录与渲染不需要重复构建逻辑

## 八、完整示例

```json
{
  "type": "document",
  "title": "一种图像检测方法",
  "meta": {
    "document_type": "patent_disclosure",
    "schema_version": "v1"
  },
  "outline": [
    {
      "id": "technical_solution",
      "title": "技术方案",
      "level": 2,
      "anchor": "technical_solution",
      "children": [
        {
          "id": "processing_flow",
          "title": "处理流程",
          "level": 3,
          "anchor": "processing_flow"
        }
      ]
    }
  ],
  "children": [
    {
      "type": "section",
      "id": "technical_solution",
      "title": "技术方案",
      "level": 2,
      "anchor": "technical_solution",
      "children": [
        {
          "type": "paragraph",
          "id": "blk_000001",
          "section_id": "technical_solution",
          "text": "本发明提供一种图像检测方法。"
        },
        {
          "type": "section",
          "id": "processing_flow",
          "title": "处理流程",
          "level": 3,
          "anchor": "processing_flow",
          "children": [
            {
              "type": "list",
              "id": "blk_000002",
              "section_id": "processing_flow",
              "ordered": true,
              "items": [
                "获取输入图像",
                "提取图像特征",
                "输出检测结果"
              ]
            }
          ]
        }
      ]
    }
  ]
}
```

## 九、与 disclosure.json 的区别

可以用一句话概括：

- `disclosure.json` 负责“怎么存”
- `render_ast` 负责“怎么展示”

典型区别包括：

1. `render_ast` 显式提供 `outline`
2. `render_ast` 显式提供 `level`
3. `render_ast` 显式提供 `anchor`
4. `render_ast` 将正文统一组织成渲染树

## 十、与 Markdown 的关系

Markdown 不是 `render_ast` 的替代品，而是 `render_ast` 的一种导出结果。

关系如下：

```text
disclosure.json -> render_ast -> Markdown
```

因此：

- 前端渲染不直接依赖 Markdown
- Markdown 导出与前端展示应共享同一套 `render_ast` 语义

## 十一、支持范围

### 支持

- `document`
- `section`
- `paragraph`
- `list`
- `image`
- `table`
- `outline`
- `id`
- `section_id`
- `block_id`
- `anchor`

### 不支持

- inline 富文本 mark 结构
- 跨段批注
- block diff 模型
- 富样式 token 系统
- 前端可编辑状态描述

## 十二、设计结论

`render_ast` 定位为：

1. 后端生成的统一展示模型
2. 前端渲染区的直接输入
3. Markdown 导出的统一语义来源

其核心结构为：

- 顶层 `document`
- 目录树 `outline`
- 正文树 `children`
- 节点类型 `section / paragraph / list / image / table`
- 关键定位字段 `id / section_id / anchor`
