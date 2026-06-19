import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiClient } from '../services/api/client';
import type { ChatEvent } from '../types';
import { formatTimestamp } from '../features/chat/chatEventTransforms';
import { buildSessionTabs } from '../features/chat/sessionTabs';
import { useWorkspaceData } from './useWorkspaceData';
import { useWorkspaceSelection } from './useWorkspaceSelection';
import { useWorkspaceStream } from './useWorkspaceStream';

export function useWorkspace(projectId: string | null) {
  const [composer, setComposer] = useState('');
  const [isCancelling, setIsCancelling] = useState(false);
  const {
    activeSectionId,
    activeBlockId,
    recentSectionIds,
    recentBlockIds,
    previewFocusTarget,
    setActiveSectionId,
    syncActiveSection,
    focusDocumentChange,
    resetRecent,
  } = useWorkspaceSelection();
  const {
    project,
    setProject,
    renderAst,
    renderUpdatedAt,
    events,
    setEvents,
    clearSessionEvents,
    invalidateSessionEventLoads,
    sessions,
    setSessions,
    selectedSessionId,
    setSelectedSessionId,
    isLoading,
    setIsLoading,
    refreshRenderAst,
    refreshProject,
    refreshSessions,
    loadSessionEvents,
    clearWorkspace,
    loadProject,
  } = useWorkspaceData(syncActiveSection);
  const { closeStream, trackOptimisticMessage, startChatMessageStream } = useWorkspaceStream({
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
  });

  useEffect(() => {
    let cancelled = false;
    if (!projectId) {
      closeStream();
      clearWorkspace();
      setIsLoading(false);
      return () => {
        cancelled = true;
      };
    }

    setIsLoading(true);
    loadProject(projectId)
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
  }, [clearWorkspace, closeStream, loadProject, projectId, setEvents, setIsLoading]);

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
    clearSessionEvents();
    trackOptimisticMessage(null);
    setProject((current) =>
      current
        ? {
            ...current,
            active_session_id: null,
          }
      : current,
    );
  }, [clearSessionEvents, closeStream, project, resetRecent, trackOptimisticMessage]);

  const handleSessionDelete = useCallback(
    async (session_id: string) => {
      if (!project || project.is_busy) {
        return;
      }
      closeStream();
      try {
        const response = await apiClient.deleteSession(project.project_id, session_id);
        const nextProject = await refreshProject(project.project_id);
        const refreshedSessions = await refreshSessions(
          project.project_id,
          response.next_session_id ?? nextProject.active_session_id,
        );
        if (selectedSessionId !== session_id) {
          return;
        }

        const nextSessionId =
          response.next_session_id ?? nextProject.active_session_id ?? refreshedSessions[0]?.session_id ?? null;
        setSelectedSessionId(nextSessionId);
        resetRecent();
        if (nextSessionId) {
          await loadSessionEvents(project.project_id, nextSessionId);
          return;
        }
        clearSessionEvents();
      } catch (error: unknown) {
        const messageText = error instanceof Error ? error.message : '删除对话失败。';
        setEvents((current) => [
          ...current,
          {
            id: `delete_session_error_${Date.now()}`,
            kind: 'message',
            role: 'assistant',
            text: messageText,
            timestamp: formatTimestamp(),
          },
        ]);
        await refreshProject(project.project_id);
      }
    },
    [
      clearSessionEvents,
      closeStream,
      loadSessionEvents,
      project,
      refreshProject,
      refreshSessions,
      resetRecent,
      selectedSessionId,
      setEvents,
      setSelectedSessionId,
    ],
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

    invalidateSessionEventLoads();
    trackOptimisticMessage(optimisticId);
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
      setIsCancelling(false);
      await startChatMessageStream(
        project.project_id,
        {
          session_id: selectedSessionId,
          message,
          active_section_id: activeSectionId || null,
          active_block_id: activeBlockId,
        },
      );
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
    invalidateSessionEventLoads,
    project,
    refreshProject,
    selectedSessionId,
    startChatMessageStream,
    trackOptimisticMessage,
  ]);

  const cancelCurrentRound = useCallback(async () => {
    if (!project?.is_busy || !project.running_session_id || !project.running_round_id || isCancelling) {
      return;
    }

    setIsCancelling(true);
    try {
      await apiClient.cancelRound(project.project_id, project.running_session_id, project.running_round_id);
      const nextProject = await refreshProject(project.project_id);
      await refreshSessions(project.project_id, nextProject.active_session_id);
      if (!nextProject.is_busy) {
        setIsCancelling(false);
      }
    } catch (error: unknown) {
      const messageText = error instanceof Error ? error.message : '取消任务失败。';
      setEvents((current) => [
        ...current,
        {
          id: `cancel_error_${Date.now()}`,
          kind: 'message',
          role: 'assistant',
          text: messageText,
          timestamp: formatTimestamp(),
        },
      ]);
      setIsCancelling(false);
      await refreshProject(project.project_id);
    }
  }, [isCancelling, project, refreshProject, refreshSessions]);

  const exportDocx = useCallback(async () => {
    if (!project) {
      return;
    }
    try {
      const result = await apiClient.downloadDocx(project.project_id);
      const filename = result.filename ?? `${project.project_id}.docx`;
      downloadBlob(result.blob, filename);
      setEvents((current) => [
        ...current,
        {
          id: `export_docx_${Date.now()}`,
          kind: 'message',
          role: 'assistant',
          text: `DOCX 已开始下载：\`${filename}\``,
          timestamp: formatTimestamp(),
        },
      ]);
    } catch (error: unknown) {
      const messageText = error instanceof Error ? error.message : '导出 DOCX 失败。';
      setEvents((current) => [
        ...current,
        {
          id: `export_docx_error_${Date.now()}`,
          kind: 'message',
          role: 'assistant',
          text: messageText,
          timestamp: formatTimestamp(),
        },
      ]);
    }
  }, [project]);

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
  const canCancelRound = Boolean(project?.is_busy && project.running_session_id && project.running_round_id);

  return {
    renderAst,
    renderUpdatedAt,
    events,
    composer,
    isLoading,
    isBusy: project?.is_busy ?? isLoading,
    isCancelling,
    canCancelRound,
    contextUsage,
    sessionTabs,
    activeSectionId,
    activeBlockId,
    previewFocusTarget,
    recentSectionIds,
    recentBlockIds,
    setComposer,
    setActiveSectionId,
    submitMessage,
    cancelCurrentRound,
    exportDocx,
    handleSessionSelect,
    handleNewSession,
    handleSessionDelete,
  };
}

function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
