export type ProjectState = {
  projectId: string;
  title: string;
  activeSessionId: string;
  isBusy: boolean;
  runningRoundId: string | null;
};
