import { ChatEvent, ProjectState, RenderAst, RenderNode } from '../types';

export const initialProject: ProjectState = {
  project_id: 'patent_creator',
  title: '一种图像检测方法',
  created_at: '2026-04-23T21:00:00+08:00',
  updated_at: '2026-04-23T21:10:00+08:00',
  active_session_id: 'sess_003',
  running_session_id: null,
  is_busy: false,
  running_round_id: null,
};

export const initialRenderAst: RenderAst = {
  type: 'document',
  title: '一种图像检测方法',
  meta: {
    document_type: 'patent_disclosure',
    schema_version: 'v1',
  },
  outline: [
    { id: 'technical_field', title: '技术领域', level: 2, anchor: 'technical_field' },
    { id: 'background_technology', title: '背景技术', level: 2, anchor: 'background_technology' },
    {
      id: 'technical_solution',
      title: '技术方案',
      level: 2,
      anchor: 'technical_solution',
      children: [
        { id: 'overall_architecture', title: '整体架构', level: 3, anchor: 'overall_architecture' },
        { id: 'processing_flow', title: '处理流程', level: 3, anchor: 'processing_flow' },
      ],
    },
    { id: 'embodiments', title: '具体实施方式', level: 2, anchor: 'embodiments' },
    { id: 'technical_effects', title: '有益效果', level: 2, anchor: 'technical_effects' },
  ],
  children: [
    {
      type: 'section',
      id: 'technical_field',
      title: '技术领域',
      level: 2,
      anchor: 'technical_field',
      children: [
        {
          type: 'paragraph',
          id: 'blk_000001',
          section_id: 'technical_field',
          text: '本发明涉及计算机视觉与边缘推理技术领域，尤其涉及一种适用于低算力终端的图像检测方法及系统。',
        },
      ],
    },
    {
      type: 'section',
      id: 'background_technology',
      title: '背景技术',
      level: 2,
      anchor: 'background_technology',
      children: [
        {
          type: 'paragraph',
          id: 'blk_000002',
          section_id: 'background_technology',
          text: '现有图像检测方案通常依赖较大模型与持续高负载推理，导致在边缘设备上存在时延高、能耗高、稳定性不足等问题。',
        },
        {
          type: 'paragraph',
          id: 'blk_000003',
          section_id: 'background_technology',
          text: '此外，现有实现通常缺少对多阶段候选区域筛选和轻量化特征重用机制的统一设计，难以在保证精度的同时控制推理成本。',
        },
      ],
    },
    {
      type: 'section',
      id: 'technical_solution',
      title: '技术方案',
      level: 2,
      anchor: 'technical_solution',
      children: [
        {
          type: 'paragraph',
          id: 'blk_000004',
          section_id: 'technical_solution',
          text: '本发明提供一种图像检测方法，通过候选区域生成、分层特征提取与结果校正三段式流程，在受限算力环境下完成目标检测。',
        },
        {
          type: 'section',
          id: 'overall_architecture',
          title: '整体架构',
          level: 3,
          anchor: 'overall_architecture',
          children: [
            {
              type: 'table',
              id: 'blk_000005',
              section_id: 'overall_architecture',
              columns: ['模块', '作用'],
              rows: [
                ['输入预处理模块', '统一分辨率与曝光参数'],
                ['候选区域模块', '快速筛出疑似目标区域'],
                ['轻量推理模块', '对候选区域做检测分类'],
                ['结果校正模块', '抑制误检并输出最终框'],
              ],
            },
          ],
        },
        {
          type: 'section',
          id: 'processing_flow',
          title: '处理流程',
          level: 3,
          anchor: 'processing_flow',
          children: [
            {
              type: 'list',
              id: 'blk_000006',
              section_id: 'processing_flow',
              ordered: true,
              items: [
                '获取输入图像并执行尺寸标准化与噪声抑制。',
                '对输入图像做快速候选区域筛选，生成目标候选集合。',
                '对候选集合执行轻量化特征提取与分类回归推理。',
                '对初步检测结果执行时序平滑与阈值校正，输出最终检测结果。',
              ],
            },
          ],
        },
      ],
    },
    {
      type: 'section',
      id: 'embodiments',
      title: '具体实施方式',
      level: 2,
      anchor: 'embodiments',
      children: [
        {
          type: 'paragraph',
          id: 'blk_000007',
          section_id: 'embodiments',
          text: '在一个实施例中，系统部署于 ARM 边缘终端，候选区域模块采用低分辨率先验筛查，轻量推理模块采用剪枝后的卷积网络。',
        },
      ],
    },
    {
      type: 'section',
      id: 'technical_effects',
      title: '有益效果',
      level: 2,
      anchor: 'technical_effects',
      children: [
        {
          type: 'paragraph',
          id: 'blk_000008',
          section_id: 'technical_effects',
          text: '通过分层筛选与轻量推理结合，本发明能够在保持检测精度的同时降低平均推理时延和功耗，适合边缘端长期运行。',
        },
      ],
    },
  ],
};

