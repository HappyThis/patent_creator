import { CSSProperties, PointerEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChatPanel } from '../features/chat/ChatPanel';
import { HomePage } from '../features/home/HomePage';
import { buildDocumentStats } from '../features/preview/documentStats';
import { PreviewPanel } from '../features/preview/PreviewPanel';
import { apiClient } from '../services/api/client';
import { useWorkspace } from '../hooks/useWorkspace';
import type { ProjectState } from '../types';

type WorkspaceStyle = CSSProperties & {
  '--right-pane-width': string;
  '--right-resizer-width': string;
};

const MIN_SIDE_WIDTH = 220;
const MAX_SIDE_WIDTH = 980;
const MIN_CHAT_WIDTH = 420;
const RESIZER_WIDTH = 10;

function readProjectIdFromPath(): string | null {
  const match = window.location.pathname.match(/^\/projects\/([^/]+)\/?$/);
  return match ? decodeURIComponent(match[1]) : null;
}

function clampSideWidth(value: number) {
  const availableWidth = window.innerWidth - RESIZER_WIDTH - MIN_CHAT_WIDTH;
  const viewportLimit = Math.max(MIN_SIDE_WIDTH, availableWidth);
  return Math.min(Math.max(value, MIN_SIDE_WIDTH), Math.min(MAX_SIDE_WIDTH, viewportLimit));
}

