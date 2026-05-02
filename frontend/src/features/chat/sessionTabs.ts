import type { SessionSummary, SessionTab } from '../../types';
import { formatTimestamp } from './chatEventTransforms';

function buildSessionTitle(firstUserText?: string | null): string {
  const normalized = (firstUserText ?? '').replace(/\s+/g, ' ').trim();
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
    title: buildSessionTitle(session.first_user_text),
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
  const existing = sessions.find((session) => session.session_id === sessionId);
  const activeSession: SessionSummary = existing
    ? {
        ...existing,
        updated_at: updatedAt,
        event_count: Math.max(existing.event_count, 1),
        last_round_id: roundId ?? existing.last_round_id,
        first_user_text: existing.first_user_text || firstUserText,
        is_active: true,
      }
    : {
        session_id: sessionId,
        updated_at: updatedAt,
        event_count: 1,
        last_round_id: roundId ?? null,
        first_user_text: firstUserText,
        is_active: true,
        context_usage: null,
      };

  const inactiveSessions = sessions
    .filter((session) => session.session_id !== sessionId)
    .map((session) => ({
      ...session,
      is_active: false,
    }));

  return [activeSession, ...inactiveSessions];
}
