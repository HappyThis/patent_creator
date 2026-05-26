import type { ChatEvent, ProcessEvent, SessionEventRecord } from '../../types';

type ToolState = {
  id: string;
  kind: 'tool_call';
  timestamp?: string;
  round_id?: string;
  message_id?: string;
  seq?: number;
  parent_call_id?: string | null;
  title: string;
  tool?: string;
  scope?: ProcessEvent['scope'];
  status?: ProcessEvent['status'];
  summary?: string;
  detail?: string;
};

export function formatTimestamp(value?: string): string {
  if (!value) {
    return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }
  return new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

function formatToolDetail(payload: unknown): string {
  if (payload == null) {
    return '无详细结果';
  }
  return JSON.stringify(payload, null, 2);
}

export function hydrateEvents(rawEvents: SessionEventRecord[]): ChatEvent[] {
  const events: ChatEvent[] = [];
  const toolIndex = new Map<string, number>();

  for (const event of rawEvents) {
    if (event.type === 'user_input') {
      events.push({
        id: event.message_id,
        kind: 'message',
        role: 'user',
        text: String(event.payload.text ?? ''),
        timestamp: formatTimestamp(event.ts),
        round_id: event.round_id,
        message_id: event.message_id,
        seq: event.seq,
      });
      continue;
    }

    if (event.type === 'agent_output') {
      events.push({
        id: event.id,
        kind: 'message',
        role: 'assistant',
        text: String(event.payload.text ?? ''),
        timestamp: formatTimestamp(event.ts),
        round_id: event.round_id,
        message_id: event.message_id,
        seq: event.seq,
      });
      continue;
    }

    if (event.type === 'context_summary') {
      events.push({
        id: event.id,
        kind: 'context_status',
        timestamp: formatTimestamp(event.ts),
        round_id: event.round_id,
        message_id: event.message_id,
        seq: event.seq,
        status: 'done',
        summary: '上下文压缩已完成',
      });
      continue;
    }

    if (event.type === 'agent_message') {
      continue;
    }

    if (event.type === 'tool_call') {
      const item: ToolState = {
        id: event.call_id || event.id,
        kind: 'tool_call',
        timestamp: formatTimestamp(event.ts),
        round_id: event.round_id,
        message_id: event.message_id,
        seq: event.seq,
        parent_call_id: event.parent_call_id,
        title: String(event.payload.tool ?? 'tool_call'),
        tool: typeof event.payload.tool === 'string' ? event.payload.tool : undefined,
        scope: event.scope as ProcessEvent['scope'],
        status: 'running',
        summary: typeof event.payload.tool === 'string' ? `开始执行 ${event.payload.tool}` : '开始执行',
        detail: formatToolDetail(event.payload.arguments),
      };
      toolIndex.set(item.id, events.length);
      events.push(item);
      continue;
    }

    const toolId = event.call_id || event.id;
    const nextStatus =
      event.payload.status === 'failed'
        ? 'failed'
        : event.payload.status === 'success'
          ? 'done'
          : 'done';
    const updatedItem: ToolState = {
      id: toolId,
      kind: 'tool_call',
      timestamp: formatTimestamp(event.ts),
      round_id: event.round_id,
      message_id: event.message_id,
      seq: event.seq,
      parent_call_id: event.parent_call_id,
      title: String(event.payload.tool ?? 'tool_call'),
      tool: typeof event.payload.tool === 'string' ? event.payload.tool : undefined,
      scope: event.scope as ProcessEvent['scope'],
      status: nextStatus,
      summary: nextStatus === 'failed' ? '执行失败' : '执行完成',
      detail: formatToolDetail(event.payload.output ?? event.payload),
    };

    const existingIndex = toolIndex.get(toolId);
    if (existingIndex == null) {
      toolIndex.set(toolId, events.length);
      events.push(updatedItem);
      continue;
    }
    events[existingIndex] = {
      ...(events[existingIndex] as ToolState),
      ...updatedItem,
    };
  }

  return events;
}

export function applyContextCompressionEvent(
  current: ChatEvent[],
  payload: Record<string, unknown>,
  status: 'running' | 'done' | 'failed',
): ChatEvent[] {
  const roundId = typeof payload.round_id === 'string' ? payload.round_id : undefined;
  const id = `context_compression_${roundId ?? 'active'}`;
  const fallbackSummary =
    status === 'running' ? '上下文正在压缩' : status === 'done' ? '上下文压缩已完成' : '上下文压缩失败';
  const nextEvent: ChatEvent = {
    id,
    kind: 'context_status',
    timestamp: formatTimestamp(),
    round_id: roundId,
    message_id: typeof payload.message_id === 'string' ? payload.message_id : undefined,
    status,
    summary: typeof payload.summary === 'string' ? payload.summary : fallbackSummary,
  };
  const existingIndex = current.findIndex((item) => item.kind === 'context_status' && item.id === id);
  if (existingIndex === -1) {
    return [...current, nextEvent];
  }
  const next = [...current];
  next[existingIndex] = nextEvent;
  return next;
}

export function applyRunningToolEvent(
  current: ChatEvent[],
  payload: Record<string, unknown>,
  status: 'running' | 'done' | 'failed',
): ChatEvent[] {
  const toolId = String(payload.call_id ?? `call_${Date.now()}`);
  const detail = payload.result ? formatToolDetail(payload.result) : undefined;
  const nextEvent: ProcessEvent = {
    id: toolId,
    kind: 'tool_call',
    timestamp: formatTimestamp(),
    round_id: typeof payload.round_id === 'string' ? payload.round_id : undefined,
    message_id: typeof payload.message_id === 'string' ? payload.message_id : undefined,
    parent_call_id: typeof payload.parent_call_id === 'string' ? payload.parent_call_id : null,
    title: String(payload.tool ?? 'tool_call'),
    tool: typeof payload.tool === 'string' ? payload.tool : undefined,
    scope: payload.scope as ProcessEvent['scope'],
    status,
    summary: typeof payload.summary === 'string' ? payload.summary : undefined,
    detail,
  };

  const existingIndex = current.findIndex((item) => item.kind === 'tool_call' && item.id === toolId);
  if (existingIndex === -1) {
    return [...current, nextEvent];
  }

  const next = [...current];
  next[existingIndex] = {
    ...(next[existingIndex] as ProcessEvent),
    ...nextEvent,
  };
  return next;
}

export function applyAssistantDelta(
  current: ChatEvent[],
  messageId: string,
  delta: string,
  timestamp: string,
  roundId?: string,
  sourceMessageId?: string,
): ChatEvent[] {
  if (!delta) {
    return current;
  }
  const existingIndex = current.findIndex((item) => item.kind === 'message' && item.id === messageId);
  if (existingIndex === -1) {
    return [
      ...current,
      {
        id: messageId,
        kind: 'message',
        role: 'assistant',
        text: delta,
        timestamp,
        round_id: roundId,
        message_id: sourceMessageId,
      },
    ];
  }

  const next = [...current];
  const existing = next[existingIndex];
  if (existing.kind !== 'message') {
    return current;
  }
  next[existingIndex] = {
    ...existing,
    text: `${existing.text}${delta}`,
    round_id: existing.round_id ?? roundId,
    message_id: existing.message_id ?? sourceMessageId,
  };
  return next;
}

export function finalizeRoundEvents(
  current: ChatEvent[],
  reply: string,
  timestamp: string,
  roundId?: string,
  sourceMessageId?: string,
): ChatEvent[] {
  if (!reply) {
    return current;
  }

  const roundMatches = (item: ChatEvent) => (roundId ? item.round_id === roundId : true);

  let lastToolIndex = -1;
  let latestStreamIndexAfterTools = -1;
  for (let index = 0; index < current.length; index += 1) {
    const item = current[index];
    if (!roundMatches(item)) {
      continue;
    }
    if (item.kind === 'tool_call') {
      lastToolIndex = index;
      latestStreamIndexAfterTools = -1;
      continue;
    }
    if (
      item.kind === 'message' &&
      item.role === 'assistant' &&
      item.id.startsWith('assistant_stream_') &&
      index > lastToolIndex
    ) {
      latestStreamIndexAfterTools = index;
    }
  }

  if (latestStreamIndexAfterTools !== -1) {
    const next = [...current];
    const streamMessage = next[latestStreamIndexAfterTools];
    if (streamMessage.kind === 'message') {
      next[latestStreamIndexAfterTools] = {
        ...streamMessage,
        text: reply,
        timestamp: streamMessage.timestamp || timestamp,
        round_id: streamMessage.round_id ?? roundId,
        message_id: streamMessage.message_id ?? sourceMessageId,
      };
    }
    return next;
  }

  return [
    ...current,
    {
      id: `assistant_final_${Date.now()}`,
      kind: 'message',
      role: 'assistant',
      text: reply,
      timestamp,
      round_id: roundId,
      message_id: sourceMessageId,
    },
  ];
}
