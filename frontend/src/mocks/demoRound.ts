import { ChatEvent, ProcessEvent, ProjectState, RenderAst, RenderNode, SessionTab } from '../types';
import { replacementTechnicalSolutionChildren } from './demoData';

const formatTimestamp = () =>
  new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });

export const initialSelectionState = {
  activeSectionId: 'technical_solution',
  activeBlockId: 'blk_000006' as string | null,
  recentSectionIds: ['technical_solution'],
  recentBlockIds: ['blk_000006'],
};

export const updatedSelectionState = {
  activeSectionId: 'technical_solution',
  activeBlockId: 'blk_000014' as string | null,
  recentSectionIds: ['technical_solution'],
  recentBlockIds: ['blk_000014', 'blk_000015', 'blk_000016', 'blk_000017'],
};

export function buildSessionTabs(project: ProjectState): SessionTab[] {
  return [
    {
      session_id: project.active_session_id ?? 'sess_001',
      updated_at: project.updated_at ?? project.created_at,
      event_count: 0,
      last_round_id: project.running_round_id,
      latest_user_text: null,
      is_active: true,
      title: '当前会话',
      subtitle: project.project_id,
      active: true,
    },
    {
      session_id: 'sess_003',
      updated_at: project.updated_at ?? project.created_at,
      event_count: 0,
      last_round_id: null,
      latest_user_text: null,
      is_active: false,
      title: 'sess_003',
      active: false,
    },
    {
      session_id: 'sess_002',
      updated_at: project.updated_at ?? project.created_at,
      event_count: 0,
      last_round_id: null,
      latest_user_text: null,
      is_active: false,
      title: 'sess_002',
      active: false,
    },
  ];
}

export function createRoundStartEvents(composer: string): ChatEvent[] {
  const timestamp = formatTimestamp();

  return [
    {
      id: `msg_user_${Date.now()}`,
      kind: 'message',
      role: 'user',
      text: composer,
      timestamp,
    },
    {
      id: `msg_assistant_plan_${Date.now() + 1}`,
      kind: 'message',
      role: 'assistant',
      text: '我会补充整体架构和处理流程，并把实时性目标写进技术效果。',
      timestamp,
    },
  ];
}

export function createRoundProcessSteps(): Array<{ delayMs: number; event: ProcessEvent }> {
  return [
    {
      delayMs: 500,
      event: {
        id: `evt_agent_${Date.now()}`,
        kind: 'agent_output',
        title: '主 agent',
        summary: '开始处理本轮需求。',
        detail: '本轮将默认采纳 section_writer proposal，并更新 technical_solution。',
      },
    },
    {
      delayMs: 500,
      event: {
        id: `evt_tool_read_${Date.now() + 1}`,
        kind: 'tool_call',
        title: 'document_read',
        tool: 'document_read',
        scope: 'main',
        status: 'done',
        summary: '读取 technical_solution',
        detail: 'action=get_section, include_children=true',
      },
    },
    {
      delayMs: 650,
      event: {
        id: `evt_tool_subagent_${Date.now() + 2}`,
        kind: 'tool_call',
        title: 'execute_subagent',
        tool: 'execute_subagent',
        scope: 'main',
        status: 'done',
        summary: 'section_writer 已完成',
        detail:
          'result.status=success, proposal.type=document_edit_proposal, intent=replace_section_blocks',
      },
    },
    {
      delayMs: 700,
      event: {
        id: `evt_tool_edit_${Date.now() + 3}`,
        kind: 'tool_call',
        title: 'document_edit',
        tool: 'document_edit',
        scope: 'main',
        status: 'done',
        summary: '完成 technical_solution 整节重写',
        detail:
          'change_scope=section_blocks_replaced, changed_section_ids=["technical_solution"], changed_block_ids=["blk_000014","blk_000015","blk_000016","blk_000017"]',
      },
    },
  ];
}

export function createRoundCompletionEvent(): ChatEvent {
  return {
    id: `msg_assistant_result_${Date.now()}`,
    kind: 'message',
    role: 'assistant',
    text: '我已经重写技术方案章节，补充了低算力实时性约束、整体架构和处理流程。当前展示的是 document_edit 落盘后的渲染结果。',
    timestamp: formatTimestamp(),
  };
}

export function applyTechnicalSolutionReplacement(renderAst: RenderAst): RenderAst {
  return {
    ...renderAst,
    children: renderAst.children.map((node) =>
      node.type === 'section' && node.id === 'technical_solution'
        ? { ...node, children: replacementTechnicalSolutionChildren as RenderNode[] }
        : node,
    ),
  };
}
