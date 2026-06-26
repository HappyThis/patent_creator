import type {
  ChatEvent,
  ChatMessageEvent,
  LLMRetryStatusEvent,
  QualityEnhancementStatusEvent,
  SessionEventRecord,
  ToolCallEvent,
} from '../../types';

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

function eventStatus(value: unknown): 'running' | 'done' | 'failed' {
  return value === 'done' || value === 'failed' ? value : 'running';
}

function qualityEnhancementPhase(value: unknown): QualityEnhancementStatusEvent['phase'] {
  if (
    value === 'assessing' ||
    value === 'enhancing' ||
    value === 'summarizing' ||
    value === 'completed' ||
    value === 'failed'
  ) {
    return value;
  }
  return 'enhancing';
}

function boundedProgress(value: unknown): number {
  const numeric = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(numeric)) {
    return 0;
  }
  return Math.max(0, Math.min(100, Math.round(numeric)));
}

function boundedPositiveInteger(value: unknown, fallback: number): number {
  const numeric = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(numeric)) {
    return fallback;
  }
  return Math.max(1, Math.round(numeric));
}

function retryStatus(value: unknown): LLMRetryStatusEvent['status'] {
  if (value === 'retrying' || value === 'done' || value === 'failed') {
    return value;
  }
  return 'waiting';
}

