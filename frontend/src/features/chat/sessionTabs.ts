import type { SessionSummary, SessionTab } from '../../types';
import { formatTimestamp } from './chatEventTransforms';

function buildSessionTitle(session: SessionSummary): string {
  const normalized = (session.title || session.first_user_text || '').replace(/\s+/g, ' ').trim();
  if (!normalized) {
    return '未命名会话';
  }
  const firstSentence = normalized.split(/[。！？.!?\n]/)[0]?.trim() || normalized;
  return firstSentence.slice(0, 18);
}

export function buildSessionTabs(
  sessions: SessionSummary[],
  selected_session_id: string | null,
): SessionTab[] {
  return sessions.map((session) => ({
    ...session,
    title: buildSessionTitle(session),
    subtitle: `更新于 ${formatTimestamp(session.updated_at)}`,
    active: session.session_id === selected_session_id,
  }));
}

export function upsertActiveSessionSummary(
  sessions: SessionSummary[],
  sessionId: string,
  firstUserText: string | null,
  roundId?: string,
): SessionSummary[] {
  const updatedAt = new Date().toISOString();
  let activeSession: SessionSummary | null = null;
  const inactiveSessions: SessionSummary[] = [];

  for (const session of sessions) {
    if (session.session_id === sessionId) {
      activeSession = {
        ...session,
        updated_at: updatedAt,
        event_count: Math.max(session.event_count, 1),
        last_round_id: roundId ?? session.last_round_id,
        first_user_text: session.first_user_text || firstUserText,
        title: session.title,
        is_active: true,
      };
      continue;
    }
    inactiveSessions.push({
      ...session,
      is_active: false,
    });
  }

  return [
    activeSession ?? {
      session_id: sessionId,
      updated_at: updatedAt,
      event_count: 1,
      last_round_id: roundId ?? null,
      first_user_text: firstUserText,
      title: null,
      is_active: true,
      context_usage: null,
    },
    ...inactiveSessions,
  ];
}
