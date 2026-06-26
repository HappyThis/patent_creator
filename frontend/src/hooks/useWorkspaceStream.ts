import { Dispatch, SetStateAction, useCallback, useEffect, useRef } from 'react';
import {
  applyAssistantDelta,
  applyContextCompressionEvent,
  applyLLMRetryStatusEvent,
  applyQualityEnhancementStatusEvent,
  applyRoundStartedEvent,
  applyRunningToolEvent,
  applyWebSearchProgressEvent,
  completeStreamingRoundEvents,
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function optionalString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function stringArray(value: unknown): string[] | undefined {
  return Array.isArray(value) && value.every((item) => typeof item === 'string') ? value : undefined;
}

function nullableString(value: unknown): string | null | undefined {
  return typeof value === 'string' || value === null ? value : undefined;
}

function documentChangePayload(payload: Record<string, unknown>): DocumentChangePayload {
  return {
    changed_section_ids: stringArray(payload.changed_section_ids),
    changed_block_ids: stringArray(payload.changed_block_ids),
    active_section_id: nullableString(payload.active_section_id),
    active_block_id: nullableString(payload.active_block_id),
  };
}

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
    optimisticUserMessageIdRef.current = null;
  }, []);

  const trackOptimisticMessage = useCallback((messageId: string | null) => {
    optimisticUserMessageIdRef.current = messageId;
  }, []);

  const consumeStreamEvent = useCallback(
    async (project_id: string, eventName: string, payload: Record<string, unknown>) => {
      if (eventName === 'stream_attached') {
        const roundId = optionalString(payload.round_id);
        if (roundId && !streamingAssistantIdRef.current) {
          streamingAssistantIdRef.current = `assistant_stream_${roundId}`;
          setEvents((current) => applyRoundStartedEvent(current, roundId));
        }
        return;
      }

      if (eventName === 'stream_closed') {
        closeStream();
        await refreshProject(project_id);
        return;
      }

      if (eventName === 'round_started') {
        const roundId = optionalString(payload.round_id);
        const messageId = optionalString(payload.message_id);
        const sessionId = optionalString(payload.session_id);
        const firstUserText = typeof payload.first_user_text === 'string' ? payload.first_user_text : null;
        setProject((current) =>
          current
            ? {
                ...current,
                active_session_id: sessionId ?? current.active_session_id,
                running_session_id: sessionId ?? current.running_session_id,
                running_round_id: roundId ?? current.running_round_id,
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
          setEvents((current) => applyRoundStartedEvent(current, roundId, messageId));
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

      if (eventName === 'web_search_progress') {
        setEvents((current) => applyWebSearchProgressEvent(current, payload));
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
            optionalString(payload.round_id),
            optionalString(payload.message_id),
          ),
        );
        return;
      }

      if (eventName === 'session_title_updated') {
        const sessionId = optionalString(payload.session_id);
        const title = optionalString(payload.title);
        if (sessionId && title) {
          setSessions((current) =>
            current.map((session) =>
              session.session_id === sessionId
                ? {
                    ...session,
                    title,
                  }
                : session,
            ),
          );
        }
        return;
      }

      if (
        eventName === 'context_compression_started' ||
        eventName === 'context_compression_completed' ||
        eventName === 'context_compression_failed' ||
        eventName === 'context_emergency_trim_applied'
      ) {
        const status =
          eventName === 'context_compression_started'
            ? 'running'
            : eventName === 'context_compression_completed' || eventName === 'context_emergency_trim_applied'
              ? 'done'
              : 'failed';
        setEvents((current) => applyContextCompressionEvent(current, payload, status));
        return;
      }

      if (eventName === 'quality_enhancement_status') {
        setEvents((current) => applyQualityEnhancementStatusEvent(current, payload));
        return;
      }

      if (eventName === 'llm_retry_status') {
        setEvents((current) => applyLLMRetryStatusEvent(current, payload));
        return;
      }

      if (eventName === 'tool_call_started') {
        streamingAssistantIdRef.current = null;
        setEvents((current) => applyRunningToolEvent(current, payload, 'running'));
        return;
      }

      if (eventName === 'tool_call_finished') {
        const status = isRecord(payload.result) && payload.result.status === 'failed' ? 'failed' : 'done';
        setEvents((current) => applyRunningToolEvent(current, payload, status));
        return;
      }

      if (eventName === 'document_changed') {
        const documentPayload = documentChangePayload(payload);
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
              optionalString(payload.round_id),
              optionalString(payload.message_id),
              payload.reply_status === 'interrupted' ? 'interrupted' : undefined,
              typeof payload.reply_detail === 'string' ? payload.reply_detail : undefined,
            ),
          );
        }
        const changed = payload.changed === true;
        const committed = payload.committed === true;
        const commitError = isRecord(payload.commit_error) ? payload.commit_error : null;
        if (commitError || (changed && !committed)) {
          setEvents((current) => [
            ...current,
            {
              id: `round_status_${Date.now()}_${current.length}`,
              kind: 'round_status',
              round_id: optionalString(payload.round_id),
              message_id: optionalString(payload.message_id),
              status: 'failed',
              summary: '本轮修改已完成，但版本提交失败。',
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
        setEvents((current) => {
          const roundId = optionalString(payload.round_id);
          const messageId = optionalString(payload.message_id);
          return [
            ...completeStreamingRoundEvents(current, roundId, messageId),
            {
              id: `assistant_error_${Date.now()}`,
              kind: 'message',
              role: 'assistant',
              text: String(payload.reply ?? '本轮处理失败。'),
              timestamp: formatTimestamp(),
              timestamp_ms: Date.now(),
              round_id: roundId,
              message_id: messageId,
              is_streaming: false,
              status: 'failed',
              detail: typeof payload.message === 'string' ? payload.message : undefined,
            },
          ];
        });
        const changed = payload.changed === true;
        if (changed) {
          const documentPayload = documentChangePayload(payload);
          focusDocumentChange(documentPayload);
          await refreshRenderAst(project_id, {
            focus_section_id: documentPayload.active_section_id,
            focus_block_id: documentPayload.active_block_id,
          });
        }
        const commitError = isRecord(payload.commit_error) ? payload.commit_error : null;
        if (commitError) {
          setEvents((current) => [
            ...current,
            {
              id: `round_status_${Date.now()}_${current.length}`,
              kind: 'round_status',
              round_id: optionalString(payload.round_id),
              message_id: optionalString(payload.message_id),
              status: 'failed',
              summary: '已完成部分修改，但版本提交失败。',
              detail: String(commitError.message ?? ''),
              timestamp: formatTimestamp(),
            },
          ]);
        }
        const nextProject = await refreshProject(project_id);
        await refreshSessions(project_id, nextProject.active_session_id);
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
            optionalString(payload.round_id),
            optionalString(payload.message_id),
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
    let attachedHandle: ChatStreamHandle | null = null;
    void (async () => {
      try {
        setSelectedSessionId(sessionId);
        await loadSessionEvents(projectId, sessionId);
        if (cancelled || streamRef.current) {
          if (!streamRef.current) {
            runningStreamKeyRef.current = null;
          }
          return;
        }
        const handle = await sseClient.streamSession(projectId, sessionId, (eventName, eventPayload) => {
          void consumeStreamEvent(projectId, eventName, eventPayload);
        });
        if (cancelled) {
          handle.close();
          runningStreamKeyRef.current = null;
          return;
        }
        attachedHandle = handle;
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
              runningStreamKeyRef.current = null;
              streamingAssistantIdRef.current = null;
            }
          });
      } catch (error: unknown) {
        if (cancelled) {
          runningStreamKeyRef.current = null;
          return;
        }
        runningStreamKeyRef.current = null;
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
      if (attachedHandle && streamRef.current === attachedHandle) {
        attachedHandle.close();
        streamRef.current = null;
        runningStreamKeyRef.current = null;
        streamingAssistantIdRef.current = null;
      }
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
