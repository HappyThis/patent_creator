import { InnovationKernelState, ProjectState, RenderAst, SessionEventRecord, SessionSummary } from '../../types';
import { requestJson } from './http';

export type ApiClient = {
  listProjects: () => Promise<{ projects: ProjectState[] }>;
  createProject: (payload: { project_name: string; disclosure_title?: string | null }) => Promise<ProjectState>;
  renameProject: (project_id: string, payload: { project_name: string }) => Promise<ProjectState>;
  deleteProject: (project_id: string) => Promise<{
    deleted: boolean;
    project_id: string;
    next_project_id: string | null;
  }>;
  getProject: (project_id: string) => Promise<ProjectState>;
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
  getInnovationKernel: (project_id: string, session_id: string) => Promise<InnovationKernelState>;
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
  async createProject(payload) {
    return requestJson<ProjectState>('/api/projects', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },
  async renameProject(project_id, payload) {
    return requestJson<ProjectState>(`/api/projects/${project_id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    });
  },
  async deleteProject(project_id) {
    return requestJson<{
      deleted: boolean;
      project_id: string;
      next_project_id: string | null;
    }>(`/api/projects/${project_id}`, {
      method: 'DELETE',
    });
  },
  async getProject(project_id) {
    return requestJson<ProjectState>(`/api/projects/${project_id}`);
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
  async getInnovationKernel(project_id, session_id) {
    return requestJson<InnovationKernelState>(
      `/api/projects/${project_id}/sessions/${session_id}/innovation-kernel`,
    );
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
