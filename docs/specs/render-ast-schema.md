# render_ast Schema

## 文档定位

`render_ast` 是交底书展示层的中间模型。它不是内部真相源，也不是导出格式。

数据流：

```text
disclosure.json v3 -> render builder -> render_ast -> React 渲染
disclosure.json v3 -> render builder -> render_ast -> Markdown 导出
```

说明：

- `disclosure.json` 是存储真相源。
- `render_ast.children` 是展示树字段，不代表存储结构中的 `children`。
- 存储结构中的子章节字段是 `sections`。

## 顶层结构

```json
{
  "type": "document",
  "title": "一种图像检测方法",
  "meta": {
    "document_type": "patent_disclosure",
    "schema_version": "v3"
  },
  "outline": [],
  "children": []
}
```

字段说明：

- `type`：固定为 `document`
- `title`：展示标题，通常来自“发明名称”章节正文
- `meta`：展示层需要的少量元信息
- `outline`：目录树
- `children`：正文展示树

## OutlineItem

```json
{
  "id": "sec_000007",
  "title": "技术方案",
  "level": 2,
  "anchor": "sec_000007",
  "children": []
}
```

`outline.children` 只用于前端目录嵌套展示，不是存储层字段。

## SectionNode

```json
{
  "type": "section",
  "id": "sec_000007",
  "title": "技术方案",
  "level": 2,
  "anchor": "sec_000007",
  "children": []
}
```

字段说明：

- `id`：section id
- `title`：section title block 的文本
- `level`：展示层级
- `anchor`：滚动定位锚点
- `children`：该 section 下的展示节点，包括正文 block 和子 section

不包含：

- `section_type`
- v2 存储层 `children`

## BlockNode

### paragraph / title

title block 通常用于 section 标题，不会作为正文子节点重复渲染。若展示层收到 `title` 节点，可按 paragraph 文本渲染。

```json
{
  "type": "paragraph",
  "id": "blk_000020",
  "section_id": "sec_000007",
  "text": "本方案包括..."
}
```

### list

```json
{
  "type": "list",
  "id": "blk_000021",
  "section_id": "sec_000007",
  "ordered": false,
  "items": ["第一项", "第二项"]
}
```

### image

```json
{
  "type": "image",
  "id": "blk_000022",
  "section_id": "sec_000007",
  "src": "assets/figure-1.png",
  "caption": "图 1 系统结构示意图",
  "alt": "系统结构"
}
```

### table

```json
{
  "type": "table",
  "id": "blk_000023",
  "section_id": "sec_000007",
  "columns": ["模块", "职责"],
  "rows": [["采集模块", "获取输入数据"]]
}
```

## 前端约束

前端只消费 `render_ast`，不直接推断 `disclosure.json` 的存储结构。

渲染层可依赖：

- section `id`
- block `id`
- block `section_id`
- section `anchor`

渲染层不应依赖：

- section 业务类型
- 存储层字段名
- block 在 JSON 数组中的原始偏移