function App() {
  const previewRef = useRef<HTMLDivElement | null>(null);
  const previousDisclosureUpdatedAtRef = useRef<string | null>(null);
  const [projects, setProjects] = useState<ProjectState[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(() => readProjectIdFromPath());
  const [isDisclosureOpen, setIsDisclosureOpen] = useState(false);
  const [hasUnseenDisclosureUpdate, setHasUnseenDisclosureUpdate] = useState(false);
  const [disclosureWidth, setDisclosureWidth] = useState(460);
  const {
    renderAst,
    renderUpdatedAt,
    events,
    composer,
    isLoading,
    isBusy,
    isCancelling,
    canCancelRound,
    contextUsage,
    sessionTabs,
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
  } = useWorkspace(selectedProjectId);
  const documentStats = useMemo(() => buildDocumentStats(renderAst), [renderAst]);

  const loadProjects = useCallback(async () => {
    setProjectsLoading(true);
    setProjectsError(null);
    try {
      const response = await apiClient.listProjects();
      setProjects(response.projects);
    } catch (error: unknown) {
      setProjectsError(error instanceof Error ? error.message : '项目列表加载失败。');
    } finally {
      setProjectsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  useEffect(() => {
    if (projectsLoading || projectsError || !selectedProjectId) {
      return;
    }
    const selectedProjectExists = projects.some((project) => project.project_id === selectedProjectId);
    if (selectedProjectExists) {
      return;
    }
    window.history.replaceState(null, '', '/');
    setSelectedProjectId(null);
    setIsDisclosureOpen(false);
    setHasUnseenDisclosureUpdate(false);
  }, [projects, projectsError, projectsLoading, selectedProjectId]);

  useEffect(() => {
    const handlePopState = () => {
      setSelectedProjectId(readProjectIdFromPath());
      setIsDisclosureOpen(false);
      setHasUnseenDisclosureUpdate(false);
    };
    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, []);

  useEffect(() => {
    previousDisclosureUpdatedAtRef.current = null;
    setHasUnseenDisclosureUpdate(false);
  }, [selectedProjectId]);

  useEffect(() => {
    if (!selectedProjectId) {
      previousDisclosureUpdatedAtRef.current = null;
      setHasUnseenDisclosureUpdate(false);
      return;
    }
    if (!renderUpdatedAt) {
      return;
    }
    if (isLoading) {
      previousDisclosureUpdatedAtRef.current = renderUpdatedAt;
      return;
    }

    const previousUpdatedAt = previousDisclosureUpdatedAtRef.current;
    previousDisclosureUpdatedAtRef.current = renderUpdatedAt;

    if (previousUpdatedAt === null || previousUpdatedAt === renderUpdatedAt) {
      return;
    }
    if (!isDisclosureOpen) {
      setHasUnseenDisclosureUpdate(true);
    }
  }, [isDisclosureOpen, isLoading, renderUpdatedAt, selectedProjectId]);

  useEffect(() => {
    if (isDisclosureOpen) {
      setHasUnseenDisclosureUpdate(false);
    }
  }, [isDisclosureOpen]);

  const handleProjectSelect = useCallback((projectId: string) => {
    window.history.pushState(null, '', `/projects/${encodeURIComponent(projectId)}`);
    setSelectedProjectId(projectId);
    setIsDisclosureOpen(false);
    setHasUnseenDisclosureUpdate(false);
  }, []);

  const handleReturnHome = useCallback(() => {
    window.history.pushState(null, '', '/');
    previousDisclosureUpdatedAtRef.current = null;
    setSelectedProjectId(null);
    setIsDisclosureOpen(false);
    setHasUnseenDisclosureUpdate(false);
  }, []);

  const handleProjectCreate = useCallback(
    async (payload: { project_name: string; disclosure_title?: string | null }) => {
      const project = await apiClient.createProject(payload);
      setProjects((current) => [project, ...current.filter((item) => item.project_id !== project.project_id)]);
      handleProjectSelect(project.project_id);
    },
    [handleProjectSelect],
  );

  const handleProjectDelete = useCallback(
    async (projectId: string) => {
      const response = await apiClient.deleteProject(projectId);
      if (selectedProjectId === projectId) {
        const nextPath = response.next_project_id
          ? `/projects/${encodeURIComponent(response.next_project_id)}`
          : '/';
        window.history.pushState(null, '', nextPath);
        setSelectedProjectId(response.next_project_id);
        setIsDisclosureOpen(false);
        setHasUnseenDisclosureUpdate(false);
      }
      void loadProjects();
    },
    [loadProjects, selectedProjectId],
  );

  const handleProjectRename = useCallback(async (projectId: string, projectName: string) => {
    const project = await apiClient.renameProject(projectId, { project_name: projectName });
    setProjects((current) => current.map((item) => (item.project_id === project.project_id ? project : item)));
  }, []);

  useEffect(() => {
    const normalizeSideWidths = () => {
      if (!isDisclosureOpen) {
        return;
      }
      setDisclosureWidth((current) => {
        const next = clampSideWidth(current);
        return next === current ? current : next;
      });
    };

    normalizeSideWidths();
    window.addEventListener('resize', normalizeSideWidths);
    return () => window.removeEventListener('resize', normalizeSideWidths);
  }, [isDisclosureOpen]);

  const beginResize = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      const startX = event.clientX;
      const startWidth = disclosureWidth;

      const handlePointerMove = (moveEvent: globalThis.PointerEvent) => {
        const delta = startX - moveEvent.clientX;
        const nextWidth = clampSideWidth(startWidth + delta);
        setDisclosureWidth(nextWidth);
      };

      const handlePointerUp = () => {
        document.body.classList.remove('is-resizing-workspace');
        window.removeEventListener('pointermove', handlePointerMove);
        window.removeEventListener('pointerup', handlePointerUp);
      };

      document.body.classList.add('is-resizing-workspace');
      window.addEventListener('pointermove', handlePointerMove);
      window.addEventListener('pointerup', handlePointerUp);
    },
    [disclosureWidth],
  );

  const toggleDisclosurePreview = useCallback(() => {
    if (isDisclosureOpen) {
      setIsDisclosureOpen(false);
      return;
    }
    setDisclosureWidth(clampSideWidth(MAX_SIDE_WIDTH));
    setIsDisclosureOpen(true);
  }, [isDisclosureOpen]);

  if (!selectedProjectId) {
    return (
      <div className="app-shell home-shell">
        <HomePage
          projects={projects}
          isLoading={projectsLoading}
          error={projectsError}
          onSelectProject={handleProjectSelect}
          onCreateProject={handleProjectCreate}
          onDeleteProject={handleProjectDelete}
          onRenameProject={handleProjectRename}
        />
      </div>
    );
  }

  const workspaceStyle: WorkspaceStyle = {
    '--right-pane-width': isDisclosureOpen ? `${disclosureWidth}px` : '0px',
    '--right-resizer-width': isDisclosureOpen ? '10px' : '0px',
  };
  const disclosureToggleLabel = hasUnseenDisclosureUpdate
    ? '交底书已更新，点击预览'
    : isDisclosureOpen
      ? '收起交底书预览'
      : '预览交底书';

  return (
    <div className="app-shell workspace-shell">
      <main
        className={[
          'workspace',
          'chat-workspace',
          isDisclosureOpen ? 'side-panel-open' : 'no-side-panels',
          isDisclosureOpen ? 'disclosure-open' : '',
        ].filter(Boolean).join(' ')}
        style={workspaceStyle}
      >
        <section className="chat-stage">
          <button
            className="workspace-home-button"
            type="button"
            onClick={handleReturnHome}
            aria-label="返回首页"
            title="返回首页"
          >
            <svg className="workspace-home-icon" viewBox="0 0 24 24" aria-hidden="true">
              <path d="M4.5 11.5 12 5l7.5 6.5" />
              <path d="M6.8 10.2V19h10.4v-8.8" />
              <path d="M10 19v-5h4v5" />
            </svg>
          </button>

          <button
            className={[
              'workspace-edge-toggle',
              'workspace-edge-toggle-right',
              'workspace-edge-toggle-disclosure',
              isDisclosureOpen ? 'active' : '',
              hasUnseenDisclosureUpdate ? 'has-disclosure-update' : '',
            ].filter(Boolean).join(' ')}
            type="button"
            onClick={toggleDisclosurePreview}
            aria-pressed={isDisclosureOpen}
            aria-label={disclosureToggleLabel}
            title={disclosureToggleLabel}
          >
            <PanelChevron direction={isDisclosureOpen ? 'right' : 'left'} />
          </button>

          <ChatPanel
            sessionTabs={sessionTabs}
            events={events}
            composer={composer}
            isBusy={isBusy || isLoading}
            contextUsage={contextUsage}
            onComposerChange={setComposer}
            onSubmit={submitMessage}
            onCancel={cancelCurrentRound}
            onSessionSelect={handleSessionSelect}
            onNewSession={handleNewSession}
            canCancel={canCancelRound}
            isCancelling={isCancelling}
          />
        </section>

        {isDisclosureOpen ? (
          <div
            className="workspace-resizer workspace-resizer-right"
            role="separator"
            aria-label="调整交底书预览宽度"
            onPointerDown={beginResize}
          />
        ) : null}

        {isDisclosureOpen ? (
          <div className="workspace-side-pane workspace-side-preview">
            <PreviewPanel
              renderAst={renderAst}
              previewFocusTarget={previewFocusTarget}
              recentSectionIds={recentSectionIds}
              recentBlockIds={recentBlockIds}
              stats={documentStats}
              previewRef={previewRef}
              onActiveSectionChange={setActiveSectionId}
              onExport={exportDocx}
            />
          </div>
        ) : null}
      </main>
    </div>
  );
}

function PanelChevron({ direction }: { direction: 'left' | 'right' }) {
  const points = direction === 'left' ? '7.5 3 4.5 6 7.5 9' : '4.5 3 7.5 6 4.5 9';

  return (
    <span className="workspace-panel-icon" aria-hidden="true">
      <svg viewBox="0 0 12 12" focusable="false">
        <polyline points={points} />
      </svg>
    </span>
  );
}

export default App;
