import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiClient } from '../services/api/client';
import { sseClient } from '../services/sse/client';
import {
  ChatEvent,
  OutlineItem,
  ProcessEvent,
  ProjectState,
  RenderAst,
  SessionSummary,
  SessionTab,
} from '../types';
import { useWorkspaceSelection } from './useWorkspaceSelection';

const PROJECT_STORAGE_KEY = 'patent_creator_project_id';
const DEFAULT_PROJECT_TITLE = '一种图像检测方法';

const emptyRenderAst: RenderAst = {
  type: 'document',
  title: DEFAULT_PROJECT_TITLE,
  meta: {
    document_type: 'patent_disclosure',
    schema_version: 'v1',
  },
  outline: [],
  children: [],
};

type SessionEventResponse = {
  id: string;
  ts: string;
  type: 'user_input' | 'agent_output' | 'tool_call' | 'tool_result';
  seq: number;
  scope: string;
  round_id: string;
  message_id: string;
  call_id?: string | null;
  parent_call_id?: string | null;
  payload: Record<string, unknown>;
};

type ToolState = {
  id: string;
  kind: 'tool_call';
  title: string;
  tool?: string;
  scope?: ProcessEvent['scope'];
  status?: ProcessEvent['status'];
  summary?: string;
  detail?: string;
};