export const replacementTechnicalSolutionChildren: RenderNode[] = [
  {
    type: 'paragraph',
    id: 'blk_000014',
    section_id: 'technical_solution',
    text: '本发明提供一种适用于低算力终端的图像检测方法，通过候选区域优先筛选、轻量化特征提取与结果校正的协同机制，在有限算力预算下实现实时检测。',
  },
  {
    type: 'paragraph',
    id: 'blk_000015',
    section_id: 'technical_solution',
    text: '系统在每一帧处理时优先复用上一时刻的稳定特征，仅对新增或变化显著区域触发完整推理，以减少冗余计算并压缩端到端时延。',
  },
  {
    type: 'section',
    id: 'overall_architecture',
    title: '整体架构',
    level: 3,
    anchor: 'overall_architecture',
    children: [
      {
        type: 'table',
        id: 'blk_000016',
        section_id: 'overall_architecture',
        columns: ['模块', '实时性约束'],
        rows: [
          ['输入预处理模块', '单帧处理时间不超过 8ms'],
          ['候选区域模块', '优先使用低成本先验筛选'],
          ['轻量推理模块', '仅对候选区域触发完整特征提取'],
          ['结果校正模块', '基于时序缓冲降低抖动与误检'],
        ],
      },
    ],
  },
  {
    type: 'section',
    id: 'processing_flow',
    title: '处理流程',
    level: 3,
    anchor: 'processing_flow',
    children: [
      {
        type: 'list',
        id: 'blk_000017',
        section_id: 'processing_flow',
        ordered: true,
        items: [
          '接收输入图像并读取上一帧缓存特征与候选区域状态。',
          '对当前帧执行快速预筛查，仅保留满足置信度和尺寸约束的候选区域。',
          '对候选区域执行轻量化特征提取与检测分类回归。',
          '根据时序一致性约束对结果做校正，并输出检测框与类别结果。',
        ],
      },
    ],
  },
];

export const initialEvents: ChatEvent[] = [
  {
    id: 'msg_user_001',
    kind: 'message',
    role: 'user',
    text: '请把技术方案部分写得更完整一点，并强调低算力实时性。',
    timestamp: '21:10',
  },
  {
    id: 'msg_assistant_001',
    kind: 'message',
    role: 'assistant',
    text: '我会补充整体架构和处理流程，并把实时性目标写进技术效果。',
    timestamp: '21:10',
  },
  {
    id: 'evt_tool_001',
    kind: 'tool_call',
    title: 'document_read',
    tool: 'document_read',
    status: 'done',
    scope: 'main',
    summary: '读取 technical_solution 和 technical_effects',
    detail: 'action: get_section',
  },
  {
    id: 'evt_tool_002',
    kind: 'tool_call',
    title: 'execute_subagent',
    tool: 'execute_subagent',
    status: 'done',
    scope: 'main',
    summary: 'section_writer 已返回候选 proposal',
    detail: 'proposal.type=document_edit_proposal',
  },
  {
    id: 'msg_assistant_002',
    kind: 'message',
    role: 'assistant',
    text: '我已经补充了技术方案和有益效果中的关键约束，下一步可以继续补实施例细节。',
    timestamp: '21:10',
  },
];
