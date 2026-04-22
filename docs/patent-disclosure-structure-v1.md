# 专利交底书结构方案 v1

## 文档定位

本文档是交底书文档本体的基础规范，属于底层定义文档。

建议先阅读本文档，再阅读后续 agent 相关文档。

相关文档：

- [Agent 基本设计原则 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles-v1.md)
- [Agent Prompt 与上下文规范 v1](/Users/yangchaoqun/myProj/patent_creator/docs/agent-prompt-context-spec-v1.md)

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
- sub agent 返回结果
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
    "schema_version": "v1",
    "title": "一种图像检测方法"
  },
  "sections": []
}
```

## Meta

`meta` 用于存放文档级信息。

建议字段：

- `document_type`：固定为 `patent_disclosure`
- `schema_version`：当前结构版本，例如 `v1`
- `title`：交底书标题

最小示例：

```json
{
  "document_type": "patent_disclosure",
  "schema_version": "v1",
  "title": "一种图像检测方法"
}
```

## 章节树

交底书正文存放在 `sections` 中。

每个章节可以包含：

- `blocks`
- `children`

这意味着正文整体是树形结构，支持：

- 一级章节
- 二级子章节

v1 建议：

- 默认支持两级章节结构
- 第一版不放开无限深嵌套

### 章节对象

```json
{
  "id": "technical_solution",
  "title": "技术方案",
  "blocks": [],
  "children": []
}
```

字段说明：

- `id`：稳定的机器可读标识
- `title`：人类可读章节标题
- `blocks`：该章节直接包含的块级内容
- `children`：子章节列表

## 标准交底书章节目录

`专利交底书结构方案 v1` 推荐使用以下固定章节：

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

- `overall_architecture`：整体架构
- `module_design`：模块设计
- `processing_flow`：处理流程
- `optional_variants`：可选变体

### `embodiments`

建议子章节：

- `embodiment_1`：实施例一
- `embodiment_2`：实施例二
- `alternative_embodiments`：替代实施方式

### `drawings`

建议子章节：

- `figure_1`：图1说明
- `figure_2`：图2说明
- `figure_n`：图N说明

## 块级内容模型

每个章节或子章节内部使用 `blocks` 承载内容。

v1 支持四类 block：

- `paragraph`
- `list`
- `image`
- `table`

### Paragraph

```json
{
  "type": "paragraph",
  "text": "本发明提供一种图像检测方法。"
}
```

字段说明：

- `type`：固定为 `paragraph`
- `text`：段落文本

### List

```json
{
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

- `type`：固定为 `list`
- `ordered`：`true` 表示有序列表，`false` 表示无序列表
- `items`：字符串数组

### Image

```json
{
  "type": "image",
  "src": "assets/fig1.png",
  "caption": "图1 系统整体架构图",
  "alt": "系统整体架构图"
}
```

字段说明：

- `type`：固定为 `image`
- `src`：相对或绝对资源路径
- `caption`：图片标题
- `alt`：替代文本

说明：

- 图标统一视为图片处理
- 流程图在 v1 中可先作为图片处理

### Table

```json
{
  "type": "table",
  "columns": ["模块", "作用"],
  "rows": [
    ["特征提取模块", "提取图像特征"],
    ["检测模块", "输出目标位置"]
  ]
}
```

字段说明：

- `type`：固定为 `table`
- `columns`：表头数组
- `rows`：表格行数组

## 完整示例

```json
{
  "meta": {
    "document_type": "patent_disclosure",
    "schema_version": "v1",
    "title": "一种图像检测方法"
  },
  "sections": [
    {
      "id": "technical_field",
      "title": "技术领域",
      "blocks": [
        {
          "type": "paragraph",
          "text": "本发明涉及计算机视觉技术领域，尤其涉及一种图像检测方法。"
        }
      ],
      "children": []
    },
    {
      "id": "technical_solution",
      "title": "技术方案",
      "blocks": [
        {
          "type": "paragraph",
          "text": "本发明提供一种低计算量的图像检测方案。"
        }
      ],
      "children": [
        {
          "id": "processing_flow",
          "title": "处理流程",
          "blocks": [
            {
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
      "id": "drawings",
      "title": "附图说明",
      "blocks": [],
      "children": [
        {
          "id": "figure_1",
          "title": "图1说明",
          "blocks": [
            {
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
4. sub agent 默认处理局部子树，而不是默认加载整篇文档。
5. 由主 agent 决定每次任务需要读取哪些章节或子章节。

## 最终结论

本项目的标准交底书格式为：

- 基于 JSON 的交底书文档
- 章节/子章节树结构
- 支持块级内容
- 不嵌入 session 内存或 agent 辅助信息

这就是专利写作 agent v1 的交底书文档基础结构。
