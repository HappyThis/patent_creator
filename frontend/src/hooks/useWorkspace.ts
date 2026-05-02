import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiClient } from '../services/api/client';
import { sseClient } from '../services/sse/client';
import {
  ChatEvent,
  OutlineItem,
  ProcessEvent,
  ProjectState,
  RenderAst,
  SessionEventRecord,
  SessionSummary,
  SessionTab,
} from '../types';
import { useWorkspaceSelection } from './useWorkspaceSelection';

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

type ToolState = {
  id: string;
  kind: 'tool_call';
  timestamp?: string;
  round_id?: string;
  message_id?: string;
  seq?: number;
  parent_call_id?: string | null;
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

function buildSessionTitle(firstUserText?: string | null): string {
  const normalized = (firstUserText ?? '').replace(/\s+/g, ' ').trim();
  if (!normalized) {
    return '未命名会话';
  }
  const firstSentence = normalized.split(/[。！？.!?\n]/)[0]?.trim() || normalized;
  return firstSentence.slice(0, 18);
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
    title: buildSessionTitle(session.first_user_text),
    subtitle: `更新于 ${formatTimestamp(session.updated_at)}`,
    active: session.session_id === selected_session_id,
  }));
}

function upsertActiveSessionSummary(
  sessions: SessionSummary[],
  sessionId: string,
  firstUserText: string | null,
  roundId?: string,
): SessionSummary[] {
  const updatedAt = new Date().toISOString();
  const existing = sessions.find((session) => session.session_id === sessionId);
  const activeSession: SessionSummary = existing
    ? {
        ...existing,
        updated_at: updatedAt,
        event_count: Math.max(existing.event_count, 1),
        last_round_id: roundId ?? existing.last_round_id,
        first_user_text: existing.first_user_text || firstUserText,
        is_active: true,
      }
    : {
        session_id: sessionId,
        updated_at: updatedAt,
        event_count: 1,
        last_round_id: roundId ?? null,
        first_user_text: firstUserText,
        is_active: true,
        context_usage: null,
      };

  const inactiveSessions = sessions
    .filter((session) => session.session_id !== sessionId)
    .map((session) => ({
      ...session,
      is_active: false,
    }));

  return [activeSession, ...inactiveSessions];
}

function hydrateEvents(rawEvents: SessionEventRecord[]): ChatEvent[] {
  const events: ChatEvent[] = [];
  const toolIndex = new Map<string, number>();

  for (const event of rawEvents) {
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
      const currentText = String(event.payload.text ?? '');
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

    if (event.type === 'context_summary' || event.type === 'context_pruned') {
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
        parent_call_id: event.parent_call_id,
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
      parent_call_id: event.parent_call_id,
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
    parent_call_id: typeof payload.parent_call_id === 'string' ? payload.parent_call_id : null,
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

  const roundMatches = (item: ChatEvent) => (roundId ? item.round_id === roundId : true);

  let lastToolIndex = -1;
  let latestStreamIndexAfterTools = -1;
  for (let index = 0; index < current.length; index += 1) {
    const item = current[index];
    if (!roundMatches(item)) {
      continue;
    }
    if (item.kind === 'tool_call') {
      lastToolIndex = index;
      latestStreamIndexAfterTools = -1;
      continue;
    }
    if (
      item.kind === 'message' &&
      item.role === 'assistant' &&
      item.id.startsWith('assistant_stream_') &&
      index > lastToolIndex
    ) {
      latestStreamIndexAfterTools = index;
    }
  }

  if (latestStreamIndexAfterTools !== -1) {
    const next = [...current];
    const streamMessage = next[latestStreamIndexAfterTools];
    if (streamMessage.kind === 'message') {
      next[latestStreamIndexAfterTools] = {
        ...streamMessage,
        text: reply,
        timestamp: streamMessage.timestamp || timestamp,
        round_id: streamMessage.round_id ?? roundId,
        message_id: streamMessage.message_id ?? sourceMessageId,
      };
    }
    return next;
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
    setEvents(hydrateEvents(response.events));
  }, []);

  const ensureProject = useCallback(async () => {
    const response = await apiClient.listProjects();
    const currentProject = response.projects[0];
    if (!currentProject) {
      throw new Error('后端未返回可用 project。');
    }
    setProject(currentProject);
    await refreshRenderAst(currentProject.project_id);
    const knownSessions = await refreshSessions(currentProject.project_id, currentProject.active_session_id);
    const targetSessionId = currentProject.active_session_id || knownSessions[0]?.session_id;
    if (targetSessionId) {
      await loadSessionEvents(currentProject.project_id, targetSessionId);
      return;
    }
    setEvents([]);
  }, [loadSessionEvents, refreshRenderAst, refreshSessions]);

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

  const handleNewSession = useCallback(() => {
    if (!project || project.is_busy) {
      return;
    }
    closeStream();
    setSelectedSessionId(null);
    resetRecent();
    setEvents([]);
    optimisticUserMessageIdRef.current = null;
    setProject((current) =>
      current
        ? {
            ...current,
            active_session_id: null,
          }
        : current,
    );
  }, [closeStream, project, resetRecent]);

  const consumeStreamEvent = useCallback(
    async (project_id: string, eventName: string, payload: Record<string, unknown>) => {
      if (eventName === 'round_started') {
        const roundId = typeof payload.round_id === 'string' ? payload.round_id : undefined;
        const messageId = typeof payload.message_id === 'string' ? payload.message_id : undefined;
        const sessionId = typeof payload.session_id === 'string' ? payload.session_id : undefined;
        const firstUserText = typeof payload.first_user_text === 'string' ? payload.first_user_text : null;
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
        if (sessionId) {
          setSelectedSessionId(sessionId);
          setSessions((current) => upsertActiveSessionSummary(current, sessionId, firstUserText, roundId));
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

      if (eventName === 'tool_call_started') {
        streamingAssistantIdRef.current = null;
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
  const contextUsage = useMemo(
    () =>
      selectedSessionId
        ? sessions.find((session) => session.session_id === selectedSessionId)?.context_usage ??
          project?.active_session_context ??
          null
        : null,
    [project?.active_session_context, selectedSessionId, sessions],
  );

  return {
    renderAst,
    events,
    composer,
    isBusy: project?.is_busy ?? isLoading,
    contextUsage,
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
    handleNewSession,
  };
}
