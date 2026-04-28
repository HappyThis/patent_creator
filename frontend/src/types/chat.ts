export type ChatMessageEvent = {
  id: string;
  kind: 'message';
  role: 'user' | 'assistant';
  text: string;
  timestamp: string;
  round_id?: string;
  message_id?: string;
  seq?: number;
};

type EventStatus = 'running' | 'done' | 'failed';
type EventScope = 'main' | `subagent:${string}`;

export type AgentOutputEvent = {
  id: string;
  kind: 'agent_output';
  timestamp?: string;
  round_id?: string;
  message_id?: string;
  seq?: number;
  status?: EventStatus;
  scope?: EventScope;
  tool?: string;
  title: string;
  summary?: string;
  detail?: string;
};

export type ToolCallEvent = {
  id: string;
  kind: 'tool_call';
  timestamp?: string;
  round_id?: string;
  message_id?: string;
  seq?: number;
  status?: EventStatus;
  scope?: EventScope;
  tool?: string;
  title: string;
  summary?: string;
  detail?: string;
};

export type RoundStatusEvent = {
  id: string;
  kind: 'round_status';
  timestamp?: string;
  round_id?: string;
  message_id?: string;
  seq?: number;
  status: 'done' | 'failed';
  summary: string;
  detail?: string;
};

export type ProcessEvent = AgentOutputEvent | ToolCallEvent;
export type ChatEvent = ChatMessageEvent | ProcessEvent | RoundStatusEvent;
