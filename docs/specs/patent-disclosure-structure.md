# 专利交底书结构方案

## 文档定位

本文档是交底书文档本体的基础规范，属于底层定义文档。

建议先阅读本文档，再阅读后续 agent 相关文档。

相关文档：

- [Agent 基本设计原则](../core/agent-principles.md)
- [Agent Prompt 与上下文规范](../core/agent-prompt-context-spec.md)

## 目标

本文档定义本项目中专利交底书的标准文档结构。

这份结构方案只描述“交底书文档本体”，不包含 session 内存、agent 推理过程、审查意见、待确认问题或其他运行态上下文。

## 设计原则

1. 交底书文档只承载真正的专利内容。
2. session 内存与 agent 运行态数据不属于交底书 schema。
3. 交底书内部采用树形结构，而不是简单的线性 markdown 文本。
4. 交底书支持章节与子章节层级。
5. 交底书支持段落、列表、图片、表格等块级内容。
6. Markdown 只是导出格式，不是内部真相源。
7. 交底书内部定位采用稳定 id 体系。

## 边界说明

本结构中包含：

- 文档元信息
- 章节树
- 子章节
- 段落
- 列表
- 图片
- 表格

本结构中不包含：

- session 上下文摘要
- agent 假设
- 待确认问题
- 审查结论
- 任务调度记录
- patch 候选内容

## 顶层结构

交底书文档是一个 JSON 对象，顶层只包含两个字段：

- `meta`
- `sections`

示例：

```json
{
  "meta": {
    "document_type": "patent_disclosure",
    "schema_version": "v2",
    "title": "一种图像检测方法",
    "id_counters": {
      "section": 0,
      "block": 0
    }
  },
  "sections": []
}
```

## Meta

`meta` 用于存放文档级信息。

建议字段：

- `document_type`：固定为 `patent_disclosure`
- `schema_version`：当前结构版本，例如 `v2`
- `title`：交底书标题
- `id_counters`：系统维护的 id 计数器

最小示例：

```json
{
  "document_type": "patent_disclosure",
  "schema_version": "v2",
  "title": "一种图像检测方法",
  "id_counters": {
    "section": 0,
    "block": 0
  }
}
```

说明：

- `id_counters` 由系统维护。
- agent 不直接修改 `id_counters`。
- 新增 section id 由文档写入工具生成。
- 新增 block id 由文档写入工具生成。

## Id 体系

交底书文档采用系统生成 id 体系。

核心原则：

1. `id` 只表示机器身份，不表达章节语义。
2. 章节语义由 `type` 表达。
3. 展示标题由 `title` 表达。
4. agent 不为新增 section 或 block 手写 id。

### Section id

`section.id` 是章节和子章节的稳定标识。

规则：

1. 使用 `sec_000001` 格式。
2. 全文唯一。
3. 新增 section 时由文档写入工具生成。
4. 替换 section 时保留原 section id。
5. section id 不携带 `technical_solution`、`technical_effects` 等业务语义。

示例：

```text
sec_000001
sec_000002
sec_000003
```

### Section type

`section.type` 表示章节在交底书结构中的语义角色。

规则：

1. `type` 使用受控枚举。
2. 标准交底书章节使用标准 type。
3. 普通子章节使用 `custom`。
4. 系统业务逻辑需要识别“技术方案”等标准章节时，基于 `type` 判断，不基于 `id` 判断。

标准 type：

```text
title
technical_field
background_technology
existing_solution
existing_solution_defects
technical_problem
technical_solution
key_innovations
embodiments
technical_effects
drawings
claim_suggestions
custom
```

### Block id

`block.id` 是块级内容的稳定标识。

规则：

1. 使用 `blk_000001` 格式。
2. 全文唯一。
3. 新增 block 时由文档写入工具生成。
4. 替换 block 时保留原 block id。
5. `list` item 和 `table` cell 不单独建立 id。

示例：

```text
blk_000001
blk_000002
```

### 定位原则

文档读取、修改、渲染、日志和前端定位统一使用：

```text
section_id
block_id
```

数组下标和文件内部位置不作为公开协议。

## 章节树

交底书正文存放在 `sections` 中。

每个章节可以包含：

- `blocks`
- `children`

这意味着正文整体是树形结构，支持：

- 一级章节
- 二级子章节

章节深度规则：

- 默认支持两级章节结构
- 不支持无限深嵌套

### 章节对象

```json
{
  "id": "sec_000007",
  "type": "technical_solution",
  "title": "技术方案",
  "blocks": [],
  "children": []
}
```

字段说明：

- `id`：系统生成的稳定机器身份
- `type`：章节语义角色
- `title`：人类可读章节标题
- `blocks`：该章节直接包含的块级内容
- `children`：子章节列表

## 标准交底书章节目录

`专利交底书结构方案` 推荐使用以下标准章节 type：

1. `title`：发明名称
2. `technical_field`：技术领域
3. `background_technology`：背景技术
4. `existing_solution`：现有技术方案
5. `existing_solution_defects`：现有技术缺陷
6. `technical_problem`：要解决的技术问题
7. `technical_solution`：技术方案
8. `key_innovations`：关键创新点
9. `embodiments`：具体实施方式
10. `technical_effects`：技术效果
11. `drawings`：附图说明
12. `claim_suggestions`：权利要求建议

