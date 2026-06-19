import { useCallback, useRef, useState } from 'react';
import { hydrateEvents } from '../features/chat/chatEventTransforms';
import { apiClient } from '../services/api/client';
import type { ChatEvent, ProjectState, RenderAst, SessionSummary } from '../types';

const EMPTY_DOCUMENT_TITLE = '未加载交底书';

const emptyRenderAst: RenderAst = {
  type: 'document',
  title: EMPTY_DOCUMENT_TITLE,
  meta: {
    document_type: 'patent_disclosure',
    schema_version: 'v3.3',
  },
  figures: [],
  outline: [],
  children: [],
};

type RenderFocus = {
  focus_section_id?: string | null;
  focus_block_id?: string | null;
};

export function useWorkspaceData(syncActiveSection: (sectionId: string | null | undefined) => void) {
  const loadProjectRequestIdRef = useRef(0);
  const sessionEventsRequestIdRef = useRef(0);
  const [project, setProject] = useState<ProjectState | null>(null);
  const [renderAst, setRenderAst] = useState<RenderAst>(emptyRenderAst);
  const [renderUpdatedAt, setRenderUpdatedAt] = useState<string | null>(null);
  const [events, setEvents] = useState<ChatEvent[]>([]);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const refreshRenderAst = useCallback(
    async (project_id: string, focus?: RenderFocus) => {
      const renderResponse = await apiClient.getRenderAst(project_id, focus);
      setRenderAst(renderResponse.render_ast);
      setRenderUpdatedAt(renderResponse.updated_at);
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
    const requestId = sessionEventsRequestIdRef.current + 1;
    sessionEventsRequestIdRef.current = requestId;
    const response = await apiClient.getSessionEvents(project_id, session_id);
    if (sessionEventsRequestIdRef.current !== requestId) {
      return;
    }
    setEvents(hydrateEvents(response.events));
  }, []);

  const invalidateSessionEventLoads = useCallback(() => {
    sessionEventsRequestIdRef.current += 1;
  }, []);

  const clearSessionEvents = useCallback(() => {
    invalidateSessionEventLoads();
    setEvents([]);
  }, [invalidateSessionEventLoads]);

  const clearWorkspace = useCallback(() => {
    loadProjectRequestIdRef.current += 1;
    invalidateSessionEventLoads();
    setProject(null);
    setRenderAst(emptyRenderAst);
    setRenderUpdatedAt(null);
    setEvents([]);
    setSessions([]);
    setSelectedSessionId(null);
    syncActiveSection(null);
  }, [invalidateSessionEventLoads, syncActiveSection]);

  const loadProject = useCallback(async (project_id: string) => {
    const requestId = loadProjectRequestIdRef.current + 1;
    loadProjectRequestIdRef.current = requestId;
    const sessionEventsRequestId = sessionEventsRequestIdRef.current + 1;
    sessionEventsRequestIdRef.current = sessionEventsRequestId;
    const isCurrentRequest = () => loadProjectRequestIdRef.current === requestId;
    const isCurrentSessionEventsRequest = () => sessionEventsRequestIdRef.current === sessionEventsRequestId;

    const currentProject = await apiClient.getProject(project_id);
    if (!isCurrentRequest()) {
      return;
    }
    setProject(currentProject);

    const renderResponse = await apiClient.getRenderAst(currentProject.project_id);
    if (!isCurrentRequest()) {
      return;
    }
    setRenderAst(renderResponse.render_ast);
    setRenderUpdatedAt(renderResponse.updated_at);
    syncActiveSection(renderResponse.active_section_id || renderResponse.render_ast.outline[0]?.id || '');

    const sessionsResponse = await apiClient.listSessions(currentProject.project_id);
    if (!isCurrentRequest()) {
      return;
    }
    const knownSessions = sessionsResponse.sessions;
    setSessions(knownSessions);
    setSelectedSessionId(currentProject.active_session_id ?? knownSessions[0]?.session_id ?? null);

    const targetSessionId = currentProject.active_session_id || knownSessions[0]?.session_id;
    if (targetSessionId) {
      const eventsResponse = await apiClient.getSessionEvents(currentProject.project_id, targetSessionId);
      if (!isCurrentRequest() || !isCurrentSessionEventsRequest()) {
        return;
      }
      setEvents(hydrateEvents(eventsResponse.events));
      return;
    }
    if (isCurrentSessionEventsRequest()) {
      setEvents([]);
    }
  }, [syncActiveSection]);

  return {
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
  };
}
