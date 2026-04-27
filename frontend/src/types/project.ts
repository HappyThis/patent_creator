export type ProjectState = {
  project_id: string;
  title: string;
  created_at: string;
  updated_at?: string | null;
  active_session_id: string | null;
  running_session_id: string | null;
  running_round_id: string | null;
  is_busy: boolean;
};

export type SessionSummary = {
  session_id: string;
  updated_at: string;
  event_count: number;
  last_round_id: string | null;
  latest_user_text: string | null;
  is_active: boolean;
};

export type SessionTab = SessionSummary & {
  title: string;
  subtitle?: string;
  active: boolean;
};
