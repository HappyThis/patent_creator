import type { ProjectState } from '../../types';

type HomePageProps = {
  projects: ProjectState[];
  isLoading: boolean;
  error: string | null;
  onRetry: () => void;
  onSelectProject: (projectId: string) => void;
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
  onRetry,
  onSelectProject,
}: HomePageProps) {
  return (
    <main className="home-page">
      <section className="home-projects" aria-labelledby="home-title">
        <div className="home-heading">
          <div>
            <p className="home-kicker">Patent Creator</p>
            <h1 id="home-title">选择项目</h1>
          </div>
          <button className="home-refresh-button" type="button" onClick={onRetry} disabled={isLoading}>
            刷新
          </button>
        </div>

        {error ? <div className="home-error">{error}</div> : null}
        {isLoading ? <div className="home-empty">正在加载项目...</div> : null}
        {!isLoading && projects.length === 0 && !error ? <div className="home-empty">暂无可用项目</div> : null}

        <div className="project-list">
          {projects.map((project) => (
            <button
              key={project.project_id}
              className="project-list-item"
              type="button"
              onClick={() => onSelectProject(project.project_id)}
            >
              <span className="project-title">{project.title}</span>
              <span className="project-meta">
                {project.is_busy ? '运行中' : '就绪'} · 更新于 {formatProjectTime(project.updated_at ?? project.created_at)}
              </span>
            </button>
          ))}
        </div>
      </section>
    </main>
  );
}
