import { ClipboardEvent, ReactNode, useEffect, useMemo, useState } from 'react';
import type { ChatEvent, ChatMessageEvent, ToolCallEvent } from '../../types';
import { TimelineList } from '../timeline/TimelineList';
import { MarkdownContent } from '../../components/MarkdownContent';

type ChatThreadProps = {
  events: ChatEvent[];
};

type StatusEvent = Extract<
  ChatEvent,
  { kind: 'round_status' | 'context_status' | 'quality_enhancement_status' | 'llm_retry_status' }
>;

type TraceBlock =
  | { kind: 'message'; event: ChatMessageEvent }
  | { kind: 'process'; items: ToolCallEvent[] }
  | { kind: 'status'; event: StatusEvent };

type RenderBlock =
  | { kind: 'user_message'; event: ChatMessageEvent }
  | {
      kind: 'assistant_round';
      roundKey: string;
      finalMessage: ChatMessageEvent;
      traceBlocks: TraceBlock[];
      durationLabel: string | null;
    }
  | { kind: 'process'; items: ToolCallEvent[] }
  | { kind: 'status'; event: StatusEvent };

export function ChatThread({ events }: ChatThreadProps) {
  const [hoveredAssistantRound, setHoveredAssistantRound] = useState<string | null>(null);
  const [openTraceRounds, setOpenTraceRounds] = useState<Set<string>>(() => new Set());
  const hasLiveRound = useMemo(() => hasLiveThreadActivity(events), [events]);
  const liveNowMs = useLiveNow(hasLiveRound);
  const blocks = useMemo(() => buildRenderBlocks(events, liveNowMs), [events, liveNowMs]);

  return (
    <section className="chat-thread">
      {blocks.map((block, index) => {
        if (block.kind === 'user_message') {
          return renderMessage(block.event, 'user');
        }

        if (block.kind === 'assistant_round') {
          const traceKey = `${block.roundKey}_${index}`;
          const isTimeVisible = hoveredAssistantRound === block.roundKey;
          const hoverProps = {
            onMouseEnter: () => setHoveredAssistantRound(block.roundKey),
            onMouseLeave: () => setHoveredAssistantRound((current) => (current === block.roundKey ? null : current)),
            onFocus: () => setHoveredAssistantRound(block.roundKey),
            onBlur: () => setHoveredAssistantRound((current) => (current === block.roundKey ? null : current)),
          };
          const isTraceOpen = openTraceRounds.has(traceKey);
          const toggleTrace = () => {
            setOpenTraceRounds((current) => {
              const next = new Set(current);
              if (next.has(traceKey)) {
                next.delete(traceKey);
              } else {
                next.add(traceKey);
              }
              return next;
            });
          };

          return (
            <div key={`assistant_round_${block.roundKey}_${index}`} className="assistant-round" {...hoverProps}>
              {block.traceBlocks.length > 0 || block.durationLabel ? (
                <div className={`assistant-round-trace ${isTraceOpen ? 'open' : ''}`}>
                  <button
                    type="button"
                    className="assistant-round-trace-summary"
                    aria-expanded={isTraceOpen}
                    onClick={toggleTrace}
                  >
                    <span>过程记录</span>
                    <small>{formatTraceSummary(block.traceBlocks, block.durationLabel)}</small>
                  </button>
                  {isTraceOpen && block.traceBlocks.length > 0 ? (
                    <div className="assistant-round-trace-body">
                      {block.traceBlocks.map((traceBlock, traceIndex) =>
                        renderTraceBlock(traceBlock, traceIndex, liveNowMs),
                      )}
                    </div>
                  ) : null}
                </div>
              ) : null}
              {renderMessage(block.finalMessage, 'assistant', isTimeVisible)}
            </div>
          );
        }

        if (block.kind === 'process') {
          return <TimelineList key={`process_${index}`} items={block.items} />;
        }

        return renderStatus(block.event, undefined, liveNowMs);
      })}
    </section>
  );
}

