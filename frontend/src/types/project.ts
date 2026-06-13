export type ContextUsageSummary = {
  max_tokens: number;
  used_tokens: number;
  used_ratio: number;
  threshold_tokens: number;
  reserved_output_tokens: number;
  status: string;
};

export type ProjectState = {
  project_id: string;
  title: string;
  created_at: string;
  updated_at?: string | null;
  active_session_id: string | null;
  running_session_id: string | null;
  running_round_id: string | null;
  is_busy: boolean;
  active_session_context?: ContextUsageSummary | null;
};

export type SessionSummary = {
  session_id: string;
  updated_at: string;
  event_count: number;
  last_round_id: string | null;
  first_user_text: string | null;
  is_active: boolean;
  context_usage?: ContextUsageSummary | null;
};

export type SessionTab = SessionSummary & {
  title: string;
  subtitle?: string;
  active: boolean;
};

export type InnovationKernelState = {
  exists: boolean;
  kernel_markdown: string;
  updated_at: string | null;
  source: 'create' | 'recreate' | null;
};
