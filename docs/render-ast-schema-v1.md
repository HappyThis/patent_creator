# render_ast Schema v1

## 文档定位

本文档定义交底书渲染层使用的统一中间展示模型 `render_ast`。

`render_ast` 既不是内部真相源 `disclosure.json`，也不是最终导出格式 `Markdown`。

它位于两者之间，用于：

1. 前端渲染
2. Markdown 导出
3. 章节定位与高亮

相关文档：

- [专利交底书结构方案 v1](/Users/yangchaoqun/myProj/patent_creator/docs/patent-disclosure-structure-v1.md)
- [API 设计规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/api-design-v1.md)
- [Agent 基本设计原则 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles-v1.md)

## 目标

`render_ast` 需要同时满足：

1. 让前端稳定渲染当前交底书
2. 让前端支持章节定位、滚动和高亮
3. 让 Markdown 导出和前端展示保持语义一致

因此它既不能过于接近底层存储结构，也不能直接退化成 HTML 或 Markdown。

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

## 二、顶层结构

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

### 字段说明

- `type`
  - 固定为 `document`
- `title`
  - 当前文档标题
- `meta`
  - 展示层需要的少量元信息
- `outline`
  - 目录树，直接服务左侧目录区域
- `children`
  - 正文渲染树

## 三、核心节点类型

V1 建议支持以下 6 种节点类型：

1. `document`
2. `section`
3. `paragraph`
4. `list`
5. `image`
6. `table`

这套节点类型应与交底书文档本体的块级内容语义保持一致。

## 四、节点通用字段

所有节点建议支持以下通用字段：

```json
{
  "type": "section",
  "id": "technical_solution",
  "pointer": "/sections/6",
  "anchor": "technical_solution"
}
```

字段说明：

- `type`
  - 节点类型
- `id`
  - 业务节点标识
- `pointer`
  - 对应 `disclosure.json` 中的 JSON Pointer
- `anchor`
  - 前端滚动定位锚点

说明：

- `section` 节点建议必须有 `id`
- `paragraph`、`list`、`image`、`table` 等 block 节点可以没有 `id`
- `pointer` 是前端定位、高亮、最近修改展示的重要基础字段

## 五、各节点结构

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
  "pointer": "/sections/6",
  "anchor": "technical_solution",
  "children": []
}
```

字段说明：

- `title`
  - 章节标题
- `level`
  - 标题层级，例如：
    - `2` 表示一级正文标题
    - `3` 表示二级子章节标题
- `children`
  - 该章节下的内容节点或子章节节点

### 3. paragraph

```json
{
  "type": "paragraph",
  "pointer": "/sections/6/blocks/0",
  "text": "本发明提供一种图像检测方法。"
}
```

字段说明：

- `text`
  - 段落正文

### 4. list

```json
{
  "type": "list",
  "pointer": "/sections/6/blocks/1",
  "ordered": true,
  "items": [
    "获取输入图像",
    "提取图像特征",
    "输出检测结果"
  ]
}
```

字段说明：

- `ordered`
  - 是否有序列表
- `items`
  - 列表项数组

### 5. image

```json
{
  "type": "image",
  "pointer": "/sections/10/blocks/0",
  "src": "/assets/fig1.png",
  "caption": "图1 系统整体架构图",
  "alt": "系统整体架构图"
}
```

字段说明：

- `src`
  - 图片资源路径
- `caption`
  - 图片说明
- `alt`
  - 替代文本

### 6. table

```json
{
  "type": "table",
  "pointer": "/sections/8/blocks/0",
  "columns": ["模块", "作用"],
  "rows": [
    ["特征提取模块", "提取图像特征"],
    ["检测模块", "输出检测结果"]
  ]
}
```

字段说明：

- `columns`
  - 表头
- `rows`
  - 表格数据行

## 六、outline 结构

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
      "pointer": "/sections/1",
      "anchor": "technical_field",
      "children": []
    },
    {
      "id": "technical_solution",
      "title": "技术方案",
      "level": 2,
      "pointer": "/sections/6",
      "anchor": "technical_solution",
      "children": [
        {
          "id": "processing_flow",
          "title": "处理流程",
          "level": 3,
          "pointer": "/sections/6/children/0",
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

## 七、完整示例

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
      "pointer": "/sections/6",
      "anchor": "technical_solution",
      "children": [
        {
          "id": "processing_flow",
          "title": "处理流程",
          "level": 3,
          "pointer": "/sections/6/children/0",
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
      "pointer": "/sections/6",
      "anchor": "technical_solution",
      "children": [
        {
          "type": "paragraph",
          "pointer": "/sections/6/blocks/0",
          "text": "本发明提供一种图像检测方法。"
        },
        {
          "type": "section",
          "id": "processing_flow",
          "title": "处理流程",
          "level": 3,
          "pointer": "/sections/6/children/0",
          "anchor": "processing_flow",
          "children": [
            {
              "type": "list",
              "pointer": "/sections/6/children/0/blocks/0",
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

## 八、与 disclosure.json 的区别

可以用一句话概括：

- `disclosure.json` 负责“怎么存”
- `render_ast` 负责“怎么展示”

因此：

- `disclosure.json` 更偏存储结构
- `render_ast` 更偏展示结构

典型区别包括：

1. `render_ast` 显式提供 `outline`
2. `render_ast` 显式提供 `level`
3. `render_ast` 显式提供 `anchor`
4. `render_ast` 将正文统一组织成渲染树

## 九、与 Markdown 的关系

Markdown 不是 `render_ast` 的替代品，而是 `render_ast` 的一种导出结果。

建议关系如下：

```text
disclosure.json -> render_ast -> Markdown
```

因此：

- 前端渲染不直接依赖 Markdown
- Markdown 导出与前端展示应共享同一套 `render_ast` 语义

## 十、V1 取舍

### V1 先做

- `document`
- `section`
- `paragraph`
- `list`
- `image`
- `table`
- `outline`
- `pointer`
- `anchor`

### V1 暂不做

- inline 富文本 mark 结构
- 跨段批注
- block diff 模型
- 富样式 token 系统
- 前端可编辑状态描述

## 十一、当前结论

V1 的 `render_ast` 定位为：

1. 后端生成的统一展示模型
2. 前端渲染区的直接输入
3. Markdown 导出的统一语义来源

其核心结构为：

- 顶层 `document`
- 目录树 `outline`
- 正文树 `children`
- 节点类型 `section / paragraph / list / image / table`
- 关键定位字段 `pointer / anchor`
