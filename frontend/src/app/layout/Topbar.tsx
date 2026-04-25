import { ProjectState } from '../../types';

type TopbarProps = {
  project: ProjectState;
};

export function Topbar({ project }: TopbarProps) {
  return (
    <header className="topbar">
      <div>
        <div className="eyebrow">Patent Creator / Frontend Prototype</div>
        <h1>{project.title}</h1>
      </div>
      <div className="project-meta">
        <div>
          <span>Project</span>
          <strong>{project.projectId}</strong>
        </div>
        <div>
          <span>Active Session</span>
          <strong>{project.activeSessionId}</strong>
        </div>
        <div>
          <span>Round</span>
          <strong>{project.runningRoundId ?? 'idle'}</strong>
        </div>
      </div>
    </header>
  );
}