function renderMessage(event: ChatMessageEvent, role: ChatMessageEvent['role'], showRoundTime = false) {
  const shouldRenderBubble = !(role === 'assistant' && event.status === 'failed');
  return (
    <article
      key={event.id}
      className={[
        'message-row',
        role,
        showRoundTime ? 'show-round-time' : '',
        !shouldRenderBubble ? 'note-only' : '',
      ].filter(Boolean).join(' ')}
    >
      {shouldRenderBubble ? (
        <div
          className={`message-bubble ${role}`}
          onCopy={role === 'user' ? (copyEvent) => copyPlainUserMessage(copyEvent, event.text ?? '') : undefined}
        >
          {role === 'user' ? (
            <div className="plain-message-text">{event.text ?? ''}</div>
          ) : event.is_placeholder && event.is_streaming ? (
            <ThinkingIndicator />
          ) : (
            <div className="markdown-body">
              <MarkdownContent>{event.text ?? ''}</MarkdownContent>
            </div>
          )}
        </div>
      ) : null}
      {role === 'assistant' && event.status ? (
        <div className={`message-note ${event.status}`}>
          {event.status === 'interrupted'
            ? event.detail ?? '输出中断，已保留当前内容。'
            : event.detail ?? '本轮未完成。'}
        </div>
      ) : null}
      <time>{event.timestamp}</time>
    </article>
  );
}

function ThinkingIndicator() {
  return (
    <div className="thinking-indicator" aria-live="polite" aria-label="Thinking">
      <span className="thinking-pulse" aria-hidden="true" />
      <span className="thinking-label">Thinking</span>
      <span className="thinking-wave" aria-hidden="true">
        <span />
        <span />
        <span />
        <span />
      </span>
    </div>
  );
}

function useLiveNow(active: boolean) {
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    if (!active) {
      return;
    }
    setNowMs(Date.now());
    const timerId = window.setInterval(() => {
      setNowMs(Date.now());
    }, 1000);
    return () => window.clearInterval(timerId);
  }, [active]);

  return active ? nowMs : null;
}

function copyPlainUserMessage(event: ClipboardEvent<HTMLDivElement>, fallbackText: string) {
  const selectedText = window.getSelection()?.toString();
  const text = selectedText && selectedText.trim() ? stripSelectionBoundaryNewlines(selectedText) : fallbackText;
  event.clipboardData.setData('text/plain', text);
  event.preventDefault();
}

function stripSelectionBoundaryNewlines(value: string) {
  return value.replace(/^(?:\r?\n)+/, '').replace(/(?:\r?\n)+$/, '');
}

function renderTraceBlock(block: TraceBlock, index: number, liveNowMs: number | null): ReactNode {
  if (block.kind === 'message') {
    return (
      <article key={`trace_message_${block.event.id}_${index}`} className="trace-message">
        <div className="markdown-body">
          <MarkdownContent>{block.event.text ?? ''}</MarkdownContent>
        </div>
      </article>
    );
  }

  if (block.kind === 'process') {
    return <TimelineList key={`trace_process_${index}`} items={block.items} label="工具调用" defaultOpen />;
  }

  return renderStatus(block.event, `trace_status_${block.event.id}_${index}`, liveNowMs);
}

