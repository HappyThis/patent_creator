import { FormEvent, useState } from 'react';
import type { ProjectState, SessionTab } from '../../types';

type WorkspaceSidebarProps = {
  projects: ProjectState[];
  selectedProjectId: string | null;
  sessionTabs: SessionTab[];
  isLoadingProjects: boolean;
  projectsError: string | null;
  isWorkspaceBusy: boolean;
  onSelectProject: (projectId: string) => void;
  onCreateProject: (payload: { project_name: string; disclosure_title?: string | null }) => Promise<void>;
  onDeleteProject: (projectId: string) => Promise<void>;
  onRenameProject: (projectId: string, projectName: string) => Promise<void>;
  onSessionSelect: (sessionId: string) => void;
  onNewSession: () => void;
  onDeleteSession: (sessionId: string) => Promise<void>;
};

function formatRelativeTime(value?: string | null): string {
  if (!value) {
    return '';
  }
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) {
    return '';
  }
  const minutes = Math.max(1, Math.round((Date.now() - timestamp) / 60000));
  if (minutes < 60) {
    return `${minutes} 分`;
  }
  const hours = Math.round(minutes / 60);
  if (hours < 24) {
    return `${hours} 时`;
  }
  const days = Math.round(hours / 24);
  if (days < 7) {
    return `${days} 天`;
  }
  return `${Math.round(days / 7)} 周`;
}

