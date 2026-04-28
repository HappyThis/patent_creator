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
  timestamp?: string;
  round_id?: string;
  message_id?: string;
  seq?: number;
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
  const lastAgentOutputByRound = new Map<string, { index: number; text: string }>();

  rawEvents.forEach((event, index) => {
    if (event.type === 'agent_output') {
      lastAgentOutputByRound.set(event.round_id, {
        index,
        text: normalizeMessageText(String(event.payload.text ?? '')),
      });
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
        round_id: event.round_id,
        message_id: event.message_id,
        seq: event.seq,
      });
      continue;
    }

    if (event.type === 'agent_output') {
      const finalReply = lastAgentOutputByRound.get(event.round_id);
      const currentText = String(event.payload.text ?? '');
      const isFinalReply = finalReply?.index === index;
      if (!isFinalReply && finalReply?.text === normalizeMessageText(currentText)) {
        continue;
      }
      if (!isFinalReply) {
        events.push({
          id: event.id,
          kind: 'agent_output',
          timestamp: formatTimestamp(event.ts),
          round_id: event.round_id,
          message_id: event.message_id,
          seq: event.seq,
          title: '主 agent',
          summary: currentText,
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
        text: currentText,
        timestamp: formatTimestamp(event.ts),
        round_id: event.round_id,
        message_id: event.message_id,
        seq: event.seq,
      });
      continue;
    }

    if (event.type === 'tool_call') {
      const item: ToolState = {
        id: event.call_id || event.id,
        kind: 'tool_call',
        timestamp: formatTimestamp(event.ts),
        round_id: event.round_id,
        message_id: event.message_id,
        seq: event.seq,
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
      timestamp: formatTimestamp(event.ts),
      round_id: event.round_id,
      message_id: event.message_id,
      seq: event.seq,
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
    timestamp: formatTimestamp(),
    round_id: typeof payload.round_id === 'string' ? payload.round_id : undefined,
    message_id: typeof payload.message_id === 'string' ? payload.message_id : undefined,
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

function applyAssistantDelta(
  current: ChatEvent[],
  messageId: string,
  delta: string,
  timestamp: string,
  roundId?: string,
  sourceMessageId?: string,
): ChatEvent[] {
  if (!delta) {
    return current;
  }
  const existingIndex = current.findIndex((item) => item.kind === 'message' && item.id === messageId);
  if (existingIndex === -1) {
    return [
      ...current,
      {
        id: messageId,
        kind: 'message',
        role: 'assistant',
        text: delta,
        timestamp,
        round_id: roundId,
        message_id: sourceMessageId,
      },
    ];
  }

  const next = [...current];
  const existing = next[existingIndex];
  if (existing.kind !== 'message') {
    return current;
  }
  next[existingIndex] = {
    ...existing,
    text: `${existing.text}${delta}`,
    round_id: existing.round_id ?? roundId,
    message_id: existing.message_id ?? sourceMessageId,
  };
  return next;
}

function normalizeMessageText(value: string): string {
  return value.trim().replace(/\s+/g, ' ');
}

function finalizeRoundEvents(
  current: ChatEvent[],
  reply: string,
  timestamp: string,
  roundId?: string,
  sourceMessageId?: string,
): ChatEvent[] {
  if (!reply) {
    return current;
  }

  const normalizedReply = normalizeMessageText(reply);
  let lastUserMessageIndex = -1;
  for (let index = current.length - 1; index >= 0; index -= 1) {
    const item = current[index];
    if (item.kind === 'message' && item.role === 'user') {
      lastUserMessageIndex = index;
      break;
    }
  }

  const roundMatches = (item: ChatEvent) => (roundId ? item.round_id === roundId : true);
  const boundary = roundId ? 0 : lastUserMessageIndex + 1;
  const tail = current.slice(boundary).filter(roundMatches);
  const duplicateAssistantIndex = tail.findIndex(
    (item) =>
      item.kind === 'message' &&
      item.role === 'assistant' &&
      normalizeMessageText(item.text) === normalizedReply,
  );

  if (duplicateAssistantIndex !== -1) {
    return current;
  }

  const streamMessageIndex = tail.findIndex(
    (item) =>
      item.kind === 'message' &&
      item.role === 'assistant' &&
      item.id.startsWith('assistant_stream_'),
  );

  if (streamMessageIndex !== -1) {
    const streamMessageId = tail[streamMessageIndex].id;
    const actualIndex = current.findIndex((item) => item.id === streamMessageId);
    if (actualIndex === -1) {
      return current;
    }
    const next = [...current];
    const streamMessage = next[actualIndex];
    if (streamMessage.kind === 'message') {
      next[actualIndex] = {
        ...streamMessage,
        text: reply,
        timestamp: streamMessage.timestamp || timestamp,
        round_id: streamMessage.round_id ?? roundId,
        message_id: streamMessage.message_id ?? sourceMessageId,
      };
    }
    return next;
  }

  const duplicateAgentOutputIndex = tail.findIndex(
    (item) =>
      item.kind === 'agent_output' &&
      normalizeMessageText(item.summary ?? '') === normalizedReply,
  );

  if (duplicateAgentOutputIndex !== -1) {
    const agentOutputId = tail[duplicateAgentOutputIndex].id;
    const actualIndex = current.findIndex((item) => item.id === agentOutputId);
    if (actualIndex === -1) {
      return current;
    }
    const agentOutput = current[actualIndex];
    return [
      ...current.slice(0, actualIndex),
      ...current.slice(actualIndex + 1),
      {
        id: agentOutput.id,
        kind: 'message',
        role: 'assistant',
        text: reply,
        timestamp: agentOutput.timestamp ?? timestamp,
        round_id: agentOutput.round_id ?? roundId,
        message_id: agentOutput.message_id ?? sourceMessageId,
      },
    ];
  }

  return [
    ...current,
    {
      id: `assistant_final_${Date.now()}`,
      kind: 'message',
      role: 'assistant',
      text: reply,
      timestamp,
      round_id: roundId,
      message_id: sourceMessageId,
    },
  ];
}

export function useWorkspace() {
  const [project, setProject] = useState<ProjectState | null>(null);
  const [renderAst, setRenderAst] = useState<RenderAst>(emptyRenderAst);
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const [composer, setComposer] = useState('');
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const streamRef = useRef<{ close: () => void } | null>(null);
  const streamingAssistantIdRef = useRef<string | null>(null);
  const optimisticUserMessageIdRef = useRef<string | null>(null);
  const {
    activeSectionId,
    activeBlockId,
    recentSectionIds,
    recentBlockIds,
    previewFocusTarget,
    setActiveSectionId,
    selectSection,
    syncActiveSection,
    focusDocumentChange,
    resetRecent,
  } = useWorkspaceSelection();

  const closeStream = useCallback(() => {
    streamRef.current?.close();
    streamRef.current = null;
    streamingAssistantIdRef.current = null;
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
      closeStream();
    };
  }, [closeStream, ensureProject]);

  const handleSessionSelect = useCallback(
    async (session_id: string) => {
      if (!project || session_id === selectedSessionId) {
        return;
      }
      closeStream();
      setSelectedSessionId(session_id);
      resetRecent();
      await loadSessionEvents(project.project_id, session_id);
    },
    [closeStream, loadSessionEvents, project, resetRecent, selectedSessionId],
  );

  const consumeStreamEvent = useCallback(
    async (project_id: string, eventName: string, payload: Record<string, unknown>) => {
      if (eventName === 'round_started') {
        const roundId = typeof payload.round_id === 'string' ? payload.round_id : undefined;
        const messageId = typeof payload.message_id === 'string' ? payload.message_id : undefined;
        setProject((current) =>
          current
            ? {
                ...current,
                active_session_id: String(payload.session_id ?? current.active_session_id ?? ''),
                running_session_id: String(payload.session_id ?? current.running_session_id ?? ''),
                running_round_id: String(payload.round_id ?? current.running_round_id ?? ''),
                is_busy: true,
              }
            : current,
        );
        if (payload.session_id) {
          setSelectedSessionId(String(payload.session_id));
        }
        if (roundId) {
          streamingAssistantIdRef.current = `assistant_stream_${roundId}`;
          const optimisticId = optimisticUserMessageIdRef.current;
          if (optimisticId) {
            setEvents((current) =>
              current.map((item) =>
                item.id === optimisticId && item.kind === 'message' && item.role === 'user'
                  ? {
                      ...item,
                      id: messageId ?? item.id,
                      round_id: roundId,
                      message_id: messageId,
                    }
                  : item,
              ),
            );
            optimisticUserMessageIdRef.current = null;
          }
        }
        return;
      }

      if (eventName === 'assistant_delta') {
        const delta = String(payload.text ?? '');
        const messageId = streamingAssistantIdRef.current ?? `assistant_stream_${Date.now()}`;
        streamingAssistantIdRef.current = messageId;
        setEvents((current) =>
          applyAssistantDelta(
            current,
            messageId,
            delta,
            formatTimestamp(),
            typeof payload.round_id === 'string' ? payload.round_id : undefined,
            typeof payload.message_id === 'string' ? payload.message_id : undefined,
          ),
        );
        return;
      }

      if (eventName === 'agent_output') {
        setEvents((current) => [
          ...current,
          {
            id: `agent_note_${Date.now()}_${current.length}`,
            kind: 'agent_output',
            timestamp: formatTimestamp(),
            round_id: typeof payload.round_id === 'string' ? payload.round_id : undefined,
            message_id: typeof payload.message_id === 'string' ? payload.message_id : undefined,
            title: '主 agent',
            summary: String(payload.text ?? ''),
            status: 'done',
            scope: 'main',
          },
        ]);
        return;
      }

      if (eventName === 'tool_call_started') {
        setEvents((current) => applyRunningToolEvent(current, payload, 'running'));
        return;
      }

      if (eventName === 'tool_call_finished') {
        const result = payload.result as Record<string, unknown> | undefined;
        const status = result?.status === 'failed' ? 'failed' : 'done';
        setEvents((current) => applyRunningToolEvent(current, payload, status));
        return;
      }

      if (eventName === 'document_changed') {
        const documentPayload = payload as {
          changed_section_ids?: string[];
          changed_block_ids?: string[];
          active_section_id?: string | null;
          active_block_id?: string | null;
        };
        focusDocumentChange(documentPayload);
        await refreshRenderAst(project_id, {
          focus_section_id: documentPayload.active_section_id,
          focus_block_id: documentPayload.active_block_id,
        });
        return;
      }

      if (eventName === 'round_finished') {
        closeStream();
        if (payload.reply) {
          const reply = String(payload.reply ?? '本轮已完成。');
          const timestamp = formatTimestamp();
          setEvents((current) =>
            finalizeRoundEvents(
              current,
              reply,
              timestamp,
              typeof payload.round_id === 'string' ? payload.round_id : undefined,
              typeof payload.message_id === 'string' ? payload.message_id : undefined,
            ),
          );
        }
        const changed = payload.changed === true;
        const committed = payload.committed === true;
        const commitError = payload.commit_error as Record<string, unknown> | null | undefined;
        if (changed || commitError) {
          setEvents((current) => [
            ...current,
            {
              id: `round_status_${Date.now()}_${current.length}`,
              kind: 'round_status',
              round_id: typeof payload.round_id === 'string' ? payload.round_id : undefined,
              message_id: typeof payload.message_id === 'string' ? payload.message_id : undefined,
              status: committed ? 'done' : 'failed',
              summary: committed ? '本轮修改已保存到版本历史。' : '本轮修改已完成，但版本提交失败。',
              detail: commitError ? String(commitError.message ?? '') : undefined,
              timestamp: formatTimestamp(),
            },
          ]);
        }
        const nextProject = await refreshProject(project_id);
        await refreshSessions(project_id, nextProject.active_session_id);
        if (nextProject.active_session_id) {
          setSelectedSessionId(nextProject.active_session_id);
        }
        return;
      }

      if (eventName === 'round_failed') {
        closeStream();
        setEvents((current) => [
          ...current,
          {
            id: `assistant_error_${Date.now()}`,
            kind: 'message',
            role: 'assistant',
            text: String(payload.reply ?? '本轮处理失败。'),
            timestamp: formatTimestamp(),
            round_id: typeof payload.round_id === 'string' ? payload.round_id : undefined,
            message_id: typeof payload.message_id === 'string' ? payload.message_id : undefined,
          },
        ]);
        await refreshProject(project_id);
      }
    },
    [closeStream, focusDocumentChange, refreshProject, refreshRenderAst, refreshSessions],
  );

  const submitMessage = useCallback(async () => {
    const message = composer.trim();
    if (!project || !message || project.is_busy) {
      return;
    }

    const optimisticId = `msg_local_${Date.now()}`;
    const optimisticMessage: ChatEvent = {
      id: optimisticId,
      kind: 'message',
      role: 'user',
      text: message,
      timestamp: formatTimestamp(),
    };

    optimisticUserMessageIdRef.current = optimisticId;
    setEvents((current) => [...current, optimisticMessage]);
    setComposer('');

    try {
      setProject((current) =>
        current
          ? {
              ...current,
              is_busy: true,
            }
          : current,
      );
      closeStream();
      const handle = await sseClient.streamChatMessage(
        project.project_id,
        {
          session_id: selectedSessionId,
          message,
          active_section_id: activeSectionId || null,
          active_block_id: activeBlockId,
        },
        (eventName, eventPayload) => {
          void consumeStreamEvent(project.project_id, eventName, eventPayload);
        },
      );
      streamRef.current = handle;
      void handle.done.catch(async (error: unknown) => {
        closeStream();
        const messageText = error instanceof Error ? error.message : '流式消息处理失败。';
        setEvents((current) => [
          ...current,
          {
            id: `msg_stream_error_${Date.now()}`,
            kind: 'message',
            role: 'assistant',
            text: messageText,
            timestamp: formatTimestamp(),
          },
        ]);
        await refreshProject(project.project_id);
      });
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
    closeStream,
    consumeStreamEvent,
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
    previewFocusTarget,
    recentSectionIds,
    recentBlockIds,
    setComposer,
    setActiveSectionId,
    selectSection,
    submitMessage,
    handleSessionSelect,
  };
}