function renderStatus(event: StatusEvent, key = event.id, liveNowMs: number | null = null) {
  if (event.kind === 'llm_retry_status') {
    const label = formatLLMRetryLabel(event, liveNowMs);
    const status =
      event.status === 'failed' ? 'failed' : event.status === 'done' ? 'done' : 'running';
    return renderProcessStatusDivider({
      key,
      className: 'llm-retry-status',
      status,
      label,
      detail: event.detail,
      ariaLabel: label,
    });
  }

  if (event.kind === 'quality_enhancement_status') {
    const label =
      event.status === 'failed' ? '未增强' : event.status === 'done' ? '已增强' : '增强中';
    return renderProcessStatusDivider({
      key,
      className: `quality-enhancement-status ${event.phase}`,
      status: event.status,
      label,
      progress: `${event.progress}%`,
      detail: event.detail,
      ariaLabel: `${event.summary}，${event.progress}%`,
    });
  }

  if (event.kind === 'context_status') {
    const label =
      event.status === 'failed' ? '压缩失败' : event.status === 'done' ? event.summary : '压缩中';
    return renderProcessStatusDivider({
      key,
      className: 'context-divider',
      status: event.status,
      label,
      detail: event.detail,
      ariaLabel: event.summary,
    });
  }

  return (
    <article key={key} className={`round-status ${event.status}`}>
      <span>{event.summary}</span>
      {event.detail ? <small>{event.detail}</small> : null}
    </article>
  );
}

function renderProcessStatusDivider({
  key,
  className,
  status,
  label,
  progress,
  detail,
  ariaLabel,
}: {
  key: string;
  className: string;
  status: 'running' | 'done' | 'failed';
  label: string;
  progress?: string;
  detail?: string;
  ariaLabel: string;
}) {
  return (
    <article
      key={key}
      className={`process-status-divider ${className} ${status}`}
      aria-label={ariaLabel}
    >
      <div className="process-status-row">
        <span className="process-status-label">{label}</span>
        {progress ? <b>{progress}</b> : null}
      </div>
      {detail ? <small>{detail}</small> : null}
    </article>
  );
}

