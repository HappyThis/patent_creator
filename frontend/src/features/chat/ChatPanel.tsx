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
  | { kind: 'process'; items: ProcessEvent[] };

function buildRenderBlocks(events: ChatEvent[]): RenderBlock[] {
  const blocks: RenderBlock[] = [];
  let processBuffer: ProcessEvent[] = [];

  const flushProcess = () => {
    if (processBuffer.length > 0) {
      const toolCalls = processBuffer.filter((item) => item.kind === 'tool_call');
      if (toolCalls.length === 0) {
        for (const item of processBuffer) {
          blocks.push({
            kind: 'message',
            event: {
              id: item.id,
              kind: 'message',
              role: 'assistant',
              text: item.summary ?? '',
              timestamp: item.timestamp ?? '',
            },
          });
        }
        processBuffer = [];
        return;
      }
      blocks.push({ kind: 'process', items: processBuffer });
      processBuffer = [];
    }
  };

  for (const event of events) {
    if (event.kind === 'message') {
      flushProcess();
      blocks.push({ kind: 'message', event });
      continue;
    }

    processBuffer.push(event);
  }

  flushProcess();
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

            return <TimelineList key={`process_${index}`} items={block.items} />;
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