## 推荐子章节模式

以下子章节模式为建议结构。

### `technical_solution`

建议子章节：

- 整体架构
- 模块设计
- 处理流程
- 可选变体

### `embodiments`

建议子章节：

- 实施例一
- 实施例二
- 替代实施方式

### `drawings`

建议子章节：

- 图1说明
- 图2说明
- 图N说明

## 块级内容模型

每个章节或子章节内部使用 `blocks` 承载内容。

支持四类 block：

- `paragraph`
- `list`
- `image`
- `table`

### Paragraph

```json
{
  "id": "blk_000001",
  "type": "paragraph",
  "text": "本发明提供一种图像检测方法。"
}
```

字段说明：

- `id`：稳定的 block 标识
- `type`：固定为 `paragraph`
- `text`：段落文本

### List

```json
{
  "id": "blk_000002",
  "type": "list",
  "ordered": true,
  "items": [
    "获取输入图像",
    "执行特征提取",
    "输出检测结果"
  ]
}
```

字段说明：

- `id`：稳定的 block 标识
- `type`：固定为 `list`
- `ordered`：`true` 表示有序列表，`false` 表示无序列表
- `items`：字符串数组

### Image

```json
{
  "id": "blk_000003",
  "type": "image",
  "src": "assets/fig1.png",
  "caption": "图1 系统整体架构图",
  "alt": "系统整体架构图"
}
```

字段说明：

- `id`：稳定的 block 标识
- `type`：固定为 `image`
- `src`：相对或绝对资源路径
- `caption`：图片标题
- `alt`：替代文本

说明：

- 图标统一视为图片处理
- 流程图作为图片处理

### Table

```json
{
  "id": "blk_000004",
  "type": "table",
  "columns": ["模块", "作用"],
  "rows": [
    ["特征提取模块", "提取图像特征"],
    ["检测模块", "输出目标位置"]
  ]
}
```

字段说明：

- `id`：稳定的 block 标识
- `type`：固定为 `table`
- `columns`：表头数组
- `rows`：表格行数组

## 完整示例

```json
{
  "meta": {
    "document_type": "patent_disclosure",
    "schema_version": "v2",
    "title": "一种图像检测方法",
    "id_counters": {
      "section": 15,
      "block": 4
    }
  },
  "sections": [
    {
      "id": "sec_000002",
      "type": "technical_field",
      "title": "技术领域",
      "blocks": [
        {
          "id": "blk_000001",
          "type": "paragraph",
          "text": "本发明涉及计算机视觉技术领域，尤其涉及一种图像检测方法。"
        }
      ],
      "children": []
    },
    {
      "id": "sec_000007",
      "type": "technical_solution",
      "title": "技术方案",
      "blocks": [
        {
          "id": "blk_000002",
          "type": "paragraph",
          "text": "本发明提供一种低计算量的图像检测方案。"
        }
      ],
      "children": [
        {
          "id": "sec_000013",
          "type": "custom",
          "title": "处理流程",
          "blocks": [
            {
              "id": "blk_000003",
              "type": "list",
              "ordered": true,
              "items": [
                "获取输入图像",
                "通过特征提取模块生成特征图",
                "基于检测模块输出目标检测结果"
              ]
            }
          ],
          "children": []
        }
      ]
    },
    {
      "id": "sec_000011",
      "type": "drawings",
      "title": "附图说明",
      "blocks": [],
      "children": [
        {
          "id": "sec_000015",
          "type": "custom",
          "title": "图1说明",
          "blocks": [
            {
              "id": "blk_000004",
              "type": "image",
              "src": "assets/fig1.png",
              "caption": "图1 系统整体架构图",
              "alt": "系统整体架构图"
            }
          ],
          "children": []
        }
      ]
    }
  ]
}
```

## Markdown 导出约定

Markdown 导出应基于这棵文档树渲染。

推荐映射规则：

- 一级章节 -> `##`
- 二级子章节 -> `###`
- 段落 -> 普通段落
- 有序列表 -> 编号列表
- 无序列表 -> 项目符号列表
- 图片 -> markdown 图片语法，并在下方保留标题
- 表格 -> markdown 表格

## 实现说明

1. 交底书文件中只保存文档内容本身。
2. session 运行态内存不进入该文件。
3. agent 编排信息进入 session 日志，不进入交底书文档。
4. 由主 agent 决定每次任务需要读取哪些章节或子章节。
5. 文档内容通过系统生成的 `section_id` 和 `block_id` 进行索引、读取和修改。
6. 需要按交底书语义定位标准章节时，基于 `section.type` 判断，不基于 `section.id` 判断。

## 最终结论

本项目的标准交底书格式为：

- 基于 JSON 的交底书文档
- 章节/子章节树结构
- 支持块级内容
- 支持系统生成的稳定 section id 与 block id
- 支持通过 section type 表达章节语义
- 不嵌入 session 内存或 agent 辅助信息

这就是专利写作 agent 的交底书文档基础结构。