function formatTimestamp(value?: string): string {
  if (!value) {
    return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }
  return new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

function formatToolDetail(payload: unknown): string {
  if (payload == null) {
    return '无详细结果';
  }
  return JSON.stringify(payload, null, 2);
}

function buildSessionTabs(
  sessions: SessionSummary[],
  selected_session_id: string | null,
): SessionTab[] {
  return sessions.map((session) => ({
    ...session,
    title:
      session.session_id === selected_session_id
        ? '当前会话'
        : session.latest_user_text?.slice(0, 18) || session.session_id,
    subtitle:
      session.session_id === selected_session_id
        ? session.session_id
        : `更新于 ${formatTimestamp(session.updated_at)}`,
    active: session.session_id === selected_session_id,
  }));
}

function hydrateEvents(rawEvents: SessionEventResponse[]): ChatEvent[] {
  const events: ChatEvent[] = [];
  const toolIndex = new Map<string, number>();
  const lastAgentOutputIndexByRound = new Map<string, number>();

  rawEvents.forEach((event, index) => {
    if (event.type === 'agent_output') {
      lastAgentOutputIndexByRound.set(event.round_id, index);
    }
  });

  for (const [index, event] of rawEvents.entries()) {
    if (event.type === 'user_input') {
      events.push({
        id: event.message_id,
        kind: 'message',
        role: 'user',
        text: String(event.payload.text ?? ''),
        timestamp: formatTimestamp(event.ts),
      });
      continue;
    }

    if (event.type === 'agent_output') {
      const isFinalReply = lastAgentOutputIndexByRound.get(event.round_id) === index;
      if (!isFinalReply) {
        events.push({
          id: event.id,
          kind: 'agent_output',
          title: '主 agent',
          summary: String(event.payload.text ?? ''),
          detail: event.scope !== 'main' ? String(event.scope) : undefined,
          scope: event.scope as ProcessEvent['scope'],
          status: 'done',
        });
        continue;
      }
      events.push({
        id: event.id,
        kind: 'message',
        role: 'assistant',
        text: String(event.payload.text ?? ''),
        timestamp: formatTimestamp(event.ts),
      });
      continue;
    }

    if (event.type === 'tool_call') {
      const item: ToolState = {
        id: event.call_id || event.id,
        kind: 'tool_call',
        title: String(event.payload.tool ?? 'tool_call'),
        tool: typeof event.payload.tool === 'string' ? event.payload.tool : undefined,
        scope: event.scope as ProcessEvent['scope'],
        status: 'running',
        summary: typeof event.payload.tool === 'string' ? `开始执行 ${event.payload.tool}` : '开始执行',
        detail: formatToolDetail(event.payload.arguments),
      };
      toolIndex.set(item.id, events.length);
      events.push(item);
      continue;
    }

    const toolId = event.call_id || event.id;
    const nextStatus =
      event.payload.status === 'failed'
        ? 'failed'
        : event.payload.status === 'success'
          ? 'done'
          : 'done';
    const updatedItem: ToolState = {
      id: toolId,
      kind: 'tool_call',
      title: String(event.payload.tool ?? 'tool_call'),
      tool: typeof event.payload.tool === 'string' ? event.payload.tool : undefined,
      scope: event.scope as ProcessEvent['scope'],
      status: nextStatus,
      summary: nextStatus === 'failed' ? '执行失败' : '执行完成',
      detail: formatToolDetail(event.payload.output ?? event.payload),
    };

    const existingIndex = toolIndex.get(toolId);
    if (existingIndex == null) {
      toolIndex.set(toolId, events.length);
      events.push(updatedItem);
      continue;
    }
    events[existingIndex] = {
      ...(events[existingIndex] as ToolState),
      ...updatedItem,
    };
  }

  return events;
}

function applyRunningToolEvent(
  current: ChatEvent[],
  payload: Record<string, unknown>,
  status: 'running' | 'done' | 'failed',
): ChatEvent[] {
  const toolId = String(payload.call_id ?? `call_${Date.now()}`);
  const detail = payload.result ? formatToolDetail(payload.result) : undefined;
  const nextEvent: ProcessEvent = {
    id: toolId,
    kind: 'tool_call',
    title: String(payload.tool ?? 'tool_call'),
    tool: typeof payload.tool === 'string' ? payload.tool : undefined,
    scope: payload.scope as ProcessEvent['scope'],
    status,
    summary: typeof payload.summary === 'string' ? payload.summary : undefined,
    detail,
  };

  const existingIndex = current.findIndex((item) => item.kind === 'tool_call' && item.id === toolId);
  if (existingIndex === -1) {
    return [...current, nextEvent];
  }

  const next = [...current];
  next[existingIndex] = {
    ...(next[existingIndex] as ProcessEvent),
    ...nextEvent,
  };
  return next;
}

export function useWorkspace() {
  const [project, setProject] = useState<ProjectState | null>(null);
  const [renderAst, setRenderAst] = useState<RenderAst>(emptyRenderAst);
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const [composer, setComposer] = useState('');
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const eventSourceRef = useRef<EventSource | null>(null);
  const {
    activeSectionId,
    activeBlockId,
    recentSectionIds,
    recentBlockIds,
    setActiveSectionId,
    selectSection,
    syncActiveSection,
    focusDocumentChange,
    resetRecent,
  } = useWorkspaceSelection();

  const closeEventSource = useCallback(() => {
    eventSourceRef.current?.close();
    eventSourceRef.current = null;
  }, []);

  const refreshRenderAst = useCallback(
    async (
      project_id: string,
      focus?: { focus_section_id?: string | null; focus_block_id?: string | null },
    ) => {
      const [outlineResponse, renderResponse] = await Promise.all([
        apiClient.getOutline(project_id),
        apiClient.getRenderAst(project_id, focus),
      ]);
      setRenderAst({
        ...renderResponse.render_ast,
        outline: outlineResponse.sections as OutlineItem[],
      });
      syncActiveSection(
        renderResponse.active_section_id || focus?.focus_section_id || outlineResponse.sections[0]?.id || '',
      );
    },
    [syncActiveSection],
  );

  const refreshProject = useCallback(async (project_id: string) => {
    const projectState = await apiClient.getProject(project_id);
    setProject(projectState);
    return projectState;
  }, []);

  const refreshSessions = useCallback(async (project_id: string, preferred_session_id?: string | null) => {
    const response = await apiClient.listSessions(project_id);
    setSessions(response.sessions);
    setSelectedSessionId((current) => preferred_session_id ?? current ?? response.sessions[0]?.session_id ?? null);
    return response.sessions;
  }, []);

  const loadSessionEvents = useCallback(async (project_id: string, session_id: string) => {
    const response = await apiClient.getSessionEvents(project_id, session_id);
    setEvents(hydrateEvents(response.events as SessionEventResponse[]));
  }, []);

  const ensureProject = useCallback(async () => {
    const savedProjectId = window.localStorage.getItem(PROJECT_STORAGE_KEY);
    if (savedProjectId) {
      try {
        const existingProject = await refreshProject(savedProjectId);
        await refreshRenderAst(existingProject.project_id);
        const knownSessions = await refreshSessions(existingProject.project_id, existingProject.active_session_id);
        const targetSessionId = existingProject.active_session_id || knownSessions[0]?.session_id;
        if (targetSessionId) {
          await loadSessionEvents(existingProject.project_id, targetSessionId);
        }
        return;
      } catch {
        window.localStorage.removeItem(PROJECT_STORAGE_KEY);
      }
    }

    const nextProject = await apiClient.createProject(DEFAULT_PROJECT_TITLE);
    window.localStorage.setItem(PROJECT_STORAGE_KEY, nextProject.project_id);
    setProject(nextProject);
    await refreshRenderAst(nextProject.project_id);
    await refreshSessions(nextProject.project_id, nextProject.active_session_id);
    setEvents([]);
  }, [loadSessionEvents, refreshProject, refreshRenderAst, refreshSessions]);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    ensureProject()
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }
        const message = error instanceof Error ? error.message : '初始化项目失败。';
        setEvents([
          {
            id: `init_error_${Date.now()}`,
            kind: 'message',
            role: 'assistant',
            text: message,
            timestamp: formatTimestamp(),
          },
        ]);
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoading(false);
        }
      });

    return () => {
      cancelled = true;
      closeEventSource();
    };
  }, [closeEventSource, ensureProject]);

  const handleSessionSelect = useCallback(
    async (session_id: string) => {
      if (!project || session_id === selectedSessionId) {
        return;
      }
      closeEventSource();
      setSelectedSessionId(session_id);
      resetRecent();
      await loadSessionEvents(project.project_id, session_id);
    },
    [closeEventSource, loadSessionEvents, project, resetRecent, selectedSessionId],
  );

  const connectStream = useCallback(
    (project_id: string, session_id: string) => {
      closeEventSource();
      const source = sseClient.connect(project_id, session_id);
      eventSourceRef.current = source;

      source.addEventListener('agent_output', (event) => {
        const payload = JSON.parse((event as MessageEvent<string>).data) as { text: string };
        setEvents((current) => [
          ...current,
          {
            id: `agent_note_${Date.now()}_${current.length}`,
            kind: 'agent_output',
            title: '主 agent',
            summary: payload.text,
            status: 'done',
            scope: 'main',
          },
        ]);
      });

      source.addEventListener('tool_call_started', (event) => {
        const payload = JSON.parse((event as MessageEvent<string>).data) as Record<string, unknown>;
        setEvents((current) => applyRunningToolEvent(current, payload, 'running'));
      });

      source.addEventListener('tool_call_finished', (event) => {
        const payload = JSON.parse((event as MessageEvent<string>).data) as Record<string, unknown>;
        const result = payload.result as Record<string, unknown> | undefined;
        const status = result?.status === 'failed' ? 'failed' : 'done';
        setEvents((current) => applyRunningToolEvent(current, payload, status));
      });

      source.addEventListener('document_changed', async (event) => {
        const payload = JSON.parse((event as MessageEvent<string>).data) as {
          changed_section_ids?: string[];
          changed_block_ids?: string[];
          active_section_id?: string | null;
          active_block_id?: string | null;
        };
        focusDocumentChange(payload);
        await refreshRenderAst(project_id, {
          focus_section_id: payload.active_section_id,
          focus_block_id: payload.active_block_id,
        });
      });

      source.addEventListener('round_finished', async (event) => {
        const payload = JSON.parse((event as MessageEvent<string>).data) as { reply?: string };
        closeEventSource();
        if (payload.reply) {
          setEvents((current) => [
            ...current,
            {
              id: `assistant_final_${Date.now()}`,
              kind: 'message',
              role: 'assistant',
              text: payload.reply ?? '本轮已完成。',
              timestamp: formatTimestamp(),
            },
          ]);
        }
        const nextProject = await refreshProject(project_id);
        await refreshSessions(project_id, session_id);
        if (nextProject.active_session_id) {
          setSelectedSessionId(nextProject.active_session_id);
        }
      });

      source.addEventListener('round_failed', async (event) => {
        const payload = JSON.parse((event as MessageEvent<string>).data) as { reply?: string };
        closeEventSource();
        setEvents((current) => [
          ...current,
          {
            id: `assistant_error_${Date.now()}`,
            kind: 'message',
            role: 'assistant',
            text: payload.reply ?? '本轮处理失败。',
            timestamp: formatTimestamp(),
          },
        ]);
        await refreshProject(project_id);
      });

      source.onerror = () => {
        closeEventSource();
      };
    },
    [closeEventSource, focusDocumentChange, refreshProject, refreshRenderAst, refreshSessions],
  );

  const submitMessage = useCallback(async () => {
    const message = composer.trim();
    if (!project || !message || project.is_busy) {
      return;
    }

    const optimisticMessage: ChatEvent = {
      id: `msg_local_${Date.now()}`,
      kind: 'message',
      role: 'user',
      text: message,
      timestamp: formatTimestamp(),
    };

    setEvents((current) => [...current, optimisticMessage]);
    setComposer('');

    try {
      const response = await apiClient.sendChatMessage(project.project_id, {
        session_id: selectedSessionId,
        message,
        active_section_id: activeSectionId || null,
        active_block_id: activeBlockId,
      });
      setProject((current) =>
        current
          ? {
              ...current,
              active_session_id: response.session_id,
              running_session_id: response.session_id,
              running_round_id: response.round_id,
              is_busy: true,
            }
          : current,
      );
      setSelectedSessionId(response.session_id);
      connectStream(project.project_id, response.session_id);
    } catch (error: unknown) {
      const messageText = error instanceof Error ? error.message : '发送消息失败。';
      setComposer(message);
      setEvents((current) => [
        ...current,
        {
          id: `msg_error_${Date.now()}`,
          kind: 'message',
          role: 'assistant',
          text: messageText,
          timestamp: formatTimestamp(),
        },
      ]);
      await refreshProject(project.project_id);
    }
  }, [
    activeBlockId,
    activeSectionId,
    composer,
    connectStream,
    project,
    refreshProject,
    selectedSessionId,
  ]);

  const sessionTabs = useMemo(
    () => buildSessionTabs(sessions, selectedSessionId),
    [selectedSessionId, sessions],
  );

  return {
    renderAst,
    events,
    composer,
    isBusy: project?.is_busy ?? isLoading,
    sessionTabs,
    activeSectionId,
    activeBlockId,
    recentSectionIds,
    recentBlockIds,
    setComposer,
    setActiveSectionId,
    selectSection,
    submitMessage,
    handleSessionSelect,
  };
}
