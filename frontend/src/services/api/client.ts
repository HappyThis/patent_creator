import { OutlineItem, ProjectState, RenderAst, SessionSummary } from '../../types';

export type ApiClient = {
  createProject: (title: string) => Promise<ProjectState>;
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
  getSessionEvents: (project_id: string, session_id: string) => Promise<{ events: unknown[] }>;
};

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000';

type ApiErrorResponse = {
  error?: {
    code?: string;
    message?: string;
  };
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
  });

  if (!response.ok) {
    let message = `Request failed with status ${response.status}`;
    try {
      const payload = (await response.json()) as ApiErrorResponse;
      if (payload.error?.message) {
        message = payload.error.message;
      }
    } catch {
      // ignore json parsing failure
    }
    throw new Error(message);
  }

  return (await response.json()) as T;
}

export const apiClient: ApiClient = {
  async createProject(title) {
    return request<ProjectState>('/api/projects', {
      method: 'POST',
      body: JSON.stringify({ title }),
    });
  },
  async getProject(project_id) {
    return request<ProjectState>(`/api/projects/${project_id}`);
  },
  async getOutline(project_id) {
    return request<{ sections: OutlineItem[] }>(`/api/projects/${project_id}/outline`);
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
    return request<{
      render_ast: RenderAst;
      active_section_id: string | null;
      active_block_id: string | null;
      updated_at: string;
    }>(`/api/projects/${project_id}/render${query ? `?${query}` : ''}`);
  },
  async listSessions(project_id) {
    return request<{ sessions: SessionSummary[] }>(`/api/projects/${project_id}/sessions`);
  },
  async getSessionEvents(project_id, session_id) {
    return request<{ events: unknown[] }>(`/api/projects/${project_id}/sessions/${session_id}/events`);
  },
};
