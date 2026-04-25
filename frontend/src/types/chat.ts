export type ChatMessage = {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  timestamp: string;
};

export type TimelineItem = {
  id: string;
  kind: 'agent_output' | 'tool_call';
  status?: 'running' | 'done' | 'failed';
  scope?: 'main' | `subagent:${string}`;
  tool?: string;
  title: string;
  summary?: string;
  detail?: string;
};
