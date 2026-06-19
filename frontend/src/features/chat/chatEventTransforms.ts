import type { ChatEvent, SessionEventRecord, ToolCallEvent } from '../../types';

const MAX_TOOL_DETAIL_CHARS = 12_000;

function findEventIndexFromEnd(
  events: ChatEvent[],
  predicate: (event: ChatEvent) => boolean,
): number {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (predicate(events[index])) {
      return index;
    }
  }
  return -1;
}

export function formatTimestamp(value?: string): string {
  if (!value) {
    return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }
  return new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

function timestampMs(value?: string): number {
  if (!value) {
    return Date.now();
  }
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : Date.now();
}

function formatToolDetail(payload: unknown): string {
  if (payload == null) {
    return '无详细结果';
  }
  const detail = JSON.stringify(payload, null, 2);
  if (detail.length <= MAX_TOOL_DETAIL_CHARS) {
    return detail;
  }
  return `${detail.slice(0, MAX_TOOL_DETAIL_CHARS)}\n\n... 已截断 ${detail.length - MAX_TOOL_DETAIL_CHARS} 个字符，仅用于前端展示。`;
}

function eventScope(value: unknown): ToolCallEvent['scope'] | undefined {
  return value === 'main' ? 'main' : undefined;
}

function mergeToolCallEvent(existing: ChatEvent, updated: ToolCallEvent): ToolCallEvent {
  if (existing.kind !== 'tool_call') {
    return updated;
  }
  return {
    ...existing,
    ...updated,
  };
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
        timestamp_ms: timestampMs(event.ts),
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
        timestamp_ms: timestampMs(event.ts),
        round_id: event.round_id,
        message_id: event.message_id,
        seq: event.seq,
      });
      continue;
    }

    if (event.type === 'context_summary' || event.type === 'context_pruned') {
      events.push({
        id: event.id,
        kind: 'context_status',
        timestamp: formatTimestamp(event.ts),
        timestamp_ms: timestampMs(event.ts),
        round_id: event.round_id,
        message_id: event.message_id,
        seq: event.seq,
        status: 'done',
        summary: event.type === 'context_summary' ? '上下文压缩已完成' : '上下文已裁剪',
      });
      continue;
    }

    if (event.type === 'agent_message') {
      continue;
    }

    if (event.type === 'tool_call') {
      const item: ToolCallEvent = {
        id: event.call_id || event.id,
        kind: 'tool_call',
        timestamp: formatTimestamp(event.ts),
        timestamp_ms: timestampMs(event.ts),
        round_id: event.round_id,
        message_id: event.message_id,
        seq: event.seq,
        title: String(event.payload.tool ?? 'tool_call'),
        tool: typeof event.payload.tool === 'string' ? event.payload.tool : undefined,
        scope: eventScope(event.scope),
        status: 'running',
        summary: typeof event.payload.tool === 'string' ? `开始执行 ${event.payload.tool}` : '开始执行',
        detail: formatToolDetail(event.payload.arguments),
      };
      toolIndex.set(item.id, events.length);
      events.push(item);
      continue;
    }

    if (event.type !== 'tool_result') {
      continue;
    }

    const toolId = event.call_id || event.id;
    const nextStatus = event.payload.status === 'failed' ? 'failed' : 'done';
    const updatedItem: ToolCallEvent = {
      id: toolId,
      kind: 'tool_call',
      timestamp: formatTimestamp(event.ts),
      timestamp_ms: timestampMs(event.ts),
      round_id: event.round_id,
      message_id: event.message_id,
      seq: event.seq,
      title: String(event.payload.tool ?? 'tool_call'),
      tool: typeof event.payload.tool === 'string' ? event.payload.tool : undefined,
      scope: eventScope(event.scope),
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
    events[existingIndex] = mergeToolCallEvent(events[existingIndex], updatedItem);
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
    timestamp_ms: Date.now(),
    round_id: roundId,
    message_id: typeof payload.message_id === 'string' ? payload.message_id : undefined,
    status,
    summary: typeof payload.summary === 'string' ? payload.summary : fallbackSummary,
  };
  const existingIndex = findEventIndexFromEnd(current, (item) => item.kind === 'context_status' && item.id === id);
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
  const detail = Object.prototype.hasOwnProperty.call(payload, 'result') ? formatToolDetail(payload.result) : undefined;
  const nextEvent: ToolCallEvent = {
    id: toolId,
    kind: 'tool_call',
    timestamp: formatTimestamp(),
    timestamp_ms: Date.now(),
    round_id: typeof payload.round_id === 'string' ? payload.round_id : undefined,
    message_id: typeof payload.message_id === 'string' ? payload.message_id : undefined,
    title: String(payload.tool ?? 'tool_call'),
    tool: typeof payload.tool === 'string' ? payload.tool : undefined,
    scope: eventScope(payload.scope),
    status,
    summary: typeof payload.summary === 'string' ? payload.summary : undefined,
    detail,
  };

  const existingIndex = findEventIndexFromEnd(current, (item) => item.kind === 'tool_call' && item.id === toolId);
  if (existingIndex === -1) {
    return [...current, nextEvent];
  }

  const next = [...current];
  next[existingIndex] = mergeToolCallEvent(next[existingIndex], nextEvent);
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
  const existingIndex = findEventIndexFromEnd(current, (item) => item.kind === 'message' && item.id === messageId);
  if (existingIndex === -1) {
    return [
      ...current,
      {
        id: messageId,
        kind: 'message',
        role: 'assistant',
        text: delta,
        timestamp,
        timestamp_ms: Date.now(),
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
        timestamp_ms: streamMessage.timestamp_ms ?? Date.now(),
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
      timestamp_ms: Date.now(),
      round_id: roundId,
      message_id: sourceMessageId,
    },
  ];
}
