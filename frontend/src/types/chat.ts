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

export type ToolCallEvent = {
  id: string;
  kind: 'tool_call';
  timestamp?: string;
  round_id?: string;
  message_id?: string;
  seq?: number;
  parent_call_id?: string | null;
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

export type ProcessEvent = ToolCallEvent;
export type ChatEvent = ChatMessageEvent | ProcessEvent | RoundStatusEvent;

export type SessionEventRecord = {
  id: string;
  ts: string;
  type: 'user_input' | 'agent_output' | 'tool_call' | 'tool_result' | 'context_summary' | 'context_pruned';
  seq: number;
  scope: string;
  round_id: string;
  message_id: string;
  call_id?: string | null;
  parent_call_id?: string | null;
  payload: Record<string, unknown>;
};
