import { CSSProperties, PointerEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChatPanel } from '../features/chat/ChatPanel';
import { buildDocumentStats } from '../features/preview/documentStats';
import { PreviewPanel } from '../features/preview/PreviewPanel';
import { WorkspaceSidebar } from '../features/workspace/WorkspaceSidebar';
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

function projectPath(projectId: string): string {
  return `/projects/${encodeURIComponent(projectId)}`;
}

function clampSideWidth(value: number) {
  const availableWidth = window.innerWidth - RESIZER_WIDTH - MIN_CHAT_WIDTH;
  const viewportLimit = Math.max(MIN_SIDE_WIDTH, availableWidth);
  return Math.min(Math.max(value, MIN_SIDE_WIDTH), Math.min(MAX_SIDE_WIDTH, viewportLimit));
}

function App() {
  return <WorkspaceApp />;
}

function WorkspaceApp() {
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
    project,
    renderAst,
    renderUpdatedAt,
    events,
    composer,
    isLoading,
    isBusy,
    isCancelling,
    canCancelRound,
    contextUsage,
    qualityMode,
    sessionTabs,
    previewFocusTarget,
    recentSectionIds,
    recentBlockIds,
    setComposer,
    setActiveSectionId,
    setQualityMode,
    submitMessage,
    cancelCurrentRound,
    exportDocx,
    refreshRenderAst,
    handleSessionSelect,
    handleNewSession,
    handleSessionDelete,
  } = useWorkspace(selectedProjectId);
  const documentStats = useMemo(() => buildDocumentStats(renderAst), [renderAst]);

  useEffect(() => {
    if (!project) {
      return;
    }
    setProjects((current) =>
      current.map((item) => (item.project_id === project.project_id ? project : item)),
    );
  }, [project]);

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
    if (projectsLoading || projectsError) {
      return;
    }

    if (!selectedProjectId && projects.length > 0) {
      const nextProjectId = projects[0].project_id;
      window.history.replaceState(null, '', projectPath(nextProjectId));
      setSelectedProjectId(nextProjectId);
      return;
    }

    if (!selectedProjectId) {
      return;
    }

    const selectedProjectExists = projects.some((project) => project.project_id === selectedProjectId);
    if (selectedProjectExists) {
      return;
    }

    const nextProjectId = projects[0]?.project_id ?? null;
    window.history.replaceState(null, '', nextProjectId ? projectPath(nextProjectId) : '/');
    setSelectedProjectId(nextProjectId);
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
    window.history.pushState(null, '', projectPath(projectId));
    setSelectedProjectId(projectId);
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
        const nextProjectId = response.next_project_id;
        window.history.pushState(null, '', nextProjectId ? projectPath(nextProjectId) : '/');
        setSelectedProjectId(nextProjectId);
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

  const handleFigureSaved = useCallback(() => {
    if (selectedProjectId) {
      void refreshRenderAst(selectedProjectId);
    }
  }, [refreshRenderAst, selectedProjectId]);

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
        <WorkspaceSidebar
          projects={projects}
          selectedProjectId={selectedProjectId}
          sessionTabs={sessionTabs}
          isLoadingProjects={projectsLoading}
          projectsError={projectsError}
          isWorkspaceBusy={isBusy || isLoading}
          onSelectProject={handleProjectSelect}
          onCreateProject={handleProjectCreate}
          onDeleteProject={handleProjectDelete}
          onRenameProject={handleProjectRename}
          onSessionSelect={handleSessionSelect}
          onNewSession={handleNewSession}
          onDeleteSession={handleSessionDelete}
        />

        <section className="chat-stage">
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
            events={events}
            composer={composer}
            isBusy={isBusy || isLoading || !selectedProjectId}
            contextUsage={contextUsage}
            qualityMode={qualityMode}
            onComposerChange={setComposer}
            onQualityModeChange={setQualityMode}
            onSubmit={submitMessage}
            onCancel={cancelCurrentRound}
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
              projectId={selectedProjectId}
              renderAst={renderAst}
              previewFocusTarget={previewFocusTarget}
              recentSectionIds={recentSectionIds}
              recentBlockIds={recentBlockIds}
              stats={documentStats}
              previewRef={previewRef}
              onActiveSectionChange={setActiveSectionId}
              onExport={exportDocx}
              onFigureSaved={handleFigureSaved}
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
