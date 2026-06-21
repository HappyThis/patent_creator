import { ClipboardEvent, ReactNode, useEffect, useMemo, useState } from 'react';
import type { ChatEvent, ChatMessageEvent, ToolCallEvent } from '../../types';
import { TimelineList } from '../timeline/TimelineList';
import { MarkdownContent } from '../../components/MarkdownContent';

type ChatThreadProps = {
  events: ChatEvent[];
};

type StatusEvent = Extract<ChatEvent, { kind: 'round_status' | 'context_status' }>;

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
  const hasLiveRound = useMemo(() => hasLiveAssistantRound(events), [events]);
  const liveNowMs = useLiveNow(hasLiveRound);
  const blocks = useMemo(() => buildRenderBlocks(events, liveNowMs), [events, liveNowMs]);

  return (
    <section className="chat-thread">
      {blocks.map((block, index) => {
        if (block.kind === 'user_message') {
          return renderMessage(block.event, 'user');
        }

        if (block.kind === 'assistant_round') {
          const isTimeVisible = hoveredAssistantRound === block.roundKey;
          const hoverProps = {
            onMouseEnter: () => setHoveredAssistantRound(block.roundKey),
            onMouseLeave: () => setHoveredAssistantRound((current) => (current === block.roundKey ? null : current)),
            onFocus: () => setHoveredAssistantRound(block.roundKey),
            onBlur: () => setHoveredAssistantRound((current) => (current === block.roundKey ? null : current)),
          };

          return (
            <div key={`assistant_round_${block.roundKey}_${index}`} className="assistant-round" {...hoverProps}>
              {block.traceBlocks.length > 0 || block.durationLabel ? (
                <details className="assistant-round-trace">
                  <summary>
                    <span>过程记录</span>
                    <small>{formatTraceSummary(block.traceBlocks, block.durationLabel)}</small>
                  </summary>
                  {block.traceBlocks.length > 0 ? (
                    <div className="assistant-round-trace-body">
                      {block.traceBlocks.map((traceBlock, traceIndex) => renderTraceBlock(traceBlock, traceIndex))}
                    </div>
                  ) : null}
                </details>
              ) : null}
              {renderMessage(block.finalMessage, 'assistant', isTimeVisible)}
            </div>
          );
        }

        if (block.kind === 'process') {
          return <TimelineList key={`process_${index}`} items={block.items} />;
        }

        return renderStatus(block.event);
      })}
    </section>
  );
}

function renderMessage(event: ChatMessageEvent, role: ChatMessageEvent['role'], showRoundTime = false) {
  return (
    <article
      key={event.id}
      className={[
        'message-row',
        role,
        showRoundTime ? 'show-round-time' : '',
      ].filter(Boolean).join(' ')}
    >
      <div
        className={`message-bubble ${role}`}
        onCopy={role === 'user' ? (copyEvent) => copyPlainUserMessage(copyEvent, event.text ?? '') : undefined}
      >
        {role === 'user' ? (
          <div className="plain-message-text">{event.text ?? ''}</div>
        ) : (
          <div className="markdown-body">
            <MarkdownContent>{event.text ?? ''}</MarkdownContent>
          </div>
        )}
      </div>
      <time>{event.timestamp}</time>
    </article>
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

function renderTraceBlock(block: TraceBlock, index: number): ReactNode {
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

  return renderStatus(block.event, `trace_status_${block.event.id}_${index}`);
}

function renderStatus(event: StatusEvent, key = event.id) {
  if (event.kind === 'context_status') {
    return (
      <article key={key} className={`context-divider ${event.status}`}>
        <span>
          {event.summary}
          {event.detail ? <small>{event.detail}</small> : null}
        </span>
      </article>
    );
  }

  return (
    <article key={key} className={`round-status ${event.status}`}>
      <span>{event.summary}</span>
      {event.detail ? <small>{event.detail}</small> : null}
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
      const roundKey = event.round_id ?? event.message_id ?? event.id;
      if (event.timestamp_ms) {
        roundStartTimes.set(roundKey, event.timestamp_ms);
      }
      continue;
    }

    if (event.kind === 'message' && event.role === 'assistant') {
      const roundKey = getAssistantRoundKey(event);
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
      if (pendingRound.finalMessage) {
        pendingRound.traceBlocks.push({ kind: 'message', event: pendingRound.finalMessage });
      }
      flushProcessToRound();
      pendingRound.finalMessage = event;
      pendingRound.isStreaming = event.is_streaming === true;
      continue;
    }

    if (event.kind === 'tool_call') {
      const roundKey = event.round_id ?? event.message_id;
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
      flushRound();
      blocks.push({ kind: 'status', event });
      continue;
    }

    if (event.kind === 'round_status') {
      const roundKey = event.round_id ?? event.message_id;
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

function hasLiveAssistantRound(events: ChatEvent[]) {
  return events.some((event) => event.kind === 'message' && event.role === 'assistant' && event.is_streaming);
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
  const times = collectTraceTimes(traceBlocks);
  if (finalMessage.timestamp_ms) {
    times.push(finalMessage.timestamp_ms);
  }
  if (startedAtMs) {
    times.push(startedAtMs);
  }
  if (times.length < 2 && !activeNowMs) {
    return null;
  }

  const start = startedAtMs ?? Math.min(...times);
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