function buildRenderBlocks(events: ChatEvent[], liveNowMs: number | null): RenderBlock[] {
  const blocks: RenderBlock[] = [];
  let pendingRound: {
    roundKey: string;
    finalMessage: ChatMessageEvent | null;
    traceBlocks: TraceBlock[];
    startedAtMs: number | null;
    isStreaming: boolean;
  } | null = null;
  let pendingProcess: ToolCallEvent[] = [];
  const roundStartTimes = new Map<string, number>();
  const roundAliases = new Map<string, string>();

  const rememberRoundAlias = (roundId?: string, messageId?: string, fallbackId?: string) => {
    const canonical = roundId ?? messageId ?? fallbackId;
    if (!canonical) {
      return null;
    }
    roundAliases.set(canonical, canonical);
    if (roundId) {
      roundAliases.set(roundId, canonical);
    }
    if (messageId) {
      roundAliases.set(messageId, canonical);
    }
    return canonical;
  };

  const resolveRoundKey = (roundId?: string, messageId?: string, fallbackId?: string) => {
    const key = roundId ?? messageId ?? fallbackId;
    if (!key) {
      return null;
    }
    return roundAliases.get(key) ?? rememberRoundAlias(roundId, messageId, fallbackId);
  };

  const flushProcessToRound = () => {
    if (pendingProcess.length === 0) {
      return;
    }
    if (pendingRound) {
      pendingRound.traceBlocks.push({ kind: 'process', items: pendingProcess });
    } else {
      blocks.push({ kind: 'process', items: pendingProcess });
    }
    pendingProcess = [];
  };

  const flushRound = () => {
    flushProcessToRound();
    if (!pendingRound) {
      return;
    }

    if (!pendingRound.finalMessage) {
      for (const traceBlock of pendingRound.traceBlocks) {
        if (traceBlock.kind === 'process') {
          blocks.push({ kind: 'process', items: traceBlock.items });
        } else if (traceBlock.kind === 'status') {
          blocks.push({ kind: 'status', event: traceBlock.event });
        } else {
          blocks.push({
            kind: 'assistant_round',
            roundKey: pendingRound.roundKey,
            finalMessage: traceBlock.event,
            traceBlocks: [],
            durationLabel: null,
          });
        }
      }
      pendingRound = null;
      return;
    }

    blocks.push({
      kind: 'assistant_round',
      roundKey: pendingRound.roundKey,
      finalMessage: pendingRound.finalMessage,
      traceBlocks: pendingRound.traceBlocks,
      durationLabel: formatRoundDuration(
        pendingRound.traceBlocks,
        pendingRound.finalMessage,
        pendingRound.startedAtMs,
        pendingRound.isStreaming ? liveNowMs : null,
      ),
    });
    pendingRound = null;
  };

  for (const event of events) {
    if (event.kind === 'message' && event.role === 'user') {
      flushRound();
      blocks.push({ kind: 'user_message', event });
      const roundKey = rememberRoundAlias(event.round_id, event.message_id, event.id);
      if (roundKey && event.timestamp_ms) {
        roundStartTimes.set(roundKey, event.timestamp_ms);
      }
      continue;
    }

    if (event.kind === 'message' && event.role === 'assistant') {
      const roundKey = resolveRoundKey(event.round_id, event.message_id, event.id) ?? getAssistantRoundKey(event);
      if (!pendingRound || pendingRound.roundKey !== roundKey) {
        if (pendingRound) {
          flushRound();
        }
        pendingRound = {
          roundKey,
          finalMessage: null,
          traceBlocks: [],
          startedAtMs: roundStartTimes.get(roundKey) ?? null,
          isStreaming: false,
        };
      }
      if (pendingRound.finalMessage && !pendingRound.finalMessage.is_placeholder) {
        pendingRound.traceBlocks.push({ kind: 'message', event: pendingRound.finalMessage });
      }
      flushProcessToRound();
      pendingRound.finalMessage = event;
      pendingRound.isStreaming = event.is_streaming === true;
      continue;
    }

    if (event.kind === 'tool_call') {
      const roundKey = resolveRoundKey(event.round_id, event.message_id);
      if (roundKey && (!pendingRound || pendingRound.roundKey !== roundKey)) {
        if (pendingRound) {
          flushRound();
        }
        pendingRound = {
          roundKey,
          finalMessage: null,
          traceBlocks: [],
          startedAtMs: roundStartTimes.get(roundKey) ?? null,
          isStreaming: false,
        };
      }
      pendingProcess.push(event);
      continue;
    }

    if (event.kind === 'context_status') {
      const roundKey = resolveRoundKey(event.round_id, event.message_id);
      if (roundKey && (!pendingRound || pendingRound.roundKey !== roundKey)) {
        if (pendingRound) {
          flushRound();
        }
        pendingRound = {
          roundKey,
          finalMessage: null,
          traceBlocks: [],
          startedAtMs: roundStartTimes.get(roundKey) ?? null,
          isStreaming: false,
        };
      }
      flushProcessToRound();
      if (pendingRound) {
        pendingRound.traceBlocks.push({ kind: 'status', event });
      } else {
        blocks.push({ kind: 'status', event });
      }
      continue;
    }

    if (event.kind === 'quality_enhancement_status') {
      flushRound();
      blocks.push({ kind: 'status', event });
      continue;
    }

    if (event.kind === 'llm_retry_status') {
      flushRound();
      blocks.push({ kind: 'status', event });
      continue;
    }

    if (event.kind === 'round_status') {
      const roundKey = resolveRoundKey(event.round_id, event.message_id);
      if (roundKey && (!pendingRound || pendingRound.roundKey !== roundKey)) {
        if (pendingRound) {
          flushRound();
        }
        pendingRound = {
          roundKey,
          finalMessage: null,
          traceBlocks: [],
          startedAtMs: roundStartTimes.get(roundKey) ?? null,
          isStreaming: false,
        };
      }
      flushProcessToRound();
      if (pendingRound) {
        pendingRound.traceBlocks.push({ kind: 'status', event });
      } else {
        blocks.push({ kind: 'status', event });
      }
    }
  }

  flushRound();
  return blocks;
}

