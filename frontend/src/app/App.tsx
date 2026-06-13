import { CSSProperties, PointerEvent, useCallback, useEffect, useRef, useState } from 'react';
import { ChatPanel } from '../features/chat/ChatPanel';
import { HomePage } from '../features/home/HomePage';
import { InnovationKernelPanel } from '../features/kernel/InnovationKernelPanel';
import { buildDocumentStats } from '../features/preview/documentStats';
import { PreviewPanel } from '../features/preview/PreviewPanel';
import { apiClient } from '../services/api/client';
import { useWorkspace } from '../hooks/useWorkspace';
import type { ProjectState } from '../types';

type WorkspaceStyle = CSSProperties & {
  '--left-pane-width': string;
  '--right-pane-width': string;
  '--left-resizer-width': string;
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

function clampSideWidth(value: number, otherSideWidth = 0) {
  const resizerReserve = otherSideWidth > 0 ? RESIZER_WIDTH * 2 : RESIZER_WIDTH;
  const availableWidth = window.innerWidth - otherSideWidth - resizerReserve - MIN_CHAT_WIDTH;
  const viewportLimit = Math.max(MIN_SIDE_WIDTH, availableWidth);
  return Math.min(Math.max(value, MIN_SIDE_WIDTH), Math.min(MAX_SIDE_WIDTH, viewportLimit));
}

function App() {
  const previewRef = useRef<HTMLDivElement | null>(null);
  const [projects, setProjects] = useState<ProjectState[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [projectsError, setProjectsError] = useState<string | null>(null);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(() => readProjectIdFromPath());
  const [isKernelOpen, setIsKernelOpen] = useState(false);
  const [isDisclosureOpen, setIsDisclosureOpen] = useState(false);
  const [hasUnseenKernelUpdate, setHasUnseenKernelUpdate] = useState(false);
  const [kernelWidth, setKernelWidth] = useState(340);
  const [disclosureWidth, setDisclosureWidth] = useState(460);
  const previousKernelUpdateRef = useRef<string | null>(null);
  const {
    renderAst,
    events,
    composer,
    isLoading,
    isBusy,
    isCancelling,
    canCancelRound,
    contextUsage,
    sessionTabs,
    innovationKernel,
    previewFocusTarget,
    recentSectionIds,
    recentBlockIds,
    setComposer,
    setActiveSectionId,
    submitMessage,
    cancelCurrentRound,
    exportMarkdown,
    handleSessionSelect,
    handleNewSession,
  } = useWorkspace(selectedProjectId);
  const documentStats = buildDocumentStats(renderAst);
  const activeSessionId = sessionTabs.find((tab) => tab.active)?.session_id ?? null;
  const hasInnovationKernel = innovationKernel.exists && innovationKernel.kernel_markdown.trim().length > 0;

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
    const handlePopState = () => {
      setSelectedProjectId(readProjectIdFromPath());
      setIsKernelOpen(false);
      setIsDisclosureOpen(false);
    };
    window.addEventListener('popstate', handlePopState);
    return () => window.removeEventListener('popstate', handlePopState);
  }, []);

  useEffect(() => {
    previousKernelUpdateRef.current = null;
    setHasUnseenKernelUpdate(false);
  }, [activeSessionId, selectedProjectId]);

  useEffect(() => {
    const updateKey = innovationKernel.updated_at ?? (hasInnovationKernel ? innovationKernel.kernel_markdown : null);
    if (!updateKey) {
      previousKernelUpdateRef.current = null;
      setHasUnseenKernelUpdate(false);
      return;
    }

    if (previousKernelUpdateRef.current === null) {
      previousKernelUpdateRef.current = updateKey;
      if (!isKernelOpen && isBusy) {
        setHasUnseenKernelUpdate(true);
      }
      return;
    }

    if (previousKernelUpdateRef.current !== updateKey) {
      previousKernelUpdateRef.current = updateKey;
      if (!isKernelOpen) {
        setHasUnseenKernelUpdate(true);
      }
    }
  }, [hasInnovationKernel, innovationKernel.kernel_markdown, innovationKernel.updated_at, isBusy, isKernelOpen]);

  useEffect(() => {
    if (isKernelOpen) {
      setHasUnseenKernelUpdate(false);
    }
  }, [isKernelOpen]);

  const handleProjectSelect = useCallback((projectId: string) => {
    window.history.pushState(null, '', `/projects/${encodeURIComponent(projectId)}`);
    setSelectedProjectId(projectId);
    setIsKernelOpen(false);
    setIsDisclosureOpen(false);
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
        setIsKernelOpen(false);
        setIsDisclosureOpen(false);
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
      let nextKernelWidth = kernelWidth;
      let nextDisclosureWidth = disclosureWidth;

      if (isKernelOpen) {
        nextKernelWidth = clampSideWidth(nextKernelWidth, isDisclosureOpen ? nextDisclosureWidth : 0);
      }
      if (isDisclosureOpen) {
        nextDisclosureWidth = clampSideWidth(nextDisclosureWidth, isKernelOpen ? nextKernelWidth : 0);
      }

      if (nextKernelWidth !== kernelWidth) {
        setKernelWidth(nextKernelWidth);
      }
      if (nextDisclosureWidth !== disclosureWidth) {
        setDisclosureWidth(nextDisclosureWidth);
      }
    };

    normalizeSideWidths();
    window.addEventListener('resize', normalizeSideWidths);
    return () => window.removeEventListener('resize', normalizeSideWidths);
  }, [disclosureWidth, isDisclosureOpen, isKernelOpen, kernelWidth]);

  const beginResize = useCallback(
    (side: 'kernel' | 'disclosure', event: PointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      const startX = event.clientX;
      const startWidth = side === 'kernel' ? kernelWidth : disclosureWidth;
      const otherSideWidth =
        side === 'kernel'
          ? isDisclosureOpen ? disclosureWidth : 0
          : isKernelOpen ? kernelWidth : 0;

      const handlePointerMove = (moveEvent: globalThis.PointerEvent) => {
        const delta = side === 'kernel' ? moveEvent.clientX - startX : startX - moveEvent.clientX;
        const nextWidth = clampSideWidth(startWidth + delta, otherSideWidth);
        if (side === 'kernel') {
          setKernelWidth(nextWidth);
          return;
        }
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
    [disclosureWidth, isDisclosureOpen, isKernelOpen, kernelWidth],
  );

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
    '--left-pane-width': isKernelOpen ? `${kernelWidth}px` : '0px',
    '--right-pane-width': isDisclosureOpen ? `${disclosureWidth}px` : '0px',
    '--left-resizer-width': isKernelOpen ? '10px' : '0px',
    '--right-resizer-width': isDisclosureOpen ? '10px' : '0px',
  };
  const kernelToggleLabel = hasUnseenKernelUpdate
    ? '技术内核已更新，点击查看'
    : isKernelOpen
      ? '收起技术内核'
      : '展开技术内核';

  return (
    <div className="app-shell workspace-shell">
      <main
        className={[
          'workspace',
          'chat-workspace',
          isKernelOpen || isDisclosureOpen ? 'side-panel-open' : 'no-side-panels',
          isKernelOpen ? 'kernel-open' : '',
          isDisclosureOpen ? 'disclosure-open' : '',
        ].filter(Boolean).join(' ')}
        style={workspaceStyle}
      >
        {isKernelOpen ? (
          <div className="workspace-side-pane workspace-side-kernel">
            <InnovationKernelPanel innovationKernel={innovationKernel} />
          </div>
        ) : null}

        {isKernelOpen ? (
          <div
            className="workspace-resizer workspace-resizer-left"
            role="separator"
            aria-label="调整创新内核宽度"
            onPointerDown={(event) => beginResize('kernel', event)}
          />
        ) : null}

        <section className="chat-stage">
          <button
            className={[
              'workspace-edge-toggle',
              'workspace-edge-toggle-left',
              'workspace-edge-toggle-kernel',
              isKernelOpen ? 'active' : '',
              hasInnovationKernel ? 'has-content' : 'empty',
              hasUnseenKernelUpdate ? 'has-unseen-update' : '',
            ].filter(Boolean).join(' ')}
            type="button"
            onClick={() => setIsKernelOpen((current) => !current)}
            aria-pressed={isKernelOpen}
            aria-label={kernelToggleLabel}
            title={kernelToggleLabel}
          >
            <span className="workspace-panel-icon workspace-panel-icon-left" aria-hidden="true" />
          </button>

          <button
            className={[
              'workspace-edge-toggle',
              'workspace-edge-toggle-right',
              'workspace-edge-toggle-disclosure',
              isDisclosureOpen ? 'active' : '',
              hasInnovationKernel ? 'kernel-ready' : 'requires-kernel',
            ].filter(Boolean).join(' ')}
            type="button"
            onClick={() => setIsDisclosureOpen((current) => !current)}
            aria-pressed={isDisclosureOpen}
            aria-label={
              isDisclosureOpen
                ? '收起交底书预览'
                : '预览交底书'
            }
            title={
              isDisclosureOpen
                ? '收起交底书预览'
                : '预览交底书'
            }
          >
            <span className="workspace-panel-icon workspace-panel-icon-right" aria-hidden="true" />
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
            onExport={exportMarkdown}
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
            onPointerDown={(event) => beginResize('disclosure', event)}
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
            />
          </div>
        ) : null}
      </main>
    </div>
  );
}

export default App;
