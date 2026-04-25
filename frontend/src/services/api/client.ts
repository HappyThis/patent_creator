export type ApiClient = {
  getProject: (projectId: string) => Promise<unknown>;
  getOutline: (projectId: string) => Promise<unknown>;
  getRenderAst: (projectId: string) => Promise<unknown>;
  sendChatMessage: (projectId: string, payload: unknown) => Promise<unknown>;
};

export const apiClient: ApiClient = {
  async getProject() {
    throw new Error('API client is not connected yet.');
  },
  async getOutline() {
    throw new Error('API client is not connected yet.');
  },
  async getRenderAst() {
    throw new Error('API client is not connected yet.');
  },
  async sendChatMessage() {
    throw new Error('API client is not connected yet.');
  },
};