function hasLiveThreadActivity(events: ChatEvent[]) {
  return events.some(
    (event) =>
      (event.kind === 'message' && event.role === 'assistant' && event.is_streaming) ||
      (event.kind === 'llm_retry_status' && (event.status === 'waiting' || event.status === 'retrying')),
  );
}

function formatLLMRetryLabel(event: Extract<ChatEvent, { kind: 'llm_retry_status' }>, liveNowMs: number | null) {
  if (event.status === 'done') {
    return `模型连接已恢复，完成第 ${event.retry_index}/${event.max_retries} 次重试`;
  }
  if (event.status === 'failed') {
    return `模型连接失败，第 ${event.retry_index}/${event.max_retries} 次重试后仍未恢复`;
  }
  if (event.status === 'retrying') {
    return `${event.reason}，正在进行第 ${event.retry_index}/${event.max_retries} 次重试`;
  }

  const fallbackRemaining = Math.ceil(event.retry_after_seconds);
  const retryAtMs = event.retry_at_ms;
  const remainingSeconds =
    retryAtMs && liveNowMs
      ? Math.max(0, Math.ceil((retryAtMs - liveNowMs) / 1000))
      : Math.max(0, fallbackRemaining);
  if (remainingSeconds <= 0) {
    return `${event.reason}，正在进行第 ${event.retry_index}/${event.max_retries} 次重试`;
  }
  return `${event.reason}，${remainingSeconds} 秒后进行第 ${event.retry_index}/${event.max_retries} 次重试`;
}

function getAssistantRoundKey(event: ChatMessageEvent) {
  return event.round_id ?? event.message_id ?? event.id;
}

function formatTraceSummary(traceBlocks: TraceBlock[], durationLabel: string | null) {
  let messageCount = 0;
  let toolCount = 0;
  for (const block of traceBlocks) {
    if (block.kind === 'message') {
      messageCount += 1;
    }
    if (block.kind === 'process') {
      toolCount += block.items.length;
    }
  }

  const parts: string[] = [];
  if (messageCount > 0) {
    parts.push(`${messageCount} 条中间输出`);
  }
  if (toolCount > 0) {
    parts.push(`${toolCount} 个工具调用`);
  }
  if (durationLabel) {
    parts.push(`处理 ${durationLabel}`);
  }
  return parts.join(' / ');
}

function formatRoundDuration(
  traceBlocks: TraceBlock[],
  finalMessage: ChatMessageEvent,
  startedAtMs: number | null,
  activeNowMs: number | null,
) {
  const traceTimes = collectTraceTimes(traceBlocks);
  const times = [...traceTimes];
  if (finalMessage.timestamp_ms) {
    times.push(finalMessage.timestamp_ms);
  }
  if (startedAtMs) {
    times.push(startedAtMs);
  }
  if (times.length < 2 && !activeNowMs) {
    return null;
  }

  const start = traceTimes.length > 0 ? Math.min(...traceTimes) : startedAtMs ?? Math.min(...times);
  const end = activeNowMs ?? Math.max(...times);
  const totalSeconds = Math.max(0, Math.round((end - start) / 1000));
  return formatDuration(totalSeconds);
}

function collectTraceTimes(traceBlocks: TraceBlock[]) {
  const times: number[] = [];
  for (const block of traceBlocks) {
    if (block.kind === 'message' && block.event.timestamp_ms) {
      times.push(block.event.timestamp_ms);
    }
    if (block.kind === 'status' && block.event.timestamp_ms) {
      times.push(block.event.timestamp_ms);
    }
    if (block.kind === 'process') {
      for (const item of block.items) {
        if (item.timestamp_ms) {
          times.push(item.timestamp_ms);
        }
      }
    }
  }
  return times;
}

function formatDuration(totalSeconds: number) {
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  if (hours > 0) {
    return `${hours}h ${minutes}m ${seconds}s`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds}s`;
  }
  return `${seconds}s`;
}
