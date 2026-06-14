import { useCallback, useState } from 'react';
import { hydrateEvents } from '../features/chat/chatEventTransforms';
import { apiClient } from '../services/api/client';
import type { ChatEvent, InnovationKernelState, ProjectState, RenderAst, SessionSummary } from '../types';

const EMPTY_DOCUMENT_TITLE = '未加载交底书';

const emptyRenderAst: RenderAst = {
  type: 'document',
  title: EMPTY_DOCUMENT_TITLE,
  meta: {
    document_type: 'patent_disclosure',
    schema_version: 'v3',
  },
  outline: [],
  children: [],
};

export const emptyInnovationKernel: InnovationKernelState = {
  exists: false,
  kernel_markdown: '',
  updated_at: null,
  source: null,
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
  const [innovationKernel, setInnovationKernel] = useState<InnovationKernelState>(emptyInnovationKernel);
  const [isLoading, setIsLoading] = useState(false);

  const refreshRenderAst = useCallback(
    async (project_id: string, focus?: RenderFocus) => {
      const renderResponse = await apiClient.getRenderAst(project_id, focus);
      setRenderAst(renderResponse.render_ast);
      syncActiveSection(
        renderResponse.active_section_id || focus?.focus_section_id || renderResponse.render_ast.outline[0]?.id || '',
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

  const refreshInnovationKernel = useCallback(async (project_id: string, session_id: string) => {
    const response = await apiClient.getInnovationKernel(project_id, session_id);
    setInnovationKernel(response);
    return response;
  }, []);

  const clearWorkspace = useCallback(() => {
    setProject(null);
    setRenderAst(emptyRenderAst);
    setEvents([]);
    setSessions([]);
    setSelectedSessionId(null);
    setInnovationKernel(emptyInnovationKernel);
    syncActiveSection(null);
  }, [syncActiveSection]);

  const loadProject = useCallback(async (project_id: string) => {
    const currentProject = await refreshProject(project_id);
    setProject(currentProject);
    await refreshRenderAst(currentProject.project_id);
    const knownSessions = await refreshSessions(currentProject.project_id, currentProject.active_session_id);
    const targetSessionId = currentProject.active_session_id || knownSessions[0]?.session_id;
    if (targetSessionId) {
      await loadSessionEvents(currentProject.project_id, targetSessionId);
      await refreshInnovationKernel(currentProject.project_id, targetSessionId);
      return;
    }
    setEvents([]);
    setInnovationKernel(emptyInnovationKernel);
  }, [loadSessionEvents, refreshInnovationKernel, refreshProject, refreshRenderAst, refreshSessions]);

  return {
    project,
    setProject,
    renderAst,
    events,
    setEvents,
    innovationKernel,
    setInnovationKernel,
    sessions,
    setSessions,
    selectedSessionId,
    setSelectedSessionId,
    isLoading,
    setIsLoading,
    refreshRenderAst,
    refreshProject,
    refreshSessions,
    refreshInnovationKernel,
    loadSessionEvents,
    clearWorkspace,
    loadProject,
  };
}
