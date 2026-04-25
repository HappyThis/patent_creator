export type SseClient = {
  connect: (projectId: string, sessionId: string) => EventSource | null;
};

export const sseClient: SseClient = {
  connect() {
    return null;
  },
};
