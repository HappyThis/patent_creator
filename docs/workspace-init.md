# 工作区初始化规范

## 文档定位

本文档定义系统内部“工作区”的初始化方式。

这里的“工作区”是技术实现概念，不是用户前端界面概念。

对用户来说，用户操作的是“交底书项目”；
对系统来说，每个交底书项目都会对应一个内部工作区。

相关文档：

- [专利交底书结构方案](/Users/yangchaoqun/myProj/patent_creator/docs/patent-disclosure-structure.md)
- [Agent 基本设计原则](/Users/yangchaoqun/myProj/patent_creator/docs/agent-principles.md)
- [Tools 设计](/Users/yangchaoqun/myProj/patent_creator/docs/tools.md)
- [Session 事件日志 Schema](/Users/yangchaoqun/myProj/patent_creator/docs/session-log.md)
- [技术栈规范](/Users/yangchaoqun/myProj/patent_creator/docs/tech-stack.md)

## 目标

本文档用于明确：

1. 什么是工作区
2. 何时初始化工作区
3. 工作区目录结构是什么
4. 初始化时需要创建哪些文件和目录
5. git 如何在工作区内初始化

## 一、工作区定义

一个工作区对应一个专利项目的内部目录。

也就是说：

- 一个新建交底书项目
- 对应一个新的内部工作区

工作区的职责是承载该项目的：

- 当前交底书文档
- session 事件日志
- 资源文件
- 导出结果
- 运行时临时文件
- git 版本历史

实现约束：

- 使用本地文件系统，不引入数据库
- Python 依赖通过 `uv` 管理

## 二、初始化时机

在以下场景初始化工作区：

1. 用户明确点击“新建交底书”或“新建项目”
2. 系统明确判断当前要开始一份新的交底书，而不是继续已有项目

一旦确认创建新项目，就自动初始化一个新的工作区。

## 三、工作区目录结构

运行数据默认保存在当前系统用户目录下：

```text
~/.patent_creator/
  current_project_id
  projects/
```

可通过环境变量 `PATENT_CREATOR_DATA_DIR` 覆盖该目录。运行数据位于数据目录内，避免会话日志、导出文件和运行态内容混入源代码目录。

每个 project 对应一个内部工作区：

```text
~/.patent_creator/projects/{project_id}/
  project.json
  disclosure.json
  sessions/
  assets/
  exports/
  runtime/
```

### 1. project.json

用于保存项目级元数据。

字段：

- `project_id`
- `title`
- `created_at`
- `updated_at`
- `schema_version`

示例：

```json
{
  "project_id": "pat_20260423_001",
  "title": "未命名交底书",
  "created_at": "2026-04-23T18:00:00+08:00",
  "updated_at": "2026-04-23T18:00:00+08:00",
  "schema_version": "v1"
}
```

### 2. disclosure.json

用于保存当前交底书文档真相源。

该文件应遵循：

- [专利交底书结构方案](/Users/yangchaoqun/myProj/patent_creator/docs/patent-disclosure-structure.md)

初始化时不应为空文件，而应写入标准骨架。

### 3. sessions/

用于保存 session 事件日志文件。

每个 session 使用一个 `jsonl` 文件，例如：

```text
sessions/
  2026-04-23-001.jsonl
  2026-04-23-002.jsonl
```

### 4. assets/

用于保存系统内部资源文件。

例如：

- agent 生成的附图
- 渲染中间产物
- 内部图片资源

### 5. exports/

用于保存导出的结果文件。

保存内容包括：

- Markdown 导出结果

### 6. runtime/

用于保存运行时临时文件。

例如：

- 调试中间结果
- 临时渲染产物
- 缓存文件

说明：

`runtime/` 不是真相源目录。

## 四、初始化动作

创建新工作区时自动执行以下步骤：

1. 创建工作区根目录
2. 创建 `sessions/`
3. 创建 `assets/`
4. 创建 `exports/`
5. 创建 `runtime/`
6. 写入 `project.json`
7. 写入标准骨架 `disclosure.json`
8. 初始化 git 仓库
9. 写入 `.gitignore`
10. 执行首次 commit

## 五、初始 disclosure.json

初始化时写入标准交底书骨架，而不是空文档。

示例：

```json
{
  "meta": {
    "document_type": "patent_disclosure",
    "schema_version": "v1",
    "title": "未命名交底书",
    "id_counters": {
      "block": 0
    }
  },
  "sections": [
    {
      "id": "title",
      "title": "发明名称",
      "blocks": [],
      "children": []
    },
    {
      "id": "technical_field",
      "title": "技术领域",
      "blocks": [],
      "children": []
    },
    {
      "id": "background_technology",
      "title": "背景技术",
      "blocks": [],
      "children": []
    },
    {
      "id": "existing_solution",
      "title": "现有技术方案",
      "blocks": [],
      "children": []
    },
    {
      "id": "existing_solution_defects",
      "title": "现有技术缺陷",
      "blocks": [],
      "children": []
    },
    {
      "id": "technical_problem",
      "title": "要解决的技术问题",
      "blocks": [],
      "children": []
    },
    {
      "id": "technical_solution",
      "title": "技术方案",
      "blocks": [],
      "children": []
    },
    {
      "id": "key_innovations",
      "title": "关键创新点",
      "blocks": [],
      "children": []
    },
    {
      "id": "embodiments",
      "title": "具体实施方式",
      "blocks": [],
      "children": []
    },
    {
      "id": "technical_effects",
      "title": "技术效果",
      "blocks": [],
      "children": []
    },
    {
      "id": "drawings",
      "title": "附图说明",
      "blocks": [],
      "children": []
    },
    {
      "id": "claim_suggestions",
      "title": "权利要求建议",
      "blocks": [],
      "children": []
    }
  ]
}
```

这样做的好处是：

- 前端目录区域可以立即渲染
- 渲染区可以立即展示一份空骨架交底书
- agent 一开始就能围绕标准章节工作
- 标准章节 id 与工具、渲染和日志保持一致

## 六、git 初始化规则

每个工作区都是独立 git 仓库。

原因：

1. 每个专利项目的历史版本天然隔离
2. commit message 可以直接围绕这份交底书生成
3. diff、回退、审计都更清晰

初始化步骤为：

1. `git init`
2. 写入 `.gitignore`
3. 初始 `git add`
4. 初始 `git commit`

初始 commit message：

```text
init disclosure workspace

Time: YYYY-MM-DD HH:mm
```

## 七、与前端产品形态的关系

虽然系统内部存在工作区，但前端不应向用户暴露“工作区”概念。

用户前端只需要感知：

- 当前交底书目录
- 当前交底书渲染结果
- 与 agent 的对话

也就是说：

- 工作区是内部实现概念
- 项目是用户可感知概念

## 八、设计结论

一个新交底书项目对应一个新的内部工作区。

工作区初始化规范固定为：

```text
~/.patent_creator/projects/{project_id}/
  project.json
  disclosure.json
  sessions/
  assets/
  exports/
  runtime/
  .git/
```

并且初始化时应自动完成：

1. 创建目录结构
2. 写入项目元数据
3. 写入交底书标准骨架
4. 初始化 git 仓库
5. 创建首次提交
