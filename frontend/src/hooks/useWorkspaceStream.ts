import { Dispatch, SetStateAction, useCallback, useEffect, useRef } from 'react';
import {
  applyAssistantDelta,
  applyContextCompressionEvent,
  applyRunningToolEvent,
  finalizeRoundEvents,
  formatTimestamp,
} from '../features/chat/chatEventTransforms';
import { upsertActiveSessionSummary } from '../features/chat/sessionTabs';
import { sseClient } from '../services/sse/client';
import type { ChatStreamHandle, ChatStreamPayload } from '../services/sse/client';
import type { ChatEvent, ProjectState, SessionSummary } from '../types';

type DocumentChangePayload = {
  changed_section_ids?: string[];
  changed_block_ids?: string[];
  active_section_id?: string | null;
  active_block_id?: string | null;
};

type WorkspaceStreamArgs = {
  project: ProjectState | null;
  setProject: Dispatch<SetStateAction<ProjectState | null>>;
  setEvents: Dispatch<SetStateAction<ChatEvent[]>>;
  setSessions: Dispatch<SetStateAction<SessionSummary[]>>;
  setSelectedSessionId: Dispatch<SetStateAction<string | null>>;
  setIsCancelling: Dispatch<SetStateAction<boolean>>;
  refreshProject: (project_id: string) => Promise<ProjectState>;
  refreshSessions: (project_id: string, preferred_session_id?: string | null) => Promise<SessionSummary[]>;
  refreshRenderAst: (
    project_id: string,
    focus?: { focus_section_id?: string | null; focus_block_id?: string | null },
  ) => Promise<void>;
  loadSessionEvents: (project_id: string, session_id: string) => Promise<void>;
  focusDocumentChange: (payload: DocumentChangePayload) => void;
};

