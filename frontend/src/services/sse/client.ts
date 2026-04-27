export type SseClient = {
  connect: (project_id: string, session_id: string) => EventSource;
};

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000';

export const sseClient: SseClient = {
  connect(project_id, session_id) {
    const url = new URL(`${API_BASE_URL}/api/projects/${project_id}/chat/stream`);
    url.searchParams.set('session_id', session_id);
    return new EventSource(url);
  },
};
