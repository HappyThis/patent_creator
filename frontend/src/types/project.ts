export type ProjectState = {
  projectId: string;
  title: string;
  activeSessionId: string;
  isBusy: boolean;
  runningRoundId: string | null;
};

export type SessionTab = {
  id: string;
  title: string;
  subtitle?: string;
  active: boolean;
};
