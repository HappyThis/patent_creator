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
    selectSection,
    syncActiveSection,
    focusDocumentChange,
    resetRecent,
  } = useWorkspaceSelection();
  const {
    project,
    setProject,
    renderAst,
    events,
    setEvents,
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
    setEvents([]);
    trackOptimisticMessage(null);
    setProject((current) =>
      current
        ? {
            ...current,
            active_session_id: null,
          }
        : current,
    );
  }, [closeStream, project, resetRecent, trackOptimisticMessage]);

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

  const exportMarkdown = useCallback(async () => {
    if (!project) {
      return;
    }
    try {
      const result = await apiClient.exportMarkdown(project.project_id);
      setEvents((current) => [
        ...current,
        {
          id: `export_markdown_${Date.now()}`,
          kind: 'message',
          role: 'assistant',
          text: `Markdown 已导出：\`${result.path}\``,
          timestamp: formatTimestamp(),
        },
      ]);
    } catch (error: unknown) {
      const messageText = error instanceof Error ? error.message : '导出 Markdown 失败。';
      setEvents((current) => [
        ...current,
        {
          id: `export_error_${Date.now()}`,
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
    selectSection,
    submitMessage,
    cancelCurrentRound,
    exportMarkdown,
    handleSessionSelect,
    handleNewSession,
  };
}
