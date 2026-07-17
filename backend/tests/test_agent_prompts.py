from __future__ import annotations

from app.agents.prompts import build_main_agent_system_prompt
from app.agents.prompts.main_agent import build_main_agent_system_prompt as build_direct_main_agent_system_prompt


def test_agent_prompt_entrypoint_is_main_agent_only() -> None:
    assert build_main_agent_system_prompt is build_direct_main_agent_system_prompt
    prompt = build_main_agent_system_prompt()
    for removed in ("子 agent", "execute_subagent", "write_pipe", "finish({})", "innovation_kernel_kit"):
        assert removed not in prompt


def test_main_agent_prompt_prioritizes_current_user_and_reads_authoritative_sources() -> None:
    prompt = build_main_agent_system_prompt()

    assert "当前用户最新输入是本轮任务" in prompt
    assert "用户指定的材料、附件、路径、链接、图纸、论文、实验记录、源码或日志是优先依据" in prompt
    assert "已经能稳定说明技术问题、核心构思、必要关系、关键边界和技术效果时停止探索" in prompt
    assert "先定位并精读相关正文" in prompt
    assert "不得根据目录预览、历史摘要或印象改写关键内容" in prompt
    assert "先区分事实整理与方案设计" in prompt
    assert "不得冒充既有事实" in prompt
    assert "阻碍交付或会实质改变方案时才向用户确认" in prompt


def test_main_agent_prompt_scopes_technical_work_and_uses_cross_domain_dimensions() -> None:
    prompt = build_main_agent_system_prompt()

    assert "适用于新建或实质改写技术方案、具体实施方式或核心机理" in prompt
    assert "纯文字润色、术语修正和不改变机制的局部更新，只处理受影响部分" in prompt
    for term in ("结构或装置", "材料或配方", "工艺", "软件、数据或控制方案"):
        assert term in prompt
    assert "不要把某一领域的框架套用于其他领域" in prompt
    assert "技术问题、核心构思、必要对象、对象间关系" in prompt
    assert "技术手段如何产生技术效果" in prompt
    assert "事实整理任务不得擅自改变已确认事实" in prompt


def test_main_agent_prompt_keeps_representations_optional_and_assigns_figure_responsibilities_once() -> None:
    prompt = build_main_agent_system_prompt()

    assert "正文必须独立讲清方案" in prompt
    assert "图、表和公式只在能降低关键关系歧义时使用，不设固定数量" in prompt
    assert "原则上应配一张最能消除歧义的图" in prompt
    assert "用户明确要求时按其目标执行" in prompt
    assert "不把正文段落逐框翻译成通用流程图" in prompt
    assert "核心命题、必须呈现的关系、适用图型或分区、主阅读方向、视觉中心和关系语法" in prompt
    for grammar in ("架构关系", "过程关系", "状态关系", "时间或滚动窗口", "队列、集合或映射关系"):
        assert grammar in prompt
    assert "不同机制无法用同一种图型讲清" in prompt
    assert "多对多连接优先通过总线、汇聚点、中间层或集合表示" in prompt
    assert "缩小到不细读文字时，仍应看出分组、层级、方向和关键机制" in prompt
    assert "复杂图可按视觉骨架、核心结构或主路径、必要分支或第二机制" in prompt
    assert "figure_kit 的 visualRole 表达视觉职责" in prompt
    assert "显式样式只覆盖确需偏离的部分" in prompt
    assert "不是固定工作流" in prompt
    assert "节点只保留职责名称和关键对象或动作" in prompt
    assert "同类节点尺寸、对齐、间距和样式一致" in prompt
    assert "主次关系用位置、留白、灰度和线宽区分" in prompt
    assert "不用默认样式临场拼凑" in prompt
    assert "黑白不等于相同矩形和简单连线" in prompt
    assert "必须先按 figure_kit 提供的唯一完整示例生成" in prompt
    assert "一次返回的全部 errors 合并修复" in prompt
    assert "warnings 不阻断渲染" in prompt
    assert "连续失败或次数将尽时使用最近稳定图" in prompt
    assert "公式只用于可精确定义" in prompt


def test_main_agent_prompt_requires_final_asset_consistency_without_overclaiming_check() -> None:
    prompt = build_main_agent_system_prompt()

    assert "本轮新增或修改的图、表、公式必须与最终正文一致" in prompt
    assert "最后一次看图后正文实质改变" in prompt
    assert "figure_kit.read 取得当前 XML 和截图后重新核对" in prompt
    assert "figure_kit.check 只检查引用、展示和资源一致性" in prompt
    assert "不代表附图技术语义完整" in prompt
    assert "无关的正文修改不要求遍历或重做全部资产" in prompt


def test_main_agent_prompt_uses_engineer_language_and_non_mechanical_structure() -> None:
    prompt = build_main_agent_system_prompt()

    assert "只写最终态正文" in prompt
    assert "不写对话过程、修改过程、内部检查或方案迭代说明" in prompt
    assert "技术人员向专利代理人员解释方案的工程语言" in prompt
    assert "避免“本发明”“保护范围”“实施例一/二”等正式申请腔" in prompt
    assert "工程变量、字段、状态枚举、接口、函数、框架、schema、伪代码或公式" in prompt
    assert "不能成为正文主线" in prompt
    assert "使用子章节能显著改善阅读时再拆分" in prompt
    assert "避免机械拆章" in prompt
    assert "通常 1–2 条，最多 3 条" in prompt


def test_main_agent_prompt_defines_minimal_tool_strategy_and_document_write_boundary() -> None:
    prompt = build_main_agent_system_prompt()

    assert "必须通过工具落盘，不能只在回复中给出一份正文" in prompt
    assert "disclosure_outline 或 disclosure_search 定位" in prompt
    assert "disclosure_read_section 精读" in prompt
    assert "disclosure_edit 小步写入" in prompt
    assert "已有足够精确原文时不重复读取" in prompt
    assert "更新原对象；只有用户明确要求另行新增时才新建" in prompt
    assert "具体参数、返回值、失败条件和编辑上限以工具声明为准" in prompt
    assert "完成后以简洁中文说明结果" in prompt
    assert "不要重复 create" not in prompt


def test_main_agent_prompt_is_compact_and_avoids_repeated_checklists() -> None:
    prompt = build_main_agent_system_prompt()

    assert len(prompt) < 3_500
    assert prompt.count("一次返回的全部 errors") == 1
    assert prompt.count("figure_kit.check") == 1
    assert "微小阶梯、短折返、标签遮线" not in prompt
    assert "fontFamily=" not in prompt
