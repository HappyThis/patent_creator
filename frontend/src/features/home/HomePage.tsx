import { useState } from 'react';
import type { FormEvent } from 'react';
import type { ProjectState } from '../../types';

type HomePageProps = {
  projects: ProjectState[];
  isLoading: boolean;
  error: string | null;
  onSelectProject: (projectId: string) => void;
  onCreateProject: (payload: { project_name: string; disclosure_title?: string | null }) => Promise<void>;
  onDeleteProject: (projectId: string) => Promise<void>;
  onRenameProject: (projectId: string, projectName: string) => Promise<void>;
};

function formatProjectTime(value?: string | null): string {
  if (!value) {
    return '未更新';
  }
  return new Date(value).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function HomePage({
  projects,
  isLoading,
  error,
  onSelectProject,
  onCreateProject,
  onDeleteProject,
  onRenameProject,
}: HomePageProps) {
  const [isCreateFormOpen, setIsCreateFormOpen] = useState(false);
  const [isCreating, setIsCreating] = useState(false);
  const [projectName, setProjectName] = useState('');
  const [disclosureTitle, setDisclosureTitle] = useState('');
  const [formError, setFormError] = useState<string | null>(null);
  const [deletingProjectId, setDeletingProjectId] = useState<string | null>(null);
  const [renamingProjectId, setRenamingProjectId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [isRenaming, setIsRenaming] = useState(false);

  const handleCreateSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmedProjectName = projectName.trim();
    const trimmedDisclosureTitle = disclosureTitle.trim();
    if (!trimmedProjectName) {
      setFormError('请先填写项目名。');
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
      setIsCreateFormOpen(false);
    } catch (createError: unknown) {
      setFormError(createError instanceof Error ? createError.message : '创建项目失败。');
    } finally {
      setIsCreating(false);
    }
  };

  const handleDeleteProject = async (project: ProjectState) => {
    if (project.is_busy) {
      return;
    }
    const confirmed = window.confirm(`确定删除项目「${project.title}」吗？该项目的会话和交底书草稿也会一并删除。`);
    if (!confirmed) {
      return;
    }

    setFormError(null);
    setDeletingProjectId(project.project_id);
    try {
      await onDeleteProject(project.project_id);
    } catch (deleteError: unknown) {
      setFormError(deleteError instanceof Error ? deleteError.message : '删除项目失败。');
    } finally {
      setDeletingProjectId(null);
    }
  };

  const beginRenameProject = (project: ProjectState) => {
    if (project.is_busy) {
      return;
    }
    setFormError(null);
    setRenamingProjectId(project.project_id);
    setRenameValue(project.title);
  };

  const cancelRenameProject = () => {
    setRenamingProjectId(null);
    setRenameValue('');
  };

  const submitRenameProject = async (project: ProjectState) => {
    const trimmedProjectName = renameValue.trim();
    if (!trimmedProjectName) {
      setFormError('项目名不能为空。');
      return;
    }
    if (trimmedProjectName === project.title) {
      cancelRenameProject();
      return;
    }

    setFormError(null);
    setIsRenaming(true);
    try {
      await onRenameProject(project.project_id, trimmedProjectName);
      cancelRenameProject();
    } catch (renameError: unknown) {
      setFormError(renameError instanceof Error ? renameError.message : '修改项目名失败。');
    } finally {
      setIsRenaming(false);
    }
  };

  return (
    <main className="home-page">
      <section className="home-projects" aria-labelledby="home-title">
        <div className="home-heading">
          <div>
            <h1 id="home-title">选择项目</h1>
            <p className="home-kicker">{projects.length} 个项目</p>
          </div>
        </div>

        {error ? <div className="home-error">{error}</div> : null}
        {formError ? <div className="home-error">{formError}</div> : null}
        {isLoading ? <div className="home-empty">正在加载项目...</div> : null}
        {!isLoading && projects.length === 0 && !error ? <div className="home-empty">暂无可用项目</div> : null}

        {projects.length > 0 ? (
          <div className="project-list">
            {projects.map((project) => (
              <div
                key={project.project_id}
                className={`project-list-item ${project.is_busy ? 'busy' : ''}`}
              >
              {renamingProjectId === project.project_id ? (
                <div className="project-rename-panel">
                  <label className="project-rename-field">
                    <span>项目名</span>
                    <input
                      value={renameValue}
                      onChange={(event) => setRenameValue(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') {
                          event.preventDefault();
                          void submitRenameProject(project);
                        }
                        if (event.key === 'Escape') {
                          cancelRenameProject();
                        }
                      }}
                      disabled={isRenaming}
                      autoFocus
                    />
                  </label>
                  <div className="project-rename-actions">
                    <button
                      className="project-save-button"
                      type="button"
                      onClick={() => void submitRenameProject(project)}
                      disabled={isRenaming}
                    >
                      {isRenaming ? '保存中' : '保存'}
                    </button>
                    <button className="project-cancel-button" type="button" onClick={cancelRenameProject} disabled={isRenaming}>
                      取消
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  <button className="project-open-button" type="button" onClick={() => onSelectProject(project.project_id)}>
                    <span className="project-copy">
                      <span className="project-title">{project.title}</span>
                      <span className="project-meta">
                        {project.is_busy ? <span className="project-status busy">运行中</span> : null}
                        <span>更新于 {formatProjectTime(project.updated_at ?? project.created_at)}</span>
                      </span>
                    </span>
                  </button>
                  <div className="project-item-actions">
                    <button
                      className="project-rename-button"
                      type="button"
                      onClick={() => beginRenameProject(project)}
                      disabled={project.is_busy || deletingProjectId === project.project_id}
                    >
                      重命名
                    </button>
                    <button
                      className="project-delete-button"
                      type="button"
                      onClick={() => void handleDeleteProject(project)}
                      disabled={project.is_busy || deletingProjectId === project.project_id}
                      aria-label={`删除项目 ${project.title}`}
                    >
                      {deletingProjectId === project.project_id ? '删除中' : '删除'}
                    </button>
                  </div>
                </>
              )}
              </div>
            ))}
          </div>
        ) : null}

        {isCreateFormOpen ? (
          <form className="project-create-form" onSubmit={handleCreateSubmit}>
            <label className="project-create-field">
              <span>项目名</span>
              <input
                value={projectName}
                onChange={(event) => setProjectName(event.target.value)}
                placeholder="例如：A2UI 消息平台动态交互"
                disabled={isCreating}
                autoFocus
              />
            </label>
            <label className="project-create-field">
              <span>交底书名称</span>
              <input
                value={disclosureTitle}
                onChange={(event) => setDisclosureTitle(event.target.value)}
                placeholder="可选；不填则先创建未命名交底书"
                disabled={isCreating}
              />
            </label>
            <div className="project-create-actions">
              <button className="project-create-button" type="submit" disabled={isCreating}>
                {isCreating ? '创建中...' : '确认创建'}
              </button>
              <button
                className="project-create-cancel"
                type="button"
                onClick={() => {
                  setProjectName('');
                  setDisclosureTitle('');
                  setIsCreateFormOpen(false);
                }}
                disabled={isCreating}
              >
                取消
              </button>
            </div>
            <p className="project-create-note">项目名用于首页管理；交底书名称是文档标题，两者可以不同。</p>
          </form>
        ) : (
          <div className="home-bottom-actions">
            <button
              className="home-create-toggle"
              type="button"
              onClick={() => {
                setFormError(null);
                setIsCreateFormOpen(true);
              }}
              aria-expanded={isCreateFormOpen}
              disabled={isCreating}
            >
              创建项目
            </button>
          </div>
        )}
      </section>
    </main>
  );
}