function retryErrorDetail(value: unknown): string | undefined {
  if (typeof value !== 'string') {
    return undefined;
  }
  const trimmed = value.trim();
  return trimmed ? `错误原因：${trimmed}` : undefined;
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
      const status =
        event.payload.status === 'interrupted' || event.payload.status === 'failed'
          ? event.payload.status
          : undefined;
      const detail =
        status === 'interrupted'
          ? typeof event.payload.detail === 'string'
            ? event.payload.detail
            : '输出中断，已保留当前内容。'
          : status === 'failed'
            ? typeof event.payload.detail === 'string'
              ? event.payload.detail
              : typeof event.payload.message === 'string'
                ? event.payload.message
                : '本轮未完成。'
            : undefined;
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
        status,
        detail,
      });
      if (status) {
        completeLLMRetryStatusInPlace(
          events,
          event.round_id,
          status === 'failed' || status === 'interrupted' ? 'failed' : 'done',
          detail,
        );
      } else {
        completeLLMRetryStatusInPlace(events, event.round_id, 'done');
      }
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

    if (event.type === 'technical_solution_enhancement_status') {
      const nextEvent: QualityEnhancementStatusEvent = {
        id: `quality_enhancement_${event.round_id || event.id}`,
        kind: 'quality_enhancement_status',
        timestamp: formatTimestamp(event.ts),
        timestamp_ms: timestampMs(event.ts),
        round_id: event.round_id,
        message_id: event.message_id,
        seq: event.seq,
        status: eventStatus(event.payload.status),
        phase: qualityEnhancementPhase(event.payload.phase),
        progress: boundedProgress(event.payload.progress),
        summary: typeof event.payload.summary === 'string' ? event.payload.summary : '增强模式：正在处理...',
        detail: typeof event.payload.detail === 'string' ? event.payload.detail : undefined,
      };
      const existingIndex = findEventIndexFromEnd(
        events,
        (existingEvent) =>
          existingEvent.kind === 'quality_enhancement_status' && existingEvent.id === nextEvent.id,
      );
      if (existingIndex === -1) {
        events.push(nextEvent);
      } else {
        events[existingIndex] = nextEvent;
      }
      continue;
    }

    if (
      event.type === 'technical_solution_check_result' ||
      event.type === 'technical_solution_check_feedback' ||
      event.type === 'technical_solution_change_assessment' ||
      event.type === 'technical_solution_improvement_advice' ||
      event.type === 'technical_solution_enhancement_feedback' ||
      event.type === 'technical_solution_enhancement_summary'
    ) {
      continue;
    }

    if (event.type === 'llm_audit') {
      const webSearchEvent = webSearchEventFromPayload(event.payload, {
        id: event.id,
        timestamp: formatTimestamp(event.ts),
        timestamp_ms: timestampMs(event.ts),
        round_id: event.round_id,
        message_id: event.message_id,
        seq: event.seq,
      });
      if (webSearchEvent) {
        events.push(webSearchEvent);
      }
      continue;
    }

    if (event.type === 'llm_retry_status') {
      const nextEvent = llmRetryEventFromPayload(event.payload, {
        timestamp: formatTimestamp(event.ts),
        timestamp_ms: timestampMs(event.ts),
        round_id: event.round_id,
        message_id: event.message_id,
        seq: event.seq,
      });
      const existingIndex = findEventIndexFromEnd(
        events,
        (existingEvent) => existingEvent.kind === 'llm_retry_status' && existingEvent.id === nextEvent.id,
      );
      if (existingIndex === -1) {
        events.push(nextEvent);
      } else {
        events[existingIndex] = nextEvent;
      }
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

export function applyRoundStartedEvent(
  current: ChatEvent[],
  roundId?: string,
  sourceMessageId?: string,
): ChatEvent[] {
  if (!roundId) {
    return current;
  }
  const id = `assistant_stream_${roundId}`;
  const existingIndex = findEventIndexFromEnd(current, (item) => item.kind === 'message' && item.id === id);
  const nextEvent: ChatEvent = {
    id,
    kind: 'message',
    role: 'assistant',
    text: '正在思考...',
    timestamp: formatTimestamp(),
    timestamp_ms: Date.now(),
    round_id: roundId,
    message_id: sourceMessageId,
    is_placeholder: true,
    is_streaming: true,
  };
  if (existingIndex === -1) {
    return [...current, nextEvent];
  }
  const next = [...current];
  next[existingIndex] = nextEvent;
  return next;
}

export function applyWebSearchProgressEvent(
  current: ChatEvent[],
  payload: Record<string, unknown>,
): ChatEvent[] {
  const event = webSearchEventFromPayload(payload, {
    id: webSearchEventId(payload),
    timestamp: formatTimestamp(),
    timestamp_ms: Date.now(),
    round_id: typeof payload.round_id === 'string' ? payload.round_id : undefined,
    message_id: typeof payload.message_id === 'string' ? payload.message_id : undefined,
  });
  if (!event) {
    return current;
  }

  const existingIndex = findEventIndexFromEnd(current, (item) => item.kind === 'tool_call' && item.id === event.id);
  if (existingIndex === -1) {
    return [...current, event];
  }
  const next = [...current];
  next[existingIndex] = event;
  return next;
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

export function applyQualityEnhancementStatusEvent(
  current: ChatEvent[],
  payload: Record<string, unknown>,
): ChatEvent[] {
  const roundId = typeof payload.round_id === 'string' ? payload.round_id : undefined;
  const phase = qualityEnhancementPhase(payload.phase);
  const id = `quality_enhancement_${roundId ?? 'active'}`;
  const fallbackSummary =
    phase === 'assessing'
      ? '增强模式：正在评估本轮修改...'
      : phase === 'enhancing'
        ? '增强模式：正在完善技术方案...'
        : phase === 'summarizing'
          ? '增强模式：正在整理增强记录...'
          : phase === 'failed'
            ? '增强模式：技术方案增强未完成'
            : '增强模式：已完成';
  const nextEvent: QualityEnhancementStatusEvent = {
    id,
    kind: 'quality_enhancement_status',
    timestamp: formatTimestamp(),
    timestamp_ms: Date.now(),
    round_id: roundId,
    message_id: typeof payload.message_id === 'string' ? payload.message_id : undefined,
    status: eventStatus(payload.status),
    phase,
    progress: boundedProgress(payload.progress),
    summary: typeof payload.summary === 'string' ? payload.summary : fallbackSummary,
    detail: typeof payload.detail === 'string' ? payload.detail : undefined,
  };
  const existingIndex = findEventIndexFromEnd(
    current,
    (item) => item.kind === 'quality_enhancement_status' && item.id === id,
  );
  if (existingIndex === -1) {
    return [...current, nextEvent];
  }
  const next = [...current];
  next[existingIndex] = nextEvent;
  return next;
}

export function applyLLMRetryStatusEvent(
  current: ChatEvent[],
  payload: Record<string, unknown>,
): ChatEvent[] {
  const roundId = typeof payload.round_id === 'string' ? payload.round_id : undefined;
  const nextEvent = llmRetryEventFromPayload(payload, {
    timestamp: formatTimestamp(),
    timestamp_ms: Date.now(),
    round_id: roundId,
    message_id: typeof payload.message_id === 'string' ? payload.message_id : undefined,
  });
  const withoutPlaceholder = current.filter(
    (item) =>
      !(
        item.kind === 'message' &&
        item.role === 'assistant' &&
        item.is_placeholder &&
        (!roundId || item.round_id === roundId)
      ),
  );
  const existingIndex = findEventIndexFromEnd(
    withoutPlaceholder,
    (item) => item.kind === 'llm_retry_status' && item.id === nextEvent.id,
  );
  if (existingIndex === -1) {
    return [...withoutPlaceholder, nextEvent];
  }
  const next = [...withoutPlaceholder];
  next[existingIndex] = nextEvent;
  return next;
}

function completeLLMRetryStatusInPlace(
  events: ChatEvent[],
  roundId: string | undefined,
  status: 'done' | 'failed',
  detail?: string,
) {
  const existingIndex = findEventIndexFromEnd(
    events,
    (item) =>
      item.kind === 'llm_retry_status' &&
      item.round_id === roundId &&
      (item.status === 'waiting' || item.status === 'retrying'),
  );
  if (existingIndex === -1) {
    return;
  }
  const existing = events[existingIndex];
  if (existing.kind !== 'llm_retry_status') {
    return;
  }
  events[existingIndex] = {
    ...existing,
    status,
    retry_after_seconds: 0,
    retry_at_ms: undefined,
    detail: detail ?? existing.detail,
  };
}

function llmRetryEventFromPayload(
  payload: Record<string, unknown>,
  meta: {
    timestamp: string;
    timestamp_ms: number;
    round_id?: string;
    message_id?: string;
    seq?: number;
  },
): LLMRetryStatusEvent {
  const status = retryStatus(payload.status);
  const attempt = boundedPositiveInteger(payload.attempt, 2);
  const maxAttempts = boundedPositiveInteger(payload.max_attempts, 2);
  const retryIndex = boundedPositiveInteger(payload.retry_index, Math.max(1, attempt - 1));
  const maxRetries = boundedPositiveInteger(payload.max_retries, Math.max(1, maxAttempts - 1));
  const retryAfterSeconds =
    typeof payload.retry_after_seconds === 'number'
      ? Math.max(0, payload.retry_after_seconds)
      : Math.max(0, Number(payload.retry_after_seconds) || 0);
  const retryAtMs =
    typeof payload.retry_at_ms === 'number' && Number.isFinite(payload.retry_at_ms)
      ? payload.retry_at_ms
      : status === 'waiting'
        ? meta.timestamp_ms + retryAfterSeconds * 1000
        : undefined;
  return {
    id: `llm_retry_${meta.round_id ?? 'active'}`,
    kind: 'llm_retry_status',
    timestamp: meta.timestamp,
    timestamp_ms: meta.timestamp_ms,
    round_id: meta.round_id,
    message_id: meta.message_id,
    seq: meta.seq,
    status,
    reason: typeof payload.reason === 'string' ? payload.reason : '模型连接失败',
    attempt,
    max_attempts: maxAttempts,
    retry_index: retryIndex,
    max_retries: maxRetries,
    retry_after_seconds: retryAfterSeconds,
    retry_at_ms: retryAtMs,
    detail: retryErrorDetail(payload.error_message),
  };
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
        is_streaming: true,
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
    text: existing.is_placeholder ? delta : `${existing.text}${delta}`,
    round_id: existing.round_id ?? roundId,
    message_id: existing.message_id ?? sourceMessageId,
    is_placeholder: false,
    is_streaming: true,
  };
  return next;
}

export function completeStreamingRoundEvents(
  current: ChatEvent[],
  roundId?: string,
  sourceMessageId?: string,
): ChatEvent[] {
  return current.map((item) => {
    if (
      item.kind !== 'message' ||
      item.role !== 'assistant' ||
      !item.is_streaming ||
      (roundId && item.round_id !== roundId)
    ) {
      return item;
    }
    return {
      ...item,
      round_id: item.round_id ?? roundId,
      message_id: item.message_id ?? sourceMessageId,
      is_streaming: false,
    };
  });
}

export function finalizeRoundEvents(
  current: ChatEvent[],
  reply: string,
  timestamp: string,
  roundId?: string,
  sourceMessageId?: string,
  replyStatus?: ChatMessageEvent['status'],
  replyDetail?: string,
): ChatEvent[] {
  if (!reply) {
    return current;
  }

  const roundMatches = (item: ChatEvent) => (roundId ? item.round_id === roundId : true);

  let lastToolIndex = -1;
  let latestStreamIndexAfterTools = -1;
  let latestStreamIndex = -1;
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
      item.id.startsWith('assistant_stream_')
    ) {
      latestStreamIndex = index;
      if (index > lastToolIndex) {
        latestStreamIndexAfterTools = index;
      }
    }
  }

  const streamIndex = latestStreamIndexAfterTools !== -1 ? latestStreamIndexAfterTools : latestStreamIndex;
  if (streamIndex !== -1) {
    const next = completeStreamingRoundEvents(current, roundId, sourceMessageId);
    const streamMessage = next[streamIndex];
    if (streamMessage.kind === 'message') {
      next[streamIndex] = {
        ...streamMessage,
        text: reply,
        timestamp,
        timestamp_ms: Date.now(),
        round_id: streamMessage.round_id ?? roundId,
        message_id: streamMessage.message_id ?? sourceMessageId,
        is_placeholder: false,
        is_streaming: false,
        status: replyStatus,
        detail: replyDetail,
      };
    }
    return next;
  }

  return [
    ...completeStreamingRoundEvents(current, roundId, sourceMessageId),
    {
      id: `assistant_final_${Date.now()}`,
      kind: 'message',
      role: 'assistant',
      text: reply,
      timestamp,
      timestamp_ms: Date.now(),
      round_id: roundId,
      message_id: sourceMessageId,
      is_streaming: false,
      status: replyStatus,
      detail: replyDetail,
    },
  ];
}

