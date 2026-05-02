import { OutlineItem, ProjectState, RenderAst, SessionEventRecord, SessionSummary } from '../../types';
import { requestJson } from './http';

export type ApiClient = {
  listProjects: () => Promise<{ projects: ProjectState[] }>;
  getProject: (project_id: string) => Promise<ProjectState>;
  getOutline: (project_id: string) => Promise<{ sections: OutlineItem[] }>;
  getRenderAst: (
    project_id: string,
    focus?: { focus_section_id?: string | null; focus_block_id?: string | null },
  ) => Promise<{
    render_ast: RenderAst;
    active_section_id: string | null;
    active_block_id: string | null;
    updated_at: string;
  }>;
  listSessions: (project_id: string) => Promise<{ sessions: SessionSummary[] }>;
  getSessionEvents: (project_id: string, session_id: string) => Promise<{ events: SessionEventRecord[] }>;
  exportMarkdown: (project_id: string) => Promise<{ path: string }>;
  cancelRound: (
    project_id: string,
    session_id: string,
    round_id: string,
  ) => Promise<{
    cancelled: boolean;
    project_id: string;
    session_id: string;
    round_id: string;
    message_id: string;
    reply: string;
  }>;
};

export const apiClient: ApiClient = {
  async listProjects() {
    return requestJson<{ projects: ProjectState[] }>('/api/projects');
  },
  async getProject(project_id) {
    return requestJson<ProjectState>(`/api/projects/${project_id}`);
  },
  async getOutline(project_id) {
    return requestJson<{ sections: OutlineItem[] }>(`/api/projects/${project_id}/outline`);
  },
  async getRenderAst(project_id, focus) {
    const search = new URLSearchParams();
    if (focus?.focus_section_id) {
      search.set('focus_section_id', focus.focus_section_id);
    }
    if (focus?.focus_block_id) {
      search.set('focus_block_id', focus.focus_block_id);
    }
    const query = search.toString();
    return requestJson<{
      render_ast: RenderAst;
      active_section_id: string | null;
      active_block_id: string | null;
      updated_at: string;
    }>(`/api/projects/${project_id}/render${query ? `?${query}` : ''}`);
  },
  async listSessions(project_id) {
    return requestJson<{ sessions: SessionSummary[] }>(`/api/projects/${project_id}/sessions`);
  },
  async getSessionEvents(project_id, session_id) {
    return requestJson<{ events: SessionEventRecord[] }>(`/api/projects/${project_id}/sessions/${session_id}/events`);
  },
  async exportMarkdown(project_id) {
    return requestJson<{ path: string }>(`/api/projects/${project_id}/export/markdown`, {
      method: 'POST',
    });
  },
  async cancelRound(project_id, session_id, round_id) {
    return requestJson<{
      cancelled: boolean;
      project_id: string;
      session_id: string;
      round_id: string;
      message_id: string;
      reply: string;
    }>(`/api/projects/${project_id}/sessions/${session_id}/rounds/${round_id}/cancel`, {
      method: 'POST',
    });
  },
};
