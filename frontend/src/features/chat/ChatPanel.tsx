import { CompositionEvent, KeyboardEvent, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ChatEvent, ChatMessageEvent, ProcessEvent, SessionTab } from '../../types';
import { TimelineList } from '../timeline/TimelineList';

type ChatPanelProps = {
  sessionTabs: SessionTab[];
  events: ChatEvent[];
  composer: string;
  isBusy: boolean;
  onComposerChange: (value: string) => void;
  onSubmit: () => void;
  onSessionSelect: (session_id: string) => void;
};

type RenderBlock =
  | { kind: 'message'; event: ChatMessageEvent }
  | { kind: 'process'; items: ProcessEvent[] }
  | { kind: 'round_status'; event: Extract<ChatEvent, { kind: 'round_status' }> };

type ChatRound = {
  id: string;
  user?: ChatMessageEvent;
  processItems: ProcessEvent[];
  assistantMessages: ChatMessageEvent[];
  statuses: Extract<ChatEvent, { kind: 'round_status' }>[];
};

function buildRenderBlocks(events: ChatEvent[]): RenderBlock[] {
  const blocks: RenderBlock[] = [];
  const rounds = new Map<string, ChatRound>();
  const roundOrder: string[] = [];
  const looseEvents: ChatEvent[] = [];

  for (const event of events) {
    if (!event.round_id) {
      looseEvents.push(event);
      continue;
    }

    if (!rounds.has(event.round_id)) {
      rounds.set(event.round_id, {
        id: event.round_id,
        processItems: [],
        assistantMessages: [],
        statuses: [],
      });
      roundOrder.push(event.round_id);
    }

    const round = rounds.get(event.round_id);
    if (!round) {
      continue;
    }

    if (event.kind === 'message' && event.role === 'user') {
      round.user = event;
      continue;
    }

    if (event.kind === 'message' && event.role === 'assistant') {
      round.assistantMessages.push(event);
      continue;
    }

    if (event.kind === 'agent_output' || event.kind === 'tool_call') {
      round.processItems.push(event);
      continue;
    }

    if (event.kind === 'round_status') {
      round.statuses.push(event);
    }
  }

  for (const event of looseEvents) {
    if (event.kind === 'message') {
      blocks.push({ kind: 'message', event });
    } else if (event.kind === 'round_status') {
      blocks.push({ kind: 'round_status', event });
    } else if (event.kind === 'tool_call') {
      blocks.push({ kind: 'process', items: [event] });
    } else if (event.summary) {
      blocks.push({
        kind: 'message',
        event: {
          id: event.id,
          kind: 'message',
          role: 'assistant',
          text: event.summary,
          timestamp: event.timestamp ?? '',
        },
      });
    }
  }

  for (const roundId of roundOrder) {
    const round = rounds.get(roundId);
    if (!round) {
      continue;
    }

    if (round.user) {
      blocks.push({ kind: 'message', event: round.user });
    }

    const toolCalls = round.processItems.filter((item) => item.kind === 'tool_call');
    if (toolCalls.length > 0) {
      blocks.push({ kind: 'process', items: round.processItems });
    } else {
      for (const item of round.processItems) {
        blocks.push({
          kind: 'message',
          event: {
            id: item.id,
            kind: 'message',
            role: 'assistant',
            text: item.summary ?? '',
            timestamp: item.timestamp ?? '',
            round_id: round.id,
          },
        });
      }
    }

    for (const message of round.assistantMessages) {
      blocks.push({ kind: 'message', event: message });
    }

    for (const status of round.statuses) {
      blocks.push({ kind: 'round_status', event: status });
    }
  }

  return blocks;
}

export function ChatPanel({
  sessionTabs,
  events,
  composer,
  isBusy,
  onComposerChange,
  onSubmit,
  onSessionSelect,
}: ChatPanelProps) {
  const [isComposing, setIsComposing] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const handleCompositionStart = (_event: CompositionEvent<HTMLTextAreaElement>) => {
    setIsComposing(true);
  };

  const handleCompositionEnd = (_event: CompositionEvent<HTMLTextAreaElement>) => {
    setIsComposing(false);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Enter' || event.shiftKey) {
      return;
    }
    // 输入法组合中，回车用于确认候选词，不应触发发送。
    // isComposing 覆盖绝大多数浏览器；event.nativeEvent.isComposing / keyCode===229 兜底。
    if (isComposing || event.nativeEvent.isComposing || event.keyCode === 229) {
      return;
    }
    event.preventDefault();
    onSubmit();
  };

  const blocks = buildRenderBlocks(events);

  useEffect(() => {
    // 每次事件更新后滚动到底部，确保最新消息可见。
    const container = scrollRef.current;
    if (!container) return;
    container.scrollTop = container.scrollHeight;
  }, [events, sessionTabs]);

  return (
    <aside className="chat-panel">
      <div className="chat-header">
        <div className="session-tab-strip" role="tablist" aria-label="Sessions">
          {sessionTabs.map((tab) => (
            <button
              key={tab.session_id}
              className={`session-card-tab ${tab.active ? 'active' : ''}`}
              role="tab"
              aria-selected={tab.active}
              onClick={() => onSessionSelect(tab.session_id)}
              disabled={isBusy && !tab.active}
            >
              <span className="session-card-title">{tab.title}</span>
              {tab.subtitle ? <span className="session-card-subtitle">{tab.subtitle}</span> : null}
            </button>
          ))}
        </div>
      </div>

      <div className="chat-scroll" ref={scrollRef}>
        <section className="chat-thread">
          {blocks.map((block, index) => {
            if (block.kind === 'message') {
              const { event } = block;
              return (
                <article key={event.id} className={`message-row ${event.role}`}>
                  <div className={`message-bubble ${event.role}`}>
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

            if (block.kind === 'process') {
              return <TimelineList key={`process_${index}`} items={block.items} />;
            }

            return (
              <article key={block.event.id} className={`round-status ${block.event.status}`}>
                <span>{block.event.summary}</span>
                {block.event.detail ? <small>{block.event.detail}</small> : null}
              </article>
            );
          })}
        </section>
      </div>

      <div className="composer">
        <div className="composer-inline">
          <textarea
            value={composer}
            onChange={(event) => onComposerChange(event.target.value)}
            onKeyDown={handleKeyDown}
            onCompositionStart={handleCompositionStart}
            onCompositionEnd={handleCompositionEnd}
            placeholder="请输入需求"
            rows={3}
            disabled={isBusy}
          />
          <button
            className="composer-send-inline"
            onClick={onSubmit}
            disabled={isBusy || composer.trim().length === 0}
            aria-label="发送"
            title="发送"
          >
            ↑
          </button>
        </div>
      </div>
    </aside>
  );
}
