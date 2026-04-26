import { useCallback, useMemo, useState } from 'react';
import {
  initialEvents,
  initialProject,
  initialRenderAst,
  replacementTechnicalSolutionChildren,
} from '../mocks/demoData';
import { ChatEvent, RenderNode, SessionTab } from '../types';

const delay = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

export function useDemoWorkspace() {
  const [project, setProject] = useState(initialProject);
  const [renderAst, setRenderAst] = useState(initialRenderAst);
  const [events, setEvents] = useState(initialEvents);
  const [composer, setComposer] = useState('请补充技术方案中的实时性约束与处理流程。');
  const [activeSectionId, setActiveSectionId] = useState('technical_solution');
  const [activeBlockId, setActiveBlockId] = useState<string | null>('blk_000006');
  const [recentSectionIds, setRecentSectionIds] = useState<string[]>(['technical_solution']);
  const [recentBlockIds, setRecentBlockIds] = useState<string[]>(['blk_000006']);

  const isBusy = project.isBusy;

  const sessionTabs = useMemo<SessionTab[]>(
    () => [
      {
        id: project.activeSessionId,
        title: '当前会话',
        subtitle: project.projectId,
        active: true,
      },
      { id: 'sess_003', title: 'sess_003', active: false },
      { id: 'sess_002', title: 'sess_002', active: false },
    ],
    [project.activeSessionId, project.projectId],
  );

  const simulateRound = useCallback(async () => {
    const trimmed = composer.trim();
    if (!trimmed || isBusy) {
      return;
    }

    const timestamp = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    const userMessage: ChatEvent = {
      id: `msg_user_${Date.now()}`,
      kind: 'message',
      role: 'user',
      text: trimmed,
      timestamp,
    };

    const assistantPlan: ChatEvent = {
      id: `msg_assistant_plan_${Date.now()}`,
      kind: 'message',
      role: 'assistant',
      text: '我会补充整体架构和处理流程，并把实时性目标写进技术效果。',
      timestamp,
    };

    setProject((current) => ({
      ...current,
      isBusy: true,
      runningRoundId: 'round_demo_001',
      activeSessionId: 'sess_004',
    }));
    setEvents((current) => [...current, userMessage, assistantPlan]);

    await delay(500);

    setEvents((current) => [
      ...current,
      {
        id: `evt_agent_${Date.now()}`,
        kind: 'agent_output',
        title: '主 agent',
        summary: '开始处理本轮需求。',
        detail: '本轮将默认采纳 section_writer proposal，并更新 technical_solution。',
      },
    ]);

    await delay(500);

    setEvents((current) => [
      ...current,
      {
        id: `evt_tool_read_${Date.now()}`,
        kind: 'tool_call',
        title: 'document_read',
        tool: 'document_read',
        scope: 'main',
        status: 'done',
        summary: '读取 technical_solution',
        detail: 'action=get_section, include_children=true',
      },
    ]);

    await delay(650);

    setEvents((current) => [
      ...current,
      {
        id: `evt_tool_subagent_${Date.now()}`,
        kind: 'tool_call',
        title: 'execute_subagent',
        tool: 'execute_subagent',
        scope: 'main',
        status: 'done',
        summary: 'section_writer 已完成',
        detail:
          'result.status=success, proposal.type=document_edit_proposal, intent=replace_section_blocks',
      },
    ]);

    await delay(700);

    setRenderAst((current) => ({
      ...current,
      children: current.children.map((node) =>
        node.type === 'section' && node.id === 'technical_solution'
          ? { ...node, children: replacementTechnicalSolutionChildren as RenderNode[] }
          : node,
      ),
    }));
    setRecentSectionIds(['technical_solution']);
    setRecentBlockIds(['blk_000014', 'blk_000015', 'blk_000016', 'blk_000017']);
    setActiveSectionId('technical_solution');
    setActiveBlockId('blk_000014');

    setEvents((current) => [
      ...current,
      {
        id: `evt_tool_edit_${Date.now()}`,
        kind: 'tool_call',
        title: 'document_edit',
        tool: 'document_edit',
        scope: 'main',
        status: 'done',
        summary: '完成 technical_solution 整节重写',
        detail:
          'change_scope=section_blocks_replaced, changed_section_ids=["technical_solution"], changed_block_ids=["blk_000014","blk_000015","blk_000016","blk_000017"]',
      },
    ]);

    await delay(600);

    setEvents((current) => [
      ...current,
      {
        id: `msg_assistant_result_${Date.now()}`,
        kind: 'message',
        role: 'assistant',
        text: '我已经重写技术方案章节，补充了低算力实时性约束、整体架构和处理流程。当前展示的是 document_edit 落盘后的渲染结果。',
        timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
      },
    ]);

    setProject((current) => ({
      ...current,
      isBusy: false,
      runningRoundId: null,
    }));
  }, [composer, isBusy]);

  return {
    renderAst,
    events,
    composer,
    isBusy,
    sessionTabs,
    activeSectionId,
    activeBlockId,
    recentSectionIds,
    recentBlockIds,
    setComposer,
    setActiveSectionId,
    setActiveBlockId,
    simulateRound,
  };
}