export function useWorkspaceStream({
  project,
  setProject,
  setEvents,
  setSessions,
  setSelectedSessionId,
  setIsCancelling,
  refreshProject,
  refreshSessions,
  refreshRenderAst,
  loadSessionEvents,
  focusDocumentChange,
}: WorkspaceStreamArgs) {
  const streamRef = useRef<ChatStreamHandle | null>(null);
  const runningStreamKeyRef = useRef<string | null>(null);
  const streamingAssistantIdRef = useRef<string | null>(null);
  const optimisticUserMessageIdRef = useRef<string | null>(null);

  const closeStream = useCallback(() => {
    streamRef.current?.close();
    streamRef.current = null;
    runningStreamKeyRef.current = null;
    streamingAssistantIdRef.current = null;
  }, []);

  const trackOptimisticMessage = useCallback((messageId: string | null) => {
    optimisticUserMessageIdRef.current = messageId;
  }, []);

  const consumeStreamEvent = useCallback(
    async (project_id: string, eventName: string, payload: Record<string, unknown>) => {
      if (eventName === 'stream_attached') {
        const roundId = typeof payload.round_id === 'string' ? payload.round_id : undefined;
        if (roundId && !streamingAssistantIdRef.current) {
          streamingAssistantIdRef.current = `assistant_stream_${roundId}`;
        }
        return;
      }

      if (eventName === 'stream_closed') {
        closeStream();
        await refreshProject(project_id);
        return;
      }

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

      if (
        eventName === 'context_compression_started' ||
        eventName === 'context_compression_completed' ||
        eventName === 'context_compression_failed'
      ) {
        const status =
          eventName === 'context_compression_started'
            ? 'running'
            : eventName === 'context_compression_completed'
              ? 'done'
              : 'failed';
        setEvents((current) => applyContextCompressionEvent(current, payload, status));
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
        const documentPayload = payload as DocumentChangePayload;
        focusDocumentChange(documentPayload);
        await refreshRenderAst(project_id, {
          focus_section_id: documentPayload.active_section_id,
          focus_block_id: documentPayload.active_block_id,
        });
        return;
      }

      if (eventName === 'round_finished') {
        setIsCancelling(false);
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
        setIsCancelling(false);
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
        return;
      }

      if (eventName === 'round_cancelled') {
        setIsCancelling(false);
        closeStream();
        const reply = String(payload.reply ?? '本轮任务已取消。');
        setEvents((current) =>
          finalizeRoundEvents(
            current,
            reply,
            formatTimestamp(),
            typeof payload.round_id === 'string' ? payload.round_id : undefined,
            typeof payload.message_id === 'string' ? payload.message_id : undefined,
          ),
        );
        const nextProject = await refreshProject(project_id);
        await refreshSessions(project_id, nextProject.active_session_id);
      }
    },
    [
      closeStream,
      focusDocumentChange,
      refreshProject,
      refreshRenderAst,
      refreshSessions,
      setEvents,
      setIsCancelling,
      setProject,
      setSelectedSessionId,
      setSessions,
    ],
  );

  useEffect(() => {
    if (!project?.is_busy || !project.running_session_id || streamRef.current) {
      return;
    }

    const projectId = project.project_id;
    const sessionId = project.running_session_id;
    const roundId = project.running_round_id ?? undefined;
    const streamKey = `${projectId}:${sessionId}:${roundId ?? ''}`;
    if (runningStreamKeyRef.current === streamKey) {
      return;
    }
    runningStreamKeyRef.current = streamKey;
    streamingAssistantIdRef.current = roundId ? `assistant_stream_${roundId}` : null;

    let cancelled = false;
    void (async () => {
      try {
        setSelectedSessionId(sessionId);
        await loadSessionEvents(projectId, sessionId);
        if (cancelled || streamRef.current) {
          return;
        }
        const handle = await sseClient.streamSession(projectId, sessionId, (eventName, eventPayload) => {
          void consumeStreamEvent(projectId, eventName, eventPayload);
        });
        if (cancelled) {
          handle.close();
          return;
        }
        streamRef.current = handle;
        void handle.done
          .catch(async (error: unknown) => {
            if (cancelled) {
              return;
            }
            const messageText = error instanceof Error ? error.message : '恢复流式连接失败。';
            setEvents((current) => [
              ...current,
              {
                id: `stream_resume_error_${Date.now()}`,
                kind: 'message',
                role: 'assistant',
                text: messageText,
                timestamp: formatTimestamp(),
              },
            ]);
            await refreshProject(projectId);
          })
          .finally(() => {
            if (streamRef.current === handle) {
              streamRef.current = null;
              streamingAssistantIdRef.current = null;
            }
          });
      } catch (error: unknown) {
        if (cancelled) {
          return;
        }
        const messageText = error instanceof Error ? error.message : '恢复流式连接失败。';
        setEvents((current) => [
          ...current,
          {
            id: `stream_resume_error_${Date.now()}`,
            kind: 'message',
            role: 'assistant',
            text: messageText,
            timestamp: formatTimestamp(),
          },
        ]);
        await refreshProject(projectId);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [
    consumeStreamEvent,
    loadSessionEvents,
    project?.is_busy,
    project?.project_id,
    project?.running_round_id,
    project?.running_session_id,
    refreshProject,
    setEvents,
    setSelectedSessionId,
  ]);

  const startChatMessageStream = useCallback(
    async (projectId: string, payload: ChatStreamPayload) => {
      closeStream();
      const handle = await sseClient.streamChatMessage(projectId, payload, (eventName, eventPayload) => {
        void consumeStreamEvent(projectId, eventName, eventPayload);
      });
      streamRef.current = handle;
      void handle.done
        .catch(async (error: unknown) => {
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
          await refreshProject(projectId);
        })
        .finally(() => {
          if (streamRef.current === handle) {
            streamRef.current = null;
            streamingAssistantIdRef.current = null;
          }
        });
    },
    [closeStream, consumeStreamEvent, refreshProject, setEvents],
  );

  return {
    closeStream,
    trackOptimisticMessage,
    startChatMessageStream,
  };
}
