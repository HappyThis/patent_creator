export type ChatMessageEvent = {
  id: string;
  kind: 'message';
  role: 'user' | 'assistant';
  text: string;
  timestamp: string;
  timestamp_ms?: number;
  round_id?: string;
  message_id?: string;
  seq?: number;
  is_placeholder?: boolean;
  is_streaming?: boolean;
  status?: 'interrupted' | 'failed';
  detail?: string;
};

type EventStatus = 'running' | 'done' | 'failed';
type EventScope = 'main';

export type ToolCallEvent = {
  id: string;
  kind: 'tool_call';
  timestamp?: string;
  timestamp_ms?: number;
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
  timestamp_ms?: number;
  round_id?: string;
  message_id?: string;
  seq?: number;
  status: 'done' | 'failed';
  summary: string;
  detail?: string;
};

export type ContextStatusEvent = {
  id: string;
  kind: 'context_status';
  timestamp?: string;
  timestamp_ms?: number;
  round_id?: string;
  message_id?: string;
  seq?: number;
  status: 'running' | 'done' | 'failed';
  summary: string;
  detail?: string;
};

export type QualityEnhancementStatusEvent = {
  id: string;
  kind: 'quality_enhancement_status';
  timestamp?: string;
  timestamp_ms?: number;
  round_id?: string;
  message_id?: string;
  seq?: number;
  status: 'running' | 'done' | 'failed';
  phase: 'assessing' | 'enhancing' | 'summarizing' | 'completed' | 'failed';
  progress: number;
  summary: string;
  detail?: string;
};

export type LLMRetryStatusEvent = {
  id: string;
  kind: 'llm_retry_status';
  timestamp?: string;
  timestamp_ms?: number;
  round_id?: string;
  message_id?: string;
  seq?: number;
  status: 'waiting' | 'retrying' | 'done' | 'failed';
  reason: string;
  attempt: number;
  max_attempts: number;
  retry_index: number;
  max_retries: number;
  retry_after_seconds: number;
  retry_at_ms?: number;
  detail?: string;
};

export type QualityMode = 'normal' | 'enhanced';

export type ChatEvent =
  | ChatMessageEvent
  | ToolCallEvent
  | RoundStatusEvent
  | ContextStatusEvent
  | QualityEnhancementStatusEvent
  | LLMRetryStatusEvent;

export type SessionEventRecord = {
  id: string;
  ts: string;
  type:
    | 'user_input'
    | 'agent_message'
    | 'agent_output'
    | 'tool_call'
    | 'tool_result'
    | 'context_summary'
    | 'context_pruned'
    | 'llm_audit'
    | 'llm_retry_status'
    | 'technical_solution_check_result'
    | 'technical_solution_check_feedback'
    | 'technical_solution_enhancement_status'
    | 'technical_solution_change_assessment'
    | 'technical_solution_improvement_advice'
    | 'technical_solution_enhancement_feedback'
    | 'technical_solution_enhancement_summary'
    | 'session_title';
  seq: number;
  scope: string;
  round_id: string;
  message_id: string;
  call_id?: string | null;
  payload: Record<string, unknown>;
};
