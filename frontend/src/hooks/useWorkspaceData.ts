import { useCallback, useState } from 'react';
import { hydrateEvents } from '../features/chat/chatEventTransforms';
import { apiClient } from '../services/api/client';
import type { ChatEvent, OutlineItem, ProjectState, RenderAst, SessionSummary } from '../types';

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

type RenderFocus = {
  focus_section_id?: string | null;
  focus_block_id?: string | null;
};

export function useWorkspaceData(syncActiveSection: (sectionId: string | null | undefined) => void) {
  const [project, setProject] = useState<ProjectState | null>(null);
  const [renderAst, setRenderAst] = useState<RenderAst>(emptyRenderAst);
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const refreshRenderAst = useCallback(
    async (project_id: string, focus?: RenderFocus) => {
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

  return {
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
    ensureProject,
  };
}
