import { ReactNode, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { ChatEvent, ChatMessageEvent, ProcessEvent } from '../../types';
import { TimelineList } from '../timeline/TimelineList';

type ChatThreadProps = {
  events: ChatEvent[];
};

type StatusEvent = Extract<ChatEvent, { kind: 'round_status' | 'context_status' }>;

type TraceBlock =
  | { kind: 'message'; event: ChatMessageEvent }
  | { kind: 'process'; items: ProcessEvent[] }
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
  | { kind: 'process'; items: ProcessEvent[] }
  | { kind: 'status'; event: StatusEvent };

export function ChatThread({ events }: ChatThreadProps) {
  const [hoveredAssistantRound, setHoveredAssistantRound] = useState<string | null>(null);
  const blocks = buildRenderBlocks(events);

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
              {block.traceBlocks.length > 0 ? (
                <details className="assistant-round-trace">
                  <summary>
                    <span>过程记录</span>
                    <small>{formatTraceSummary(block.traceBlocks, block.durationLabel)}</small>
                  </summary>
                  <div className="assistant-round-trace-body">
                    {block.traceBlocks.map((traceBlock, traceIndex) => renderTraceBlock(traceBlock, traceIndex))}
                  </div>
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
      <div className={`message-bubble ${role}`}>
        <div className="markdown-body">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              a: ({ node: _node, ...props }) => (
                <a {...props} target="_blank" rel="noopener noreferrer" />
              ),
            }}
          >
            {event.text ?? ''}
          </ReactMarkdown>
        </div>
      </div>
      <time>{event.timestamp}</time>
    </article>
  );
}

function renderTraceBlock(block: TraceBlock, index: number): ReactNode {
  if (block.kind === 'message') {
    return (
      <article key={`trace_message_${block.event.id}_${index}`} className="trace-message">
        <div className="markdown-body">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{block.event.text ?? ''}</ReactMarkdown>
        </div>
      </article>
    );
  }

  if (block.kind === 'process') {
    return <TimelineList key={`trace_process_${index}`} items={block.items} />;
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

function buildRenderBlocks(events: ChatEvent[]): RenderBlock[] {
  const blocks: RenderBlock[] = [];
  let pendingRound: {
    roundKey: string;
    finalMessage: ChatMessageEvent | null;
    traceBlocks: TraceBlock[];
  } | null = null;
  let pendingProcess: ProcessEvent[] = [];

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
      pendingRound = null;
      return;
    }

    blocks.push({
      kind: 'assistant_round',
      roundKey: pendingRound.roundKey,
      finalMessage: pendingRound.finalMessage,
      traceBlocks: pendingRound.traceBlocks,
      durationLabel: formatRoundDuration(pendingRound.traceBlocks, pendingRound.finalMessage),
    });
    pendingRound = null;
  };

  for (const event of events) {
    if (event.kind === 'message' && event.role === 'user') {
      flushRound();
      blocks.push({ kind: 'user_message', event });
      continue;
    }

    if (event.kind === 'message' && event.role === 'assistant') {
      const roundKey = getAssistantRoundKey(event);
      if (!pendingRound || pendingRound.roundKey !== roundKey) {
        flushRound();
        pendingRound = { roundKey, finalMessage: null, traceBlocks: [] };
      }
      if (pendingRound.finalMessage) {
        pendingRound.traceBlocks.push({ kind: 'message', event: pendingRound.finalMessage });
      }
      flushProcessToRound();
      pendingRound.finalMessage = event;
      continue;
    }

    if (event.kind === 'tool_call') {
      pendingProcess.push(event);
      continue;
    }

    if (event.kind === 'round_status' || event.kind === 'context_status') {
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

function formatRoundDuration(traceBlocks: TraceBlock[], finalMessage: ChatMessageEvent) {
  const times = collectTraceTimes(traceBlocks);
  if (finalMessage.timestamp_ms) {
    times.push(finalMessage.timestamp_ms);
  }
  if (times.length < 2) {
    return null;
  }

  const start = Math.min(...times);
  const end = Math.max(...times);
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