type WebSearchMeta = {
  id: string;
  timestamp: string;
  timestamp_ms: number;
  round_id?: string;
  message_id?: string;
  seq?: number;
};

function webSearchEventId(payload: Record<string, unknown>): string {
  const item = isPlainRecord(payload.item) ? payload.item : null;
  const itemId = typeof item?.id === 'string' ? item.id : '';
  const annotationKey = Array.isArray(payload.annotations) ? String(payload.annotations.length) : '';
  const roundId = typeof payload.round_id === 'string' ? payload.round_id : 'active';
  return `web_search_${roundId}_${itemId || annotationKey || Date.now()}`;
}

function webSearchEventFromPayload(payload: Record<string, unknown>, meta: WebSearchMeta): ToolCallEvent | null {
  if (payload.category !== 'web_search') {
    return null;
  }
  const item = isPlainRecord(payload.item) ? payload.item : null;
  const action = isPlainRecord(item?.action) ? item.action : null;
  const annotations = Array.isArray(payload.annotations)
    ? payload.annotations.filter(isPlainRecord)
    : [];

  if (action) {
    const actionType = typeof action.type === 'string' ? action.type : 'search';
    const query = typeof action.query === 'string' ? action.query : '';
    const url = typeof action.url === 'string' ? action.url : '';
    const queries = Array.isArray(action.queries)
      ? action.queries.filter((queryItem): queryItem is string => typeof queryItem === 'string')
      : [];
    const detailLines = [
      query ? `query: ${query}` : '',
      url ? `url: ${url}` : '',
      queries.length > 0 ? `queries:\n${queries.map((item) => `- ${item}`).join('\n')}` : '',
    ].filter(Boolean);
    return {
      id: meta.id,
      kind: 'tool_call',
      timestamp: meta.timestamp,
      timestamp_ms: meta.timestamp_ms,
      round_id: meta.round_id,
      message_id: meta.message_id,
      seq: meta.seq,
      status: item?.status === 'failed' ? 'failed' : 'done',
      scope: 'main',
      tool: 'web_search',
      title: actionType === 'open_page' ? '打开网页' : '网页搜索',
      summary: actionType === 'open_page' ? `打开网页：${url || '未知页面'}` : `搜索：${query || queries[0] || '网页'}`,
      detail: detailLines.join('\n\n') || formatToolDetail(payload),
    };
  }

  if (annotations.length > 0) {
    const urls = annotations
      .map((annotation) => {
        const title = typeof annotation.title === 'string' ? annotation.title : '引用网页';
        const url = typeof annotation.url === 'string' ? annotation.url : '';
        return url ? `- ${title}\n  ${url}` : `- ${title}`;
      })
      .join('\n');
    return {
      id: meta.id,
      kind: 'tool_call',
      timestamp: meta.timestamp,
      timestamp_ms: meta.timestamp_ms,
      round_id: meta.round_id,
      message_id: meta.message_id,
      seq: meta.seq,
      status: 'done',
      scope: 'main',
      tool: 'web_search',
      title: '引用网页',
      summary: `引用了 ${annotations.length} 个网页`,
      detail: urls,
    };
  }

  return null;
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