export function WorkspaceSidebar({
  projects,
  selectedProjectId,
  sessionTabs,
  isLoadingProjects,
  projectsError,
  isWorkspaceBusy,
  onSelectProject,
  onCreateProject,
  onDeleteProject,
  onRenameProject,
  onSessionSelect,
  onNewSession,
  onDeleteSession,
}: WorkspaceSidebarProps) {
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [projectName, setProjectName] = useState('');
  const [disclosureTitle, setDisclosureTitle] = useState('');
  const [formError, setFormError] = useState<string | null>(null);
  const [isCreating, setIsCreating] = useState(false);
  const [renamingProjectId, setRenamingProjectId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [isRenaming, setIsRenaming] = useState(false);
  const [deletingProjectId, setDeletingProjectId] = useState<string | null>(null);
  const [deletingSessionId, setDeletingSessionId] = useState<string | null>(null);

  const selectedProject = projects.find((project) => project.project_id === selectedProjectId) ?? null;

  const handleCreateSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmedProjectName = projectName.trim();
    const trimmedDisclosureTitle = disclosureTitle.trim();
    if (!trimmedProjectName) {
      setFormError('项目名不能为空。');
      return;
    }
    setFormError(null);
    setIsCreating(true);
    try {
      await onCreateProject({
        project_name: trimmedProjectName,
        disclosure_title: trimmedDisclosureTitle || null,
      });
      setProjectName('');
      setDisclosureTitle('');
      setIsCreateOpen(false);
    } catch (error: unknown) {
      setFormError(error instanceof Error ? error.message : '创建项目失败。');
    } finally {
      setIsCreating(false);
    }
  };

  const beginRename = (project: ProjectState) => {
    if (project.is_busy) {
      return;
    }
    setFormError(null);
    setRenamingProjectId(project.project_id);
    setRenameValue(project.title);
  };

  const submitRename = async (project: ProjectState) => {
    const nextName = renameValue.trim();
    if (!nextName) {
      setFormError('项目名不能为空。');
      return;
    }
    if (nextName === project.title) {
      setRenamingProjectId(null);
      return;
    }
    setIsRenaming(true);
    setFormError(null);
    try {
      await onRenameProject(project.project_id, nextName);
      setRenamingProjectId(null);
    } catch (error: unknown) {
      setFormError(error instanceof Error ? error.message : '重命名失败。');
    } finally {
      setIsRenaming(false);
    }
  };

  const deleteProject = async (project: ProjectState) => {
    if (project.is_busy) {
      return;
    }
    const confirmed = window.confirm(`确定删除项目「${project.title}」吗？`);
    if (!confirmed) {
      return;
    }
    setDeletingProjectId(project.project_id);
    setFormError(null);
    try {
      await onDeleteProject(project.project_id);
    } catch (error: unknown) {
      setFormError(error instanceof Error ? error.message : '删除项目失败。');
    } finally {
      setDeletingProjectId(null);
    }
  };

  const deleteSession = async (session: SessionTab) => {
    if (isWorkspaceBusy) {
      return;
    }
    const confirmed = window.confirm(`确定删除对话「${session.title}」吗？`);
    if (!confirmed) {
      return;
    }
    setDeletingSessionId(session.session_id);
    setFormError(null);
    try {
      await onDeleteSession(session.session_id);
    } catch (error: unknown) {
      setFormError(error instanceof Error ? error.message : '删除对话失败。');
    } finally {
      setDeletingSessionId(null);
    }
  };

  return (
    <aside className="workspace-sidebar" aria-label="项目和对话">
      <div className="workspace-sidebar-section">
        <div className="workspace-sidebar-heading">
          <span>项目</span>
          <button
            className="workspace-sidebar-icon-button"
            type="button"
            onClick={() => {
              setFormError(null);
              setIsCreateOpen((current) => !current);
            }}
            aria-label="新建项目"
            title="新建项目"
          >
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path d="M8 3v10M3 8h10" />
            </svg>
          </button>
        </div>

        {projectsError ? <div className="workspace-sidebar-error">{projectsError}</div> : null}
        {formError ? <div className="workspace-sidebar-error">{formError}</div> : null}

        {isCreateOpen ? (
          <form className="workspace-project-form" onSubmit={handleCreateSubmit}>
            <input
              value={projectName}
              onChange={(event) => setProjectName(event.target.value)}
              placeholder="项目名"
              disabled={isCreating}
              autoFocus
            />
            <input
              value={disclosureTitle}
              onChange={(event) => setDisclosureTitle(event.target.value)}
              placeholder="交底书名称，可选"
              disabled={isCreating}
            />
            <div className="workspace-project-form-actions">
              <button type="submit" disabled={isCreating}>{isCreating ? '创建中' : '创建'}</button>
              <button type="button" disabled={isCreating} onClick={() => setIsCreateOpen(false)}>取消</button>
            </div>
          </form>
        ) : null}

        <div className="workspace-sidebar-list">
          {isLoadingProjects ? <div className="workspace-sidebar-empty">正在加载项目</div> : null}
          {!isLoadingProjects && projects.length === 0 ? <div className="workspace-sidebar-empty">暂无项目</div> : null}
          {projects.map((project) => {
            const selected = project.project_id === selectedProjectId;
            return (
              <div key={project.project_id} className={`workspace-project-group ${selected ? 'selected' : ''}`}>
                {renamingProjectId === project.project_id ? (
                  <div className="workspace-project-rename">
                    <input
                      value={renameValue}
                      onChange={(event) => setRenameValue(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') {
                          event.preventDefault();
                          void submitRename(project);
                        }
                        if (event.key === 'Escape') {
                          setRenamingProjectId(null);
                        }
                      }}
                      disabled={isRenaming}
                      autoFocus
                    />
                    <div className="workspace-project-row-actions">
                      <button type="button" onClick={() => void submitRename(project)} disabled={isRenaming}>保存</button>
                      <button type="button" onClick={() => setRenamingProjectId(null)} disabled={isRenaming}>取消</button>
                    </div>
                  </div>
                ) : (
                  <div className="workspace-project-row">
                    <button
                      className="workspace-project-button"
                      type="button"
                      onClick={() => onSelectProject(project.project_id)}
                    >
                      <svg viewBox="0 0 16 16" aria-hidden="true">
                        <path d="M2.5 5.5h11v7h-11z" />
                        <path d="M2.5 5.5V3.8h4.1l1.2 1.7" />
                      </svg>
                      <span>{project.title}</span>
                      {project.is_busy ? <span className="workspace-sidebar-status">运行中</span> : null}
                    </button>
                    <div className="workspace-project-actions">
                      <button type="button" onClick={() => beginRename(project)} disabled={project.is_busy} aria-label="重命名项目">
                        <svg viewBox="0 0 16 16" aria-hidden="true">
                          <path d="m3 11.8.5-2.4L10.8 2l2.2 2.2-7.4 7.3z" />
                          <path d="M9.7 3.1 11.9 5.3" />
                        </svg>
                      </button>
                      <button
                        type="button"
                        onClick={() => void deleteProject(project)}
                        disabled={project.is_busy || deletingProjectId === project.project_id}
                        aria-label="删除项目"
                      >
                        <svg viewBox="0 0 16 16" aria-hidden="true">
                          <path d="M3 4.5h10" />
                          <path d="M6.3 4.5V3h3.4v1.5" />
                          <path d="M5 6.3V13h6V6.3" />
                        </svg>
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      <div className="workspace-sidebar-section workspace-session-section">
        <div className="workspace-sidebar-heading">
          <span>对话</span>
          <button
            className="workspace-sidebar-icon-button"
            type="button"
            onClick={onNewSession}
            disabled={!selectedProject || isWorkspaceBusy}
            aria-label="新建对话"
            title="新建对话"
          >
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path d="M8 3v10M3 8h10" />
            </svg>
          </button>
        </div>

        {!selectedProject ? (
          <div className="workspace-sidebar-empty">选择或创建项目后开始对话</div>
        ) : sessionTabs.length > 0 ? (
          <div className="workspace-sidebar-list">
            {sessionTabs.map((session) => (
              <div
                key={session.session_id}
                className={`workspace-session-row ${session.active ? 'active' : ''}`}
              >
                <button
                  className="workspace-session-button"
                  type="button"
                  onClick={() => onSessionSelect(session.session_id)}
                  disabled={isWorkspaceBusy && !session.active}
                >
                  <span className="workspace-session-title">{session.title}</span>
                  <span className="workspace-session-time">{formatRelativeTime(session.updated_at)}</span>
                </button>
                <button
                  className="workspace-session-delete-button"
                  type="button"
                  onClick={() => void deleteSession(session)}
                  disabled={isWorkspaceBusy || deletingSessionId === session.session_id}
                  aria-label="删除对话"
                  title="删除对话"
                >
                  <svg viewBox="0 0 16 16" aria-hidden="true">
                    <path d="M3 4.5h10" />
                    <path d="M6.3 4.5V3h3.4v1.5" />
                    <path d="M5 6.3V13h6V6.3" />
                  </svg>
                </button>
              </div>
            ))}
          </div>
        ) : (
          <div className="workspace-sidebar-empty">暂无历史对话</div>
        )}
      </div>
    </aside>
  );
}
